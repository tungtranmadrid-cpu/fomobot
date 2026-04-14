"""Token bucket per chat_id (chống spam)."""
import time as _time
from typing import Dict, Tuple

from .config import RATE_LIMIT_BURST, RATE_LIMIT_PER_MINUTE

_rate_buckets: Dict[int, Tuple[float, float]] = {}
_rate_notified: Dict[int, float] = {}


def rate_limit_check(chat_id: int) -> bool:
    """Trả về True nếu chat_id còn quota, False nếu vượt."""
    now = _time.monotonic()
    refill_per_sec = RATE_LIMIT_PER_MINUTE / 60.0
    capacity = float(RATE_LIMIT_PER_MINUTE + RATE_LIMIT_BURST)
    tokens, last = _rate_buckets.get(chat_id, (capacity, now))
    tokens = min(capacity, tokens + (now - last) * refill_per_sec)
    if tokens < 1.0:
        _rate_buckets[chat_id] = (tokens, now)
        return False
    _rate_buckets[chat_id] = (tokens - 1.0, now)
    return True


def rate_limit_should_notify(chat_id: int, cooldown: float = 30.0) -> bool:
    """Chỉ gửi cảnh báo 1 lần mỗi `cooldown` giây để không spam."""
    now = _time.monotonic()
    last = _rate_notified.get(chat_id, 0.0)
    if now - last < cooldown:
        return False
    _rate_notified[chat_id] = now
    return True
