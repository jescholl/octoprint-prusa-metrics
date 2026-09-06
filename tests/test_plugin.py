"""Integration tests against the real OctoPrint plugin API.

These import octoprint proper, so they catch mixin/hook/event drift that a
stubbed test would silently pass over.
"""

import logging

import pytest
from octoprint.events import Events
from prometheus_client import CollectorRegistry, generate_latest

from octoprint_prusa_metrics import plugin as plugin_module
from octoprint_prusa_metrics.collector import build_metrics
from octoprint_prusa_metrics.plugin import PrusaMetricsPlugin


def render_snapshot(snapshot):
    """Render a snapshot through the real collector, as a scrape would."""
    registry = CollectorRegistry()

    class _Collector:
        def collect(self):
            return build_metrics(snapshot)

    registry.register(_Collector())
    return generate_latest(registry).decode()


PRUSA_M115 = (
    "FIRMWARE_NAME:Prusa-Firmware 3.14.1 based on Marlin "
    "FIRMWARE_URL:https://github.com/prusa3d/Prusa-Firmware "
    "PROTOCOL_VERSION:1.0 MACHINE_TYPE:Prusa i3 MK3S EXTRUDER_COUNT:1"
)


class FakePrinter:
    def __init__(self, data=None, temperatures=None, raises=False):
        self._data = data or {}
        self._temperatures = temperatures or {}
        self._raises = raises

    def get_current_data(self):
        if self._raises:
            raise RuntimeError("printer is reconnecting")
        return self._data

    def get_current_temperatures(self):
        if self._raises:
            raise RuntimeError("printer is reconnecting")
        return self._temperatures


@pytest.fixture
def plugin():
    instance = PrusaMetricsPlugin()
    instance._logger = logging.getLogger("test")
    instance._printer = FakePrinter(
        data={
            "state": {"text": "Printing", "flags": {"operational": True, "printing": True}},
            "progress": {"completion": 50.0, "printTime": 600, "printTimeLeft": 600},
            "job": {"estimatedPrintTime": 1200},
        },
        temperatures={"tool0": {"actual": 215.0, "target": 215.0}},
    )
    return instance


class TestPluginRegistration:
    def test_plugin_load_wires_implementation_and_hooks(self):
        plugin_module.__plugin_load__()
        assert isinstance(plugin_module.__plugin_implementation__, PrusaMetricsPlugin)
        hooks = plugin_module.__plugin_hooks__
        assert "octoprint.comm.protocol.gcode.received" in hooks
        assert "octoprint.comm.protocol.gcode.sent" in hooks

    def test_metrics_route_is_registered_on_the_blueprint(self, plugin):
        rules = plugin.metrics_endpoint._blueprint_rules
        assert rules["metrics_endpoint"] == [("/metrics", {"methods": ["GET"]})]

    def test_endpoint_is_unauthenticated_for_prometheus(self, plugin):
        assert plugin.is_blueprint_protected() is False
        assert plugin.is_blueprint_csrf_protected() is False


class TestFirmwareCapture:
    def test_m115_response_populates_firmware(self, plugin):
        plugin.on_gcode_received(None, PRUSA_M115)
        assert plugin.build_snapshot()["firmware"]["firmware_version"] == "3.14.1"

    def test_hook_returns_line_unmodified(self, plugin):
        assert plugin.on_gcode_received(None, PRUSA_M115) == PRUSA_M115
        assert plugin.on_gcode_received(None, "ok") == "ok"

    def test_firmware_absent_before_any_m115(self, plugin):
        assert plugin.build_snapshot()["firmware"] is None

    def test_mmu_version_captured_from_protocol_reads(self, plugin):
        # Verbatim from the MK3S+/MMU3 init exchange.
        for line in (
            "echo:MMU2:<S0 A3*22.",
            "echo:MMU2:<S1 A0*34.",
            "echo:MMU2:<S2 A3*70.",
            "echo:MMU2:<S3 A380*d9.",
        ):
            plugin.on_gcode_received(None, line)
        assert plugin.build_snapshot()["mmu"] == {
            "mmu_version": "3.0.3",
            "mmu_build": "896",
        }

    def test_mmu_absent_before_init_exchange(self, plugin):
        assert plugin.build_snapshot()["mmu"] is None


class TestWithoutMmu:
    """A printer with no MMU, or one that is powered off/disconnected, must
    degrade to 'no MMU metric' rather than erroring anywhere."""

    # A plain MK3S reports one extruder and no PRUSA_MMU2 capability.
    NON_MMU_M115 = (
        "FIRMWARE_NAME:Prusa-Firmware 3.14.1 based on Marlin "
        "PROTOCOL_VERSION:1.0 MACHINE_TYPE:Prusa i3 MK3S EXTRUDER_COUNT:1"
    )

    @pytest.fixture
    def no_mmu_plugin(self):
        instance = PrusaMetricsPlugin()
        instance._logger = logging.getLogger("test")
        instance._printer = FakePrinter(
            data={"state": {"text": "Operational", "flags": {"operational": True}}},
            # Single tool, no MMU slots.
            temperatures={
                "tool0": {"actual": 21.0, "target": 0.0},
                "bed": {"actual": 20.0, "target": 0.0},
            },
        )
        return instance

    def test_no_mmu_traffic_yields_no_mmu_metric_and_no_error(self, no_mmu_plugin):
        no_mmu_plugin.on_gcode_received(None, self.NON_MMU_M115)
        snapshot = no_mmu_plugin.build_snapshot()
        assert snapshot["mmu"] is None
        output = render_snapshot(snapshot)
        assert "octoprint_mmu_" not in output
        # Everything else still reports.
        assert 'firmware_version="3.14.1"' in output
        assert 'octoprint_temperature_actual_celsius{sensor="tool0"} 21.0' in output
        assert 'octoprint_printer_state{state="Operational"} 1.0' in output

    def test_mmu_erroring_does_not_produce_a_version(self, no_mmu_plugin):
        # An MMU that is present but faulted answers the state poll with an
        # error and never completes the S0-S3 version exchange.
        for line in (
            "echo:MMU2:<X0 E8008*1b.",
            "echo:MMU2:Command Error, last bytes: 00 00 58",
            "echo:MMU2:<R8 A1*98.",
        ):
            no_mmu_plugin.on_gcode_received(None, line)
        snapshot = no_mmu_plugin.build_snapshot()
        assert snapshot["mmu"] is None
        assert "octoprint_mmu_info" not in render_snapshot(snapshot)

    def test_partial_version_exchange_is_not_reported(self, no_mmu_plugin):
        # MMU powered off midway: some replies arrive, the rest never do.
        no_mmu_plugin.on_gcode_received(None, "echo:MMU2:<S0 A3*22.")
        no_mmu_plugin.on_gcode_received(None, "echo:MMU2:<S1 A0*34.")
        assert no_mmu_plugin.build_snapshot()["mmu"] is None

    def test_mmu_named_gcode_file_is_not_mistaken_for_a_version(self, no_mmu_plugin):
        # Real filenames on this printer embed "MMU3"; the M20 listing must not
        # be parsed as MMU protocol traffic.
        no_mmu_plugin.on_gcode_received(
            None, 'CUPHOL~1.GCO 10487787 0x50d94ec7 "cupholder_MK3SMMU3_7h56m.gcode"'
        )
        assert no_mmu_plugin.build_snapshot()["mmu"] is None

    def test_full_render_is_stable_with_no_mmu(self, no_mmu_plugin):
        # The whole exposition must render cleanly start to finish.
        output = render_snapshot(no_mmu_plugin.build_snapshot())
        for expected in (
            "octoprint_info",
            "octoprint_printer_flag",
            "octoprint_temperature_actual_celsius",
            "octoprint_prints_total",
            "octoprint_travel_mm_total",
            "octoprint_timelapse_captures_total",
        ):
            assert expected in output


class TestGcodeSent:
    def test_extrusion_accumulates(self, plugin):
        plugin.on_gcode_sent(None, "sending", "M83", None, "M83")
        plugin.on_gcode_sent(None, "sending", "G1 E4", None, "G1")
        assert plugin.build_snapshot()["extrusion_total_mm"] == 4.0

    def test_fan_speed_tracked(self, plugin):
        plugin.on_gcode_sent(None, "sending", "M106 S200", None, "M106")
        assert plugin.build_snapshot()["fan_speed"] == 200.0

    def test_empty_command_is_ignored(self, plugin):
        plugin.on_gcode_sent(None, "sending", None, None, None)
        assert plugin.build_snapshot()["extrusion_total_mm"] == 0.0


class TestEvents:
    def test_client_count_tracks_open_and_close(self, plugin):
        plugin.on_event(Events.CLIENT_OPENED, {})
        plugin.on_event(Events.CLIENT_OPENED, {})
        plugin.on_event(Events.CLIENT_CLOSED, {})
        assert plugin.build_snapshot()["clients"] == 1

    def test_client_count_never_goes_negative(self, plugin):
        plugin.on_event(Events.CLIENT_CLOSED, {})
        assert plugin.build_snapshot()["clients"] == 0

    def test_print_outcomes_counted(self, plugin):
        plugin.on_event(Events.PRINT_STARTED, {})
        plugin.on_event(Events.PRINT_DONE, {"time": 120})
        plugin.on_event(Events.PRINT_FAILED, {"time": 30})
        plugin.on_event(Events.PRINT_CANCELLED, {"time": 10})
        snapshot = plugin.build_snapshot()
        assert snapshot["prints"] == {
            "started": 1,
            "done": 1,
            "failed": 1,
            "cancelled": 1,
        }
        assert snapshot["print_time_total"] == 160
        assert snapshot["last_print_time"] == 10

    def test_per_print_extrusion_is_scoped_to_the_print(self, plugin):
        plugin.on_gcode_sent(None, "sending", "M83", None, "M83")
        plugin.on_gcode_sent(None, "sending", "G1 E10", None, "G1")
        plugin.on_event(Events.PRINT_STARTED, {})
        plugin.on_gcode_sent(None, "sending", "G1 E7", None, "G1")
        plugin.on_event(Events.PRINT_DONE, {"time": 60})
        snapshot = plugin.build_snapshot()
        assert snapshot["extrusion_total_mm"] == 17.0
        assert snapshot["last_print"]["extrusion"] == 7.0
        # No longer printing, so the live set is withdrawn.
        assert snapshot["current_print"] is None

    def test_live_per_print_figures_while_printing(self, plugin):
        plugin.on_gcode_sent(None, "sending", "M83", None, "M83")
        plugin.on_gcode_sent(None, "sending", "G90", None, "G90")
        plugin.on_gcode_sent(None, "sending", "G1 X5 E2", None, "G1")
        plugin.on_event(Events.PRINT_STARTED, {})
        plugin.on_gcode_sent(None, "sending", "G1 X15 E3", None, "G1")
        snapshot = plugin.build_snapshot()
        assert snapshot["current_print"]["extrusion"] == 3.0
        assert snapshot["current_print"]["X"] == 10.0
        # Lifetime totals still include the pre-print movement.
        assert snapshot["extrusion_total_mm"] == 5.0
        assert snapshot["travel_total_mm"]["X"] == 15.0

    def test_timelapse_counters(self, plugin):
        plugin.on_event(Events.CAPTURE_DONE, {})
        plugin.on_event(Events.CAPTURE_DONE, {})
        plugin.on_event(Events.MOVIE_DONE, {})
        snapshot = plugin.build_snapshot()
        assert snapshot["timelapse_captures"] == 2
        assert snapshot["timelapse_renders"] == 1

    def test_slicing_progress_recorded(self, plugin):
        plugin.on_slicing_progress("cura", "local", "in.stl", "local", "out.gcode", 42)
        assert plugin.build_snapshot()["slice_progress"] == 42

    def test_print_done_without_time_payload(self, plugin):
        plugin.on_event(Events.PRINT_DONE, {})
        assert plugin.build_snapshot()["last_print_time"] is None


class TestSnapshot:
    def test_reads_printer_state(self, plugin):
        snapshot = plugin.build_snapshot()
        assert snapshot["state_text"] == "Printing"
        assert snapshot["flags"]["printing"] is True
        assert snapshot["job"]["completion"] == 50.0
        assert snapshot["temperatures"]["tool0"]["actual"] == 215.0

    def test_survives_printer_errors(self, plugin):
        plugin._printer = FakePrinter(raises=True)
        snapshot = plugin.build_snapshot()
        # Degrades to empty printer state rather than failing the scrape.
        assert snapshot["state_text"] is None
        assert snapshot["info"]["octoprint_version"] != ""

    def test_reports_octoprint_version(self, plugin):
        assert plugin.build_snapshot()["info"]["octoprint_version"] == "1.11.8"
