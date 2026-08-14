package pinterest

import "testing"

// TestOriginalFromSrcset_PrefersExplicitOriginals يثبت أن رابط originals/
// الصريح داخل srcset يُفضَّل دائماً حتى لو وُجدت عناصر أخرى بمعامل Nx
// أعلى ظاهرياً — نفس أولوية originalFromSrcset في pinterest_scraper.js
// السابق بالضبط.
func TestOriginalFromSrcset_PrefersExplicitOriginals(t *testing.T) {
	srcset := "https://i.pinimg.com/236x/aa/bb/cc.jpg 1x, " +
		"https://i.pinimg.com/originals/aa/bb/cc.jpg 4x, " +
		"https://i.pinimg.com/474x/aa/bb/cc.jpg 2x"
	got := originalFromSrcset(srcset)
	want := "https://i.pinimg.com/originals/aa/bb/cc.jpg"
	if got != want {
		t.Fatalf("originalFromSrcset() = %q, want %q", got, want)
	}
}

// TestOriginalFromSrcset_PicksHighestScale يثبت أنه بلا originals/ صريح،
// يُختار أعلى معامل مقياس (Nx) من عناصر srcset.
func TestOriginalFromSrcset_PicksHighestScale(t *testing.T) {
	srcset := "https://i.pinimg.com/236x/a.jpg 1x, https://i.pinimg.com/736x/a.jpg 3x, https://i.pinimg.com/474x/a.jpg 2x"
	got := originalFromSrcset(srcset)
	want := "https://i.pinimg.com/736x/a.jpg"
	if got != want {
		t.Fatalf("originalFromSrcset() = %q, want %q", got, want)
	}
}

func TestOriginalFromSrcset_Empty(t *testing.T) {
	if got := originalFromSrcset(""); got != "" {
		t.Fatalf("originalFromSrcset(\"\") = %q, want empty string", got)
	}
	if got := originalFromSrcset("not a valid srcset at all"); got != "" {
		t.Fatalf("originalFromSrcset(garbage) = %q, want empty string", got)
	}
}

func TestConvertQuality_OriginalIsNoop(t *testing.T) {
	u := "https://i.pinimg.com/736x/a/b/c.jpg"
	if got := convertQuality(u, "original"); got != u {
		t.Fatalf("convertQuality(original) = %q, want unchanged %q", got, u)
	}
}

func TestConvertQuality_RewritesSizeSegment(t *testing.T) {
	u := "https://i.pinimg.com/736x/a/b/c.jpg"
	got := convertQuality(u, "474x")
	want := "https://i.pinimg.com/474x/a/b/c.jpg"
	if got != want {
		t.Fatalf("convertQuality(474x) = %q, want %q", got, want)
	}
}

func TestPinFromRaw_PrefersSrcsetOriginalOverThumbRewrite(t *testing.T) {
	p := rawPin{
		ID:          "123",
		URL:         "https://www.pinterest.com/pin/123/",
		Title:       "  عنوان  ",
		ImageSrc:    "https://i.pinimg.com/236x/thumb.jpg",
		ImageSrcset: "https://i.pinimg.com/originals/thumb.jpg 2x",
	}
	got := pinFromRaw(p, "original")
	if got == nil {
		t.Fatal("pinFromRaw() = nil, want non-nil")
	}
	if got.URL != "https://i.pinimg.com/originals/thumb.jpg" {
		t.Errorf("URL = %q, want originals/ URL from srcset", got.URL)
	}
	if got.ID != "123" || got.SourceURL != p.URL || got.Thumbnail != p.ImageSrc {
		t.Errorf("unexpected mapping: %+v", got)
	}
}

func TestPinFromRaw_FallsBackToThumbRewrite(t *testing.T) {
	p := rawPin{ID: "456", ImageSrc: "https://i.pinimg.com/236x/thumb.jpg"}
	got := pinFromRaw(p, "original")
	if got == nil {
		t.Fatal("pinFromRaw() = nil, want non-nil")
	}
	want := "https://i.pinimg.com/originals/thumb.jpg"
	if got.URL != want {
		t.Errorf("URL = %q, want %q (236x/ rewritten to originals/)", got.URL, want)
	}
}

func TestPinFromRaw_NoImageReturnsNil(t *testing.T) {
	if got := pinFromRaw(rawPin{ID: "789"}, "original"); got != nil {
		t.Fatalf("pinFromRaw() with no image = %+v, want nil", got)
	}
}
