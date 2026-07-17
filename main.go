// main.go هو الملف الوحيد الذي "يعرف" عن كل الخدمات — كل خدمة أخرى
// معزولة تماماً عن غيرها ولا تعرف بوجودها. إضافة خدمة جديدة = حزمة جديدة
// تحت plugins/ تلتزم بعقد plugins.Service + سطر واحد في services() أدناه.
// لا registry، لا init()، لا blank imports — الأخطاء (مثل نسيان تسجيل
// خدمة، وهو تحديداً ما حدث سابقاً مع img_tr) تُكتشف الآن وقت الترجمة:
// نسيان سطر هنا يعني ببساطة أن الخدمة غير موجودة في القائمة، لا عطلاً
// صامتاً وقت التشغيل.
package main

import (
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/middleware"
	"sunkenbot/internal/plugins"
	"sunkenbot/internal/session"
	"sunkenbot/internal/shared"

	"sunkenbot/plugins/chess"
	"sunkenbot/plugins/fb"
	"sunkenbot/plugins/fdown"
	"sunkenbot/plugins/gemini"
	"sunkenbot/plugins/groq"
	"sunkenbot/plugins/img_tr"
	"sunkenbot/plugins/mangabridge"
	"sunkenbot/plugins/novel"
	"sunkenbot/plugins/ping"
	"sunkenbot/plugins/pinterest"
	"sunkenbot/plugins/sub"
)

const defaultPort = "7860"

// descriptions يقابل وصف كل خدمة (كان description في registry.Result
// سابقاً) — يبقى منفصلاً عن عقد plugins.Service عمداً (الواجهة تحتوي فقط
// Name/Routes، راجع internal/plugins) بدل توسيعها بميثود Description()
// لخدمة عرض بحت في endpoint واحد.
var descriptions = map[string]string{
	"chess":        chess.Description,
	"fb":           fb.Description,
	"fdown":        fdown.Description,
	"gemini":       gemini.Description,
	"groq":         groq.Description,
	"img_tr":       img_tr.Description,
	"manga_bridge": mangabridge.Description,
	"novel":        novel.Description,
	"ping":         ping.Description,
	"pinterest":    pinterest.Description,
	"sub":          sub.Description,
}

// services يبني كل الخدمات باعتمادياتها الصريحة. هذا الموضع — وليس كل
// حزمة plugin بمفردها — هو المكان الوحيد الذي يقرر أي http.Client يُمرَّر
// لأي خدمة (راجع جدول قرار http.Client في برومبت الترحيل لسبب كل
// اختيار: shared.Client لكل عميل كانت مهلته 30 ثانية أصلاً، وعميل محلي
// صريح لأي مهلة مختلفة — بما فيها "بلا مهلة إطلاقاً" لـ novel وimg_tr).
func services() []plugins.Service {
	geminiClient := &http.Client{Timeout: 25 * time.Second}
	groqDL := &http.Client{Timeout: 120 * time.Second}
	subClient := &http.Client{Timeout: 60 * time.Second}
	novelClient := &http.Client{}                       // بلا مهلة عامة — كل طلب يضبط مهلته عبر context، نفس السلوك القديم حرفياً
	fbClient := &http.Client{Timeout: 10 * time.Minute} // فيديوهات فيسبوك قد تكون كبيرة الحجم؛ مهلة أطول من subClient لنفس سبب plugins/sub لكن مع هامش أكبر

	return []plugins.Service{
		chess.New(),
		fb.New(fbClient),
		fdown.New(),
		gemini.New(geminiClient, session.New("gemini_sessions")),
		groq.New(shared.Client, groqDL, session.New("groq_sessions")),
		img_tr.New(http.DefaultClient), // بلا مهلة إطلاقاً — نفس http.DefaultClient الذي كان يُستخدَم مباشرة سابقاً
		mangabridge.New(),
		novel.New(novelClient),
		pinterest.New(shared.Client),
		sub.New(subClient),
		ping.New(),
	}
}

func main() {
	log.SetFlags(log.LstdFlags)

	mux := http.NewServeMux()

	// ─── / و /health: نفس المكافئ لـ main.py الأصلي ───────────────────
	mux.HandleFunc("GET /health", httpx.Handle(func(r *http.Request) (map[string]any, error) {
		return map[string]any{
			"status":    "healthy",
			"timestamp": time.Now().Unix(),
		}, nil
	}))

	// ─── تسجيل كل خدمة صراحةً (بدل تحميل ديناميكي عبر registry) ───────
	pluginResults := map[string]any{}
	for _, svc := range services() {
		routes := svc.Routes()
		for _, rt := range routes {
			mux.HandleFunc(rt.Method+" "+rt.Pattern, rt.Handler)
		}
		pluginResults[svc.Name()] = map[string]any{
			"status":       "loaded",
			"routes_added": len(routes),
			"description":  descriptions[svc.Name()],
		}
		log.Printf("[%s] ✅ محمَّل — %d route(s) مضافة", svc.Name(), len(routes))
	}
	log.Printf("[main] ✅ تم تحميل %d خدمة", len(pluginResults))

	mux.HandleFunc("GET /{$}", httpx.Handle(func(r *http.Request) (map[string]any, error) {
		return map[string]any{
			"status":  "online",
			"plugins": pluginResults,
		}, nil
	}))

	// ─── طبقات الـ middleware: CORS ثم حماية التوكن (نفس ترتيب بايثون) ─
	internalToken := os.Getenv("INTERNAL_TOKEN")
	var handler http.Handler = mux
	handler = middleware.Auth(internalToken, handler)
	handler = middleware.CORS(handler)

	port := os.Getenv("PORT")
	if port == "" {
		port = defaultPort
	}
	if _, err := strconv.Atoi(port); err != nil {
		port = defaultPort
	}

	addr := ":" + port
	log.Printf("🚀 Sunken Bot (Go) يستمع على %s", addr)
	log.Fatal(http.ListenAndServe(addr, handler))
}
