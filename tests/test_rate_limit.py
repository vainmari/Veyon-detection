"""
tests/test_rate_limit.py
────────────────────────
Unit tests for app/core/rate_limit.py — the login brute-force limiter.
Time is controlled by monkeypatching time.monotonic inside the module.

Run:  pytest tests/test_rate_limit.py -v
"""
from __future__ import annotations
import pytest

from app.core import rate_limit


@pytest.fixture(autouse=True)
def clean_state():
    rate_limit.reset_all()
    yield
    rate_limit.reset_all()


@pytest.fixture
def clock(monkeypatch):
    """Fake monotonic clock — tests advance it explicitly."""
    state = {"now": 1000.0}
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: state["now"])

    def advance(seconds: float) -> None:
        state["now"] += seconds

    return advance


IP = "10.0.0.42"


class TestRateLimit:

    def test_fresh_key_not_blocked(self):
        assert not rate_limit.is_blocked(IP)
        assert rate_limit.retry_after(IP) == 0

    def test_blocked_after_max_attempts(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS):
            assert not rate_limit.is_blocked(IP)
            rate_limit.record_failure(IP)
        assert rate_limit.is_blocked(IP)
        assert rate_limit.retry_after(IP) == pytest.approx(rate_limit.WINDOW_SEC)

    def test_one_attempt_below_limit_not_blocked(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS - 1):
            rate_limit.record_failure(IP)
        assert not rate_limit.is_blocked(IP)

    def test_unblocks_when_window_expires(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure(IP)
        assert rate_limit.is_blocked(IP)
        clock(rate_limit.WINDOW_SEC + 1)
        assert not rate_limit.is_blocked(IP)

    def test_retry_after_shrinks_over_time(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure(IP)
        first = rate_limit.retry_after(IP)
        clock(60)
        assert rate_limit.retry_after(IP) == pytest.approx(first - 60)

    def test_success_clears_history(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS - 1):
            rate_limit.record_failure(IP)
        rate_limit.record_success(IP)
        for _ in range(rate_limit.MAX_ATTEMPTS - 1):
            rate_limit.record_failure(IP)
        assert not rate_limit.is_blocked(IP)

    def test_keys_are_independent(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure(IP)
        assert rate_limit.is_blocked(IP)
        assert not rate_limit.is_blocked("10.0.0.99")

    def test_old_failures_age_out_of_window(self, clock):
        # Failures spread out so the oldest ones expire before the limit hits.
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure(IP)
            clock(rate_limit.WINDOW_SEC / rate_limit.MAX_ATTEMPTS + 1)
        assert not rate_limit.is_blocked(IP)
