// Package groq corresponds to plugins/groq.py entirely: endpoint POST /groq
// (Flexible model routing per command type using Groq exclusively without Gemini).
package groq

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/netguard"
	"sunkenbot/internal/plugins"
	"sunkenbot/internal/session"
	"sunkenbot/internal/strutil"
)

// Description defines the plugin description.
const Description = "Groq AI Bot — Flexible Multi-Model Routing (Groq Only)"

const (
	// systemPrompt sets the default behavior and persona for the AI assistant.
	systemPrompt = `أنت "Sunken"، صاحب في غروب.
- رد بالدارجة الجزائرية دايما (درجة، واش، راك، خويا، بصح، كيفاش...)
- اقصر ما يمكن — جملة واحدة أو جملتين كافية
- بلا عناوين، بلا نقاط، بلا تنسيق
- إذا السؤال غبي رد بنكتة قصيرة
- إذا جاك صورة/صوت/فيديو وصفه مباشرة بلا مقدمات`
	// whisperModel specifies the model used for audio transcriptions.
	whisperModel = "whisper-large-v3"
	// ttsModel/ttsVoice specify the Groq Orpheus Arabic (Saudi dialect) text-to-speech model.
	ttsModel = "canopylabs/orpheus-arabic-saudi"
	ttsVoice = "fahad"
	// ttsMaxChars is Orpheus's documented per-request input limit; longer replies are split into chunks.
	ttsMaxChars = 190
)

// Flexible model lists for each command type, ordered by fallback preference.
var (
	textModels   = []string{"openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b"}
	visionModels = []string{"qwen/qwen3.6-27b", "meta-llama/llama-4-scout-17b-16e-instruct"}
)

// Service holds the HTTP clients and session store as explicit dependencies.
type Service struct {
	client   *http.Client
	dlClient *http.Client
	store    session.Store
}

// New creates and returns a new instance of the Groq service.
func New(client, dlClient *http.Client, store session.Store) *Service {
	return &Service{client: client, dlClient: dlClient, store: store}
}

// Name returns the unique identifier name of the plugin.
func (s *Service) Name() string { return "groq" }

// Routes registers the HTTP endpoints handled by this plugin.
func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/groq", Handler: httpx.Handle(s.handleGroq)},
	}
}

// groqKey retrieves the Groq API key from environment variables.
func groqKey() string { return os.Getenv("GROQ_API_KEY") }

// attachment represents incoming file payloads (images, audio, video).
type attachment struct {
	Kind        string `json:"kind"`
	URL         string `json:"url"`
	Base64      string `json:"base64"`
	ContentType string `json:"contentType"`
}

// parseAttachment safely extracts attachment data from raw JSON input.
func parseAttachment(raw any) *attachment {
	obj, ok := raw.(map[string]any)
	if !ok {
		return nil
	}
	return &attachment{
		Kind:        strutil.StringOr(obj["kind"], ""),
		URL:         strutil.StringOr(obj["url"], ""),
		Base64:      strutil.StringOr(obj["base64"], ""),
		ContentType: strutil.StringOr(obj["contentType"], ""),
	}
}

// handleGroq processes incoming HTTP POST requests to the /groq endpoint.
func (s *Service) handleGroq(r *http.Request) (map[string]any, error) {
	ctx := r.Context()

	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": strutil.Truncate(err.Error(), 200)},
		}
	}

	_, hasThreadID := body["thread_id"]
	_, hasPrompt := body["prompt"]

	// Route to session-managed mode if thread_id or prompt is present.
	if hasThreadID || hasPrompt {
		return s.handleSessionMode(ctx, body)
	}

	messages := parseMessages(body["messages"])
	if len(messages) == 0 {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "messages أو prompt مطلوب"},
		}
	}
	if !hasSystem(messages) {
		messages = append([]session.Message{{Role: "system", Content: systemPrompt}}, messages...)
	}

	att := parseAttachment(body["attachment"])
	last := messages[len(messages)-1]
	prompt := strings.TrimSpace(strutil.StringOr(body["prompt"], last.Content))

	// Dispatch request based on attachment type or standard text chat.
	reply, provider, err := s.dispatchAttachment(ctx, att, messages, prompt)
	if err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusServiceUnavailable,
			Body: map[string]any{"error": "فشلت خوادم Groq: " + strutil.Truncate(err.Error(), 100)},
		}
	}

	result := map[string]any{"reply": reply, "provider": provider}
	if wantsTTS, _ := body["tts"].(bool); wantsTTS {
		if audioB64, terr := s.groqTTS(ctx, reply); terr == nil {
			result["audio"] = audioB64
			result["audio_format"] = "wav"
		} else {
			result["tts_error"] = strutil.Truncate(terr.Error(), 150)
		}
	}
	return result, nil
}

// handleSessionMode manages chat history sessions and state clearing.
func (s *Service) handleSessionMode(ctx context.Context, body map[string]any) (map[string]any, error) {
	threadID := strutil.StringOr(body["thread_id"], "default")
	senderName := strutil.StringOr(body["sender_name"], "مستخدم")
	prompt := strings.TrimSpace(strutil.StringOr(body["prompt"], ""))
	doClear, _ := body["clear"].(bool)
	att := parseAttachment(body["attachment"])

	// Clear session history if requested.
	if doClear {
		_ = s.store.Clear(ctx, threadID)
		return map[string]any{"reply": "🧹 تم مسح ذاكرة المجموعة."}, nil
	}

	ctxMsgs, _ := s.store.Load(ctx, threadID)
	userContent := fmt.Sprintf("[%s]: %s", senderName, prompt)
	if prompt == "" {
		userContent = fmt.Sprintf("[%s]: ما هذا؟", senderName)
	}

	messages := append([]session.Message{{Role: "system", Content: systemPrompt}}, ctxMsgs...)
	messages = append(messages, session.Message{Role: "user", Content: userContent})

	reply, provider, err := s.dispatchAttachment(ctx, att, messages, prompt)
	if err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusServiceUnavailable,
			Body: map[string]any{"error": "فشلت خوادم Groq: " + strutil.Truncate(err.Error(), 100)},
		}
	}

	attLabel := ""
	if att != nil {
		attLabel = fmt.Sprintf("[%s] ", att.Kind)
	}
	userText := strings.TrimSpace(fmt.Sprintf("[%s]: %s%s", senderName, attLabel, prompt))

	// Save updated conversation history to the session store.
	newHistory := append(append([]session.Message{}, ctxMsgs...),
		session.Message{Role: "user", Content: userText},
		session.Message{Role: "assistant", Content: reply},
	)
	_ = s.store.Save(ctx, threadID, newHistory)

	result := map[string]any{"reply": reply, "provider": provider}
	if wantsTTS, _ := body["tts"].(bool); wantsTTS {
		if audioB64, terr := s.groqTTS(ctx, reply); terr == nil {
			result["audio"] = audioB64
			result["audio_format"] = "wav"
		} else {
			result["tts_error"] = strutil.Truncate(terr.Error(), 150)
		}
	}
	return result, nil
}

// dispatchAttachment routes the request to the correct handler depending on the attachment type.
func (s *Service) dispatchAttachment(ctx context.Context, att *attachment, messages []session.Message, prompt string) (reply, provider string, err error) {
	if att == nil {
		reply, err = s.tryModels(ctx, textModels, messages, 30*time.Second)
		return reply, "groq-text", err
	}

	switch att.Kind {
	case "image":
		b64 := att.Base64
		mime := att.ContentType
		if mime == "" {
			mime = "image/jpeg"
		}
		if b64 == "" && att.URL != "" {
			raw, b, ferr := s.fetchBase64(ctx, att.URL)
			if ferr != nil {
				return "", "", ferr
			}
			b64 = b
			mime = guessMime(att.URL, raw)
		}
		reply, err = s.groqVision(ctx, messages, b64, mime)
		return reply, "groq-vision", err

	case "audio":
		if att.URL == "" {
			break
		}
		raw, _, ferr := s.fetchBase64(ctx, att.URL)
		if ferr != nil {
			return "", "", ferr
		}
		mime := guessMime(att.URL, raw)
		reply, err = s.groqAudio(ctx, raw, mime, prompt)
		return reply, "groq-whisper", err

	case "video":
		if att.URL == "" {
			break
		}
		reply, err = s.processVideo(ctx, att.URL, prompt, messages)
		return reply, "groq-video", err
	}

	reply, err = s.tryModels(ctx, textModels, messages, 30*time.Second)
	return reply, "groq-text", err
}

// tryModels iterates through a list of models in descending priority until one succeeds.
func (s *Service) tryModels(ctx context.Context, models []string, messages []session.Message, timeout time.Duration) (string, error) {
	key := groqKey()
	if key == "" {
		return "", fmt.Errorf("NO_GROQ_KEY")
	}

	var lastErr error
	for _, model := range models {
		payload := map[string]any{
			"model":       model,
			"messages":    toChatMessages(messages),
			"max_tokens":  1024,
			"temperature": 0.7,
		}
		reply, err := s.postGroqChat(ctx, key, payload, timeout)
		if err == nil && reply != "" {
			return reply, nil
		}
		lastErr = err
	}
	if lastErr != nil {
		return "", lastErr
	}
	return "", fmt.Errorf("ALL_MODELS_FAILED")
}

const maxAttachmentBytes = 20 * 1024 * 1024 // 20MB limit for attachments

// fetchBase64 securely downloads an attachment from a URL and converts it to Base64.
func (s *Service) fetchBase64(ctx context.Context, url string) ([]byte, string, error) {
	raw, err := netguard.SafeFetch(ctx, s.dlClient, url, maxAttachmentBytes)
	if err != nil {
		return nil, "", fmt.Errorf("رابط المرفق مرفوض أو تعذّر جلبه: %w", err)
	}
	return raw, base64.StdEncoding.EncodeToString(raw), nil
}

// guessMime determines the MIME type of a file based on its file extension or raw header bytes.
func guessMime(url string, raw []byte) string {
	low := strings.ToLower(strings.SplitN(url, "?", 2)[0])
	switch {
	case strings.HasSuffix(low, ".png"):
		return "image/png"
	case strings.HasSuffix(low, ".gif"):
		return "image/gif"
	case strings.HasSuffix(low, ".webp"):
		return "image/webp"
	case strings.HasSuffix(low, ".mp3"):
		return "audio/mp3"
	case strings.HasSuffix(low, ".m4a"):
		return "audio/mp4"
	case strings.HasSuffix(low, ".ogg"):
		return "audio/ogg"
	case strings.HasSuffix(low, ".wav"):
		return "audio/wav"
	case strings.HasSuffix(low, ".mp4"):
		return "video/mp4"
	}
	switch {
	case len(raw) >= 4 && bytes.Equal(raw[:4], []byte{0x89, 'P', 'N', 'G'}):
		return "image/png"
	case len(raw) >= 3 && string(raw[:3]) == "GIF":
		return "image/gif"
	case len(raw) >= 2 && raw[0] == 0xFF && raw[1] == 0xD8:
		return "image/jpeg"
	case len(raw) >= 4 && string(raw[:4]) == "RIFF":
		return "audio/wav"
	case len(raw) >= 3 && string(raw[:3]) == "ID3":
		return "audio/mp3"
	}
	return "image/jpeg"
}

// groqVision handles image analysis requests by cycling through vision-capable models.
func (s *Service) groqVision(ctx context.Context, messages []session.Message, imgB64, mime string) (string, error) {
	key := groqKey()
	if key == "" {
		return "", fmt.Errorf("NO_GROQ_KEY")
	}

	chatMsgs := make([]any, 0, len(messages))
	for i, m := range messages {
		if i == len(messages)-1 && m.Role == "user" {
			text := m.Content
			if text == "" {
				text = "وصف هذه الصورة"
			}
			chatMsgs = append(chatMsgs, map[string]any{
				"role": "user",
				"content": []any{
					map[string]any{"type": "text", "text": text},
					map[string]any{"type": "image_url", "image_url": map[string]any{
						"url": fmt.Sprintf("data:%s;base64,%s", mime, imgB64),
					}},
				},
			})
		} else {
			chatMsgs = append(chatMsgs, map[string]any{"role": m.Role, "content": m.Content})
		}
	}

	var lastErr error
	for _, model := range visionModels {
		payload := map[string]any{
			"model":      model,
			"messages":   chatMsgs,
			"max_tokens": 1024,
		}
		reply, err := s.postGroqChat(ctx, key, payload, 45*time.Second)
		if err == nil && reply != "" {
			return reply, nil
		}
		lastErr = err
	}
	if lastErr != nil {
		return "", lastErr
	}
	return "", fmt.Errorf("ALL_VISION_MODELS_FAILED")
}

// groqAudio transcribes audio files using Whisper and processes text queries against the transcript.
func (s *Service) groqAudio(ctx context.Context, audioRaw []byte, mime, prompt string) (string, error) {
	key := groqKey()
	if key == "" {
		return "", fmt.Errorf("NO_GROQ_KEY")
	}

	extMap := map[string]string{
		"audio/mp3": "mp3", "audio/mpeg": "mp3", "audio/mp4": "m4a",
		"audio/m4a": "m4a", "audio/ogg": "ogg", "audio/wav": "wav",
		"audio/webm": "webm", "audio/flac": "flac",
	}
	ext := extMap[mime]
	if ext == "" {
		ext = "mp3"
	}

	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	part, err := mw.CreateFormFile("file", "audio."+ext)
	if err != nil {
		return "", err
	}
	if _, err := part.Write(audioRaw); err != nil {
		return "", err
	}
	_ = mw.WriteField("model", whisperModel)
	_ = mw.WriteField("language", "ar")
	_ = mw.WriteField("response_format", "text")
	mw.Close()

	ctxTimeout, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctxTimeout, http.MethodPost,
		"https://api.groq.com/openai/v1/audio/transcriptions", &buf)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", mw.FormDataContentType())

	resp, err := s.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("groq audio status %d: %s", resp.StatusCode, strutil.Truncate(string(respBody), 200))
	}

	transcription := strings.TrimSpace(string(respBody))
	if transcription == "" {
		return "", fmt.Errorf("EMPTY_TRANSCRIPTION")
	}

	followUp := strings.TrimSpace(prompt)
	if followUp == "" {
		followUp = "لخص ما قيل في هذا الصوت"
	}
	textMsgs := []session.Message{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: fmt.Sprintf("[تفريغ الصوت]: %s\n\nالسؤال: %s", transcription, followUp)},
	}
	reply, err := s.tryModels(ctx, textModels, textMsgs, 30*time.Second)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("🎵 التفريغ:\n%s\n\n💬 الرد:\n%s", transcription, reply), nil
}

// splitForTTS breaks text into chunks that respect Orpheus's per-request character limit,
// preferring to break at sentence or word boundaries rather than mid-word.
func splitForTTS(text string) []string {
	text = strings.TrimSpace(text)
	if text == "" {
		return nil
	}
	var chunks []string
	runes := []rune(text)
	for len(runes) > 0 {
		if len(runes) <= ttsMaxChars {
			if piece := strings.TrimSpace(string(runes)); piece != "" {
				chunks = append(chunks, piece)
			}
			break
		}
		cut := ttsMaxChars
		for i := ttsMaxChars; i > 0; i-- {
			switch runes[i] {
			case '.', '؟', '!', '،', '\n', ' ':
				cut = i + 1
			}
			if cut != ttsMaxChars {
				break
			}
		}
		if piece := strings.TrimSpace(string(runes[:cut])); piece != "" {
			chunks = append(chunks, piece)
		}
		runes = runes[cut:]
	}
	return chunks
}

// groqTTSChunk synthesizes a single chunk of text (must respect ttsMaxChars) into wav audio.
func (s *Service) groqTTSChunk(ctx context.Context, key, text string) ([]byte, error) {
	payload := map[string]any{
		"model":           ttsModel,
		"voice":           ttsVoice,
		"input":           text,
		"response_format": "wav",
	}
	buf, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}

	ctxTimeout, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctxTimeout, http.MethodPost,
		"https://api.groq.com/openai/v1/audio/speech", bytes.NewReader(buf))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")

	resp, err := s.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("groq tts status %d: %s", resp.StatusCode, strutil.Truncate(string(respBody), 200))
	}
	return respBody, nil
}

// groqTTS converts arbitrary-length text to speech, chunking as needed for Orpheus's
// character limit and stitching multi-chunk replies into a single wav file via ffmpeg.
// Returns base64-encoded wav audio.
func (s *Service) groqTTS(ctx context.Context, text string) (string, error) {
	key := groqKey()
	if key == "" {
		return "", fmt.Errorf("NO_GROQ_KEY")
	}

	chunks := splitForTTS(text)
	if len(chunks) == 0 {
		return "", fmt.Errorf("EMPTY_TTS_TEXT")
	}

	var tmpFiles []string
	defer func() {
		for _, f := range tmpFiles {
			os.Remove(f)
		}
	}()

	for i, c := range chunks {
		raw, err := s.groqTTSChunk(ctx, key, c)
		if err != nil {
			return "", fmt.Errorf("فشل توليد الصوت للمقطع %d: %w", i+1, err)
		}
		f, err := os.CreateTemp("", fmt.Sprintf("sunkenbot-tts-%d-*.wav", i))
		if err != nil {
			return "", err
		}
		if _, err := f.Write(raw); err != nil {
			f.Close()
			return "", err
		}
		f.Close()
		tmpFiles = append(tmpFiles, f.Name())
	}

	if len(tmpFiles) == 1 {
		raw, err := os.ReadFile(tmpFiles[0])
		if err != nil {
			return "", err
		}
		return base64.StdEncoding.EncodeToString(raw), nil
	}

	// Multiple chunks: concatenate into one wav using ffmpeg's concat demuxer.
	listFile, err := os.CreateTemp("", "sunkenbot-tts-list-*.txt")
	if err != nil {
		return "", err
	}
	defer os.Remove(listFile.Name())
	for _, f := range tmpFiles {
		fmt.Fprintf(listFile, "file '%s'\n", f)
	}
	listFile.Close()

	outPath := listFile.Name() + "_merged.wav"
	defer os.Remove(outPath)

	ctxTimeout, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctxTimeout, "ffmpeg",
		"-f", "concat", "-safe", "0", "-i", listFile.Name(), "-c", "copy", "-y", outPath)
	if out, err := cmd.CombinedOutput(); err != nil {
		return "", fmt.Errorf("ffmpeg concat failed: %w (%s)", err, strutil.Truncate(string(out), 150))
	}

	merged, err := os.ReadFile(outPath)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(merged), nil
}

// processVideo extracts the first frame of a video using FFmpeg and passes it to the vision model.
func (s *Service) processVideo(ctx context.Context, url, prompt string, messages []session.Message) (string, error) {
	raw, _, err := s.fetchBase64(ctx, url)
	if err != nil {
		return videoFailMessage(err), nil
	}

	vidFile, err := os.CreateTemp("", "sunkenbot-*.mp4")
	if err != nil {
		return videoFailMessage(err), nil
	}
	vidPath := vidFile.Name()
	defer os.Remove(vidPath)
	if _, err := vidFile.Write(raw); err != nil {
		vidFile.Close()
		return videoFailMessage(err), nil
	}
	vidFile.Close()

	framePath := strings.TrimSuffix(vidPath, ".mp4") + "_frame.jpg"
	defer os.Remove(framePath)

	ctxTimeout, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctxTimeout, "ffmpeg",
		"-i", vidPath, "-ss", "00:00:01", "-vframes", "1", "-q:v", "2", framePath, "-y")
	if err := cmd.Run(); err != nil {
		return videoFailMessage(fmt.Errorf("ffmpeg failed: %w", err)), nil
	}

	frameRaw, err := os.ReadFile(framePath)
	if err != nil {
		return videoFailMessage(err), nil
	}

	reply, err := s.groqVision(ctx, messages, base64.StdEncoding.EncodeToString(frameRaw), "image/jpeg")
	if err != nil {
		return videoFailMessage(err), nil
	}
	return fmt.Sprintf("🎬 تحليل الفيديو (الإطار الأول):\n%s", reply), nil
}

// videoFailMessage returns a standardized fallback warning when video processing fails.
func videoFailMessage(err error) string {
	return fmt.Sprintf("⚠️ تعذّر تحليل الفيديو (%s). يمكنك أخذ screenshot وإرساله كصورة.", strutil.Truncate(err.Error(), 60))
}

// postGroqChat sends a chat completion request to the Groq API endpoint.
func (s *Service) postGroqChat(ctx context.Context, key string, payload map[string]any, timeout time.Duration) (string, error) {
	buf, _ := json.Marshal(payload)
	ctxTimeout, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctxTimeout, http.MethodPost,
		"https://api.groq.com/openai/v1/chat/completions", bytes.NewReader(buf))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")

	resp, err := s.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("groq status %d: %s", resp.StatusCode, strutil.Truncate(string(respBody), 200))
	}

	var parsed struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(respBody, &parsed); err != nil {
		return "", err
	}
	if len(parsed.Choices) == 0 {
		return "", fmt.Errorf("empty choices")
	}
	return stripThinking(parsed.Choices[0].Message.Content), nil
}

// stripThinking removes <think>...</think> reasoning traces that some models (e.g. qwen
// reasoning models) prepend to their output. Handles an unclosed trailing <think> too,
// in case the response was truncated mid-thought.
func stripThinking(text string) string {
	for {
		start := strings.Index(text, "<think>")
		if start == -1 {
			break
		}
		end := strings.Index(text[start:], "</think>")
		if end == -1 {
			// Unclosed think block: drop everything from <think> onward.
			text = text[:start]
			break
		}
		text = text[:start] + text[start+end+len("</think>"):]
	}
	return strings.TrimSpace(text)
}

// hasSystem checks if a system prompt is already included in the message slice.
func hasSystem(messages []session.Message) bool {
	for _, m := range messages {
		if m.Role == "system" {
			return true
		}
	}
	return false
}

// toChatMessages converts domain session messages into standard map formats for API payloads.
func toChatMessages(messages []session.Message) []map[string]string {
	out := make([]map[string]string, len(messages))
	for i, m := range messages {
		out[i] = map[string]string{"role": m.Role, "content": m.Content}
	}
	return out
}

// parseMessages decodes raw interface arrays into structured session messages.
func parseMessages(raw any) []session.Message {
	arr, ok := raw.([]any)
	if !ok {
		return nil
	}
	out := make([]session.Message, 0, len(arr))
	for _, item := range arr {
		obj, ok := item.(map[string]any)
		if !ok {
			continue
		}
		out = append(out, session.Message{
			Role:    strutil.StringOr(obj["role"], ""),
			Content: strutil.StringOr(obj["content"], ""),
		})
	}
	return out
}

