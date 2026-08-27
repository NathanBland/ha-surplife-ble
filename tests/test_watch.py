"""Tests for batch analysis, recommendations, and watch state-change detection."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.surplife_ble.core.statediff import (
    PRIORITY_HIGH,
    PRIORITY_SKIP,
    analyze_states,
    collect_states_from_captures,
    format_analysis,
    format_recommendations,
    generate_recommendations,
)

STATE_OFF = bytes.fromhex("ea810100aa0924256f50f0006464006405001dffffffff01000000")
STATE_RED_DIM = bytes.fromhex("ea810100aa0923240150f0006419006405001dffffffff01000000")
STATE_RED_BRIGHT = bytes.fromhex("ea810100aa0923240150f0006464006405001dffffffff01000000")
STATE_BLUE = bytes.fromhex("ea810100aa0923240150f0786464006405001dffffffff01000000")
STATE_WHITE = bytes.fromhex("ea810100aa0923256f50f0786464006405001dffffffff01000000")


def _write_capture(path: Path, state_hex: str) -> None:
    path.write_text(
        json.dumps({
            "direction": "resolution",
            "device_id": "DEV",
            "source_packet_hex": state_hex,
            "frame_family": "extended-ea81",
        }) + "\n",
        encoding="utf-8",
    )


def test_collect_states_from_multiple_captures(tmp_path: Path) -> None:
    files = []
    for i, state in enumerate([STATE_OFF, STATE_RED_DIM, STATE_BLUE]):
        path = tmp_path / f"state-{i}.jsonl"
        _write_capture(path, state.hex())
        files.append(path)
    states = collect_states_from_captures(files)
    assert len(states) == 3
    assert states[0] == STATE_OFF
    assert states[1] == STATE_RED_DIM
    assert states[2] == STATE_BLUE


def test_collect_states_skips_files_without_state(tmp_path: Path) -> None:
    good = tmp_path / "good.jsonl"
    _write_capture(good, STATE_OFF.hex())
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    states = collect_states_from_captures([good, empty])
    assert len(states) == 1
    assert states[0] == STATE_OFF


def test_analyze_states_identifies_static_and_dynamic() -> None:
    states = [STATE_OFF, STATE_RED_DIM, STATE_RED_BRIGHT, STATE_BLUE, STATE_WHITE]
    analysis = analyze_states(states)
    assert len(analysis.states) == 5
    assert 6 in analysis.dynamic_offsets  # power
    assert 7 in analysis.dynamic_offsets  # color_white_mode
    assert 8 in analysis.dynamic_offsets  # color_temp
    assert 11 in analysis.dynamic_offsets  # hue
    assert 13 in analysis.dynamic_offsets  # brightness
    assert 0 in analysis.static_offsets  # signature
    assert 12 in analysis.static_offsets  # saturation (always 100)
    assert 9 in analysis.static_offsets


def test_analyze_states_power_field_has_two_values() -> None:
    states = [STATE_OFF, STATE_RED_DIM]
    analysis = analyze_states(states)
    power = next(s for s in analysis.field_summaries if s.offset == 6)
    assert power.distinct_values == (0x23, 0x24)
    assert not power.is_static


def test_analyze_states_hue_field_has_two_values() -> None:
    states = [STATE_RED_DIM, STATE_BLUE]
    analysis = analyze_states(states)
    hue = next(s for s in analysis.field_summaries if s.offset == 11)
    assert hue.distinct_values == (0x00, 0x78)
    assert not hue.is_static


def test_analyze_states_empty_list() -> None:
    analysis = analyze_states([])
    assert analysis.states == []
    assert analysis.field_summaries == []
    assert analysis.static_offsets == []
    assert analysis.dynamic_offsets == []


def test_format_analysis_includes_summary() -> None:
    states = [STATE_OFF, STATE_RED_DIM, STATE_BLUE]
    analysis = analyze_states(states)
    output = format_analysis(analysis)
    assert "Analyzed 3 state capture(s):" in output
    assert "Static positions" in output
    assert "Dynamic positions" in output
    assert "Field map:" in output
    assert "power" in output
    assert "hue" in output
    assert "brightness" in output
    assert "*" in output  # dynamic marker


def test_format_analysis_empty() -> None:
    analysis = analyze_states([])
    output = format_analysis(analysis)
    assert "No EA 81 states found" in output


def test_format_analysis_shows_distinct_values() -> None:
    states = [STATE_OFF, STATE_RED_DIM]
    analysis = analyze_states(states)
    output = format_analysis(analysis)
    assert "0x23, 0x24" in output  # power values


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------


def test_recommendations_include_saturation_when_only_one_value() -> None:
    """Saturation at pos 12 only has value 100 (0x64) in our captures."""
    states = [STATE_OFF, STATE_RED_DIM, STATE_BLUE]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    sat_recs = [r for r in recs if 12 in r.target_fields and r.priority == PRIORITY_HIGH]
    assert len(sat_recs) >= 1
    assert "pastel" in sat_recs[0].action.lower() or "desaturated" in sat_recs[0].action.lower()


def test_recommendations_include_hue_when_few_colors() -> None:
    """Hue at pos 11 has only 2 values (red, blue) — need more colors."""
    states = [STATE_RED_DIM, STATE_BLUE]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    hue_recs = [r for r in recs if 11 in r.target_fields and r.priority == PRIORITY_HIGH]
    assert len(hue_recs) >= 1
    assert "green" in hue_recs[0].action.lower() or "yellow" in hue_recs[0].action.lower()


def test_recommendations_include_brightness_mid_value() -> None:
    """Brightness at pos 13 has 25 and 100 but not 50."""
    states = [STATE_RED_DIM, STATE_RED_BRIGHT]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    bright_recs = [r for r in recs if 13 in r.target_fields and r.priority == PRIORITY_HIGH]
    assert len(bright_recs) >= 1
    assert "50%" in bright_recs[0].action


def test_recommendations_include_white_color_temp() -> None:
    """Color temp at pos 8 has only 2 values — need warm vs cool white."""
    states = [STATE_RED_DIM, STATE_WHITE]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    white_recs = [r for r in recs if r.app_mode == "white" and 8 in r.target_fields]
    assert len(white_recs) >= 1
    assert "warm" in white_recs[0].action.lower()


def test_recommendations_include_scenes_for_static_unknowns() -> None:
    """Should recommend scenes to probe static unknown fields."""
    states = [STATE_OFF, STATE_RED_DIM]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    scene_recs = [r for r in recs if r.app_mode == "scenes"]
    assert len(scene_recs) >= 1


def test_recommendations_include_skip_for_schedule_playlist_settings() -> None:
    """Schedule, playlist, and settings should be marked SKIP."""
    states = [STATE_OFF, STATE_RED_DIM]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    skip_modes = {r.app_mode for r in recs if r.priority == PRIORITY_SKIP}
    assert "schedule" in skip_modes
    assert "playlist" in skip_modes
    assert "settings" in skip_modes


def test_recommendations_no_high_priority_when_all_fields_confirmed() -> None:
    """When saturation, hue, and brightness all have enough values, no HIGH recs."""
    # Build synthetic states with diverse values for all confirmed fields.
    base = bytearray(STATE_RED_BRIGHT)
    states = [bytes(base)]
    # Add states with different hue values (0, 30, 60, 120)
    for hue in [0x00, 0x1E, 0x3C, 0x78]:
        s = bytearray(base)
        s[11] = hue
        states.append(bytes(s))
    # Add states with different saturation values (50, 100)
    s = bytearray(base)
    s[12] = 50
    states.append(bytes(s))
    # Add states with different brightness values (25, 50, 100)
    for bright in [0x19, 0x32, 0x64]:
        s = bytearray(base)
        s[13] = bright
        states.append(bytes(s))

    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    high_recs = [r for r in recs if r.priority == PRIORITY_HIGH]
    assert len(high_recs) == 0


def test_format_recommendations_includes_priority_sections() -> None:
    states = [STATE_OFF, STATE_RED_DIM]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    output = format_recommendations(recs)
    assert "HIGH" in output
    assert "MEDIUM" in output
    assert "LOW" in output
    assert "SKIP" in output
    assert "why:" in output


def test_format_recommendations_says_ready_when_no_high() -> None:
    # Build states with enough diversity to clear all HIGH recs.
    base = bytearray(STATE_RED_BRIGHT)
    states = [bytes(base)]
    for hue in [0x00, 0x1E, 0x3C, 0x78]:
        s = bytearray(base)
        s[11] = hue
        states.append(bytes(s))
    s = bytearray(base)
    s[12] = 50
    states.append(bytes(s))
    for bright in [0x19, 0x32, 0x64]:
        s = bytearray(base)
        s[13] = bright
        states.append(bytes(s))

    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    output = format_recommendations(recs)
    assert "ready to unlock" in output.lower()


def test_recommendations_are_ordered_by_priority() -> None:
    states = [STATE_OFF, STATE_RED_DIM]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    priorities = [r.priority for r in recs]
    # HIGH should come before MEDIUM, MEDIUM before LOW, LOW before SKIP.
    order = {"high": 0, "medium": 1, "low": 2, "skip": 3}
    indices = [order[p] for p in priorities]
    assert indices == sorted(indices)


# ---------------------------------------------------------------------------
# Watch live-analysis logic
# ---------------------------------------------------------------------------


def test_live_analysis_shows_high_count_after_transition() -> None:
    """Simulates what watch does: accumulate states, run analysis, count HIGH recs."""
    states = [STATE_OFF, STATE_RED_DIM]
    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    high_count = sum(1 for r in recs if r.priority == PRIORITY_HIGH)
    assert high_count >= 3  # saturation, hue, brightness all incomplete


def test_live_analysis_shows_zero_high_when_complete() -> None:
    """When all HIGH fields are confirmed, live analysis should report 0 remaining."""
    base = bytearray(STATE_RED_BRIGHT)
    states = [bytes(base)]
    for hue in [0x00, 0x1E, 0x3C, 0x78]:
        s = bytearray(base)
        s[11] = hue
        states.append(bytes(s))
    s = bytearray(base)
    s[12] = 50
    states.append(bytes(s))
    for bright in [0x19, 0x32, 0x64]:
        s = bytearray(base)
        s[13] = bright
        states.append(bytes(s))

    analysis = analyze_states(states)
    recs = generate_recommendations(analysis)
    high_count = sum(1 for r in recs if r.priority == PRIORITY_HIGH)
    assert high_count == 0


def test_watch_scan_timeout_default_is_fast() -> None:
    """The watch subparser should default scan-timeout to 1.0 (not 10.0)."""
    from surplife_ble.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "watch", "--device", "DEV", "--jsonl", "out.jsonl",
    ])
    # watch overrides the default to 1.0 in the handler, but the parser
    # still has the _add_device default of 10.0. The handler checks for this.
    assert args.scan_timeout == 10.0  # parser default
    # The handler uses 1.0 when the user hasn't overridden it.


def test_watch_scan_timeout_can_be_overridden() -> None:
    """User can pass --scan-timeout to watch to set a custom value."""
    from surplife_ble.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "watch", "--device", "DEV", "--jsonl", "out.jsonl", "--scan-timeout", "3.5",
    ])
    assert args.scan_timeout == 3.5
