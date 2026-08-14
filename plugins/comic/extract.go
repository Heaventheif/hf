// استخراج data-label من صفحة سلسلة، وروابط صور صفحات الفصل — بـ regexp
// خفيف على نمط plugins/novel/novel.go تماماً (لا goquery، محجوبة في بيئة
// بناء هذا المشروع — راجع novel.go).
package comic

import (
	"regexp"
	"strconv"
	"strings"
)

var mangaWidgetOpenTagRe = regexp.MustCompile(`(?is)<div\b[^>]*class\s*=\s*["'][^"']*manga-widget[^"']*["'][^>]*>`)
var dataLabelAttrRe = regexp.MustCompile(`(?i)data-label\s*=\s*["']([^"']*)["']`)

// extractMangaLabel يطابق $('.manga-widget[data-label]').first().attr('data-label')
// في comic.js حرفياً: أول عنصر بصنف manga-widget يحمل data-label.
func extractMangaLabel(htmlStr string) (string, bool) {
	openTag := mangaWidgetOpenTagRe.FindString(htmlStr)
	if openTag == "" {
		return "", false
	}
	m := dataLabelAttrRe.FindStringSubmatch(openTag)
	if m == nil {
		return "", false
	}
	return m[1], true
}

var imgTagRe = regexp.MustCompile(`(?is)<img\b[^>]*>`)
var srcAttrRe = regexp.MustCompile(`(?i)\bsrc\s*=\s*["']([^"']*)["']`)
var dataSrcAttrRe = regexp.MustCompile(`(?i)\bdata-src\s*=\s*["']([^"']*)["']`)
var origWidthAttrRe = regexp.MustCompile(`(?i)\bdata-original-width\s*=\s*["'](\d+)["']`)
var origHeightAttrRe = regexp.MustCompile(`(?i)\bdata-original-height\s*=\s*["'](\d+)["']`)

// isPageImage يقابل isPageImage في comic.js حرفياً: من blogger فقط، ليست
// placeholder، وإما نسبة بورتريه (h > w) أو عرض كبير (>= 800)، أو لا
// أبعاد موثوقة إطلاقاً (تُقبَل افتراضياً حينها، كما في الأصل).
func isPageImage(src string, width, height int, hasDims bool) bool {
	if src == "" || !strings.HasPrefix(src, "https://blogger.googleusercontent.com/") {
		return false
	}
	if strings.Contains(src, "dagruel-no-image") {
		return false
	}
	if hasDims {
		if height > width {
			return true
		}
		return width >= 800
	}
	return true
}

// extractChapterImages يفحص كل وسوم <img> في الصفحة (بدل حصر البحث داخل
// div.separator أولاً كما يفعل fetchChapterImages الحالي) لأن الفلتر
// (isPageImage) هو ما يميّز فعلياً — راجع "تبسيط موثَّق" في خطة التنفيذ
// قبل الاعتماد على هذا في الإنتاج.
func extractChapterImages(htmlStr string) []string {
	seen := map[string]bool{}
	var out []string

	for _, tag := range imgTagRe.FindAllString(htmlStr, -1) {
		src := ""
		if m := srcAttrRe.FindStringSubmatch(tag); m != nil {
			src = m[1]
		} else if m := dataSrcAttrRe.FindStringSubmatch(tag); m != nil {
			src = m[1]
		}

		width, height, hasDims := 0, 0, false
		wm := origWidthAttrRe.FindStringSubmatch(tag)
		hm := origHeightAttrRe.FindStringSubmatch(tag)
		if wm != nil && hm != nil {
			width, _ = strconv.Atoi(wm[1])
			height, _ = strconv.Atoi(hm[1])
			hasDims = true
		}

		if !isPageImage(src, width, height, hasDims) {
			continue
		}
		if seen[src] {
			continue
		}
		seen[src] = true
		out = append(out, src)
	}
	return out
}
