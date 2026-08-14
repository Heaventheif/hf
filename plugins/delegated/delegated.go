// Package delegated يحمل قائمة المسارات التي تُنفَّذ في خدمة Python
// الداخلية (s) لكن تُعرَّض عبر Go (g) على نفس المنفذ ونفس الحماية —
// بحيث يبدو كل شيء من خارج الفضاء كفضاء واحد (HF_SPACE_URL واحد).
//
// إضافة مسار موكَل جديد = سطر واحد في delegatedEndpoints أدناه. لا تعديل
// في أي ملف آخر (راجع internal/delegate).
//
// لماذا هذه المسارات في Python تحديداً؟ لأنها تعتمد على حزم Python ثقيلة
// أو منطق موجود فقط هناك: chess (chess + cairosvg + Stockfish/Lichess)،
// dama (cairosvg)، ocr (PaddleOCR + torch). المنطق الخفيف السريع (gemini،
// groq، comic، novel، sub، manga-bridge، pinterest) يبقى محسوباً في Go
// مباشرة.
package delegated

import (
	"context"
	"log"
	"time"

	"sunkenbot/internal/delegate"
	"sunkenbot/internal/plugins"
	"sunkenbot/internal/pyclient"
)

const Description = "مسارات Python الموكلة: chess / dama / OCR — تعمل عبر نفس المنفذ والحماية"

// ─── قائمة المسارات الموكلة ─────────────────────────────────────────────────
// كل سطر هنا = endpoint واحد يظهر على Go (وبالتالي على HF_SPACE_URL) لكن
// يُنفَّذ فعلياً في خدمة Python. الصيغة: Method + Path الذي كانت Python
// تستقبله مباشرة سابقاً. المهلة اختيارية (تُترك صفرية = افتراضي 90 ثانية).
var delegatedEndpoints = []delegate.Endpoint{
	// chess.py — منطق شطرنج كامل + رسم PNG عبر cairosvg + محرك
	// Lichess Cloud/Stockfish.
	{Method: "POST", Path: "/process_move", Timeout: 45 * time.Second},
	// dama.py — لعبة الدامة (dama) + لوحة PNG.
	{Method: "POST", Path: "/dama/new_game"},
	{Method: "POST", Path: "/dama/move"},
	// ocr.py — PaddleOCR + ترجمة النص المستخرج.
	{Method: "POST", Path: "/ocr/infer", Timeout: 180 * time.Second},
}

// Routes يعيد المسارات الموكلة كلها دفعة واحدة — من الخارج لا فرق بين plugin
// محلي وplugin موكل، كلاهما يلتزم بعقد plugins.Service نفسه.
func Routes(pc *pyclient.Client) []plugins.Route {
	svc := delegate.New("delegated", pc, delegatedEndpoints)
	return svc.Routes()
}

// HealthCheck يتحقق أن خدمة Python الداخلية حية بعد البدء — تحذير واضح في
// اللوق بدل فشل صامت عند أول طلب لاحق. يُستدعى مرة واحدة من main.go.
func HealthCheck(pc *pyclient.Client) {
	go func() {
		time.Sleep(3 * time.Second)
		var out struct {
			Status string `json:"status"`
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := pc.Get(ctx, "/health", &out); err != nil {
			log.Println("⚠️  [delegated] خدمة Python الداخلية غير مستجيبة على HF_PYTHON_URL:", err)
			return
		}
		log.Println("✅  [delegated] خدمة Python الداخلية جاهزة — مسارات chess/dama/OCR تعمل عبر Go")
	}()
}
