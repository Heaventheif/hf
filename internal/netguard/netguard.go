// Package netguard يوفّر حماية بسيطة ضد SSRF (Server-Side Request Forgery):
// قبل أن يطلب الخادم أي رابط وارد من المستخدم (صورة/صوت/فيديو مرفق)، يجب
// التأكد أن هذا الرابط لا يشير لعنوان شبكي داخلي/خاص (الشبكة الداخلية،
// localhost، أو endpoint بيانات اعتماد السحابة مثل 169.254.169.254).
//
// هذا النمط كان مطبَّقاً سابقاً في plugins/fb (تقييد النطاق على facebook.com
// فقط)، لكنه لم يكن مطبَّقاً على مسارات جلب المرفقات العامة في groq/img_tr،
// حيث يمكن لأي رابط عشوائي أن يصل لِـ http.Client مباشرة من جهة الخادم.
package netguard

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
)

// resolvePublicHost يحلّل الرابط، يتحقق من مخططه ومضيفه، ثم يحلّ اسم
// النطاق (لو لم يكن IP حرفياً) ويُعيد أول عنوان IP عام تحقق منه (pinned)
// بالإضافة للرابط المُحلَّل. هذا العنوان المُعاد هو ما يجب استخدامه فعلياً
// للاتصال — راجع SafeFetch لسبب أهمية هذا بالتحديد.
func resolvePublicHost(ctx context.Context, rawURL string) (*url.URL, net.IP, error) {
	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return nil, nil, fmt.Errorf("رابط غير صالح: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, nil, fmt.Errorf("مخطط الرابط غير مسموح: %q (مسموح http/https فقط)", u.Scheme)
	}
	host := u.Hostname()
	if host == "" {
		return nil, nil, fmt.Errorf("الرابط لا يحتوي مضيفاً صالحاً")
	}
	if strings.EqualFold(host, "localhost") {
		return nil, nil, fmt.Errorf("الروابط المحلية (localhost) غير مسموحة")
	}

	// لو المضيف عنوان IP حرفي، تحقق منه مباشرة.
	if ip := net.ParseIP(host); ip != nil {
		if !isPublicIP(ip) {
			return nil, nil, fmt.Errorf("عنوان IP غير مسموح به (شبكة داخلية/خاصة): %s", ip)
		}
		return u, ip, nil
	}

	// وإلا، حلّ اسم النطاق وتأكد أن كل العناوين الناتجة عامة. نستخدم
	// LookupIPAddr مع ctx (بدل net.LookupIP التي لا تحترم أي مهلة/إلغاء)
	// حتى لا يُعلَّق الطلب إلى ما لا نهاية لو كان محلل DNS بطيئاً أو غير
	// مستجيب — راجع مهلة ctx التي يمررها المستدعي (عادة عبر http.Client
	// أو context.WithTimeout).
	if ctx == nil {
		ctx = context.Background()
	}
	addrs, err := net.DefaultResolver.LookupIPAddr(ctx, host)
	if err != nil {
		return nil, nil, fmt.Errorf("تعذّر تحليل اسم النطاق: %w", err)
	}
	if len(addrs) == 0 {
		return nil, nil, fmt.Errorf("لم يُحلَّل اسم النطاق لأي عنوان IP")
	}
	for _, addr := range addrs {
		if !isPublicIP(addr.IP) {
			return nil, nil, fmt.Errorf("اسم النطاق يُحلّ لعنوان شبكة داخلية/خاصة غير مسموح بها")
		}
	}
	return u, addrs[0].IP, nil
}

// ValidatePublicURL يتحقق أن الرابط آمن للجلب (راجع resolvePublicHost)
// دون تنفيذ أي طلب فعلي. ملاحظة مهمة: هذا التحقق وحده *لا* يمنع
// DNS rebinding — أي كود يستدعي ValidatePublicURL ثم ينفّذ طلب HTTP منفصل
// بعدها (مثل http.Client.Do على نفس الرابط) يعيد حلّ DNS من جديد بشكل
// مستقل تماماً عن هذا التحقق؛ لو تحكّم مهاجم بخادم DNS بـ TTL قصير جداً،
// يمكنه إرجاع IP عام هنا وIP داخلي عند الاتصال الفعلي، فيتجاوز الحماية
// بالكامل رغم نجاح هذا التحقق. **استخدم SafeFetch بدل هذه الدالة+طلب
// منفصل لأي مسار يجلب رابطاً من مستخدم فعلياً** — هذه الدالة أُبقيت للتوافق
// وللاستخدامات التي تريد فحصاً بلا تنفيذ طلب (مثلاً رسائل خطأ مبكرة في واجهة).
func ValidatePublicURL(rawURL string) error {
	return ValidatePublicURLContext(context.Background(), rawURL)
}

// ValidatePublicURLContext مثل ValidatePublicURL لكنها تحترم مهلة/إلغاء
// ctx أثناء تحليل DNS (مفيدة حين يكون لدى المستدعي بالفعل مهلة محددة
// ولا يريد أن يتجاوزها فحص الرابط نفسه).
func ValidatePublicURLContext(ctx context.Context, rawURL string) error {
	_, _, err := resolvePublicHost(ctx, rawURL)
	return err
}

// SafeFetch ينفّذ GET آمن ضد SSRF *و* DNS rebinding معاً: يحلّ الرابط
// ويتحقق منه عبر resolvePublicHost، ثم يُجبر الاتصال الفعلي على العنوان
// نفسه الذي تحقق منه (بدل ترك net/http يعيد حلّ DNS بشكل مستقل وقت
// الاتصال) — يُغلق بذلك فجوة "تحقّق ثم استخدام" (TOCTOU) الموجودة في نمط
// ValidatePublicURL()+http.Client.Do() القديم.
//
// maxBytes: حد أقصى لحجم جسم الاستجابة (0 = بلا حد، غير مستحسن لمرفقات من
// مستخدمين). يُعاد خطأ واضح لو تجاوزت الاستجابة الحد.
func SafeFetch(ctx context.Context, client *http.Client, rawURL string, maxBytes int64) ([]byte, error) {
	resp, err := SafeFetchStream(ctx, client, rawURL)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	return readWithLimit(resp.Body, maxBytes)
}

// SafeFetchStream يقوم بنفس التحقق والتثبيت (pinning) اللذين تقوم بهما
// SafeFetch، لكنه يُعيد *http.Response مباشرة بدل قراءة الجسم بالكامل في
// الذاكرة. مخصّص للحالات التي يجب فيها بث الاستجابة إلى القرص (مثل تحميل
// فيديو كبير) مع فرض حد الحجم لاحقاً عبر io.LimitReader على resp.Body —
// راجع plugins/sub لمثال. المستدعي مسؤول عن إغلاق resp.Body.
func SafeFetchStream(ctx context.Context, client *http.Client, rawURL string) (*http.Response, error) {
	u, pinnedIP, err := resolvePublicHost(ctx, rawURL)
	if err != nil {
		return nil, fmt.Errorf("رابط المرفق مرفوض: %w", err)
	}

	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	originalHostport := net.JoinHostPort(u.Hostname(), port)
	pinnedHostport := net.JoinHostPort(pinnedIP.String(), port)

	// ننسخ الـ Transport الأصلي (أو الافتراضي) ونستبدل DialContext فقط —
	// بقية الإعدادات (TLS, keep-alive, ...) تبقى كما هي.
	var base *http.Transport
	if t, ok := client.Transport.(*http.Transport); ok && t != nil {
		base = t.Clone()
	} else {
		base = http.DefaultTransport.(*http.Transport).Clone()
	}
	dialer := &net.Dialer{}
	base.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
		if addr == originalHostport {
			addr = pinnedHostport
		}
		return dialer.DialContext(ctx, network, addr)
	}

	pinnedClient := &http.Client{
		Transport: base,
		Timeout:   client.Timeout,
		Jar:       client.Jar,
		// نرفض أي إعادة توجيه (HTTP redirect) دائماً، بصرف النظر عمّا
		// كان مضبوطاً في client.CheckRedirect الأصلي: حتى لو كان الرابط
		// الأصلي عاماً وصالحاً تماماً، يمكن لخادم بعيد (خبيث أو مُخترَق)
		// أن يرد بـ 3xx يشير لعنوان داخلي، وDialContext المُعدَّل أعلاه
		// يطابق originalHostport فقط — أي وجهة إعادة توجيه مختلفة تمر
		// دون تثبيت (pin) ودون أي تحقق SSRF إضافي، فتفتح نفس فئة الثغرة
		// عملياً عبر مسار مختلف تماماً. إن احتجت دعم إعادة التوجيه
		// مستقبلاً، شغّل SafeFetch من جديد على رابط الوجهة (من خطأ
		// ErrUseLastResponse أو مماثل)، لا تتبعه تلقائياً.
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return fmt.Errorf("إعادة التوجيه غير مسموحة لروابط تحقّق منها netguard (وجهة: %s)", req.URL)
		},
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")

	resp, err := pinnedClient.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		resp.Body.Close()
		return nil, fmt.Errorf("fetch failed: status %d", resp.StatusCode)
	}

	return resp, nil
}

// readWithLimit يقرأ من r حتى maxBytes بايت (0 = بلا حد)، ويُعيد خطأً
// واضحاً لو تجاوزت البيانات الحد بدل قراءة غير محدودة بالكامل في الذاكرة
// أولاً. مستخرجة كدالة مستقلة لتكون قابلة للاختبار بمعزل عن الشبكة.
func readWithLimit(r io.Reader, maxBytes int64) ([]byte, error) {
	var reader io.Reader = r
	if maxBytes > 0 {
		reader = io.LimitReader(r, maxBytes+1)
	}
	raw, err := io.ReadAll(reader)
	if err != nil {
		return nil, err
	}
	if maxBytes > 0 && int64(len(raw)) > maxBytes {
		return nil, fmt.Errorf("حجم المرفق يتجاوز الحد المسموح (%d بايت)", maxBytes)
	}
	return raw, nil
}

func isPublicIP(ip net.IP) bool {
	if ip.IsLoopback() || ip.IsUnspecified() || ip.IsLinkLocalUnicast() ||
		ip.IsLinkLocalMulticast() || ip.IsMulticast() || ip.IsPrivate() {
		return false
	}
	// 169.254.169.254 وما شابه مغطاة أصلاً بـ IsLinkLocalUnicast، لكن نتحقق
	// صراحة لمزيد من الوضوح لأنه المسار الأشهر لسرقة بيانات اعتماد السحابة.
	if ip4 := ip.To4(); ip4 != nil && ip4[0] == 169 && ip4[1] == 254 {
		return false
	}
	return true
}
