from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.surplife_ble.core.framing import checksum_valid
from custom_components.surplife_ble.core.protocol import (
    classic_power,
    classic_rgb,
    classify_packet,
    experimental_e122_fill,
    extended_e0_rgb,
    iotbt_3b_brightness,
    iotbt_3b_rgb,
    iotbt_3b_white,
    iotbt_e0_rgb,
    iotbt_e0_white,
    iotbt_e1_segment_rgb,
    iotbt_power,
    parse_state,
    probe_frames,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "protocol_packets.json").read_text(encoding="utf-8")
)


def test_probe_matrix_contains_iotbt_query_and_all_transports() -> None:
    frames = probe_frames()
    assert len(frames) == 12
    assert {frame.transport for frame in frames} == {
        "raw",
        "magichome2",
        "magichome2-len-plus-2",
    }
    assert any(frame.payload == bytes.fromhex("ea818a8b") for frame in frames)
    assert any(frame.payload == bytes.fromhex("818a8b40") for frame in frames)


def test_classic_floor_lamp_classification_and_state() -> None:
    packet = bytes.fromhex(FIXTURES["classic_floor_lamp"]["state_hex"])
    resolution, inner, framing = classify_packet(packet)  # type: ignore[misc]
    assert inner == packet
    assert framing == "raw"
    assert resolution.model_num == 0x0E
    assert resolution.codec == "classic-9byte"
    assert {"warm-white", "cool-white"}.issubset(resolution.capabilities)

    state = parse_state(packet)
    assert state is not None
    assert state.power is True
    assert state.rgb == (255, 0, 0)
    assert state.firmware_version == 9


@pytest.mark.parametrize(
    ("fixture_name", "model"),
    [("extended_b6", 0xB6), ("extended_unknown_77", 0x77)],
)
def test_extended_state_uses_frame_shape_not_registration(fixture_name: str, model: int) -> None:
    packet = bytes.fromhex(FIXTURES[fixture_name]["state_hex"])
    classified = classify_packet(packet)
    assert classified is not None
    resolution, _inner, _framing = classified
    assert resolution.frame_family == "extended-ea81"
    assert resolution.codec == "extended-custom"
    assert resolution.model_num == model

    state = parse_state(packet)
    assert state is not None
    assert state.model_num == model
    assert state.led_count == 100


def test_unknown_ea81_layout_remains_control_locked() -> None:
    packet = bytearray(bytes.fromhex(FIXTURES["dx23012_iotbt144"]["state_hex"]))
    packet[4:6] = bytes.fromhex("1234")
    classified = classify_packet(bytes(packet))
    assert classified is not None
    resolution, _inner, _framing = classified
    assert resolution.codec == "unresolved-ea81"
    assert "rgb" not in resolution.capabilities


def test_physical_dx23012_json_response_is_authoritative() -> None:
    fixture = FIXTURES["dx23012_iotbt144"]
    packet = bytes.fromhex(fixture["wrapped_response_hex"])
    classified = classify_packet(packet)
    assert classified is not None
    resolution, inner, framing = classified
    assert inner.hex() == fixture["state_hex"]
    assert framing.endswith("/json-payload")
    assert resolution.frame_family == "extended-ea81"
    assert resolution.codec == "iotbt-ea81"
    assert resolution.model_num is None
    assert resolution.firmware_version is None
    assert "rgb" in resolution.capabilities
    assert "brightness" in resolution.capabilities
    assert "power" in resolution.capabilities

    state = parse_state(packet)
    assert state is not None
    assert state.power is True
    assert state.rgb is not None
    assert state.brightness is not None
    assert state.led_count is not None


def test_e122_matches_open_pr_golden_packet() -> None:
    expected = bytes.fromhex(FIXTURES["e122_uniform_red"]["inner_hex"])
    assert experimental_e122_fill(255, 0, 0) == expected
    assert len(expected) == 107


def test_classic_commands_are_checksummed() -> None:
    assert classic_power(True).hex() == "71230fa3"
    assert checksum_valid(classic_power(False))
    rgb = classic_rgb(255, 0, 0, nine_byte=True)
    assert len(rgb) == 9
    assert checksum_valid(rgb)


def test_invalid_packet_is_not_classified() -> None:
    assert classify_packet(bytes.fromhex("810e2361")) is None


# ---------------------------------------------------------------------------
# IOTBT DX23012 codec tests
# ---------------------------------------------------------------------------

# Authoritative off-state from the physical DX23012.
IOTBT_OFF = bytes.fromhex("ea810100aa0924256f50f0006464006405001dffffffff01000000")
# Red at 25% brightness (hue=0, sat=100, bright=25).
IOTBT_RED_DIM = bytes.fromhex("ea810100aa0923240150f0006419006405001dffffffff01000000")
# Blue at 100% brightness (hue=120, sat=100, bright=100).
IOTBT_BLUE = bytes.fromhex("ea810100aa0923240150f0786464006405001dffffffff01000000")
# White mode at 100% brightness.
IOTBT_WHITE = bytes.fromhex("ea810100aa0923256f50f0786464006405001dffffffff01000000")


def test_iotbt_state_decodes_power() -> None:
    state = parse_state(IOTBT_OFF)
    assert state is not None
    assert state.power is False
    state = parse_state(IOTBT_RED_DIM)
    assert state is not None
    assert state.power is True


def test_iotbt_state_decodes_hsv() -> None:
    state = parse_state(IOTBT_RED_DIM)
    assert state is not None
    assert state.rgb is not None
    # Red at full saturation, 25% brightness: R=64, G=0, B=0.
    assert state.rgb[0] > 50
    assert state.rgb[1] == 0
    assert state.rgb[2] == 0
    # Brightness 25% = 0x19 → ~64/255
    assert 50 < state.brightness < 80


def test_iotbt_state_decodes_blue() -> None:
    state = parse_state(IOTBT_BLUE)
    assert state is not None
    assert state.rgb is not None
    assert state.rgb[2] > 200  # blue dominant
    assert state.brightness > 200  # 100%


def test_iotbt_state_decodes_white_mode() -> None:
    state = parse_state(IOTBT_WHITE)
    assert state is not None
    assert state.power is True
    assert state.white > 0  # white mode should report white brightness


def test_iotbt_state_color_mode_has_no_white() -> None:
    state = parse_state(IOTBT_RED_DIM)
    assert state is not None
    assert state.white == 0  # color mode should not report white


def test_iotbt_e0_rgb_encodes_hue_in_180_scale() -> None:
    """IOTBT uses 0-180 hue (half degrees), not 0-255 like extended-custom."""
    # Red: hue=0 in both scales.
    cmd = iotbt_e0_rgb(255, 0, 0, 100)
    assert cmd[0] == 0xE0
    assert cmd[4] == 0  # hue=0 for red
    assert cmd[5] == 100  # saturation=100%
    assert cmd[6] == 100  # brightness=100%


def test_iotbt_e0_rgb_blue_hue_is_120() -> None:
    """Blue is hue 240° → 120 in the 0-180 scale."""
    cmd = iotbt_e0_rgb(0, 0, 255, 100)
    assert cmd[4] == 120  # hue=120 for blue in 0-180 scale


def test_iotbt_e0_rgb_differs_from_extended() -> None:
    """The IOTBT encoder uses 0-180 hue while extended-custom uses 0-255."""
    iotbt_cmd = iotbt_e0_rgb(0, 0, 255, 100)
    ext_cmd = extended_e0_rgb(0, 0, 255, 100)
    # The hue byte should differ: 120 (IOTBT) vs ~170 (extended 0-255).
    assert iotbt_cmd[4] != ext_cmd[4]
    assert iotbt_cmd[4] == 120


def test_iotbt_power_on_command() -> None:
    cmd = iotbt_power(True)
    assert cmd[0] == 0xE0
    assert cmd[3] == 0x23  # POWER_ON


def test_iotbt_power_off_command() -> None:
    cmd = iotbt_power(False)
    assert cmd[0] == 0xE0
    assert cmd[3] == 0x24  # POWER_OFF


def test_iotbt_e0_white_uses_white_rgb() -> None:
    cmd = iotbt_e0_white(50)
    # White = (255,255,255) → hue=0, sat=0, brightness=50
    assert cmd[4] == 0  # hue=0
    assert cmd[5] == 0  # saturation=0
    assert cmd[6] == 50  # brightness=50%


def test_iotbt_3b_rgb_starts_with_3b_a1() -> None:
    cmd = iotbt_3b_rgb(255, 0, 0, 50)
    assert cmd[0] == 0x3B
    assert cmd[1] == 0xA1


def test_iotbt_3b_rgb_has_valid_checksum() -> None:
    cmd = iotbt_3b_rgb(255, 0, 0, 50)
    assert checksum_valid(cmd)


def test_iotbt_3b_rgb_red_hue_is_zero() -> None:
    cmd = iotbt_3b_rgb(255, 0, 0, 50)
    # packed = (0 << 7) | 100 = 100 = 0x0064
    assert cmd[2] == 0x00  # hs_hi
    assert cmd[3] == 0x64  # hs_lo
    assert cmd[4] == 50  # brightness


def test_iotbt_3b_rgb_blue_hue_packed() -> None:
    cmd = iotbt_3b_rgb(0, 0, 255, 50)
    # packed = (240 << 7) | 100 = 30820 = 0x7864
    assert cmd[2] == 0x78  # hs_hi
    assert cmd[3] == 0x64  # hs_lo


def test_iotbt_3b_rgb_includes_rgb_bytes() -> None:
    cmd = iotbt_3b_rgb(255, 128, 0, 50)
    # RGB at positions 7, 8, 9
    assert cmd[7] == 255
    assert cmd[8] == 128
    assert cmd[9] == 0


def test_iotbt_3b_white_starts_with_3b_b1() -> None:
    cmd = iotbt_3b_white(50)
    assert cmd[0] == 0x3B
    assert cmd[1] == 0xB1


def test_iotbt_3b_white_has_valid_checksum() -> None:
    cmd = iotbt_3b_white(50)
    assert checksum_valid(cmd)


def test_iotbt_3b_white_encodes_brightness() -> None:
    cmd = iotbt_3b_white(75)
    # brightness at position 6
    assert cmd[6] == 75


def test_iotbt_3b_brightness_starts_with_3b_01() -> None:
    cmd = iotbt_3b_brightness(50)
    assert cmd[0] == 0x3B
    assert cmd[1] == 0x01


def test_iotbt_3b_brightness_has_valid_checksum() -> None:
    cmd = iotbt_3b_brightness(50)
    assert checksum_valid(cmd)


def test_iotbt_3b_brightness_encodes_value() -> None:
    cmd = iotbt_3b_brightness(60)
    # value at positions 4 and 6
    assert cmd[4] == 60
    assert cmd[6] == 60


# ---------------------------------------------------------------------------
# E1 03 segment color command (verified on physical DX23012, 2026-08-26)
# ---------------------------------------------------------------------------

# Verified frame: blue at 40% brightness, 20 segments.
# Header: E1 03 00 14 00 00 14
# Record: A1 78 64 28  (hue=120, sat=100, bright=40)
E1_03_BLUE_40 = (
    bytes.fromhex("e1030014000014")
    + bytes.fromhex("a1786428") * 20
)


def test_iotbt_e1_segment_rgb_matches_verified_blue_40_frame() -> None:
    cmd = iotbt_e1_segment_rgb(0, 0, 255, 40)
    assert cmd == E1_03_BLUE_40


def test_iotbt_e1_segment_rgb_header_is_correct() -> None:
    cmd = iotbt_e1_segment_rgb(255, 0, 0, 100)
    assert cmd[0] == 0xE1
    assert cmd[1] == 0x03
    assert cmd[2] == 0x00
    assert cmd[3] == 20  # segment count
    assert cmd[4] == 0x00
    assert cmd[5] == 0x00
    assert cmd[6] == 20  # segment count repeated


def test_iotbt_e1_segment_rgb_frame_length() -> None:
    # 7-byte header + 20 * 4-byte records = 87 bytes.
    cmd = iotbt_e1_segment_rgb(255, 0, 0, 100)
    assert len(cmd) == 87


def test_iotbt_e1_segment_rgb_records_repeat_segment_count_times() -> None:
    cmd = iotbt_e1_segment_rgb(255, 0, 0, 100)
    body = cmd[7:]
    assert len(body) == 20 * 4
    # Every 4-byte record starts with A1.
    for i in range(0, len(body), 4):
        assert body[i] == 0xA1


def test_iotbt_e1_segment_rgb_red_hue_is_zero() -> None:
    cmd = iotbt_e1_segment_rgb(255, 0, 0, 50)
    record = cmd[7:11]
    assert record[0] == 0xA1
    assert record[1] == 0  # hue=0 for red
    assert record[2] == 100  # saturation=100%
    assert record[3] == 50  # brightness=50%


def test_iotbt_e1_segment_rgb_blue_hue_is_120() -> None:
    # Blue is hue 240 degrees -> 120 in the 0-180 scale.
    cmd = iotbt_e1_segment_rgb(0, 0, 255, 100)
    record = cmd[7:11]
    assert record[1] == 120


def test_iotbt_e1_segment_rgb_white_is_saturation_zero() -> None:
    cmd = iotbt_e1_segment_rgb(255, 255, 255, 80)
    record = cmd[7:11]
    assert record[1] == 0  # hue=0
    assert record[2] == 0  # saturation=0 for white
    assert record[3] == 80  # brightness=80%


def test_iotbt_e1_segment_rgb_rejects_invalid_segment_count() -> None:
    with pytest.raises(ValueError):
        iotbt_e1_segment_rgb(255, 0, 0, 100, segment_count=0)
    with pytest.raises(ValueError):
        iotbt_e1_segment_rgb(255, 0, 0, 100, segment_count=256)


def test_iotbt_e1_segment_rgb_clamps_brightness() -> None:
    cmd = iotbt_e1_segment_rgb(255, 0, 0, 200)
    assert cmd[10] == 100  # clamped to 100
    cmd_low = iotbt_e1_segment_rgb(255, 0, 0, -5)
    assert cmd_low[10] == 0  # clamped to 0
