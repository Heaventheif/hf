// ocr.go يقابل ocrAction في scripts/img_tr/img_tr_engine.js السابق
// (tesseract.js) — هنا Go خالص عبر github.com/otiai10/gosseract/v2،
// روابط cgo لمحرك Tesseract OCR نفسه (نفس محرك OCR بالضبط الذي كانت
// tesseract.js تستخدمه أصلاً عبر WASM؛ الفرق فقط رابط اللغة: cgo مباشر
// بدل WASM). يحتاج Tesseract مثبَّتاً فعلياً على الجهاز (راجع الـ
// Dockerfile: tesseract-ocr-dev وقت البناء، tesseract-ocr وحزم اللغات
// وقت التشغيل).
//
// ملاحظة إصلاح مهمة (2026): النسخة السابقة كانت تأخذ كل bounding box
// يُرجعه RIL_PARA بلا أي فلترة — تحليل Tesseract التخطيطي (layout
// analysis) كثيراً ما "يفهم" حدود رسم/ظلال/شعر في صفحة مانجا كأنها
// "فقرة نصية"، فتخرج مناطق وهمية (نص عشوائي: رموز/أرقام منفردة) تُترجم
// وتُرسم مكان فقاعات غير موجودة أصلاً، وأحياناً bbox يغطي الصفحة كلها
// فيُبيّضها بالكامل عند renderRegions. الحل هنا: فلترة صريحة على 3
// مستويات قبل قبول أي Region:
//  1. ثقة Tesseract نفسه (Confidence) — دونها لا معنى للنص المستخرَج.
//  2. المحتوى الفعلي للنص — لازم يحتوي حرفين حقيقيين على الأقل (وليس
//     مجرد أرقام/رموز تنظيمية اعتبرها Tesseract "كلمة").
//  3. مساحة bbox نسبة لمساحة الصورة كلها — منطقة ضخمة غير واقعية لفقاعة
//     كلام حقيقية على الأرجح خطأ في التحليل التخطيطي.
package img_tr

import (
	"bytes"
	"fmt"
	"image"
	"log"
	"strings"
	"unicode"

	"github.com/otiai10/gosseract/v2"
)

// langToTesseract: نفس LANG_TO_TESSERACT بالضبط من img_tr_engine.js
// السابق — خريطة كود لغة MangaDex (ISO 639-1، أو رمز خاص مثل pt-br) إلى
// كود حزمة تدريب Tesseract المقابل.
var langToTesseract = map[string]string{
	"en":    "eng",
	"ja":    "jpn",
	"ko":    "kor",
	"zh":    "chi_sim",
	"zh-hk": "chi_tra",
	"es":    "spa",
	"es-la": "spa",
	"pt":    "por",
	"pt-br": "por",
	"fr":    "fra",
	"de":    "deu",
	"it":    "ita",
	"ru":    "rus",
	"id":    "ind",
	"vi":    "vie",
	"th":    "tha",
	"pl":    "pol",
	"tr":    "tur",
	"nl":    "nld",
	"uk":    "ukr",
	"el":    "ell",
	"ar":    "ara",
}

// minOCRConfidence: أي منطقة ثقة Tesseract فيها أقل من هذا الرقم (0-100)
// تُرفض بالكامل — ثقة منخفضة هي التوقيع المعتاد لمنطقة "فُهمت" من حدود
// رسم أو ظل وليس نصاً فعلياً.
const minOCRConfidence = 60.0

// minRealLetters: أقل عدد حروف أبجدية حقيقية (بأي لغة، عبر
// unicode.IsLetter) يجب أن يحتويها نص المنطقة كي تُعتبر نصاً فعلياً
// وليس ضجيجاً (أرقام صفحة، رموز تنظيمية، خط مفرد...).
const minRealLetters = 2

// maxRegionAreaRatio: أقصى نسبة تسمح بها منطقة واحدة من مساحة الصورة
// كلها. فقاعة كلام حقيقية نادراً ما تتجاوز هذا؛ أي شيء أكبر غالباً خطأ
// تحليل تخطيطي (الصفحة كلها اعتُبرت "فقرة واحدة").
const maxRegionAreaRatio = 0.25

// resolveTesseractLangs يقابل resolveTesseractLangs في النسخة السابقة:
// يحدد قائمة لغات Tesseract المناسبة بناءً على لغة الفصل المصدر، مع
// 'eng' دائماً كاحتياطي (نصوص SFX كثيراً ما تبقى إنجليزية حتى في فصول
// غير إنجليزية) و'ara' دائماً (الهدف النهائي عربي، وبعض الصفحات قد تكون
// مُترجمة جزئياً بالفعل).
func resolveTesseractLangs(sourceLang string) []string {
	mapped, ok := langToTesseract[strings.ToLower(sourceLang)]
	if !ok {
		mapped = "eng"
	}
	seen := map[string]bool{"ara": true, mapped: true, "eng": true}
	langs := make([]string, 0, len(seen))
	for l := range seen {
		langs = append(langs, l)
	}
	return langs
}

// countRealLetters يعد عدد runes التي يعتبرها Unicode "حرفاً" (بأي
// أبجدية: لاتيني، عربي، ياباني...) — عمداً غير مقيّد بـ [a-zA-Z] لأن
// لغة المصدر قد تكون يابانية/كورية/عربية جزئياً.
func countRealLetters(s string) int {
	n := 0
	for _, r := range s {
		if unicode.IsLetter(r) {
			n++
		}
	}
	return n
}

// isLikelyGarbage يجمّع فحوصات المحتوى: نص فارغ بعد التقليم، أو أقل من
// minRealLetters حرفاً حقيقياً، يُعتبر ضجيجاً (أرقام صفحة، خطوط رسم
// اعتُبرت خطأً "كلمة"، رموز تنظيمية منفردة...).
func isLikelyGarbage(text string) bool {
	trimmed := strings.TrimSpace(text)
	if trimmed == "" {
		return true
	}
	return countRealLetters(trimmed) < minRealLetters
}

// imageAreaFromBytes يقرأ فقط أبعاد الصورة (بلا فك الصورة كاملة في
// الذاكرة) لحساب مساحتها الكلية — تُستخدم كمقام لفلترة نسبة مساحة كل
// bbox.
func imageAreaFromBytes(imgBytes []byte) (int, error) {
	cfg, _, err := image.DecodeConfig(bytes.NewReader(imgBytes))
	if err != nil {
		return 0, fmt.Errorf("تعذّر قراءة أبعاد الصورة: %w", err)
	}
	if cfg.Width <= 0 || cfg.Height <= 0 {
		return 0, fmt.Errorf("أبعاد صورة غير صالحة: %dx%d", cfg.Width, cfg.Height)
	}
	return cfg.Width * cfg.Height, nil
}

// runOCR يقابل ocrAction في img_tr_engine.js السابق: يشغّل Tesseract على
// بايتات الصورة الخام مباشرة (gosseract يفكّها داخلياً عبر Leptonica —
// لا حاجة لفكّها في Go أولاً) بمجموعة لغات مناسبة، ثم يحوّل كل فقرة
// (RIL_PARA — نفس مستوى data.paragraphs في tesseract.js سابقاً) إلى
// Region واحدة مع مستطيل إحاطتها — بعد رفض أي منطقة تفشل فلترة الثقة أو
// المحتوى أو المساحة (راجع الملاحظة أعلى الملف).
func runOCR(imgBytes []byte, sourceLang string) ([]Region, error) {
	imgArea, err := imageAreaFromBytes(imgBytes)
	if err != nil {
		return nil, err
	}

	client := gosseract.NewClient()
	defer client.Close()

	langs := resolveTesseractLangs(sourceLang)
	if err := client.SetLanguage(langs...); err != nil {
		return nil, fmt.Errorf("تعذّر ضبط لغات OCR (%v): %w", langs, err)
	}
	if err := client.SetImageFromBytes(imgBytes); err != nil {
		return nil, fmt.Errorf("تعذّر قراءة الصورة لـ OCR: %w", err)
	}

	boxes, err := client.GetBoundingBoxes(gosseract.RIL_PARA)
	if err != nil {
		return nil, fmt.Errorf("ocr error: %w", err)
	}

	regions := make([]Region, 0, len(boxes))
	for _, b := range boxes {
		text := strings.TrimSpace(b.Word)
		if text == "" {
			continue
		}

		// 1) فلترة الثقة: gosseract يرجع Confidence بمقياس 0-100 لكل box.
		if b.Confidence < minOCRConfidence {
			log.Printf("[img_tr/ocr] رفض منطقة بثقة منخفضة (%.1f < %.1f): %q", b.Confidence, minOCRConfidence, text)
			continue
		}

		// 2) فلترة المحتوى: نص بلا حروف حقيقية كافية = ضجيج.
		if isLikelyGarbage(text) {
			log.Printf("[img_tr/ocr] رفض منطقة بلا محتوى نصي حقيقي كافٍ: %q", text)
			continue
		}

		w := b.Box.Max.X - b.Box.Min.X
		if w < 1 {
			w = 1
		}
		h := b.Box.Max.Y - b.Box.Min.Y
		if h < 1 {
			h = 1
		}

		// 3) فلترة المساحة: bbox أكبر من الحد المسموح نسبة لمساحة الصورة
		// كلها غالباً خطأ تحليل تخطيطي (الصفحة كلها اعتُبرت فقرة واحدة).
		if imgArea > 0 && float64(w*h) > maxRegionAreaRatio*float64(imgArea) {
			log.Printf("[img_tr/ocr] رفض منطقة ضخمة (%dx%d = %.1f%% من الصورة): %q",
				w, h, 100*float64(w*h)/float64(imgArea), text)
			continue
		}

		regions = append(regions, Region{Text: text, BBox: [4]int{b.Box.Min.X, b.Box.Min.Y, w, h}})
	}
	return regions, nil
}
