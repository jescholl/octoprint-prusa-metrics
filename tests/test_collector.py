import pytest
from prometheus_client import CollectorRegistry, generate_latest

from octoprint_prusa_metrics.collector import build_metrics


@pytest.fixture
def snapshot():
    return {
        "info": {"octoprint_version": "1.11.8", "python_version": "3.10.18"},
        "firmware": {
            "firmware_name": "Prusa-Firmware 3.14.1 based on Marlin",
            "firmware_version": "3.14.1",
            "machine_type": "Prusa i3 MK3S",
        },
        "mmu": {"mmu_version": "3.0.3", "mmu_build": "896"},
        "mmu_seen": True,
        "mmu_registers": {
            "finda": 1,
            "selector_slot": 2,
            "idler_slot": 2,
            "pulley_position": 17,
            "drive_errors": 3,
        },
        "mmu_error": {
            "code": "0x8008",
            "lcd_code": "04506",
            "url": "https://prusa.io/04506",
        },
        "mmu_progress": {"code": "5", "name": "FeedingToFinda"},
        "heater_pwm": {"tool": 127.0, "bed": 64.0},
        "position": {"X": 100.0, "Y": 50.0, "Z": 0.2},
        "job_filament_mm": 8500.0,
        "flags": {"operational": True, "printing": True, "error": False},
        "state_text": "Printing",
        "temperatures": {
            "tool0": {"actual": 214.9, "target": 215.0},
            "bed": {"actual": 59.8, "target": 60.0},
        },
        "job": {
            "completion": 42.5,
            "print_time": 1800,
            "print_time_left": 2400,
            "estimated": 4200,
        },
        "clients": 2,
        "fan_speed": 128.0,
        "slice_progress": 60.0,
        "prints": {"started": 5, "done": 3, "failed": 1, "cancelled": 1},
        "print_time_total": 9000,
        "extrusion_total_mm": 1234.5,
        "travel_total_mm": {"X": 5000.0, "Y": 4000.0, "Z": 300.0},
        "timelapse_captures": 12,
        "timelapse_renders": 2,
        "last_print_time": 3000,
        "current_print": {"extrusion": 120.0, "X": 900.0, "Y": 800.0, "Z": 40.0},
        "last_print": {"extrusion": 400.0, "X": 2000.0, "Y": 1800.0, "Z": 90.0},
    }


def has_metric(output, name):
    """True if a real sample line for the metric exists.

    Line-anchored on purpose: a bare substring check also matches metric
    names mentioned inside another metric's HELP text.
    """
    return any(
        line.split("{")[0].split(" ")[0] == name
        for line in output.splitlines()
        if not line.startswith("#")
    )


def render(snapshot):
    registry = CollectorRegistry()

    class _Collector:
        def collect(self):
            return build_metrics(snapshot)

    registry.register(_Collector())
    return generate_latest(registry).decode()


class TestExposition:
    def test_renders_without_error(self, snapshot):
        assert render(snapshot)

    def test_firmware_version_is_exposed_as_a_label(self, snapshot):
        output = render(snapshot)
        assert 'firmware_version="3.14.1"' in output
        assert 'machine_type="Prusa i3 MK3S"' in output

    def test_mmu_info_exposed_when_present(self, snapshot):
        output = render(snapshot)
        assert 'mmu_version="3.0.3"' in output
        assert 'mmu_build="896"' in output

    def test_mmu_info_omitted_when_unknown(self, snapshot):
        snapshot["mmu"] = None
        assert "octoprint_mmu_info" not in render(snapshot)

    def test_temperatures_per_sensor(self, snapshot):
        output = render(snapshot)
        assert 'octoprint_temperature_actual_celsius{sensor="tool0"} 214.9' in output
        assert 'octoprint_temperature_target_celsius{sensor="bed"} 60.0' in output

    def test_state_flags_emit_zero_and_one(self, snapshot):
        output = render(snapshot)
        assert 'octoprint_printer_flag{flag="printing"} 1.0' in output
        assert 'octoprint_printer_flag{flag="error"} 0.0' in output

    def test_unreported_flag_defaults_to_zero(self, snapshot):
        assert 'octoprint_printer_flag{flag="paused"} 0.0' in render(snapshot)

    def test_job_metrics(self, snapshot):
        output = render(snapshot)
        assert "octoprint_job_completion_percent 42.5" in output
        assert "octoprint_job_print_time_left_seconds 2400.0" in output

    def test_job_metrics_omitted_when_idle(self, snapshot):
        snapshot["job"] = {
            "completion": None,
            "print_time": None,
            "print_time_left": None,
            "estimated": None,
        }
        output = render(snapshot)
        assert "octoprint_job_completion_percent" not in output
        # Temperatures still report while idle.
        assert "octoprint_temperature_actual_celsius" in output

    def test_print_counters_by_result(self, snapshot):
        output = render(snapshot)
        assert 'octoprint_prints_total{result="done"} 3.0' in output
        assert 'octoprint_prints_total{result="failed"} 1.0' in output

    def test_counters_present_at_zero_on_fresh_start(self):
        output = render({"prints": {}, "flags": {}})
        assert 'octoprint_prints_total{result="started"} 0.0' in output
        assert "octoprint_extrusion_mm_total 0.0" in output
        assert "octoprint_print_time_seconds_total 0.0" in output

    def test_empty_snapshot_does_not_raise(self):
        assert render({})

    def test_axis_travel_counters(self, snapshot):
        output = render(snapshot)
        assert 'octoprint_travel_mm_total{axis="x"} 5000.0' in output
        assert 'octoprint_travel_mm_total{axis="z"} 300.0' in output

    def test_timelapse_counters(self, snapshot):
        output = render(snapshot)
        assert "octoprint_timelapse_captures_total 12.0" in output
        assert "octoprint_timelapse_renders_total 2.0" in output

    def test_slice_progress(self, snapshot):
        assert "octoprint_slice_progress_percent 60.0" in render(snapshot)

    def test_slice_progress_omitted_when_not_slicing(self, snapshot):
        snapshot["slice_progress"] = None
        assert "octoprint_slice_progress_percent" not in render(snapshot)

    def test_live_per_print_figures(self, snapshot):
        output = render(snapshot)
        assert "octoprint_print_extrusion_mm 120.0" in output
        assert 'octoprint_print_travel_mm{axis="x"} 900.0' in output

    def test_live_per_print_figures_withdrawn_between_prints(self, snapshot):
        snapshot["current_print"] = None
        output = render(snapshot)
        assert not has_metric(output, "octoprint_print_extrusion_mm")
        assert not has_metric(output, "octoprint_print_travel_mm")
        # The frozen set from the previous print remains.
        assert "octoprint_last_print_extrusion_mm 400.0" in output

    def test_last_print_figures(self, snapshot):
        output = render(snapshot)
        assert "octoprint_last_print_extrusion_mm 400.0" in output
        assert 'octoprint_last_print_travel_mm{axis="y"} 1800.0' in output

    def test_no_file_path_labels_are_emitted(self, snapshot):
        # Job file names can carry personal/model detail; deliberately excluded.
        output = render(snapshot)
        assert "path=" not in output
        assert "filename=" not in output


class TestNewSignals:
    def test_finda_and_slots(self, snapshot):
        output = render(snapshot)
        assert "octoprint_mmu_finda 1.0" in output
        assert "octoprint_mmu_selector_slot 2.0" in output
        assert "octoprint_mmu_idler_slot 2.0" in output

    def test_slot_help_explains_the_parked_and_disengaged_value(self, snapshot):
        output = render(snapshot)
        assert "or 5 when it is parked" in output
        assert "or 5 when it is disengaged" in output

    def test_mmu_error_carries_code_and_support_url(self, snapshot):
        output = render(snapshot)
        assert 'lcd_code="04506"' in output
        assert 'url="https://prusa.io/04506"' in output

    def test_mmu_error_exports_no_sample_when_healthy(self, snapshot):
        snapshot["mmu_error"] = None
        output = render(snapshot)
        # The family is declared, but carries no sample -- so an alert has to
        # match on the series existing, not on it being 0.
        assert "# TYPE octoprint_mmu_error gauge" in output
        assert "octoprint_mmu_error{" not in output

    def test_mmu_progress_named(self, snapshot):
        assert 'name="FeedingToFinda"' in render(snapshot)

    def test_mmu_drive_error_counter(self, snapshot):
        output = render(snapshot)
        assert "octoprint_mmu_drive_errors_total 3.0" in output
        # The old name promised general MMU errors, which this register is not.
        assert "octoprint_mmu_errors_total" not in output

    def test_heater_pwm(self, snapshot):
        output = render(snapshot)
        assert 'octoprint_heater_pwm{heater="tool"} 127.0' in output
        assert 'octoprint_heater_pwm{heater="bed"} 64.0' in output

    def test_position(self, snapshot):
        output = render(snapshot)
        assert 'octoprint_position_mm{axis="x"} 100.0' in output
        assert 'octoprint_position_mm{axis="z"} 0.2' in output

    def test_job_filament_estimate(self, snapshot):
        assert "octoprint_job_filament_estimate_mm 8500.0" in render(snapshot)

    def test_no_mmu_family_at_all_without_an_mmu(self, snapshot):
        # The single most important portability guarantee.
        snapshot["mmu_seen"] = False
        output = render(snapshot)
        assert "octoprint_mmu" not in output
        # Non-MMU signals are unaffected.
        assert "octoprint_heater_pwm" in output
        assert "octoprint_temperature_actual_celsius" in output

    def test_optional_signals_omitted_when_absent(self):
        output = render({"prints": {}, "flags": {}})
        for absent in (
            "octoprint_heater_pwm",
            "octoprint_position_mm",
            "octoprint_job_filament_estimate_mm",
            "octoprint_mmu",
        ):
            assert absent not in output
