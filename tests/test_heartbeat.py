"""Event-delivery heartbeat logic (no OctoPrint needed) and its metrics."""

import pytest
from test_collector import has_metric, render

from octoprint_prusa_metrics.heartbeat import EventHeartbeat

INTERVAL = 30
THRESHOLD = 900


@pytest.fixture
def hb():
    return EventHeartbeat()


def stall(hb, at=1000.0):
    """Fire one heartbeat that never comes back."""
    hb.fired(at)
    return at


class TestDelivery:
    def test_first_heartbeat_is_due_immediately(self, hb):
        assert hb.due(0.0, INTERVAL)

    def test_next_heartbeat_waits_for_the_interval(self, hb):
        hb.fired(0.0)
        hb.delivered(0.0, 0.1)
        assert not hb.due(29.0, INTERVAL)
        assert hb.due(30.0, INTERVAL)

    def test_only_one_heartbeat_is_ever_outstanding(self, hb):
        # A stalled OctoPrint must not accumulate a queue of our events.
        hb.fired(0.0)
        assert not hb.due(3600.0, INTERVAL)

    def test_lag_is_zero_while_delivery_keeps_up(self, hb):
        hb.fired(0.0)
        hb.delivered(0.0, 0.2)
        assert hb.lag(500.0) == 0.0
        assert hb.last_delivered == 0.2

    def test_lag_climbs_while_a_heartbeat_is_undelivered(self, hb):
        stall(hb, at=1000.0)
        assert hb.lag(1000.0) == 0.0
        assert hb.lag(1300.0) == 300.0

    def test_a_stale_delivery_does_not_clear_a_newer_heartbeat(self, hb):
        hb.fired(0.0)
        hb.delivered(0.0, 1.0)
        hb.fired(30.0)
        hb.delivered(0.0, 31.0)  # a duplicate of the old one
        assert hb.lag(100.0) == 70.0


class TestRestartDecision:
    def ask(self, hb, now, enabled=True, print_active=False, recovery_pending=False):
        return hb.should_restart(now, enabled, THRESHOLD, print_active, recovery_pending)

    def test_off_by_default_means_never(self, hb):
        stall(hb)
        assert not self.ask(hb, 1000 + THRESHOLD + 1, enabled=False)

    def test_restarts_after_the_threshold_with_no_print(self, hb):
        stall(hb)
        assert not self.ask(hb, 1000 + THRESHOLD - 1)
        assert self.ask(hb, 1000 + THRESHOLD)

    def test_never_while_a_print_is_active(self, hb):
        stall(hb)
        assert not self.ask(hb, 1000 + THRESHOLD * 10, print_active=True)

    def test_never_while_a_print_recovery_is_pending(self, hb):
        stall(hb)
        assert not self.ask(hb, 1000 + THRESHOLD * 10, recovery_pending=True)

    def test_waits_for_the_print_to_end_then_restarts(self, hb):
        stall(hb)
        assert not self.ask(hb, 1000 + THRESHOLD, print_active=True)
        assert self.ask(hb, 1000 + THRESHOLD + 3600)

    def test_restarts_at_most_once(self, hb):
        stall(hb)
        assert self.ask(hb, 1000 + THRESHOLD)
        assert not self.ask(hb, 1000 + THRESHOLD * 2)

    def test_nothing_to_restart_for_when_delivery_is_fine(self, hb):
        hb.fired(0.0)
        hb.delivered(0.0, 0.1)
        assert not self.ask(hb, 10**6)


class TestMetrics:
    def test_lag_and_last_delivery_are_exported(self):
        output = render({"event_delivery_lag": 42.0, "event_heartbeat_last_delivered": 1234.5})
        assert "octoprint_event_delivery_lag_seconds 42.0" in output
        assert "octoprint_event_heartbeat_last_delivered_timestamp_seconds 1234.5" in output

    def test_no_delivery_yet_omits_the_timestamp(self):
        output = render({"event_delivery_lag": 0.0, "event_heartbeat_last_delivered": None})
        assert has_metric(output, "octoprint_event_delivery_lag_seconds")
        assert not has_metric(output, "octoprint_event_heartbeat_last_delivered_timestamp_seconds")
