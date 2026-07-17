// Package groq يقابل plugins/groq.py بالكامل: endpoint POST /groq
// (Llama 4 Scout عبر Groq — نص/صورة/صوت/فيديو، جلسات جماعية، Gemini fallback).
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
)

const Description = "Llama 4 Scout — Vision + Audio + Video + جلسات جماعية + Gemini fallback"

const (
	llama4Model  = "meta-llama/llama-4-scout-17b-16e-instruct"
	whisperModel = "whisper-large-v3"
	systemPrompt = `أنت بوت مساعد ذكي اسمك "Sunken". أجب دائماً باللغة العربية بإيجاز (أقل من 300 كلمة). كن ودوداً ومهذباً. إذا أُرسلت إليك صورة أو صوت أو فيديو فحللها بدقة.`
)

// Service يحمل عميلي http.Client (client: طلبات Groq/Gemini API، 30s؛
// dlClient: تحميل مرفقات صورة/صوت/فيديو، 120s) ومخزن الجلسات كاعتماديات
// صريحة بدل متغيرات حزمة عامة.
type Service struct {
	client   *http.Client
	dlClient *http.Client
	store    session.Store
}

func New(client, dlClient *http.Client, store session.Store) *Service {
	return &Service{client: client, dlClient: dlClient, store: store}
}

func (s *Service) Name() string { return "groq" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/groq", Handler: httpx.Handle(s.handleGroq)},
	}
}

func groqKey() string { return os.Getenv("GROQ_API_KEY") }

func geminiKeys() []string {
	var keys []string
	for _, name := range []string{"GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"} {
		if v := strings.TrimSpace(os.Getenv(name)); len(v) > 10 {
			keys = append(keys, v)
		}
	}
	return keys
}

// ─── أنواع الطلب/المرفق ────────────────────────────────────────────
//
// ملاحظة حرِجة (راجع §0/§3.5 في برومبت الترحيل): attachment حقل جذر
// (root-level)، وليس متداخلاً داخل أي عنصر messages. هذا يطابق تماماً ما
// يرسله ss-main/cmds/groq.js فعلياً بعد إصلاح عطل تحليل الصور/الصوت/
// الفيديو الحرِج سابقاً — لا تُعِد هذا الحقل لمكانه القديم أبداً.
type attachment struct {
	Kind        string `json:"kind"`
	URL         string `json:"url"`
	Base64      string `json:"base64"`
	ContentType string `json:"contentType"`
}

func parseAttachment(raw any) *attachment {
	obj, ok := raw.(map[string]any)
	if !ok {
		return nil
	}
	return &attachment{
		Kind:        stringOr(obj["kind"], ""),
		URL:         stringOr(obj["url"], ""),
		Base64:      stringOr(obj["base64"], ""),
		ContentType: stringOr(obj["contentType"], ""),
	}
}

// ─── HTTP handler ──────────────────────────────────────────────────

func (s *Service) handleGroq(r *http.Request) (map[string]any, error) {
	ctx := r.Context()

	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusInternalServerError,
			Body: map[string]any{"error": truncate(err.Error(), 200)},
		}
	}

	_, hasThreadID := body["thread_id"]
	_, hasPrompt := body["prompt"]

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

	// حقل الجذر "attachment" (وليس داخل أي عنصر messages) — راجع الملاحظة أعلاه.
	att := parseAttachment(body["attachment"])
	last := messages[len(messages)-1]
	prompt := last.Content

	reply, provider, err := s.dispatchAttachment(ctx, att, messages, prompt)
	if err != nil {
		reply2, err2 := s.geminiFallback(ctx, messages)
		if err2 != nil {
			return nil, &httpx.HTTPError{
				Code: http.StatusServiceUnavailable,
				Body: map[string]any{"error": "كل الخوادم فشلت: " + truncate(err2.Error(), 100)},
			}
		}
		return map[string]any{"reply": reply2, "provider": "gemini-fallback"}, nil
	}
	return map[string]any{"reply": reply, "provider": provider}, nil
}

func (s *Service) handleSessionMode(ctx context.Context, body map[string]any) (map[string]any, error) {
	threadID := stringOr(body["thread_id"], "default")
	senderName := stringOr(body["sender_name"], "مستخدم")
	prompt := strings.TrimSpace(stringOr(body["prompt"], ""))
	doClear, _ := body["clear"].(bool)
	att := parseAttachment(body["attachment"])

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
		reply, err = s.geminiFallback(ctx, messages)
		if err != nil {
			return nil, &httpx.HTTPError{
				Code: http.StatusServiceUnavailable,
				Body: map[string]any{"error": "كل الخوادم فشلت: " + truncate(err.Error(), 100)},
			}
		}
		provider = "gemini-fallback"
	}

	attLabel := ""
	if att != nil {
		attLabel = fmt.Sprintf("[%s] ", att.Kind)
	}
	userText := strings.TrimSpace(fmt.Sprintf("[%s]: %s%s", senderName, attLabel, prompt))

	newHistory := append(append([]session.Message{}, ctxMsgs...),
		session.Message{Role: "user", Content: userText},
		session.Message{Role: "assistant", Content: reply},
	)
	_ = s.store.Save(ctx, threadID, newHistory)

	return map[string]any{"reply": reply, "provider": provider}, nil
}

// dispatchAttachment يقابل كتلة if kind == "image"/"audio"/"video" المكرَّرة
// مرتين في بايثون (نمط الجلسات والنمط القديم) — وُحِّدت هنا في مكان واحد.
func (s *Service) dispatchAttachment(ctx context.Context, att *attachment, messages []session.Message, prompt string) (reply, provider string, err error) {
	if att == nil {
		reply, err = s.groqText(ctx, messages)
		return reply, "groq", err
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

	reply, err = s.groqText(ctx, messages)
	return reply, "groq", err
}

// ─── تحميل الوسائط ──────────────────────────────────────────────────

// maxAttachmentBytes: حد أقصى لحجم أي مرفق (صورة/صوت/فيديو) يُجلب من رابط
// مُرسَل من المستخدم — بدونه، خادم بعيد (متعاون مع مهاجم أو معطوب) يمكن أن
// يبث استجابة ضخمة أو غير منتهية ويستنزف ذاكرة العملية بالكامل.
const maxAttachmentBytes = 20 * 1024 * 1024 // 20MB

func (s *Service) fetchBase64(ctx context.Context, url string) ([]byte, string, error) {
	// حماية SSRF + DNS rebinding: SafeFetch يتحقق من الرابط ثم يثبّت
	// (pin) نفس العنوان المتحقق منه للاتصال الفعلي — بدل الاعتماد على
	// http.Client الذي كان سيعيد حلّ DNS بشكل مستقل وقت الاتصال (راجع
	// التعليق التفصيلي في internal/netguard/netguard.go). كما يفرض حد
	// حجم أقصى (maxAttachmentBytes) بدل io.ReadAll غير محدود سابقاً.
	raw, err := netguard.SafeFetch(ctx, s.dlClient, url, maxAttachmentBytes)
	if err != nil {
		return nil, "", fmt.Errorf("رابط المرفق مرفوض أو تعذّر جلبه: %w", err)
	}
	return raw, base64.StdEncoding.EncodeToString(raw), nil
}

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

// ─── استدعاءات Groq ─────────────────────────────────────────────────

func (s *Service) groqText(ctx context.Context, messages []session.Message) (string, error) {
	key := groqKey()
	if key == "" {
		return "", fmt.Errorf("NO_GROQ_KEY")
	}
	payload := map[string]any{
		"model":       llama4Model,
		"messages":    toChatMessages(messages),
		"max_tokens":  1024,
		"temperature": 0.7,
	}
	reply, err := s.postGroqChat(ctx, key, payload, 30*time.Second)
	if err != nil {
		return "", err
	}
	if reply == "" {
		return "", fmt.Errorf("EMPTY")
	}
	return reply, nil
}

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

	payload := map[string]any{
		"model":      llama4Model,
		"messages":   chatMsgs,
		"max_tokens": 1024,
	}
	reply, err := s.postGroqChat(ctx, key, payload, 45*time.Second)
	if err != nil {
		return "", err
	}
	if reply == "" {
		return "", fmt.Errorf("EMPTY")
	}
	return reply, nil
}

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
		return "", fmt.Errorf("groq audio status %d: %s", resp.StatusCode, truncate(string(respBody), 200))
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
	reply, err := s.groqText(ctx, textMsgs)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("🎵 التفريغ:\n%s\n\n💬 الرد:\n%s", transcription, reply), nil
}

// processVideo يقابل _process_video: تحميل الفيديو، استخراج أول إطار
// عبر ffmpeg (subprocess في بايثون / os/exec في Go)، ثم تحليله كصورة.
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

func videoFailMessage(err error) string {
	return fmt.Sprintf("⚠️ تعذّر تحليل الفيديو (%s). يمكنك أخذ screenshot وإرساله كصورة.", truncate(err.Error(), 60))
}

// ─── Gemini fallback (REST مباشر — نفس _gemini_fallback في بايثون) ───

func (s *Service) geminiFallback(ctx context.Context, messages []session.Message) (string, error) {
	contents := make([]map[string]any, 0, len(messages))
	for _, m := range messages {
		if m.Role == "system" {
			continue
		}
		role := "user"
		if m.Role == "assistant" {
			role = "model"
		}
		contents = append(contents, map[string]any{
			"role":  role,
			"parts": []any{map[string]any{"text": m.Content}},
		})
	}

	payload := map[string]any{
		"systemInstruction": map[string]any{"parts": []any{map[string]any{"text": systemPrompt}}},
		"contents":          contents,
		"generationConfig":  map[string]any{"temperature": 0.7, "maxOutputTokens": 1024},
	}
	buf, _ := json.Marshal(payload)

	for _, key := range geminiKeys() {
		ctxTimeout, cancel := context.WithTimeout(ctx, 25*time.Second)
		req, err := http.NewRequestWithContext(ctxTimeout, http.MethodPost,
			"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
			bytes.NewReader(buf))
		if err != nil {
			cancel()
			continue
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-goog-api-key", key)

		resp, err := s.client.Do(req)
		if err != nil {
			cancel()
			continue
		}
		respBody, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		cancel()

		if resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 400 {
			continue
		}

		var parsed struct {
			Candidates []struct {
				Content struct {
					Parts []struct {
						Text string `json:"text"`
					} `json:"parts"`
				} `json:"content"`
			} `json:"candidates"`
		}
		if err := json.Unmarshal(respBody, &parsed); err != nil {
			continue
		}
		if len(parsed.Candidates) > 0 && len(parsed.Candidates[0].Content.Parts) > 0 {
			if reply := parsed.Candidates[0].Content.Parts[0].Text; reply != "" {
				return reply, nil
			}
		}
	}
	return "", fmt.Errorf("ALL_GEMINI_EXHAUSTED")
}

// ─── مشترك Groq chat/completions ─────────────────────────────────────

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
		return "", fmt.Errorf("groq status %d: %s", resp.StatusCode, truncate(string(respBody), 200))
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
	return parsed.Choices[0].Message.Content, nil
}

// ─── أدوات مساعدة ──────────────────────────────────────────────────

func hasSystem(messages []session.Message) bool {
	for _, m := range messages {
		if m.Role == "system" {
			return true
		}
	}
	return false
}

func toChatMessages(messages []session.Message) []map[string]string {
	out := make([]map[string]string, len(messages))
	for i, m := range messages {
		out[i] = map[string]string{"role": m.Role, "content": m.Content}
	}
	return out
}

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
			Role:    stringOr(obj["role"], ""),
			Content: stringOr(obj["content"], ""),
		})
	}
	return out
}

func stringOr(v any, def string) string {
	if s, ok := v.(string); ok {
		return s
	}
	return def
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
