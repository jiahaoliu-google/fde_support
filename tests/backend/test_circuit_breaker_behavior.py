import os
import sys
import unittest
import time

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


from backend.core.circuit_breaker import CircuitBreaker, CircuitState, counts_as_failure

class TestCircuitBreaker(unittest.TestCase):
    def test_circuit_breaker_initial_state(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=2)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertIsNotNone(cb.try_acquire())

    def test_circuit_breaker_trips_to_open(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=2)
        
        # Record 2 failures -> state remains CLOSED
        cb.record_failure(429)
        cb.record_failure(429)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertIsNotNone(cb.try_acquire())

        # 3rd failure -> state transitions to OPEN
        cb.record_failure(429)
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertIsNone(cb.try_acquire())

    def test_circuit_breaker_half_open_recovery(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=1)
        cb.record_failure(429)
        cb.record_failure(429)
        cb.record_failure(429)
        self.assertEqual(cb.state, CircuitState.OPEN)

        # Wait for recovery timeout
        time.sleep(1.1)

        # State transitions to HALF_OPEN
        self.assertIsNotNone(cb.try_acquire())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # Record success -> resets to CLOSED
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb.failure_count, 0)

    def _tripped(self, **kw):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=0, **kw)
        for _ in range(3):
            cb.record_failure(503)
        return cb

    def test_half_open_admits_exactly_one_probe(self):
        cb = self._tripped()
        self.assertEqual(cb.try_acquire(), CircuitState.HALF_OPEN)
        self.assertIsNone(cb.try_acquire())
        self.assertIsNone(cb.try_acquire())
        cb.release_probe()
        self.assertEqual(cb.try_acquire(), CircuitState.HALF_OPEN)

    def test_stale_probe_is_replaced(self):
        cb = self._tripped(probe_timeout_seconds=0)
        self.assertEqual(cb.try_acquire(), CircuitState.HALF_OPEN)
        self.assertEqual(cb.try_acquire(), CircuitState.HALF_OPEN)

    def test_probe_failure_reopens(self):
        cb = self._tripped()
        cb.try_acquire()
        cb.recovery_timeout_seconds = 30
        cb.record_failure(429)
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.probe_in_flight)
        self.assertIsNone(cb.try_acquire())

    def test_closed_admits_normally(self):
        cb = CircuitBreaker()
        self.assertEqual(cb.try_acquire(), CircuitState.CLOSED)
        self.assertEqual(cb.try_acquire(), CircuitState.CLOSED)

    def test_only_availability_failures_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        for code in (400, 401, 403, 404, 422):
            cb.record_failure(code)
        self.assertEqual(cb.failure_count, 0)
        for code in (429, 500, 502, 503, 504):
            self.assertTrue(counts_as_failure(code), code)
        cb.record_failure(429)
        cb.record_failure(503)
        cb.record_failure(504)
        self.assertEqual(cb.state, CircuitState.OPEN)

if __name__ == '__main__':
    unittest.main()
