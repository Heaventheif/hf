// render.go يقابل renderAction في scripts/img_tr/img_tr_engine.js
//
// ملاحظة إصلاح مهمة (2026) — تراجع متعمَّد عن محاولة الرسم اليدوي عبر
// image/draw + golang.org/x/image/font:
//
// تلك المحاولة كانت توثّق بصدق قيدها الحقيقي: مكتبات Go القياسية
// (golang.org/x/image/font) لا تتضمن محرك تشكيل نصوص عربي (لا HarfBuzz
// ولا Pango) — فكانت ترسم كل حرف Unicode منفصلاً بشكله المعزول
// (isolated form) بدل الاتصال الطبيعي بين الحروف، وحتى عكس ترتيب الرموز
// (visualOrderRTL) كان تقريباً غير كافٍ. هذا سبب رئيسي وراء سوء شكل
// النص العربي المُخرَج.
//
// الحل هنا: العودة لنفس أسلوب النسخة القديمة (SVG + librsvg عبر Pango
// داخلياً) لكن من Go مباشرة بدل Node: نبني SVG بسيط (خلفية شفافة، عنصر
// <text> واحد لكل Region بـ direction="rtl") ثم نُرمّزه لصورة PNG عبر
// استدعاء rsvg-convert كعملية خارجية (نفس الثنائي المستخدم في النسخة
// القديمة تحت الغطاء)، ثم نُركّب الناتج فوق الصورة الأصلية بعد تبييض
// مناطق النص القديم. هذا يعيد تشكيل الحروف العربية الصحيح (اتصال
// الحروف حسب موقعها في الكلمة) لأن Pango هو من يرسم النص فعلياً، لا
// كودنا.
//
// المتطلب الوحيد الإضافي: تثبيت حزمة librsvg2-bin (توفّر rsvg-convert)
// في الـ Dockerfile — بجانب fonts-noto-core الموجودة أصلاً لأجل خط Noto
// Sans Arabic.
package img_tr

import (
	"bytes"
	"fmt"
	"image"
	"image/draw"
	"image/jpeg"
	"image/png"
	"log"
	"os"
	"os/exec"
	"strings"

	_ "golang.org/x/image/webp" // يسجّل decoder فقط عبر image.RegisterFormat — لا مُرمِّز WEBP متاح في Go خالص
)

// arabicFontFamily: نفس font-family المستخدم في img_tr_engine.js
// السابق (Pango/fontconfig يبحث عنه بالاسم عبر المكتبة المثبَّتة في
// الـ Dockerfile: حزمة fonts-noto-core).
const arabicFontFamily = "Noto Sans Arabic"

// rsvgConvertBin: اسم الثنائي القابل للاستبدال عبر متغير بيئة لأغراض
// الاختبار/بيئات مختلفة (نفس نمط IMG_TR_FONT_PATH القديم).
func rsvgConvertBin() string {
	if p := os.Getenv("IMG_TR_RSVG_CONVERT_PATH"); p != "" {
		return p
	}
	return "rsvg-convert"
}

// maxRegionAreaRatio مُعرَّفة في ocr.go وتُستخدم هناك كخط دفاع أول. هنا
// نضيف خط دفاع ثانٍ مستقل (defense in depth): حتى لو مرّت منطقة كبيرة
// بطريق آخر (استدعاء مباشر لـ renderRegions من كود غير runOCR، مثلاً
// اختبار أو مسار مستقبلي)، لا تُرسم فوق الصورة كاملة أو معظمها.

// maxFontSize: حد أقصى لحجم الخط بالبكسل — حتى مع bbox كبير لكن شرعي
// (فقاعة كلام كبيرة فعلاً)، نص أكبر من هذا يخرج عن التناسب المعتاد
// لصفحات المانجا ويبدو مبالغاً فيه.
const maxFontSize = 48.0

// renderRegions يقابل renderAction في img_tr_engine.js السابق: لكل
// Region، يمسح مستطيل إحاطته بمستطيل أبيض (إخفاء النص الأصلي) ثم يرسم
// النص المترجَم فوقه عبر SVG+librsvg (تشكيل عربي صحيح)، محاذى لليمين
// (نفس text-anchor="end" سابقاً).
func renderRegions(imgBytes []byte, regions []Region) ([]byte, error) {
	img, format, err := image.Decode(bytes.NewReader(imgBytes))
	if err != nil {
		return nil, fmt.Errorf("تعذّر قراءة الصورة: %w", err)
	}

	// نسخ إلى RGBA قابلة للرسم عليها مباشرة عبر image/draw (الصورة
	// المفكوكة أصلاً — خصوصاً JPEG — عادة YCbCr، غير قابلة للاستخدام
	// كوجهة draw.Draw مباشرة).
	bounds := img.Bounds()
	dst := image.NewRGBA(bounds)
	draw.Draw(dst, bounds, img, bounds.Min, draw.Src)

	imgW := bounds.Dx()
	imgH := bounds.Dy()
	imgArea := imgW * imgH

	var svgTextElems []string
	for _, region := range regions {
		if region.Text == "" {
			continue
		}
		x, y, w, h := region.BBox[0], region.BBox[1], region.BBox[2], region.BBox[3]
		if w <= 0 || h <= 0 {
			continue
		}

		// خط دفاع ثانٍ مستقل عن فلترة ocr.go — راجع الملاحظة أعلى الملف.
		if imgArea > 0 && float64(w*h) > maxRegionAreaRatio*float64(imgArea) {
			log.Printf("[img_tr/render] رفض رسم منطقة ضخمة (%dx%d = %.1f%% من الصورة) في مرحلة الرسم: %q",
				w, h, 100*float64(w*h)/float64(imgArea), region.Text)
			continue
		}

		rect := image.Rect(x, y, x+w, y+h).Intersect(bounds)
		if rect.Empty() {
			continue
		}

		// تبييض منطقة النص الأصلي.
		draw.Draw(dst, rect, image.White, image.Point{}, draw.Src)

		fontSize := float64(h) * 0.65
		if fontSize < 10 {
			fontSize = 10
		}
		if fontSize > maxFontSize {
			fontSize = maxFontSize
		}

		// نفس صيغة y=Math.floor(h/2 + fontSize/3) من النسخة السابقة —
		// baseline-y بالنسبة لعنصر <text> في SVG له نفس المعنى بالضبط.
		baselineY := y + int(float64(h)/2+fontSize/3)
		rightX := x + w - 4

		svgTextElems = append(svgTextElems, fmt.Sprintf(
			`<text x="%d" y="%d" font-family="%s" font-size="%.2f" fill="black" text-anchor="end" direction="rtl" unicode-bidi="bidi-override">%s</text>`,
			rightX, baselineY, arabicFontFamily, fontSize, xmlEscape(region.Text),
		))
	}

	if len(svgTextElems) > 0 {
		svg := fmt.Sprintf(
			`<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">%s</svg>`,
			imgW, imgH, imgW, imgH, strings.Join(svgTextElems, ""),
		)

		textLayer, err := rasterizeSVG(svg, imgW, imgH)
		if err != nil {
			// فشل الرسم عبر librsvg لا يجب أن يُسقط الصورة كاملة (الصفحة
			// ستُرسل بمناطقها مبيضّة بلا ترجمة بدل ما تفشل الترجمة كلها).
			log.Printf("[img_tr/render] فشل تركيب طبقة النص عبر rsvg-convert: %v", err)
		} else {
			draw.Draw(dst, bounds, textLayer, bounds.Min, draw.Over)
		}
	}

	var buf bytes.Buffer
	switch format {
	case "png", "webp":
		// WEBP لا يوجد له مُرمِّز (encoder) ضمن مكتبات Go الخالصة المتاحة —
		// نُخرج PNG بدلاً منه (بلا فقدان جودة إضافي، أكبر حجماً من WEBP
		// الأصلي). فرق سلوك حقيقي عن sharp (التي كانت تحافظ على WEBP).
		if err := png.Encode(&buf, dst); err != nil {
			return nil, fmt.Errorf("تعذّر ترميز PNG: %w", err)
		}
	default: // jpeg أو أي صيغة أخرى غير معروفة
		if err := jpeg.Encode(&buf, dst, &jpeg.Options{Quality: 92}); err != nil {
			return nil, fmt.Errorf("تعذّر ترميز JPEG: %w", err)
		}
	}
	return buf.Bytes(), nil
}

// rasterizeSVG يُشغّل rsvg-convert كعملية خارجية: يمرر SVG عبر stdin
// ويستقبل PNG عبر stdout (بلا ملفات مؤقتة على القرص) — Pango (المحرك
// الداخلي لـ librsvg) هو من يتولى تشكيل الحروف العربية الصحيح هنا،
// نفس ما كانت تفعله النسخة القديمة (SVG + librsvg من Node).
func rasterizeSVG(svg string, width, height int) (image.Image, error) {
	cmd := exec.Command(rsvgConvertBin(),
		"--format=png",
		fmt.Sprintf("--width=%d", width),
		fmt.Sprintf("--height=%d", height),
	)
	cmd.Stdin = strings.NewReader(svg)

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("rsvg-convert فشل: %w (stderr: %s)", err, stderr.String())
	}

	img, err := png.Decode(&stdout)
	if err != nil {
		return nil, fmt.Errorf("تعذّر قراءة PNG الناتج من rsvg-convert: %w", err)
	}
	return img, nil
}

// xmlEscape يهرب الرموز الخاصة بـ XML/SVG داخل نص المستخدم (الترجمة)
// قبل تضمينه في مستند SVG — تفادياً لكسر بنية الـ SVG أو حقن عناصر غير
// مرغوبة عبر نص مترجَم قادم من مصدر خارجي غير موثوق بالكامل (نص
// المانجا المصدر).
func xmlEscape(s string) string {
	replacer := strings.NewReplacer(
		"&", "&amp;",
		"<", "&lt;",
		">", "&gt;",
		`"`, "&quot;",
		"'", "&apos;",
	)
	return replacer.Replace(s)
}

// visualOrderRTL: أُبقيت لأجل التوافق (اختبارات قديمة تعتمد عليها)، لكنها
// لم تعد تُستخدم في مسار الرسم الفعلي — SVG direction="rtl" +
// unicode-bidi="bidi-override" أعلاه يتوليان اتجاه/تشكيل النص عبر Pango
// بدل هذا التقريب اليدوي.
func visualOrderRTL(s string) string {
	runes := []rune(s)
	for i, j := 0, len(runes)-1; i < j; i, j = i+1, j-1 {
		runes[i], runes[j] = runes[j], runes[i]
	}
	return string(runes)
}
