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
from .parsing import (
    ExtrusionTracker,
    MmuTracker,
    MovementTracker,
    firmware_labels,
    parse_fan_speed,
    parse_heater_pwm,
    parse_m115,
)

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
    octoprint.plugin.ProgressPlugin,
    octoprint.plugin.BlueprintPlugin,
):
    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._registry = CollectorRegistry()
        self._firmware = None
        self._mmu = MmuTracker()
        self._heater_pwm = {}
        self._clients = 0
        self._fan_speed = 0.0
        self._extrusion = ExtrusionTracker()
        self._movement = MovementTracker()
        self._prints = {"started": 0, "done": 0, "failed": 0, "cancelled": 0}
        self._print_time_total = 0.0
        self._last_print_time = None
        self._timelapse_captures = 0
        self._timelapse_renders = 0
        self._slice_progress = None
        # Per-print figures are derived by diffing the lifetime totals against
        # a baseline captured at PrintStarted, so there is only ever one source
        # of truth for each quantity.
        self._printing = False
        self._print_baseline = self._totals()
        self._last_print_totals = None

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

        if "MMU" in line:
            with self._lock:
                self._mmu.feed(line)

        # Heater PWM rides along in every temperature report.
        if "@:" in line:
            pwm = parse_heater_pwm(line)
            if pwm:
                with self._lock:
                    self._heater_pwm = pwm

        return line

    def on_gcode_sent(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
        if not cmd:
            return
        with self._lock:
            self._extrusion.feed(cmd)
            self._movement.feed(cmd)
            self._fan_speed = parse_fan_speed(cmd, self._fan_speed)

    # ~~ ProgressPlugin

    def on_slicing_progress(
        self,
        slicer,
        source_location,
        source_path,
        destination_location,
        destination_path,
        progress,
    ):
        # Deliberately unlabelled by path: a per-file label would add a new
        # timeseries for every model ever sliced, and leak model names.
        with self._lock:
            self._slice_progress = progress

    # ~~ EventHandlerPlugin

    def on_event(self, event, payload):
        with self._lock:
            if event == Events.CLIENT_OPENED:
                self._clients += 1
            elif event == Events.CLIENT_CLOSED:
                self._clients = max(0, self._clients - 1)
            elif event == Events.PRINT_STARTED:
                self._prints["started"] += 1
                self._printing = True
                self._print_baseline = self._totals()
            elif event == Events.PRINT_DONE:
                self._prints["done"] += 1
                self._record_print_finished(payload)
            elif event == Events.PRINT_FAILED:
                self._prints["failed"] += 1
                self._record_print_finished(payload)
            elif event == Events.PRINT_CANCELLED:
                self._prints["cancelled"] += 1
                self._record_print_finished(payload)
            elif event == Events.CAPTURE_DONE:
                # One timelapse *frame*, not a finished movie.
                self._timelapse_captures += 1
            elif event == Events.MOVIE_DONE:
                self._timelapse_renders += 1

    def _totals(self):
        """Lifetime counters that per-print figures are diffed against."""
        totals = {"extrusion": self._extrusion.total_mm}
        totals.update(self._movement.totals)
        return totals

    def _since_baseline(self):
        current = self._totals()
        return {k: current[k] - self._print_baseline.get(k, 0.0) for k in current}

    def _record_print_finished(self, payload):
        elapsed = (payload or {}).get("time")
        if elapsed is not None:
            self._last_print_time = elapsed
            self._print_time_total += elapsed
        self._last_print_totals = self._since_baseline()
        self._printing = False

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
                "mmu": self._mmu.version_labels,
                "mmu_registers": self._mmu.named_registers,
                "mmu_error": self._mmu.error_labels,
                "mmu_progress": self._mmu.progress_labels,
                "mmu_seen": self._mmu.seen,
                "heater_pwm": dict(self._heater_pwm),
                "position": self._movement.position,
                "job_filament_mm": _job_filament_mm(job),
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
                "slice_progress": self._slice_progress,
                "prints": dict(self._prints),
                "print_time_total": self._print_time_total,
                "extrusion_total_mm": self._extrusion.total_mm,
                "travel_total_mm": dict(self._movement.totals),
                "timelapse_captures": self._timelapse_captures,
                "timelapse_renders": self._timelapse_renders,
                "last_print_time": self._last_print_time,
                # Live per-print figures only while a print is running; the
                # frozen last_print_* set is what remains between prints.
                "current_print": self._since_baseline() if self._printing else None,
                "last_print": dict(self._last_print_totals) if self._last_print_totals else None,
            }


def _job_filament_mm(job):
    """Total estimated filament for the current job, summed across tools."""
    filament = (job or {}).get("filament")
    if not isinstance(filament, dict):
        return None
    total = 0.0
    found = False
    for entry in filament.values():
        if isinstance(entry, dict) and entry.get("length") is not None:
            total += entry["length"]
            found = True
    return total if found else None


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
