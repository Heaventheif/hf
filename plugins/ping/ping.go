// Package ping مثال توضيحي بسيط لا علاقة له بأي ميزة من ميزات بايثون
// الأصلية — الغرض الوحيد منه إثبات أن إضافة خدمة جديدة كاملة لهذا
// المشروع تحتاج فقط: (١) حزمة جديدة كهذه تلتزم بعقد plugins.Service،
// و(٢) سطر واحد يضيفها لقائمة services() في main.go. لا registry، لا
// init()، لا أي تعديل آخر في أي مكان — هذا هو الاختبار العملي لبساطة
// البنية بعد الترحيل.
package ping

import (
	"net/http"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/plugins"
)

const Description = "مثال توضيحي: خدمة صحة بسيطة تثبت سهولة إضافة خدمة جديدة"

type Service struct{}

func New() *Service { return &Service{} }

func (s *Service) Name() string { return "ping" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "GET", Pattern: "/ping", Handler: httpx.Handle(s.handlePing)},
	}
}

type pingResult struct {
	Message string `json:"message"`
}

// handlePing يستخدم httpx.Handle مباشرة (لا WrapJSON) لأن /ping بلا جسم
// طلب أصلاً — تماماً كما توثّق internal/httpx: Handle لأي endpoint بلا
// جسم، وWrapJSON للجديد فقط عند وجود جسم JSON.
func (s *Service) handlePing(r *http.Request) (pingResult, error) {
	return pingResult{Message: "pong"}, nil
}
