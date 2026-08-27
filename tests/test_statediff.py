"""Tests for state extraction, byte diffing, and field annotations."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.surplife_ble.core.statediff import (
    CONFIRMED,
    EA81_FIELDS,
    EXTENDED_HYPOTHESIS,
    HYPOTHESIS,
    UNKNOWN,
    diff_states,
    extract_state_from_capture,
    format_diff,
)

# The authoritative DX23012 state from the physical capture.
STATE_ON = bytes.fromhex("ea810100aa0923256f50f0006464006405001dffffffff01000000")
STATE_OFF = bytes.fromhex("ea810100aa0924256f50f0006464006405001dffffffff01000000")
STATE_RED_BRIGHT = bytes.fromhex("ea810100aa0923256f50f000ff64006405001dffffffff01000000")
STATE_BLUE = bytes.fromhex("ea810100aa0923256f50f0006464006405001dffffffff01000000")


# ---------------------------------------------------------------------------
# Field annotations
# ---------------------------------------------------------------------------


def test_ea81_fields_cover_all_27_bytes() -> None:
    offsets = [f.offset for f in EA81_FIELDS]
    assert offsets == list(range(27))


def test_ea81_fields_have_confidence_levels() -> None:
    confidences = {f.confidence for f in EA81_FIELDS}
    assert confidences == {CONFIRMED, HYPOTHESIS, EXTENDED_HYPOTHESIS, UNKNOWN}


def test_power_field_is_confirmed_at_offset_6() -> None:
    power_field = next(f for f in EA81_FIELDS if f.name == "power")
    assert power_field.offset == 6
    assert power_field.confidence == CONFIRMED


# ---------------------------------------------------------------------------
# Byte diffing
# ---------------------------------------------------------------------------


def test_diff_on_vs_off_shows_only_power_changed() -> None:
    result = diff_states(STATE_ON, STATE_OFF)
    assert result.changed_offsets == [6]
    assert result.changed_fields == ["power"]
    byte_6 = result.bytes[6]
    assert byte_6.value_a == 0x23
    assert byte_6.value_b == 0x24
    assert byte_6.field_confidence == CONFIRMED


def test_diff_identical_states_shows_no_changes() -> None:
    result = diff_states(STATE_ON, STATE_ON)
    assert result.changed_offsets == []
    assert result.changed_fields == []


def test_diff_detects_multiple_changes() -> None:
    # Change byte 6 (power) and byte 11 (hue? candidate)
    modified = bytearray(STATE_ON)
    modified[6] = 0x24  # off
    modified[11] = 0xFF  # change hue candidate
    result = diff_states(STATE_ON, bytes(modified))
    assert result.changed_offsets == [6, 11]
    assert "power" in result.changed_fields
    assert "hue" in result.changed_fields


def test_diff_handles_different_lengths() -> None:
    short = STATE_ON[:20]
    result = diff_states(STATE_ON, short)
    assert result.length_a == 27
    assert result.length_b == 20
    # Bytes 20-26 should be marked as changed (missing in B)
    for offset in range(20, 27):
        assert offset in result.changed_offsets


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_diff_includes_changed_fields_and_byte_map() -> None:
    result = diff_states(STATE_ON, STATE_OFF)
    output = format_diff(result, label_a="on", label_b="off")
    assert "on:" in output
    assert "off:" in output
    assert "Changed positions: [6]" in output
    assert "power" in output
    assert "[confirmed]" in output
    assert "Full byte map:" in output


def test_format_diff_identical_states() -> None:
    result = diff_states(STATE_ON, STATE_ON)
    output = format_diff(result)
    assert "No bytes changed" in output


# ---------------------------------------------------------------------------
# State extraction from JSONL captures
# ---------------------------------------------------------------------------


def _write_capture(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def test_extract_state_from_resolution_record(tmp_path: Path) -> None:
    capture = tmp_path / "probe.jsonl"
    _write_capture(capture, [
        {"direction": "tx", "device_id": "DEV", "payload_hex": "818a8b96",
         "characteristic": "ff01", "note": "classic-magichome2"},
        {"direction": "rx", "device_id": "DEV", "payload_hex": "ea810100aa0923",
         "characteristic": "ff02", "note": "notification"},
        {"direction": "resolution", "device_id": "DEV",
         "source_packet_hex": STATE_ON.hex(),
         "frame_family": "extended-ea81", "codec": "unresolved-ea81"},
    ])
    state = extract_state_from_capture(capture)
    assert state == STATE_ON


def test_extract_state_falls_back_to_notifications(tmp_path: Path) -> None:
    capture = tmp_path / "probe.jsonl"
    # No resolution record, but a notification with a wrapped EA 81 payload.
    # Use the raw EA 81 state directly as the notification payload.
    _write_capture(capture, [
        {"direction": "rx", "device_id": "DEV", "payload_hex": STATE_ON.hex(),
         "characteristic": "ff02", "note": "notification"},
    ])
    state = extract_state_from_capture(capture)
    assert state == STATE_ON


def test_extract_state_returns_none_for_empty_capture(tmp_path: Path) -> None:
    capture = tmp_path / "empty.jsonl"
    _write_capture(capture, [])
    state = extract_state_from_capture(capture)
    assert state is None


def test_extract_state_returns_none_for_non_ea81(tmp_path: Path) -> None:
    capture = tmp_path / "probe.jsonl"
    _write_capture(capture, [
        {"direction": "rx", "device_id": "DEV", "payload_hex": "0123456789",
         "characteristic": "ff02", "note": "notification"},
    ])
    state = extract_state_from_capture(capture)
    assert state is None


def test_extract_state_prefers_latest_resolution(tmp_path: Path) -> None:
    capture = tmp_path / "probe.jsonl"
    _write_capture(capture, [
        {"direction": "resolution", "device_id": "DEV",
         "source_packet_hex": STATE_ON.hex(), "frame_family": "extended-ea81"},
        {"direction": "resolution", "device_id": "DEV",
         "source_packet_hex": STATE_OFF.hex(), "frame_family": "extended-ea81"},
    ])
    state = extract_state_from_capture(capture)
    assert state == STATE_OFF


# ---------------------------------------------------------------------------
# End-to-end with the real DX23012 fixture
# ---------------------------------------------------------------------------


def test_diff_real_dx23012_on_vs_off() -> None:
    """The authoritative capture should show power as the only difference."""
    result = diff_states(STATE_ON, STATE_OFF)
    assert result.changed_offsets == [6]
    power_byte = result.bytes[6]
    assert power_byte.field_name == "power"
    assert power_byte.field_confidence == CONFIRMED
    assert power_byte.value_a == 0x23  # on
    assert power_byte.value_b == 0x24  # off
