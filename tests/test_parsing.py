from octoprint_prusa_metrics.parsing import (
    ExtrusionTracker,
    MmuTracker,
    MovementTracker,
    extract_version,
    firmware_labels,
    parse_fan_speed,
    parse_heater_pwm,
    parse_m115,
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


class TestMmuTracker:
    # Captured verbatim from the MK3S+/MMU3 serial stream. Values are hex:
    # S3 A380 -> 0x380 -> build 896, which matches the firmware on the unit.
    REAL_EXCHANGE = [
        "echo:MMU2:<S0 A3*22.",
        "echo:MMU2:<S1 A0*34.",
        "echo:MMU2:<S2 A3*70.",
        "echo:MMU2:<S3 A380*d9.",
    ]

    def test_reassembles_real_printer_version(self):
        t = MmuTracker()
        for line in self.REAL_EXCHANGE:
            t.feed(line)
        assert t.version == "3.0.3"
        assert t.build == "896"
        assert t.version_labels == {"mmu_version": "3.0.3", "mmu_build": "896"}

    def test_build_is_parsed_as_hex_not_decimal(self):
        t = MmuTracker()
        t.feed("echo:MMU2:<S3 A380*d9.")
        assert t.build == "896"

    def test_version_unknown_until_all_parts_arrive(self):
        t = MmuTracker()
        t.feed("echo:MMU2:<S0 A3*22.")
        t.feed("echo:MMU2:<S1 A0*34.")
        assert t.version is None
        assert t.version_labels is None
        t.feed("echo:MMU2:<S2 A3*70.")
        assert t.version == "3.0.3"

    def test_version_available_without_build(self):
        t = MmuTracker()
        for line in self.REAL_EXCHANGE[:3]:
            t.feed(line)
        assert t.version_labels == {"mmu_version": "3.0.3", "mmu_build": ""}

    def test_ignores_other_mmu_traffic(self):
        t = MmuTracker()
        # Ordinary polling and the error state seen during the 04506 fault.
        for line in ("echo:MMU2:<X0 E8008*1b.", "echo:MMU2:<R8 A1*98.", "ok", ""):
            t.feed(line)
        assert t.version is None

    def test_handles_none(self):
        t = MmuTracker()
        t.feed(None)
        assert t.version is None


class TestMmuLiveState:
    """All input here is verbatim from the MK3S+/MMU3 serial capture taken
    during a real 04506 fault and after it was cleared."""

    FAULT_CYCLE = [
        "echo:MMU2:<X0 E8008*1b.",
        "echo:MMU2:<R8 A1*98.",
        "echo:MMU2:<R1b Aff*bf.",
        "echo:MMU2:<R1c Aff*60.",
        "echo:MMU2:<R4 A0*66.",
        "echo:MMU2:<R1a A0*41.",
    ]

    def feed_all(self, lines):
        t = MmuTracker()
        for line in lines:
            t.feed(line)
        return t

    def test_registers_captured_by_name(self):
        t = self.feed_all(self.FAULT_CYCLE)
        # Both slot registers read 0xff during the fault -- the protocol's
        # "empty" sentinel, so they are dropped rather than published as slots.
        assert t.named_registers == {
            "finda": 1,
            "drive_errors": 0,
            "pulley_position": 0,
        }

    def test_sentinel_slots_are_dropped_but_kept_in_the_raw_registers(self):
        t = self.feed_all(self.FAULT_CYCLE)
        assert t.registers[0x1B] == 0xFF
        assert "selector_slot" not in t.named_registers

    def test_slot_registers_publish_the_parked_value(self):
        t = self.feed_all(["echo:MMU2:<R1b A5*29.", "echo:MMU2:<R1c A4*f6."])
        assert t.named_registers["selector_slot"] == 5
        assert t.named_registers["idler_slot"] == 4

    def test_pulley_position_behind_the_origin_reads_negative(self):
        # The MMU truncates a signed int32 of mm into the uint16 register, so
        # -20mm arrives as 0xffec.
        t = self.feed_all(["echo:MMU2:<R1a Affec*41."])
        assert t.named_registers["pulley_position"] == -20

    def test_pulley_position_forward_is_unchanged(self):
        # 0x1dd is the 477mm load observed on the real printer.
        t = self.feed_all(["echo:MMU2:<R1a A1dd*41."])
        assert t.named_registers["pulley_position"] == 477

    def test_finda_matches_the_lcd_during_the_real_fault(self):
        # The printer's LCD showed "FI:1" at this moment.
        assert self.feed_all(self.FAULT_CYCLE).named_registers["finda"] == 1

    def test_error_code_maps_to_lcd_code_and_url(self):
        t = self.feed_all(self.FAULT_CYCLE)
        assert t.error_code == 0x8008
        assert t.error_labels == {
            "code": "0x8008",
            "lcd_code": "04506",
            "url": "https://prusa.io/04506",
        }

    def test_unmapped_error_still_reports_raw_code(self):
        t = MmuTracker()
        t.feed("echo:MMU2:<X0 Edef*aa.")
        assert t.error_labels["code"] == "0x0def"
        assert t.error_labels["lcd_code"] == ""
        assert t.error_labels["url"] == ""

    def test_healthy_registers_after_the_fault_cleared(self):
        # Post-fix the slot registers read 5 (parked) instead of 0xff.
        t = self.feed_all(["echo:MMU2:<R1b A5*29.", "echo:MMU2:<R1c A5*f6."])
        assert t.named_registers["selector_slot"] == 5
        assert t.named_registers["idler_slot"] == 5

    def test_axis_jam_errors_carry_a_support_url(self):
        # HOMING_FAILED/MOVE_FAILED are reported with the failing axis bit set;
        # these are physical obstructions, not TMC driver faults.
        for line, lcd in (
            ("echo:MMU2:<X0 E8087*aa.", "04115"),  # selector cannot home
            ("echo:MMU2:<X0 E808b*aa.", "04116"),  # selector cannot move
            ("echo:MMU2:<X0 E8107*aa.", "04125"),  # idler cannot home
            ("echo:MMU2:<X0 E810b*aa.", "04126"),  # idler cannot move
            ("echo:MMU2:<X0 E8047*aa.", "04105"),  # pulley stalled
        ):
            t = MmuTracker()
            t.feed(line)
            assert t.error_labels["lcd_code"] == lcd, line
            assert t.error_labels["url"] == f"https://prusa.io/{lcd}"

    def test_progress_code_is_named(self):
        t = MmuTracker()
        t.feed("echo:MMU2:<T0 P1a*3f.")
        assert t.progress_labels == {"code": "26", "name": "Homing"}

    def test_progress_clears_a_previous_error(self):
        t = self.feed_all(self.FAULT_CYCLE)
        assert t.error_code == 0x8008
        t.feed("echo:MMU2:<T0 P5*aa.")
        assert t.error_code is None
        assert t.progress_labels["name"] == "FeedingToFinda"

    def test_progress_names_match_the_firmware_enum(self):
        t = MmuTracker()
        t.feed("echo:MMU2:<T0 P6*aa.")
        assert t.progress_labels["name"] == "FeedingToBondtech"
        t.feed("echo:MMU2:<T0 P24*aa.")
        assert t.progress_labels["name"] == "ErrHwTestFailed"

    def test_finished_clears_error_and_progress(self):
        t = self.feed_all(self.FAULT_CYCLE)
        t.feed("echo:MMU2:<T0 F0*aa.")
        assert t.error_code is None
        assert t.progress_code is None
        assert t.error_labels is None

    def test_seen_flag_distinguishes_no_mmu_from_healthy_mmu(self):
        assert MmuTracker().seen is False
        assert self.feed_all(self.FAULT_CYCLE).seen is True

    def test_rejected_command_is_not_an_error_code(self):
        # The U0 rejection loop must not be reported as an MMU error.
        t = MmuTracker()
        t.feed("echo:MMU2:<U0 R*ce.")
        assert t.error_code is None
        assert t.error_labels is None


class TestParseHeaterPwm:
    # Verbatim temperature line from the printer.
    REAL_LINE = "T:18.0 /0.0 B:17.9 /0.0 T0:18.0 /0.0 @:0 B@:0 P:0.0 A:26.4"

    def test_parses_both_heaters(self):
        assert parse_heater_pwm(self.REAL_LINE) == {"tool": 0.0, "bed": 0.0}

    def test_bed_pwm_is_not_confused_with_hotend(self):
        pwm = parse_heater_pwm("T:210 /210 B:60 /60 @:127 B@:64")
        assert pwm == {"tool": 127.0, "bed": 64.0}

    def test_line_without_pwm_yields_nothing(self):
        assert parse_heater_pwm("T:210.0 /210.0 B:60.0 /60.0") == {}
        assert parse_heater_pwm("") == {}
        assert parse_heater_pwm(None) == {}


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
