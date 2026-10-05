from __future__ import annotations

from pathlib import Path

import ctranslate2
from transformers import AutoTokenizer


class Translator:
    def __init__(self, model_dir: Path):
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
        self.engine = ctranslate2.Translator(str(model_dir), device="cpu", compute_type="int8")
        self.lang_map = {
            "en": "eng_Latn", "fr": "fra_Latn", "de": "deu_Latn", "es": "spa_Latn",
            "it": "ita_Latn", "pt": "por_Latn", "tr": "tur_Latn", "ru": "rus_Cyrl",
            "ja": "jpn_Jpan", "ko": "kor_Hang", "zh": "zho_Hans", "ar": "arb_Arab",
        }

    def _code(self, value: str) -> str:
        value = (value or "").replace("-", "_")
        return self.lang_map.get(value, value if "_" in value else "eng_Latn")

    def _language_token_id(self, code: str) -> int:
        """Get an NLLB language token from both slow and fast tokenizers."""
        language_map = getattr(self.tokenizer, "lang_code_to_id", None)
        if language_map and code in language_map:
            return int(language_map[code])
        token_id = self.tokenizer.convert_tokens_to_ids(code)
        if isinstance(token_id, list):
            token_id = token_id[0]
        if token_id is None or token_id == self.tokenizer.unk_token_id:
            token_id = self.tokenizer.convert_tokens_to_ids(f"__{code}__")
        if isinstance(token_id, list):
            token_id = token_id[0]
        if token_id is None or token_id == self.tokenizer.unk_token_id:
            raise ValueError(f"NLLB language token is missing: {code}")
        return int(token_id)

    def translate(self, text: str, src: str, tgt: str = "ar") -> str:
        if not text.strip():
            return ""
        src_code, tgt_code = self._code(src), self._code(tgt)
        self.tokenizer.src_lang = src_code
        encoded = self.tokenizer(text, return_tensors="np", add_special_tokens=True)
        tokens = self.tokenizer.convert_ids_to_tokens(encoded["input_ids"][0].tolist())
        target_token = self.tokenizer.convert_ids_to_tokens([self._language_token_id(tgt_code)])
        result = self.engine.translate_batch([tokens], target_prefix=[target_token], beam_size=1)[0]
        output_tokens = result.hypotheses[0]
        ids = self.tokenizer.convert_tokens_to_ids(output_tokens)
        return self.tokenizer.decode(ids, skip_special_tokens=True).strip()
