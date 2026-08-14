// tts.go يقابل tts.js بالكامل: استدعاء نموذج gemini-2.5-flash-preview-tts
// عبر REST مباشرة (نفس أسلوب callGemini في gemini.go)، مع تدوير مفاتيح
// GEMINI_API_KEY (geminiKeys() المشتركة من gemini.go) وتحويل PCM الخام
// الذي يرجعه Gemini إلى WAV صالح للتشغيل مباشرة.
//
// فرق سلوكي متعمّد عن tts.js: بايثون/جافاسكربت كانا يرسلان الصوت مباشرة
// كمرفق ماسنجر عبر safeSend — لا معنى لذلك هنا (هذا الملف endpoint HTTP
// بحت، راجع ملاحظة song.js/soundcloud.go في نقاش الترحيل)، لذلك يُعاد
// الصوت base64 في جسم JSON، والجهة المستدعية (جسر الميسنجر) هي من ترسله.
package gemini

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math/rand"
	"net/http"
	"strings"
	"sync"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/strutil"
)

const ttsModel = "gemini-2.5-flash-preview-tts"

// allVoices: نفس القائمة الثلاثين صوتاً بالضبط من tts.js.
var allVoices = []string{
	"Achernar", "Achird", "Algenib", "Algieba", "Alnilam",
	"Aoede", "Autonoe", "Callirrhoe", "Charon", "Despina",
	"Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus",
	"Kore", "Laomedeia", "Leda", "Orus", "Puck",
	"Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager",
	"Schedar", "Sulafat", "Umbriel", "Vindemiatrix",
	"Zephyr", "Zubenelgenubi",
}

// voicePool يقابل _voicePool/nextVoice في tts.js: تبديل عشوائي كامل
// للقائمة، ثم استهلاكها بالترتيب (pop) حتى تنفد فيُعاد الخلط — يضمن عدم
// تكرار نفس الصوت مرتين متتاليتين بقدر الإمكان، أفضل من اختيار عشوائي
// مستقل في كل مرة. محمي بقفل لأن Go خادم متزامن (خلافاً لعملية Node
// الواحدة single-threaded التي كتب لها tts.js أصلاً).
var (
	voicePoolMu sync.Mutex
	voicePool   []string
)

func nextVoice() string {
	voicePoolMu.Lock()
	defer voicePoolMu.Unlock()
	if len(voicePool) == 0 {
		voicePool = append([]string(nil), allVoices...)
		rand.Shuffle(len(voicePool), func(i, j int) { voicePool[i], voicePool[j] = voicePool[j], voicePool[i] })
	}
	v := voicePool[len(voicePool)-1]
	voicePool = voicePool[:len(voicePool)-1]
	return v
}

func isValidVoice(name string) (string, bool) {
	for _, v := range allVoices {
		if strings.EqualFold(v, name) {
			return v, true
		}
	}
	return "", false
}

// pcmToWav يطابق pcmToWav في tts.js حرفياً: Gemini يُرجع PCM خام (24kHz،
// أحادي، 16 بت) — هذه الدالة تبني رأس WAV يدوياً بدل أي مكتبة خارجية.
func pcmToWav(pcm []byte, sampleRate, channels, bitDepth int) []byte {
	byteRate := sampleRate * channels * (bitDepth / 8)
	blockAlign := channels * (bitDepth / 8)
	dataSize := len(pcm)
	wav := make([]byte, 44+dataSize)

	copy(wav[0:4], "RIFF")
	putUint32LE(wav[4:8], uint32(36+dataSize))
	copy(wav[8:12], "WAVE")
	copy(wav[12:16], "fmt ")
	putUint32LE(wav[16:20], 16)
	putUint16LE(wav[20:22], 1)
	putUint16LE(wav[22:24], uint16(channels))
	putUint32LE(wav[24:28], uint32(sampleRate))
	putUint32LE(wav[28:32], uint32(byteRate))
	putUint16LE(wav[32:34], uint16(blockAlign))
	putUint16LE(wav[34:36], uint16(bitDepth))
	copy(wav[36:40], "data")
	putUint32LE(wav[40:44], uint32(dataSize))
	copy(wav[44:], pcm)

	return wav
}

func putUint32LE(b []byte, v uint32) {
	b[0] = byte(v)
	b[1] = byte(v >> 8)
	b[2] = byte(v >> 16)
	b[3] = byte(v >> 24)
}

func putUint16LE(b []byte, v uint16) {
	b[0] = byte(v)
	b[1] = byte(v >> 8)
}

// callGeminiTTS يقابل callGeminiTTS في tts.js: يجرّب كل مفتاح بالترتيب،
// ويستمر للمفتاح التالي عند 429/503 فقط (نفس منطق الاستمرار في النسخة
// الأصلية)، وإلا يفشل فوراً برسالة الخطأ الحقيقية.
func (s *Service) callGeminiTTS(ctx context.Context, text, voice string) ([]byte, error) {
	keys := geminiKeys()
	if len(keys) == 0 {
		return nil, fmt.Errorf("لا توجد مفاتيح GEMINI_API_KEY في البيئة")
	}

	payload := map[string]any{
		"contents": []any{map[string]any{"parts": []any{map[string]any{"text": text}}}},
		"generationConfig": map[string]any{
			"responseModalities": []string{"AUDIO"},
			"speechConfig": map[string]any{
				"voiceConfig": map[string]any{
					"prebuiltVoiceConfig": map[string]any{"voiceName": voice},
				},
			},
		},
	}
	buf, _ := json.Marshal(payload)
	url := fmt.Sprintf("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent", ttsModel)

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
				return nil, fmt.Errorf("%s", msg)
			}
			continue
		}

		var parsed struct {
			Candidates []struct {
				Content struct {
					Parts []struct {
						InlineData struct {
							Data     string `json:"data"`
							MimeType string `json:"mimeType"`
						} `json:"inlineData"`
					} `json:"parts"`
				} `json:"content"`
			} `json:"candidates"`
		}
		if err := json.Unmarshal(respBody, &parsed); err != nil {
			publicErrors = append(publicErrors, fmt.Sprintf("مفتاح #%d: استجابة غير صالحة", i+1))
			continue
		}
		if len(parsed.Candidates) == 0 || len(parsed.Candidates[0].Content.Parts) == 0 ||
			parsed.Candidates[0].Content.Parts[0].InlineData.Data == "" {
			publicErrors = append(publicErrors, fmt.Sprintf("مفتاح #%d: استجابة فارغة", i+1))
			continue
		}

		part := parsed.Candidates[0].Content.Parts[0]
		raw, err := base64.StdEncoding.DecodeString(part.InlineData.Data)
		if err != nil {
			publicErrors = append(publicErrors, fmt.Sprintf("مفتاح #%d: base64 غير صالح", i+1))
			continue
		}

		mime := part.InlineData.MimeType
		if strings.Contains(mime, "pcm") || strings.Contains(mime, "L16") || !strings.Contains(mime, "wav") {
			return pcmToWav(raw, 24000, 1, 16), nil
		}
		return raw, nil
	}

	return nil, fmt.Errorf("كل مفاتيح Gemini فشلت:\n%s", strings.Join(publicErrors, "\n"))
}

// ─── HTTP handler ──────────────────────────────────────────────────

type TTSRequest struct {
	Text  string `json:"text"`
	Voice string `json:"voice"` // اختياري — راجع voice في tts.js
}

type TTSResponse struct {
	AudioBase64 string `json:"audio_base64"`
	MimeType    string `json:"mime_type"`
	Voice       string `json:"voice"`
}

func (s *Service) handleTTS(ctx context.Context, req TTSRequest) (TTSResponse, error) {
	text := strings.TrimSpace(req.Text)
	if text == "" {
		return TTSResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "text مطلوب"},
		}
	}
	if len(text) > 3000 {
		return TTSResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "النص طويل جداً (3000 حرف كحد أقصى)"},
		}
	}

	voice := strings.TrimSpace(req.Voice)
	if voice == "" {
		voice = nextVoice()
	} else if v, ok := isValidVoice(voice); ok {
		voice = v
	} else {
		voice = nextVoice()
	}

	audio, err := s.callGeminiTTS(ctx, text, voice)
	if err != nil {
		return TTSResponse{}, &httpx.HTTPError{
			Code: http.StatusServiceUnavailable,
			Body: map[string]any{"error": strutil.Truncate(err.Error(), 500)},
		}
	}

	return TTSResponse{
		AudioBase64: base64.StdEncoding.EncodeToString(audio),
		MimeType:    "audio/wav",
		Voice:       voice,
	}, nil
}

// GET /gemini/tts/voices — يقابل قسم "tts voices" في tts.js.
func (s *Service) handleTTSVoices(r *http.Request) (map[string]any, error) {
	return map[string]any{
		"model":  ttsModel,
		"voices": allVoices,
		"count":  len(allVoices),
	}, nil
}
