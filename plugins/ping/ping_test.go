package ping

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ملاحظة: نسخة ping في g2 تضيف أيضاً POST /ping/echo وتوضّح "عقد الخطأ
// الموحّد" (WrapJSON). نسخة g1 هذه أبسط (GET /ping فقط، بلا echo) — لذا
// يُختبر هنا فقط ما هو موجود فعلاً، دون افتراض سلوك غير مطبَّق.

func TestPing_Name(t *testing.T) {
	s := New()
	if s.Name() != "ping" {
		t.Fatalf("Name() = %q, want %q", s.Name(), "ping")
	}
}

func TestPing_ReturnsPongMessage(t *testing.T) {
	s := New()
	var handler http.HandlerFunc
	for _, r := range s.Routes() {
		if r.Method == "GET" && r.Pattern == "/ping" {
			handler = r.Handler
		}
	}
	if handler == nil {
		t.Fatal("GET /ping not registered in Routes()")
	}

	req := httptest.NewRequest(http.MethodGet, "/ping", nil)
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusOK)
	}
	if got := rec.Body.String(); !strings.Contains(got, `"message":"pong"`) {
		t.Fatalf("body = %q, want it to contain %q", got, `"message":"pong"`)
	}
}
