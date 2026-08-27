"""State extraction, byte-level diffing, and field annotations for EA 81 frames.

Compares two 27-byte ``EA 81`` state captures byte-by-byte and annotates which
positions correspond to known, hypothesized, or unknown fields. Used to decode
the IOTBT DeviceState2 layout by probing the lamp at different physical states
and observing which bytes change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .framing import candidate_payloads
from .protocol import classify_packet

# ---------------------------------------------------------------------------
# Field annotations for the 27-byte EA 81 state frame
# ---------------------------------------------------------------------------

CONFIRMED = "confirmed"
HYPOTHESIS = "hypothesis"
EXTENDED_HYPOTHESIS = "extended-hypothesis"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FieldAnnotation:
    """One annotated field position in the EA 81 state frame."""

    offset: int
    name: str
    confidence: str  # one of the constants above
    description: str


# Annotations derived from the codebase's current understanding and
# verified by state-diff captures on 2026-08-26.
# - "confirmed" fields are verified by at least two independent capture pairs.
# - "hypothesis" fields are documented in NOTICE.md/CAPTURE_WORKFLOW.md but
#   not yet verified for the DX23012.
# - "extended-hypothesis" fields are decoded in the extended-custom codec for
#   known models (0x77/0x82/0xB6) and may NOT apply to the IOTBT layout.
# - "unknown" fields have no current interpretation.
EA81_FIELDS: list[FieldAnnotation] = [
    FieldAnnotation(0, "signature", CONFIRMED, "EA frame marker"),
    FieldAnnotation(1, "signature", CONFIRMED, "81 frame marker"),
    FieldAnnotation(2, "version?", UNKNOWN, "always 0x01 in captures so far"),
    FieldAnnotation(3, "mesh_addr_low", HYPOTHESIS, "IOTBT mesh address (low byte)"),
    FieldAnnotation(4, "mesh_addr_high", HYPOTHESIS, "IOTBT mesh address (high byte)"),
    FieldAnnotation(5, "mode", HYPOTHESIS, "IOTBT mode byte"),
    FieldAnnotation(6, "power", CONFIRMED, "0x23=on, 0x24=off"),
    FieldAnnotation(7, "color_white_mode", CONFIRMED, "0x24=color mode, 0x25=white mode"),
    FieldAnnotation(8, "color_temp", CONFIRMED, "0x01=color mode, 0x6f=white mode"),
    FieldAnnotation(9, "static_09", UNKNOWN, "always 0x50 in captures (static)"),
    FieldAnnotation(10, "static_10", UNKNOWN, "always 0xf0 in captures (static)"),
    FieldAnnotation(11, "hue", CONFIRMED, "HSV hue 0-180 scale (0=red, 120=blue)"),
    FieldAnnotation(12, "saturation", CONFIRMED, "HSV saturation 0-100 (always 100 in captures)"),
    FieldAnnotation(13, "brightness", CONFIRMED, "brightness 0-100 percentage (25=0x19, 100=0x64)"),
    FieldAnnotation(14, "static_14", UNKNOWN, "always 0x00 in captures (static)"),
    FieldAnnotation(
        15, "white_level", EXTENDED_HYPOTHESIS, "white percent in extended-custom codec"
    ),
    FieldAnnotation(16, "static_16", UNKNOWN, "always 0x05 in captures (static)"),
    FieldAnnotation(17, "static_17", UNKNOWN, "always 0x00 in captures (static)"),
    FieldAnnotation(18, "led_count?", EXTENDED_HYPOTHESIS, "LED count in extended-custom codec"),
    FieldAnnotation(19, "static_19", UNKNOWN, "always 0xff in captures (static)"),
    FieldAnnotation(20, "static_20", UNKNOWN, "always 0xff in captures (static)"),
    FieldAnnotation(21, "static_21", UNKNOWN, "always 0xff in captures (static)"),
    FieldAnnotation(22, "static_22", UNKNOWN, "always 0xff in captures (static)"),
    FieldAnnotation(23, "static_23", UNKNOWN, "always 0x01 in captures (static)"),
    FieldAnnotation(24, "static_24", UNKNOWN, "always 0x00 in captures (static)"),
    FieldAnnotation(25, "static_25", UNKNOWN, "always 0x00 in captures (static)"),
    FieldAnnotation(26, "static_26", UNKNOWN, "always 0x00 in captures (static)"),
]

_FIELD_BY_OFFSET: dict[int, FieldAnnotation] = {f.offset: f for f in EA81_FIELDS}


# ---------------------------------------------------------------------------
# State extraction from JSONL captures
# ---------------------------------------------------------------------------


def extract_state_from_capture(path: str | Path) -> bytes | None:
    """Extract the most recent EA 81 state from a JSONL probe capture.

    Looks first for a ``resolution`` record (which carries ``source_packet_hex``),
    then falls back to classifying notification payloads.
    """
    resolved = Path(path).expanduser().resolve()
    state_hex: str | None = None
    notification_hex: str | None = None

    with resolved.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            record: dict[str, Any] = json.loads(line)
            if record.get("direction") == "resolution" and record.get("source_packet_hex"):
                state_hex = record["source_packet_hex"]
            elif record.get("direction") == "rx" and record.get("note") == "notification":
                notification_hex = record.get("payload_hex")

    if state_hex:
        return bytes.fromhex(state_hex)

    if notification_hex:
        packet = bytes.fromhex(notification_hex)
        for candidate, _description in candidate_payloads(packet):
            classified = classify_packet(candidate)
            if classified and classified[0].frame_family == "extended-ea81":
                return candidate

    return None


# ---------------------------------------------------------------------------
# Byte-level diffing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ByteDiff:
    """One byte position in the comparison."""

    offset: int
    value_a: int
    value_b: int
    changed: bool
    field_name: str
    field_confidence: str
    field_description: str


@dataclass(frozen=True, slots=True)
class StateDiff:
    """Complete diff result between two EA 81 state frames."""

    state_a_hex: str
    state_b_hex: str
    length_a: int
    length_b: int
    bytes: list[ByteDiff] = field(default_factory=list)

    @property
    def changed_offsets(self) -> list[int]:
        return [b.offset for b in self.bytes if b.changed]

    @property
    def changed_fields(self) -> list[str]:
        seen: list[str] = []
        for b in self.bytes:
            if b.changed and b.field_name not in seen:
                seen.append(b.field_name)
        return seen


def diff_states(state_a: bytes, state_b: bytes) -> StateDiff:
    """Compare two EA 81 state frames byte-by-byte with field annotations."""
    max_len = max(len(state_a), len(state_b))
    byte_diffs: list[ByteDiff] = []
    for offset in range(max_len):
        value_a = state_a[offset] if offset < len(state_a) else -1
        value_b = state_b[offset] if offset < len(state_b) else -1
        annotation = _FIELD_BY_OFFSET.get(offset)
        if annotation is None:
            byte_diffs.append(
                ByteDiff(offset, value_a, value_b, value_a != value_b, "beyond_frame", UNKNOWN, "")
            )
        else:
            byte_diffs.append(
                ByteDiff(
                    offset,
                    value_a,
                    value_b,
                    value_a != value_b,
                    annotation.name,
                    annotation.confidence,
                    annotation.description,
                )
            )
    return StateDiff(
        state_a_hex=state_a.hex(),
        state_b_hex=state_b.hex(),
        length_a=len(state_a),
        length_b=len(state_b),
        bytes=byte_diffs,
    )


def format_diff(diff: StateDiff, *, label_a: str = "A", label_b: str = "B") -> str:
    """Format a StateDiff as a human-readable table for the CLI."""
    lines: list[str] = []
    lines.append(f"  {label_a}: {diff.state_a_hex}")
    lines.append(f"  {label_b}: {diff.state_b_hex}")
    lines.append("")
    lines.append(f"  Length: {diff.length_a} vs {diff.length_b} bytes")
    changed = diff.changed_offsets
    lines.append(f"  Changed positions: {changed if changed else 'none'}")
    lines.append("")

    if not changed:
        lines.append("  No bytes changed — the two states are identical.")
        return "\n".join(lines)

    lines.append("  Changed fields:")
    for offset in changed:
        byte_diff = diff.bytes[offset]
        confidence_tag = f"[{byte_diff.field_confidence}]"
        desc = f"  {byte_diff.field_description}" if byte_diff.field_description else ""
        lines.append(
            f"    pos {offset:2d}  "
            f"0x{byte_diff.value_a:02x} → 0x{byte_diff.value_b:02x}  "
            f"{byte_diff.field_name}  {confidence_tag}{desc}"
        )

    lines.append("")
    lines.append("  Full byte map:")
    lines.append(f"    {'Pos':>3}  {label_a:>6}  {label_b:>6}  Δ  Field                Confidence")
    for byte_diff in diff.bytes:
        marker = "*" if byte_diff.changed else " "
        value_a_str = f"{byte_diff.value_a:02x}" if byte_diff.value_a >= 0 else "--"
        value_b_str = f"{byte_diff.value_b:02x}" if byte_diff.value_b >= 0 else "--"
        lines.append(
            f"    {byte_diff.offset:3d}  {value_a_str:>6}  {value_b_str:>6}  {marker}  "
            f"{byte_diff.field_name:<20s}  {byte_diff.field_confidence}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Batch analysis of multiple captures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSummary:
    """Aggregate analysis of one byte position across many captures."""

    offset: int
    field_name: str
    field_confidence: str
    field_description: str
    distinct_values: tuple[int, ...]
    change_count: int
    is_static: bool


@dataclass(frozen=True, slots=True)
class BatchAnalysis:
    """Full field map produced from multiple state captures."""

    states: list[bytes]
    field_summaries: list[FieldSummary]

    @property
    def static_offsets(self) -> list[int]:
        return [s.offset for s in self.field_summaries if s.is_static]

    @property
    def dynamic_offsets(self) -> list[int]:
        return [s.offset for s in self.field_summaries if not s.is_static]


def extract_all_states_from_capture(path: str | Path) -> list[bytes]:
    """Extract all EA 81 states from a JSONL capture file.

    Handles both probe captures (resolution records with source_packet_hex)
    and watch captures (state/transition records with state_hex).
    """
    resolved = Path(path).expanduser().resolve()
    states: list[bytes] = []
    seen: set[str] = set()

    with resolved.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            record: dict[str, Any] = json.loads(line)
            hex_str: str | None = None
            if record.get("direction") == "resolution" and record.get("source_packet_hex"):
                hex_str = record["source_packet_hex"]
            elif record.get("direction") in ("state", "transition") and record.get("state_hex"):
                hex_str = record["state_hex"]
            elif record.get("direction") == "rx" and record.get("note") == "notification":
                # Fall back to classifying notification payloads, but only
                # accept decoded 27-byte EA 81 frames (not raw wrapped transport).
                notification_hex = record.get("payload_hex")
                if notification_hex:
                    packet = bytes.fromhex(notification_hex)
                    for candidate, _description in candidate_payloads(packet):
                        if len(candidate) == 27 and candidate[:2] == b"\xea\x81":
                            classified = classify_packet(candidate)
                            if classified and classified[0].frame_family == "extended-ea81":
                                hex_str = candidate.hex()
                                break

            if hex_str and hex_str not in seen:
                states.append(bytes.fromhex(hex_str))
                seen.add(hex_str)

    return states


def collect_states_from_captures(paths: list[str | Path]) -> list[bytes]:
    """Extract all EA 81 states from multiple JSONL capture files.

    Handles both probe captures (one state per file) and watch captures
    (multiple states per file). Deduplicates identical states.
    """
    all_states: list[bytes] = []
    seen: set[str] = set()
    for path in paths:
        for state in extract_all_states_from_capture(path):
            hex_str = state.hex()
            if hex_str not in seen:
                all_states.append(state)
                seen.add(hex_str)
    return all_states


def analyze_states(states: list[bytes]) -> BatchAnalysis:
    """Analyze multiple state captures to produce a full field map."""
    if not states:
        return BatchAnalysis(states=[], field_summaries=[])

    max_len = max(len(s) for s in states)
    summaries: list[FieldSummary] = []

    for offset in range(max_len):
        values: set[int] = set()
        for state in states:
            if offset < len(state):
                values.add(state[offset])

        annotation = _FIELD_BY_OFFSET.get(offset)
        if annotation is None:
            name, confidence, desc = "beyond_frame", UNKNOWN, ""
        else:
            name, confidence, desc = annotation.name, annotation.confidence, annotation.description

        change_count = len(values) - 1 if values else 0
        summaries.append(
            FieldSummary(
                offset=offset,
                field_name=name,
                field_confidence=confidence,
                field_description=desc,
                distinct_values=tuple(sorted(values)),
                change_count=change_count,
                is_static=len(values) <= 1,
            )
        )

    return BatchAnalysis(states=states, field_summaries=summaries)


def format_analysis(analysis: BatchAnalysis) -> str:
    """Format a BatchAnalysis as a human-readable summary for the CLI."""
    lines: list[str] = []
    states = analysis.states

    if not states:
        lines.append("No EA 81 states found in the provided captures.")
        return "\n".join(lines)

    lines.append(f"Analyzed {len(states)} state capture(s):")
    for i, state in enumerate(states):
        lines.append(f"  [{i}] {state.hex()}")
    lines.append("")

    static = analysis.static_offsets
    dynamic = analysis.dynamic_offsets
    lines.append(f"Static positions ({len(static)}): {static}")
    lines.append(f"Dynamic positions ({len(dynamic)}): {dynamic}")
    lines.append("")

    lines.append("Field map:")
    lines.append(
        f"  {'Pos':>3}  {'Field':<20s}  {'Confidence':<20s}  "
        f"{'Values':<30s}  Description"
    )
    for summary in analysis.field_summaries:
        values_str = ", ".join(f"0x{v:02x}" for v in summary.distinct_values)
        if len(values_str) > 28:
            values_str = values_str[:25] + "..."
        marker = " " if summary.is_static else "*"
        lines.append(
            f"  {summary.offset:3d}  {summary.field_name:<20s}  "
            f"{summary.field_confidence:<20s}  {values_str:<30s}  "
            f"{marker} {summary.field_description}"
        )

    lines.append("")
    lines.append("(* = dynamic, changes across captures)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITY_SKIP = "skip"

# App modes that map to Surplife app menu options.
APP_MODES = {
    "colorful": "Color picker (HSV color wheel)",
    "white": "White mode with color temperature",
    "scenes": "Preset scenes (static or animated)",
    "rhythm": "Music/rhythm sync effect",
    "scribble": "Segment drawing/editing",
    "link": "Device grouping/mesh linking",
    "settings": "Device settings (firmware, name, etc.)",
    "schedule": "Timer/schedule configuration",
    "playlist": "Sequence of effects",
    "power": "Power on/off toggle",
}


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One suggested next capture action."""

    priority: str
    app_mode: str
    action: str
    rationale: str
    target_fields: list[int]


def generate_recommendations(analysis: BatchAnalysis) -> list[Recommendation]:
    """Analyze the current field map and recommend what to capture next.

    Recommendations are ordered by priority (high → medium → low → skip).
    High-priority items are needed to unlock basic control. Medium items
    expand control to white/effects. Low items are exploratory. Skip items
    are probably not useful for state mapping.
    """
    recs: list[Recommendation] = []
    by_offset = {s.offset: s for s in analysis.field_summaries}

    def _has_values(offset: int) -> set[int]:
        summary = by_offset.get(offset)
        return set(summary.distinct_values) if summary else set()

    # --- HIGH: saturation needs a desaturated color ---
    sat_values = _has_values(12)
    if sat_values and len(sat_values) <= 1:
        recs.append(
            Recommendation(
                priority=PRIORITY_HIGH,
                app_mode="colorful",
                action="Set a pastel/desaturated color (e.g. light pink at ~25% saturation)",
                rationale=(
                    f"Saturation (pos 12) has only been seen at {sorted(sat_values)}. "
                    "A pastel color will confirm it encodes 0-100 saturation."
                ),
                target_fields=[12],
            )
        )

    # --- HIGH: hue needs more colors to confirm the 0-180 scale ---
    hue_values = _has_values(11)
    if hue_values and len(hue_values) < 4:
        needed = []
        if 0x00 not in hue_values:
            needed.append("red (hue 0)")
        if 0x78 not in hue_values:
            needed.append("blue (hue 120)")
        green = 0x78 // 2  # green is hue 120° → 60 in 0-180 scale
        if green not in hue_values:
            needed.append("green (hue 60)")
        yellow = 0x1E  # yellow is hue 60° → 30 in 0-180 scale
        if yellow not in hue_values:
            needed.append("yellow (hue 30)")
        if needed:
            recs.append(
                Recommendation(
                    priority=PRIORITY_HIGH,
                    app_mode="colorful",
                    action=f"Set these colors: {', '.join(needed)}",
                    rationale=(
                        f"Hue (pos 11) has {len(hue_values)} value(s): "
                        f"{sorted(hue_values)}. Need at least 4 colors to confirm "
                        "the 0-180 hue scale (0=red, 30=yellow, 60=green, 120=blue)."
                    ),
                    target_fields=[11],
                )
            )

    # --- HIGH: brightness needs a mid-value to confirm linear scale ---
    bright_values = _has_values(13)
    if bright_values and len(bright_values) < 3:
        recs.append(
            Recommendation(
                priority=PRIORITY_HIGH,
                app_mode="colorful",
                action="Set brightness to 50% (in addition to 25% and 100% already captured)",
                rationale=(
                    f"Brightness (pos 13) has {len(bright_values)} value(s): "
                    f"{sorted(bright_values)}. A 50% value will confirm it's a "
                    "linear 0-100 percentage scale."
                ),
                target_fields=[13],
            )
        )

    # --- MEDIUM: white mode needs warm vs cool to check color temperature ---
    ct_values = _has_values(8)
    if ct_values and len(ct_values) <= 2:
        recs.append(
            Recommendation(
                priority=PRIORITY_MEDIUM,
                app_mode="white",
                action=(
                    "Set white mode, then try warm white (lowest color temp) "
                    "and cool white (highest)"
                ),
                rationale=(
                    f"Color temp (pos 8) has {len(ct_values)} value(s): "
                    f"{sorted(ct_values)}. Warm vs cool white will reveal if this "
                    "byte encodes color temperature or just a mode flag."
                ),
                target_fields=[8],
            )
        )

    # --- MEDIUM: white brightness ---
    white_level_values = _has_values(15)
    if white_level_values and len(white_level_values) <= 1:
        recs.append(
            Recommendation(
                priority=PRIORITY_MEDIUM,
                app_mode="white",
                action="Set white mode at different brightness levels (25%, 50%, 100%)",
                rationale=(
                    "White level (pos 15) has only been seen at "
                    f"{sorted(white_level_values)}. Different white brightness "
                    "values will confirm if this is the white brightness byte."
                ),
                target_fields=[15],
            )
        )

    # --- MEDIUM: scenes might reveal effect/animation fields ---
    # Check if any currently-static unknown fields could be effect fields.
    potentially_effect_fields = [
        s.offset
        for s in analysis.field_summaries
        if s.is_static and s.field_confidence == UNKNOWN and s.offset not in (0, 1, 2, 3, 4, 5)
    ]
    if potentially_effect_fields and len(analysis.states) < 10:
        recs.append(
            Recommendation(
                priority=PRIORITY_MEDIUM,
                app_mode="scenes",
                action="Try 2-3 different preset scenes and probe after each",
                rationale=(
                    f"Positions {potentially_effect_fields} are static so far but "
                    "could encode scene/effect/animation data. Scenes are the most "
                    "likely app feature to activate them."
                ),
                target_fields=potentially_effect_fields,
            )
        )

    # --- MEDIUM: rhythm mode ---
    recs.append(
        Recommendation(
            priority=PRIORITY_MEDIUM,
            app_mode="rhythm",
            action="Enable rhythm mode, probe once, then disable it and probe again",
            rationale=(
                "Rhythm mode may set an effect flag in the state frame. "
                "Comparing rhythm-on vs rhythm-off will isolate it."
            ),
            target_fields=potentially_effect_fields,
        )
    )

    # --- LOW: scribble (segment control) ---
    recs.append(
        Recommendation(
            priority=PRIORITY_LOW,
            app_mode="scribble",
            action="Draw a simple segment pattern, probe, then clear it and probe again",
            rationale=(
                "Scribble mode may write segment-level data to the state frame. "
                "Only useful if the lamp supports per-segment control."
            ),
            target_fields=potentially_effect_fields,
        )
    )

    # --- LOW: link (mesh grouping) ---
    recs.append(
        Recommendation(
            priority=PRIORITY_LOW,
            app_mode="link",
            action="Check if the lamp is linked to any group; probe with and without linking",
            rationale=(
                "Link/group mode may change the mesh address bytes (pos 3-4). "
                "Only relevant if you have multiple Surplife devices."
            ),
            target_fields=[3, 4],
        )
    )

    # --- SKIP: not useful for state mapping ---
    for mode, desc in [("schedule", "timers"), ("playlist", "effect sequences"),
                       ("settings", "device settings"), ("power", "already captured")]:
        recs.append(
            Recommendation(
                priority=PRIORITY_SKIP,
                app_mode=mode,
                action=f"Skip — {desc} are unlikely to change the state frame",
                rationale=f"{APP_MODES[mode]} does not map to a persistent state field.",
                target_fields=[],
            )
        )

    return recs


def format_recommendations(recs: list[Recommendation]) -> str:
    """Format recommendations as a human-readable guide for the CLI."""
    lines: list[str] = []
    lines.append("Recommended next captures:")
    lines.append("")

    priority_labels = {
        PRIORITY_HIGH: "HIGH (needed to unlock control)",
        PRIORITY_MEDIUM: "MEDIUM (expands control coverage)",
        PRIORITY_LOW: "LOW (exploratory)",
        PRIORITY_SKIP: "SKIP (probably not useful)",
    }
    priority_order = [PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW, PRIORITY_SKIP]

    for priority in priority_order:
        priority_recs = [r for r in recs if r.priority == priority]
        if not priority_recs:
            continue
        lines.append(f"  [{priority_labels[priority]}]")
        for rec in priority_recs:
            fields_str = f" (targets pos {rec.target_fields})" if rec.target_fields else ""
            lines.append(f"    {rec.app_mode}: {rec.action}{fields_str}")
            lines.append(f"      why: {rec.rationale}")
        lines.append("")

    high_count = sum(1 for r in recs if r.priority == PRIORITY_HIGH)
    if high_count == 0:
        lines.append(
            "  All high-priority fields are confirmed. Control may be ready to unlock."
        )
    else:
        lines.append(
            f"  {high_count} high-priority capture(s) remaining "
            "before control can be unlocked."
        )

    return "\n".join(lines)
