"""Protocol classification, state parsing, and conservative command encoders."""

from __future__ import annotations

import colorsys
from dataclasses import dataclass

from .framing import candidate_payloads, checksum_valid, with_checksum, wrap_magichome
from .models import LightState, ProtocolResolution

QUERY_LED_BLE = bytes.fromhex("ef0177")
QUERY_CLASSIC = bytes.fromhex("818a8b96")
QUERY_CLASSIC_IOTBT = bytes.fromhex("818a8b40")
QUERY_EXTENDED = bytes.fromhex("ea818a8b")

POWER_ON = 0x23
POWER_OFF = 0x24
MODEL_FLOOR_LAMP_RGBCW = 0x0E
KNOWN_EXTENDED_MODELS = {0x77, 0x82, 0xB6}

# IOTBT DX23012 state field positions, confirmed by state-diff captures
# on 2026-08-26/27. See docs/CAPTURE_WORKFLOW.md and statediff.py.
IOTBT_POS_POWER = 6
IOTBT_POS_COLOR_WHITE_MODE = 7
IOTBT_POS_COLOR_TEMP = 8
IOTBT_POS_HUE = 11
IOTBT_POS_SATURATION = 12
IOTBT_POS_BRIGHTNESS = 13
IOTBT_COLOR_MODE = 0x24
IOTBT_WHITE_MODE = 0x25

# IOTBT command prefixes (v2/v3 unified format).
IOTBT_CMD_PREFIX = 0x3B
IOTBT_CMD_RGB = 0xA1
IOTBT_CMD_WHITE = 0xB1
IOTBT_CMD_BRIGHTNESS = 0x01
IOTBT_CMD_E0_PREFIX = 0xE0
IOTBT_CMD_E0_ZONE = 0x01
IOTBT_CMD_E0_RGB_OP = 0xC1
IOTBT_CMD_TELINK_PREFIX = 0xE2
IOTBT_CMD_TELINK_OP = 0x0B
IOTBT_CMD_FILLER = 0x1E

# E1 03 app-style segment color command (verified on DX23012, 2026-08-26).
# See docs/DX23012_PROTOCOL_FINDINGS.md.
IOTBT_CMD_E1_PREFIX = 0xE1
IOTBT_CMD_E1_SEGMENT_OP = 0x03
IOTBT_CMD_E1_SEGMENT_RECORD = 0xA1
IOTBT_SEGMENT_COUNT = 20


@dataclass(frozen=True, slots=True)
class ProbeFrame:
    name: str
    payload: bytes
    transport: str


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def percent_to_byte(value: int) -> int:
    return round(clamp(value, 0, 100) * 255 / 100)


def byte_to_percent(value: int) -> int:
    return round(clamp(value, 0, 255) * 100 / 255)


def probe_frames(sequence_start: int = 1) -> list[ProbeFrame]:
    """Return ordered, low-risk state-query candidates."""
    raw = [
        ("led-ble-raw", QUERY_LED_BLE),
        ("classic-raw", QUERY_CLASSIC),
        ("classic-iotbt-raw", QUERY_CLASSIC_IOTBT),
        ("extended-raw", QUERY_EXTENDED),
    ]
    frames = [ProbeFrame(name, payload, "raw") for name, payload in raw]
    for name, payload in raw:
        frames.append(
            ProbeFrame(
                f"{name.removesuffix('-raw')}-magichome2",
                payload,
                "magichome2",
            )
        )
        frames.append(
            ProbeFrame(
                f"{name.removesuffix('-raw')}-magichome2-len-plus-2",
                payload,
                "magichome2-len-plus-2",
            )
        )
    return frames


def classify_packet(packet: bytes) -> tuple[ProtocolResolution, bytes, str] | None:
    """Classify raw or wrapped state data without trusting vendor names."""
    for candidate, framing in candidate_payloads(packet):
        if len(candidate) >= 20 and candidate[:2] == b"\xea\x81":
            layout_byte = candidate[4]
            capabilities = {"power", "extended-state"}
            notes = ["Extended frame shape selected independently of model registration."]
            if layout_byte in KNOWN_EXTENDED_MODELS:
                model_num = layout_byte
                firmware = candidate[5]
                codec = "extended-custom"
                confidence = 0.95
                capabilities.update({"rgb", "brightness", "candidate-white"})
            else:
                model_num = None
                firmware = None
                # The IOTBT DX23012 uses the same HSV byte positions (11/12/13)
                # as the extended-custom codec, but with a different model byte
                # and extra mode bytes at positions 7-8. Confirmed by state-diff
                # captures on 2026-08-26/27.
                codec = "iotbt-ea81"
                confidence = 0.90
                capabilities.update({"rgb", "brightness", "candidate-white", "power"})
                notes.append(
                    "IOTBT EA 81 layout: HSV at pos 11/12/13 confirmed by state-diff. "
                    "Pos 7-8 encode color/white mode. Model and firmware not inferred."
                )
            if model_num == 0xB6:
                capabilities.add("candidate-e122")
                notes.append("E1 22 remains experimental for this physical lamp.")
            return (
                ProtocolResolution(
                    frame_family="extended-ea81",
                    codec=codec,
                    confidence=confidence,
                    model_num=model_num,
                    firmware_version=firmware,
                    capabilities=capabilities,
                    source_packet_hex=candidate.hex(),
                    notes=notes,
                ),
                candidate,
                framing,
            )
        if len(candidate) == 14 and candidate[0] == 0x81 and checksum_valid(candidate):
            model_num = candidate[1]
            capabilities = {"power", "rgb", "brightness", "classic-state"}
            codec = "classic-9byte" if model_num == MODEL_FLOOR_LAMP_RGBCW else "classic-8byte"
            if model_num == MODEL_FLOOR_LAMP_RGBCW:
                capabilities.update({"warm-white", "cool-white"})
            return (
                ProtocolResolution(
                    frame_family="classic-81",
                    codec=codec,
                    confidence=0.98,
                    model_num=model_num,
                    firmware_version=candidate[10],
                    capabilities=capabilities,
                    source_packet_hex=candidate.hex(),
                ),
                candidate,
                framing,
            )
        if len(candidate) >= 11 and candidate[0] == 0x66:
            return (
                ProtocolResolution(
                    frame_family="original-66",
                    codec="original-ledenet",
                    confidence=0.75,
                    model_num=candidate[1],
                    capabilities={"power", "rgb"},
                    source_packet_hex=candidate.hex(),
                    notes=["Original response has no reliable checksum."],
                ),
                candidate,
                framing,
            )
    return None


def parse_state(packet: bytes) -> LightState | None:
    """Normalize a supported state response."""
    classified = classify_packet(packet)
    if classified is None:
        return None
    resolution, state, _framing = classified
    if resolution.frame_family == "classic-81":
        rgb = (state[6], state[7], state[8])
        brightness = max(rgb) if any(rgb) else max(state[9], state[11])
        return LightState(
            power=state[2] == POWER_ON,
            rgb=rgb,
            brightness=brightness,
            white=max(state[9], state[11]),
            model_num=state[1],
            firmware_version=state[10],
            raw_hex=state.hex(),
        )
    if resolution.frame_family == "extended-ea81":
        if resolution.codec == "iotbt-ea81":
            # IOTBT DX23012: HSV at pos 11/12/13, color/white mode at pos 7-8.
            # Hue is 0-180 (half degrees), saturation and brightness are 0-100.
            hue = clamp(state[IOTBT_POS_HUE], 0, 180) * 2 / 360
            saturation = clamp(state[IOTBT_POS_SATURATION], 0, 100) / 100
            value = clamp(state[IOTBT_POS_BRIGHTNESS], 0, 100) / 100
            red_f, green_f, blue_f = colorsys.hsv_to_rgb(hue, saturation, value)
            is_white_mode = state[IOTBT_POS_COLOR_WHITE_MODE] == IOTBT_WHITE_MODE
            white_percent = value * 100 if is_white_mode else 0
            return LightState(
                power=state[IOTBT_POS_POWER] == POWER_ON,
                rgb=(round(red_f * 255), round(green_f * 255), round(blue_f * 255)),
                brightness=round(value * 255),
                white=round(white_percent * 255 / 100),
                model_num=None,
                firmware_version=None,
                led_count=state[18] if len(state) > 18 else None,
                raw_hex=state.hex(),
            )
        hue = clamp(state[11], 0, 180) * 2 / 360
        saturation = clamp(state[12], 0, 100) / 100
        value = clamp(state[13], 0, 100) / 100
        red_f, green_f, blue_f = colorsys.hsv_to_rgb(hue, saturation, value)
        white_percent = state[15] if state[15] <= 100 else 0
        return LightState(
            power=state[6] == POWER_ON,
            rgb=(round(red_f * 255), round(green_f * 255), round(blue_f * 255)),
            brightness=round(value * 255),
            white=round(white_percent * 255 / 100),
            model_num=state[4],
            firmware_version=state[5],
            led_count=state[18] if len(state) > 18 else None,
            raw_hex=state.hex(),
        )
    if resolution.frame_family == "original-66":
        return LightState(
            power=state[2] == POWER_ON,
            rgb=(state[6], state[7], state[8]),
            brightness=max(state[6], state[7], state[8], state[9]),
            white=state[9],
            model_num=state[1],
            raw_hex=state.hex(),
        )
    return None


def classic_power(turn_on: bool) -> bytes:
    return with_checksum(bytes((0x71, POWER_ON if turn_on else POWER_OFF, 0x0F)))


def original_power(turn_on: bool) -> bytes:
    return bytes((0xCC, POWER_ON if turn_on else POWER_OFF, 0x33))


def classic_rgb(red: int, green: int, blue: int, *, nine_byte: bool) -> bytes:
    channels = [0x31, clamp(red, 0, 255), clamp(green, 0, 255), clamp(blue, 0, 255), 0]
    if nine_byte:
        channels.extend((0, 0xF0, 0x0F))
    else:
        channels.extend((0xF0, 0x0F))
    return with_checksum(bytes(channels))


def classic_white(brightness: int, *, nine_byte: bool) -> bytes:
    white = clamp(brightness, 0, 255)
    channels = [0x31, 0, 0, 0, white]
    if nine_byte:
        channels.extend((white, 0x0F, 0x0F))
    else:
        channels.extend((0x0F, 0x0F))
    return with_checksum(bytes(channels))


def extended_e0_rgb(red: int, green: int, blue: int, brightness: int) -> bytes:
    """Build the direct HSV command observed on MagicHome2 BLE devices."""
    h, s, _v = colorsys.rgb_to_hsv(
        clamp(red, 0, 255) / 255,
        clamp(green, 0, 255) / 255,
        clamp(blue, 0, 255) / 255,
    )
    return bytes(
        (
            0xE0,
            0x01,
            0x00,
            0xC1,
            round(h * 255) & 0xFF,
            round(s * 100),
            clamp(brightness, 0, 100),
            0,
            0,
            0,
            0,
            0x14,
            0,
            0,
        )
    )


def iotbt_e0_rgb(red: int, green: int, blue: int, brightness: int) -> bytes:
    """Build an HSV command for the IOTBT DX23012.

    The IOTBT lamp uses the same E0 command format as other MagicHome2 BLE
    devices, with hue in the 0-180 scale (half degrees) matching the state
    response layout.
    """
    h, s, _v = colorsys.rgb_to_hsv(
        clamp(red, 0, 255) / 255,
        clamp(green, 0, 255) / 255,
        clamp(blue, 0, 255) / 255,
    )
    return bytes(
        (
            IOTBT_CMD_E0_PREFIX,
            IOTBT_CMD_E0_ZONE,
            0x00,
            IOTBT_CMD_E0_RGB_OP,
            round(h * 180) & 0xFF,
            round(s * 100),
            clamp(brightness, 0, 100),
            0,
            0,
            0,
            0,
            0x14,
            0,
            0,
        )
    )


def iotbt_e0_white(brightness: int) -> bytes:
    """Build a white-mode command for the IOTBT DX23012."""
    return iotbt_e0_rgb(255, 255, 255, brightness)


def iotbt_3b_white(brightness: int, cct: int = 0) -> bytes:
    """Build a 0x3B 0xB1 white/CCT command (v2 unified format).

    Format: 3b b1 00 00 00 {cct} {bright} 00 00 1e 00 00 {checksum}
    cct: 0-100 (0=warm, 100=cool)
    brightness: 0-100
    """
    brightness = clamp(brightness, 0, 100)
    cct = clamp(cct, 0, 100)
    payload = bytes(
        (
            IOTBT_CMD_PREFIX,
            IOTBT_CMD_WHITE,
            0x00,
            0x00,
            0x00,
            cct & 0xFF,
            brightness & 0xFF,
            0x00,
            0x00,
            IOTBT_CMD_FILLER,
            0x00,
            0x00,
        )
    )
    return with_checksum(payload)


def iotbt_3b_brightness(brightness: int) -> bytes:
    """Build a standalone 0x3B 0x01 brightness command (v2 unified format).

    Format: 3b 01 00 00 {val} 00 {val} {dly_hi} {dly_mid} {dly_lo} {grd_hi} {grd_lo} {checksum}
    """
    brightness = clamp(brightness, 0, 100)
    payload = bytes(
        (
            IOTBT_CMD_PREFIX,
            IOTBT_CMD_BRIGHTNESS,
            0x00,
            0x00,
            brightness & 0xFF,
            0x00,
            brightness & 0xFF,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
        )
    )
    return with_checksum(payload)


def iotbt_power(turn_on: bool) -> bytes:
    """Build a power command for the IOTBT DX23012.

    Uses the same E0 zone-power format as the extended-custom codec.
    """
    zone = POWER_ON if turn_on else POWER_OFF
    return bytes((IOTBT_CMD_E0_PREFIX, IOTBT_CMD_E0_ZONE, 0x00, zone, *([0] * 10)))


def iotbt_3b_rgb(red: int, green: int, blue: int, brightness: int) -> bytes:
    """Build a standalone 0x3B 0xA1 HSV color command (v2 unified format).

    This is the format used by Symphony/v5+ LEDnetWF BLE devices.
    Includes an additive checksum.
    """
    h, s, _v = colorsys.rgb_to_hsv(
        clamp(red, 0, 255) / 255,
        clamp(green, 0, 255) / 255,
        clamp(blue, 0, 255) / 255,
    )
    brightness = clamp(brightness, 0, 100)
    packed = (int(h * 360) << 7) | int(s * 100)
    hs_hi = (packed >> 8) & 0xFF
    hs_lo = packed & 0xFF
    payload = bytes(
        (
            IOTBT_CMD_PREFIX,
            IOTBT_CMD_RGB,
            hs_hi,
            hs_lo,
            brightness,
            0x00,
            0x00,
            clamp(red, 0, 255),
            clamp(green, 0, 255),
            clamp(blue, 0, 255),
            0x00,
            0x00,
        )
    )
    return with_checksum(payload)


def iotbt_e0_3b_rgb(red: int, green: int, blue: int, brightness: int) -> bytes:
    """Build an E0-wrapped 0x3B 0xA1 color command (v3 unified format).

    The E0 wrapper matches the working power command format (E0 01 00 ...).
    No checksum (v3 commands omit it).
    """
    h, s, _v = colorsys.rgb_to_hsv(
        clamp(red, 0, 255) / 255,
        clamp(green, 0, 255) / 255,
        clamp(blue, 0, 255) / 255,
    )
    brightness = clamp(brightness, 0, 100)
    packed = (int(h * 360) << 7) | int(s * 100)
    hs_hi = (packed >> 8) & 0xFF
    hs_lo = packed & 0xFF
    return bytes(
        (
            IOTBT_CMD_E0_PREFIX,
            IOTBT_CMD_E0_ZONE,
            0x00,
            IOTBT_CMD_PREFIX,
            IOTBT_CMD_RGB,
            hs_hi,
            hs_lo,
            brightness,
            0x00,
            0x00,
            clamp(red, 0, 255),
            clamp(green, 0, 255),
            clamp(blue, 0, 255),
            0x00,
            0x00,
        )
    )


def iotbt_e2_rgb(red: int, green: int, blue: int, brightness: int) -> bytes:
    """Build an IOTBT Telink 0xE2 hue-based color command.

    Format: [0xE2, 0x0B, hue, brightness_byte]
    - hue: 0=white, 1-240 quantized hue (24-bin)
    - brightness_byte: 0xE0 | level (level 0-31, gamma corrected)
    """
    r, g, b = clamp(red, 0, 255), clamp(green, 0, 255), clamp(blue, 0, 255)
    if r == g == b:
        hue = 0
    else:
        h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.05:
            hue = 0
        else:
            bin_idx = int(round((h * 360 % 360) / 360 * 24)) % 24
            hue = max(1, min(240, int(round(bin_idx * 10)) + 1))
    brightness_255 = int(clamp(brightness, 0, 100) * 255 / 100)
    level = int(round((brightness_255 / 255) ** 2.2 * 31))
    level = max(0, min(31, level))
    brightness_byte = IOTBT_CMD_E0_PREFIX | level
    return bytes((IOTBT_CMD_TELINK_PREFIX, IOTBT_CMD_TELINK_OP, hue & 0xFF, brightness_byte))


def iotbt_e1_segment_rgb(
    red: int,
    green: int,
    blue: int,
    brightness: int,
    *,
    segment_count: int = IOTBT_SEGMENT_COUNT,
) -> bytes:
    """Build the verified E1 03 app-style segment color command.

    The Android Surplife app sends this segmented HSV command to FF01. The
    DX23012 lamp persists the new color in its EA 81 state, unlike the older
    3B A1 command which only changes the physical output.

    Format:
      E1 03 00 <segment_count> 00 00 <segment_count>
      followed by ``segment_count`` copies of ``A1 <hue> <sat> <bright>``.

    Hue uses the 0-180 (half degrees) scale that matches the EA 81 state
    layout. Saturation and brightness are 0-100 percent.

    Verified on the physical DX23012 (IOTBT144) on 2026-08-26 with 20
    segments and the 0x0A transport family. See
    docs/DX23012_PROTOCOL_FINDINGS.md.
    """
    if not 1 <= segment_count <= 255:
        raise ValueError("segment_count must be between 1 and 255")
    h, s, _v = colorsys.rgb_to_hsv(
        clamp(red, 0, 255) / 255,
        clamp(green, 0, 255) / 255,
        clamp(blue, 0, 255) / 255,
    )
    hue = round(h * 180) & 0xFF
    saturation = clamp(round(s * 100), 0, 100)
    level = clamp(brightness, 0, 100)
    header = bytes(
        (
            IOTBT_CMD_E1_PREFIX,
            IOTBT_CMD_E1_SEGMENT_OP,
            0x00,
            segment_count & 0xFF,
            0x00,
            0x00,
            segment_count & 0xFF,
        )
    )
    record = bytes((IOTBT_CMD_E1_SEGMENT_RECORD, hue, saturation, level))
    return header + record * segment_count


def extended_e0_white(brightness: int) -> bytes:
    return extended_e0_rgb(255, 255, 255, brightness)


def extended_zone_power_off(zone: int) -> bytes:
    if zone not in (0x23, 0x24):
        raise ValueError("zone must be 0x23 or 0x24")
    return bytes((0xE0, 0x01, 0x00, zone, *([0] * 10)))


def experimental_e122_fill(
    red: int,
    green: int,
    blue: int,
    *,
    white: int = 0,
) -> bytes:
    """Build PR #544's twenty-segment uniform fill candidate."""
    white = clamp(white, 0, 255)
    if white and not (red or green or blue):
        record = bytes((0, 100, 0, 0, round(white * 100 / 255)))
    else:
        h, s, v = colorsys.rgb_to_hsv(
            clamp(red, 0, 255) / 255,
            clamp(green, 0, 255) / 255,
            clamp(blue, 0, 255) / 255,
        )
        record = bytes((int(h * 180), int(s * 100), int(v * 100), 0, 0))
    return bytes.fromhex("e1220000000014") + record * 20


def frame_for_transport(payload: bytes, transport: str, sequence: int) -> bytes:
    if transport == "raw":
        return payload
    if transport == "magichome2":
        return wrap_magichome(payload, sequence)
    if transport == "magichome2-len-plus-2":
        return wrap_magichome(payload, sequence, length_bias=2)
    raise ValueError(f"unsupported transport: {transport}")
