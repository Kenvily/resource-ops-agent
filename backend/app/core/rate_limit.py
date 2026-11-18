"""Bounded per-process token buckets. Client address comes from ASGI, never X-Forwarded-For."""

import time
from collections import OrderedDict


class RequestLimiter:
    def __init__(self, per_minute=60, burst=30):
        self.rate, self.burst = per_minute / 60, burst
        self.clients: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def allow(self, key, now=None):
        now = time.monotonic() if now is None else now
        tokens, last = self.clients.pop(key, (self.burst, now))
        tokens = min(self.burst, tokens + max(0, now - last) * self.rate)
        allowed = tokens >= 1
        self.clients[key] = (tokens - 1 if allowed else tokens, now)
        if len(self.clients) > 512:
            self.clients.popitem(last=False)
        return allowed
