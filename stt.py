"""faster-whisper (CTranslate2) مع سلّم جودة تكيّفي."""
import logging
import threading
from collections import deque

import ctranslate2
from faster_whisper import WhisperModel

import config as C

log = logging.getLogger("ytdub.stt")


class STT:
    def __init__(self):
        cuda = ctranslate2.get_cuda_device_count() > 0
        self.device = "cuda" if cuda else "cpu"
        ctype = "float16" if cuda else "int8"
        self.models = {}
        for lvl in C.WHISPER_LEVELS:
            path = C.MODELS_DIR / f"faster-whisper-{lvl}"
            if (path / "model.bin").exists():
                self.models[lvl] = WhisperModel(
                    str(path), device=self.device, compute_type=ctype,
                    cpu_threads=C.CPU_THREADS, local_files_only=True)
                log.info("loaded whisper %s (%s/%s)", lvl, self.device, ctype)
        if not self.models:
            raise RuntimeError("لا توجد نماذج faster-whisper في " + str(C.MODELS_DIR))
        self.order = [l for l in C.WHISPER_LEVELS if l in self.models]
        self.default = C.WHISPER_DEFAULT if C.WHISPER_DEFAULT in self.models else self.order[0]
        self.level = self.default
        self._lock = threading.Lock()
        self._rtf = deque(maxlen=8)

    def transcribe(self, audio, language=None):
        """يعيد (النص، اللغة، ثقة اللغة)."""
        with self._lock:
            model = self.models[self.level]
            segs, info = model.transcribe(
                audio, language=language, task="transcribe", beam_size=1, best_of=1,
                temperature=0.0, condition_on_previous_text=False, without_timestamps=True,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 150})
            parts = []
            for s in segs:
                if s.no_speech_prob > 0.7 and s.avg_logprob < -1.0:
                    continue                                   # هلوسة على ضجيج
                t = s.text.strip()
                if t:
                    parts.append(t)
        text = " ".join(parts).strip()
        return text, info.language, float(info.language_probability or 0.0)

    def report_rtf(self, rtf: float):
        """يعيد اسم المستوى الجديد إن تغيّر، وإلا None."""
        self._rtf.append(rtf)
        idx = self.order.index(self.level)
        last3 = list(self._rtf)[-3:]
        if idx > 0 and len(last3) == 3 and all(r > C.RTF_DOWN for r in last3):
            self.level = self.order[idx - 1]
            self._rtf.clear()
            log.warning("RTF مرتفع → خفض النموذج إلى %s", self.level)
            return self.level
        if (self.order.index(self.level) < self.order.index(self.default)
                and len(self._rtf) == self._rtf.maxlen
                and sum(self._rtf) / len(self._rtf) < C.RTF_UP):
            self.level = self.order[self.order.index(self.level) + 1]
            self._rtf.clear()
            log.info("RTF منخفض → رفع النموذج إلى %s", self.level)
            return self.level
        return None
