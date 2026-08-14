// Package novel يقابل plugins/novel.py: جلب فصول الروايات من freewebnovel.com.
//
// ملاحظة تصميم: بايثون استخدمت BeautifulSoup (محلل HTML كامل + محدّدات CSS).
// المكتبات المكافئة الناضجة في Go (goquery، وحتى net/html نفسها) تعيش تحت
// golang.org/x/net، وهو نطاق كان محجوباً سابقاً في بيئة التطوير. بدل تأجيل
// الميزة، كتبنا محلّل HTML خفيف بدون أي تبعية خارجية (stdlib فقط: regexp +
// strings + html.UnescapeString) يغطي بالضبط المحددات الثابتة التي يحتاجها
// هذا الموقع تحديداً (نفس قائمة selectors في SITE ببايثون) — كافٍ ومضبوط
// لهذه الحالة الخاصة، ويبني فوراً بدون إنترنت في أي بيئة. هذا المحلّل بقي
// كما هو؛ ما تغيّر هو طريقة *جلب* الصفحة عند فشل HTTP البسيط (كلاودفلير
// مثلاً) — الآن عبر internal/browser (chromedp حقيقي) بدل تخطٍّ صامت.
//
// ملاحظة ترحيل: extract/extractTitle/slugify/isFiltered وبقية الدوال
// المساعدة النقية (بلا حالة) تبقى دوال حزمة حرة عمداً — novel_test.go
// يستدعيها مباشرة كدوال حزمة، وتحويلها لـ methods كان سيكسر تلك
// الاختبارات بلا أي فائدة بنيوية (هذه الدوال لا تحتاج أي حالة Service
// أصلاً). فقط ما يحتاج فعلياً حالة مشتركة (httpClient، الكاش) انتقل إلى
// Service.
package novel

import (
	"context"
	"encoding/json"
	"fmt"
	"html"
	"log"
	"math/rand"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"sunkenbot/internal/browser"
	"sunkenbot/internal/httpx"
	"sunkenbot/internal/plugins"
	"sunkenbot/internal/strutil"

	"github.com/chromedp/chromedp"
)

const Description = "جلب فصول الروايات من freewebnovel.com"

// Service يحمل عميل http.Client (بلا مهلة عامة — كل طلب يضبط مهلته عبر
// context، تماماً كما كان httpClient الحزمي القديم) وحالة الكاش
// (كانت جميعها متغيرات حزمة عامة: sync.Mutex + map — ممنوعة صراحة على
// مستوى الحزمة، راجع §2.2 والقيود الصارمة) كحقول صريحة.
type Service struct {
	client *http.Client

	cacheMu    sync.Mutex
	cache      map[string]cacheItem
	cacheOrder []string // لتتبّع ترتيب الإدخال (لمحاكاة next(iter(dict)) في بايثون)
}

func New(client *http.Client) *Service {
	return &Service{client: client, cache: map[string]cacheItem{}}
}

func (s *Service) Name() string { return "novel" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/novel", Handler: httpx.Handle(s.handleGetChapter)},
		{Method: "GET", Pattern: "/novel/sites", Handler: httpx.Handle(s.handleListSites)},
		{Method: "DELETE", Pattern: "/novel/cache", Handler: httpx.Handle(s.handleClearCache)},
	}
}

// ─── Cache (تقابل _cache/_cache_get/_cache_set في بايثون) ───────────

type cacheItem struct {
	value   map[string]any
	expires time.Time
}

const cacheTTL = time.Hour
const cacheMaxSize = 300

func (s *Service) cacheGet(key string) (map[string]any, bool) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	item, ok := s.cache[key]
	if !ok {
		return nil, false
	}
	if time.Now().After(item.expires) {
		delete(s.cache, key)
		s.removeFromOrderLocked(key)
		return nil, false
	}
	return item.value, true
}

// removeFromOrderLocked يحذف key من s.cacheOrder إن وُجد. يجب استدعاؤها
// مع الاحتفاظ بـ s.cacheMu بالفعل (locked). مسح صريح من الشريحة عند كل
// إزالة من s.cache (سواء بانتهاء الصلاحية في cacheGet أو بالإخلاء في
// cacheSet) يمنع تراكم مفاتيح منتهية الصلاحية في cacheOrder إلى ما لا
// نهاية (تسرّب ذاكرة بطيء لكن غير محدود مع طول مدة التشغيل).
func (s *Service) removeFromOrderLocked(key string) {
	for i, k := range s.cacheOrder {
		if k == key {
			s.cacheOrder = append(s.cacheOrder[:i], s.cacheOrder[i+1:]...)
			return
		}
	}
}

func (s *Service) cacheSet(key string, value map[string]any) {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	if _, exists := s.cache[key]; !exists {
		// أخلِ أقدم المفاتيح حتى يعود حجم الخريطة تحت الحد الأقصى. حلقة
		// (وليس شرط واحد) لأن s.cacheOrder قد يحوي مفاتيح لم تعد موجودة
		// في s.cache أصلاً (أُزيلت سابقاً)، فتخطّيها ببساطة دون عدّها.
		for len(s.cache) >= cacheMaxSize && len(s.cacheOrder) > 0 {
			oldest := s.cacheOrder[0]
			s.cacheOrder = s.cacheOrder[1:]
			delete(s.cache, oldest)
		}
		s.cacheOrder = append(s.cacheOrder, key)
	}
	s.cache[key] = cacheItem{value: value, expires: time.Now().Add(cacheTTL)}
}

func (s *Service) cacheClear() int {
	s.cacheMu.Lock()
	defer s.cacheMu.Unlock()
	n := len(s.cache)
	s.cache = map[string]cacheItem{}
	s.cacheOrder = nil
	return n
}

// ─── User agents ─────────────────────────────────────────────────

var userAgents = []string{
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
	"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
}

func randomUA() string { return userAgents[rand.Intn(len(userAgents))] }

func setBrowserHeaders(req *http.Request) {
	req.Header.Set("User-Agent", randomUA())
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Cache-Control", "no-cache")
}

// ─── slugify ─────────────────────────────────────────────────────

var (
	nonAlnumRe = regexp.MustCompile(`[^a-z0-9]+`)
	trimDashRe = regexp.MustCompile(`^-|-$`)
)

func slugify(name string) string {
	lower := strings.ToLower(strings.ReplaceAll(name, "'", ""))
	dashed := nonAlnumRe.ReplaceAllString(lower, "-")
	return trimDashRe.ReplaceAllString(dashed, "")
}

// ─── فلترة النص (نفس FILTER_WORDS/STOLEN في بايثون) ────────────────

var filterWords = []string{
	"advertisement", "report chapter", "next chapter", "prev chapter",
	"table of contents", "access denied", "just a moment", "cloudflare",
	"enable javascript", "read more at",
	"cookie", "privacy", "terms of service", "subscribe",
}

var stolenPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)stol(en|e)\s+(content|chapter)`),
	regexp.MustCompile(`(?i)if\s+you.re\s+reading\s+this\s+on`),
	regexp.MustCompile(`(?i)unauthorized\s+(use|reproduction)`),
}

func isFiltered(t string) bool {
	if len(t) < 15 {
		return true
	}
	lo := strings.ToLower(t)
	for _, w := range filterWords {
		if strings.Contains(lo, w) {
			return true
		}
	}
	for _, p := range stolenPatterns {
		if p.MatchString(t) {
			return true
		}
	}
	return false
}

var (
	multiSpaceRe = regexp.MustCompile(`\s{2,}`)
	multiDotRe   = regexp.MustCompile(`\.{4,}`)
)

func clean(t string) string {
	t = strings.ReplaceAll(t, "\u00a0", " ")
	t = multiDotRe.ReplaceAllString(t, "...")
	t = multiSpaceRe.ReplaceAllString(t, " ")
	return strings.TrimSpace(t)
}

// ─── محلّل HTML خفيف (بدون تبعيات) ───────────────────────────────

var tagStripRe = regexp.MustCompile(`(?is)<[^>]*>`)

// textOf يقابل .get_text() في بايثون على جزء HTML صغير: يزيل الوسوم
// الداخلية ويفك ترميز HTML entities.
func textOf(fragment string) string {
	return html.UnescapeString(tagStripRe.ReplaceAllString(fragment, ""))
}

type tagEvent struct {
	pos, end int
	open     bool
}

// extractBalancedTag يبحث عن أول وسم <tag> يحقق matchAttrs، ويُرجع
// محتواه الداخلي مع موازنة الوسوم المتداخلة من نفس النوع (تقابل
// soup.select_one(sel) من ناحية اختيار أول عنصر مطابق).
func extractBalancedTag(htmlStr, tag string, matchAttrs func(string) bool) (string, bool) {
	openRe := regexp.MustCompile(`(?is)<` + tag + `(?:\s[^>]*)?>`)
	closeRe := regexp.MustCompile(`(?is)</` + tag + `\s*>`)

	var events []tagEvent
	for _, o := range openRe.FindAllStringIndex(htmlStr, -1) {
		events = append(events, tagEvent{o[0], o[1], true})
	}
	for _, c := range closeRe.FindAllStringIndex(htmlStr, -1) {
		events = append(events, tagEvent{c[0], c[1], false})
	}
	sort.Slice(events, func(i, j int) bool { return events[i].pos < events[j].pos })

	for i, e := range events {
		if !e.open {
			continue
		}
		openTagText := htmlStr[e.pos:e.end]
		if matchAttrs != nil && !matchAttrs(openTagText) {
			continue
		}
		depth := 1
		for j := i + 1; j < len(events); j++ {
			if events[j].open {
				depth++
			} else {
				depth--
				if depth == 0 {
					return htmlStr[e.end:events[j].pos], true
				}
			}
		}
	}
	return "", false
}

func attrEquals(attrName, value string) func(string) bool {
	re := regexp.MustCompile(`(?i)\b` + attrName + `\s*=\s*["']` + regexp.QuoteMeta(value) + `["']`)
	return func(openTag string) bool { return re.MatchString(openTag) }
}

func classToken(token string) func(string) bool {
	re := regexp.MustCompile(`(?i)\bclass\s*=\s*["']([^"']*)["']`)
	return func(openTag string) bool {
		m := re.FindStringSubmatch(openTag)
		if m == nil {
			return false
		}
		for c := range strings.FieldsSeq(m[1]) {
			if strings.EqualFold(c, token) {
				return true
			}
		}
		return false
	}
}

func classContainsSubstr(sub string) func(string) bool {
	re := regexp.MustCompile(`(?i)\bclass\s*=\s*["']([^"']*)["']`)
	return func(openTag string) bool {
		m := re.FindStringSubmatch(openTag)
		return m != nil && strings.Contains(strings.ToLower(m[1]), strings.ToLower(sub))
	}
}

// containerCandidateTags: عناصر HTML المرشّحة للاحتواء (تغطي الأغلبية
// الساحقة من مواقع روايات الويب الحقيقية).
var containerCandidateTags = []string{"div", "section", "article", "main"}

// contentSelector يقابل سطراً واحداً من selectors في SITE ببايثون.
type contentSelector struct {
	tag       string // "" = جرّب كل containerCandidateTags
	matchAttr func(string) bool
}

func findContainer(htmlStr string, selectors []contentSelector) (string, bool) {
	for _, sel := range selectors {
		if sel.tag != "" {
			if content, ok := extractBalancedTag(htmlStr, sel.tag, sel.matchAttr); ok && len(textOf(content)) > 200 {
				return content, true
			}
			continue
		}
		for _, tag := range containerCandidateTags {
			if content, ok := extractBalancedTag(htmlStr, tag, sel.matchAttr); ok && len(textOf(content)) > 200 {
				return content, true
			}
		}
	}
	return "", false
}

var paragraphRe = regexp.MustCompile(`(?is)<p(?:\s[^>]*)?>(.*?)</p>`)

func extractParagraphsFrom(containerHTML string) []string {
	matches := paragraphRe.FindAllStringSubmatch(containerHTML, -1)
	var paras []string
	for _, m := range matches {
		t := clean(textOf(m[1]))
		if len(t) > 15 && !isFiltered(t) {
			paras = append(paras, t)
		}
	}

	if len(paras) < 3 {
		// جرب التقسيم على الأسطر (بعد استبدال <br> بأسطر جديدة)
		withBreaks := regexp.MustCompile(`(?i)<br\s*/?>`).ReplaceAllString(containerHTML, "\n")
		lines := strings.Split(textOf(withBreaks), "\n")
		paras = paras[:0]
		for _, l := range lines {
			t := clean(l)
			if len(t) > 15 && !isFiltered(t) {
				paras = append(paras, t)
			}
		}
	}

	if len(paras) < 2 {
		text := textOf(containerHTML)
		sentences := regexp.MustCompile(`[.!?]\s+`).Split(text, -1)
		paras = paras[:0]
		for _, s := range sentences {
			t := clean(s)
			if len(t) > 20 && !isFiltered(t) {
				paras = append(paras, t)
			}
		}
	}

	if len(paras) >= 2 {
		return paras
	}
	return nil
}

// extract يقابل extract(html, selectors) في بايثون بالكامل.
func extract(htmlStr string, selectors []contentSelector) []string {
	// إزالة العناصر المزعجة أولاً (script/style/ins/.ads/noscript/nav/header/footer/.advertisement)
	cleaned := regexp.MustCompile(`(?is)<(script|style|noscript|nav|header|footer)\b[^>]*>.*?</(?:script|style|noscript|nav|header|footer)>`).
		ReplaceAllString(htmlStr, "")

	container, ok := findContainer(cleaned, selectors)
	if !ok {
		if c, ok2 := extractBalancedTag(cleaned, "div", classToken("m-read")); ok2 && len(textOf(c)) > 200 {
			container = c
		} else if body, ok3 := extractBalancedTag(cleaned, "body", nil); ok3 {
			container = body
		} else {
			return nil
		}
	}

	return extractParagraphsFrom(container)
}

var titleSplitRe = regexp.MustCompile(`[–\-|]`)

func extractTitle(htmlStr string) string {
	candidates := []contentSelector{
		{tag: "h1", matchAttr: classToken("tit")},
		{tag: "", matchAttr: classToken("tit")},
		{tag: "h1", matchAttr: nil},
	}
	for _, sel := range candidates {
		tags := []string{sel.tag}
		if sel.tag == "" {
			tags = []string{"h1", "div", "span"}
		}
		for _, tag := range tags {
			if content, ok := extractBalancedTag(htmlStr, tag, sel.matchAttr); ok {
				t := strings.TrimSpace(titleSplitRe.Split(textOf(content), 2)[0])
				if len(t) > 2 {
					return t
				}
			}
		}
	}

	if titleTag, ok := extractBalancedTag(htmlStr, "title", nil); ok {
		t := strings.TrimSpace(textOf(titleTag))
		if len(t) > 2 {
			return strings.TrimSpace(strings.SplitN(t, " - ", 2)[0])
		}
	}
	return ""
}

// ─── جلب الصفحة عبر HTTP ────────────────────────────────────────

func (s *Service) fetchPageHTTP(ctx context.Context, url string) string {
	const retries = 2
	for attempt := 0; attempt <= retries; attempt++ {
		ctxTimeout, cancel := context.WithTimeout(ctx, 30*time.Second)
		req, err := http.NewRequestWithContext(ctxTimeout, http.MethodGet, url, nil)
		if err == nil {
			setBrowserHeaders(req)
			resp, err := s.client.Do(req)
			if err == nil {
				body := readAllLimited(resp.Body, 5*1024*1024)
				resp.Body.Close()
				if resp.StatusCode == 200 && len(body) > 500 {
					lower := strings.ToLower(body)
					sample := lower
					if len(sample) > 3000 {
						sample = sample[:3000]
					}
					if strings.Contains(sample, "just a moment") || strings.Contains(sample, "cloudflare") {
						log.Printf("[novel] Cloudflare على %s", url)
						cancel()
						break
					}
					cancel()
					return body
				}
			}
		}
		cancel()
		time.Sleep(time.Second)
	}
	return ""
}

func readAllLimited(r interface{ Read([]byte) (int, error) }, limit int64) string {
	buf := make([]byte, 0, 64*1024)
	tmp := make([]byte, 32*1024)
	total := int64(0)
	for {
		n, err := r.Read(tmp)
		if n > 0 {
			total += int64(n)
			if total > limit {
				break
			}
			buf = append(buf, tmp[:n]...)
		}
		if err != nil {
			break
		}
	}
	return string(buf)
}

// fetchPageBrowser يقابل fetch_page_playwright في بايثون. يستخدم الآن
// internal/browser (chromedp حقيقي، بعد أن أصبح متاحاً في هذه البيئة)
// بدل التخطي الصامت السابق: يفتح جلسة Chrome بلا واجهة عبر browser.New
// (نفس stealth script + user agent عشوائي المستخدمين في pinterest/manga_bridge)،
// ينتقل إلى url، ينتظر زوال أي تحدي Cloudflare، ثم يُعيد outerHTML كاملاً
// لتغذية نفس extract()/extractTitle() الموجودين أعلاه — لا حاجة لتكرار
// منطق التحليل، فقط طريقة الجلب تغيّرت من HTTP بسيط إلى متصفح حقيقي.
func fetchPageBrowser(ctx context.Context, url string) string {
	bctx, cancel, err := browser.New(ctx, browser.Options{ExecPathEnv: "NOVEL_CHROMIUM_PATH"})
	if err != nil {
		log.Printf("[novel] [Browser] تعذّر تشغيل Chromium: %v", err)
		return ""
	}
	defer cancel()

	navCtx, navCancel := context.WithTimeout(bctx, 45*time.Second)
	defer navCancel()

	var htmlContent string
	if err := chromedp.Run(navCtx,
		chromedp.Navigate(url),
		chromedp.WaitNotPresent(`#challenge-form`, chromedp.ByID),
		chromedp.WaitNotPresent(`#cf-challenge-running`, chromedp.ByID),
		chromedp.WaitNotPresent(`.cf-browser-verification`, chromedp.ByQuery),
		chromedp.Sleep(2*time.Second),
		chromedp.OuterHTML("html", &htmlContent),
	); err != nil {
		log.Printf("[novel] [Browser] فشل التنقل إلى %s: %v", url, err)
		return ""
	}

	lower := strings.ToLower(htmlContent)
	sample := lower
	if len(sample) > 3000 {
		sample = sample[:3000]
	}
	if strings.Contains(sample, "just a moment") || strings.Contains(sample, "cloudflare") {
		log.Printf("[novel] [Browser] Cloudflare لا يزال ظاهراً بعد الانتظار على %s", url)
		return ""
	}

	return htmlContent
}

// ─── الموقع (SITE في بايثون) ─────────────────────────────────────

const siteName = "Freewebnovel"

func buildChapterURL(slug string, chapter int) string {
	return fmt.Sprintf("https://freewebnovel.com/novel/%s/chapter-%d", slug, chapter)
}

var contentSelectors = []contentSelector{
	{tag: "div", matchAttr: attrEquals("id", "chapter-content")},
	{tag: "div", matchAttr: classToken("chapter-content")},
	{tag: "div", matchAttr: attrEquals("id", "content")},
	{tag: "div", matchAttr: classToken("content")},
	{tag: "div", matchAttr: attrEquals("id", "reading-content")},
	{tag: "div", matchAttr: classToken("reading-content")},
	{tag: "div", matchAttr: attrEquals("id", "article")},
	{tag: "div", matchAttr: classContainsSubstr("chapter")},
	{tag: "div", matchAttr: classToken("txt")},
	{tag: "article", matchAttr: nil},
	{tag: "main", matchAttr: nil},
	{tag: "div", matchAttr: classToken("m-read")},
	{tag: "body", matchAttr: nil},
}

// ─── fetchChapter — المنطق الكامل مع الكاش ─────────────────────

// fetchChapter يجلب فصلاً من Freewebnovel. إن كان explicitSlug غير فارغ
// (حقل novel_id القادم من العميل)، يُستخدم مباشرة كـ slug الرابط بدل
// تخمينه تلقائياً عبر slugify(novelName) — مفيد عندما يعرف العميل الـ slug
// الدقيق مسبقاً (مثلاً من نتيجة بحث سابقة) ويريد تفادي أي التباس.
func (s *Service) fetchChapter(ctx context.Context, novelName string, chapterNum int, explicitSlug string) (map[string]any, error) {
	cacheNovelKey := strings.ToLower(novelName)
	if explicitSlug != "" {
		cacheNovelKey = "id:" + strings.ToLower(explicitSlug)
	}
	key := fmt.Sprintf("freewebnovel:%s:%d", cacheNovelKey, chapterNum)
	if cached, ok := s.cacheGet(key); ok {
		return cached, nil
	}

	slug := explicitSlug
	if slug == "" {
		slug = slugify(novelName)
	}
	if slug == "" {
		return nil, fmt.Errorf("اسم الرواية غير صالح")
	}

	url := buildChapterURL(slug, chapterNum)

	log.Printf("[novel] [HTTP] جلب %s", url)
	htmlStr := s.fetchPageHTTP(ctx, url)

	if htmlStr == "" {
		urlHTML := url + ".html"
		log.Printf("[novel] [HTTP] جرب .html: %s", urlHTML)
		htmlStr = s.fetchPageHTTP(ctx, urlHTML)
	}

	if htmlStr == "" {
		log.Printf("[novel] [Browser] جلب %s", url)
		htmlStr = fetchPageBrowser(ctx, url)
	}

	// freewebnovel.com غير متّسق في تسمية الـ slug عبر الروايات: بعضها
	// بلا لاحقة (shadow-slave)، وبعضها بلاحقة -novel (martial-god-asura-novel).
	// إن كان الـ slug مُخمَّناً تلقائياً (لا explicitSlug من العميل) وفشلت
	// كل المحاولات أعلاه ولا يحمل بالفعل لاحقة -novel، جرّب إضافتها قبل
	// الاستسلام نهائياً.
	if htmlStr == "" && explicitSlug == "" && !strings.HasSuffix(slug, "-novel") {
		altSlug := slug + "-novel"
		altURL := buildChapterURL(altSlug, chapterNum)

		log.Printf("[novel] [HTTP] جرب slug بديل: %s", altURL)
		htmlStr = s.fetchPageHTTP(ctx, altURL)
		if htmlStr == "" {
			htmlStr = s.fetchPageHTTP(ctx, altURL+".html")
		}
		if htmlStr == "" {
			log.Printf("[novel] [Browser] جرب slug بديل: %s", altURL)
			htmlStr = fetchPageBrowser(ctx, altURL)
		}
		if htmlStr != "" {
			slug = altSlug
			url = altURL
		}
	}

	if htmlStr == "" {
		return nil, fmt.Errorf("فشل جلب الصفحة بكل الطرق: %s", url)
	}

	paragraphs := extract(htmlStr, contentSelectors)
	if paragraphs == nil {
		sample := htmlStr
		if len(sample) > 1000 {
			sample = sample[:1000]
		}
		log.Printf("[novel] عينة HTML: %s", strings.ReplaceAll(sample, "\n", " "))
		return nil, fmt.Errorf("لم يُعثر على محتوى (تحقق من بنية الصفحة)")
	}

	title := extractTitle(htmlStr)
	if title == "" {
		title = novelName
	}

	wordCount := 0
	for _, p := range paragraphs {
		wordCount += len(strings.Fields(p))
	}

	result := map[string]any{
		"title":      title,
		"chapter":    chapterNum,
		"paragraphs": paragraphs,
		"site":       siteName,
		"url":        url,
		"word_count": wordCount,
	}
	s.cacheSet(key, result)
	return result, nil
}

// ─── HTTP handlers ───────────────────────────────────────────────

func (s *Service) handleGetChapter(r *http.Request) (map[string]any, error) {
	var body struct {
		Novel   string `json:"novel"`
		Chapter any    `json:"chapter"`
		Site    string `json:"site"`     // اختياري — كان يُرسَل من ss-main/cmds/novel2.js ويُهمَل بصمت سابقاً
		NovelID string `json:"novel_id"` // اختياري — نفس الشيء، يُستخدم الآن كـ slug صريح
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusInternalServerError,
			Body: map[string]any{"error": strutil.Truncate(err.Error(), 200)},
		}
	}

	// هذا الـ plugin يدعم مصدراً واحداً فقط حالياً (Freewebnovel). إن طلب
	// العميل مصدراً آخر صراحةً، الأفضل رفض واضح بدل تجاهل صامت للحقل —
	// كان هذا الحقل يُرسَل من novel2.js ويُهمَل بالكامل من قبل.
	if site := strings.TrimSpace(body.Site); site != "" && !strings.EqualFold(site, siteName) {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{
				"error":           fmt.Sprintf("المصدر %q غير مدعوم حالياً", site),
				"supported_sites": []string{siteName},
			},
		}
	}

	novelName := strings.TrimSpace(body.Novel)
	novelID := strings.TrimSpace(body.NovelID)
	if novelName == "" && novelID == "" {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "novel أو novel_id مطلوب"},
		}
	}

	chapterNum := 0
	switch v := body.Chapter.(type) {
	case float64:
		chapterNum = int(v)
	case string:
		chapterNum, _ = strconv.Atoi(v)
	}
	if chapterNum < 1 {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "chapter موجب"},
		}
	}

	result, err := s.fetchChapter(r.Context(), novelName, chapterNum, novelID)
	if err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusNotFound,
			Body: map[string]any{"error": err.Error()},
		}
	}
	return result, nil
}

func (s *Service) handleListSites(r *http.Request) (map[string]any, error) {
	return map[string]any{"sites": []string{siteName}}, nil
}

func (s *Service) handleClearCache(r *http.Request) (map[string]any, error) {
	n := s.cacheClear()
	return map[string]any{"cleared": n}, nil
}
