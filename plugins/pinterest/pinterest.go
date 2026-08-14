// Package pinterest يقابل plugins/pinterest.py: بحث وتحميل صور Pinterest
// عالية الدقة. الكشط الفعلي (كان عبر Playwright في Node.js —
// scripts/pinterest/pinterest_scraper.js، المحذوف الآن) صار هنا Go
// خالصاً 100%: chromedp يتحدث مباشرة مع Chrome عبر بروتوكول Chrome
// DevTools (CDP) بلا أي وسيط Node/npm — راجع internal/browser للبنية
// التحتية المشتركة مع plugins/mangabridge (نفس منطق التشغيل بالضبط).
package pinterest

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/chromedp"

	"sunkenbot/internal/browser"
	"sunkenbot/internal/httpx"
	"sunkenbot/internal/plugins"
	"sunkenbot/internal/strutil"
)

const Description = "بحث وتحميل صور عالية الدقة من Pinterest (chromedp/CDP خالص Go + Ferdev fallback)"

// Service يحمل عميل http.Client (المشترك بمهلة 30 ثانية — راجع جدول قرار
// http.Client) وحالة health-check البسيطة (healthMu/lastScraperRan —
// كانت متغيرات حزمة عامة، انتقلت هنا لنفس سبب انتقال jobsMu في
// mangabridge) كحقول Service صريحة.
type Service struct {
	client *http.Client

	healthMu       sync.Mutex
	lastScraperRan bool

	// حارس التزامن: يحد عدد نسخ Chromium (عبر chromedp/CDP مباشرة الآن،
	// بدل Node/Playwright سابقاً) المشغّلة في آن واحد — راجع نفس المنطق
	// في plugins/mangabridge.
	sem chan struct{}
}

// concurrentScrapes: حد أقصى لعدد عمليات الكشط (Chromium) المتزامنة.
const concurrentScrapes = 2

func New(client *http.Client) *Service {
	return &Service{client: client, sem: make(chan struct{}, concurrentScrapes)}
}

func (s *Service) Name() string { return "pinterest" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/pinterest", Handler: httpx.Handle(s.handleSearch)},
		{Method: "GET", Pattern: "/pinterest/health", Handler: httpx.Handle(s.handleHealth)},
	}
}

// ─── أنواع الطلب/الرد (تطابق PinterestRequest/PinterestResponse) ────

type pinterestRequest struct {
	Query          string `json:"query"`
	Limit          int    `json:"limit"`
	Quality        string `json:"quality"`
	Username       string `json:"username"`
	BoardSlug      string `json:"board_slug"`
	AsBase64       bool   `json:"as_base64"`
	FallbackFerdev *bool  `json:"fallback_ferdev"` // مؤشر لتمييز "لم يُرسَل" عن false صراحة
}

type imageResult struct {
	ID        string `json:"id"`
	URL       string `json:"url"`
	Title     string `json:"title,omitempty"`
	SourceURL string `json:"source_url,omitempty"`
	Thumbnail string `json:"thumbnail,omitempty"`
	Width     *int   `json:"width,omitempty"`
	Height    *int   `json:"height,omitempty"`
}

type pinterestResponse struct {
	Success   bool          `json:"success"`
	Query     string        `json:"query,omitempty"`
	Count     int           `json:"count"`
	Images    []imageResult `json:"images"`
	Provider  string        `json:"provider"`
	Error     string        `json:"error,omitempty"`
	ElapsedMs int64         `json:"elapsed_ms"`
}

// ─── HTTP handlers ───────────────────────────────────────────────

func (s *Service) handleHealth(r *http.Request) (map[string]any, error) {
	s.healthMu.Lock()
	ran := s.lastScraperRan
	s.healthMu.Unlock()

	return map[string]any{
		"ok":                  true,
		"browser_initialised": ran, // true فقط لو نُفِّذ الكشط من قبل بنجاح
		"ferdev_key_present":  os.Getenv("FERDEV_API_KEY") != "",
	}, nil
}

func (s *Service) handleSearch(r *http.Request) (pinterestResponse, error) {
	t0 := time.Now()

	var req pinterestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		return pinterestResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"detail": "طلب غير صالح: " + err.Error()},
		}
	}
	if strings.TrimSpace(req.Query) == "" && req.Username == "" {
		return pinterestResponse{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"detail": "query مطلوب (أو username+board_slug)"},
		}
	}
	if req.Limit <= 0 {
		req.Limit = 5
	}
	if req.Limit > 20 {
		req.Limit = 20
	}
	if req.Quality == "" {
		req.Quality = "original"
	}
	fallbackFerdev := true
	if req.FallbackFerdev != nil {
		fallbackFerdev = *req.FallbackFerdev
	}

	provider := "chromedp"
	var images []imageResult
	var scraperErr string

	pins, err := s.runScraper(r.Context(), req)
	if err != nil {
		provider = "chromedp-error"
		scraperErr = err.Error()
	} else {
		s.healthMu.Lock()
		s.lastScraperRan = true
		s.healthMu.Unlock()
		if len(pins) > req.Limit {
			pins = pins[:req.Limit]
		}
		images = pins
	}

	// ─── fallback إلى Ferdev ─────────────────────────────────────
	isBoardMode := req.Username != "" && req.BoardSlug != ""
	if len(images) == 0 && fallbackFerdev && !isBoardMode {
		apiKey := os.Getenv("FERDEV_API_KEY")
		if ferdevImgs := s.ferdevSearch(r.Context(), req.Query, req.Limit, apiKey); len(ferdevImgs) > 0 {
			images = ferdevImgs
			provider = "ferdev"
			scraperErr = ""
		}
	}

	// ─── ترميز base64 اختياري (نفس التقصير b64[:120]+"..." في بايثون) ─
	if req.AsBase64 && len(images) > 0 {
		s.encodeThumbnailsBase64(r.Context(), images)
	}

	if len(images) == 0 && scraperErr == "" {
		scraperErr = "no images found"
	}

	return pinterestResponse{
		Success:   len(images) > 0,
		Query:     req.Query,
		Count:     len(images),
		Images:    images,
		Provider:  provider,
		Error:     scraperErr,
		ElapsedMs: time.Since(t0).Milliseconds(),
	}, nil
}

// ─── تشغيل الكشط الفعلي عبر chromedp (CDP مباشرة، لا Node) ─────────

const (
	pinterestBaseURL      = "https://www.pinterest.com"
	pinterestViewportW    = 1280
	pinterestViewportH    = 900
	pinterestNavTimeout   = 30 * time.Second
	pinterestNavRetries   = 2
	pinterestChallengeMax = 20 * time.Second
	pinterestScrollWait   = 1200 * time.Millisecond
	pinterestPageGapWait  = 1000 * time.Millisecond
)

var pinterestChallengeMarkers = []string{
	"just a moment",
	"attention required",
	"checking your browser",
	"ddos protection",
	"verify you are human",
}

// rawPin يقابل شكل عنصر واحد كما يُستخرج من DOM عبر extractPinsJS —
// نفس حقول EXTRACT_JS بالضبط في pinterest_scraper.js السابق.
type rawPin struct {
	ID          string `json:"id"`
	URL         string `json:"url"`
	Title       string `json:"title"`
	ImageSrc    string `json:"image_src"`
	ImageSrcset string `json:"image_srcset"`
}

// extractPinsJS: نفس EXTRACT_JS بالضبط من pinterest_scraper.js السابق،
// مُنفَّذة داخل الصفحة عبر chromedp.Evaluate (المكافئ لِـ page.evaluate).
const extractPinsJS = `
(() => {
    const cards = document.querySelectorAll('.ADXRXN');
    const out = [];
    const seen = new Set();
    for (const c of cards) {
        const a = c.querySelector('a[href*="/pin/"]');
        if (!a) continue;
        const m = (a.getAttribute('href') || '').match(/\/pin\/(\d+)/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        const img = c.querySelector('img');
        const h = c.querySelector('h1, h2, h3, [class*="title"]');
        out.push({
            id,
            url: a.href,
            title: h ? h.textContent.trim() : '',
            image_src: img ? (img.getAttribute('src') || '') : '',
            image_srcset: img ? (img.getAttribute('srcset') || '') : ''
        });
    }
    return out;
})()
`

func (s *Service) runScraper(ctx context.Context, req pinterestRequest) ([]imageResult, error) {
	// حارس التزامن: يحد عدد نسخ Chromium المتشغّلة معاً.
	s.sem <- struct{}{}
	defer func() { <-s.sem }()

	pages := max((req.Limit+24)/25, 1)

	var listingURL string
	if req.Username != "" && req.BoardSlug != "" {
		listingURL = fmt.Sprintf("%s/%s/%s/", pinterestBaseURL, req.Username, req.BoardSlug)
	} else {
		listingURL = fmt.Sprintf("%s/search/pins/?q=%s&rs=typed", pinterestBaseURL, url.QueryEscape(req.Query))
	}

	ctxTimeout, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()

	bctx, bcancel, err := browser.New(ctxTimeout, browser.Options{
		WindowWidth:  pinterestViewportW,
		WindowHeight: pinterestViewportH,
		ExecPathEnv:  "PINTEREST_CHROMIUM_PATH",
	})
	if err != nil {
		return nil, err
	}
	defer bcancel()

	rawPins, err := scrapePinterestListing(bctx, listingURL, pages, 6)
	if err != nil {
		return nil, err
	}

	images := make([]imageResult, 0, len(rawPins))
	for _, p := range rawPins {
		if img := pinFromRaw(p, req.Quality); img != nil {
			images = append(images, *img)
		}
	}
	return images, nil
}

// scrapePinterestListing يقابل scrapeListing في pinterest_scraper.js
// السابق: يمر على صفحات ?page=1..pages، يستخرج pins جديدة من كل صفحة،
// ويتوقف مبكراً لو صفحة كاملة لم تُضِف أي pin جديدة (نفس addedAny في
// النسخة السابقة).
func scrapePinterestListing(ctx context.Context, listingURL string, pages, maxScrolls int) ([]rawPin, error) {
	var allPins []rawPin
	seen := map[string]bool{}

	for i := 1; i <= pages; i++ {
		sep := "?"
		if strings.Contains(listingURL, "?") {
			sep = "&"
		}
		pageURL := fmt.Sprintf("%s%spage=%d", listingURL, sep, i)

		pins, err := scrapePinterestPage(ctx, pageURL, maxScrolls)
		if err != nil {
			// فشل صفحة واحدة لا يجب أن يُسقط ما جُمع من صفحات سابقة —
			// لكن لو كانت هذه الصفحة الأولى (لا نتائج بعد إطلاقاً)، نُعيد
			// الخطأ فعلياً حتى لا يظهر "نجاح" بصفر صور بلا تفسير.
			if i == 1 && len(allPins) == 0 {
				return nil, err
			}
			break
		}

		addedAny := false
		for _, p := range pins {
			if p.ID == "" || seen[p.ID] {
				continue
			}
			seen[p.ID] = true
			allPins = append(allPins, p)
			addedAny = true
		}
		if !addedAny {
			break
		}
		if err := chromedp.Run(ctx, chromedp.Sleep(pinterestPageGapWait)); err != nil {
			break
		}
	}
	return allPins, nil
}

// scrapePinterestPage يقابل scrapeFullURL في pinterest_scraper.js
// السابق: تنقّل مع إعادة محاولة، انتظار زوال أي تحدي Cloudflare، انتظار
// ظهور أول pin، ثم حلقة سكرول+استخراج بحد maxScrolls.
func scrapePinterestPage(ctx context.Context, pageURL string, maxScrolls int) ([]rawPin, error) {
	if err := navigateWithRetry(ctx, pageURL, pinterestNavTimeout, pinterestNavRetries); err != nil {
		return nil, err
	}
	waitForChallengeToClear(ctx, pinterestChallengeMax, pinterestChallengeMarkers)

	// انتظار ظهور أول pin — فشل هذا الانتظار لا يوقف الكشط (قد تظهر
	// النتائج لاحقاً أثناء السكرول)، تماماً كما في try/catch الأصلي.
	waitCtx, waitCancel := context.WithTimeout(ctx, 15*time.Second)
	_ = chromedp.Run(waitCtx, chromedp.WaitVisible(`.ADXRXN, a[href*="/pin/"]`, chromedp.ByQuery))
	waitCancel()

	seen := map[string]bool{}
	var pins []rawPin

	for range maxScrolls {
		var raw []rawPin
		if err := chromedp.Run(ctx, chromedp.Evaluate(extractPinsJS, &raw)); err != nil {
			return pins, err
		}
		for _, d := range raw {
			if d.ID == "" || seen[d.ID] {
				continue
			}
			seen[d.ID] = true
			pins = append(pins, d)
		}
		if err := chromedp.Run(ctx,
			chromedp.Evaluate(`window.scrollBy(0, window.innerHeight * 0.9)`, nil),
			chromedp.Sleep(pinterestScrollWait),
		); err != nil {
			return pins, err
		}
	}
	return pins, nil
}

// navigateWithRetry يقابل gotoWithRetry في النسخة السابقة: يحاول
// التنقّل حتى retries+1 مرة، مع فاصل عشوائي بسيط بين المحاولات عند
// الفشل.
func navigateWithRetry(ctx context.Context, u string, timeout time.Duration, retries int) error {
	var lastErr error
	for attempt := 0; attempt <= retries; attempt++ {
		navCtx, cancel := context.WithTimeout(ctx, timeout)
		err := chromedp.Run(navCtx, chromedp.Navigate(u))
		cancel()
		if err == nil {
			return nil
		}
		lastErr = err
		sleepCtx, sleepCancel := context.WithTimeout(ctx, 2500*time.Millisecond)
		_ = chromedp.Run(sleepCtx, chromedp.Sleep(1500*time.Millisecond))
		sleepCancel()
	}
	return fmt.Errorf("تعذّر التنقّل إلى %s بعد %d محاولة: %w", u, retries+1, lastErr)
}

// waitForChallengeToClear يقابل waitForChallengeToClear في النسخة
// السابقة: يفحص عنوان الصفحة+نصها كل ثانيتين بحثاً عن أي من
// challengeMarkers، حتى deadline. لا تُرجع خطأً أبداً (فشل الفحص أو
// انتهاء المهلة كلاهما يُكملان الكشط بأفضل ما هو متاح، تماماً كما في
// try/catch الأصلي الذي يُعيد true عند أي استثناء أثناء الفحص).
func waitForChallengeToClear(ctx context.Context, maxWait time.Duration, markers []string) {
	deadline := time.Now().Add(maxWait)
	for time.Now().Before(deadline) {
		var title, body string
		checkCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
		errTitle := chromedp.Run(checkCtx, chromedp.Title(&title))
		if errTitle == nil {
			_ = chromedp.Run(checkCtx, chromedp.Evaluate(`document.body ? document.body.innerText : ''`, &body))
		}
		cancel()
		if errTitle != nil {
			return // الصفحة تغيّرت أثناء الفحص — على الأغلب انتقلنا فعلاً
		}
		combined := strings.ToLower(title + " " + body)
		found := false
		for _, m := range markers {
			if strings.Contains(combined, m) {
				found = true
				break
			}
		}
		if !found {
			return
		}
		sleepCtx, sleepCancel := context.WithTimeout(ctx, 3*time.Second)
		_ = chromedp.Run(sleepCtx, chromedp.Sleep(2*time.Second))
		sleepCancel()
	}
}

// ─── تحويل rawPin -> imageResult (منطق pinToImage/originalFromSrcset/convertQuality) ─

var pinimgThumbRe = regexp.MustCompile(`https://i\.pinimg\.com/(?:\d+x|originals|736x|474x|236x)/`)
var pinimgOriginalInSrcsetRe = regexp.MustCompile(`(https://i\.pinimg\.com/originals/[^ \s]+\.(?:jpg|jpeg|png|webp))`)

// originalFromSrcset يقابل originalFromSrcset في النسخة السابقة: يفضّل
// رابط originals/ الصريح لو وُجد داخل srcset، وإلا يختار أعلى دقة
// (بالمعامل Nx) من عناصر srcset.
func originalFromSrcset(srcset string) string {
	if srcset == "" {
		return ""
	}
	if m := pinimgOriginalInSrcsetRe.FindStringSubmatch(srcset); m != nil {
		return m[1]
	}
	type entry struct {
		scale float64
		url   string
	}
	var entries []entry
	for chunk := range strings.SplitSeq(srcset, ",") {
		parts := strings.Fields(strings.TrimSpace(chunk))
		if len(parts) == 0 || !strings.HasPrefix(parts[0], "http") {
			continue
		}
		scale := 1.0
		if len(parts) > 1 {
			s := strings.TrimSuffix(parts[1], "x")
			if f, err := strconv.ParseFloat(s, 64); err == nil {
				scale = f
			}
		}
		entries = append(entries, entry{scale, parts[0]})
	}
	if len(entries) == 0 {
		return ""
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].scale > entries[j].scale })
	return entries[0].url
}

// convertQuality يقابل convertQuality في النسخة السابقة.
func convertQuality(u, quality string) string {
	if u == "" || quality == "original" {
		return u
	}
	re := regexp.MustCompile(`https://i\.pinimg\.com/(?:originals|736x|474x|236x)/`)
	return re.ReplaceAllString(u, "https://i.pinimg.com/"+quality+"/")
}

// pinFromRaw يقابل pinToImage في النسخة السابقة.
func pinFromRaw(p rawPin, quality string) *imageResult {
	imgURL := originalFromSrcset(p.ImageSrcset)
	if imgURL == "" {
		imgURL = p.ImageSrc
	}
	if imgURL == "" {
		return nil
	}
	if originalFromSrcset(p.ImageSrcset) == "" && p.ImageSrc != "" {
		imgURL = pinimgThumbRe.ReplaceAllString(p.ImageSrc, "https://i.pinimg.com/originals/")
	}

	return &imageResult{
		ID:        p.ID,
		URL:       convertQuality(imgURL, quality),
		Title:     p.Title,
		SourceURL: p.URL,
		Thumbnail: p.ImageSrc,
	}
}

// ─── Ferdev fallback (نفس _ferdev_search في بايثون) ─────────────────

func (s *Service) ferdevSearch(ctx context.Context, query string, limit int, apiKey string) []imageResult {
	if apiKey == "" {
		return nil
	}

	u := fmt.Sprintf("https://api.ferdev.my.id/search/pinterest?query=%s&apikey=%s&limit=%d",
		url.QueryEscape(query), url.QueryEscape(apiKey), limit)

	ctxTimeout, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctxTimeout, http.MethodGet, u, nil)
	if err != nil {
		return nil
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)

	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		return nil
	}

	var rawResults []any
	for _, key := range []string{"result", "data", "results"} {
		if arr, ok := data[key].([]any); ok {
			rawResults = arr
			break
		}
	}

	var out []imageResult
	for i, item := range rawResults {
		if i >= limit {
			break
		}
		obj, ok := item.(map[string]any)
		if !ok {
			continue
		}
		imgURL := firstNonEmpty(
			strutil.StringOr(obj["image"], ""),
			strutil.StringOr(obj["url"], ""),
			strutil.StringOr(obj["imageUrl"], ""),
		)
		if imgURL == "" {
			if imgs, ok := obj["images"].([]any); ok && len(imgs) > 0 {
				imgURL = strutil.StringOr(imgs[0], "")
			}
		}
		if imgURL == "" {
			continue
		}

		pinID := strutil.StringOr(obj["id"], "")
		if pinID == "" {
			if pin, ok := obj["pin"].(map[string]any); ok {
				pinID = strutil.StringOr(pin["id"], "")
			}
		}
		if pinID == "" {
			pinID = fmt.Sprintf("ferdev_%d", i)
		}

		out = append(out, imageResult{
			ID:        pinID,
			URL:       imgURL,
			Title:     firstNonEmpty(strutil.StringOr(obj["title"], ""), strutil.StringOr(obj["description"], "")),
			SourceURL: firstNonEmpty(strutil.StringOr(obj["source"], ""), strutil.StringOr(obj["link"], "")),
		})
	}
	return out
}

// ─── ترميز base64 مقصوص (نفس منطق بايثون بالضبط، حتى لو كان تقصيره غريباً) ─

func (s *Service) encodeThumbnailsBase64(ctx context.Context, images []imageResult) {
	var wg sync.WaitGroup
	for i := range images {
		wg.Add(1)
		go func(img *imageResult) {
			defer wg.Done()
			ctxTimeout, cancel := context.WithTimeout(ctx, 20*time.Second)
			defer cancel()

			req, err := http.NewRequestWithContext(ctxTimeout, http.MethodGet, img.URL, nil)
			if err != nil {
				return
			}
			resp, err := s.client.Do(req)
			if err != nil {
				return
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusOK {
				return
			}
			content, err := io.ReadAll(resp.Body)
			if err != nil {
				return
			}

			mime := "image/jpeg"
			low := strings.ToLower(img.URL)
			if strings.HasSuffix(low, ".png") {
				mime = "image/png"
			} else if strings.HasSuffix(low, ".webp") {
				mime = "image/webp"
			}

			b64 := base64.StdEncoding.EncodeToString(content)
			if len(b64) > 120 {
				b64 = b64[:120]
			}
			img.Thumbnail = fmt.Sprintf("data:%s;base64,%s...", mime, b64)
		}(&images[i])
	}
	wg.Wait()
}

// ─── أدوات مساعدة ──────────────────────────────────────────────────


func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}
