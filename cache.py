"""كاش LRU محدود بالحجم (بايت)."""
from collections import OrderedDict
import threading


class LRUCache:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self.size = 0
        self._d: "OrderedDict[str, bytes]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key, value: bytes):
        if len(value) > self.max_bytes:
            return
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self.size -= len(old)
            self._d[key] = value
            self.size += len(value)
            while self.size > self.max_bytes and self._d:
                _, ev = self._d.popitem(last=False)
                self.size -= len(ev)
