package comic

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func routeByPattern(svc *Service, pattern string) *http.HandlerFunc {
	for _, rt := range svc.Routes() {
		if rt.Pattern == pattern {
			h := rt.Handler
			return &h
		}
	}
	return nil
}

func TestRoutes(t *testing.T) {
	svc := New(http.DefaultClient)
	routes := svc.Routes()
	if len(routes) != 2 {
		t.Fatalf("expected exactly 2 routes, got %d", len(routes))
	}
	for _, pattern := range []string{"/comic/label", "/comic/images"} {
		if routeByPattern(svc, pattern) == nil {
			t.Errorf("missing route: %s", pattern)
		}
	}
}

func TestHandleLabel_MissingURL(t *testing.T) {
	svc := New(http.DefaultClient)
	h := routeByPattern(svc, "/comic/label")

	req := httptest.NewRequest(http.MethodPost, "/comic/label", strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	(*h)(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d. body: %s", rec.Code, http.StatusBadRequest, rec.Body.String())
	}
}

func TestHandleImages_MissingURL(t *testing.T) {
	svc := New(http.DefaultClient)
	h := routeByPattern(svc, "/comic/images")

	req := httptest.NewRequest(http.MethodPost, "/comic/images", strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	(*h)(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d. body: %s", rec.Code, http.StatusBadRequest, rec.Body.String())
	}
}
