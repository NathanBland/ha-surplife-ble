"""Serializable models shared by the CLI and Home Assistant integration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now_iso() -> str:
    """Return a stable UTC timestamp for capture files."""
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class DeviceAdvertisement:
    """A platform-neutral BLE advertisement snapshot."""

    identifier: str
    name: str | None
    rssi: int
    local_name: str | None = None
    service_uuids: list[str] = field(default_factory=list)
    manufacturer_data: dict[int, str] = field(default_factory=dict)
    service_data: dict[str, str] = field(default_factory=dict)
    tx_power: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GattCharacteristic:
    """A GATT characteristic and its relevant properties."""

    uuid: str
    handle: int
    properties: list[str]
    descriptors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GattService:
    """A GATT service snapshot."""

    uuid: str
    handle: int
    characteristics: list[GattCharacteristic] = field(default_factory=list)


@dataclass(slots=True)
class GattProfile:
    """All GATT services visible for one device."""

    device_id: str
    services: list[GattService] = field(default_factory=list)

    @property
    def writable(self) -> list[GattCharacteristic]:
        return [
            characteristic
            for service in self.services
            for characteristic in service.characteristics
            if {"write", "write-without-response"}.intersection(characteristic.properties)
        ]

    @property
    def notifiable(self) -> list[GattCharacteristic]:
        return [
            characteristic
            for service in self.services
            for characteristic in service.characteristics
            if {"notify", "indicate"}.intersection(characteristic.properties)
        ]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TrafficRecord:
    """One sent, received, or diagnostic capture event."""

    device_id: str
    direction: str
    payload_hex: str
    characteristic: str | None = None
    framing: str | None = None
    note: str | None = None
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProtocolResolution:
    """Result of classifying a repeatable state response."""

    frame_family: str
    codec: str
    confidence: float
    repeatable: bool = False
    model_num: int | None = None
    firmware_version: int | None = None
    capabilities: set[str] = field(default_factory=set)
    source_packet_hex: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["capabilities"] = sorted(self.capabilities)
        return result


@dataclass(slots=True)
class LightState:
    """Normalized light state, with unknown fields represented by None."""

    power: bool | None = None
    rgb: tuple[int, int, int] | None = None
    brightness: int | None = None
    white: int | None = None
    model_num: int | None = None
    firmware_version: int | None = None
    led_count: int | None = None
    assumed: bool = False
    raw_hex: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
