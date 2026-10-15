import time
import logging
from enum import Enum

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout_seconds: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_state_change = time.time()

    @property
    def current_state(self) -> CircuitState:
        """
        Evaluates auto-cooldown timer. If in OPEN state and recovery_timeout_seconds
        has elapsed, automatically transitions to HALF_OPEN.
        """
        now = time.time()
        if self.state == CircuitState.OPEN and (now - self.last_state_change >= self.recovery_timeout_seconds):
            logger.info("Circuit breaker recovery timeout reached. Auto-cooldown transitioning to HALF_OPEN.")
            self.state = CircuitState.HALF_OPEN
            self.last_state_change = now
        return self.state

    def allow_request(self) -> bool:
        state = self.current_state
        if state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
            return True
        return False

    def record_failure(self, status_code: int = 500):
        self.failure_count += 1
        logger.warning(f"Circuit breaker recorded failure {self.failure_count}/{self.failure_threshold} (status_code={status_code})")
        if self.failure_count >= self.failure_threshold or self.state == CircuitState.HALF_OPEN:
            logger.error(f"Circuit breaker tripping to OPEN state! Downstream failures={self.failure_count}")
            self.state = CircuitState.OPEN
            self.last_state_change = time.time()

    def record_success(self):
        if self.state != CircuitState.CLOSED:
            logger.info("Circuit breaker probe succeeded. Transitioning back to CLOSED state.")
        self.failure_count = 0
        self.state = CircuitState.CLOSED
