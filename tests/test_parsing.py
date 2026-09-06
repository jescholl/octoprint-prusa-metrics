from octoprint_prusa_metrics.parsing import (
    ExtrusionTracker,
    MovementTracker,
    extract_version,
    firmware_labels,
    parse_fan_speed,
    parse_m115,
    parse_mmu_line,
    strip_gcode_comment,
)

# Shape of the real response from the MK3S+ on firmware 3.14.1.
PRUSA_M115 = (
    "FIRMWARE_NAME:Prusa-Firmware 3.14.1 based on Marlin "
    "FIRMWARE_URL:https://github.com/prusa3d/Prusa-Firmware "
    "PROTOCOL_VERSION:1.0 MACHINE_TYPE:Prusa i3 MK3S EXTRUDER_COUNT:1 "
    "UUID:00000000-0000-0000-0000-000000000000"
)


class TestParseM115:
    def test_parses_all_fields(self):
        fields = parse_m115(PRUSA_M115)
        assert fields["FIRMWARE_NAME"] == "Prusa-Firmware 3.14.1 based on Marlin"
        assert fields["MACHINE_TYPE"] == "Prusa i3 MK3S"
        assert fields["EXTRUDER_COUNT"] == "1"
        assert fields["PROTOCOL_VERSION"] == "1.0"

    def test_url_value_is_not_split_on_its_own_colon(self):
        fields = parse_m115(PRUSA_M115)
        assert fields["FIRMWARE_URL"] == "https://github.com/prusa3d/Prusa-Firmware"

    def test_returns_empty_for_unrelated_line(self):
        assert parse_m115("ok T:210.0 /210.0 B:60.0 /60.0") != {}
        assert parse_m115("wait") == {}
        assert parse_m115("") == {}
        assert parse_m115(None) == {}


class TestExtractVersion:
    def test_pulls_version_out_of_firmware_name(self):
        assert extract_version("Prusa-Firmware 3.14.1 based on Marlin") == "3.14.1"

    def test_handles_two_component_and_suffixed_versions(self):
        assert extract_version("Marlin 2.0") == "2.0"
        assert extract_version("Prusa-Firmware 3.14.1-RC1") == "3.14.1-RC1"

    def test_keeps_full_build_metadata(self):
        # Real string observed from the MK3S+: the build id contains an
        # underscore, which previously truncated this to "3.14.1+8237".
        assert (
            extract_version("Prusa-Firmware 3.14.1+8237_74a577bc0 based on Marlin")
            == "3.14.1+8237_74a577bc0"
        )

    def test_empty_when_absent(self):
        assert extract_version("Prusa-Firmware") == ""
        assert extract_version("") == ""


class TestFirmwareLabels:
    def test_maps_onto_stable_label_set(self):
        labels = firmware_labels(parse_m115(PRUSA_M115))
        assert labels["firmware_version"] == "3.14.1"
        assert labels["machine_type"] == "Prusa i3 MK3S"
        assert labels["extruder_count"] == "1"

    def test_missing_fields_become_empty_strings(self):
        labels = firmware_labels({})
        assert set(labels.values()) == {""}

    def test_real_printer_response(self):
        # Captured verbatim from the MK3S+ connection handshake.
        real = (
            "FIRMWARE_NAME:Prusa-Firmware 3.14.1+8237_74a577bc0 based on Marlin "
            "FIRMWARE_URL:https://github.com/prusa3d/Prusa-Firmware "
            "PROTOCOL_VERSION:1.0 MACHINE_TYPE:Prusa i3 MK3S EXTRUDER_COUNT:1"
        )
        labels = firmware_labels(parse_m115(real))
        assert labels["firmware_version"] == "3.14.1+8237_74a577bc0"
        assert labels["firmware_name"] == "Prusa-Firmware 3.14.1+8237_74a577bc0 based on Marlin"
        assert labels["machine_type"] == "Prusa i3 MK3S"
        assert labels["extruder_count"] == "1"


class TestParseMmuLine:
    def test_extracts_descriptor(self):
        assert parse_mmu_line("MMU2:Version 3.0.3") == "Version 3.0.3"

    def test_ignores_unrelated_lines(self):
        assert parse_mmu_line("ok") is None
        assert parse_mmu_line("") is None
        assert parse_mmu_line(None) is None


class TestStripGcodeComment:
    def test_removes_trailing_comment(self):
        assert strip_gcode_comment("G1 E5 ; prime") == "G1 E5"

    def test_handles_none_and_blank(self):
        assert strip_gcode_comment(None) == ""
        assert strip_gcode_comment("   ") == ""


class TestExtrusionTracker:
    def test_relative_mode_accumulates_each_move(self):
        tracker = ExtrusionTracker()
        tracker.feed("M83")
        tracker.feed("G1 X10 E2.5")
        tracker.feed("G1 X20 E2.5")
        assert tracker.total_mm == 5.0

    def test_absolute_mode_accumulates_deltas(self):
        tracker = ExtrusionTracker()
        tracker.feed("M82")
        tracker.feed("G1 X10 E5")
        tracker.feed("G1 X20 E12")
        assert tracker.total_mm == 12.0

    def test_retraction_does_not_reduce_total(self):
        tracker = ExtrusionTracker()
        tracker.feed("M83")
        tracker.feed("G1 E5")
        tracker.feed("G1 E-2")
        assert tracker.total_mm == 5.0

    def test_g92_resets_absolute_origin_without_inflating_total(self):
        tracker = ExtrusionTracker()
        tracker.feed("M82")
        tracker.feed("G1 E10")
        tracker.feed("G92 E0")
        tracker.feed("G1 E3")
        assert tracker.total_mm == 13.0

    def test_bare_g92_resets_e_to_zero(self):
        tracker = ExtrusionTracker()
        tracker.feed("M82")
        tracker.feed("G1 E10")
        tracker.feed("G92")
        tracker.feed("G1 E4")
        assert tracker.total_mm == 14.0

    def test_ignores_non_move_commands_and_comments(self):
        tracker = ExtrusionTracker()
        tracker.feed("M83")
        tracker.feed("M104 S210")
        tracker.feed("; E5 in a comment")
        tracker.feed("G1 E1 ; real move")
        assert tracker.total_mm == 1.0

    def test_mode_switch_mid_stream(self):
        tracker = ExtrusionTracker()
        tracker.feed("M83")
        tracker.feed("G1 E2")
        tracker.feed("M82")
        tracker.feed("G92 E0")
        tracker.feed("G1 E3")
        assert tracker.total_mm == 5.0


class TestMovementTracker:
    def test_absolute_moves_accumulate_distance(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 X10 Y20")
        t.feed("G1 X30 Y20")
        assert t.totals["X"] == 30.0
        assert t.totals["Y"] == 20.0

    def test_relative_moves_accumulate(self):
        t = MovementTracker()
        t.feed("G91")
        t.feed("G1 X5")
        t.feed("G1 X5")
        assert t.totals["X"] == 10.0

    def test_negative_moves_count_as_distance_travelled(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 X10")
        t.feed("G1 X0")
        assert t.totals["X"] == 20.0

    def test_z_tracked_independently(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 Z0.2")
        t.feed("G1 Z0.4")
        assert round(t.totals["Z"], 6) == 0.4
        assert t.totals["X"] == 0.0

    def test_homing_resets_origin_without_adding_travel(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 X50")
        t.feed("G28")
        t.feed("G1 X10")
        # 50 to get there, then 10 from the new origin -- homing itself is not
        # attributed any distance.
        assert t.totals["X"] == 60.0

    def test_prusa_g28_w_homes_all_axes(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 X50 Y50")
        t.feed("G28 W")
        t.feed("G1 X5 Y5")
        assert t.totals["X"] == 55.0
        assert t.totals["Y"] == 55.0

    def test_partial_homing_only_resets_named_axis(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 X50 Y50")
        t.feed("G28 X")
        t.feed("G1 X10 Y50")
        assert t.totals["X"] == 60.0
        assert t.totals["Y"] == 50.0

    def test_g92_e0_does_not_reset_xyz(self):
        # The single most common G92 in slicer output; it must not be mistaken
        # for a positional reset.
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 X10")
        t.feed("G92 E0")
        t.feed("G1 X20")
        assert t.totals["X"] == 20.0

    def test_g92_with_axis_sets_origin(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 X10")
        t.feed("G92 X0")
        t.feed("G1 X5")
        assert t.totals["X"] == 15.0

    def test_bare_g92_resets_all_axes(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G1 X10 Y10")
        t.feed("G92")
        t.feed("G1 X1 Y1")
        assert t.totals["X"] == 11.0
        assert t.totals["Y"] == 11.0

    def test_non_move_and_feedrate_only_lines_ignored(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("M104 S210")
        t.feed("G1 F1800")
        t.feed("; G1 X99 in a comment")
        assert t.totals["X"] == 0.0

    def test_arc_moves_are_not_counted(self):
        t = MovementTracker()
        t.feed("G90")
        t.feed("G2 X10 Y10 I5 J5")
        assert t.totals["X"] == 0.0


class TestParseFanSpeed:
    def test_m106_sets_speed(self):
        assert parse_fan_speed("M106 S128", 0.0) == 128.0

    def test_m106_without_s_is_full_speed(self):
        assert parse_fan_speed("M106", 0.0) == 255.0

    def test_m107_turns_fan_off(self):
        assert parse_fan_speed("M107", 255.0) == 0.0

    def test_unrelated_command_keeps_current(self):
        assert parse_fan_speed("G1 X10", 128.0) == 128.0
        assert parse_fan_speed("", 128.0) == 128.0
