package novel

import (
	"strings"
	"testing"
)

const sampleHTML = `<html><head><title>Chapter 12 - My Great Novel - FreeWebNovel</title></head>
<body>
<nav>Ads and nav junk that should be removed entirely from consideration</nav>
<div class="wrap">
<h1 class="tit">Chapter 12: The Awakening - FreeWebNovel</h1>
<div id="chapter-content">
<p>The morning sun rose slowly over the mountains, casting long shadows across the valley below.</p>
<p>advertisement</p>
<p>She had never seen anything quite like this before in her entire life, and it amazed her deeply.</p>
<p>cloudflare just a moment please wait</p>
<div class="ads">buy now click here</div>
<p>He whispered softly, "We must leave before the sun sets completely over these ancient hills."</p>
</div>
</div>
</body></html>`

func TestExtractParagraphs(t *testing.T) {
	paras := extract(sampleHTML, contentSelectors)
	if paras == nil {
		t.Fatal("expected paragraphs, got nil")
	}
	if len(paras) != 3 {
		t.Fatalf("expected 3 real paragraphs (ads/cloudflare filtered out), got %d: %v", len(paras), paras)
	}
	if !strings.Contains(paras[0], "morning sun rose") {
		t.Errorf("first paragraph unexpected: %q", paras[0])
	}
	for _, p := range paras {
		low := strings.ToLower(p)
		if strings.Contains(low, "advertisement") || strings.Contains(low, "cloudflare") {
			t.Errorf("filtered text leaked into paragraphs: %q", p)
		}
	}
}

func TestExtractTitle(t *testing.T) {
	title := extractTitle(sampleHTML)
	if !strings.Contains(title, "Chapter 12: The Awakening") {
		t.Errorf("unexpected title: %q", title)
	}
}

func TestSlugify(t *testing.T) {
	cases := map[string]string{
		"Lord of the Mysteries": "lord-of-the-mysteries",
		"Don't Fear the Reaper": "dont-fear-the-reaper",
		"  Trailing Spaces  ":   "trailing-spaces",
	}
	for in, want := range cases {
		if got := slugify(in); got != want {
			t.Errorf("slugify(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestIsFiltered(t *testing.T) {
	if !isFiltered("advertisement here") {
		t.Error("expected ad text to be filtered")
	}
	if isFiltered("This is a perfectly normal sentence of a story.") {
		t.Error("expected normal sentence to pass")
	}
}
