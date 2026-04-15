"""
core/circuit_breaker.py — Circuit breaker pattern for external API calls.

States:
  CLOSED  — normal operation, calls go through
  OPEN    — too many failures, calls blocked (raises CircuitOpenError)
  HALF    — testing recovery, one call allowed through

Usage:
    cb = CircuitBreaker("dexscreener", failure_threshold=5, recovery_timeout=30)

    @cb.call
    def fetch():
        return requests.get(...)
"""

import time
import logging
import threading
from enum import Enum
from typing import Callable, Any

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN   = "open"
    HALF   = "half_open"


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self,
                 name: str,
                 failure_threshold: int = 5,
                 recovery_timeout: int = 30,
                 success_threshold: int = 2):
        self.name              = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout  = recovery_timeout
        self.success_threshold = success_threshold

        self._state          = CircuitState.CLOSED
        self._failure_count  = 0
        self._success_count  = 0
        self._last_failure   = None
        self._lock           = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if (self._state == CircuitState.OPEN and
                    self._last_failure and
                    time.time() - self._last_failure >= self.recovery_timeout):
                self._state = CircuitState.HALF
                logger.info(f"Circuit [{self.name}] → HALF_OPEN (testing recovery)")
            return self._state

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        state = self.state
        if state == CircuitState.OPEN:
            raise CircuitOpenError(
                f"Circuit [{self.name}] is OPEN — calls blocked"
            )

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except CircuitOpenError:
            raise
        except Exception as e:
            self._on_failure(e)
            raise

    def _on_success(self):
        with self._lock:
            self._failure_count = 0
            if self._state == CircuitState.HALF:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state         = CircuitState.CLOSED
                    self._success_count = 0
                    logger.info(f"Circuit [{self.name}] → CLOSED (recovered)")

    def _on_failure(self, exc: Exception):
        with self._lock:
            self._failure_count += 1
            self._last_failure  = time.time()
            self._success_count = 0

            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    f"Circuit [{self.name}] → OPEN after "
                    f"{self._failure_count} failures. "
                    f"Last: {exc}"
                )

    def is_available(self) -> bool:
        return self.state != CircuitState.OPEN

    def reset(self):
        with self._lock:
            self._state         = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._last_failure  = None


# ── Registry ─────────────────────────────────────────────────

_registry: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    if name not in _registry:
        _registry[name] = CircuitBreaker(name, **kwargs)
    return _registry[name]


def status_all() -> dict:
    return {
        name: cb.state.value
        for name, cb in _registry.items()
    }
