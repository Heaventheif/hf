package comic

import (
	"reflect"
	"testing"
)

func TestExtractMangaLabel_Found(t *testing.T) {
	html := `<html><body>
		<div class="header">...</div>
		<div class="manga-widget" data-label="TheLastHero">series info here</div>
	</body></html>`

	label, ok := extractMangaLabel(html)
	if !ok {
		t.Fatal("expected label to be found")
	}
	if label != "TheLastHero" {
		t.Errorf("label = %q, want %q", label, "TheLastHero")
	}
}

func TestExtractMangaLabel_NotFound(t *testing.T) {
	html := `<html><body><div class="header">no widget here</div></body></html>`
	_, ok := extractMangaLabel(html)
	if ok {
		t.Fatal("expected label NOT to be found")
	}
}

func TestExtractChapterImages_SeparatorDivs(t *testing.T) {
	html := `<html><body>
		<div class="separator" style="clear: both;">
			<a href="..."><img src="https://blogger.googleusercontent.com/img/page1.jpg"
				data-original-width="900" data-original-height="1384" /></a>
		</div>
		<div class="separator" style="clear: both;">
			<a href="..."><img src="https://blogger.googleusercontent.com/img/page2.jpg"
				data-original-width="900" data-original-height="1384" /></a>
		</div>
		<img src="https://blogger.googleusercontent.com/img/dagruel-no-image-placeholder.png" />
		<img src="https://other-cdn.example.com/img/unrelated.jpg" data-original-width="900" data-original-height="1384" />
	</body></html>`

	images := extractChapterImages(html)
	want := []string{
		"https://blogger.googleusercontent.com/img/page1.jpg",
		"https://blogger.googleusercontent.com/img/page2.jpg",
	}
	if !reflect.DeepEqual(images, want) {
		t.Errorf("images = %v, want %v", images, want)
	}
}

func TestExtractChapterImages_WideNoDimensions(t *testing.T) {
	// صور بلوغر بلا data-original-width/height — تُعتبر صفحة (نفس isPageImage الحالية)
	html := `<div class="separator"><img src="https://blogger.googleusercontent.com/img/page1.jpg" /></div>`
	images := extractChapterImages(html)
	if len(images) != 1 || images[0] != "https://blogger.googleusercontent.com/img/page1.jpg" {
		t.Errorf("images = %v, want single blogger image with no dimensions", images)
	}
}

func TestExtractChapterImages_Dedup(t *testing.T) {
	html := `
		<img src="https://blogger.googleusercontent.com/img/page1.jpg" data-original-width="900" data-original-height="1384" />
		<img src="https://blogger.googleusercontent.com/img/page1.jpg" data-original-width="900" data-original-height="1384" />
	`
	images := extractChapterImages(html)
	if len(images) != 1 {
		t.Errorf("expected duplicate src to be deduplicated, got %v", images)
	}
}
