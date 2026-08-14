// internal/pyclient/pyclient.go
//
// Client داخلي يتصل بخدمة Python (s) من Go (g).
// يُضاف هذا الملف داخل Go project في: internal/pyclient/pyclient.go
//
// القاعدة الواحدة:
//   - Render يتصل بـ Go فقط (HF_SPACE_URL + INTERNAL_TOKEN)
//   - Go يتصل بـ Python عبر هذا الـ client (HF_PYTHON_URL + نفس INTERNAL_TOKEN)
//   - Python لا يتصل بأحد
//
// الاستخدام في أي plugin Go:
//
//   import "sunkenbot/internal/pyclient"
//
//   client := pyclient.New(httpClient)  // أو pyclient.Default()
//   result, err := client.Post(ctx, "/ocr/infer", reqBody, &respBody)
package pyclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
)

// Client يحمل http.Client والـ base URL والتوكن.
// أنشئه مرة واحدة في main.go وأمرره كاعتمادية صريحة للـ plugins التي تحتاجه.
type Client struct {
	http    *http.Client
	baseURL string
	token   string
}

// New ينشئ Client جديداً.
// baseURL: قيمة HF_PYTHON_URL (بدون trailing slash).
// token:   قيمة INTERNAL_TOKEN (نفس التوكن الذي يحمي Go).
func New(httpClient *http.Client, baseURL, token string) *Client {
	return &Client{
		http:    httpClient,
		baseURL: strings.TrimRight(baseURL, "/"),
		token:   token,
	}
}

// FromEnv ينشئ Client من متغيرات البيئة تلقائياً.
// يعيد (nil, error) إن لم يوجد HF_PYTHON_URL.
func FromEnv(httpClient *http.Client) (*Client, error) {
	base := strings.TrimSpace(os.Getenv("HF_PYTHON_URL"))
	if base == "" {
		return nil, fmt.Errorf("HF_PYTHON_URL غير مضبوط في متغيرات البيئة")
	}
	token := os.Getenv("INTERNAL_TOKEN")
	return New(httpClient, base, token), nil
}

// Post يرسل POST JSON لـ Python ويفك تشفير الرد في dst.
// dst: مؤشر لبنية الرد المتوقعة — يُملَأ تلقائياً.
func (c *Client) Post(ctx context.Context, path string, body any, dst any) error {
	data, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("pyclient: marshal: %w", err)
	}

	url := c.baseURL + path
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(data))
	if err != nil {
		return fmt.Errorf("pyclient: new request: %w", err)
	}

	req.Header.Set("Content-Type", "application/json")
	if c.token != "" {
		req.Header.Set("X-Internal-Token", c.token)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("pyclient: do request: %w", err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("pyclient: read body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("pyclient: Python رد بـ %d: %s", resp.StatusCode, truncate(string(raw), 200))
	}

	if dst != nil {
		if err := json.Unmarshal(raw, dst); err != nil {
			return fmt.Errorf("pyclient: unmarshal: %w", err)
		}
	}
	return nil
}

// Get يرسل GET لـ Python ويفك تشفير الرد في dst.
func (c *Client) Get(ctx context.Context, path string, dst any) error {
	url := c.baseURL + path
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("pyclient: new request: %w", err)
	}
	if c.token != "" {
		req.Header.Set("X-Internal-Token", c.token)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("pyclient: do request: %w", err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("pyclient: read body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("pyclient: Python رد بـ %d: %s", resp.StatusCode, truncate(string(raw), 200))
	}

	if dst != nil {
		if err := json.Unmarshal(raw, dst); err != nil {
			return fmt.Errorf("pyclient: unmarshal: %w", err)
		}
	}
	return nil
}

// Delegator هو عقد الواجهة الذي يستهلكه internal/delegate (راجع delegate.go).
// Forward يعيد إرسال طلب كامل (method + path + جسم خام + content-type) إلى
// Python ويعيد الاستجابة كاملة (status + body bytes) دون أي تفسير — هذا ما
// يسمح بتمرير multipart/form-data وأي جسم آخر حرفياً كما وصل.
type Delegator interface {
	Forward(ctx context.Context, method, path string, body io.Reader, contentType string) (status int, data []byte, err error)
}

// Forward يرسل طلباً خاماً إلى Python ويعيد الاستجابة كاملة.
// body قد تكون nil (لطلبات GET/DELETE بلا جسم). contentType يُمرَّر كما هو
// (مثلاً "multipart/form-data; boundary=...") أو فارغاً لطلبات JSON التي
// يُعاد تركيبها هنا.
func (c *Client) Forward(ctx context.Context, method, path string, body io.Reader, contentType string) (int, []byte, error) {
	url := c.baseURL + path
	if body == nil {
		body = http.NoBody
	}
	req, err := http.NewRequestWithContext(ctx, method, url, body)
	if err != nil {
		return 0, nil, fmt.Errorf("pyclient: new request: %w", err)
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if c.token != "" {
		req.Header.Set("X-Internal-Token", c.token)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, nil, fmt.Errorf("pyclient: do request: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return 0, nil, fmt.Errorf("pyclient: read body: %w", err)
	}
	return resp.StatusCode, raw, nil
}

// Assert: *Client ترضي واجهة Delegator (فحص وقت الترجمة — لو أزيلت Forward
// أو تغير توقيعها يفشل البناء فوراً).
var _ Delegator = (*Client)(nil)

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
