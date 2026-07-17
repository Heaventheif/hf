package img_tr

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

func findRoute(t *testing.T, method, pattern string) http.HandlerFunc {
	t.Helper()
	svc := New(http.DefaultClient)
	if svc.Name() != "img_tr" {
		t.Fatalf("Name() = %q, want %q", svc.Name(), "img_tr")
	}
	for _, r := range svc.Routes() {
		if r.Method == method && r.Pattern == pattern {
			return r.Handler
		}
	}
	t.Fatalf("route %s %s not found in Routes()", method, pattern)
	return nil
}

// ─── POST /img_tr (صورة واحدة) ────────────────────────────────────────

func TestHandleTranslate_InvalidJSON_Returns400(t *testing.T) {
	handler := findRoute(t, "POST", "/img_tr")

	req := httptest.NewRequest(http.MethodPost, "/img_tr", strings.NewReader(`{not valid json`))
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d (historical behavior: img_tr returns 400 on decode error)", rec.Code, http.StatusBadRequest)
	}
	if !strings.Contains(rec.Body.String(), "جسم الطلب ليس JSON صالح") {
		t.Fatalf("body = %q, want it to contain the original error message", rec.Body.String())
	}
}

func TestHandleTranslate_MissingImage_Returns400(t *testing.T) {
	handler := findRoute(t, "POST", "/img_tr")

	req := httptest.NewRequest(http.MethodPost, "/img_tr", strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusBadRequest)
	}
}

// TestHandleTranslate_SSRFRejected يثبت أن حماية netguard مفعّلة فعلياً
// على مسار الصورة الواحدة: رابط يشير لعنوان شبكة داخلية/خاصة يُرفض بـ
// 400 بدل محاولة جلبه من جهة الخادم.
func TestHandleTranslate_SSRFRejected(t *testing.T) {
	handler := findRoute(t, "POST", "/img_tr")

	req := httptest.NewRequest(http.MethodPost, "/img_tr",
		strings.NewReader(`{"image_url": "http://169.254.169.254/latest/meta-data/"}`))
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d (SSRF-guarded URL must be rejected)", rec.Code, http.StatusBadRequest)
	}
	if !strings.Contains(rec.Body.String(), "مرفوض") {
		t.Fatalf("body = %q, want it to mention rejection", rec.Body.String())
	}
}

// ─── POST /img_tr/batch (عدة صور بطلب واحد) ───────────────────────────

func TestHandleTranslateBatch_InvalidJSON_Returns400(t *testing.T) {
	handler := findRoute(t, "POST", "/img_tr/batch")

	req := httptest.NewRequest(http.MethodPost, "/img_tr/batch", strings.NewReader(`{not valid json`))
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusBadRequest)
	}
}

func TestHandleTranslateBatch_EmptyList_Returns400(t *testing.T) {
	handler := findRoute(t, "POST", "/img_tr/batch")

	req := httptest.NewRequest(http.MethodPost, "/img_tr/batch", strings.NewReader(`{"image_urls": []}`))
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d", rec.Code, http.StatusBadRequest)
	}
}

func TestHandleTranslateBatch_TooManyImages_Returns400(t *testing.T) {
	handler := findRoute(t, "POST", "/img_tr/batch")

	urls := make([]string, maxBatchImages+1)
	for i := range urls {
		urls[i] = "http://example.invalid/" + strconv.Itoa(i) + ".jpg"
	}
	body, _ := json.Marshal(batchTranslateRequest{ImageURLs: urls})

	req := httptest.NewRequest(http.MethodPost, "/img_tr/batch", strings.NewReader(string(body)))
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d (batch exceeding maxBatchImages must be rejected)", rec.Code, http.StatusBadRequest)
	}
}

// TestHandleTranslateBatch_SSRFRejectedPerImage يثبت السلوك الجوهري
// للدفعة: فشل SSRF-guard على بعض الروابط لا يُسقط الطلب كله (يبقى الرد
// 200)، بل يظهر كـ Error في عنصر تلك الصورة فقط، مع الحفاظ على Index
// مطابق تماماً لموضع الرابط في image_urls الأصلية رغم أن الترجمة الفعلية
// تجري بالتزامن (goroutines) لا بالتتابع.
func TestHandleTranslateBatch_SSRFRejectedPerImage(t *testing.T) {
	handler := findRoute(t, "POST", "/img_tr/batch")

	body, _ := json.Marshal(batchTranslateRequest{
		ImageURLs: []string{
			"http://169.254.169.254/latest/meta-data/",
			"http://10.0.0.5/private.jpg",
			"http://127.0.0.1/loopback.jpg",
		},
	})

	req := httptest.NewRequest(http.MethodPost, "/img_tr/batch", strings.NewReader(string(body)))
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d (partial per-image failures must not fail the whole batch)", rec.Code, http.StatusOK)
	}

	var got batchTranslateResult
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("failed to decode response: %v", err)
	}
	if len(got.Results) != 3 {
		t.Fatalf("len(Results) = %d, want 3", len(got.Results))
	}
	for i, res := range got.Results {
		if res.Index != i {
			t.Errorf("Results[%d].Index = %d, want %d (order must be preserved despite concurrency)", i, res.Index, i)
		}
		if res.Error == "" {
			t.Errorf("Results[%d].Error is empty, want an SSRF-rejection message", i)
		}
		if !strings.Contains(res.Error, "مرفوض") {
			t.Errorf("Results[%d].Error = %q, want it to mention rejection", i, res.Error)
		}
		if res.ImageBase64 != "" {
			t.Errorf("Results[%d].ImageBase64 should be empty on failure", i)
		}
	}
}
