import os
import sys
import unittest
import time

# Ensure workspace root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.circuit_breaker import CircuitBreaker, CircuitState

class TestCircuitBreaker(unittest.TestCase):
    def test_circuit_breaker_initial_state(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=2)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.allow_request())

    def test_circuit_breaker_trips_to_open(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=2)
        
        # Record 2 failures -> state remains CLOSED
        cb.record_failure(429)
        cb.record_failure(429)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.allow_request())

        # 3rd failure -> state transitions to OPEN
        cb.record_failure(429)
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.allow_request())

    def test_circuit_breaker_half_open_recovery(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=1)
        cb.record_failure(429)
        cb.record_failure(429)
        cb.record_failure(429)
        self.assertEqual(cb.state, CircuitState.OPEN)

        # Wait for recovery timeout
        time.sleep(1.1)

        # State transitions to HALF_OPEN
        self.assertTrue(cb.allow_request())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # Record success -> resets to CLOSED
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertEqual(cb.failure_count, 0)

if __name__ == '__main__':
    unittest.main()
