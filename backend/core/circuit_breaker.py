import time
import logging
import threading
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

def counts_as_failure(status_code: int) -> bool:
    """Only availability failures count toward tripping the breaker.

    429 (rate limited) and 5xx/timeouts (mapped to 504) mean the dependency is unhealthy.
    Other 4xx are request or configuration errors that more traffic won't change, so they
    neither trip nor reset the breaker.
    """
    return status_code == 429 or status_code >= 500

class CircuitBreaker:
    """CLOSED -> OPEN -> HALF_OPEN state machine guarding the FAQ agent / Agent Search.

    Callers record one outcome per customer question, not per retry attempt, so the
    breaker opens after `failure_threshold` consecutive failed questions (TDD: 3).

    In HALF_OPEN exactly one request is admitted as the probe. Everyone else gets the
    fallback until the probe reports back, so a recovering dependency gets 1 request
    instead of a burst.
    """

    def __init__(self, failure_threshold: int = 3, recovery_timeout_seconds: float = 30.0,
                 probe_timeout_seconds: float = 90.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        # A probe that never reports back (e.g. the worker died) stops blocking new probes
        # after this long. Must exceed the FAQ agent's HTTP timeout.
        self.probe_timeout_seconds = probe_timeout_seconds
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_state_change = time.time()
        self._probe_started_at: Optional[float] = None
        self._lock = threading.Lock()

    def _refresh_state(self) -> CircuitState:
        """OPEN -> HALF_OPEN once the cooldown has elapsed. Caller must hold the lock."""
        now = time.time()
        if self.state == CircuitState.OPEN and (now - self.last_state_change >= self.recovery_timeout_seconds):
            logger.info("Circuit breaker recovery timeout reached. Auto-cooldown transitioning to HALF_OPEN.")
            self.state = CircuitState.HALF_OPEN
            self.last_state_change = now
            self._probe_started_at = None
        return self.state

    @property
    def current_state(self) -> CircuitState:
        """
        Evaluates auto-cooldown timer. If in OPEN state and recovery_timeout_seconds
        has elapsed, automatically transitions to HALF_OPEN.
        """
        with self._lock:
            return self._refresh_state()

    @property
    def probe_in_flight(self) -> bool:
        with self._lock:
            return self._probe_started_at is not None

    def try_acquire(self) -> Optional[CircuitState]:
        """Admission decision for one request.

        Returns CircuitState.CLOSED for a normal call, CircuitState.HALF_OPEN if this
        request is the single recovery probe, or None if it must take the fallback.
        """
        with self._lock:
            state = self._refresh_state()
            if state == CircuitState.CLOSED:
                return CircuitState.CLOSED
            if state == CircuitState.HALF_OPEN:
                now = time.time()
                stale = (self._probe_started_at is not None
                         and now - self._probe_started_at >= self.probe_timeout_seconds)
                if self._probe_started_at is None or stale:
                    if stale:
                        logger.warning("Circuit breaker probe never reported back; admitting a new probe.")
                    self._probe_started_at = now
                    return CircuitState.HALF_OPEN
            return None

    def release_probe(self):
        """Frees the probe slot if the probe ended without a countable outcome (e.g. a 4xx)."""
        with self._lock:
            self._probe_started_at = None

    def record_failure(self, status_code: int = 503):
        """Records one failed customer question. Non-availability codes are ignored."""
        with self._lock:
            if not counts_as_failure(status_code):
                logger.info(f"Circuit breaker ignoring non-availability status_code={status_code}")
                return
            self._probe_started_at = None
            self.failure_count += 1
            logger.warning(f"Circuit breaker recorded failure {self.failure_count}/{self.failure_threshold} (status_code={status_code})")
            if self.failure_count >= self.failure_threshold or self.state == CircuitState.HALF_OPEN:
                logger.error(f"Circuit breaker tripping to OPEN state! Downstream failures={self.failure_count}")
                self.state = CircuitState.OPEN
                self.last_state_change = time.time()

    def record_success(self):
        with self._lock:
            if self.state != CircuitState.CLOSED:
                logger.info("Circuit breaker probe succeeded. Transitioning back to CLOSED state.")
            self.failure_count = 0
            self.state = CircuitState.CLOSED
            self._probe_started_at = None
