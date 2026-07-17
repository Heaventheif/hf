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
// ملاحظة صادقة مهمة (بنفس روح التعليقات الصادقة الموجودة سابقاً فوق
// scripts/pinterest/pinterest_scraper.js وscripts/mangabridge/manga_scraper.js):
// هذا الكود يحتاج ثنائي Chromium فعلياً مثبَّتاً على الجهاز الذي يشغّله
// (راجع الـ Dockerfile — مرحلة التشغيل تثبّت حزمة chromium عبر apt).
// لم يتسنَّ اختبار هذا بمتصفح حقيقي ضد مواقع تحمي نفسها بـ Cloudflare
// (Pinterest، 3asq.online) في بيئة تطوير هذا المشروع تحديداً، لأن تحميل
// ثنائي Chromium نفسه محجوب شبكياً هنا (نفس قيد حجب cdn.playwright.dev
// الموثَّق سابقاً ينطبق أيضاً على مصادر تحميل Chromium المستقل) — لكن
// حزمة chromedp نفسها تم تنزيلها وبناؤها واختبارها فعلياً في هذه البيئة
// (راجع replace directives في go.mod لسبب استخدام مرايا GitHub بدل
// golang.org/x/... مباشرة)، وSTEALTH_INIT_SCRIPT أدناه نسخة طبق الأصل
// عن السكربت الذي كان يُستخدَم في نسخة Node/Playwright السابقة.
package browser

import (
	"context"
	"fmt"
	"math/rand"
	"os"

	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/chromedp"
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
