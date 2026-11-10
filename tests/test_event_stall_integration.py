"""Event-delivery heartbeat and stall restart, against a real running OctoPrint.

OctoPrint runs with the virtual printer and tests/support/stall_events.py, which blocks
OctoPrint's event thread on demand. Its restart command is pointed at a command that
appends a line to a file, so a restart is observable and countable without one.
"""

import os
import shutil
import socket
import subprocess
import sys
import time

import pytest
import requests
import yaml

API_KEY = "prusametricstestkey0123456789abcd"
HERE = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.path.join(HERE, "support", "stall_events.py")
LAG = "octoprint_event_delivery_lag_seconds"
RESTART_AFTER = 0.1  # minutes: 6 s


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    def __init__(self, tmp_path, metrics_settings):
        self.basedir = tmp_path / "octoprint"
        self.basedir.mkdir()
        self.control = tmp_path / "control"
        self.control.mkdir()
        self.restarts = self.control / "restarts"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        settings = {"heartbeat_interval_seconds": 1}
        settings.update(metrics_settings)
        cfg = {
            "api": {"key": API_KEY},
            "server": {
                "host": "127.0.0.1",
                "port": self.port,
                "firstRun": False,
                "onlineCheck": {"enabled": False},
                "pluginBlacklist": {"enabled": False},
                "commands": {"serverRestartCommand": f"sh -c 'echo restart >> {self.restarts}'"},
            },
            "feature": {"sdSupport": False},
            "webcam": {"webcamEnabled": False, "timelapseEnabled": False},
            "plugins": {
                "_disabled": [
                    "announcements",
                    "errortracking",
                    "health_check",
                    "pluginmanager",
                    "softwareupdate",
                    "tracking",
                    "discovery",
                ],
                "virtual_printer": {"enabled": True},
                "prusa_metrics": settings,
            },
            "serial": {"port": "VIRTUAL", "baudrate": 115200, "autoconnect": True},
        }
        with open(self.basedir / "config.yaml", "w") as f:
            yaml.safe_dump(cfg, f)
        plugins = self.basedir / "plugins"
        plugins.mkdir()
        shutil.copy(SUPPORT, plugins / "stall_events.py")
        env = dict(os.environ, PYTHONUNBUFFERED="1", STALL_EVENTS_DIR=str(self.control))
        self.stdout = open(self.basedir / "stdout.log", "w")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "octoprint",
                "--basedir",
                str(self.basedir),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            stdout=self.stdout,
            stderr=subprocess.STDOUT,
            env=env,
            # `python -m` puts the cwd on sys.path: never let a checkout shadow the
            # installed plugin under test.
            cwd=str(self.basedir),
        )
        assert self.wait_for(lambda: self.metric(LAG) is not None, 90), self.diagnostics()

    def stop(self):
        self.release()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.stdout.close()

    def api(self, method, path, **kw):
        headers = kw.pop("headers", {})
        headers["X-Api-Key"] = API_KEY
        return requests.request(method, self.base + path, headers=headers, timeout=30, **kw)

    def metric(self, name):
        try:
            text = requests.get(self.base + "/plugin/prusa_metrics/metrics", timeout=10).text
        except requests.RequestException:
            return None
        for line in text.splitlines():
            if line.startswith(name + " "):
                return float(line.split()[1])
        return None

    def wait_for(self, predicate, timeout, interval=0.25):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if predicate():
                    return True
            except (requests.RequestException, ValueError, KeyError):
                pass
            time.sleep(interval)
        return False

    def printer_state(self):
        return self.api("GET", "/api/connection").json()["current"]["state"]

    def block_events(self):
        """Stall OctoPrint's event delivery: the next Upload event blocks it."""
        (self.control / "block").touch()
        r = self.api("POST", "/api/files/local", files={"file": ("trigger.gcode", b"M117 hi\n")})
        assert r.status_code == 201, r.text

    def release(self):
        try:
            os.remove(self.control / "block")
        except FileNotFoundError:
            pass

    def restart_count(self):
        try:
            return len(self.restarts.read_text().splitlines())
        except FileNotFoundError:
            return 0

    def log_text(self):
        try:
            return (self.basedir / "logs" / "octoprint.log").read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def diagnostics(self):
        lines = [x for x in self.log_text().splitlines() if "prusa_metrics" in x or "stall" in x]
        return "\n".join(lines[-40:])


@pytest.fixture
def make_rig(tmp_path):
    rigs = []

    def factory(**settings):
        rig = Rig(tmp_path, settings)
        rigs.append(rig)
        return rig

    yield factory
    for rig in rigs:
        rig.stop()


def test_lag_is_zero_while_events_flow(make_rig):
    rig = make_rig()
    assert rig.wait_for(
        lambda: rig.metric("octoprint_event_heartbeat_last_delivered_timestamp_seconds"), 15
    ), rig.diagnostics()
    rig.wait_for(lambda: False, 3)
    assert rig.metric(LAG) == 0.0


def test_a_blocked_event_thread_shows_as_climbing_lag_and_recovers(make_rig):
    rig = make_rig()
    rig.block_events()
    assert rig.wait_for(lambda: rig.metric(LAG) > 5, 20), rig.diagnostics()
    rig.release()
    assert rig.wait_for(lambda: rig.metric(LAG) == 0.0, 15), rig.diagnostics()


def test_no_restart_unless_enabled(make_rig):
    rig = make_rig(event_stall_restart_minutes=RESTART_AFTER)  # restart left at its default
    rig.block_events()
    assert rig.wait_for(lambda: rig.metric(LAG) > 15, 30), rig.diagnostics()
    assert rig.restart_count() == 0


def test_restarts_once_when_enabled_and_idle(make_rig):
    rig = make_rig(event_stall_restart=True, event_stall_restart_minutes=RESTART_AFTER)
    rig.wait_for(lambda: rig.printer_state() == "Operational", 30)
    rig.block_events()
    assert rig.wait_for(lambda: rig.restart_count() == 1, 30), rig.diagnostics()
    rig.wait_for(lambda: False, 8)  # still stalled, well past the threshold again
    assert rig.restart_count() == 1
    assert "restarting OctoPrint with its configured restart command" in rig.log_text()


def test_never_restarts_while_a_print_recovery_is_pending(make_rig):
    rig = make_rig(event_stall_restart=True, event_stall_restart_minutes=RESTART_AFTER)
    (rig.control / "recovery_pending").touch()
    rig.block_events()
    assert rig.wait_for(lambda: rig.metric(LAG) > 15, 30), rig.diagnostics()
    assert rig.restart_count() == 0
    os.remove(rig.control / "recovery_pending")
    assert rig.wait_for(lambda: rig.restart_count() == 1, 15), rig.diagnostics()


def test_waits_for_an_active_print_to_end(make_rig):
    rig = make_rig(event_stall_restart=True, event_stall_restart_minutes=RESTART_AFTER)
    assert rig.wait_for(lambda: rig.printer_state() == "Operational", 30), rig.diagnostics()
    (rig.control / "block").touch()
    # ~20 s of dwells. The upload stalls event delivery; printing doesn't depend on it.
    job = ("G4 P1000\n" * 20).encode()
    r = rig.api(
        "POST", "/api/files/local", files={"file": ("slow.gcode", job)}, data={"print": "true"}
    )
    assert r.status_code == 201, r.text
    assert rig.wait_for(lambda: rig.printer_state().startswith("Printing"), 15), rig.diagnostics()
    assert rig.wait_for(lambda: rig.metric(LAG) > 10, 20), rig.diagnostics()
    assert rig.printer_state().startswith("Printing")
    assert rig.restart_count() == 0  # past the threshold, but printing

    assert rig.wait_for(lambda: rig.printer_state() == "Operational", 60), rig.diagnostics()
    assert rig.wait_for(lambda: rig.restart_count() == 1, 15), rig.diagnostics()
