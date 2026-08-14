// Package gemini يقابل plugins/gemini.py بالكامل: endpoint POST /gemini
// (Gemini 2.5 Flash مع Google Search Grounding، جلسات جماعية، وGroq fallback).
//
// فرق تصميم واحد متعمّد عن بايثون: بدل استخدام SDK الرسمي (google-genai)
// الذي لا مكافئ رسمي ناضج له بلغة Go، نستدعي REST API لـ Gemini مباشرة
// (نفس ما كانت groq.py تفعله فعلاً في _gemini_fallback الخاص بها —
// أي أن الاتصال المباشر بالـ REST API لم يكن غريباً حتى في نسخة بايثون
// الأصلية). النتيجة سلوك مطابق تماماً بدون تبعية SDK إضافية.
package gemini

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/plugins"
	"sunkenbot/internal/session"
	"sunkenbot/internal/strutil"
)

const Description = "Gemini 2.5 Flash (Google Search Grounding) — جلسات جماعية + Groq fallback، بالإضافة لتحويل نص↔صوت عبر /gemini/tts و/gemini/stt"

const (
	systemPrompt = `أنت "Sunken"، صاحب في غروب.
- رد بالدارجة الجزائرية دايما (درجة، واش، راك، خويا، بصح، كيفاش...)
- اقصر ما يمكن — جملة واحدة أو جملتين كافية
- بلا عناوين، بلا نقاط، بلا تنسيق
- إذا السؤال غبي رد بنكتة قصيرة
- إذا جاك صورة/صوت/فيديو وصفه مباشرة بلا مقدمات`
	modelName    = "gemini-2.5-flash"
	groqModel    = "llama-3.3-70b-versatile"
)

// Service يحمل عميل http.Client (25s، محلي — راجع جدول قرار http.Client:
// هذا هو التعارض الوحيد المحسوم بين النسخ الأصلية للبرومبت) ومخزن
// الجلسات كاعتماديتين صريحتين بدل متغيرات حزمة عامة.
type Service struct {
	client *http.Client
	store  session.Store
}

func New(client *http.Client, store session.Store) *Service {
	return &Service{client: client, store: store}
}

func (s *Service) Name() string { return "gemini" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/gemini", Handler: httpx.Handle(s.handleGemini)},
		{Method: "POST", Pattern: "/gemini/tts", Handler: httpx.WrapJSON(s.handleTTS)},
		{Method: "GET", Pattern: "/gemini/tts/voices", Handler: httpx.Handle(s.handleTTSVoices)},
		{Method: "POST", Pattern: "/gemini/stt", Handler: httpx.WrapJSON(s.handleSTT)},
	}
}

// ─── مفاتيح البيئة (تقابل GEMINI_KEYS/GROQ_KEY في بايثون) ─────────────

func geminiKeys() []string {
	var keys []string
	for _, name := range []string{"GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"} {
		if v := strings.TrimSpace(os.Getenv(name)); len(v) > 10 {
			keys = append(keys, v)
		}
	}
	return keys
}

func groqKey() string { return os.Getenv("GROQ_API_KEY") }

// ─── HTTP handler ──────────────────────────────────────────────────

// handleGemini يحافظ حرفياً على 500 (لا 400) عند فشل decode، وعلى فحص
// *وجود* حقلي thread_id/prompt في JSON الخام (لا فحص أن قيمتها غير فارغة)
// عبر قراءة الجسم كـ map[string]any أولاً — راجع الملاحظة الخاصة بـ
// gemini في برومبت الترحيل؛ هذا فرق سلوكي حقيقي يجب الحفاظ عليه حرفياً.
func (s *Service) handleGemini(r *http.Request) (map[string]any, error) {
	ctx := r.Context()

	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusInternalServerError,
			Body: map[string]any{"error": strutil.Truncate(err.Error(), 200)},
		}
	}

	_, hasThreadID := body["thread_id"]
	_, hasPrompt := body["prompt"]

	if hasThreadID || hasPrompt {
		return s.handleSessionMode(ctx, body)
	}

	// ─── النمط القديم: messages مباشرة (بدون جلسات) ───────────────
	messages := parseMessages(body["messages"])
	if len(messages) == 0 {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "messages أو prompt مطلوب"},
		}
	}

	reply, err := s.callGemini(ctx, messages)
	if err == nil {
		return map[string]any{"reply": reply, "provider": "gemini"}, nil
	}

	groqMsgs := ensureSystem(messages)
	reply, err2 := s.callGroq(ctx, groqMsgs)
	if err2 != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusServiceUnavailable,
			Body: map[string]any{"error": "كل الخوادم فشلت: " + strutil.Truncate(err2.Error(), 100)},
		}
	}
	return map[string]any{"reply": reply, "provider": "groq"}, nil
}

func (s *Service) handleSessionMode(ctx context.Context, body map[string]any) (map[string]any, error) {
	threadID := strutil.StringOr(body["thread_id"], "default")
	senderName := strutil.StringOr(body["sender_name"], "مستخدم")
	prompt := strings.TrimSpace(strutil.StringOr(body["prompt"], ""))
	doClear, _ := body["clear"].(bool)

	if doClear {
		_ = s.store.Clear(ctx, threadID)
		return map[string]any{"reply": "🧹 تم مسح ذاكرة المجموعة."}, nil
	}

	if prompt == "" {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "prompt مطلوب"},
		}
	}

	ctxMsgs, _ := s.store.Load(ctx, threadID)
	userContent := fmt.Sprintf("[%s]: %s", senderName, prompt)

	messages := append([]session.Message{{Role: "system", Content: systemPrompt}}, ctxMsgs...)
	messages = append(messages, session.Message{Role: "user", Content: userContent})

	var reply, provider string
	reply, err := s.callGemini(ctx, messages)
	if err == nil {
		provider = "gemini"
	} else {
		reply, err = s.callGroq(ctx, ensureSystem(messages))
		if err != nil {
			return nil, &httpx.HTTPError{
				Code: http.StatusServiceUnavailable,
				Body: map[string]any{"error": "كل الخوادم فشلت: " + strutil.Truncate(err.Error(), 100)},
			}
		}
		provider = "groq"
	}

	newHistory := append(append([]session.Message{}, ctxMsgs...),
		session.Message{Role: "user", Content: userContent},
		session.Message{Role: "assistant", Content: reply},
	)
	_ = s.store.Save(ctx, threadID, newHistory)

	return map[string]any{"reply": reply, "provider": provider}, nil
}

// ─── استدعاء Gemini (REST مباشر، مع Google Search Grounding) ──────────

func (s *Service) callGemini(ctx context.Context, messages []session.Message) (string, error) {
	contents := toGeminiContents(messages)

	payload := map[string]any{
		"systemInstruction": map[string]any{"parts": []any{map[string]any{"text": systemPrompt}}},
		"contents":          contents,
		"generationConfig":  map[string]any{"temperature": 0.7, "maxOutputTokens": 1024},
		"tools":             []any{map[string]any{"google_search": map[string]any{}}},
	}
	buf, _ := json.Marshal(payload)

	url := fmt.Sprintf("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent", modelName)

	for _, key := range geminiKeys() {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(buf))
		if err != nil {
			continue
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-goog-api-key", key)

		resp, err := s.client.Do(req)
		if err != nil {
			continue
		}
		respBody, _ := io.ReadAll(resp.Body)
		resp.Body.Close()

		if resp.StatusCode == http.StatusTooManyRequests {
			continue
		}
		if resp.StatusCode >= 400 {
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
			reply := strings.TrimSpace(parsed.Candidates[0].Content.Parts[0].Text)
			if reply != "" {
				return reply, nil
			}
		}
	}

	return "", fmt.Errorf("ALL_GEMINI_KEYS_EXHAUSTED")
}

func toGeminiContents(messages []session.Message) []map[string]any {
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
	return contents
}

// ─── Groq fallback (نفس /openai/v1/chat/completions في بايثون) ───────

func (s *Service) callGroq(ctx context.Context, messages []session.Message) (string, error) {
	key := groqKey()
	if key == "" {
		return "", fmt.Errorf("NO_GROQ_KEY")
	}

	type chatMsg struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	chatMsgs := make([]chatMsg, len(messages))
	for i, m := range messages {
		chatMsgs[i] = chatMsg{Role: m.Role, Content: m.Content}
	}

	payload := map[string]any{
		"model":       groqModel,
		"messages":    chatMsgs,
		"max_tokens":  1024,
		"temperature": 0.7,
	}
	buf, _ := json.Marshal(payload)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
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
	return parsed.Choices[0].Message.Content, nil
}

// ─── أدوات مساعدة ──────────────────────────────────────────────────

func ensureSystem(messages []session.Message) []session.Message {
	for _, m := range messages {
		if m.Role == "system" {
			return messages
		}
	}
	return append([]session.Message{{Role: "system", Content: systemPrompt}}, messages...)
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
			Role:    strutil.StringOr(obj["role"], ""),
			Content: strutil.StringOr(obj["content"], ""),
		})
	}
	return out
}


