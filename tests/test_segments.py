"""Segmented transport, per-segment colour, strip profile and product table."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.surplife_ble.core.client import (
    POWER_FORM_APP,
    POWER_FORM_ZONE,
    LightClient,
)
from custom_components.surplife_ble.core.framing import (
    NotificationReassembler,
    is_ack_frame,
    segment_magichome,
    with_checksum,
    wrap_magichome,
)
from custom_components.surplife_ble.core.products import (
    PRODUCTS,
    TIER_RECOGNISED,
    TIER_SYMPHONY_E1,
    is_symphony_e1_product,
    product_for,
)
from custom_components.surplife_ble.core.protocol import (
    SYMPHONY_CONFIG_QUERY,
    classify_packet,
    iotbt_e1_segments,
    parse_symphony_config,
)
from tests.test_client import FakeDevice, FakeIotbtBleakClient

PROFILE_RESPONSE = with_checksum(bytes.fromhex("0063003c00140302001e"))


# --- framing ---------------------------------------------------------------


def test_short_payload_stays_one_frame() -> None:
    payload = bytes(range(10))
    frames = segment_magichome(payload, 5, command_family=0x0B, max_frame_length=20)
    assert frames == [wrap_magichome(payload, 5, command_family=0x0B)]


def test_long_payload_matches_app_segment_layout() -> None:
    payload = bytes(range(30))
    frames = segment_magichome(payload, 7, command_family=0x0B, max_frame_length=20)
    first = frames[0]
    assert len(first) == 20
    assert first[0] & 0x40
    assert first[1] == 7
    assert first[2:4] == b"\x00\x00"
    assert int.from_bytes(first[4:6], "big") == 30
    assert first[6] == 20 - 7
    assert first[7] == 0x0B
    assert first[8:] == payload[:12]
    second, third = frames[1:]
    assert int.from_bytes(second[2:4], "big") == 1
    assert second[4] == 15 and second[5:] == payload[12:27]
    assert int.from_bytes(third[2:4], "big") == 0x8000 | 2
    assert third[4] == 3 and third[5:] == payload[27:]


def test_segmented_frames_reassemble_to_payload() -> None:
    payload = iotbt_e1_segments([(255, 0, 0), (0, 0, 255)] * 10, 80)
    frames = segment_magichome(payload, 3, command_family=0x0B, max_frame_length=30)
    assert len(frames) > 2
    reassembler = NotificationReassembler()
    results: list[bytes] = []
    for frame in frames:
        results.extend(reassembler.feed(frame))
    assert payload in results


def test_ack_frames_are_recognised_and_dropped() -> None:
    ack = bytes.fromhex("0a05800000010000")
    assert is_ack_frame(ack)
    assert NotificationReassembler().feed(ack) == []
    assert not is_ack_frame(wrap_magichome(b"\x81\x8a\x8b", 1))


# --- protocol --------------------------------------------------------------


def test_e1_segments_encodes_each_colour() -> None:
    payload = iotbt_e1_segments([(255, 0, 0), (0, 0, 255)], [100, 50])
    assert payload[:7] == bytes.fromhex("e1030002000002")
    assert payload[7:11] == bytes.fromhex("a1006464")
    assert payload[11] == 0xA1
    assert ((payload[12] << 8) | payload[13]) >> 7 == 240
    assert payload[14] == 50


def test_e1_segments_rejects_mismatched_brightness_list() -> None:
    with pytest.raises(ValueError):
        iotbt_e1_segments([(1, 2, 3), (4, 5, 6)], [10])


def test_parse_symphony_config_reads_profile() -> None:
    profile = parse_symphony_config(PROFILE_RESPONSE)
    assert profile is not None
    assert profile.point_count == 60
    assert profile.segment_count == 20
    assert profile.ic_type == 3
    assert profile.music_segment_count == 30


def test_parse_symphony_config_reads_json_wrapped_response() -> None:
    body = '{"code":0,"payload":"' + PROFILE_RESPONSE.hex().upper() + '"}'
    frame = bytes.fromhex("041e8000") + len(body).to_bytes(2, "big") + bytes((len(body) + 1, 0x0A))
    profile = parse_symphony_config(frame + body.encode())
    assert profile is not None and profile.segment_count == 20


def test_parse_symphony_config_rejects_bad_checksum() -> None:
    corrupt = PROFILE_RESPONSE[:-1] + bytes([(PROFILE_RESPONSE[-1] + 1) & 0xFF])
    assert parse_symphony_config(corrupt) is None


# --- products --------------------------------------------------------------


def test_lamp_product_is_symphony_tier() -> None:
    lamp = product_for(170)
    assert lamp is not None
    assert lamp.tier == TIER_SYMPHONY_E1
    assert "DX23012" in lamp.label


def test_reference_extended_product_is_not_symphony_tier() -> None:
    assert not is_symphony_e1_product(0xB6)
    assert not is_symphony_e1_product(None)  # type: ignore[arg-type]
    assert product_for(0xB6) is None or product_for(0xB6).tier == TIER_RECOGNISED


def test_symphony_tier_matches_app_table() -> None:
    tier_a = sorted(pid for pid in PRODUCTS if is_symphony_e1_product(pid))
    assert tier_a == [57, 69, 76, 83, 86, 170, 171, 175, 181, 183, 184, 186, 187, 188]


def test_lamp_state_carries_product_and_segments_capability() -> None:
    state = bytes.fromhex("ea810100aa0923240150f0006464006405001dffffffff01000000")
    classified = classify_packet(state)
    assert classified is not None
    resolution = classified[0]
    assert resolution.product_id == 0xAA
    assert "segments" in resolution.capabilities


# --- client ----------------------------------------------------------------


class FakeLamp(FakeIotbtBleakClient):
    """IOTBT lamp that answers the profile query and applies power commands."""

    def __init__(self, *, mtu: int | None = None, accepts_app_power: bool = True) -> None:
        super().__init__()
        if mtu is not None:
            self.mtu_size = mtu
        self.accepts_app_power = accepts_app_power
        self._reassembler = NotificationReassembler()

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool | None = None) -> None:
        await super().write_gatt_char(uuid, data, response)
        data = bytes(data)
        if data.endswith(SYMPHONY_CONFIG_QUERY):
            self.callbacks[self.notify.uuid](self.notify, bytearray(PROFILE_RESPONSE))
            return
        body = data[8:] if len(data) > 8 and data[2:4] == b"\x80\x00" else b""
        if body[:2] == b"\xe0\x01" and len(body) == 15 and self.accepts_app_power:
            self._set_power(body[4])
        elif body[:2] == b"\xe0\x01" and len(body) == 14:
            self._set_power(body[3])

    def _set_power(self, value: int) -> None:
        state = bytearray(self.state)
        state[6] = value
        self.state = bytes(state)


def _client(fake: Any) -> LightClient:
    return LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_power_uses_app_form_on_family_0b_and_caches_it() -> None:
    fake = FakeLamp()
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        await client.set_power(False)
        assert client._power_form == POWER_FORM_APP
        assert client.state is not None and client.state.power is False
        sent = next(d for _u, d, _r in fake.writes if d[8:10] == b"\xe0\x01")
        assert sent[7] == 0x0B
        assert len(sent) == 8 + 15


@pytest.mark.asyncio
async def test_power_falls_back_to_zone_form_when_ignored() -> None:
    fake = FakeLamp(accepts_app_power=False)
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        await client.set_power(False)
        assert client._power_form == POWER_FORM_ZONE
        assert client.state is not None and client.state.power is False
        before = len(fake.writes)
        await client.set_power(True)
        # A cached form costs a single write.
        assert len(fake.writes) == before + 1


@pytest.mark.asyncio
async def test_query_strip_profile_sets_segment_count() -> None:
    fake = FakeLamp()
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        profile = await client.query_strip_profile(response_timeout=0.2)
        assert profile is not None
        assert client.segment_count == 20


@pytest.mark.asyncio
async def test_set_segments_stretches_colours_and_remembers_them() -> None:
    fake = FakeLamp()
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        await client.set_segments([(255, 0, 0), (0, 0, 255)], brightness=60)
        sent = next(d for _u, d, _r in fake.writes if d[8:10] == b"\xe1\x03")
        assert sent[7] == 0x0B
        assert sent[11] == 20
        assert client.segments is not None
        assert client.segments[:10] == ((255, 0, 0),) * 10
        assert client.segments[10:] == ((0, 0, 255),) * 10
        # Brightness changes keep the per-segment pattern.
        before = len(fake.writes)
        await client.set_brightness(30)
        resent = next(d for _u, d, _r in fake.writes[before:] if d[8:10] == b"\xe1\x03")
        assert resent[8:] == iotbt_e1_segments(client.segments, 30)
        # A uniform colour clears the pattern.
        await client.set_rgb(0, 255, 0)
        assert client.segments is None


@pytest.mark.asyncio
async def test_set_segments_rejects_too_many_colours() -> None:
    fake = FakeLamp()
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        with pytest.raises(ValueError):
            await client.set_segments([(1, 1, 1)] * 21)


@pytest.mark.asyncio
async def test_long_command_is_segmented_when_mtu_is_small() -> None:
    fake = FakeLamp(mtu=50)
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        before = len(fake.writes)
        await client.set_segments([(255, 0, 0)] * 20)
        frames = [d for _u, d, _r in fake.writes[before:] if d[0] & 0x40]
        assert len(frames) >= 2
        assert all(len(frame) <= 47 for frame in frames)
        assert all(r is True for _u, d, r in fake.writes[before:] if d[0] & 0x40)
        reassembler = NotificationReassembler()
        out: list[bytes] = []
        for frame in frames:
            out.extend(reassembler.feed(frame))
        assert iotbt_e1_segments([(255, 0, 0)] * 20, 100) in out


@pytest.mark.asyncio
async def test_unknown_mtu_keeps_single_write() -> None:
    fake = FakeLamp()
    client = _client(fake)
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        before = len(fake.writes)
        await client.set_segments([(255, 0, 0)] * 20)
        assert not any(d[0] & 0x40 for _u, d, _r in fake.writes[before:])
