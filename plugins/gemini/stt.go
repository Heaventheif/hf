// stt.go يقابل stt.js بالكامل: يرسل الصوت كـ inlineData إلى
// gemini-2.5-flash (نموذج فهم صوت، عكس ttsModel في tts.go الذي يولّد
// صوتاً) مع نفس نص TRANSCRIBE_PROMPT حرفياً، ويدور على نفس مفاتيح
// GEMINI_API_KEY المشتركة (geminiKeys() في gemini.go).
package gemini

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/netguard"
	"sunkenbot/internal/strutil"
)

const sttModel = "gemini-2.5-flash"

const transcribePrompt = "فرّغ (Transcribe) هذا المقطع الصوتي حرفياً وبدقة كاملة، بنفس اللغة المستخدمة في " +
	"التسجيل دون ترجمة. إن وُجد أكثر من متحدث فاذكر ذلك بإيجاز. أعد النص فقط دون أي " +
	"تعليق أو مقدمة إضافية. إن كان المقطع لا يحتوي على كلام مفهوم فأجب حرفياً بـ: " +
	"[لا يوجد كلام واضح في المقطع]"

// maxSTTAudioBytes: نفس حد الـ 19 ميجابايت في stt.js (تعليق "Gemini
// inline data محدود الحجم" هناك).
const maxSTTAudioBytes = 19 * 1024 * 1024

var mimeByExt = map[string]string{
	"mp3":  "audio/mp3",
	"mp4":  "audio/mp4",
	"m4a":  "audio/mp4",
	"ogg":  "audio/ogg",
	"wav":  "audio/wav",
	"aac":  "audio/aac",
	"flac": "audio/flac",
}

func guessMimeType(ext string) string {
	if m, ok := mimeByExt[strings.ToLower(ext)]; ok {
		return m
	}
	return "audio/mp3"
}

// callGeminiSTT يقابل callGeminiSTT في stt.js حرفياً — نفس منطق الاستمرار
// عند 429/503 فقط، وإلا فشل فوري برسالة Gemini الحقيقية.
func (s *Service) callGeminiSTT(ctx context.Context, base64Audio, mimeType string) (string, error) {
	keys := geminiKeys()
	if len(keys) == 0 {
		return "", fmt.Errorf("لا توجد مفاتيح GEMINI_API_KEY في البيئة")
	}

	payload := map[string]any{
		"contents": []any{map[string]any{
			"parts": []any{
				map[string]any{"text": transcribePrompt},
				map[string]any{"inlineData": map[string]any{"mimeType": mimeType, "data": base64Audio}},
			},
		}},
	}
	buf, _ := json.Marshal(payload)
	url := fmt.Sprintf("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent", sttModel)

	var publicErrors []string
	for i, key := range keys {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url+"?key="+key, bytes.NewReader(buf))
		if err != nil {
			continue
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := s.client.Do(req)
		if err != nil {
			publicErrors = append(publicErrors, fmt.Sprintf("مفتاح #%d: %s", i+1, strutil.Truncate(err.Error(), 150)))
			continue
		}
		respBody, _ := io.ReadAll(resp.Body)
		resp.Body.Close()

		if resp.StatusCode >= 400 {
			msg := extractGoogleErrorMessage(respBody, resp.StatusCode)
			publicErrors = append(publicErrors, fmt.Sprintf("مفتاح #%d: %s", i+1, msg))
			if resp.StatusCode != http.StatusTooManyRequests && resp.StatusCode != http.StatusServiceUnavailable {
				return "", fmt.Errorf("%s", msg)
			}
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
			publicErrors = append(publicErrors, fmt.Sprintf("مفتاح #%d: استجابة غير صالحة", i+1))
			continue
		}
		if len(parsed.Candidates) == 0 || len(parsed.Candidates[0].Content.Parts) == 0 {
			publicErrors = append(publicErrors, fmt.Sprintf("مفتاح #%d: استجابة فارغة", i+1))
			continue
		}
		text := strings.TrimSpace(parsed.Candidates[0].Content.Parts[0].Text)
		if text == "" {
			publicErrors = append(publicErrors, fmt.Sprintf("مفتاح #%d: استجابة فارغة", i+1))
			continue
		}
		return text, nil
	}

	return "", fmt.Errorf("كل مفاتيح Gemini فشلت:\n%s", strings.Join(publicErrors, "\n"))
}

// ─── HTTP handler ──────────────────────────────────────────────────

// STTRequest: إما audio_url (يُجلب عبر netguard.SafeFetch كباقي
// المرفقات في المشروع) أو audio_base64 مباشرة. ext اختياري — يُستخدم
// فقط لتخمين mime_type (راجع guessMimeType)، الافتراضي "mp3" كما في
// stt.js (لأن الأصل هناك يخمّن mp3/mp4 فقط بحسب attachment.type).
type STTRequest struct {
	AudioURL    string `json:"audio_url"`
	AudioBase64 string `json:"audio_base64"`
	Ext         string `json:"ext"`
}

type STTResponse struct {
	Transcript string `json:"transcript"`
}

func (s *Service) handleSTT(ctx context.Context, req STTRequest) (STTResponse, error) {
	var raw []byte

	switch {
	case strings.TrimSpace(req.AudioURL) != "":
		fetched, err := netguard.SafeFetch(ctx, s.client, req.AudioURL, maxSTTAudioBytes)
		if err != nil {
			return STTResponse{}, &httpx.HTTPError{
				Code: http.StatusServiceUnavailable,
				Body: map[string]any{"error": "تعذّر جلب الملف الصوتي: " + strutil.Truncate(err.Error(), 200)},
			}
		}
		raw = fetched
	case strings.TrimSpace(req.AudioBase64) != "":
		decoded, err := decodeAudioBase64(req.AudioBase64)
		if err != nil {
			return STTResponse{}, &httpx.HTTPError{
				Code: http.StatusBadRequest,
				Body: map[string]any{"error": "audio_base64 غير صالح"},
			}
		}
		raw = decoded
	default:
		return STTResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "audio_url أو audio_base64 مطلوب"},
		}
	}

	if len(raw) == 0 {
		return STTResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "الملف الصوتي فارغ"},
		}
	}
	if len(raw) > maxSTTAudioBytes {
		return STTResponse{}, &httpx.HTTPError{
			Code: http.StatusRequestEntityTooLarge,
			Body: map[string]any{"error": "الملف كبير جداً (الحد الأقصى تقريباً 19MB لهذا الأمر)"},
		}
	}

	ext := req.Ext
	if ext == "" {
		ext = "mp3"
	}
	mimeType := guessMimeType(ext)
	base64Audio := base64.StdEncoding.EncodeToString(raw)

	transcript, err := s.callGeminiSTT(ctx, base64Audio, mimeType)
	if err != nil {
		return STTResponse{}, &httpx.HTTPError{
			Code: http.StatusServiceUnavailable,
			Body: map[string]any{"error": strutil.Truncate(err.Error(), 500)},
		}
	}

	return STTResponse{Transcript: transcript}, nil
}

func decodeAudioBase64(s string) ([]byte, error) {
	if idx := strings.Index(s, ","); idx != -1 && strings.HasPrefix(s, "data:") {
		s = s[idx+1:]
	}
	return base64.StdEncoding.DecodeString(s)
}

// extractGoogleErrorMessage يقابل e.response?.data?.error?.message في كل
// من tts.js وstt.js — يحاول استخراج رسالة خطأ Google الحقيقية من جسم
// JSON، وإلا يرجع رسالة عامة تتضمن كود الحالة.
func extractGoogleErrorMessage(body []byte, status int) string {
	var parsed struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(body, &parsed); err == nil && parsed.Error.Message != "" {
		return parsed.Error.Message
	}
	return fmt.Sprintf("HTTP %d", status)
}
