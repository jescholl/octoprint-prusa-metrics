# octoprint-prusa-metrics

Prometheus metrics and printer firmware-version reporting for OctoPrint.

Read-only by design: the plugin observes the gcode stream and the public
`PrinterInterface`. It never sends a command to the printer and adds no print
or control surface.

## Endpoint

```
GET /plugin/prusa_metrics/metrics
```

Unauthenticated, so that a Prometheus scraper with no way to present an
OctoPrint API key can reach it. Keep it on a trusted network; do not expose it
publicly.

## Metrics

| Metric | Type | Notes |
| --- | --- | --- |
| `octoprint_info` | gauge | OctoPrint/plugin/Python versions, hostname, OS |
| `octoprint_printer_firmware_info` | gauge | Firmware name/version, machine type, extruder count |
| `octoprint_heater_pwm{heater}` | gauge | Heater duty cycle, `tool`/`bed` |
| `octoprint_position_mm{axis}` | gauge | Current commanded position |
| `octoprint_job_filament_estimate_mm` | gauge | Slicer's filament estimate for the job |
| `octoprint_printer_flag{flag}` | gauge | `operational`, `printing`, `paused`, `error`, … |
| `octoprint_printer_state{state}` | gauge | Current state as a label |
| `octoprint_temperature_actual_celsius{sensor}` | gauge | Per tool and bed |
| `octoprint_temperature_target_celsius{sensor}` | gauge | Per tool and bed |
| `octoprint_job_completion_percent` | gauge | Omitted when idle |
| `octoprint_job_print_time_seconds` | gauge | Elapsed, current job |
| `octoprint_job_print_time_left_seconds` | gauge | Estimated remaining |
| `octoprint_job_estimated_print_time_seconds` | gauge | Sliced estimate |
| `octoprint_connected_clients` | gauge | Connected UI clients |
| `octoprint_fan_speed` | gauge | Last commanded part-cooling fan speed, 0-255 |
| `octoprint_slice_progress_percent` | gauge | Only while slicing |
| `octoprint_prints_total{result}` | counter | `started`/`done`/`failed`/`cancelled` |
| `octoprint_print_time_seconds_total` | counter | Cumulative print time |
| `octoprint_extrusion_mm_total` | counter | Cumulative extruded filament |
| `octoprint_travel_mm_total{axis}` | counter | Cumulative axis travel, `x`/`y`/`z` |
| `octoprint_timelapse_captures_total` | counter | Frames captured |
| `octoprint_timelapse_renders_total` | counter | Movies rendered |
| `octoprint_print_extrusion_mm` | gauge | Filament used **this** print; only while printing |
| `octoprint_print_travel_mm{axis}` | gauge | Axis travel **this** print; only while printing |
| `octoprint_last_print_time_seconds` | gauge | Duration of last finished print |
| `octoprint_last_print_extrusion_mm` | gauge | Filament used by last finished print |
| `octoprint_last_print_travel_mm{axis}` | gauge | Axis travel of last finished print |

### MMU metrics (Prusa MMU only)

Every metric below is **absent entirely** on printers without an MMU, and on an
MMU that is powered off or unreachable. Nothing here is required for the rest of
the exposition to render.

| Metric | Type | Notes |
| --- | --- | --- |
| `octoprint_mmu_info` | gauge | MMU firmware version and build |
| `octoprint_mmu_finda` | gauge | FINDA filament sensor in the selector, 0/1 |
| `octoprint_mmu_selector_slot` | gauge | Slot the selector is on (`5` = parked) |
| `octoprint_mmu_idler_slot` | gauge | Slot the idler is engaged with (`5` = disengaged, which is its state for all of a normal print) |
| `octoprint_mmu_pulley_position` | gauge | Filament driven through the pulley, mm, signed and cumulative since the MMU powered on |
| `octoprint_mmu_drive_errors_total` | counter | MMU drive errors (motor power rail voltage loss); filament faults are not counted here |
| `octoprint_mmu_error{code,lcd_code,url}` | gauge | Current error; `url` links to Prusa's page for the code |
| `octoprint_mmu_progress{code,name}` | gauge | What the MMU is doing, e.g. `FeedingToFinda` |

All of this is read passively from the request/response chatter the printer
already exchanges with the MMU about once a second — the plugin never sends a
command to obtain it.

The **extruder** filament sensor (`FS` on the printer's LCD) is deliberately
**not** collected: the printer does not report it continuously, and reading MMU
register `0x09` would mean sending commands. FINDA and the extruder sensor
bracket the filament path, so having only FINDA means you can detect filament in
the selector but not localise a blockage between the two.

Counters reset when OctoPrint restarts; use `rate()`/`increase()`.

Cumulative axis travel is a useful wear proxy for belts, bearings, and
lubrication intervals.

Job file names are deliberately **not** exposed as labels: a per-file label
would create a new timeseries for every model ever printed, and leak model
names into metrics.

### Caveats

- **Firmware info appears only after the next printer connect.** OctoPrint
  issues `M115` as part of its own connection handshake and the plugin parses
  the reply as it streams past. It does not send `M115` itself, since that
  would break the read-only posture.
- **MMU firmware version appears only after an MMU initialisation** observed
  while OctoPrint is connected. It does not come from `M115` — the printer
  reads it from the MMU as four separate protocol queries (`S0`-`S3` for
  major/minor/revision/build) during MMU startup, and the plugin reassembles
  them from the responses. The build number is transmitted in hex, so
  `<S3 A380` is build 896. Verified against a real MK3S+/MMU3.
- **Not collected:** Raspberry Pi core temperature. That is a host metric, not
  an OctoPrint one — use node_exporter, which also works when OctoPrint runs
  somewhere other than a Pi.
- **Arc moves (`G2`/`G3`) are not counted** toward axis travel; PrusaSlicer
  emits linear moves. Homing resets the origin without being attributed any
  travel, since the distance covered is indeterminate.

## Portability

The core metrics are firmware-agnostic and work on any Marlin-derived printer:
temperatures, heater PWM, state, job progress, print counters, travel,
extrusion, fan speed, position, and `M115` firmware info.

Printer-specific signals are strictly additive:

- **MMU metrics** appear only when MMU protocol traffic is observed.
- **Temperature sensors are enumerated, not hardcoded**, so a Prusa's ambient
  (`A`) and PINDA (`P`) sensors and all five MMU tool slots are picked up
  automatically, while a single-extruder printer simply reports fewer series.
  The two fields Marlin emits on its `M109`/`M190` heat-up line that are
  numbers but not temperatures -- `E`, the active extruder index, and `W`, the
  residency countdown -- are dropped.

## Install

Install from a pinned tag archive:

```
pip install https://github.com/jescholl/octoprint-prusa-metrics/archive/refs/tags/v0.4.0.zip
```

Inside the official `octoprint/octoprint` image, `PIP_USER=true` and
`PYTHONUSERBASE=/octoprint/plugins` mean this lands on the persistent volume.

## Development

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]" "octoprint==1.11.8"
.venv/bin/pytest
```

OctoPrint is a test-only dependency: `tests/test_plugin.py` runs against the
real plugin API so mixin/hook/event drift is caught, while `parsing.py` and
`collector.py` import without it.

Releases are tagged from the `version` in `pyproject.toml`, so bump it in the
same commit as the change being released.
