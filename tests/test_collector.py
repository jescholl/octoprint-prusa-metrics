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
        "mmu": "Version 3.0.3",
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
        "prints": {"started": 5, "done": 3, "failed": 1, "cancelled": 1},
        "print_time_total": 9000,
        "extrusion_total_mm": 1234.5,
        "last_print_time": 3000,
        "last_print_extrusion_mm": 400.0,
    }


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
        assert 'octoprint_printer_mmu_info{mmu="Version 3.0.3"} 1.0' in render(snapshot)

    def test_mmu_info_omitted_when_unknown(self, snapshot):
        snapshot["mmu"] = None
        assert "octoprint_printer_mmu_info" not in render(snapshot)

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

    def test_no_file_path_labels_are_emitted(self, snapshot):
        # Job file names can carry personal/model detail; deliberately excluded.
        output = render(snapshot)
        assert "path=" not in output
        assert "filename=" not in output
