"""يُنفَّذ أثناء بناء الصورة (Dockerfile) فتُخبَّأ النماذج داخلها."""
import os
import shutil
import sys

from huggingface_hub import hf_hub_download, snapshot_download

import config as C


def whisper():
    for lvl in os.getenv("WHISPER_MODELS", "tiny base small").split():
        print(f"[dl] faster-whisper-{lvl}", flush=True)
        snapshot_download(f"Systran/faster-whisper-{lvl}", local_dir=C.MODELS_DIR / f"faster-whisper-{lvl}")


def nllb():
    print("[dl] NLLB", C.NLLB_REPO, flush=True)
    snapshot_download(C.NLLB_REPO, local_dir=C.NLLB_DIR)
    for f in ("model.bin", "tokenizer.json"):
        if not (C.NLLB_DIR / f).exists():
            sys.exit(f"ملف مفقود في نموذج NLLB: {f}. حوّل النموذج الرسمي بـ ct2-transformers-converter.")


def piper():
    C.VOICES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = C.MODELS_DIR / "_hf"
    for vid in os.getenv("PIPER_VOICES", "ar_JO-kareem-medium ar_JO-kareem-low").split():
        lang_region, name, quality = vid.split("-")
        base = f"{lang_region.split('_')[0]}/{lang_region}/{name}/{quality}/{vid}.onnx"
        for rev in ("main", "refs/pr/11"):
            try:
                for fn in (base, base + ".json"):
                    p = hf_hub_download("rhasspy/piper-voices", fn, revision=rev, local_dir=tmp)
                    shutil.copy(p, C.VOICES_DIR / os.path.basename(fn))
                print(f"[dl] voice {vid} (rev={rev})", flush=True)
                break
            except Exception as e:  # noqa
                print(f"[dl] {vid} rev={rev} failed: {e}", flush=True)
        else:
            sys.exit(f"تعذّر تنزيل الصوت {vid}")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    C.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    whisper()
    nllb()
    piper()
    print("[dl] done")
