// Package fdown يستخرج روابط تحميل مباشرة (SD/HD) لفيديو فيسبوك عبر
// خدمة fdown.net الخارجية — plugin منفصل ومستقل تماماً عن plugins/fb
// (الذي يستخرج الرابط مباشرة من صفحة فيسبوك نفسها بلا وسيط خارجي).
// الاثنان يتعايشان عمداً بلا اعتماد أحدهما على الآخر: كل واحد نقطة
// استخراج مختلفة، فلو واحد فشل (تغيّرت بنية fdown.net، أو بنية صفحة
// فيسبوك) يبقى التاني مسار بديل مستقل بالكامل لصاحب البوت يختار بينهم.
//
// ملاحظة صادقة عن هشاشة هذا الـ plugin تحديداً (بخلاف plugins/fb): بيعتمد
// كلياً على موقع طرف ثالث (fdown.net) خارج سيطرتنا — لا بنيته HTML مضمونة
// الثبات، ولا حتى توفّره أصلاً مضمون. لو توقف الاستخراج فجأة، أول خطوة
// تشخيص هي فتح fdown.net يدوياً بمتصفح والتأكد إنه لسه شغال وبنفس
// الـ IDs المستخدَمة هنا (#sdlink, #hdlink).
package fdown

import (
	"context"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"strings"
	"time"

	"github.com/PuerkitoBio/goquery"

	"sunkenbot/internal/httpx"
	"sunkenbot/internal/plugins"
)

const Description = "استخراج روابط تحميل مباشرة (SD/HD) لفيديو فيسبوك عبر fdown.net"

// Service يغلّف Client بعقد plugins.Service المطلوب من main.go.
type Service struct {
	client *Client
}

// New ينشئ خدمة fdown جديدة. لا تأخذ *http.Client من main.go كباقي
// الخدمات عمداً: Client الداخلي هنا يبني http.Client خاصاً به بإعدادات
// محددة (Transport بحدود اتصال صريحة) غير قابلة للتهيئة من الخارج حالياً
// — نفس نمط chess.New()/mangabridge.New()/ping.New() اللي بتاخدش عميل.
func New() *Service {
	return &Service{client: NewClient()}
}

func (s *Service) Name() string { return "fdown" }

func (s *Service) Routes() []plugins.Route {
	return []plugins.Route{
		{Method: "POST", Pattern: "/fdown", Handler: httpx.WrapJSON(s.handleGetLinks)},
	}
}

// ─── أنواع الطلب/الرد ──────────────────────────────────────────────

type linksRequest struct {
	URL string `json:"url"`
}

// linksResult تطابق DownloadResult حرفياً — إعادة تعريف بسيطة هنا لإبقاء
// الحدود الخارجية للـ plugin (JSON) منفصلة صراحةً عن نوع القيمة الداخلي
// لعميل fdown نفسه، ولو كانا متطابقين شكلياً الآن.
type linksResult struct {
	SDLink string `json:"sd_link,omitempty"`
	HDLink string `json:"hd_link,omitempty"`
	Title  string `json:"title,omitempty"`
}

// isFacebookHost نفس التقييد المستخدَم في plugins/fb — يمنع استخدام هذا
// الـ plugin كأداة جلب عامة (open proxy) لأي رابط غير فيسبوك يُمرَّر
// لـ fdown.net.
func isFacebookHost(host string) bool {
	host = strings.ToLower(host)
	return host == "facebook.com" || strings.HasSuffix(host, ".facebook.com") || host == "fb.watch"
}

func (s *Service) handleGetLinks(ctx context.Context, req linksRequest) (linksResult, error) {
	url := strings.TrimSpace(req.URL)
	if url == "" {
		return linksResult{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]string{"error": "url مطلوب"},
		}
	}
	parsed, err := neturl.Parse(url)
	if err != nil || !isFacebookHost(parsed.Hostname()) {
		return linksResult{}, &httpx.HTTPError{
			Code: http.StatusBadRequest,
			Body: map[string]string{"error": "الرابط يجب أن يكون من نطاق facebook.com أو fb.watch"},
		}
	}

	result, err := s.client.GetDownloadURL(ctx, url)
	if err != nil {
		return linksResult{}, &httpx.HTTPError{
			Code: http.StatusBadGateway,
			Body: map[string]string{"error": err.Error()},
		}
	}

	return linksResult{SDLink: result.SDLink, HDLink: result.HDLink, Title: result.Title}, nil
}

// ─── عميل fdown.net (نفس منطق الملف الأصلي المرفوع، بلا rate limiter) ─

// Client هو عميل لخدمة fdown.net.
type Client struct {
	httpClient *http.Client
}

// NewClient ينشئ عميلاً جديداً.
func NewClient() *Client {
	return &Client{
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
			Transport: &http.Transport{
				MaxIdleConns:    10,
				IdleConnTimeout: 30 * time.Second,
			},
		},
	}
}

// GetDownloadURL يستقبل رابط فيديو فيسبوك ويعيد روابط التحميل (SD و HD).
func (c *Client) GetDownloadURL(ctx context.Context, videoURL string) (*DownloadResult, error) {
	data := neturl.Values{}
	data.Set("URLz", videoURL)

	req, err := http.NewRequestWithContext(ctx, "POST", "https://fdown.net/download.php", strings.NewReader(data.Encode()))
	if err != nil {
		return nil, fmt.Errorf("فشل إنشاء الطلب: %w", err)
	}

	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("User-Agent", c.getRandomUserAgent())
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8")
	req.Header.Set("Accept-Language", "en-US,en;q=0.9")
	req.Header.Set("Accept-Encoding", "gzip, deflate, br")
	req.Header.Set("Connection", "keep-alive")
	req.Header.Set("Sec-Fetch-Dest", "document")
	req.Header.Set("Sec-Fetch-Mode", "navigate")
	req.Header.Set("Sec-Fetch-Site", "same-origin")
	req.Header.Set("Sec-Fetch-User", "?1")
	req.Header.Set("Upgrade-Insecure-Requests", "1")
	req.Header.Set("Referer", "https://fdown.net/")

	// تأخير عشوائي (محاكاة سلوك بشري) — يحترم إلغاء الـ context بدل
	// time.Sleep الخام، حتى لا يبقى الـ handler محجوزاً كاملاً لو ألغى
	// العميل الطلب أو انتهت مهلته أثناء الانتظار.
	select {
	case <-time.After(c.getRandomDelay()):
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("فشل الاتصال بـ fdown: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("الموقع أعاد حالة غير متوقعة: %d", resp.StatusCode)
	}

	return c.parseResponse(resp.Body)
}

// DownloadResult يحتوي على روابط التحميل.
type DownloadResult struct {
	SDLink string `json:"sd_link,omitempty"`
	HDLink string `json:"hd_link,omitempty"`
	Title  string `json:"title,omitempty"`
}

// parseResponse يستخرج الروابط من HTML.
func (c *Client) parseResponse(body io.Reader) (*DownloadResult, error) {
	doc, err := goquery.NewDocumentFromReader(body)
	if err != nil {
		return nil, fmt.Errorf("فشل تحليل HTML: %w", err)
	}

	result := &DownloadResult{}

	doc.Find("a#sdlink").Each(func(i int, s *goquery.Selection) {
		if href, exists := s.Attr("href"); exists && href != "" {
			result.SDLink = href
		}
	})

	doc.Find("a#hdlink").Each(func(i int, s *goquery.Selection) {
		if href, exists := s.Attr("href"); exists && href != "" {
			result.HDLink = href
		}
	})

	doc.Find(".lib-header").Each(func(i int, s *goquery.Selection) {
		text := strings.TrimSpace(s.Text())
		if text != "" && !strings.Contains(text, "No video title") {
			result.Title = text
		}
	})

	// إذا لم نجد باستخدام ID، نبحث باستخدام أنماط أخرى (احتياطي).
	if result.SDLink == "" && result.HDLink == "" {
		doc.Find("a[href*='.mp4']").Each(func(i int, s *goquery.Selection) {
			if href, exists := s.Attr("href"); exists && href != "" {
				if result.SDLink == "" {
					result.SDLink = href
				} else if result.HDLink == "" && href != result.SDLink {
					result.HDLink = href
				}
			}
		})
	}

	if result.SDLink == "" && result.HDLink == "" {
		return nil, fmt.Errorf("لم يتم العثور على روابط تحميل. قد يكون الرابط غير صحيح أو الفيديو خاص")
	}

	return result, nil
}

// getRandomUserAgent يعيد User-Agent عشوائي لمتصفحات حديثة.
func (c *Client) getRandomUserAgent() string {
	userAgents := []string{
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
		"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
		"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
		"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
		"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
	}
	return userAgents[time.Now().UnixNano()%int64(len(userAgents))]
}

// getRandomDelay يعيد تأخير عشوائي بين 5 و 10 ثوانٍ.
func (c *Client) getRandomDelay() time.Duration {
	base := 5 * time.Second
	jitter := time.Duration(time.Now().UnixNano()%5000) * time.Millisecond
	return base + jitter
}
