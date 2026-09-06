"""OctoPrint plugin exposing printer telemetry as Prometheus metrics, plus the
printer firmware version parsed out of the M115 handshake.

Read-only by design: the plugin observes the gcode stream and the public
PrinterInterface, and never sends a command to the printer.
"""

import platform
import socket
import threading

import flask
import octoprint.plugin
from octoprint.events import Events
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

from .collector import build_metrics
from .parsing import ExtrusionTracker, firmware_labels, parse_fan_speed, parse_m115, parse_mmu_line

__plugin_name__ = "Prusa Metrics"
__plugin_pythoncompat__ = ">=3.7,<4"


class _SnapshotCollector:
    """Adapter letting prometheus_client pull a fresh snapshot per scrape."""

    def __init__(self, plugin):
        self._plugin = plugin

    def collect(self):
        return build_metrics(self._plugin.build_snapshot())


class PrusaMetricsPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.BlueprintPlugin,
):
    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._registry = CollectorRegistry()
        self._firmware = None
        self._mmu = None
        self._clients = 0
        self._fan_speed = 0.0
        self._extrusion = ExtrusionTracker()
        self._prints = {"started": 0, "done": 0, "failed": 0, "cancelled": 0}
        self._print_time_total = 0.0
        self._last_print_time = None
        self._last_print_extrusion = None
        self._extrusion_at_print_start = 0.0

    # ~~ StartupPlugin

    def on_after_startup(self):
        self._registry.register(_SnapshotCollector(self))
        self._logger.info(
            "Prusa Metrics ready; scrape /plugin/prusa_metrics/metrics. "
            "Firmware info appears after the next printer connect."
        )

    # ~~ BlueprintPlugin

    @octoprint.plugin.BlueprintPlugin.route("/metrics", methods=["GET"])
    def metrics_endpoint(self):
        # content_type, not mimetype: CONTENT_TYPE_LATEST already carries a
        # charset, and Flask appends another to a bare mimetype, yielding a
        # malformed "...; charset=utf-8; charset=utf-8" header.
        return flask.Response(generate_latest(self._registry), content_type=CONTENT_TYPE_LATEST)

    def is_blueprint_protected(self):
        # Prometheus scrapers have no way to present an OctoPrint API key, so
        # this endpoint is open. Keep it on a trusted network.
        return False

    def is_blueprint_csrf_protected(self):
        # GET-only and side-effect free.
        return False

    # ~~ Hooks

    def on_gcode_received(self, comm, line, *args, **kwargs):
        if not line:
            return line

        if "FIRMWARE_NAME:" in line:
            fields = parse_m115(line)
            if fields:
                with self._lock:
                    self._firmware = firmware_labels(fields)

        mmu = parse_mmu_line(line)
        if mmu:
            with self._lock:
                self._mmu = mmu

        return line

    def on_gcode_sent(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if not cmd:
            return
        with self._lock:
            self._extrusion.feed(cmd)
            self._fan_speed = parse_fan_speed(cmd, self._fan_speed)

    # ~~ EventHandlerPlugin

    def on_event(self, event, payload):
        with self._lock:
            if event == Events.CLIENT_OPENED:
                self._clients += 1
            elif event == Events.CLIENT_CLOSED:
                self._clients = max(0, self._clients - 1)
            elif event == Events.PRINT_STARTED:
                self._prints["started"] += 1
                self._extrusion_at_print_start = self._extrusion.total_mm
            elif event == Events.PRINT_DONE:
                self._prints["done"] += 1
                self._record_print_finished(payload)
            elif event == Events.PRINT_FAILED:
                self._prints["failed"] += 1
                self._record_print_finished(payload)
            elif event == Events.PRINT_CANCELLED:
                self._prints["cancelled"] += 1
                self._record_print_finished(payload)

    def _record_print_finished(self, payload):
        elapsed = (payload or {}).get("time")
        if elapsed is not None:
            self._last_print_time = elapsed
            self._print_time_total += elapsed
        self._last_print_extrusion = self._extrusion.total_mm - self._extrusion_at_print_start

    # ~~ Snapshot

    def build_snapshot(self):
        data = {}
        temperatures = {}
        try:
            data = self._printer.get_current_data() or {}
            temperatures = self._printer.get_current_temperatures() or {}
        except Exception:
            # A scrape must never take the endpoint down just because the
            # printer subsystem is mid-reconnect.
            self._logger.exception("Failed to read printer state for scrape")

        state = data.get("state") or {}
        progress = data.get("progress") or {}
        job = data.get("job") or {}

        with self._lock:
            return {
                "info": {
                    "octoprint_version": _octoprint_version(),
                    "plugin_version": _plugin_version(),
                    "python_version": platform.python_version(),
                    "hostname": socket.gethostname(),
                    "os": platform.system(),
                },
                "firmware": dict(self._firmware) if self._firmware else None,
                "mmu": self._mmu,
                "flags": dict(state.get("flags") or {}),
                "state_text": state.get("text"),
                "temperatures": _clean_temperatures(temperatures),
                "job": {
                    "completion": progress.get("completion"),
                    "print_time": progress.get("printTime"),
                    "print_time_left": progress.get("printTimeLeft"),
                    "estimated": job.get("estimatedPrintTime"),
                },
                "clients": self._clients,
                "fan_speed": self._fan_speed,
                "prints": dict(self._prints),
                "print_time_total": self._print_time_total,
                "extrusion_total_mm": self._extrusion.total_mm,
                "last_print_time": self._last_print_time,
                "last_print_extrusion_mm": self._last_print_extrusion,
            }


def _clean_temperatures(temperatures):
    """Keep only sensors OctoPrint reports as dicts with numeric readings."""
    cleaned = {}
    for sensor, values in (temperatures or {}).items():
        if isinstance(values, dict):
            cleaned[sensor] = {
                "actual": values.get("actual"),
                "target": values.get("target"),
            }
    return cleaned


def _octoprint_version():
    try:
        from octoprint import __version__

        return __version__
    except Exception:
        return "unknown"


def _plugin_version():
    try:
        from importlib.metadata import version

        return version("octoprint-prusa-metrics")
    except Exception:
        return "unknown"


def __plugin_load__():
    plugin = PrusaMetricsPlugin()

    global __plugin_implementation__
    __plugin_implementation__ = plugin

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.received": plugin.on_gcode_received,
        "octoprint.comm.protocol.gcode.sent": plugin.on_gcode_sent,
    }
