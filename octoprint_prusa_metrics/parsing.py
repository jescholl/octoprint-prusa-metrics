"""Pure parsing helpers, kept free of OctoPrint imports so they can be tested
standalone."""

import re

# M115 responses are a flat run of ``KEY:value`` pairs where values may contain
# spaces, so the only reliable delimiter is the start of the *next* all-caps key.
_M115_KEY_RE = re.compile(r"\b(?P<key>[A-Z][A-Z0-9_]*):")

# The build-metadata charset includes "_" because Prusa firmware reports e.g.
# "3.14.1+8237_74a577bc0"; without it the version truncates mid-string to
# "3.14.1+8237", which is neither a clean version nor the full build id.
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z._]+)?)")

# MMU protocol responses are "<{command}{param} {status}{value}", e.g.
# ``<R8 A1`` (read register 8, Accepted, value 1) or ``<X0 E8008`` (command X0,
# Error, code 0x8008). All numbers are HEX -- 0x380 is build 896.
_MMU_RESPONSE_RE = re.compile(
    r"MMU\d*:<(?P<command>[A-Z])(?P<param>[0-9a-fA-F]*)\s+"
    r"(?P<status>[A-Z])(?P<value>[0-9a-fA-F]*)"
)

# Heater PWM duty, reported in every temperature line as "@:0 B@:0". The hotend
# pattern must not match the bed's "B@:", hence the lookbehind.
_HOTEND_PWM_RE = re.compile(r"(?<![A-Za-z])@:(\d+)")
_BED_PWM_RE = re.compile(r"\bB@:(\d+)")

# The base RepRap/Marlin line-numbering protocol asking for one line again --
# distinct from the MMU's own resend-shaped commands, which are addressed
# "MMU2:..." and never start a line with this word.
_RESEND_RE = re.compile(r"^resend\b", re.IGNORECASE)

_FAN_SET_RE = re.compile(r"^M106\b")
_FAN_OFF_RE = re.compile(r"^M107\b")
_E_PARAM_RE = re.compile(r"\bE(-?\d+(?:\.\d+)?)")
_S_PARAM_RE = re.compile(r"\bS(\d+(?:\.\d+)?)")
_MOVE_RE = re.compile(r"^G[01]\b")
_G92_RE = re.compile(r"^G92\b")
_G28_RE = re.compile(r"^G28\b")
_AXIS_RES = {axis: re.compile(rf"\b{axis}(-?\d+(?:\.\d+)?)") for axis in ("X", "Y", "Z")}
# G28 names axes with no value ("G28 X"), so presence alone must be detected.
_AXIS_PRESENT_RES = {axis: re.compile(rf"\b{axis}") for axis in ("X", "Y", "Z")}


def strip_gcode_comment(line):
    """Drop a trailing ``;`` comment and surrounding whitespace."""
    if line is None:
        return ""
    return line.split(";", 1)[0].strip()


def parse_m115(line):
    """Parse an M115 firmware-identification response into a dict.

    Returns an empty dict if the line carries no recognisable key/value pairs.
    """
    if not line:
        return {}

    matches = list(_M115_KEY_RE.finditer(line))
    if not matches:
        return {}

    fields = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        fields[match.group("key")] = line[start:end].strip()
    return fields


def extract_version(text):
    """Pull a dotted version out of a free-form string, e.g.
    ``Prusa-Firmware 3.14.1 based on Marlin`` -> ``3.14.1``."""
    if not text:
        return ""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else ""


def firmware_labels(fields):
    """Map raw M115 fields onto the stable label set the info metric exposes."""
    name = fields.get("FIRMWARE_NAME", "")
    return {
        "firmware_name": name,
        "firmware_version": extract_version(name),
        "machine_type": fields.get("MACHINE_TYPE", ""),
        "extruder_count": fields.get("EXTRUDER_COUNT", ""),
        "protocol_version": fields.get("PROTOCOL_VERSION", ""),
    }


# Registers the printer polls continuously, so their values arrive for free.
# Addresses from Prusa-Firmware Firmware/mmu2/registers.h.
MMU_REGISTERS = {
    0x04: "drive_errors",
    0x08: "finda",
    0x1A: "pulley_position",
    0x1B: "selector_slot",
    0x1C: "idler_slot",
}

# Slots run 0-4, plus 5 meaning "parked" for the selector and "disengaged" for
# the idler. The idler reads 5 for the whole of a normal print -- once filament
# is loaded the printer's own extruder pulls it and the MMU lets go -- so a
# disengaged idler alongside a selector sitting on a real slot is the expected
# steady state, not a disagreement between the two.
MMU_SLOT_PARKED = 5
MMU_SLOT_REGISTERS = ("selector_slot", "idler_slot")

# Protocol ErrorCode -> the 3-digit code shown on the LCD, which also forms the
# support URL (506 -> prusa.io/04506). Derived from Prusa-Firmware's
# mmu2_error_converter.cpp and mmu2/errors_list.h.
#
# The genuine TMC-driver faults (0x8200 TMC_IOIN_MISMATCH and up) are omitted:
# the firmware derives those from bit combinations rather than a lookup, and
# several can be raised at once. The homing and move failures below are not
# among them -- they carry the same per-axis bits but describe something
# physically obstructing the axis, which is the common MMU jam.
MMU_ERROR_LCD_CODES = {
    0x8001: 101,  # FINDA didn't trigger
    0x8002: 102,  # FINDA: filament stuck
    0x8003: 103,  # FSensor didn't trigger
    0x8004: 104,  # FSensor: filament stuck
    0x8005: 501,  # filament already loaded
    0x8006: 502,  # invalid tool
    0x8008: 506,  # FINDA vs EEPROM discrepancy -- "unload manually"
    0x8009: 106,  # FSensor triggered too early
    0x800A: 107,  # FINDA flickers -- inspect it
    0x800C: 507,  # filament ejected
    0x800D: 306,  # MMU MCU undervoltage
    0x8029: 508,  # filament change
    0x802A: 108,  # load to extruder failed
    0x802B: 503,  # queue full
    0x802C: 504,  # firmware update needed
    0x802D: 402,  # protocol/communication error
    0x802E: 401,  # MMU not responding
    0x802F: 505,  # firmware runtime error
    # HOMING_FAILED (0x8007) and MOVE_FAILED (0x800b) are always reported with
    # the bit of the axis that failed: pulley 0x40, selector 0x80, idler 0x100.
    0x8047: 105,  # pulley stalled -- StallGuard tripped during a pulley move
    0x804B: 105,  # pulley cannot move
    0x8087: 115,  # selector cannot home -- something is blocking it
    0x808B: 116,  # selector cannot move
    0x8107: 125,  # idler cannot home
    0x810B: 126,  # idler cannot move
}

# From Prusa-Firmware-MMU src/logic/progress_codes.h.
MMU_PROGRESS_CODES = {
    0: "OK",
    1: "EngagingIdler",
    2: "DisengagingIdler",
    3: "UnloadingToFinda",
    4: "UnloadingToPulley",
    5: "FeedingToFinda",
    6: "FeedingToBondtech",
    7: "FeedingToNozzle",
    8: "AvoidingGrind",
    9: "FinishingMoves",
    10: "ERRDisengagingIdler",
    11: "ERREngagingIdler",
    12: "ERRWaitingForUser",
    13: "ERRInternal",
    14: "ERRHelpingFilament",
    15: "ERRTMCFailed",
    16: "UnloadingFilament",
    17: "LoadingFilament",
    18: "SelectingFilamentSlot",
    19: "PreparingBlade",
    20: "PushingFilament",
    21: "PerformingCut",
    22: "ReturningSelector",
    23: "ParkingSelector",
    24: "EjectingFilament",
    25: "RetractingFromFinda",
    26: "Homing",
    27: "MovingSelector",
    28: "FeedingToFSensor",
    29: "HWTestBegin",
    30: "HWTestIdler",
    31: "HWTestSelector",
    32: "HWTestPulley",
    33: "HWTestCleanup",
    34: "HWTestExec",
    35: "HWTestDisplay",
    36: "ErrHwTestFailed",
    0xFF: "Empty",
}


def normalise_register(name, value):
    """Map a raw register read onto the value to publish, or None to drop it."""
    if name in MMU_SLOT_REGISTERS:
        # 0xff was observed on both slot registers during the real 04506 fault:
        # that is the protocol's "empty" sentinel, not a slot the MMU is on.
        return value if value <= MMU_SLOT_PARKED else None
    if name == "finda":
        # A uint8 whose only real values are 0 and 1. Its 0xff sentinel has to
        # be dropped rather than passed on, because every non-zero value is
        # truthy and would publish "unknown" as "filament detected".
        return value if value <= 1 else None
    if name == "drive_errors":
        # uint16, sentinel 0xffff. This one backs a Prometheus counter, so
        # publishing the sentinel would register as an enormous increase().
        return value if value < 0xFFFF else None
    if name == "pulley_position":
        # The MMU holds this as a signed int32 of millimetres and the register
        # read truncates it into a uint16, so a position behind the origin --
        # an unload retracting past it -- arrives as ~65500 rather than a small
        # negative. Net travel beyond 32.7m without an MMU power cycle would
        # alias, but every load is undone by its unload, so the axis oscillates
        # around the origin instead of accumulating that far.
        return value - 0x10000 if value > 0x7FFF else value
    return value


def mmu_error_url(lcd_code):
    """Support URL for an LCD error code, e.g. 506 -> https://prusa.io/04506."""
    return f"https://prusa.io/04{lcd_code:03d}"


class MmuTracker:
    """Tracks MMU state from the protocol chatter the printer already emits.

    Everything here is passive: the printer polls the MMU roughly once a second
    and those request/response pairs stream past on the serial line, so no
    command is ever sent to obtain any of it.

    Verified against a real MK3S+/MMU3: S0=3, S1=0, S2=3, S3=0x380 reassembles
    to 3.0.3 build 896, and register 0x08 tracked the FINDA state shown on the
    printer's own LCD during a real 04506 fault.
    """

    MAJOR, MINOR, REVISION, BUILD = 0, 1, 2, 3

    def __init__(self):
        self._parts = {}
        self.registers = {}
        self.error_code = None
        self.progress_code = None
        self.seen = False

    def feed(self, line):
        if not line:
            return
        match = _MMU_RESPONSE_RE.search(line)
        if not match:
            return

        self.seen = True
        command = match.group("command")
        status = match.group("status")
        raw_param = match.group("param")
        raw_value = match.group("value")
        value = int(raw_value, 16) if raw_value else None

        if status == "A" and command == "R" and raw_param:
            self.registers[int(raw_param, 16)] = value
        elif status == "A" and command == "S" and raw_param is not None:
            self._parts[int(raw_param, 16)] = value
        elif status == "E":
            self.error_code = value
            self.progress_code = None
        elif status == "P":
            self.progress_code = value
            self.error_code = None
        elif status == "F":
            # Command finished cleanly, so any previous error is resolved.
            self.error_code = None
            self.progress_code = None

    # ~~ Firmware version

    @property
    def version(self):
        """Dotted version, or None until major/minor/revision have all arrived."""
        needed = (self.MAJOR, self.MINOR, self.REVISION)
        if not all(part in self._parts for part in needed):
            return None
        return ".".join(str(self._parts[part]) for part in needed)

    @property
    def build(self):
        build = self._parts.get(self.BUILD)
        return str(build) if build is not None else None

    @property
    def version_labels(self):
        """Label set for the info metric, or None if the version is unknown."""
        version = self.version
        if version is None:
            return None
        return {"mmu_version": version, "mmu_build": self.build or ""}

    # ~~ Live state

    @property
    def named_registers(self):
        """Polled registers keyed by name, skipping any not yet seen and any
        whose raw value is a sentinel rather than a reading."""
        named = {}
        for address, name in MMU_REGISTERS.items():
            if address not in self.registers:
                continue
            value = normalise_register(name, self.registers[address])
            if value is not None:
                named[name] = value
        return named

    @property
    def error_labels(self):
        """Labels describing the current error, or None when there is none."""
        if self.error_code is None:
            return None
        lcd_code = MMU_ERROR_LCD_CODES.get(self.error_code)
        return {
            "code": f"0x{self.error_code:04x}",
            "lcd_code": f"04{lcd_code:03d}" if lcd_code else "",
            "url": mmu_error_url(lcd_code) if lcd_code else "",
        }

    @property
    def progress_labels(self):
        """Labels describing what the MMU is currently doing, or None."""
        if self.progress_code is None:
            return None
        return {
            "code": str(self.progress_code),
            "name": MMU_PROGRESS_CODES.get(self.progress_code, "Unknown"),
        }


def parse_heater_pwm(line):
    """Extract heater PWM duty from a temperature report line.

    Marlin reports these as ``@:`` (hotend) and ``B@:`` (bed); OctoPrint's own
    temperature API drops them, so they are read straight off the wire.
    """
    if not line:
        return {}
    pwm = {}
    hotend = _HOTEND_PWM_RE.search(line)
    if hotend:
        pwm["tool"] = float(hotend.group(1))
    bed = _BED_PWM_RE.search(line)
    if bed:
        pwm["bed"] = float(bed.group(1))
    return pwm


class ExtrusionTracker:
    """Accumulates total extruded filament from the outgoing gcode stream.

    Handles both absolute (``M82``) and relative (``M83``) extrusion, plus
    ``G92`` axis resets. PrusaSlicer emits relative E by default, but absolute
    is still valid input so both are tracked.
    """

    def __init__(self, relative=False):
        self.relative = relative
        self.total_mm = 0.0
        self._last_e = 0.0

    def feed(self, raw_line):
        line = strip_gcode_comment(raw_line).upper()
        if not line:
            return

        if line.startswith("M82"):
            self.relative = False
            return
        if line.startswith("M83"):
            self.relative = True
            return

        if _G92_RE.match(line):
            match = _E_PARAM_RE.search(line)
            # A bare G92 with no E resets all axes, E included.
            self._last_e = float(match.group(1)) if match else 0.0
            return

        if not _MOVE_RE.match(line):
            return

        match = _E_PARAM_RE.search(line)
        if not match:
            return
        value = float(match.group(1))

        if self.relative:
            delta = value
        else:
            delta = value - self._last_e
            self._last_e = value

        # Retractions are negative moves; they undo extrusion rather than
        # counting as filament consumed, so only positive deltas accumulate.
        if delta > 0:
            self.total_mm += delta


class MovementTracker:
    """Accumulates per-axis travel distance from the outgoing gcode stream.

    XYZ positioning mode is set by ``G90``/``G91``, which is independent of the
    ``M82``/``M83`` mode that governs extrusion, so this tracks its own state
    rather than sharing :class:`ExtrusionTracker`'s.

    Arc moves (``G2``/``G3``) are not counted; PrusaSlicer emits linear moves.
    """

    AXES = ("X", "Y", "Z")

    def __init__(self, relative=False):
        self.relative = relative
        self.totals = dict.fromkeys(self.AXES, 0.0)
        self._pos = dict.fromkeys(self.AXES, 0.0)

    @property
    def position(self):
        """Current commanded position per axis, relative to the last origin."""
        return dict(self._pos)

    def feed(self, raw_line):
        line = strip_gcode_comment(raw_line).upper()
        if not line:
            return

        if line.startswith("G90"):
            self.relative = False
            return
        if line.startswith("G91"):
            self.relative = True
            return

        if _G28_RE.match(line):
            # Homing travels an indeterminate distance, so reset the origin
            # without attributing any travel to it. A bare G28 (or Prusa's
            # "G28 W") homes every axis.
            named = [a for a in self.AXES if _AXIS_PRESENT_RES[a].search(line)]
            for axis in named or self.AXES:
                self._pos[axis] = 0.0
            return

        if _G92_RE.match(line):
            named = [a for a in self.AXES if _AXIS_RES[a].search(line)]
            if named:
                for axis in named:
                    self._pos[axis] = float(_AXIS_RES[axis].search(line).group(1))
            elif not _E_PARAM_RE.search(line):
                # Bare G92 resets everything; "G92 E0" must not touch XYZ.
                for axis in self.AXES:
                    self._pos[axis] = 0.0
            return

        if not _MOVE_RE.match(line):
            return

        for axis in self.AXES:
            match = _AXIS_RES[axis].search(line)
            if not match:
                continue
            value = float(match.group(1))
            if self.relative:
                delta = value
                self._pos[axis] += value
            else:
                delta = value - self._pos[axis]
                self._pos[axis] = value
            self.totals[axis] += abs(delta)


def is_resend_request(line):
    """True if a received line is the firmware asking to resend one gcode line.

    This is the base line-numbering handshake (``Resend: N``, following an
    ``Error:Line Number is not Last Line Number+1`` line) that every
    Marlin-derived firmware speaks, so it works the same on any printer --
    unlike the MMU's own protocol, which is Prusa-specific.
    """
    if not line:
        return False
    return bool(_RESEND_RE.match(line.strip()))


def parse_fan_speed(raw_line, current):
    """Return the fan speed (0-255) implied by a gcode line, else ``current``."""
    line = strip_gcode_comment(raw_line).upper()
    if not line:
        return current
    if _FAN_OFF_RE.match(line):
        return 0.0
    if _FAN_SET_RE.match(line):
        match = _S_PARAM_RE.search(line)
        # M106 with no S means "full speed" in Marlin.
        return float(match.group(1)) if match else 255.0
    return current
