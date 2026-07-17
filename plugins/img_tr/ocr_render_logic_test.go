package img_tr

import (
	"sort"
	"testing"
)

// TestResolveTesseractLangs_AlwaysIncludesArabicAndEnglish يثبت أن 'ara'
// و'eng' حاضرتان دائماً بغض النظر عن source_lang — نفس ضمان
// resolveTesseractLangs في img_tr_engine.js السابق بالضبط (SFX إنجليزية
// شائعة حتى في فصول غير إنجليزية، والهدف النهائي عربي دائماً).
func TestResolveTesseractLangs_AlwaysIncludesArabicAndEnglish(t *testing.T) {
	for _, src := range []string{"ja", "ko", "fr", "", "unknown-lang"} {
		got := resolveTesseractLangs(src)
		has := func(l string) bool {
			for _, g := range got {
				if g == l {
					return true
				}
			}
			return false
		}
		if !has("ara") {
			t.Errorf("resolveTesseractLangs(%q) = %v, missing 'ara'", src, got)
		}
		if !has("eng") {
			t.Errorf("resolveTesseractLangs(%q) = %v, missing 'eng'", src, got)
		}
	}
}

func TestResolveTesseractLangs_MapsKnownSourceLang(t *testing.T) {
	got := resolveTesseractLangs("ja")
	sort.Strings(got)
	want := []string{"ara", "eng", "jpn"}
	sort.Strings(want)
	if len(got) != len(want) {
		t.Fatalf("resolveTesseractLangs(ja) = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("resolveTesseractLangs(ja) = %v, want %v", got, want)
		}
	}
}

func TestResolveTesseractLangs_UnknownFallsBackToEnglishOnly(t *testing.T) {
	// لغة غير معروفة يجب أن تُعامل كأنها إنجليزي (لا يُضاف رمز ثالث خاطئ)
	// — يعني المجموعة الناتجة يجب أن تكون بالضبط {ara, eng}.
	got := resolveTesseractLangs("xx-not-a-real-lang")
	if len(got) != 2 {
		t.Fatalf("resolveTesseractLangs(unknown) = %v, want exactly 2 langs (ara, eng)", got)
	}
}

func TestVisualOrderRTL_ReversesRuneOrder(t *testing.T) {
	in := "مرحبا"
	got := visualOrderRTL(in)
	inRunes := []rune(in)
	gotRunes := []rune(got)
	if len(gotRunes) != len(inRunes) {
		t.Fatalf("visualOrderRTL length changed: got %d runes, want %d", len(gotRunes), len(inRunes))
	}
	for i := range inRunes {
		if gotRunes[i] != inRunes[len(inRunes)-1-i] {
			t.Fatalf("visualOrderRTL(%q) = %q, not a pure rune-order reversal", in, got)
		}
	}
}

func TestVisualOrderRTL_EmptyAndSingleRune(t *testing.T) {
	if got := visualOrderRTL(""); got != "" {
		t.Errorf("visualOrderRTL(\"\") = %q, want empty", got)
	}
	if got := visualOrderRTL("ا"); got != "ا" {
		t.Errorf("visualOrderRTL(single rune) = %q, want unchanged", got)
	}
}
