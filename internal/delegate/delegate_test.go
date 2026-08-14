package delegate

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// fakeDelegator يُجسِّد PyDelegator — يعيد ما استقبله حرفياً.
type fakeDelegator struct {
	receivedMethod, receivedPath, receivedCT string
	receivedBody                             string
	status                                   int
	body                                     string
}

func (f *fakeDelegator) Forward(_ context.Context, method, path string, body io.Reader, contentType string) (int, []byte, error) {
	f.receivedMethod = method
	f.receivedPath = path
	f.receivedCT = contentType
	if body != nil {
		raw, _ := io.ReadAll(body)
		f.receivedBody = string(raw)
	}
	return f.status, []byte(f.body), nil
}

// ─── اختبار Routes: كل endpoint يُسجَّل بالمسار والمهلة الصحيحين ─────────────
func TestRoutesRegistered(t *testing.T) {
	f := &fakeDelegator{status: 200}
	svc := New("test", f, []Endpoint{
		{Method: "POST", Path: "/process_move", Timeout: 45 * time.Second},
		{Method: "POST", Path: "/dama/new_game"}, // مهلة افتراضية
	})
	routes := svc.Routes()
	if len(routes) != 2 {
		t.Fatalf("توقعت routeين، حصلت على %d", len(routes))
	}
	// Pattern = المسار فقط (main.go يضيف method تلقائياً — نفس نمط كل plugins).
	if routes[0].Pattern != "/process_move" {
		t.Errorf("pattern خاطئ: %s", routes[0].Pattern)
	}
	if routes[0].Method != "POST" {
		t.Errorf("method خاطئ: %s", routes[0].Method)
	}
}

// ─── اختبار handler: يعيد إرسال method + path + الجسم + content-type حرفياً ─
func TestForwardsRawBody(t *testing.T) {
	f := &fakeDelegator{status: 200, body: `{"fen":"abc"}`}
	svc := New("test", f, []Endpoint{{Method: "POST", Path: "/process_move"}})
	routes := svc.Routes()

	body := `{"fen":"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1","move":"e2e4"}`
	req := httptest.NewRequest("POST", "/process_move", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rr := httptest.NewRecorder()

	routes[0].Handler(rr, req)

	if f.receivedMethod != "POST" || f.receivedPath != "/process_move" {
		t.Errorf("لم يُمرَّر method/path بشكل صحيح: %s %s", f.receivedMethod, f.receivedPath)
	}
	if f.receivedBody != body {
		t.Errorf("الجسم لم يُمرَّر حرفياً — استُقبل: %s", f.receivedBody)
	}
	if f.receivedCT != "application/json" {
		t.Errorf("content-type لم يُمرَّر: %s", f.receivedCT)
	}
	if rr.Code != 200 || rr.Body.String() != `{"fen":"abc"}` {
		t.Errorf("الاستجابة لم تُعد كما هي: %d %s", rr.Code, rr.Body.String())
	}
}

// ─── اختبار فشل التوجيه: خطأ من pyclient = 502 واضح ────────────────────────
func TestDelegatorErrorReturns502(t *testing.T) {
	errDelegator := &errDelegatorImpl{}
	svc := New("test", errDelegator, []Endpoint{{Method: "POST", Path: "/ocr/infer"}})
	routes := svc.Routes()

	req := httptest.NewRequest("POST", "/ocr/infer", nil)
	rr := httptest.NewRecorder()
	routes[0].Handler(rr, req)

	if rr.Code != http.StatusBadGateway {
		t.Errorf("توقعت 502 عند فشل التوجيه، حصلت على %d", rr.Code)
	}
}

type errDelegatorImpl struct{}

func (e *errDelegatorImpl) Forward(_ context.Context, _, _ string, _ io.Reader, _ string) (int, []byte, error) {
	return 0, nil, io.EOF
}

// ─── اختبار GET بلا جسم ─────────────────────────────────────────────────────
func TestGetNoBody(t *testing.T) {
	f := &fakeDelegator{status: 200, body: "ok"}
	svc := New("test", f, []Endpoint{{Method: "GET", Path: "/dama/new_game"}})
	routes := svc.Routes()

	req := httptest.NewRequest("GET", "/dama/new_game", nil)
	rr := httptest.NewRecorder()
	routes[0].Handler(rr, req)

	if f.receivedMethod != "GET" || f.receivedBody != "" {
		t.Errorf("طلب GET أُرسل بجسم: method=%s body=%q", f.receivedMethod, f.receivedBody)
	}
}
