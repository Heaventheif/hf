// Package delegate يحول أي route في Go إلى "موكل" (delegated) لخدمة Python
// الداخلية (s) — أي أن الطلب يصل إلى Go أولاً (نفس منفذ 7860 ونفس حماية
// INTERNAL_TOKEN)، ثم يعيد Go توجيهه كاملاً إلى Python عبر pyclient مع
// نفس المسار ونفس الـ headers ونفس جسم الطلب حرفياً (بما فيه الملفات
// المرفقة كـ multipart/form-data).
//
// لماذا Proxy كامل (bytes) بدل إعادة بناء JSON؟ لأن بعض endpoints الموكلة
// تستقبل multipart (صور OCR) أو أجسام لا نعرف بنيتها من Go — إعادة إرسال
// الجسم خاماً (raw body) يضمن تطابقاً تاماً مع ما كانت Python تستقبله
// سابقاً من Render مباشرة، دون أي ازدواجية في تعريف request structs.
package delegate

import (
	"context"
	"io"
	"log"
	"net/http"
	"time"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/plugins"
)

// Endpoint يصف route واحداً موكلاً لـ Python: method + path (نفس path الذي
// كانت تتلقاه Python مباشرة سابقاً، مثل "/process_move") + مهلة اختيارية
// (افتراضي 90 ثانية — مناسب لـ OCR والتحويلات الثقيلة).
type Endpoint struct {
	Method  string
	Path    string
	Timeout time.Duration
}

// Service يحمل pyclient ويولّد routes موكلة من قائمة endpoints.
type Service struct {
	pc        PyDelegator
	endpoints []Endpoint
	name      string
}

// PyDelegator هو الحد الأدنى الذي يحتاجه delegate من pyclient — واجهة صغيرة
// بدلاً من ربط delegate بحزمة pyclient بكاملها (يبقي اختبار delegate بسيطاً
// ويمنع اعتماديات دائرية محتملة). pyclient.Delegator يُعرَّف هناك ويرضيها.
type PyDelegator interface {
	Forward(ctx context.Context, method, path string, body io.Reader, contentType string) (status int, data []byte, err error)
}

// New ينشئ Service موكلاً باسم name وendpoints محددة. pc: wrapper على
// pyclient (راجع pyclient.Delegator).
func New(name string, pc PyDelegator, endpoints []Endpoint) Service {
	return Service{name: name, pc: pc, endpoints: endpoints}
}

// DefaultTimeout هي المهلة الافتراضية لأي endpoint موكَل بلا مهلة صريحة.
const DefaultTimeout = 90 * time.Second

func (s *Service) Name() string { return s.name }

func (s *Service) Routes() []plugins.Route {
	routes := make([]plugins.Route, 0, len(s.endpoints))
	for _, ep := range s.endpoints {
		t := ep.Timeout
		if t <= 0 {
			t = DefaultTimeout
		}
		// نسخة محلية من ep داخل closure (قاعدة Go الشهيرة في الحلقات).
		ep := ep
		routes = append(routes, plugins.Route{
			Method:  ep.Method,
			Pattern: ep.Path, // المسار فقط — main.go يضيف method تلقائياً (نفس نمط كل plugins)
			Handler: s.makeHandler(ep, t),
		})
	}
	return routes
}

// makeHandler يبني handler يعيد إرسال الطلب كاملاً (headers باستثناء
// hop-by-hop، الجسم، content-type) إلى Python ويعيد استجابته كما هي.
func (s *Service) makeHandler(ep Endpoint, timeout time.Duration) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), timeout)
		defer cancel()

		// نقرأ الجسم كاملاً مرة واحدة — بعض الأجسام (multipart) لا تُقرأ
		// مرتين، والقراءة هنا تضمن أيضاً أن body nil للطلبات بلا جسم.
		var body io.Reader
		if r.Body != nil {
			defer r.Body.Close()
			body = r.Body
		}
		status, data, err := s.pc.Forward(ctx, ep.Method, ep.Path, body, r.Header.Get("Content-Type"))
		if err != nil {
			log.Printf("⚠️  [delegate:%s] فشل التوجيه إلى Python: %s", s.name, err)
			httpx.Error(w, http.StatusBadGateway, "خدمة Python غير متاحة: "+err.Error())
			return
		}

		w.Header().Set("Content-Type", firstNonEmpty(r.Header.Get("Accept"), "application/json"))
		w.WriteHeader(status)
		if len(data) > 0 {
			_, _ = w.Write(data)
		}
	}
}

func firstNonEmpty(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

// Description يعيد وصفاً عاماً للخدمة الموكلة — يمكن تجاوزه عند البناء.
const Description = "مسارات موكلة لخدمة Python الداخلية (chess/ocr/dama) — تمر عبر Go بنفس المنفذ والحماية"
