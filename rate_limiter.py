"""
core/rate_limiter.py — Token bucket rate limiter for API calls.

Usage:
    limiter = RateLimiter("dexscreener", calls_per_second=2)
    limiter.wait()   # blocks until a token is available
    response = requests.get(...)
"""

import time
import threading
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Token bucket implementation.
    Allows bursts up to `burst` calls, refills at `calls_per_second`.
    """

    def __init__(self, name: str, calls_per_second: float = 2.0,
                 burst: int = None):
        self.name              = name
        self.calls_per_second  = calls_per_second
        self.burst             = burst or max(1, int(calls_per_second * 2))
        self._tokens           = float(self.burst)
        self._last_refill      = time.monotonic()
        self._lock             = threading.Lock()

    def _refill(self):
        now      = time.monotonic()
        elapsed  = now - self._last_refill
        new_tok  = elapsed * self.calls_per_second
        self._tokens = min(self.burst, self._tokens + new_tok)
        self._last_refill = now

    def wait(self, tokens: float = 1.0):
        """Block until `tokens` tokens are available."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait_for = (tokens - self._tokens) / self.calls_per_second

            if wait_for > 0:
                logger.debug(
                    f"RateLimiter [{self.name}] waiting {wait_for:.2f}s"
                )
                time.sleep(wait_for)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-blocking. Returns True if tokens available."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False


# ── Registry ─────────────────────────────────────────────────

_registry: dict[str, RateLimiter] = {}

# Default limits per API
_DEFAULTS = {
    "dexscreener": {"calls_per_second": 2.0,  "burst": 5},
    "rugcheck":    {"calls_per_second": 1.0,  "burst": 3},
    "helius":      {"calls_per_second": 5.0,  "burst": 10},
    "birdeye":     {"calls_per_second": 1.0,  "burst": 3},
    "gecko":       {"calls_per_second": 2.0,  "burst": 5},
    "telegram":    {"calls_per_second": 0.5,  "burst": 3},
}


def get_limiter(name: str, **kwargs) -> RateLimiter:
    if name not in _registry:
        defaults = _DEFAULTS.get(name, {})
        merged   = {**defaults, **kwargs}
        _registry[name] = RateLimiter(name, **merged)
    return _registry[name]


def wait(name: str, tokens: float = 1.0):
    """Convenience: wait on a named limiter."""
    get_limiter(name).wait(tokens)
