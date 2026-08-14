package mangabridge

import "testing"

func TestSlugify(t *testing.T) {
	cases := []struct{ in, want string }{
		{"One Piece", "one-piece"},
		{"  Attack On Titan  ", "attack-on-titan"},
		{"already-slugged", "already-slugged"},
		{"MULTIPLE   spaces", "multiple---spaces"},
		{"", ""},
	}
	for _, c := range cases {
		if got := slugify(c.in); got != c.want {
			t.Errorf("slugify(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
