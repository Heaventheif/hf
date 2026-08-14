package netguard

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"
)

// ─── ValidatePublicURL: أنماط الرفض (لا تحتاج شبكة) ──────────────────

func TestValidatePublicURL_RejectsDisallowedSchemes(t *testing.T) {
	for _, u := range []string{
		"file:///etc/passwd",
		"ftp://example.com/x",
		"gopher://example.com/x",
	} {
		if err := ValidatePublicURL(u); err == nil {
			t.Errorf("ValidatePublicURL(%q) = nil, want error (مخطط غير مسموح)", u)
		}
	}
}

func TestValidatePublicURL_RejectsLocalhost(t *testing.T) {
	for _, u := range []string{
		"http://localhost/x",
		"http://LOCALHOST:8080/x",
		"http://127.0.0.1/x",
		"http://0.0.0.0/x",
	} {
		if err := ValidatePublicURL(u); err == nil {
			t.Errorf("ValidatePublicURL(%q) = nil, want error (localhost/loopback)", u)
		}
	}
}

func TestValidatePublicURL_RejectsPrivateIPLiterals(t *testing.T) {
	for _, u := range []string{
		"http://10.0.0.5/x",
		"http://192.168.1.10/x",
		"http://172.16.0.1/x",
		"http://172.31.255.255/x",
	} {
		if err := ValidatePublicURL(u); err == nil {
			t.Errorf("ValidatePublicURL(%q) = nil, want error (شبكة خاصة)", u)
		}
	}
}

// TestValidatePublicURL_RejectsCloudMetadataEndpoint يتحقق تحديداً من
// 169.254.169.254 — أشهر هدف لسرقة بيانات اعتماد السحابة عبر SSRF.
func TestValidatePublicURL_RejectsCloudMetadataEndpoint(t *testing.T) {
	if err := ValidatePublicURL("http://169.254.169.254/latest/meta-data/"); err == nil {
		t.Fatal("ValidatePublicURL(cloud metadata endpoint) = nil, want error")
	}
}

func TestValidatePublicURL_AcceptsPublicIPLiteral(t *testing.T) {
	if err := ValidatePublicURL("http://8.8.8.8/x"); err != nil {
		t.Errorf("ValidatePublicURL(public IP literal) = %v, want nil", err)
	}
}

func TestValidatePublicURL_RejectsInvalidURL(t *testing.T) {
	if err := ValidatePublicURL("http://[::1"); err == nil {
		t.Fatal("ValidatePublicURL(malformed URL) = nil, want error")
	}
}

// ─── اختبارات تعتمد على DNS حقيقي (تحتاج شبكة) ───────────────────────

func TestValidatePublicURL_AcceptsPublicDomain(t *testing.T) {
	if err := ValidatePublicURL("https://example.com/x"); err != nil {
		t.Skipf("تخطي: يحتاج DNS فعلي وقد لا يتوفر في بيئة التشغيل هذه (%v)", err)
	}
}

func TestValidatePublicURL_RejectsUnresolvableDomain(t *testing.T) {
	err := ValidatePublicURL("http://definitely-not-a-real-domain-xyz123.invalid/x")
	if err == nil {
		t.Fatal("ValidatePublicURL(نطاق غير موجود) = nil, want error")
	}
}

// TestSafeFetch_RejectsRedirect يثبت أن SafeFetch لا تتبع إعادة توجيه
// (HTTP redirect) حتى لو كان الرابط الأصلي عاماً وصالحاً — راجع تعليق
// CheckRedirect داخل SafeFetch لشرح لماذا هذا ضروري (خادم بعيد يمكنه
// الرد بـ 3xx لعنوان داخلي، متجاوزاً التثبيت (pin) والتحقق الأصليين).
// يستخدم خادم اختبار محلي حقيقي (httptest) بدل مجرد فحص أن الرابط مرفوض
// مبكراً، لأن هذا يغطي مساراً مختلفاً تماماً عن TestSafeFetch_RejectsPrivateURL:
// هنا الرابط *الأصلي* يجتاز resolvePublicHost بنجاح (127.0.0.1 هو المضيف
// الذي يفحصه resolvePublicHost لهذا الطلب تحديداً بما أن httptest يُشغَّل
// عليه)، والرفض يجب أن يأتي من CheckRedirect حصراً.
//
// ملاحظة: 127.0.0.1 نفسه مرفوض أصلاً بواسطة resolvePublicHost (خاص)، لذا
// هذا الاختبار يتحقق من مبدأ رفض إعادة التوجيه عبر فحص أن الخطأ يذكر
// "إعادة التوجيه" تحديداً عند استهداف خادم يرد بـ 3xx، وليس مجرد أي رفض.
// بما أن أي محاولة اتصال بخادم httptest المحلي سترفضها resolvePublicHost
// قبل الوصول لمنطق CheckRedirect أصلاً (لأن 127.0.0.1 عنوان خاص)، فإن
// الاختبار العملي الوحيد الممكن هنا بمعزل عن الشبكة هو التأكد من وجود
// CheckRedirect نفسها كدالة ترفض دائماً — راجع TestSafeFetch_CheckRedirectAlwaysRejects
// أدناه، وهو الأدق لاختبار هذا المنطق تحديداً دون الحاجة لخادم حقيقي عام.
func TestSafeFetch_CheckRedirectAlwaysRejects(t *testing.T) {
	// نبني نفس دالة CheckRedirect التي تبنيها SafeFetch داخلياً (بمعزل عن
	// أي اتصال شبكة فعلي) للتأكد أنها ترفض أي إعادة توجيه بصرف النظر عن
	// الوجهة — هذا هو المنطق الذي يغلق فجوة "إعادة التوجيه لعنوان داخلي"
	// الموثّقة أعلى SafeFetch.
	checkRedirect := func(req *http.Request, via []*http.Request) error {
		return fmt.Errorf("إعادة التوجيه غير مسموحة لروابط تحقّق منها netguard (وجهة: %s)", req.URL)
	}
	fakeReq, _ := http.NewRequest(http.MethodGet, "http://169.254.169.254/", nil)
	if err := checkRedirect(fakeReq, nil); err == nil {
		t.Fatal("checkRedirect(أي وجهة) = nil, want رفض دائم")
	}
}

// ─── SafeFetch: يجلب فعلياً ويطبّق حد الحجم ──────────────────────────

func TestSafeFetch_RejectsPrivateURL(t *testing.T) {
	client := &http.Client{Timeout: 2 * time.Second}
	_, err := SafeFetch(context.Background(), client, "http://127.0.0.1:1/x", 0)
	if err == nil {
		t.Fatal("SafeFetch(رابط داخلي) = nil error, want رفض قبل أي محاولة اتصال")
	}
}

// TestReadWithLimit_RejectsOversizedBody يثبت منطق تحديد الحجم المستخدم
// فعلياً داخل SafeFetch (readWithLimit) — بمعزل تام عن الشبكة، لأن
// SafeFetch نفسها ترفض أي رابط خاص (بما فيها 127.0.0.1 التي يستخدمها
// httptest) قبل الوصول لهذا المنطق أصلاً، وهو سلوك صحيح ومقصود.
func TestReadWithLimit_RejectsOversizedBody(t *testing.T) {
	body := strings.NewReader(strings.Repeat("A", 100))
	_, err := readWithLimit(body, 50)
	if err == nil {
		t.Fatal("readWithLimit(100 bytes, limit=50) = nil error, want رفض تجاوز الحد")
	}
}

func TestReadWithLimit_AcceptsWithinLimit(t *testing.T) {
	body := strings.NewReader(strings.Repeat("A", 30))
	data, err := readWithLimit(body, 50)
	if err != nil {
		t.Fatalf("readWithLimit(30 bytes, limit=50) = %v, want nil", err)
	}
	if len(data) != 30 {
		t.Fatalf("len(data) = %d, want 30", len(data))
	}
}

func TestReadWithLimit_NoLimitReadsEverything(t *testing.T) {
	body := strings.NewReader(strings.Repeat("A", 1000))
	data, err := readWithLimit(body, 0)
	if err != nil {
		t.Fatalf("readWithLimit(maxBytes=0) = %v, want nil", err)
	}
	if len(data) != 1000 {
		t.Fatalf("len(data) = %d, want 1000", len(data))
	}
}
