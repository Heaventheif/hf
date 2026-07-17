package fb

import "testing"

func TestExtractBestVideoURL(t *testing.T) {
	cases := []struct {
		name string
		html string
		want string
	}{
		{
			name: "hd url",
			html: `{"foo":1,"browser_native_hd_url":"https:\/\/video.xx.fbcdn.net\/v\/abc.mp4?token=1","bar":2}`,
			want: `https:\/\/video.xx.fbcdn.net\/v\/abc.mp4?token=1`,
		},
		{
			name: "falls back to sd when hd absent",
			html: `{"browser_native_sd_url":"https://video.xx.fbcdn.net/v/sd.mp4"}`,
			want: `https://video.xx.fbcdn.net/v/sd.mp4`,
		},
		{
			name: "protocol-relative src",
			html: `{"src":"//video.xx.fbcdn.net/v/rel.mp4"}`,
			want: `https://video.xx.fbcdn.net/v/rel.mp4`,
		},
		{
			name: "no known field present",
			html: `<html><body>login required</body></html>`,
			want: "",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := extractBestVideoURL(c.html)
			if got != c.want {
				t.Errorf("extractBestVideoURL() = %q, want %q", got, c.want)
			}
		})
	}
}

func TestCleanURL(t *testing.T) {
	raw := `https:\/\/video.xx.fbcdn.net\/v\/abc.mp4?a=1\u0026b=2\u003d3`
	want := `https://video.xx.fbcdn.net/v/abc.mp4?a=1&b=2=3`
	if got := cleanURL(raw); got != want {
		t.Errorf("cleanURL() = %q, want %q", got, want)
	}
}

func TestIsFacebookHost(t *testing.T) {
	allowed := []string{"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com", "fb.watch"}
	for _, h := range allowed {
		if !isFacebookHost(h) {
			t.Errorf("isFacebookHost(%q) = false, want true", h)
		}
	}

	rejected := []string{"evil.com", "facebook.com.evil.com", "notfacebook.com", "l.facebook.com.attacker.net", ""}
	for _, h := range rejected {
		if isFacebookHost(h) {
			t.Errorf("isFacebookHost(%q) = true, want false", h)
		}
	}
}

func TestBuildCookieHeader(t *testing.T) {
	cookies := []BrowserCookie{{Name: "c_user", Value: "123"}, {Name: "xs", Value: "abc"}}
	want := "c_user=123; xs=abc"
	if got := buildCookieHeader(cookies); got != want {
		t.Errorf("buildCookieHeader() = %q, want %q", got, want)
	}

	if got := buildCookieHeader(nil); got != "" {
		t.Errorf("buildCookieHeader(nil) = %q, want empty string", got)
	}
}
