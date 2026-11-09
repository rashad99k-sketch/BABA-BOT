"""Cross-platform, failure-tolerant resource manager for long-running RORO processes."""
from __future__ import annotations

from collections import OrderedDict, deque
import gc
import threading
import time
from typing import Any, Dict, Optional

try:
    import psutil
except Exception:  # Resource monitoring is diagnostic and must never block startup.
    psutil = None


class BoundedEventQueue:
    """Bounded queue with explicit critical-event retention and backpressure stats."""
    def __init__(self, maxsize: int = 1000, critical_reserve: int = 100):
        self.maxsize = max(1, int(maxsize)); self.critical_reserve = max(0, min(self.maxsize, int(critical_reserve)))
        self._items = deque(); self._lock = threading.RLock(); self.dropped = 0; self.rejected_critical = 0

    def put(self, item: Any, critical: bool = False) -> bool:
        with self._lock:
            if len(self._items) >= self.maxsize:
                if critical:
                    index = next((i for i, x in enumerate(self._items) if not (isinstance(x, tuple) and len(x) > 1 and x[1] == "critical")), None)
                    if index is None:
                        self.rejected_critical += 1; return False
                    del self._items[index]
                else:
                    self.dropped += 1; return False
            self._items.append((item, "critical" if critical else "normal")); return True

    def get(self) -> Optional[Any]:
        with self._lock:
            return self._items.popleft()[0] if self._items else None

    def qsize(self) -> int:
        with self._lock: return len(self._items)


class ResourceManager:
    def __init__(self, max_cache_items: int = 5000, max_history: int = 2000):
        self.max_cache_items = max(10, int(max_cache_items)); self.last_cleanup = 0.0; self.last_result = "never"
        self.cache = OrderedDict(); self.history = deque(maxlen=max(10, int(max_history))); self._lock = threading.RLock(); self._last_cpu = None
        self._process = None
        if psutil is not None:
            try:
                self._process = psutil.Process()
            except Exception:
                self._process = None

    def cache_set(self, key: Any, value: Any, ttl_s: float) -> None:
        with self._lock:
            self.cache[key] = (time.monotonic() + max(0.0, float(ttl_s)), value); self.cache.move_to_end(key)
            while len(self.cache) > self.max_cache_items: self.cache.popitem(last=False)

    def cache_get(self, key: Any) -> Optional[Any]:
        with self._lock:
            item = self.cache.get(key)
            if not item: return None
            if item[0] < time.monotonic(): self.cache.pop(key, None); return None
            self.cache.move_to_end(key); return item[1]

    def record(self, event: Any) -> None:
        with self._lock:
            self.history.append((time.time(), event))

    def cleanup(self, preserve_keys=()) -> Dict[str, Any]:
        preserve = set(preserve_keys); removed = 0; now = time.monotonic()
        with self._lock:
            for key, (expiry, _) in list(self.cache.items()):
                if key not in preserve and expiry < now: self.cache.pop(key, None); removed += 1
            while len(self.cache) > self.max_cache_items:
                key, value = self.cache.popitem(last=False)
                if key in preserve:
                    self.cache[key] = value
                    break
            self.last_cleanup = time.time(); self.last_result = f"removed={removed}"
            cache_size, history_size = len(self.cache), len(self.history)
        try:
            gc.collect()
        except Exception:
            pass
        return {"removed": removed, "cache_size": cache_size, "history_size": history_size, "last_cleanup": self.last_cleanup, "result": self.last_result}

    def metrics(self, queue_sizes: Optional[Dict[str, int]] = None, thread_count: Optional[int] = None) -> Dict[str, Any]:
        rss_mb = cpu_percent = None
        if self._process is not None:
            try:
                rss_mb = self._process.memory_info().rss / (1024.0 * 1024.0)
            except Exception:
                pass
            try:
                cpu_percent = self._process.cpu_percent(interval=None)
            except Exception:
                pass
        with self._lock:
            cache_size, history_size = len(self.cache), len(self.history)
            last_cleanup = self.last_cleanup
        warnings = ["cache_pressure"] if cache_size >= self.max_cache_items else []
        return {"rss_mb": round(rss_mb, 2) if rss_mb is not None else None,
                "cpu_percent": round(cpu_percent, 2) if cpu_percent is not None else None,
                "monitoring_available": self._process is not None,
                "threads": int(thread_count if thread_count is not None else threading.active_count()),
                "cache_size": cache_size, "history_size": history_size,
                "queues": queue_sizes or {}, "last_cleanup": last_cleanup, "warnings": warnings}
