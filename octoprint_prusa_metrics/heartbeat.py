"""Detects OctoPrint's event delivery stalling, by sending ourselves an event.

OctoPrint hands every event to every plugin one at a time, on a single thread. A
plugin whose event handler blocks stops delivery for every plugin, with nothing
logged (on 2026-11-08 one did so for 8 hours). The plugin fires a heartbeat event
and records when it comes back; how long the outstanding one has waited is the lag.

Deliberately free of OctoPrint imports so it unit-tests standalone, like parsing and
collector. Every method takes ``now`` so tests drive time directly.
"""

import threading


class EventHeartbeat:
    """At most one heartbeat is outstanding, so a stall can't pile up events."""

    def __init__(self):
        self._lock = threading.Lock()
        self._outstanding = None  # "sent" time of the heartbeat not yet delivered
        self._last_fired = None
        self._last_delivered = None
        self._restart_attempted = False

    def due(self, now, interval):
        """True if a new heartbeat should be fired now."""
        with self._lock:
            if self._outstanding is not None:
                return False
            return self._last_fired is None or now - self._last_fired >= interval

    def fired(self, now):
        with self._lock:
            self._outstanding = now
            self._last_fired = now

    def delivered(self, sent, now):
        """A heartbeat came back through OctoPrint's event delivery."""
        with self._lock:
            if self._outstanding is not None and sent == self._outstanding:
                self._outstanding = None
            self._last_delivered = now

    def lag(self, now):
        """Seconds the outstanding heartbeat has waited; 0 while delivery keeps up."""
        with self._lock:
            return 0.0 if self._outstanding is None else max(0.0, now - self._outstanding)

    @property
    def last_delivered(self):
        with self._lock:
            return self._last_delivered

    def should_restart(self, now, enabled, threshold, print_active, recovery_pending):
        """Whether to restart OctoPrint now. True at most once per process.

        Never while a print is active or an interrupted print may still be resumed:
        a print must never be disturbed for the sake of monitoring.
        """
        with self._lock:
            if not enabled or self._restart_attempted or self._outstanding is None:
                return False
            if now - self._outstanding < threshold:
                return False
            if print_active or recovery_pending:
                return False
            self._restart_attempted = True
            return True
