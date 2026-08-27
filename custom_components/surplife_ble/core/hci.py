"""Pure-Python btsnoop parsing and ATT PDU extraction for HCI snoop captures.

Android bug reports contain ``btsnoop_hci.log`` files. This module reads those
files, isolates ATT writes and notifications for one BLE connection, and converts
them into ``TrafficRecord`` objects that match the JSONL capture format used by
the rest of the toolkit. No external dependencies are required.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from .models import GattCharacteristic, GattProfile, GattService, TrafficRecord

BTSNOOP_MAGIC = b"btsnoop\x00"
# btsnoop timestamps are microseconds since 2000-01-01 UTC.
BTSNOOP_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

# HCI H4 packet type prefixes.
HCI_COMMAND = 0x01
HCI_ACL = 0x02
HCI_SCO = 0x03
HCI_EVENT = 0x04

# L2CAP CID for the Attribute Protocol.
L2CAP_CID_ATT = 0x0004

# ATT opcodes relevant to the investigation.
ATT_WRITE_REQUEST = 0x12
ATT_WRITE_RESPONSE = 0x13
ATT_WRITE_COMMAND = 0x1B
ATT_HANDLE_VALUE_NOTIFICATION = 0x1D
ATT_HANDLE_VALUE_INDICATION = 0x1E

# ATT opcodes that carry an attribute handle in bytes 1-2.
ATT_HANDLE_VALUE_OPCODES = frozenset(
    {
        ATT_WRITE_REQUEST,
        ATT_WRITE_COMMAND,
        ATT_HANDLE_VALUE_NOTIFICATION,
        ATT_HANDLE_VALUE_INDICATION,
    }
)

CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"


@dataclass(frozen=True, slots=True)
class BtsnoopRecord:
    """One raw btsnoop packet record."""

    timestamp_us: int
    direction: int  # 0 = received by host, 1 = sent by host
    data: bytes


@dataclass(frozen=True, slots=True)
class AttRecord:
    """One ATT PDU extracted from an HCI ACL packet."""

    timestamp: str
    direction: str  # "tx" or "rx"
    opcode: int
    attribute_handle: int
    value: bytes
    connection_handle: int


@dataclass(frozen=True, slots=True)
class ConnectionSummary:
    """Aggregate ATT traffic for one BLE connection handle."""

    connection_handle: int
    write_count: int
    notification_count: int
    attribute_handles: frozenset[int]


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    data = stream.read(count)
    if len(data) != count:
        raise ValueError(
            f"unexpected end of btsnoop stream: wanted {count} bytes, got {len(data)}"
        )
    return data


def parse_btsnoop(path: str | Path) -> Iterator[BtsnoopRecord]:
    """Yield raw packet records from a btsnoop file.

    Only the HCI H4 datalink types (1001 and 1002) produced by Android are
    supported. The packet-type prefix byte is preserved inside ``data``.
    """
    with Path(path).expanduser().resolve().open("rb") as stream:
        magic = _read_exact(stream, 8)
        if magic != BTSNOOP_MAGIC:
            raise ValueError(f"not a btsnoop file: bad magic {magic!r}")
        version = int.from_bytes(_read_exact(stream, 4), "big")
        datalink = int.from_bytes(_read_exact(stream, 4), "big")
        if datalink not in (1001, 1002):
            raise ValueError(
                f"unsupported btsnoop datalink type {datalink}; "
                "only HCI H4 (1001) and HCI UART (1002) are handled"
            )
        if version != 1:
            raise ValueError(f"unsupported btsnoop version {version}")

        while True:
            header = stream.read(24)
            if not header:
                break
            if len(header) != 24:
                raise ValueError("truncated btsnoop record header")
            included_length = int.from_bytes(header[4:8], "big")
            flags = int.from_bytes(header[8:12], "big")
            timestamp_us = int.from_bytes(header[16:24], "big")
            data = _read_exact(stream, included_length)
            # For HCI H4, bit 0 of flags: 0 = received by host, 1 = sent by host.
            direction = flags & 0x01
            yield BtsnoopRecord(timestamp_us=timestamp_us, direction=direction, data=data)


def _timestamp_from_us(microseconds: int) -> str:
    return (BTSNOOP_EPOCH + timedelta(microseconds=microseconds)).isoformat()


def _extract_acl_att(
    packet: bytes,
    timestamp: str,
    direction: int,
) -> AttRecord | None:
    """Parse one HCI ACL packet and return its ATT PDU if present."""
    if len(packet) < 5 or packet[0] != HCI_ACL:
        return None
    handle_flags = int.from_bytes(packet[1:3], "little")
    connection_handle = handle_flags & 0x0FFF
    _data_length = int.from_bytes(packet[3:5], "little")
    l2cap = packet[5:]
    if len(l2cap) < 4:
        return None
    l2cap_length = int.from_bytes(l2cap[0:2], "little")
    cid = int.from_bytes(l2cap[2:4], "little")
    if cid != L2CAP_CID_ATT:
        return None
    att = l2cap[4 : 4 + l2cap_length]
    if len(att) < 1:
        return None
    opcode = att[0]
    if opcode not in ATT_HANDLE_VALUE_OPCODES:
        return None
    if len(att) < 3:
        return None
    attribute_handle = int.from_bytes(att[1:3], "little")
    value = bytes(att[3:])
    # direction=1 (sent by host) = TX from the app to the lamp.
    # direction=0 (received by host) = RX from the lamp to the app.
    return AttRecord(
        timestamp=timestamp,
        direction="tx" if direction == 1 else "rx",
        opcode=opcode,
        attribute_handle=attribute_handle,
        value=value,
        connection_handle=connection_handle,
    )


def extract_att_records(records: Iterable[BtsnoopRecord]) -> Iterator[AttRecord]:
    """Filter btsnoop records down to ATT writes and notifications."""
    for record in records:
        timestamp = _timestamp_from_us(record.timestamp_us)
        att = _extract_acl_att(record.data, timestamp, record.direction)
        if att is not None:
            yield att


def summarize_connections(att_records: Iterable[AttRecord]) -> list[ConnectionSummary]:
    """Group ATT records by connection handle for ``--list-connections``."""
    by_handle: dict[int, list[AttRecord]] = {}
    for record in att_records:
        by_handle.setdefault(record.connection_handle, []).append(record)
    summaries: list[ConnectionSummary] = []
    for handle, records in sorted(by_handle.items()):
        writes = sum(1 for r in records if r.direction == "tx")
        notifications = sum(1 for r in records if r.direction == "rx")
        handles = frozenset(r.attribute_handle for r in records)
        summaries.append(
            ConnectionSummary(
                connection_handle=handle,
                write_count=writes,
                notification_count=notifications,
                attribute_handles=handles,
            )
        )
    return summaries


def build_handle_to_uuid(profile: GattProfile) -> dict[int, GattCharacteristic]:
    """Map ATT value handles to their characteristic objects."""
    mapping: dict[int, GattCharacteristic] = {}
    for service in profile.services:
        for characteristic in service.characteristics:
            mapping[characteristic.handle] = characteristic
    return mapping


def _opcode_note(opcode: int) -> str:
    if opcode == ATT_WRITE_REQUEST:
        return "ATT Write Request"
    if opcode == ATT_WRITE_COMMAND:
        return "ATT Write Command"
    if opcode == ATT_HANDLE_VALUE_NOTIFICATION:
        return "ATT Notification"
    if opcode == ATT_HANDLE_VALUE_INDICATION:
        return "ATT Indication"
    return f"ATT opcode 0x{opcode:02x}"


def _characteristic_label(
    attribute_handle: int,
    handle_map: dict[int, GattCharacteristic],
) -> tuple[str, str | None]:
    """Return (characteristic_uuid, note_suffix) for an ATT handle."""
    characteristic = handle_map.get(attribute_handle)
    if characteristic is not None:
        return characteristic.uuid, None
    # Check if this is a CCCD (value handle + 1).
    for handle, characteristic in handle_map.items():
        if attribute_handle == handle + 1:
            return characteristic.uuid, "cccd enable"
    return f"handle:0x{attribute_handle:04x}", "unknown handle"


def att_to_traffic_records(
    att_records: Iterable[AttRecord],
    *,
    device_id: str,
    handle_map: dict[int, GattCharacteristic] | None = None,
    connection_handle: int | None = None,
) -> list[TrafficRecord]:
    """Convert ATT records into JSONL-compatible ``TrafficRecord`` objects."""
    resolved_map = handle_map or {}
    records: list[TrafficRecord] = []
    for att in att_records:
        if connection_handle is not None and att.connection_handle != connection_handle:
            continue
        characteristic_uuid, suffix = _characteristic_label(att.attribute_handle, resolved_map)
        note = _opcode_note(att.opcode)
        if suffix:
            note = f"{note} ({suffix})"
        records.append(
            TrafficRecord(
                device_id=device_id,
                direction=att.direction,
                payload_hex=att.value.hex(),
                characteristic=characteristic_uuid,
                note=note,
                timestamp=att.timestamp,
            )
        )
    return records


def load_gatt_profile(path: str | Path) -> GattProfile:
    """Load a GATT profile JSON produced by ``ble-rgb inspect --json``."""
    import json

    resolved = Path(path).expanduser().resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"invalid JSON in GATT profile {resolved}: {err}") from err

    try:
        services: list[GattService] = []
        for service_data in data.get("services", []):
            characteristics = [
                GattCharacteristic(
                    uuid=char["uuid"],
                    handle=int(char["handle"]),
                    properties=list(char["properties"]),
                    descriptors=list(char.get("descriptors", [])),
                )
                for char in service_data.get("characteristics", [])
            ]
            services.append(
                GattService(
                    uuid=service_data["uuid"],
                    handle=int(service_data["handle"]),
                    characteristics=characteristics,
                )
            )
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError(f"malformed GATT profile structure in {resolved}: {err}") from err
    return GattProfile(device_id=data.get("device_id", ""), services=services)
