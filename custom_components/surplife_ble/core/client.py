"""Async BLE client with guarded probe, control, raw-write, and replay paths."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Any, Protocol

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .capture import JsonlCapture
from .framing import NotificationReassembler, SequenceCounter, segment_magichome
from .models import (
    GattCharacteristic,
    GattProfile,
    GattService,
    LightState,
    ProtocolResolution,
    StripProfile,
    TrafficRecord,
    rgb_state_matches,
)
from .protocol import (
    IOTBT_COMMAND_FAMILY,
    IOTBT_SEGMENT_COUNT,
    SYMPHONY_CONFIG_QUERY,
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
    iotbt_3b_white,
    iotbt_e1_segment_rgb,
    iotbt_e1_segments,
    iotbt_power,
    iotbt_zone_power,
    original_power,
    parse_state,
    parse_symphony_config,
    probe_frames,
)

_LOGGER = logging.getLogger(__name__)

ClientFactory = Callable[[BLEDevice], Any | Awaitable[Any]]

# ATT header bytes subtracted from the negotiated MTU to get a write's payload.
_ATT_HEADER_LENGTH = 3
# The default ATT MTU. A client reporting this (or nothing) has not told us the
# negotiated value, so long commands are sent as one write, as before.
_DEFAULT_ATT_MTU = 23
# Power command forms for IOTBT lamps, tried in order until one is confirmed.
POWER_FORM_APP = "switch_led_v3"
POWER_FORM_ZONE = "zone"


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
        # Characteristics whose subscription failed on this connection. They are
        # not retried until the next connection, so a broken one does not add a
        # slow GATT round trip before every write.
        self._notify_failed: set[str] = set()
        self._sequence = SequenceCounter()
        self._write_characteristic: GattCharacteristic | None = None
        self._probe_frame: ProbeFrame | None = None
        self.resolution: ProtocolResolution | None = None
        self.state: LightState | None = None
        self.strip_profile: StripProfile | None = None
        # The IOTBT power form confirmed to work on this lamp, once known.
        self._power_form: str | None = None
        # Per-segment colours last sent, kept across state refreshes.
        self._segments: tuple[tuple[int, int, int], ...] | None = None

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    @property
    def resolved(self) -> bool:
        """True once a state query path has been found (it survives reconnects)."""
        return self._probe_frame is not None

    async def clear_cache(self) -> None:
        """Drop the cached GATT services, when the client supports a cache."""
        clear = getattr(self._client, "clear_cache", None)
        if clear is None:
            return
        with suppress(Exception):
            result = clear()
            if inspect.isawaitable(result):
                await result

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
        self.mark_disconnected()
        created = self._client_factory(self.device)
        self._client = await created if inspect.isawaitable(created) else created
        if not self._client.is_connected:
            await self._client.connect()
        self._profile = self._build_profile()
        self._write_characteristic = self._choose_write_characteristic(self._profile)

    def mark_disconnected(self, client: Any | None = None) -> None:
        """Clear state that belongs to the previous BLE connection."""
        if client is not None and client is not self._client:
            return
        self._notifying.clear()
        self._notify_failed.clear()
        self._flush_notifications()

    async def disconnect(self) -> None:
        if self._client is None:
            return
        for uuid in tuple(self._notifying):
            with suppress(Exception):
                await self._client.stop_notify(uuid)
        self.mark_disconnected()
        if self._client.is_connected:
            await self._client.disconnect()

    async def __aenter__(self) -> LightClient:
        await self.connect()
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.disconnect()

    def _require_client(self) -> BleakClientLike:
        if self._client is None or not self._client.is_connected:
            raise ConnectionError("BLE client is not connected")
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
        # The lamps answer on FF02. Subscribing to every notifiable characteristic
        # (Service Changed, vendor or OTA characteristics) adds GATT traffic and
        # can upset the firmware, so only fall back to all of them when FF02 is absent.
        notifiable = self._profile.notifiable
        preferred = [item for item in notifiable if _uuid_is(item.uuid, "ff02")]
        ordered = preferred or sorted(notifiable, key=lambda item: item.uuid)
        for characteristic in ordered:
            if characteristic.uuid in self._notifying or characteristic.uuid in self._notify_failed:
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
                self._notify_failed.add(characteristic.uuid)
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
        prefer_response: bool = False,
    ) -> None:
        client = self._require_client()
        target = characteristic or self._write_characteristic
        if target is None:
            raise RuntimeError("device exposes no writable GATT characteristic")
        if prefer_response and "write" in target.properties:
            response = True
        else:
            response = (
                "write-without-response" not in target.properties
                and "write" in target.properties
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
        command_family: int = 0x0A,
    ) -> None:
        await self._start_notifications()
        sequence = self._sequence.next()
        max_frame = self._max_frame_length()
        if transport == "magichome2" and max_frame is not None:
            frames = segment_magichome(
                payload,
                sequence,
                command_family=command_family,
                max_frame_length=max_frame,
            )
        else:
            frames = [
                frame_for_transport(
                    payload,
                    transport,
                    sequence,
                    command_family=command_family,
                )
            ]
        segmented = len(frames) > 1
        for index, framed in enumerate(frames):
            await self._write(
                framed,
                characteristic=characteristic,
                framing=transport if not segmented else f"{transport}:segment-{index}",
                note=note,
                # Like the app, wait for each segment to be acknowledged at the
                # GATT level before sending the next one.
                prefer_response=segmented,
            )

    def _max_frame_length(self) -> int | None:
        """Largest single write, or None when the negotiated MTU is unknown."""
        mtu = getattr(self._client, "mtu_size", None)
        if not isinstance(mtu, int) or mtu <= _DEFAULT_ATT_MTU:
            return None
        # The app caps v0 frames at 255 bytes.
        return min(mtu - _ATT_HEADER_LENGTH, 255)

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
        self,
        wait_seconds: float,
        *,
        allow_reads: bool = True,
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
        if allow_reads:
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
        exhaustive: bool = False,
    ) -> ProtocolResolution | None:
        """Send known state queries and require a repeatable response for control.

        By default the queries go only to the FF01 write characteristic when the
        device has one, and readable characteristics are not polled after each
        unanswered query. Writing guessed frames to other characteristics can make
        these lamps drop the connection. ``exhaustive=True`` tries every writable
        characteristic with the read fallback, for investigating unknown devices.
        """
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
        preferred = [item for item in write_targets if _uuid_is(item.uuid, "ff01")]
        narrow = not exhaustive and bool(preferred)
        if narrow:
            write_targets = preferred
        allow_reads = not narrow
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
                first = await self._await_classification(
                    response_timeout, allow_reads=allow_reads
                )
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
                confirmation = await self._await_classification(
                    response_timeout, allow_reads=allow_reads
                )
                if confirmation:
                    confirmed_resolution, confirmed_state = confirmation
                    same_identity = (
                        confirmed_resolution.frame_family == resolution.frame_family
                        and confirmed_resolution.model_num == resolution.model_num
                    )
                    if same_identity:
                        resolution.repeatable = True
                        resolution.transport = candidate.transport
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
            self.resolution.transport = self._probe_frame.transport
            self.resolution.notes.append("A matching response was seen once but not confirmed.")
            return self.resolution
        return None

    async def query_state(
        self,
        *,
        response_timeout: float = 1.5,
        allow_reads: bool = True,
    ) -> LightState | None:
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
        result = await self._await_classification(
            response_timeout,
            allow_reads=allow_reads,
        )
        if result is None:
            return None
        resolution, state = result
        resolution.repeatable = bool(self.resolution and self.resolution.repeatable)
        resolution.transport = self.resolution.transport if self.resolution else None
        self.resolution = resolution
        self.state = state
        return state

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
            await self._set_iotbt_power(turn_on, probe)
            return
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

    async def _set_iotbt_power(self, turn_on: bool, probe: ProbeFrame) -> None:
        """Send power and confirm it with a state query, retrying once.

        The app's switch_led_v3 form is tried first (twice, to cover a dropped
        write), then the legacy zone form. A form is only cached when the lamp
        was confirmed in the opposite state beforehand, so a lamp that was
        already on cannot "confirm" a form it ignored.
        """
        prior = None
        if self.state is not None and not self.state.assumed:
            prior = self.state.power
        preferred = self._power_form or POWER_FORM_APP
        other = POWER_FORM_ZONE if preferred == POWER_FORM_APP else POWER_FORM_APP
        for form in (preferred, preferred, other):
            if form == POWER_FORM_ZONE:
                await self._send_inner(
                    iotbt_zone_power(turn_on), probe.transport, note="power zone"
                )
            else:
                await self._send_inner(
                    iotbt_power(turn_on),
                    probe.transport,
                    note="power switch_led_v3",
                    command_family=IOTBT_COMMAND_FAMILY,
                )
            await asyncio.sleep(0.15)
            confirmed = await self.query_state(response_timeout=0.75, allow_reads=False)
            if confirmed is not None and confirmed.power is turn_on:
                if prior is not None and prior is not turn_on:
                    self._power_form = form
                return
            _LOGGER.debug("Power %s via %s not confirmed", "on" if turn_on else "off", form)
        self._mark_power(turn_on)

    def _mark_power(self, turn_on: bool) -> None:
        if self.state is None:
            self.state = LightState()
        self.state.power = turn_on
        self.state.assumed = True

    @property
    def segment_count(self) -> int:
        """Segments to address: the queried strip profile, else the lamp default."""
        if self.strip_profile is not None:
            return self.strip_profile.segment_count
        return IOTBT_SEGMENT_COUNT

    @property
    def segments(self) -> tuple[tuple[int, int, int], ...] | None:
        """Per-segment colours last sent, or None after a uniform colour."""
        return self._segments

    async def query_strip_profile(self, *, response_timeout: float = 1.0) -> StripProfile | None:
        """Ask the lamp for its LED strip configuration (get_symp_config_v2)."""
        resolution, probe = self._require_control_resolution()
        if resolution.codec != "iotbt-ea81":
            return None
        loop = asyncio.get_running_loop()
        for family in (0x0A, IOTBT_COMMAND_FAMILY):
            self._flush_notifications()
            await self._send_inner(
                SYMPHONY_CONFIG_QUERY,
                probe.transport,
                note="strip profile query",
                command_family=family,
            )
            deadline = loop.time() + response_timeout
            while (remaining := deadline - loop.time()) > 0:
                try:
                    _characteristic, packet = await asyncio.wait_for(
                        self._notifications.get(), timeout=remaining
                    )
                except TimeoutError:
                    break
                profile = parse_symphony_config(packet)
                if profile is not None:
                    _LOGGER.debug("Strip profile reply (cmd %#04x): %s", family, packet.hex())
                    self.strip_profile = profile
                    return profile
                _LOGGER.debug(
                    "Unparsed reply to strip profile query (cmd %#04x): %s", family, packet.hex()
                )
        _LOGGER.debug("No strip profile reply; using %s segments", IOTBT_SEGMENT_COUNT)
        return None

    async def set_segments(
        self,
        colors: Sequence[tuple[int, int, int]],
        *,
        brightness: int = 100,
    ) -> None:
        """Paint each lamp segment its own colour with one E1 03 command.

        A shorter colour list is stretched evenly across the segments, so two
        colours give a half-and-half split.
        """
        resolution, probe = self._require_control_resolution()
        if resolution.codec != "iotbt-ea81":
            raise RuntimeError("per-segment colour needs an IOTBT symphony lamp")
        if not colors:
            raise ValueError("at least one colour is required")
        brightness = max(1, min(100, brightness))
        count = self.segment_count
        if len(colors) > count:
            raise ValueError(f"this lamp has {count} segments; got {len(colors)} colours")
        stretched = tuple(
            tuple(max(0, min(255, int(c))) for c in colors[i * len(colors) // count])
            for i in range(count)
        )
        self._flush_notifications()
        await self._send_inner(
            iotbt_e1_segments(stretched, brightness),
            probe.transport,
            note="set segments e1 03",
            command_family=IOTBT_COMMAND_FAMILY,
        )
        self._segments = stretched  # type: ignore[assignment]
        self.state = LightState(
            power=True,
            rgb=stretched[0],  # type: ignore[arg-type]
            brightness=round(brightness * 255 / 100),
            white=0,
            model_num=resolution.model_num,
            firmware_version=resolution.firmware_version,
            product_id=resolution.product_id,
            segments=self._segments,
            assumed=True,
        )
        # The lamp needs a state round trip before it accepts the next long
        # segment write. Its first reply can still describe the previous
        # colours, so only a reply matching the first segment replaces the
        # commanded state.
        await self._confirm_rgb_state(stretched[0], round(brightness * 255 / 100))  # type: ignore[arg-type]
        if self.state is not None:
            self.state.segments = self._segments

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
            payload = iotbt_e1_segment_rgb(
                red, green, blue, brightness, segment_count=self.segment_count
            )
            expected_brightness = round(brightness * 255 / 100)
            self._segments = None
            self._flush_notifications()
            await self._send_inner(
                payload,
                probe.transport,
                note="set rgb e1 03",
                command_family=IOTBT_COMMAND_FAMILY,
            )
            self.state = LightState(
                power=True,
                rgb=(red, green, blue),
                brightness=expected_brightness,
                white=0,
                model_num=resolution.model_num,
                firmware_version=resolution.firmware_version,
                assumed=True,
            )
            await self._confirm_rgb_state((red, green, blue), expected_brightness)
            return
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

    async def _confirm_rgb_state(
        self,
        expected_rgb: tuple[int, int, int],
        expected_brightness: int,
    ) -> None:
        """Complete the state round trip required between segment writes."""
        optimistic = self.state
        result = await self._await_classification(0.15, allow_reads=False)
        if result is not None:
            resolution, confirmed = result
            resolution.repeatable = bool(self.resolution and self.resolution.repeatable)
            resolution.transport = self.resolution.transport if self.resolution else None
            self.resolution = resolution
            if confirmed is not None and rgb_state_matches(
                confirmed, expected_rgb, expected_brightness
            ):
                confirmed.assumed = False
                self.state = confirmed
                return
        for attempt in range(2):
            confirmed = await self.query_state(response_timeout=0.75, allow_reads=False)
            if confirmed is None:
                break
            if rgb_state_matches(confirmed, expected_rgb, expected_brightness):
                confirmed.assumed = False
                self.state = confirmed
                return
            if attempt == 0:
                await asyncio.sleep(0.15)
        self.state = optimistic

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
            self._segments = None
            await self._send_inner(
                payload,
                probe.transport,
                note="set white",
                command_family=IOTBT_COMMAND_FAMILY,
            )
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
            if self.state and self.state.white:
                await self.set_white(brightness)
            elif self._segments is not None:
                await self.set_segments(self._segments, brightness=brightness)
            else:
                rgb = self.state.rgb if self.state and self.state.rgb else (255, 255, 255)
                await self.set_rgb(*rgb, brightness=brightness)
            return
        if self.state and self.state.white:
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
