"""Per-device token bucket. Cheap, in-process, and good enough to stop a
misbehaving or spoofed recorder from filling the disk."""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_buckets: dict[str, tuple[float, float]] = {}


def allow(key: str, per_minute: int, burst: int) -> bool:
    if per_minute <= 0:
        return True
    rate = per_minute / 60.0
    capacity = float(max(burst, 1))
    nowt = time.monotonic()
    with _lock:
        tokens, last = _buckets.get(key, (capacity, nowt))
        tokens = min(capacity, tokens + (nowt - last) * rate)
        if tokens < 1.0:
            _buckets[key] = (tokens, nowt)
            return False
        _buckets[key] = (tokens - 1.0, nowt)
        if len(_buckets) > 4096:
            _buckets.clear()
        return True


def reset() -> None:
    with _lock:
        _buckets.clear()
