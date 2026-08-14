// Package browser يوفّر بنية تحتية مشتركة لتشغيل Chrome بلا واجهة
// (headless) عبر chromedp — مكتبة Go خالصة تتحدَّث مباشرة مع Chrome
// عبر بروتوكول Chrome DevTools (CDP)، بلا أي وسيط. هذا يستبدل استدعاء
// Node.js/Playwright كعملية فرعية عبر stdin/stdout (الذي كان يُستخدَم
// سابقاً في plugins/pinterest عبر scripts/pinterest/pinterest_scraper.js
// وplugins/mangabridge عبر scripts/mangabridge/manga_scraper.js).
//
// كلا الـ plugin يحتاجان نفس منطق التشغيل بالضبط (headless، no-sandbox،
// حقن سكربت stealth، user agent عشوائي)، لذا استُخرج هنا بدل تكراره —
// نفس فلسفة internal/netguard وinternal/session: بنية تحتية مشتركة بين
// عدة plugins، وليست هي نفسها plugins.Service.
//
// ⭐ إضافة (2026-07): Scrape() — استخراج DOM من صفحة بعد التنقل عبر
// chromedp، باستخدام Colly لتحليل HTML. أي plugin يحتاج استخراج بيانات
// من صفحة ويب (مثل pinterest، manga_bridge، أو أي plugin مستقبلي) يمكنه
// استخدامها مباشرة بدلاً من تكرار منطق chromedp + HTML parsing.
package browser

import (
	"context"
	"fmt"
	"maps"
	"math/rand"
	"os"
	"strings"
	"sync"

	"net/http"
	"net/http/httptest"

	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"
	"github.com/gocolly/colly/v2"
)

// stealthInitScript: نفس STEALTH_INIT_SCRIPT بالضبط من
// scripts/pinterest/pinterest_scraper.js السابق (browser.py الأصلي في
// نسخة بايثون كان مصدرها الأول). يُحقن في كل صفحة جديدة *قبل* أي
// جافاسكربت آخر عبر page.AddScriptToEvaluateOnNewDocument — المكافئ
// الدقيق في CDP لِـ context.addInitScript في Playwright — لإخفاء آثار
// الأتمتة الشائعة (navigator.webdriver، عدد plugins، إلخ) التي تفحصها
// بعض الخوادم (Cloudflare وغيرها) لتمييز المتصفح الآلي عن متصفح حقيقي.
const stealthInitScript = `
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
    window.navigator.permissions.query = (params) => (
        params && params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params)
    );
}
`

// userAgents: نفس USER_AGENTS بالضبط من pinterest_scraper.js السابق.
var userAgents = []string{
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
	"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
}

// RandomUserAgent يعيد user agent عشوائياً من نفس قائمة USER_AGENTS في
// النسخة السابقة (تنويع بسيط لتقليل بصمة التزامن بين عدة جلسات كشط).
func RandomUserAgent() string {
	return userAgents[rand.Intn(len(userAgents))]
}

// Options يخصص جلسة متصفح واحدة.
type Options struct {
	// UserAgent: فارغ = يُختار عشوائياً عبر RandomUserAgent().
	UserAgent string
	// WindowWidth/WindowHeight: فارغ (0) = لا يُضبط حجم نافذة صراحة
	// (يُستخدم افتراضي Chromium).
	WindowWidth  int
	WindowHeight int
	// ExecPathEnv: اسم متغير البيئة الذي قد يحدد مساراً مخصصاً لثنائي
	// Chromium (مطابق لـ PINTEREST_CHROMIUM_PATH / MANGA_CHROMIUM_PATH
	// السابقين) — كل plugin يمرر اسم متغيره الخاص هنا.
	ExecPathEnv string
}

// New يخصص Chrome headless جديد كلياً (سياق exec allocator منفصل تماماً
// عن أي جلسة أخرى — لا يشارك ملف تعريف مستخدم ولا ذاكرة تخزين مؤقت مع
// أي طلب متزامن آخر، تماماً كما كان كل استدعاء subprocess Node مستقلاً
// بالكامل سابقاً)، يفتح تبويباً جديداً، ويحقن سكربت stealth فوراً.
//
// يُعيد context.Context جاهزاً للتمرير مباشرة لـ chromedp.Run، ودالة
// تنظيف واحدة تُغلق كل شيء (التبويب + عملية Chromium الفرعية بالكامل)
// — استدعها عبر defer فور نجاح New. مهلة/إلغاء parent (عادة عبر
// context.WithTimeout في الـ plugin المستدعي) تُطبَّق على كامل الجلسة،
// بما فيها تشغيل Chromium نفسه — نفس أثر context.WithTimeout الذي كان
// يُلف حول exec.CommandContext سابقاً.
func New(parent context.Context, opts Options) (context.Context, context.CancelFunc, error) {
	allocOpts := append([]chromedp.ExecAllocatorOption{}, chromedp.DefaultExecAllocatorOptions[:]...)
	allocOpts = append(allocOpts,
		chromedp.Flag("disable-blink-features", "AutomationControlled"),
		chromedp.NoSandbox,
		chromedp.Flag("disable-dev-shm-usage", true),
		chromedp.Flag("disable-setuid-sandbox", true),
	)
	if opts.WindowWidth > 0 && opts.WindowHeight > 0 {
		allocOpts = append(allocOpts, chromedp.WindowSize(opts.WindowWidth, opts.WindowHeight))
	}
	ua := opts.UserAgent
	if ua == "" {
		ua = RandomUserAgent()
	}
	allocOpts = append(allocOpts, chromedp.UserAgent(ua))

	if opts.ExecPathEnv != "" {
		if p := os.Getenv(opts.ExecPathEnv); p != "" {
			allocOpts = append(allocOpts, chromedp.ExecPath(p))
		}
	}

	allocCtx, allocCancel := chromedp.NewExecAllocator(parent, allocOpts...)
	ctx, ctxCancel := chromedp.NewContext(allocCtx)

	cancel := func() {
		ctxCancel()
		allocCancel()
	}

	// chromedp.Run بلا actions إضافية غير حقن stealth يخصص العملية
	// الفرعية والتبويب فوراً (بدل الانتظار حتى أول Navigate) — هذا يعطي
	// خطأ فوري واضح لو تعذّر تشغيل Chromium (ثنائي مفقود مثلاً)، بدل
	// فشل غامض لاحقاً عند أول استخدام لـ ctx.
	if err := chromedp.Run(ctx, chromedp.ActionFunc(func(ctx context.Context) error {
		_, err := page.AddScriptToEvaluateOnNewDocument(stealthInitScript).Do(ctx)
		return err
	})); err != nil {
		cancel()
		return nil, nil, fmt.Errorf("تعذّر تشغيل Chromium: %w", err)
	}

	return ctx, cancel, nil
}

// ============================================================
// ⭐ SCRAPE — استخراج DOM من صفحة (جديد 2026-07)
// ============================================================
//
// Scrape() تستخدم نفس جلسة chromedp (عبر New() أعلاه) للتنقل إلى URL،
// انتظار زوال تحدي Cloudflare، ثم استخراج البيانات حسب extractRules عبر Colly.
//
// هذا يعني أن pinterest وmanga_bridge وأي plugin مستقبلي *كلهم* يستخدمون
// نفس بنية التشغيل الأساسية (stealth script، no-sandbox، user agent
// عشوائي) — لا تكرار، لا inconsistencies.
//
// الاستخدام: في أي plugin، استدعِ browser.Scrape(ctx, url, rules, opts)
// مباشرة — لا حاجة لplugin scraper منفصل.

// ExtractRule تحدد قاعدة استخراج واحدة: selector CSS + ما إذا كان
// الحقل وحيداً أم قائمة + استخراج attribute اختياري + عناصر فرعية.
type ExtractRule struct {
	Selector string            `json:"selector"`
	Attr     string            `json:"attr,omitempty"`
	Multiple bool              `json:"multiple"`
	Children map[string]string `json:"children,omitempty"`
}

// ExtractedItem يحمل قيمة مُستخرجة مع عناصرها الفرعية (لـ Multiple=true + Children).
type ExtractedItem struct {
	Value    string            `json:"value"`
	Children map[string]string `json:"children,omitempty"`
}

// ScrapeResult يحمل نتائج الاستخراج كاملة (آمن للاستخدام المتزامن).
type ScrapeResult struct {
	mu   sync.RWMutex
	Data map[string]any
}

// NewScrapeResult يُنشئ result فارغاً.
func NewScrapeResult() *ScrapeResult {
	return &ScrapeResult{Data: make(map[string]any)}
}

// All يُعيد نسخة من كل البيانات (بـ RLock).
func (r *ScrapeResult) All() map[string]any {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make(map[string]any, len(r.Data))
	maps.Copy(out, r.Data)
	return out
}

// Scrape ينتقل إلى urlStr عبر جلسة chromedp (تُنشأ من New() داخلياً)،
// ينتظر زوال تحدي Cloudflare، ثم يستخرج البيانات حسب extractRules عبر Colly.
//
// opts: خيارات جلسة المتصفح (نفس Options في New() — يمكن تمرير
// ExecPathEnv="SCRAPE_CHROMIUM_PATH" مثلاً).
//
// يُعيد *ScrapeResult جاهزاً للقراءة، أو خطأ لو فشل التنقل/الاستخراج.
// المستدعي مسؤول عن التحقق من أن النتائج غير فارغة إن كان ذلك ضرورياً.
func Scrape(ctx context.Context, urlStr string, extractRules map[string]ExtractRule, opts Options) (*ScrapeResult, error) {
	if urlStr == "" {
		return nil, fmt.Errorf("empty URL")
	}

	bctx, cancel, err := New(ctx, opts)
	if err != nil {
		return nil, fmt.Errorf("browser.New failed: %w", err)
	}
	defer cancel()

	var htmlContent string
	if err := chromedp.Run(bctx,
		chromedp.Navigate(urlStr),
		chromedp.WaitNotPresent(`#challenge-form`, chromedp.ByID),
		chromedp.WaitNotPresent(`#cf-challenge-running`, chromedp.ByID),
		chromedp.WaitNotPresent(`.cf-browser-verification`, chromedp.ByQuery),
		chromedp.Sleep(2), // 2 seconds
		chromedp.OuterHTML("html", &htmlContent),
	); err != nil {
		return nil, fmt.Errorf("chromedp navigation failed: %w", err)
	}

	result := NewScrapeResult()
	var collyWg sync.WaitGroup
	c := colly.NewCollector()

	for key, rule := range extractRules {
		k := key
		r := rule
		c.OnHTML(r.Selector, func(e *colly.HTMLElement) {
			collyWg.Add(1)
			defer collyWg.Done()

			value := ""
			if r.Attr != "" {
				value = e.Attr(r.Attr)
			} else {
				value = strings.TrimSpace(e.Text)
			}

			result.mu.Lock()
			defer result.mu.Unlock()

			if r.Multiple {
				if len(r.Children) > 0 {
					childData := make(map[string]string)
					for ck, csel := range r.Children {
						childData[ck] = strings.TrimSpace(e.ChildText(csel))
					}
					item := ExtractedItem{Value: value, Children: childData}
					existing, ok := result.Data[k]
					if !ok {
						result.Data[k] = []ExtractedItem{item}
					} else if slice, ok := existing.([]ExtractedItem); ok {
						result.Data[k] = append(slice, item)
					} else {
						result.Data[k] = []ExtractedItem{item}
					}
				} else {
					existing, ok := result.Data[k]
					if !ok {
						result.Data[k] = []string{value}
					} else if slice, ok := existing.([]string); ok {
						result.Data[k] = append(slice, value)
					} else {
						result.Data[k] = []string{value}
					}
				}
			} else {
				result.Data[k] = value
			}
		})
	}

	// colly.Collector ليس لديها طريقة لتحليل نص HTML جاهز في الذاكرة مباشرة
	// (لا توجد ParseBytes) — هي مبنية حول Visit(URL) عبر HTTP فقط. لذا
	// نُنشئ خادم HTTP محلي مؤقت يخدم htmlContent (القادم من chromedp)،
	// ثم نجعل colly يزوره كأي صفحة عادية؛ هذا يبقينا ضمن colly بالكامل
	// لعملية الاستخراج نفسها (OnHTML/ChildText/...).
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(htmlContent))
	}))
	defer ts.Close()

	if err := c.Visit(ts.URL); err != nil {
		return nil, fmt.Errorf("colly visit error: %w", err)
	}
	collyWg.Wait()

	return result, nil
}
