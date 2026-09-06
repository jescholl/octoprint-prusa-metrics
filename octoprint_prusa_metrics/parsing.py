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

# The printer queries the MMU's firmware version once during MMU init, as four
# separate protocol reads S0-S3 (major/minor/revision/build). Responses look
# like ``echo:MMU2:<S3 A380*d9.`` and the value is HEX -- 0x380 is build 896.
_MMU_VERSION_RE = re.compile(r"MMU\d*:<S(?P<index>[0-3])\s+A(?P<value>[0-9a-fA-F]+)")

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


class MmuVersionTracker:
    """Reassembles the MMU firmware version from the S0-S3 protocol reads.

    The printer issues these once per MMU initialisation, so the version only
    becomes known after an MMU startup observed while OctoPrint is connected --
    the same constraint that applies to the printer's own M115 response.

    Verified against a real MK3S+/MMU3: S0=3, S1=0, S2=3, S3=0x380 reassembles
    to 3.0.3 build 896, matching the firmware actually flashed to the unit.
    """

    MAJOR, MINOR, REVISION, BUILD = 0, 1, 2, 3

    def __init__(self):
        self._parts = {}

    def feed(self, line):
        if not line:
            return
        match = _MMU_VERSION_RE.search(line)
        if match:
            self._parts[int(match.group("index"))] = int(match.group("value"), 16)

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
    def labels(self):
        """Label set for the info metric, or None if the version is unknown."""
        version = self.version
        if version is None:
            return None
        return {"mmu_version": version, "mmu_build": self.build or ""}


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
