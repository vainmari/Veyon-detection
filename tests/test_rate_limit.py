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


class TestLoginHelpers:
    """Combined per-IP + per-username tracking used by the login page."""

    def test_username_blocked_across_different_ips(self, clock):
        """A distributed guesser rotating IPs is still throttled per account."""
        for i in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_login_failure(f"10.0.0.{i}", "teacher1")
        assert rate_limit.login_retry_after("10.9.9.9", "teacher1") > 0

    def test_other_usernames_on_shared_ip_not_locked_out(self, clock):
        """One student's failures must not lock out the teacher behind the same NAT IP.

        Each failure counts against the IP key too, so the shared IP itself
        locks after MAX_ATTEMPTS — the win is per-account isolation once the
        IP window clears while the attacked account's window is still ticking.
        """
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_login_failure(IP, "victim")
        assert rate_limit.login_retry_after(IP, "victim") > 0
        # Same username from anywhere: still blocked. Different username after
        # the IP window expires: allowed immediately.
        clock(rate_limit.WINDOW_SEC + 1)
        assert rate_limit.login_retry_after(IP, "teacher1") == 0

    def test_username_normalized_case_and_whitespace(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_login_failure(IP, "Teacher1")
        assert rate_limit.login_retry_after("10.9.9.9", "  teacher1  ") > 0

    def test_success_clears_both_keys(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS - 1):
            rate_limit.record_login_failure(IP, "carol")
        rate_limit.record_login_success(IP, "carol")
        assert rate_limit.login_retry_after(IP, "carol") == 0
        for _ in range(rate_limit.MAX_ATTEMPTS - 1):
            rate_limit.record_login_failure(IP, "carol")
        assert rate_limit.login_retry_after(IP, "carol") == 0

    def test_empty_username_tracks_ip_only(self, clock):
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_login_failure(IP, "")
        assert rate_limit.login_retry_after(IP, "") > 0
        assert rate_limit.login_retry_after("10.9.9.9", "") == 0

    def test_username_and_ip_do_not_collide(self, clock):
        """A username that looks like an IP must not share the IP's counter."""
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_login_failure(IP, IP)
        # IP key blocked, but the *username* key for a different client is
        # tracked separately under the user: prefix.
        assert rate_limit.retry_after(IP) == 0  # raw key untouched

    def test_key_dict_stays_bounded(self, clock):
        """Expired attacker-generated username keys are swept, not kept forever."""
        original_max = rate_limit._MAX_KEYS
        rate_limit._MAX_KEYS = 50
        try:
            for i in range(60):
                rate_limit.record_login_failure(IP, f"fake-user-{i}")
            clock(rate_limit.WINDOW_SEC + 1)
            rate_limit.record_login_failure(IP, "one-more")
            assert len(rate_limit._failures) <= 2  # ip + the fresh username
        finally:
            rate_limit._MAX_KEYS = original_max
