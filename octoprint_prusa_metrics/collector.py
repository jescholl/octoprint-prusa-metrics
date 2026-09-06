"""Builds Prometheus metric families from a plain snapshot dict.

Deliberately decoupled from OctoPrint: the plugin assembles the snapshot from
the PrinterInterface and hands it here, which keeps every metric shape unit
testable without a running server.
"""

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

PREFIX = "octoprint"

# Flags OctoPrint reports on the printer state. Emitted as a labelled 0/1 gauge
# rather than one series per state name, to keep cardinality flat.
STATE_FLAGS = (
    "operational",
    "printing",
    "paused",
    "pausing",
    "cancelling",
    "error",
    "ready",
    "closedOrError",
)

PRINT_RESULTS = ("started", "done", "failed", "cancelled")

AXES = ("X", "Y", "Z")


def _gauge(name, documentation, value, labels=None):
    if labels:
        family = GaugeMetricFamily(f"{PREFIX}_{name}", documentation, labels=list(labels.keys()))
        family.add_metric(list(labels.values()), value)
    else:
        family = GaugeMetricFamily(f"{PREFIX}_{name}", documentation, value=value)
    return family


def build_metrics(snapshot):
    """Yield prometheus_client metric families for a snapshot dict."""
    yield from _build_info(snapshot)
    yield from _build_state(snapshot)
    yield from _build_temperatures(snapshot)
    yield from _build_job(snapshot)
    yield from _build_counters(snapshot)


def _build_info(snapshot):
    info = snapshot.get("info") or {}
    if info:
        family = GaugeMetricFamily(
            f"{PREFIX}_info",
            "OctoPrint server build information.",
            labels=list(info.keys()),
        )
        family.add_metric([str(v) for v in info.values()], 1)
        yield family

    firmware = snapshot.get("firmware")
    if firmware:
        family = GaugeMetricFamily(
            f"{PREFIX}_printer_firmware_info",
            "Printer firmware as reported in the M115 response.",
            labels=list(firmware.keys()),
        )
        family.add_metric([str(v) for v in firmware.values()], 1)
        yield family

    mmu = snapshot.get("mmu")
    if mmu:
        family = GaugeMetricFamily(
            f"{PREFIX}_printer_mmu_info",
            "MMU firmware version, reassembled from the S0-S3 protocol reads the "
            "printer issues during MMU initialisation.",
            labels=list(mmu.keys()),
        )
        family.add_metric([str(v) for v in mmu.values()], 1)
        yield family


def _build_state(snapshot):
    flags = snapshot.get("flags") or {}
    family = GaugeMetricFamily(
        f"{PREFIX}_printer_flag",
        "Printer state flags, 1 when the flag is set.",
        labels=["flag"],
    )
    for flag in STATE_FLAGS:
        family.add_metric([flag], 1 if flags.get(flag) else 0)
    yield family

    state_text = snapshot.get("state_text")
    if state_text:
        yield _gauge(
            "printer_state",
            "Current printer state as a label, always 1.",
            1,
            {"state": str(state_text)},
        )

    yield _gauge(
        "connected_clients",
        "Number of currently connected OctoPrint clients.",
        snapshot.get("clients", 0),
    )

    fan_speed = snapshot.get("fan_speed")
    if fan_speed is not None:
        yield _gauge(
            "fan_speed",
            "Last commanded part-cooling fan speed (0-255).",
            fan_speed,
        )

    slice_progress = snapshot.get("slice_progress")
    if slice_progress is not None:
        yield _gauge(
            "slice_progress_percent",
            "Progress of the current slicing job, 0-100.",
            slice_progress,
        )


def _build_temperatures(snapshot):
    temperatures = snapshot.get("temperatures") or {}

    actual = GaugeMetricFamily(
        f"{PREFIX}_temperature_actual_celsius",
        "Measured temperature per sensor.",
        labels=["sensor"],
    )
    target = GaugeMetricFamily(
        f"{PREFIX}_temperature_target_celsius",
        "Target temperature per sensor.",
        labels=["sensor"],
    )

    for sensor, values in sorted(temperatures.items()):
        if not isinstance(values, dict):
            continue
        if values.get("actual") is not None:
            actual.add_metric([sensor], values["actual"])
        if values.get("target") is not None:
            target.add_metric([sensor], values["target"])

    yield actual
    yield target


def _build_job(snapshot):
    job = snapshot.get("job") or {}

    # Completion is absent between prints; emitting 0 would look like a stalled
    # print, so the series is simply omitted when there is no active job.
    if job.get("completion") is not None:
        yield _gauge(
            "job_completion_percent",
            "Completion of the current job, 0-100.",
            job["completion"],
        )
    if job.get("print_time") is not None:
        yield _gauge(
            "job_print_time_seconds",
            "Elapsed print time of the current job.",
            job["print_time"],
        )
    if job.get("print_time_left") is not None:
        yield _gauge(
            "job_print_time_left_seconds",
            "Estimated remaining print time of the current job.",
            job["print_time_left"],
        )
    if job.get("estimated") is not None:
        yield _gauge(
            "job_estimated_print_time_seconds",
            "Sliced estimate of total print time for the current job.",
            job["estimated"],
        )


def _build_counters(snapshot):
    prints = snapshot.get("prints") or {}
    family = CounterMetricFamily(
        f"{PREFIX}_prints",
        "Print jobs observed since plugin start, by outcome.",
        labels=["result"],
    )
    for result in PRINT_RESULTS:
        family.add_metric([result], prints.get(result, 0))
    yield family

    # prometheus_client appends "_total" to counter names, so these are named
    # without it to avoid rendering as e.g. "..._total_seconds_total".
    yield CounterMetricFamily(
        f"{PREFIX}_print_time_seconds",
        "Cumulative print time of completed jobs since plugin start.",
        value=snapshot.get("print_time_total", 0),
    )
    yield CounterMetricFamily(
        f"{PREFIX}_extrusion_mm",
        "Cumulative extruded filament length since plugin start.",
        value=snapshot.get("extrusion_total_mm", 0),
    )

    travel = snapshot.get("travel_total_mm") or {}
    family = CounterMetricFamily(
        f"{PREFIX}_travel_mm",
        "Cumulative axis travel since plugin start. Useful as a wear proxy for "
        "belts, bearings and lubrication intervals.",
        labels=["axis"],
    )
    for axis in AXES:
        family.add_metric([axis.lower()], travel.get(axis, 0))
    yield family

    yield CounterMetricFamily(
        f"{PREFIX}_timelapse_captures",
        "Timelapse frames captured (one per CaptureDone, not per movie).",
        value=snapshot.get("timelapse_captures", 0),
    )
    yield CounterMetricFamily(
        f"{PREFIX}_timelapse_renders",
        "Timelapse movies successfully rendered.",
        value=snapshot.get("timelapse_renders", 0),
    )

    last_print_time = snapshot.get("last_print_time")
    if last_print_time is not None:
        yield _gauge(
            "last_print_time_seconds",
            "Duration of the most recently completed print.",
            last_print_time,
        )

    # Live figures for the running print, and the frozen set from the last one.
    yield from _build_per_print(snapshot.get("current_print"), "print")
    yield from _build_per_print(snapshot.get("last_print"), "last_print")


def _build_per_print(totals, prefix):
    """Emit extrusion/travel for one print, live or most-recent."""
    if not totals:
        return

    if totals.get("extrusion") is not None:
        yield _gauge(
            f"{prefix}_extrusion_mm",
            "Filament extruded during this print."
            if prefix == "print"
            else "Filament extruded during the most recently completed print.",
            totals["extrusion"],
        )

    family = GaugeMetricFamily(
        f"{PREFIX}_{prefix}_travel_mm",
        "Axis travel during this print."
        if prefix == "print"
        else "Axis travel during the most recently completed print.",
        labels=["axis"],
    )
    for axis in AXES:
        family.add_metric([axis.lower()], totals.get(axis, 0))
    yield family
