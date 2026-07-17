// Package mangabridge يقابل plugins/manga_bridge.py: endpoints الـ job
// (إنشاء/حالة/صور) + الكشط الفعلي. الكشط (كان عبر Node.js/Playwright في
// scripts/mangabridge/manga_scraper.js، المحذوف الآن) صار هنا Go خالصاً
// 100%: chromedp يتحدث مباشرة مع Chrome عبر CDP — راجع internal/browser
// للبنية التحتية المشتركة مع plugins/pinterest (نفس منطق التشغيل بالضبط).
//
// فرق تصميم متعمّد عن بايثون: بدل SQLite (يحتاج إما cgo مع
// mattn/go-sqlite3، أو محرّك Go نقي من modernc.org — نطاق محجوب هنا
// أيضاً بنفس قيد golang.org الموثّق سابقاً)، استخدمنا نفس نمط مخزن
// الجلسات (internal/session): خريطة في الذاكرة محمية بـ mutex — لكن هنا
// كحقلَي Service بدل متغيرات حزمة عامة (راجع §2.2). النتيجة نفس السلوك
// الوظيفي (بيانات job قصيرة العمر، TTL ساعة واحدة) بدون أي تبعية خارجية.
package mangabridge

import (
	"context"
	cryptorand "crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/chromedp/cdproto/network"
	"github.com/chromedp/chromedp"

	"sunkenbot/internal/browser"
	"sunkenbot/internal/httpx"
	"sunkenbot/internal/plugins"
)

const Description = "كشط فصول المانجا — Go خالص (chromedp/CDP) + بنية job كاملة"

const (
	jobTTL       = time.Hour
	imagesSubdir = "data/manga_bridge/images"
)

// ─── حالة الـ job في الذاكرة (تقابل جدول jobs في SQLite) ────────────

type jobStatus string

const (
	statusPending jobStatus = "pending"
	statusRunning jobStatus = "running"
	statusDone    jobStatus = "done"
	statusError   jobStatus = "error"
)

type mangaJob struct {
	ID           string
	Manga        string
	Chapter      string
	Status       jobStatus
	ChapterTitle string
	ChapterURL   string
	ImageCount   int
	Error        string
	CreatedAt    time.Time
	UpdatedAt    time.Time
}

// Service يحمل حالة الـ jobs في الذاكرة كحقلين صريحين (بدل jobsMu/jobs
// كمتغيرات حزمة عامة سابقاً) — هذا بالضبط النمط الممنوع صراحة في القيود
// الصارمة (map[string]*job على مستوى الحزمة)، والسبب الرئيسي لوجود هذا
// الـ Service أصلاً.
// حارس التزامن (sem): يحد عدد نسخ Chromium المشغّلة في آن واحد — كل
// واحدة ثقيلة بالذاكرة، وعدة طلبات متزامنة قد تُسقط العملية كلها بدون
// هذا الحد على حاوية بموارد محدودة.
type Service struct {
	jobsMu sync.Mutex
	jobs   map[string]*mangaJob

	sem chan struct{}
}

// concurrentScrapes: حد أقصى لعدد عمليات الكشط (Chromium) المتزامنة.
// اضبطه حسب الذاكرة المتاحة فعلياً في بيئة الإنتاج.
const concurrentScrapes = 2

func New() *Service {
	return &Service{
		jobs: map[string]*mangaJob{},
		sem:  make(chan struct{}, concurrentScrapes),
	}
}

func (s *Service) Name() string { return "manga_bridge" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/manga-bridge/jobs", Handler: httpx.Handle(s.handleCreateJob)},
		{Method: "GET", Pattern: "/manga-bridge/jobs/{job_id}", Handler: httpx.Handle(s.handleGetJob)},
		{Method: "GET", Pattern: "/manga-bridge/jobs/{job_id}/image/{idx}", Handler: s.handleGetJobImage},
	}
}

func (s *Service) createJob(manga, chapter string) *mangaJob {
	id := randomHex(16)
	now := time.Now()
	j := &mangaJob{ID: id, Manga: manga, Chapter: chapter, Status: statusPending, CreatedAt: now, UpdatedAt: now}
	s.jobsMu.Lock()
	s.jobs[id] = j
	s.jobsMu.Unlock()
	return j
}

func (s *Service) getJob(id string) (*mangaJob, bool) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	j, ok := s.jobs[id]
	return j, ok
}

func (s *Service) updateJob(id string, mutate func(*mangaJob)) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	if j, ok := s.jobs[id]; ok {
		mutate(j)
		j.UpdatedAt = time.Now()
	}
}

// cleanupOldJobs يقابل _cleanup_old_jobs في بايثون — يمسح الـ jobs
// وصورها الأقدم من JOB_TTL_SECONDS، يُستدعى مع كل job جديدة.
func (s *Service) cleanupOldJobs() {
	cutoff := time.Now().Add(-jobTTL)
	s.jobsMu.Lock()
	var expired []string
	for id, j := range s.jobs {
		if j.CreatedAt.Before(cutoff) {
			expired = append(expired, id)
			delete(s.jobs, id)
		}
	}
	s.jobsMu.Unlock()

	for _, id := range expired {
		_ = os.RemoveAll(filepath.Join(imagesSubdir, id))
	}
}

// ─── محرّك الكشط (chromedp/CDP خالص Go — بدل Node.js/Playwright سابقاً) ─
//
// ملاحظة صادقة: يحتاج Chromium مثبَّتاً فعلياً على الجهاز الذي يعمل عليه
// هذا الثنائي (راجع الـ Dockerfile). لم يتسنَّ اختبار هذا بمتصفح حقيقي
// في بيئة تطوير هذا المشروع (تحميل ثنائي Chromium نفسه محجوب شبكياً
// هناك) — لكن حزمة chromedp نفسها بُنيت واختُبرت فعلياً هنا (راجع
// internal/browser). حتى نسخة بايثون الأصلية توثّق أن Cloudflare قد
// يحجب هذا بغض النظر عن الأداة، فهذا الجزء هش حتى في أفضل الأحوال.

const (
	mangaBaseURL          = "https://3asq.online"
	mangaCFChallengeWaitS = 15
	mangaUserAgent        = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
		"(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

func slugify(name string) string {
	return strings.ReplaceAll(strings.ToLower(strings.TrimSpace(name)), " ", "-")
}

// waitForCloudflareTitle يقابل waitForCloudflare في manga_scraper.js
// السابق: يفحص عنوان الصفحة كل ثانية حتى deadline، بحثاً عن زوال
// "Just a moment" من العنوان.
func waitForCloudflareTitle(ctx context.Context, maxWaitS int) bool {
	for i := 0; i < maxWaitS; i++ {
		var title string
		checkCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		err := chromedp.Run(checkCtx, chromedp.Title(&title))
		cancel()
		if err == nil && !strings.Contains(title, "Just a moment") {
			return true
		}
		sleepCtx, sleepCancel := context.WithTimeout(ctx, 2*time.Second)
		_ = chromedp.Run(sleepCtx, chromedp.Sleep(1*time.Second))
		sleepCancel()
	}
	var title string
	checkCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	err := chromedp.Run(checkCtx, chromedp.Title(&title))
	cancel()
	return err == nil && !strings.Contains(title, "Just a moment")
}

const extractImgSrcsJS = `
(() => {
    const imgs = document.querySelectorAll('.page-break img, .reading-content img');
    const out = [];
    for (const img of imgs) {
        const src = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.getAttribute('src');
        if (src && src.trim()) out.push(src.trim());
    }
    return out;
})()
`

const firstSearchResultHrefJS = `
(() => {
    const a = document.querySelector('.page-item-detail .item-thumb a');
    return a ? (a.getAttribute('href') || '') : '';
})()
`

type scrapedChapter struct {
	title string
	url   string
	srcs  []string
}

// scrapeChapterDOM يقابل الجزء الأول (بلا تحميل الصور) من scrapeChapter
// في manga_scraper.js السابق: تنقّل، انتظار Cloudflare، استخراج روابط
// الصور، ومع منطق fallback البحث لو لم توجد صور مباشرة.
func scrapeChapterDOM(ctx context.Context, manga, chapter string) (scrapedChapter, error) {
	slug := slugify(manga)
	chapterURL := fmt.Sprintf("%s/manga/%s/%s/", mangaBaseURL, slug, chapter)

	navCtx, cancel := context.WithTimeout(ctx, 45*time.Second)
	err := chromedp.Run(navCtx, chromedp.Navigate(chapterURL))
	cancel()
	if err != nil {
		return scrapedChapter{}, fmt.Errorf("تعذّر فتح صفحة الفصل: %w", err)
	}
	waitForCloudflareTitle(ctx, mangaCFChallengeWaitS)

	var srcs []string
	if err := chromedp.Run(ctx, chromedp.Evaluate(extractImgSrcsJS, &srcs)); err != nil {
		return scrapedChapter{}, fmt.Errorf("فشل استخراج روابط الصور: %w", err)
	}

	if len(srcs) == 0 {
		// لا صور — جرّب البحث (نفس منطق manga2.js الأصلي)
		searchURL := fmt.Sprintf("%s/?s=%s", mangaBaseURL, url.QueryEscape(manga))
		searchCtx, searchCancel := context.WithTimeout(ctx, 30*time.Second)
		err := chromedp.Run(searchCtx, chromedp.Navigate(searchURL))
		searchCancel()
		if err != nil {
			return scrapedChapter{}, fmt.Errorf("تعذّر فتح صفحة البحث: %w", err)
		}
		waitForCloudflareTitle(ctx, mangaCFChallengeWaitS)

		var href string
		if err := chromedp.Run(ctx, chromedp.Evaluate(firstSearchResultHrefJS, &href)); err != nil || href == "" {
			return scrapedChapter{title: "", url: "", srcs: nil}, nil
		}

		retryURL := strings.TrimRight(href, "/") + "/" + chapter + "/"
		retryCtx, retryCancel := context.WithTimeout(ctx, 45*time.Second)
		err = chromedp.Run(retryCtx, chromedp.Navigate(retryURL))
		retryCancel()
		if err != nil {
			return scrapedChapter{}, fmt.Errorf("تعذّر فتح رابط الفصل البديل: %w", err)
		}
		waitForCloudflareTitle(ctx, mangaCFChallengeWaitS)

		if err := chromedp.Run(ctx, chromedp.Evaluate(extractImgSrcsJS, &srcs)); err != nil {
			return scrapedChapter{}, fmt.Errorf("فشل استخراج روابط الصور (بعد البحث): %w", err)
		}
	}

	var title, finalURL string
	_ = chromedp.Run(ctx, chromedp.Title(&title), chromedp.Location(&finalURL))

	if len(srcs) == 0 {
		return scrapedChapter{title: title, url: finalURL, srcs: nil}, nil
	}
	return scrapedChapter{title: title, url: finalURL, srcs: srcs}, nil
}

// browserCookieHeader يستخرج كل الكوكيز الحالية في جلسة chromedp (تشمل
// أي cf_clearance حصلت عليه الجلسة من اجتياز تحدي Cloudflare) ويبنيها
// كسلسلة رأس Cookie قياسية — يُستخدم لاحقاً في fetchImages ليطلب Go
// نفس الصور بنفس هوية الجلسة التي اجتازت التحدي، بدل أن يبدأ من صفر
// ويصطدم بالتحدي من جديد (نفس أثر مشاركة browser context بين page.goto
// وcontext.request.get في Playwright/Node السابق).
func browserCookieHeader(ctx context.Context, forURL string) (string, error) {
	if err := chromedp.Run(ctx, network.Enable()); err != nil {
		return "", err
	}
	var cookies []*network.Cookie
	if err := chromedp.Run(ctx, chromedp.ActionFunc(func(ctx context.Context) error {
		var err error
		cookies, err = network.GetCookies().WithUrls([]string{forURL}).Do(ctx)
		return err
	})); err != nil {
		return "", err
	}
	parts := make([]string, 0, len(cookies))
	for _, c := range cookies {
		parts = append(parts, c.Name+"="+c.Value)
	}
	return strings.Join(parts, "; "), nil
}

// fetchImages يقابل حلقة تحميل الصور في scrapeChapter (manga_scraper.js
// السابق): يطلب كل رابط بـ Referer=chapterURL ومحاولتين عند الفشل، لكن
// عبر http.Client عادي في Go بدل context.request.get في Playwright —
// مع نفس الكوكيز (راجع browserCookieHeader) ونفس الـ User-Agent حتى
// يبقى الخادم البعيد يرى نفس الهوية التي اجتازت تحدي Cloudflare.
func fetchImages(ctx context.Context, srcs []string, referer, cookieHeader string) [][]byte {
	client := &http.Client{Timeout: 20 * time.Second}
	buffers := make([][]byte, 0, len(srcs))

	for _, src := range srcs {
		var body []byte
		for attempt := 0; attempt < 2; attempt++ {
			reqCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
			req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, src, nil)
			if err == nil {
				req.Header.Set("Referer", referer)
				req.Header.Set("User-Agent", mangaUserAgent)
				if cookieHeader != "" {
					req.Header.Set("Cookie", cookieHeader)
				}
				resp, doErr := client.Do(req)
				if doErr == nil {
					if resp.StatusCode == http.StatusOK {
						b, readErr := io.ReadAll(resp.Body)
						resp.Body.Close()
						if readErr == nil {
							body = b
							cancel()
							break
						}
					} else {
						resp.Body.Close()
					}
				}
			}
			cancel()
			if attempt == 0 {
				time.Sleep(1 * time.Second)
			}
		}
		if body != nil {
			buffers = append(buffers, body)
		}
	}
	return buffers
}

// scrapeChapter يقابل الدالة بنفس الاسم في manga_bridge.py الأصلي
// (وscrapeChapter في manga_scraper.js السابق قبل حذفه): تشغّل جلسة
// Chromium مخصصة كاملة، تكشط DOM صفحة الفصل، ثم تُحمِّل كل الصور
// وتكتبها كملفات 0.jpg, 1.jpg, ... داخل outputDir.
func (s *Service) scrapeChapter(ctx context.Context, manga, chapter, outputDir string) (title, chapterURL string, imageCount int, err error) {
	// حارس التزامن: يحد عدد نسخ Chromium المتشغّلة معاً.
	s.sem <- struct{}{}
	defer func() { <-s.sem }()

	ctxTimeout, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()

	bctx, bcancel, err := browser.New(ctxTimeout, browser.Options{
		UserAgent:   mangaUserAgent,
		ExecPathEnv: "MANGA_CHROMIUM_PATH",
	})
	if err != nil {
		return "", "", 0, err
	}
	defer bcancel()

	result, err := scrapeChapterDOM(bctx, manga, chapter)
	if err != nil {
		return "", "", 0, err
	}
	if len(result.srcs) == 0 {
		return result.title, result.url, 0, nil
	}

	cookieHeader, _ := browserCookieHeader(bctx, result.url)
	buffers := fetchImages(ctxTimeout, result.srcs, result.url, cookieHeader)
	if len(buffers) == 0 {
		return result.title, result.url, 0, nil
	}

	if err := os.MkdirAll(outputDir, 0o755); err != nil {
		return "", "", 0, fmt.Errorf("تعذّر إنشاء مجلد الصور: %w", err)
	}
	for idx, buf := range buffers {
		fpath := filepath.Join(outputDir, fmt.Sprintf("%d.jpg", idx))
		if err := os.WriteFile(fpath, buf, 0o644); err != nil {
			return "", "", 0, fmt.Errorf("تعذّر كتابة الصورة %d: %w", idx, err)
		}
	}

	return result.title, result.url, len(buffers), nil
}

// ─── تشغيل الـ job في الخلفية (goroutine تقابل BackgroundTasks) ─────

func (s *Service) runJob(jobID, manga, chapter string) {
	s.updateJob(jobID, func(j *mangaJob) { j.Status = statusRunning })

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Second)
	defer cancel()

	jobDir := filepath.Join(imagesSubdir, jobID)
	title, u, imageCount, err := s.scrapeChapter(ctx, manga, chapter, jobDir)
	if err != nil || imageCount == 0 {
		reason := "لم يتم العثور على صور — إما اسم/فصل غير صحيح، أو تحدي Cloudflare لم يُحل من IP الخادم."
		if err != nil {
			reason = err.Error()
		}
		s.updateJob(jobID, func(j *mangaJob) {
			j.Status = statusError
			j.Error = truncate(reason, 500)
		})
		return
	}

	s.updateJob(jobID, func(j *mangaJob) {
		j.Status = statusDone
		j.ChapterTitle = title
		j.ChapterURL = u
		j.ImageCount = imageCount
	})
	log.Printf("[manga-bridge] job %s اكتملت — %d صورة", jobID, imageCount)
}

// ─── HTTP handlers ───────────────────────────────────────────────

type createJobResult struct {
	JobID  string `json:"job_id"`
	Status string `json:"status"`
}

func (s *Service) handleCreateJob(r *http.Request) (createJobResult, error) {
	s.cleanupOldJobs()

	var body struct {
		Manga   string `json:"manga"`
		Chapter string `json:"chapter"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		return createJobResult{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"detail": "طلب غير صالح"},
		}
	}

	manga := strings.TrimSpace(body.Manga)
	chapter := strings.TrimSpace(body.Chapter)
	if manga == "" || chapter == "" {
		return createJobResult{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"detail": "الحقول manga و chapter مطلوبة"},
		}
	}

	j := s.createJob(manga, chapter)
	log.Printf("[manga-bridge] job جديدة %s | %s #%s", j.ID, manga, chapter)

	go s.runJob(j.ID, manga, chapter)

	return createJobResult{JobID: j.ID, Status: "pending"}, nil
}

type jobStatusResult struct {
	Status       jobStatus `json:"status"`
	ChapterTitle string    `json:"chapter_title"`
	ChapterURL   string    `json:"chapter_url"`
	ImageCount   int       `json:"image_count"`
	Error        string    `json:"error"`
}

func (s *Service) handleGetJob(r *http.Request) (jobStatusResult, error) {
	jobID := r.PathValue("job_id")
	j, ok := s.getJob(jobID)
	if !ok {
		return jobStatusResult{}, &httpx.HTTPError{
			Code: http.StatusNotFound,
			Body: map[string]any{"detail": "job غير موجودة (ممكن تكون انتهت صلاحيتها)"},
		}
	}
	return jobStatusResult{
		Status:       j.Status,
		ChapterTitle: j.ChapterTitle,
		ChapterURL:   j.ChapterURL,
		ImageCount:   j.ImageCount,
		Error:        j.Error,
	}, nil
}

// handleGetJobImage يخدم ملف الصورة مباشرة (http.ServeFile) — لا يلائم
// شكل httpx.Handle[Res] العام (لا يوجد جسم JSON للرد الناجح)، لذا يبقى
// http.HandlerFunc عادياً يُمرَّر مباشرة في Routes()، تماماً كما يسمح به
// نوع Route.Handler (راجع internal/plugins).
func (s *Service) handleGetJobImage(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	idxStr := r.PathValue("idx")
	idx, err := strconv.Atoi(idxStr)
	if err != nil {
		httpx.JSON(w, http.StatusBadRequest, map[string]any{"detail": "idx غير صالح"})
		return
	}

	path := filepath.Join(imagesSubdir, jobID, fmt.Sprintf("%d.jpg", idx))
	if _, err := os.Stat(path); err != nil {
		httpx.JSON(w, http.StatusNotFound, map[string]any{"detail": "الصورة غير موجودة"})
		return
	}
	http.ServeFile(w, r, path)
}

// ─── أدوات مساعدة ──────────────────────────────────────────────────

func randomHex(n int) string {
	buf := make([]byte, n/2+1)
	if _, err := cryptorand.Read(buf); err != nil {
		// احتياط شديد الندرة فقط لو فشل مصدر العشوائية بالنظام
		return fmt.Sprintf("%x", time.Now().UnixNano())[:n]
	}
	return hex.EncodeToString(buf)[:n]
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
