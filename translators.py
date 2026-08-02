from typing import Optional
import re

from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

MODEL_CANDIDATES = [
    "facebook/nllb-200-distilled-600M",
    "facebook/nllb-200-3.3B",
]

# FIX: الكود الصحيح في NLLB هو "arb_Arab" وليس "arb"
AR_LANG_CODE = "arb_Arab"

_tokenizer = None
_model = None

def _load_model():
    global _tokenizer, _model
    last_err = None
    for name in MODEL_CANDIDATES:
        try:
            _tokenizer = AutoTokenizer.from_pretrained(name)
            _model = AutoModelForSeq2SeqLM.from_pretrained(name)
            print(f"[translators] Loaded model: {name}")
            return
        except Exception as e:
            last_err = e
            print(f"[translators] Failed to load {name}: {e}")
    raise RuntimeError(f"Failed to load any translation model: {last_err}")

def _lang_to_id(lang_code: str) -> Optional[int]:
    if _tokenizer is None:
        return None
    # NLLB tokenizers يستخدمون lang_code_to_id dict
    if hasattr(_tokenizer, "lang_code_to_id") and lang_code in _tokenizer.lang_code_to_id:
        return _tokenizer.lang_code_to_id[lang_code]
    # بديل: convert_tokens_to_ids
    token_id = _tokenizer.convert_tokens_to_ids(lang_code)
    if token_id != _tokenizer.unk_token_id:
        return token_id
    return None

def translate_to_ar(text: str, src_lang: Optional[str] = None) -> str:
    if not text or not text.strip():
        return ""

    global _tokenizer, _model
    if _tokenizer is None or _model is None:
        _load_model()

    t = re.sub(r"[ \t]+", " ", text).strip()

    # FIX: NLLB يحتاج تعيين src_lang في الـ tokenizer قبل الترميز
    if src_lang:
        _tokenizer.src_lang = src_lang

    inputs = _tokenizer(t, return_tensors="pt", truncation=True, max_length=512)

    gen_kwargs: dict = dict(
        max_new_tokens=256,   # FIX: max_new_tokens أصح من max_length مع generate()
        num_beams=4,
        do_sample=False,
    )

    target_id = _lang_to_id(AR_LANG_CODE)
    if target_id is not None:
        gen_kwargs["forced_bos_token_id"] = target_id

    out = _model.generate(**inputs, **gen_kwargs)
    translated = _tokenizer.batch_decode(out, skip_special_tokens=True)[0]
    return translated.strip()
