// Package fb يعيد تنزيل فيديو من فيسبوك (رابط عادي أو رابط مشاركة
// share/v/...) كـ plugin يتبع نفس نمط الوظائف الخلفية (job) المستخدم في
// plugins/sub: POST /fb/create يبدأ التنزيل في الخلفية (goroutine)،
// GET /fb/status/{job_id} يتابع حالته، وGET /fb/download/{job_id} يبث
// الملف الناتج ثم يحذفه.
//
// ملاحظة: كان plugin بهذا الاسم موجوداً سابقاً في هذا الترحيل وأُزيل
// (راجع الملاحظة أعلى README) — على الأرجح لأنه كان مبنياً كطلب واحد
// متزامن (blocking) غير مناسب لفيديوهات قد يستغرق تنزيلها دقائق. هذه
// النسخة تُعاد كتابتها من الصفر بنمط الوظائف الخلفية الحالي.
package fb

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/netguard"
	"sunkenbot/internal/plugins"
)

const Description = "تنزيل فيديو من فيسبوك (رابط عادي أو رابط مشاركة share/v/...) بصيغة mp4"

// jobTTL: أقصى عمر لأي وظيفة (وملفها) لم تُحمَّل عبر /fb/download — نفس
// نمط التنظيف الدوري في plugins/sub.
const jobTTL = 30 * time.Minute

// maxDownloadBytes: أقصى حجم لفيديو يُقبل تنزيله، لتفادي استنزاف القرص —
// نفس الحد المستخدم في plugins/sub لنفس السبب.
const maxDownloadBytes = 300 * 1024 * 1024 // 300MB

// maxRedirects: أقصى عدد قفزات إعادة توجيه نتابعها (روابط المشاركة
// share/v/... تمر عادة بقفزة أو قفزتين فقط، لكن نُبقي هامشاً).
const maxRedirects = 20

// Service يحمل عميل http.Client (تُقرَّر مهلته صراحة في main.go
// services()، راجع تعليق main.go) وحالة jobs في الذاكرة — نفس بنية
// plugins/sub.
type Service struct {
	client *http.Client

	jobsMu sync.Mutex
	jobs   map[string]*job
}

func New(client *http.Client) *Service {
	return &Service{client: client, jobs: map[string]*job{}}
}

func (s *Service) Name() string { return "fb" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/fb/create", Handler: httpx.Handle(s.handleCreate)},
		{Method: "GET", Pattern: "/fb/status/{job_id}", Handler: httpx.Handle(s.handleStatus)},
		{Method: "GET", Pattern: "/fb/download/{job_id}", Handler: s.handleDownload},
	}
}

// ─── أنواع الطلب ────────────────────────────────────────────────────

// BrowserCookie كوكيز مفردة (name/value) من متصفح المستخدم. اختيارية —
// مطلوبة فقط لفيديوهات خاصة/مقيّدة يملك المستخدم صلاحية وصول حقيقية لها
// عبر حسابه؛ الفيديوهات العامة تُنزَّل عادة بدونها (راجع mbasic في
// toBasicURL أدناه).
type BrowserCookie struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// DownloadRequest تقابل جسم POST /fb/create.
type DownloadRequest struct {
	URL     string          `json:"url"`
	Cookies []BrowserCookie `json:"cookies,omitempty"`
}

// ─── حالة العمليات في الذاكرة (نفس نمط plugins/sub) ──────────────────

type jobStatus string

const (
	jobPending jobStatus = "pending"
	jobDone    jobStatus = "done"
	jobError   jobStatus = "error"
)

type job struct {
	Status     jobStatus
	ResultPath string
	Reason     string
	CreatedAt  time.Time
}

func (s *Service) setJob(id string, j *job) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	if j.CreatedAt.IsZero() {
		if existing, ok := s.jobs[id]; ok {
			j.CreatedAt = existing.CreatedAt // حافظ على وقت الإنشاء الأصلي عند تحديث حالة عمل موجود
		} else {
			j.CreatedAt = time.Now()
		}
	}
	s.jobs[id] = j
}

func (s *Service) getJob(id string) (*job, bool) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	j, ok := s.jobs[id]
	return j, ok
}

func (s *Service) deleteJob(id string) {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()
	delete(s.jobs, id)
}

// cleanupOldJobs يحذف أي وظيفة (وملفها) أقدم من jobTTL ولم تُحمَّل بعد —
// نفس نمط plugins/sub بالضبط. يُستدعى بشكل كسول عند كل طلب إنشاء جديد.
func (s *Service) cleanupOldJobs() {
	s.jobsMu.Lock()
	defer s.jobsMu.Unlock()

	cutoff := time.Now().Add(-jobTTL)
	for id, j := range s.jobs {
		if j.CreatedAt.IsZero() || j.CreatedAt.After(cutoff) {
			continue
		}
		if j.ResultPath != "" {
			_ = os.Remove(j.ResultPath)
		}
		delete(s.jobs, id)
	}
}

// ─── تقييد النطاق: فيسبوك فقط لروابط الإدخال ──────────────────────────

// isFacebookHost يتحقق أن المضيف هو facebook.com أو نطاق فرعي منه، أو
// fb.watch (روابط المشاركة القصيرة). هذا تقييد أضيق عمداً من فحص
// netguard العام (الذي يسمح بأي IP عام) لأن هذا الـ plugin تحديداً
// لا معنى له إلا لفيسبوك، ويمنع استخدامه كأداة جلب عامة (open proxy)
// لأي رابط يمرره المستخدم. نفس نمط التقييد المذكور في تعليق
// internal/netguard/netguard.go عن نسخة هذا الـ plugin السابقة.
func isFacebookHost(host string) bool {
	host = strings.ToLower(host)
	return host == "facebook.com" || strings.HasSuffix(host, ".facebook.com") || host == "fb.watch"
}

// buildCookieHeader يبني قيمة رأس Cookie كاملة من قائمة الكوكيز، لإعادة
// استخدامها يدوياً بعد كل قفزة إعادة توجيه (راجع pageClientFor).
func buildCookieHeader(cookies []BrowserCookie) string {
	parts := make([]string, 0, len(cookies))
	for _, c := range cookies {
		parts = append(parts, c.Name+"="+c.Value)
	}
	return strings.Join(parts, "; ")
}

var facebookHostRe = regexp.MustCompile(`https?://(?:[a-zA-Z0-9-]+\.)*facebook\.com`)

// toBasicURL نسخة mbasic (أبسط نسخة نصية من فيسبوك، بدون جافاسكريبت
// تقريباً، مصممة أصلاً للهواتف القديمة) — غالباً تعرض محتوى الفيديوهات
// العامة بدون جدار تسجيل دخول، على عكس نسخة سطح المكتب الحديثة.
func toBasicURL(u string) string {
	return facebookHostRe.ReplaceAllString(u, "https://mbasic.facebook.com")
}

// toMobileURL نسخة الموبايل العادية — احتياطية بعد mbasic.
func toMobileURL(u string) string {
	return facebookHostRe.ReplaceAllString(u, "https://m.facebook.com")
}

// cleanURL تنظف الرابط المستخرج من أحرف الهروب الشائعة في نصوص فيسبوك
// (escaped JSON).
func cleanURL(raw string) string {
	raw = strings.ReplaceAll(raw, `\/`, "/")
	raw = strings.ReplaceAll(raw, `\u0026`, "&")
	raw = strings.ReplaceAll(raw, `\u003d`, "=")
	raw = strings.ReplaceAll(raw, `\u002F`, "/")
	raw = strings.ReplaceAll(raw, "&amp;", "&")
	return strings.TrimSpace(raw)
}

// extractBestVideoURL يبحث عن أفضل رابط تنزيل مباشر (فيديو+صوت) داخل نص
// الصفحة. الأنماط أسماء حقول JSON استخدمتها فيسبوك تاريخياً لتضمين رابط
// الفيديو داخل الصفحة. **قد تتغيّر هذه الأسماء مستقبلاً** — فيسبوك تعدّل
// بنية صفحاتها بشكل متكرر؛ لو توقف الاستخراج عن العمل فجأة، هذا أول مكان
// يُراجَع (قارن ببنية صفحة حقيقية محفوظة، لا تخمّن حقولاً جديدة عشوائياً).
func extractBestVideoURL(html string) string {
	patterns := []string{
		`"browser_native_hd_url":"([^"]+)"`,
		`"browser_native_sd_url":"([^"]+)"`,
		`"playable_url_quality_hd":"([^"]+)"`,
		`"playable_url":"([^"]+)"`,
		`"hd_src_no_ratelimit":"([^"]+)"`,
		`"sd_src_no_ratelimit":"([^"]+)"`,
		`"hd_src":"([^"]+)"`,
		`"sd_src":"([^"]+)"`,
		`"src":"([^"]+\.mp4[^"]*)"`,
		`"video_url":"([^"]+)"`,
		`"unmuted_video_url":"([^"]+)"`,
	}
	for _, pat := range patterns {
		re := regexp.MustCompile(pat)
		m := re.FindStringSubmatch(html)
		if len(m) > 1 {
			link := m[1]
			if strings.HasPrefix(link, "//") {
				link = "https:" + link
			}
			if strings.HasPrefix(link, "http") {
				return link
			}
		}
	}
	return ""
}

func setPageHeaders(req *http.Request, referer string) {
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8")
	req.Header.Set("Accept-Language", "ar,en;q=0.9")
	req.Header.Set("Referer", referer)
	req.Header.Set("Origin", "https://www.facebook.com")
}

// pageClientFor يبني http.Client مخصصاً لهذه الوظيفة تحديداً (يشارك نفس
// Transport لإعادة استخدام الاتصالات، لكن بمنطق CheckRedirect خاص):
//  1. يرفض أي قفزة إعادة توجيه تخرج عن نطاق فيسبوك — دفاع إضافي في حال
//     استُغِلّ open-redirect داخل فيسبوك نفسها للتوجيه لخادم خارجي.
//  2. يفرض إعادة إضافة رأس Cookie يدوياً بعد كل قفزة، لأن net/http يحذفه
//     تلقائياً كإجراء أمني افتراضي كلما تغيّر النطاق الفرعي (مثلاً
//     www.facebook.com → m.facebook.com) — سلوك صحيح بشكل عام، لكنه يكسر
//     هذا السيناريو تحديداً لأن كل هذه النطاقات هي فيسبوك نفسها.
func (s *Service) pageClientFor(cookieHeader string) *http.Client {
	return &http.Client{
		Timeout:   s.client.Timeout,
		Transport: s.client.Transport,
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			if len(via) >= maxRedirects {
				return fmt.Errorf("تجاوز عدد مرات إعادة التوجيه (%d)", maxRedirects)
			}
			if !isFacebookHost(req.URL.Hostname()) {
				return fmt.Errorf("رُفضت إعادة التوجيه إلى نطاق خارج فيسبوك: %s", req.URL.Hostname())
			}
			if cookieHeader != "" {
				req.Header.Set("Cookie", cookieHeader)
			}
			return nil
		},
	}
}

// resolveFinalURL يتابع أي إعادة توجيه (خاصة لروابط المشاركة share/v/...)
// ويعيد الرابط النهائي الفعلي للفيديو.
func resolveFinalURL(client *http.Client, rawURL, cookieHeader string) (string, error) {
	req, err := http.NewRequest("GET", rawURL, nil)
	if err != nil {
		return "", err
	}
	setPageHeaders(req, rawURL)
	if cookieHeader != "" {
		req.Header.Set("Cookie", cookieHeader)
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	return resp.Request.URL.String(), nil
}

func fetchPage(client *http.Client, target, referer, cookieHeader string) (string, error) {
	req, err := http.NewRequest("GET", target, nil)
	if err != nil {
		return "", err
	}
	setPageHeaders(req, referer)
	if cookieHeader != "" {
		req.Header.Set("Cookie", cookieHeader)
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("استجابة غير متوقعة: %s", resp.Status)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	return string(body), nil
}

// ─── المعالجة الفعلية (goroutine خلفية، نفس نمط plugins/sub) ─────────

func (s *Service) processDownload(jobID, rawURL string, cookies []BrowserCookie) {
	fail := func(reason string) { s.setJob(jobID, &job{Status: jobError, Reason: reason}) }

	cookieHeader := buildCookieHeader(cookies)
	client := s.pageClientFor(cookieHeader)

	finalURL, err := resolveFinalURL(client, rawURL, cookieHeader)
	if err != nil {
		fail("فشل متابعة إعادة التوجيه: " + err.Error())
		return
	}

	// نجرب عدة نسخ من نفس الصفحة بالترتيب: الرابط النهائي كما هو، ثم
	// mbasic (الأرجح نجاحاً بدون كوكيز)، ثم m. كاحتياط أخير.
	candidates := []string{finalURL}
	if basic := toBasicURL(finalURL); basic != finalURL {
		candidates = append(candidates, basic)
	}
	if mobile := toMobileURL(finalURL); mobile != finalURL {
		candidates = append(candidates, mobile)
	}

	var downloadURL string
	for _, u := range candidates {
		html, ferr := fetchPage(client, u, finalURL, cookieHeader)
		if ferr != nil {
			continue
		}
		if found := extractBestVideoURL(html); found != "" {
			downloadURL = cleanURL(found)
			break
		}
	}
	if downloadURL == "" {
		fail("تعذّر استخراج رابط الفيديو. قد يتطلب الفيديو تسجيل دخول (مرّر cookies)، أو الفيديو غير متاح، أو تغيّرت بنية صفحة فيسبوك.")
		return
	}

	// تنزيل ملف الفيديو الفعلي عبر netguard (حماية SSRF + تثبيت DNS ضد
	// rebinding) — نفس الآلية المستخدمة في plugins/sub لتنزيل video_url.
	ctx := context.Background()
	if s.client.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.client.Timeout)
		defer cancel()
	}
	resp, err := netguard.SafeFetchStream(ctx, s.client, downloadURL)
	if err != nil {
		fail("رابط التنزيل مرفوض أو تعذّر جلبه: " + err.Error())
		return
	}
	defer resp.Body.Close()

	outPath := fmt.Sprintf("fb_%s.mp4", uuid.New().String())
	out, err := os.Create(outPath)
	if err != nil {
		fail("فشل إنشاء الملف محلياً")
		return
	}
	limited := io.LimitReader(resp.Body, maxDownloadBytes+1)
	written, copyErr := io.Copy(out, limited)
	_ = out.Close()
	if copyErr != nil {
		_ = os.Remove(outPath)
		fail("فشل قراءة محتوى الفيديو")
		return
	}
	if written > maxDownloadBytes {
		_ = os.Remove(outPath)
		fail(fmt.Sprintf("الفيديو يتجاوز الحد الأقصى المسموح (%d MB)", maxDownloadBytes/(1024*1024)))
		return
	}
	if written == 0 {
		_ = os.Remove(outPath)
		fail("الملف الناتج فارغ")
		return
	}

	s.setJob(jobID, &job{Status: jobDone, ResultPath: outPath})
}

// ─── HTTP handlers ───────────────────────────────────────────────────

func (s *Service) handleCreate(r *http.Request) (map[string]any, error) {
	s.cleanupOldJobs()

	var req DownloadRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "طلب غير صالح: " + err.Error()},
		}
	}
	if strings.TrimSpace(req.URL) == "" {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "url مطلوب"},
		}
	}
	parsed, err := neturl.Parse(req.URL)
	if err != nil || !isFacebookHost(parsed.Hostname()) {
		return nil, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]any{"error": "الرابط يجب أن يكون من نطاق facebook.com أو fb.watch"},
		}
	}

	jobID := "fb_" + uuid.New().String()[:12]
	s.setJob(jobID, &job{Status: jobPending})

	go s.processDownload(jobID, req.URL, req.Cookies)

	return map[string]any{"job_id": jobID, "status": "pending"}, nil
}

func (s *Service) handleStatus(r *http.Request) (map[string]any, error) {
	jobID := r.PathValue("job_id")
	j, ok := s.getJob(jobID)
	if !ok {
		return nil, &httpx.HTTPError{
			Code: http.StatusNotFound,
			Body: map[string]any{"detail": "العملية غير موجودة"},
		}
	}

	switch j.Status {
	case jobDone:
		return map[string]any{
			"status":       "done",
			"download_url": "/fb/download/" + jobID,
		}, nil
	case jobError:
		reason := j.Reason
		if reason == "" {
			reason = "خطأ غير معروف"
		}
		return map[string]any{"status": "error", "reason": reason}, nil
	default:
		return map[string]any{"status": "pending"}, nil
	}
}

// handleDownload يبث ملف الفيديو مباشرة ثم يحذف الملف المؤقت — لا يلائم
// شكل httpx.Handle[Res] العام (لا يوجد جسم JSON للرد الناجح)، فيبقى
// http.HandlerFunc عادياً في Routes()، تماماً مثل plugins/sub.
func (s *Service) handleDownload(w http.ResponseWriter, r *http.Request) {
	jobID := r.PathValue("job_id")
	j, ok := s.getJob(jobID)
	if !ok || j.Status != jobDone {
		httpx.JSON(w, http.StatusNotFound, map[string]any{"detail": "الملف غير جاهز أو غير موجود"})
		return
	}

	f, err := os.Open(j.ResultPath)
	if err != nil {
		httpx.JSON(w, http.StatusNotFound, map[string]any{"detail": "الملف غير جاهز أو غير موجود"})
		return
	}
	defer f.Close()

	w.Header().Set("Content-Type", "video/mp4")
	w.Header().Set("Content-Disposition", `attachment; filename="fb_video.mp4"`)
	_, _ = io.Copy(w, f)

	_ = os.Remove(j.ResultPath)
	s.deleteJob(jobID)
}
