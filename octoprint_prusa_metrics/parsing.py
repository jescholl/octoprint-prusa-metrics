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

# Prusa reports the MMU on its own line rather than inside M115, e.g.
# ``MMU2:Version 3.0.3`` or ``MMU2:Not responding``. Captured opportunistically;
# see README - this may never fire on a given firmware.
_MMU_RE = re.compile(r"\bMMU\d*:\s*(?P<value>.+)$")

_FAN_SET_RE = re.compile(r"^M106\b")
_FAN_OFF_RE = re.compile(r"^M107\b")
_E_PARAM_RE = re.compile(r"\bE(-?\d+(?:\.\d+)?)")
_S_PARAM_RE = re.compile(r"\bS(\d+(?:\.\d+)?)")
_MOVE_RE = re.compile(r"^G[01]\b")
_G92_RE = re.compile(r"^G92\b")


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


def parse_mmu_line(line):
    """Return the MMU descriptor from a firmware line, or ``None``."""
    if not line:
        return None
    match = _MMU_RE.search(line.strip())
    return match.group("value").strip() if match else None


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
