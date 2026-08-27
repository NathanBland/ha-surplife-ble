"""Async BLE client with guarded probe, control, raw-write, and replay paths."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Protocol

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .capture import JsonlCapture
from .framing import NotificationReassembler, SequenceCounter
from .models import (
    GattCharacteristic,
    GattProfile,
    GattService,
    LightState,
    ProtocolResolution,
    TrafficRecord,
)
from .protocol import (
    ProbeFrame,
    byte_to_percent,
    classic_power,
    classic_rgb,
    classic_white,
    classify_packet,
    experimental_e122_fill,
    extended_e0_rgb,
    extended_e0_white,
    extended_zone_power_off,
    frame_for_transport,
    iotbt_3b_rgb,
    iotbt_3b_white,
    iotbt_power,
    original_power,
    parse_state,
    probe_frames,
)

ClientFactory = Callable[[BLEDevice], Any | Awaitable[Any]]


class BleakClientLike(Protocol):
    is_connected: bool
    services: Any

    async def connect(self) -> bool: ...
    async def disconnect(self) -> bool: ...
    async def start_notify(self, char_specifier: Any, callback: Callable[..., Any]) -> None: ...
    async def stop_notify(self, char_specifier: Any) -> None: ...
    async def write_gatt_char(
        self, char_specifier: Any, data: bytes, response: bool | None = None
    ) -> None: ...
    async def read_gatt_char(self, char_specifier: Any) -> bytearray: ...


def _uuid_is(uuid: str, short: str) -> bool:
    compact = uuid.lower().replace("-", "")
    return compact == short.lower() or compact.startswith(f"0000{short.lower()}")


class LightClient:
    """One BLE light connection and its resolved transport/protocol state."""

    def __init__(
        self,
        device: BLEDevice,
        advertisement: AdvertisementData | None = None,
        *,
        capture_path: str | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.device = device
        self.advertisement = advertisement
        self.capture = JsonlCapture(capture_path)
        self._client_factory = client_factory or BleakClient
        self._client: BleakClientLike | None = None
        self._profile: GattProfile | None = None
        self._notifications: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self._reassemblers: dict[str, NotificationReassembler] = {}
        self._notifying: set[str] = set()
        self._sequence = SequenceCounter()
        self._write_characteristic: GattCharacteristic | None = None
        self._probe_frame: ProbeFrame | None = None
        self.resolution: ProtocolResolution | None = None
        self.state: LightState | None = None

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    def set_ble_device(
        self,
        device: BLEDevice,
        advertisement: AdvertisementData | None = None,
    ) -> None:
        """Refresh the platform BLE object while preserving protocol resolution."""
        self.device = device
        self.advertisement = advertisement

    async def connect(self) -> None:
        if self.connected:
            return
        created = self._client_factory(self.device)
        self._client = await created if inspect.isawaitable(created) else created
        if not self._client.is_connected:
            await self._client.connect()
        self._profile = self._build_profile()
        self._write_characteristic = self._choose_write_characteristic(self._profile)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        for uuid in tuple(self._notifying):
            with suppress(Exception):
                await self._client.stop_notify(uuid)
        self._notifying.clear()
        if self._client.is_connected:
            await self._client.disconnect()

    async def __aenter__(self) -> LightClient:
        await self.connect()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.disconnect()

    def _require_client(self) -> BleakClientLike:
        if self._client is None or not self._client.is_connected:
            raise RuntimeError("BLE client is not connected")
        return self._client

    def _build_profile(self) -> GattProfile:
        client = self._require_client()
        services: list[GattService] = []
        for service in client.services:
            characteristics: list[GattCharacteristic] = []
            for characteristic in service.characteristics:
                descriptors = [
                    str(descriptor.uuid).lower() for descriptor in characteristic.descriptors
                ]
                characteristics.append(
                    GattCharacteristic(
                        uuid=str(characteristic.uuid).lower(),
                        handle=int(characteristic.handle),
                        properties=sorted(str(value) for value in characteristic.properties),
                        descriptors=descriptors,
                    )
                )
            services.append(
                GattService(
                    uuid=str(service.uuid).lower(),
                    handle=int(service.handle),
                    characteristics=characteristics,
                )
            )
        return GattProfile(device_id=self.device.address, services=services)

    async def inspect_gatt(self) -> GattProfile:
        if not self.connected:
            await self.connect()
        assert self._profile is not None
        return self._profile

    @staticmethod
    def _choose_write_characteristic(profile: GattProfile) -> GattCharacteristic | None:
        writable = profile.writable
        return next((item for item in writable if _uuid_is(item.uuid, "ff01")), None) or (
            writable[0] if writable else None
        )

    async def _start_notifications(self) -> None:
        client = self._require_client()
        assert self._profile is not None
        ordered = sorted(
            self._profile.notifiable,
            key=lambda item: (not _uuid_is(item.uuid, "ff02"), item.uuid),
        )
        for characteristic in ordered:
            if characteristic.uuid in self._notifying:
                continue

            def callback(sender: Any, data: bytearray, *, uuid: str = characteristic.uuid) -> None:
                sender_uuid = str(getattr(sender, "uuid", uuid)).lower()
                payload = bytes(data)
                self.capture.write(
                    TrafficRecord(
                        device_id=self.device.address,
                        direction="rx",
                        payload_hex=payload.hex(),
                        characteristic=sender_uuid,
                        note="notification",
                    )
                )
                reassembler = self._reassemblers.setdefault(sender_uuid, NotificationReassembler())
                complete = reassembler.feed(payload)
                if complete:
                    for packet in complete:
                        self._notifications.put_nowait((sender_uuid, packet))

            try:
                await client.start_notify(characteristic.uuid, callback)
            except Exception as err:
                self.capture.write(
                    TrafficRecord(
                        device_id=self.device.address,
                        direction="diagnostic",
                        payload_hex="",
                        characteristic=characteristic.uuid,
                        note=f"start_notify failed: {err}",
                    )
                )
                continue
            self._notifying.add(characteristic.uuid)

    def _flush_notifications(self) -> None:
        while not self._notifications.empty():
            with suppress(asyncio.QueueEmpty):
                self._notifications.get_nowait()
        for reassembler in self._reassemblers.values():
            reassembler.reset()

    async def _write(
        self,
        payload: bytes,
        *,
        characteristic: GattCharacteristic | None = None,
        framing: str | None = None,
        note: str | None = None,
    ) -> None:
        client = self._require_client()
        target = characteristic or self._write_characteristic
        if target is None:
            raise RuntimeError("device exposes no writable GATT characteristic")
        response = (
            "write-without-response" not in target.properties and "write" in target.properties
        )
        await client.write_gatt_char(target.uuid, payload, response=response)
        self.capture.write(
            TrafficRecord(
                device_id=self.device.address,
                direction="tx",
                payload_hex=payload.hex(),
                characteristic=target.uuid,
                framing=framing,
                note=note,
            )
        )

    async def _send_inner(
        self,
        payload: bytes,
        transport: str,
        *,
        note: str,
        characteristic: GattCharacteristic | None = None,
    ) -> None:
        framed = frame_for_transport(payload, transport, self._sequence.next())
        await self._write(
            framed,
            characteristic=characteristic,
            framing=transport,
            note=note,
        )

    async def _read_candidates(self) -> list[tuple[str, bytes]]:
        client = self._require_client()
        assert self._profile is not None
        packets: list[tuple[str, bytes]] = []
        for service in self._profile.services:
            for characteristic in service.characteristics:
                if "read" not in characteristic.properties:
                    continue
                with suppress(Exception):
                    data = bytes(await client.read_gatt_char(characteristic.uuid))
                    if data:
                        packets.append((characteristic.uuid, data))
                        self.capture.write(
                            TrafficRecord(
                                device_id=self.device.address,
                                direction="rx",
                                payload_hex=data.hex(),
                                characteristic=characteristic.uuid,
                                note="read fallback",
                            )
                        )
        return packets

    async def _await_classification(
        self, wait_seconds: float
    ) -> tuple[ProtocolResolution, LightState | None] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while (remaining := deadline - loop.time()) > 0:
            try:
                _characteristic, packet = await asyncio.wait_for(
                    self._notifications.get(), timeout=remaining
                )
            except TimeoutError:
                break
            classified = classify_packet(packet)
            if classified:
                resolution, _inner, framing = classified
                resolution.notes.append(f"Response interpretation: {framing}")
                return resolution, parse_state(packet)
        for _characteristic, packet in await self._read_candidates():
            classified = classify_packet(packet)
            if classified:
                resolution, _inner, framing = classified
                resolution.notes.append(f"Read interpretation: {framing}")
                return resolution, parse_state(packet)
        return None

    async def probe(
        self,
        *,
        allow_writes: bool,
        response_timeout: float = 1.25,
    ) -> ProtocolResolution | None:
        """Send known state queries and require a repeatable response for control."""
        if not allow_writes:
            raise PermissionError("probing requires allow_writes=True")
        if not self.connected:
            await self.connect()
        await self._start_notifications()
        best: tuple[ProtocolResolution, LightState | None, ProbeFrame] | None = None
        assert self._profile is not None
        write_targets = sorted(
            self._profile.writable,
            key=lambda item: (not _uuid_is(item.uuid, "ff01"), item.uuid),
        )
        for write_target in write_targets:
            for candidate in probe_frames():
                self._flush_notifications()
                try:
                    await self._send_inner(
                        candidate.payload,
                        candidate.transport,
                        note=candidate.name,
                        characteristic=write_target,
                    )
                except Exception as err:
                    self.capture.write(
                        TrafficRecord(
                            device_id=self.device.address,
                            direction="diagnostic",
                            payload_hex=candidate.payload.hex(),
                            characteristic=write_target.uuid,
                            framing=candidate.transport,
                            note=f"probe write failed: {err}",
                        )
                    )
                    continue
                first = await self._await_classification(response_timeout)
                if first is None:
                    continue
                resolution, state = first
                resolution.notes.append(f"Probe candidate: {candidate.name}")
                resolution.notes.append(f"Write characteristic: {write_target.uuid}")
                best = (resolution, state, candidate)

                self._flush_notifications()
                await self._send_inner(
                    candidate.payload,
                    candidate.transport,
                    note=f"{candidate.name}:confirmation",
                    characteristic=write_target,
                )
                confirmation = await self._await_classification(response_timeout)
                if confirmation:
                    confirmed_resolution, confirmed_state = confirmation
                    same_identity = (
                        confirmed_resolution.frame_family == resolution.frame_family
                        and confirmed_resolution.model_num == resolution.model_num
                    )
                    if same_identity:
                        resolution.repeatable = True
                        resolution.confidence = 1.0
                        resolution.notes.append(
                            "State response repeated with the same frame family/model."
                        )
                        self.resolution = resolution
                        self.state = confirmed_state or state
                        self._probe_frame = candidate
                        self._write_characteristic = write_target
                        self.capture.write(
                            {
                                "timestamp": TrafficRecord(
                                    self.device.address, "diagnostic", ""
                                ).timestamp,
                                "device_id": self.device.address,
                                "direction": "resolution",
                                **resolution.to_dict(),
                            }
                        )
                        return resolution
        if best:
            self.resolution, self.state, self._probe_frame = best
            self.resolution.notes.append("A matching response was seen once but not confirmed.")
            return self.resolution
        return None

    async def query_state(self) -> LightState | None:
        """Refresh state with the resolved query path, probing when necessary."""
        if not self.connected:
            await self.connect()
        if self._probe_frame is None:
            await self.probe(allow_writes=True)
            return self.state
        await self._start_notifications()
        self._flush_notifications()
        await self._send_inner(
            self._probe_frame.payload,
            self._probe_frame.transport,
            note="state refresh",
        )
        result = await self._await_classification(1.5)
        if result:
            resolution, state = result
            resolution.repeatable = bool(self.resolution and self.resolution.repeatable)
            self.resolution = resolution
            self.state = state
        return self.state

    async def query_raw_state(self) -> bytes | None:
        """Query and return the raw state packet bytes, or None if no response."""
        state = await self.query_state()
        if state is not None and state.raw_hex:
            return bytes.fromhex(state.raw_hex)
        return None

    def _require_control_resolution(self) -> tuple[ProtocolResolution, ProbeFrame]:
        if self.resolution is None or self._probe_frame is None or not self.resolution.repeatable:
            raise RuntimeError("control requires a repeatable state response from this connection")
        if self.resolution.codec == "unresolved-ea81":
            raise RuntimeError(
                "control remains locked until the IOTBT Telink or segment "
                "command family is resolved"
            )
        return self.resolution, self._probe_frame

    async def set_power(self, turn_on: bool) -> None:
        resolution, probe = self._require_control_resolution()
        if resolution.codec.startswith("classic"):
            await self._send_inner(classic_power(turn_on), probe.transport, note="power")
        elif resolution.codec == "original-ledenet":
            await self._send_inner(original_power(turn_on), probe.transport, note="power")
        elif resolution.codec == "iotbt-ea81":
            await self._send_inner(iotbt_power(turn_on), probe.transport, note="power")
        elif turn_on:
            brightness = (
                byte_to_percent(self.state.brightness)
                if self.state and self.state.brightness
                else 100
            )
            await self._send_inner(extended_e0_white(brightness), probe.transport, note="power on")
        else:
            for zone in (0x24, 0x23):
                await self._send_inner(
                    extended_zone_power_off(zone), probe.transport, note=f"power off zone {zone:#x}"
                )
                await asyncio.sleep(0.05)
        if self.state is None:
            self.state = LightState()
        self.state.power = turn_on
        self.state.assumed = True

    async def set_rgb(
        self,
        red: int,
        green: int,
        blue: int,
        *,
        brightness: int = 100,
        experimental_e122: bool = False,
    ) -> None:
        resolution, probe = self._require_control_resolution()
        brightness = max(1, min(100, brightness))
        if resolution.codec.startswith("classic"):
            scale = brightness / 100
            payload = classic_rgb(
                round(red * scale),
                round(green * scale),
                round(blue * scale),
                nine_byte=resolution.codec == "classic-9byte",
            )
            await self._send_inner(payload, probe.transport, note="set rgb")
        elif resolution.codec == "iotbt-ea81":
            payload = iotbt_3b_rgb(red, green, blue, brightness)
            await self._send_inner(payload, probe.transport, note="set rgb")
        elif experimental_e122:
            if "candidate-e122" not in resolution.capabilities:
                raise RuntimeError("E1 22 is only enabled for a matching extended candidate")
            scale = brightness / 100
            payload = experimental_e122_fill(
                round(red * scale), round(green * scale), round(blue * scale)
            )
            await self._send_inner(payload, probe.transport, note="set rgb")
        else:
            payload = extended_e0_rgb(red, green, blue, brightness)
            await self._send_inner(payload, probe.transport, note="set rgb")
        self.state = LightState(
            power=True,
            rgb=(red, green, blue),
            brightness=round(brightness * 255 / 100),
            white=0,
            model_num=resolution.model_num,
            firmware_version=resolution.firmware_version,
            assumed=True,
        )

    async def set_white(
        self,
        brightness: int,
        *,
        experimental_e122: bool = False,
    ) -> None:
        resolution, probe = self._require_control_resolution()
        brightness = max(1, min(100, brightness))
        white_byte = round(brightness * 255 / 100)
        if resolution.codec.startswith("classic"):
            payload = classic_white(
                white_byte,
                nine_byte=resolution.codec == "classic-9byte",
            )
            await self._send_inner(payload, probe.transport, note="set white")
        elif resolution.codec == "iotbt-ea81":
            payload = iotbt_3b_white(brightness)
            await self._send_inner(payload, probe.transport, note="set white")
        elif experimental_e122:
            if "candidate-e122" not in resolution.capabilities:
                raise RuntimeError("E1 22 is only enabled for a matching extended candidate")
            payload = experimental_e122_fill(0, 0, 0, white=white_byte)
            await self._send_inner(payload, probe.transport, note="set white")
        else:
            payload = extended_e0_white(brightness)
            await self._send_inner(payload, probe.transport, note="set white")
        self.state = LightState(
            power=True,
            rgb=(0, 0, 0),
            brightness=white_byte,
            white=white_byte,
            model_num=resolution.model_num,
            firmware_version=resolution.firmware_version,
            assumed=True,
        )

    async def set_brightness(self, brightness: int) -> None:
        brightness = max(1, min(100, brightness))
        if self.resolution and self.resolution.codec == "iotbt-ea81":
            # The 3B 01 standalone brightness command is not accepted by this
            # device. Re-send the current color or white command with the new
            # brightness, which is what the official app does.
            if self.state and self.state.white and not any(
                self.state.rgb or (0, 0, 0)
            ):
                await self.set_white(brightness)
            else:
                rgb = self.state.rgb if self.state and self.state.rgb else (255, 255, 255)
                await self.set_rgb(*rgb, brightness=brightness)
            return
        if self.state and self.state.white and not any(self.state.rgb or (0, 0, 0)):
            await self.set_white(brightness)
            return
        rgb = self.state.rgb if self.state and self.state.rgb else (255, 255, 255)
        await self.set_rgb(*rgb, brightness=brightness)

    async def raw_write(
        self,
        characteristic_uuid: str,
        payload: bytes,
        *,
        allow_raw_write: bool,
    ) -> None:
        if not allow_raw_write:
            raise PermissionError("raw writes require allow_raw_write=True")
        if not 1 <= len(payload) <= 512:
            raise ValueError("raw payload must contain 1 to 512 bytes")
        profile = await self.inspect_gatt()
        normalized_uuid = characteristic_uuid.casefold()
        target = next(
            (item for item in profile.writable if item.uuid.casefold() == normalized_uuid),
            None,
        )
        if target is None:
            raise ValueError("the selected characteristic is not writable")
        await self._write(payload, characteristic=target, framing="raw", note="explicit raw write")

    async def replay(
        self,
        records: list[TrafficRecord],
        *,
        allow_raw_write: bool,
        delay: float = 0.05,
    ) -> None:
        """Replay reviewed TX records through their exact characteristics."""
        if not allow_raw_write:
            raise PermissionError("replay requires allow_raw_write=True")
        if delay < 0:
            raise ValueError("replay delay cannot be negative")
        profile = await self.inspect_gatt()
        writable_uuids = {char.uuid.casefold() for char in profile.writable}
        for index, record in enumerate(records, 1):
            if not record.characteristic:
                raise ValueError(
                    f"record {index}: every replay TX record must name a characteristic"
                )
            if record.characteristic.casefold() not in writable_uuids:
                raise ValueError(
                    f"record {index}: characteristic {record.characteristic} "
                    "is not writable on this device"
                )
            bytes.fromhex(record.payload_hex)
        for record in records:
            await self.raw_write(
                record.characteristic,
                bytes.fromhex(record.payload_hex),
                allow_raw_write=True,
            )
            if delay:
                await asyncio.sleep(delay)
