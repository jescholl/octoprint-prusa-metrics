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

    yield from _build_mmu(snapshot)


def _build_mmu(snapshot):
    """MMU metrics. Entirely absent on printers without an MMU -- nothing here
    is required for the rest of the exposition to render."""
    # No MMU chatter ever observed means no MMU (or it is unpowered), so emit
    # nothing at all rather than a family full of empty labels.
    if not snapshot.get("mmu_seen"):
        return

    mmu = snapshot.get("mmu")
    if mmu:
        family = GaugeMetricFamily(
            f"{PREFIX}_mmu_info",
            "MMU firmware version, reassembled from the S0-S3 protocol reads the "
            "printer issues during MMU initialisation.",
            labels=list(mmu.keys()),
        )
        family.add_metric([str(v) for v in mmu.values()], 1)
        yield family

    registers = snapshot.get("mmu_registers") or {}

    # FINDA is the filament sensor in the MMU selector. Paired with the
    # extruder's own sensor it brackets the filament path, which is how a
    # blockage between the two gets localised.
    if "finda" in registers:
        yield _gauge(
            "mmu_finda",
            "MMU FINDA filament sensor: 1 when filament is detected in the selector.",
            1 if registers["finda"] else 0,
        )

    for name, documentation in (
        (
            "selector_slot",
            "Filament slot the MMU selector is currently on: 0-4, or 5 when it "
            "is parked. Absent while the MMU reports no slot at all.",
        ),
        (
            "idler_slot",
            "Filament slot the MMU idler is currently engaged with: 0-4, or 5 "
            "when it is disengaged. Reads 5 for the whole of a normal print, "
            "since the printer's own extruder pulls the filament once loaded.",
        ),
        (
            "pulley_position",
            "Filament driven through the MMU pulley, in mm, signed and "
            "cumulative since the MMU last powered on. Not a per-load figure.",
        ),
    ):
        if name in registers:
            yield _gauge(f"mmu_{name}", documentation, registers[name])

    if "drive_errors" in registers:
        yield CounterMetricFamily(
            f"{PREFIX}_mmu_drive_errors",
            "MMU drive errors (motor power rail voltage loss) from register "
            "0x04, counted by the MMU in its own EEPROM. Filament faults such "
            "as a FINDA or FSensor error do not appear here -- watch "
            "octoprint_mmu_error for those.",
            value=registers["drive_errors"],
        )

    error = snapshot.get("mmu_error")
    yield _labelled_state(
        "mmu_error",
        "Current MMU error. Absent when the MMU is not in an error state; the "
        "url label points at Prusa's page for the code.",
        error,
        ["code", "lcd_code", "url"],
    )

    progress = snapshot.get("mmu_progress")
    yield _labelled_state(
        "mmu_progress",
        "What the MMU is currently doing, as a progress code and its name. "
        "MMU operations last seconds, so at any realistic scrape interval this "
        "samples them rather than capturing them all; treat a gap as no "
        "information, not as an idle MMU.",
        progress,
        ["code", "name"],
    )


def _labelled_state(name, documentation, labels, label_names):
    """A gauge that exists, with label detail and a value of 1, only while the
    state it describes is set.

    Nothing is emitted when the state is absent, so alert on the series being
    present rather than on it being 0 -- there is no 0 to match. A resolved
    state stops being exported and goes stale on its own."""
    family = GaugeMetricFamily(f"{PREFIX}_{name}", documentation, labels=label_names)
    if labels:
        family.add_metric([str(labels.get(n, "")) for n in label_names], 1)
    return family


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

    # Heater duty cycle, read off the temperature line. A hotend working much
    # harder than usual to hold temperature is an early sign of a failing
    # heater, a draft, or a drifting thermistor.
    pwm = snapshot.get("heater_pwm") or {}
    if pwm:
        family = GaugeMetricFamily(
            f"{PREFIX}_heater_pwm",
            "Heater PWM duty as reported by the firmware (Marlin scale, 0-127).",
            labels=["heater"],
        )
        for heater, value in sorted(pwm.items()):
            family.add_metric([heater], value)
        yield family


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

    filament = snapshot.get("job_filament_mm")
    if filament is not None:
        yield _gauge(
            "job_filament_estimate_mm",
            "Filament the slicer estimates this job needs, summed across tools. "
            "Compare against octoprint_print_extrusion_mm for actual usage.",
            filament,
        )

    # Commanded position, tracked from the gcode stream.
    position = snapshot.get("position") or {}
    if position:
        family = GaugeMetricFamily(
            f"{PREFIX}_position_mm",
            "Current commanded axis position, relative to the last origin.",
            labels=["axis"],
        )
        for axis in AXES:
            if axis in position:
                family.add_metric([axis.lower()], position[axis])
        yield family


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
