// Package img_tr يسجّل plugin يترجم أي نص موجود داخل صورة إلى العربية
// ويعيد رسمه في مكانه.
//
// ملاحظة تسمية: اسم الحزمة/المجلد بقي "img_tr" (بـ underscore) بدل
// الاسم الأكثر اصطلاحية "imgtr" — قرار متعمَّد بعد عطل نشر حقيقي: تسمية
// جديدة تعني مجلداً جديداً بالكامل، وأي فجوة مزامنة/رفع بين هذا العمل
// والمستودع الفعلي (git/CI) تجعل ذلك المجلد الجديد غائباً عن سياق البناء
// بينما main.go يستورده بالفعل — بالضبط ما حدث هنا (خطأ بناء: "package
// sunkenbot/plugins/imgtr is not in std"، لأن المجلد الجديد لم يصل
// لسياق البناء). الإبقاء على اسم المجلد الأصلي يُلغي هذه الفئة من
// الأعطال بالكامل، بلا أي تكلفة وظيفية حقيقية. الـ endpoint نفسه يبقى
// POST /img_tr، واسم الخدمة في GET / يبقى "img_tr" — لا شيء من هذا سلوك
// خارجي تغيّر أصلاً.
//
// Pipeline (Go خالص 100% الآن — لا Node subprocess):
//  1. Go receives an image (URL or base64).
//  2. Go runs OCR in-process via github.com/otiai10/gosseract/v2 (cgo
//     bindings to Tesseract — راجع ocr.go) -> نصوص المناطق + مستطيلات
//     إحاطتها.
//  3. Go checks each region for Arabic script using a Unicode range regex.
//     Non-Arabic regions get translated via Groq (preferred) or Gemini.
//  4. Go re-renders the image in-process via image/draw +
//     golang.org/x/image/font (راجع render.go) لمسح النص الأصلي ورسم
//     الترجمة العربية مكانه.
//  5. The final image is returned as base64 JSON.
package img_tr

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"sync"
	"time"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/netguard"
	"sunkenbot/internal/plugins"
)

const Description = "محرك ترجمة نصوص الصور إلى العربية (OCR + ترجمة + إعادة رسم)"

// Unicode ranges covering Arabic, Arabic Supplement, Arabic Extended-A,
// Arabic Presentation Forms A/B.
var arabicRegex = regexp.MustCompile(`[\x{0600}-\x{06FF}\x{0750}-\x{077F}\x{08A0}-\x{08FF}\x{FB50}-\x{FDFF}\x{FE70}-\x{FEFF}]`)

// Service يحمل عميل http.Client كاعتمادية صريحة. لا مهلة (Timeout) مضافة
// هنا غير الموجودة سابقاً — http.DefaultClient نفسه (بلا أي timeout
// إطلاقاً) يُمرَّر صراحةً من main.go، نفس السلوك حرفياً.
type Service struct {
	client *http.Client
}

func New(client *http.Client) *Service {
	return &Service{client: client}
}

func (s *Service) Name() string { return "img_tr" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/img_tr", Handler: httpx.Handle(s.handleTranslate)},
		{Method: "POST", Pattern: "/img_tr/batch", Handler: httpx.Handle(s.handleTranslateBatch)},
	}
}

// Region is one block of text detected on the image, shared between the
// Go layer and the Node script via JSON.
type Region struct {
	Text string `json:"text"`
	// BBox is [x, y, width, height] in pixels.
	BBox [4]int `json:"bbox"`
}

type translateRequest struct {
	ImageURL    string `json:"image_url"`
	ImageBase64 string `json:"image_base64"`
	SourceLang  string `json:"source_lang"`
}

// translateResult تقابل شكل الرد الناجح لـ handleTranslate.
type translateResult struct {
	ImageBase64 string   `json:"image_base64"`
	Regions     []Region `json:"regions"`
}

// stageError يحمل رمز HTTP المناسب مع كل مرحلة من مراحل الترجمة
// (resolve/OCR/translate/render) — مستخرج خصيصاً ليكون translateImage
// قابلة للاستخدام من كل من /img_tr (حيث الرمز يتحوّل مباشرة لرمز HTTP
// فعلي) و/img_tr/batch (حيث كل صورة قد تفشل بمرحلة مختلفة عن غيرها ضمن
// نفس الطلب، فيُحفظ رمز/رسالة الخطأ نصياً في عنصر تلك الصورة فقط، بدل
// إسقاط الطلب بالكامل).
type stageError struct {
	code int
	msg  string
}

func (e *stageError) Error() string { return e.msg }

func (s *Service) handleTranslate(r *http.Request) (translateResult, error) {
	var req translateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		return translateResult{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]string{"error": "جسم الطلب ليس JSON صالح"},
		}
	}

	result, err := s.translateImage(r.Context(), req)
	if err != nil {
		var se *stageError
		if errors.As(err, &se) {
			return translateResult{}, &httpx.HTTPError{Code: se.code, Body: map[string]string{"error": se.msg}}
		}
		return translateResult{}, &httpx.HTTPError{Code: http.StatusInternalServerError, Body: map[string]string{"error": err.Error()}}
	}
	return result, nil
}

// translateImage هي خط الأنابيب الكامل (resolve -> OCR -> ترجمة كل
// منطقة -> إعادة رسم) بمعزل عن أي تفاصيل HTTP — لا تعرف شيئاً عن
// httpx.HTTPError أو *http.Request. هذا ما يجعلها قابلة لإعادة الاستخدام
// حرفياً من handleTranslate (صورة واحدة، خطأ HTTP واحد) وhandleTranslateBatch
// (عدة صور بالتزامن، خطأ نصي مستقل لكل صورة لا يوقف باقي الدفعة).
func (s *Service) translateImage(ctx context.Context, req translateRequest) (translateResult, error) {
	imgB64, err := s.resolveImageBase64(ctx, req)
	if err != nil {
		return translateResult{}, &stageError{http.StatusBadRequest, err.Error()}
	}
	imgBytes, err := base64.StdEncoding.DecodeString(imgB64)
	if err != nil {
		return translateResult{}, &stageError{http.StatusBadRequest, "image_base64 غير صالح"}
	}

	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()

	// 1) OCR (gosseract في العملية نفسها — راجع ocr.go)
	regions, err := runOCR(imgBytes, req.SourceLang)
	if err != nil {
		return translateResult{}, &stageError{http.StatusInternalServerError, "فشل استخراج النص من الصورة: " + err.Error()}
	}
	if len(regions) == 0 {
		return translateResult{}, &stageError{http.StatusUnprocessableEntity, "لم يتم العثور على أي نص في الصورة"}
	}

	// 2) Translate every region that is not already Arabic.
	for i, reg := range regions {
		if reg.Text == "" || arabicRegex.MatchString(reg.Text) {
			continue // عربي أصلاً أو فارغ -> يُرسل كما هو
		}
		translated, terr := s.translateToArabic(ctx, reg.Text)
		if terr != nil {
			// فشل ترجمة منطقة واحدة لا يجب أن يوقف الصورة كاملة
			continue
		}
		regions[i].Text = translated
	}

	// 3) Re-render the image with translated text in place (image/draw
	// + x/image/font في العملية نفسها — راجع render.go).
	outBytes, err := renderRegions(imgBytes, regions)
	if err != nil {
		return translateResult{}, &stageError{http.StatusInternalServerError, "فشل رسم النص المترجم: " + err.Error()}
	}

	return translateResult{ImageBase64: base64.StdEncoding.EncodeToString(outBytes), Regions: regions}, nil
}

// ─── دفعة (batch): ترجمة عدة صور بطلب واحد ────────────────────────────
//
// السياق: عملاء مثل جسر مانجا خارجي (يجلب صفحات فصل من موقع مصدر مباشرة
// بلا حاجة لتجاوز حماية Cloudflare) يريدون ترجمة كل صفحات الفصل، لا صورة
// واحدة. استدعاء /img_tr لكل صفحة على حدة يعني N اتصال HTTP منفصل لكل
// فصل بدل واحد، وينقل مسؤولية تحديد التزامن للعميل بدل الخادم الذي يملك
// المعرفة الفعلية بموارده. هذا الـ endpoint يقبل قائمة روابط، يترجمها هو
// نفسه بتزامن محدود داخلياً، ويعيدها كلها في رد واحد.

// concurrentTranslations: حد أقصى لعدد صور تُترجم في آن واحد ضمن طلب
// batch واحد — كل صورة تشغّل subprocess Node مرتين (OCR ثم render)
// بالإضافة لاستدعاء LLM خارجي لكل منطقة نص فيها؛ بدون هذا الحد، فصل
// مانجا طويل (30-50 صفحة) سيُطلق نفس العدد من عمليات Node متزامنة دفعة
// واحدة على حاوية بموارد محدودة — نفس فئة الخطر الذي عولج بنفس الحل
// (قناة semaphore) لعمليات Chromium في plugins/pinterest وplugins/mangabridge.
const concurrentTranslations = 3

// maxBatchImages: حد أقصى لعدد الصور المسموح بها في طلب batch واحد —
// يمنع طلباً واحداً من الاستحواذ على الحاوية بالكامل لوقت طويل. فصل
// مانجا عادي نادراً ما يتجاوز 60-70 صفحة؛ أي رقم أكبر بكثير على الأرجح
// إساءة استخدام لا استخداماً حقيقياً.
const maxBatchImages = 80

type batchTranslateRequest struct {
	ImageURLs  []string `json:"image_urls"`
	SourceLang string   `json:"source_lang"`
}

// batchImageResult يحمل النتيجة أو الخطأ لصورة واحدة ضمن الدفعة — Index
// يطابق موضعها في image_urls الأصلية بالضبط، حتى مع التزامن (كل نتيجة
// تُكتب لموضعها الخاص في مصفوفة بحجم ثابت، لا تُلحَق بالترتيب الذي تنتهي
// فيه goroutines). فشل صورة واحدة لا يمنع إرجاع نتائج البقية — يظهر فقط
// كـ Error في عنصرها هي، والرد الكلي يبقى 200.
type batchImageResult struct {
	Index       int      `json:"index"`
	ImageBase64 string   `json:"image_base64,omitempty"`
	Regions     []Region `json:"regions,omitempty"`
	Error       string   `json:"error,omitempty"`
}

type batchTranslateResult struct {
	Results []batchImageResult `json:"results"`
}

func (s *Service) handleTranslateBatch(r *http.Request) (batchTranslateResult, error) {
	var req batchTranslateRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		return batchTranslateResult{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]string{"error": "جسم الطلب ليس JSON صالح"},
		}
	}
	if len(req.ImageURLs) == 0 {
		return batchTranslateResult{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]string{"error": "يجب توفير image_urls بعنصر واحد على الأقل"},
		}
	}
	if len(req.ImageURLs) > maxBatchImages {
		return batchTranslateResult{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]string{"error": fmt.Sprintf("عدد الصور (%d) يتجاوز الحد المسموح لكل طلب (%d)", len(req.ImageURLs), maxBatchImages)},
		}
	}

	results := make([]batchImageResult, len(req.ImageURLs))
	sem := make(chan struct{}, concurrentTranslations)
	var wg sync.WaitGroup

	for i, imgURL := range req.ImageURLs {
		wg.Add(1)
		go func(i int, imgURL string) {
			defer wg.Done()

			sem <- struct{}{}
			defer func() { <-sem }()

			res, err := s.translateImage(r.Context(), translateRequest{ImageURL: imgURL, SourceLang: req.SourceLang})
			if err != nil {
				results[i] = batchImageResult{Index: i, Error: err.Error()}
				return
			}
			results[i] = batchImageResult{Index: i, ImageBase64: res.ImageBase64, Regions: res.Regions}
		}(i, imgURL)
	}
	wg.Wait()

	return batchTranslateResult{Results: results}, nil
}

func (s *Service) resolveImageBase64(ctx context.Context, req translateRequest) (string, error) {
	if req.ImageBase64 != "" {
		return req.ImageBase64, nil
	}
	if req.ImageURL == "" {
		return "", fmt.Errorf("يجب توفير image_url أو image_base64")
	}

	// حماية SSRF + DNS rebinding: SafeFetch يتحقق من الرابط ثم يثبّت
	// (pin) نفس العنوان المتحقق منه للاتصال الفعلي — بدل الاعتماد على
	// http.Client الذي كان سيعيد حلّ DNS بشكل مستقل وقت الاتصال (راجع
	// التعليق التفصيلي في internal/netguard/netguard.go). كما يفرض حد
	// حجم أقصى (maxImageBytes) بدل io.ReadAll غير محدود سابقاً.
	data, err := netguard.SafeFetch(ctx, s.client, req.ImageURL, maxImageBytes)
	if err != nil {
		return "", fmt.Errorf("رابط الصورة مرفوض أو تعذّر تحميلها: %w", err)
	}
	return base64.StdEncoding.EncodeToString(data), nil
}

// maxImageBytes: حد أقصى لحجم أي صورة تُجلب من رابط مُرسَل من المستخدم —
// بدونه، خادم بعيد يمكن أن يبث استجابة ضخمة ويستنزف ذاكرة العملية.
const maxImageBytes = 20 * 1024 * 1024 // 20MB

// translateToArabic uses whichever key is already configured in this
// project (Groq preferred for latency, Gemini as fallback).
func (s *Service) translateToArabic(ctx context.Context, text string) (string, error) {
	if key := os.Getenv("GROQ_API_KEY"); key != "" {
		if out, err := s.translateViaGroq(ctx, key, text); err == nil {
			return out, nil
		}
	}
	if key := os.Getenv("GEMINI_API_KEY"); key != "" {
		return s.translateViaGemini(ctx, key, text)
	}
	return "", fmt.Errorf("لا يوجد GROQ_API_KEY أو GEMINI_API_KEY مضبوط")
}

func (s *Service) translateViaGroq(ctx context.Context, apiKey, text string) (string, error) {
	body := map[string]any{
		"model": "llama-3.3-70b-versatile",
		"messages": []map[string]string{
			{"role": "system", "content": "ترجم النص التالي إلى العربية الفصحى المبسطة فقط. أعد الترجمة فقط بدون أي شرح أو علامات اقتباس أو تعليق إضافي."},
			{"role": "user", "content": text},
		},
		"temperature": 0.2,
	}
	data, _ := json.Marshal(body)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "https://api.groq.com/openai/v1/chat/completions", bytes.NewReader(data))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+apiKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := s.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("groq status %d", resp.StatusCode)
	}

	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(raw, &out); err != nil || len(out.Choices) == 0 {
		return "", fmt.Errorf("تعذر قراءة رد groq")
	}
	return out.Choices[0].Message.Content, nil
}

func (s *Service) translateViaGemini(ctx context.Context, apiKey, text string) (string, error) {
	url := "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key=" + apiKey
	body := map[string]any{
		"contents": []map[string]any{
			{"parts": []map[string]string{
				{"text": "ترجم النص التالي إلى العربية الفصحى المبسطة فقط، بدون أي شرح: " + text},
			}},
		},
	}
	data, _ := json.Marshal(body)

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := s.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("gemini status %d", resp.StatusCode)
	}

	var out struct {
		Candidates []struct {
			Content struct {
				Parts []struct {
					Text string `json:"text"`
				} `json:"parts"`
			} `json:"content"`
		} `json:"candidates"`
	}
	if err := json.Unmarshal(raw, &out); err != nil || len(out.Candidates) == 0 || len(out.Candidates[0].Content.Parts) == 0 {
		return "", fmt.Errorf("تعذر قراءة رد gemini")
	}
	return out.Candidates[0].Content.Parts[0].Text, nil
}
