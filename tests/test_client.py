from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from custom_components.surplife_ble.core.client import LightClient
from custom_components.surplife_ble.core.framing import with_checksum
from custom_components.surplife_ble.core.models import (
    LightState,
    TrafficRecord,
    apply_assumed_rgb,
    rgb_chromaticity_matches,
    rgb_state_matches,
)
from custom_components.surplife_ble.core.protocol import parse_state


@dataclass
class FakeDescriptor:
    uuid: str = "00002902-0000-1000-8000-00805f9b34fb"


@dataclass
class FakeCharacteristic:
    uuid: str
    handle: int
    properties: list[str]
    descriptors: list[FakeDescriptor] = field(default_factory=list)


@dataclass
class FakeService:
    uuid: str
    handle: int
    characteristics: list[FakeCharacteristic]


@dataclass
class FakeDevice:
    address: str = "DEVICE-UUID"
    name: str = "DX23012"


class FakeBleakClient:
    def __init__(self) -> None:
        self.is_connected = False
        self.notify = FakeCharacteristic(
            "0000ff02-0000-1000-8000-00805f9b34fb", 0x14, ["read", "notify"]
        )
        self.write = FakeCharacteristic(
            "0000ff01-0000-1000-8000-00805f9b34fb",
            0x17,
            ["write", "write-without-response"],
        )
        self.services = [
            FakeService(
                "0000ffff-0000-1000-8000-00805f9b34fb",
                1,
                [self.notify, self.write],
            )
        ]
        self.callbacks: dict[str, Any] = {}
        self.writes: list[tuple[str, bytes, bool | None]] = []
        self.state = with_checksum(
            bytes((0x81, 0x0E, 0x23, 0x61, 0, 0x10, 0xFF, 0, 0, 0, 9, 0, 0xF0))
        )

    async def connect(self) -> bool:
        self.is_connected = True
        return True

    async def disconnect(self) -> bool:
        self.is_connected = False
        return True

    async def start_notify(self, uuid: str, callback: Any) -> None:
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid: str) -> None:
        self.callbacks.pop(uuid, None)

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool | None = None) -> None:
        self.writes.append((uuid, bytes(data), response))
        if bytes(data) == bytes.fromhex("818a8b96"):
            callback = self.callbacks[self.notify.uuid]
            callback(self.notify, bytearray(self.state))

    async def read_gatt_char(self, uuid: str) -> bytearray:
        return bytearray()


@pytest.mark.asyncio
async def test_probe_requires_explicit_permission() -> None:
    fake = FakeBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        with pytest.raises(PermissionError):
            await client.probe(allow_writes=False)


@pytest.mark.asyncio
async def test_repeatable_probe_unlocks_classic_control() -> None:
    fake = FakeBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        profile = await client.inspect_gatt()
        assert profile.writable[0].uuid.endswith("34fb")
        resolution = await client.probe(allow_writes=True, response_timeout=0.01)
        assert resolution is not None
        assert resolution.repeatable
        assert resolution.model_num == 0x0E

        await client.set_rgb(255, 0, 0, brightness=50)
        assert fake.writes[-1][1][0] == 0x31
        assert client.state is not None
        assert client.state.assumed


class FakeIotbtBleakClient(FakeBleakClient):
    """Fake device that responds to the magichome2-wrapped probe with EA81 state."""

    def __init__(self) -> None:
        super().__init__()
        self.respond = True
        self.state = bytes.fromhex(
            "ea810100aa0923240150f0006464006405001dffffffff01000000"
        )

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool | None = None) -> None:
        self.writes.append((uuid, bytes(data), response))
        if (
            self.respond
            and data[2:4] == bytes.fromhex("8000")
            and data.endswith(bytes.fromhex("ea818a8b"))
        ):
            callback = self.callbacks[self.notify.uuid]
            callback(self.notify, bytearray(self.state))


@pytest.mark.asyncio
async def test_iotbt_set_rgb_uses_e1_03_segment_format() -> None:
    fake = FakeIotbtBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        resolution = await client.probe(allow_writes=True, response_timeout=0.5)
        assert resolution is not None
        assert resolution.codec == "iotbt-ea81"
        assert resolution.transport == "magichome2"

        await client.set_rgb(255, 0, 0, brightness=50)
        sent = next(
            data for _uuid, data, _response in reversed(fake.writes) if data[8:10] == b"\xe1\x03"
        )
        assert sent[7] == 0x0B
        assert sent[8] == 0xE1
        assert sent[9] == 0x03
        assert client.state is not None
        # The fake's static state does not match the commanded color, so
        # the client keeps the commanded values as assumed state.
        assert client.state.assumed
        assert client.state.rgb == (255, 0, 0)


@pytest.mark.asyncio
async def test_iotbt_set_white_uses_verified_3b_b1_format() -> None:
    fake = FakeIotbtBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        await client.set_white(50)
        sent = fake.writes[-1][1]
        assert sent[8] == 0x3B
        assert sent[9] == 0xB1


@pytest.mark.asyncio
async def test_iotbt_set_brightness_resends_color_via_e1_03() -> None:
    """Brightness resends the current color with new brightness via E1 03."""
    fake = FakeIotbtBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        await client.set_rgb(255, 0, 0, brightness=50)
        first_count = len(fake.writes)
        await client.set_brightness(75)
        # set_brightness re-sends the color command via set_rgb, which
        # uses the E1 03 encoder. The E1 03 command is the first write of
        # this batch; the second is the confirmation query.
        sent = fake.writes[first_count][1]
        assert sent[7] == 0x0B
        assert sent[8] == 0xE1
        assert sent[9] == 0x03


class FakeIotbtConfirmingBleakClient(FakeIotbtBleakClient):
    """Fake device that updates its state to match the commanded color.

    This simulates a real DX23012 that persists the E1 03 segment command
    in its EA 81 state response.
    """

    def __init__(self) -> None:
        super().__init__()
        self._commanded_state: bytes | None = None

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool | None = None) -> None:
        self.writes.append((uuid, bytes(data), response))
        is_probe = data[2:4] == bytes.fromhex("8000") and data.endswith(
            bytes.fromhex("ea818a8b")
        )
        is_segment_command = data[8:10] == bytes.fromhex("e103")
        if is_probe or (is_segment_command and self._commanded_state is not None):
            callback = self.callbacks[self.notify.uuid]
            state = self._commanded_state if is_segment_command else self.state
            callback(self.notify, bytearray(state))


@pytest.mark.asyncio
async def test_iotbt_set_rgb_confirmed_state_is_not_assumed() -> None:
    """When the device reports a matching state, assumed is False."""
    fake = FakeIotbtConfirmingBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        fake._commanded_state = bytes.fromhex(
            "ea810100aa0923240150f0006432006405001dffffffff01000000"
        )
        await client.set_rgb(255, 0, 0, brightness=50)
        assert client.state is not None
        assert not client.state.assumed


@pytest.mark.asyncio
async def test_query_state_returns_none_without_a_fresh_response() -> None:
    fake = FakeIotbtBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        fake.respond = False

        async def no_response(
            _wait_seconds: float, *, allow_reads: bool = True
        ) -> None:
            return None

        client._await_classification = no_response  # type: ignore[method-assign]
        assert await client.query_state() is None


@pytest.mark.asyncio
async def test_reconnect_subscribes_before_sending_control() -> None:
    first = FakeIotbtBleakClient()
    second = FakeIotbtBleakClient()
    clients = iter((first, second))
    client = LightClient(FakeDevice(), client_factory=lambda _device: next(clients))  # type: ignore[arg-type]
    await client.connect()
    await client.probe(allow_writes=True, response_timeout=0.5)
    first.is_connected = False
    client.mark_disconnected(first)

    await client.connect()
    await client.set_rgb(255, 0, 0, brightness=50)

    sent = next(
        data for _uuid, data, _response in second.writes if data[8:10] == b"\xe1\x03"
    )
    assert second.notify.uuid in second.callbacks
    assert sent[7] == 0x0B
    await client.disconnect()


@pytest.mark.asyncio
async def test_iotbt_white_brightness_stays_in_white_mode() -> None:
    fake = FakeIotbtBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        await client.probe(allow_writes=True, response_timeout=0.5)
        client.state = parse_state(
            bytes.fromhex("ea810100aa0923256f50f0786464006405001dffffffff01000000")
        )
        await client.set_brightness(75)
        assert fake.writes[-1][1][8:10] == bytes.fromhex("3bb1")


def test_rgb_state_match_ignores_rgb_value_brightness() -> None:
    state = LightState(rgb=(0, 0, 102), brightness=102, white=0)
    assert rgb_state_matches(state, (0, 0, 255), 102)
    assert rgb_chromaticity_matches((255, 0, 0), (128, 0, 0))


@pytest.mark.asyncio
async def test_raw_write_is_separately_guarded() -> None:
    fake = FakeBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    async with client:
        with pytest.raises(PermissionError):
            await client.raw_write(fake.write.uuid, b"\x01", allow_raw_write=False)
        with pytest.raises(ValueError, match="not writable"):
            await client.raw_write(fake.notify.uuid, b"\x01", allow_raw_write=True)


@pytest.mark.asyncio
async def test_replay_pre_validates_characteristics() -> None:
    fake = FakeBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    records = [
        TrafficRecord(
            device_id="DEVICE-UUID",
            direction="tx",
            payload_hex="71230f",
            characteristic="0000ff01-0000-1000-8000-00805f9b34fb",
        ),
        TrafficRecord(
            device_id="DEVICE-UUID",
            direction="tx",
            payload_hex="0102",
            characteristic="0000ff02-0000-1000-8000-00805f9b34fb",  # not writable
        ),
    ]
    async with client:
        with pytest.raises(ValueError, match="not writable"):
            await client.replay(records, allow_raw_write=True)


@pytest.mark.asyncio
async def test_replay_rejects_missing_characteristic() -> None:
    fake = FakeBleakClient()
    client = LightClient(FakeDevice(), client_factory=lambda _device: fake)  # type: ignore[arg-type]
    records = [
        TrafficRecord(device_id="DEVICE-UUID", direction="tx", payload_hex="01"),
    ]
    async with client:
        with pytest.raises(ValueError, match="must name a characteristic"):
            await client.replay(records, allow_raw_write=True)


def test_assumed_rgb_replaces_stale_polled_color_mode() -> None:
    state = LightState(
        power=True,
        rgb=(255, 255, 255),
        brightness=255,
        white=255,
    )

    result = apply_assumed_rgb(state, (255, 0, 0), 128)

    assert result.power is True
    assert result.rgb == (255, 0, 0)
    assert result.brightness == 128
    assert result.white == 0
    assert result.assumed is True


def test_polled_state_is_not_assumed_without_local_rgb() -> None:
    state = LightState(power=True, rgb=(255, 255, 255), brightness=255, white=255)

    result = apply_assumed_rgb(state, None, None)

    assert result.white == 255
    assert result.assumed is False
