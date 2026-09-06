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
| `octoprint_printer_mmu_info` | gauge | MMU descriptor, only if the firmware reports one |
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
| `octoprint_prints_total{result}` | counter | `started`/`done`/`failed`/`cancelled` |
| `octoprint_print_time_seconds_total` | counter | Cumulative print time |
| `octoprint_extrusion_mm_total` | counter | Cumulative extruded filament |
| `octoprint_last_print_time_seconds` | gauge | Duration of last finished print |
| `octoprint_last_print_extrusion_mm` | gauge | Filament used by last finished print |

Counters reset when OctoPrint restarts; use `rate()`/`increase()`.

Job file names are deliberately **not** exposed as labels.

### Caveats

- **Firmware info appears only after the next printer connect.** OctoPrint
  issues `M115` as part of its own connection handshake and the plugin parses
  the reply as it streams past. It does not send `M115` itself, since that
  would break the read-only posture.
- **MMU firmware version may never populate.** The MMU talks to the Einsy
  board over its own UART sub-protocol rather than the `M115` exchange. The
  plugin captures an `MMU…:` line opportunistically if the firmware emits one;
  on a given firmware it may simply never appear.
- **Not collected:** Raspberry Pi core temperature (a host metric — use
  node_exporter), X/Y/Z travel distance, timelapse count, and slicer progress.

## Install

Install from a pinned tag archive:

```
pip install https://github.com/jescholl/octoprint-prusa-metrics/archive/refs/tags/v0.1.0.zip
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
