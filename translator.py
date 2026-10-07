"""NLLB-200-distilled-600M (CTranslate2 int8) → العربية الفصحى (arb_Arab)."""
import logging
import threading

import ctranslate2
from tokenizers import Tokenizer

import config as C

log = logging.getLogger("ytdub.mt")

# رموز Whisper → رموز FLORES-200
LANG_MAP = {
    "en": "eng_Latn", "fr": "fra_Latn", "es": "spa_Latn", "de": "deu_Latn", "it": "ita_Latn",
    "pt": "por_Latn", "ru": "rus_Cyrl", "tr": "tur_Latn", "zh": "zho_Hans", "ja": "jpn_Jpan",
    "ko": "kor_Hang", "hi": "hin_Deva", "ur": "urd_Arab", "fa": "pes_Arab", "nl": "nld_Latn",
    "pl": "pol_Latn", "id": "ind_Latn", "uk": "ukr_Cyrl", "sv": "swe_Latn", "vi": "vie_Latn",
    "th": "tha_Thai", "he": "heb_Hebr", "cs": "ces_Latn", "ro": "ron_Latn", "el": "ell_Grek",
    "hu": "hun_Latn", "bn": "ben_Beng", "ms": "zsm_Latn", "fi": "fin_Latn", "da": "dan_Latn",
    "no": "nob_Latn", "bg": "bul_Cyrl", "ca": "cat_Latn", "sr": "srp_Cyrl", "hr": "hrv_Latn",
}
TARGETS = {"ar": "arb_Arab"}


class UnsupportedLanguage(Exception):
    pass


class Translator:
    def __init__(self, device: str = "cpu"):
        if not (C.NLLB_DIR / "model.bin").exists():
            raise RuntimeError("نموذج NLLB غير موجود في " + str(C.NLLB_DIR))
        ctype = "float16" if device == "cuda" else "int8"
        self.tr = ctranslate2.Translator(
            str(C.NLLB_DIR), device=device, compute_type=ctype,
            inter_threads=1, intra_threads=C.CPU_THREADS)
        self.tok = Tokenizer.from_file(str(C.NLLB_DIR / "tokenizer.json"))
        self._lock = threading.Lock()
        log.info("loaded NLLB (%s/%s)", device, ctype)

    def translate(self, text: str, src: str, tgt: str = "ar", beam: int = 2) -> str:
        src_code = LANG_MAP.get(src)
        tgt_code = TARGETS.get(tgt)
        if not src_code:
            raise UnsupportedLanguage(src)
        if not tgt_code:
            raise UnsupportedLanguage(tgt)
        pieces = self.tok.encode(text, add_special_tokens=False).tokens[:400]
        source = [src_code] + pieces + ["</s>"]
        with self._lock:
            res = self.tr.translate_batch(
                [source], target_prefix=[[tgt_code]], beam_size=beam,
                max_decoding_length=int(len(pieces) * 2 + 16), no_repeat_ngram_size=4)
        out = res[0].hypotheses[0][1:]                       # أول رمز هو رمز اللغة
        ids = [i for i in (self.tok.token_to_id(t) for t in out) if i is not None]
        return self.tok.decode(ids, skip_special_tokens=True).strip()
