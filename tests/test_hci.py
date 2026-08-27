"""Tests for btsnoop parsing, ATT extraction, handle mapping, and replay hardening."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from custom_components.surplife_ble.core.hci import (
    BTSNOOP_EPOCH,
    AttRecord,
    att_to_traffic_records,
    build_handle_to_uuid,
    extract_att_records,
    load_gatt_profile,
    parse_btsnoop,
    summarize_connections,
)
from custom_components.surplife_ble.core.models import (
    GattCharacteristic,
    GattProfile,
    GattService,
)

# ---------------------------------------------------------------------------
# Synthetic btsnoop file construction helpers
# ---------------------------------------------------------------------------

BTSNOOP_MAGIC = b"btsnoop\x00"
HCI_ACL = 0x02
L2CAP_CID_ATT = 0x0004
ATT_WRITE_REQUEST = 0x12
ATT_WRITE_COMMAND = 0x1B
ATT_HANDLE_VALUE_NOTIFICATION = 0x1D


def _btsnoop_header(datalink: int = 1002) -> bytes:
    return BTSNOOP_MAGIC + struct.pack(">II", 1, datalink)


def _btsnoop_record(data: bytes, *, direction: int, timestamp_us: int) -> bytes:
    flags = direction & 0x01
    header = struct.pack(">IIII", len(data), len(data), flags, 0)
    return header + struct.pack(">Q", timestamp_us) + data


def _acl_packet(connection_handle: int, att_pdu: bytes) -> bytes:
    l2cap_payload = att_pdu
    l2cap = struct.pack("<HH", len(l2cap_payload), L2CAP_CID_ATT) + l2cap_payload
    handle_flags = connection_handle & 0x0FFF
    return bytes((HCI_ACL,)) + struct.pack("<HH", handle_flags, len(l2cap)) + l2cap


def _att_write_request(handle: int, value: bytes) -> bytes:
    return bytes((ATT_WRITE_REQUEST,)) + struct.pack("<H", handle) + value


def _att_write_command(handle: int, value: bytes) -> bytes:
    return bytes((ATT_WRITE_COMMAND,)) + struct.pack("<H", handle) + value


def _att_notification(handle: int, value: bytes) -> bytes:
    return bytes((ATT_HANDLE_VALUE_NOTIFICATION,)) + struct.pack("<H", handle) + value


def _write_snoop(path: Path, records: list[tuple[bytes, int, int]]) -> None:
    """Write a synthetic btsnoop file. Each record is (data, direction, timestamp_us)."""
    with path.open("wb") as stream:
        stream.write(_btsnoop_header())
        for data, direction, ts in records:
            stream.write(_btsnoop_record(data, direction=direction, timestamp_us=ts))


# A minimal GATT profile matching the DX23012 capture.
def _dx23012_profile() -> GattProfile:
    return GattProfile(
        device_id="DEVICE-UUID",
        services=[
            GattService(
                uuid="0000fe00-0000-1000-8000-00805f9b34fb",
                handle=12,
                characteristics=[
                    GattCharacteristic(
                        uuid="0000ff22-0000-1000-8000-00805f9b34fb",
                        handle=13,
                        properties=["notify", "read", "write-without-response"],
                    ),
                    GattCharacteristic(
                        uuid="0000ff11-0000-1000-8000-00805f9b34fb",
                        handle=16,
                        properties=["write-without-response"],
                    ),
                ],
            ),
            GattService(
                uuid="0000ffff-0000-1000-8000-00805f9b34fb",
                handle=18,
                characteristics=[
                    GattCharacteristic(
                        uuid="0000ff02-0000-1000-8000-00805f9b34fb",
                        handle=19,
                        properties=["notify", "read"],
                    ),
                    GattCharacteristic(
                        uuid="0000ff01-0000-1000-8000-00805f9b34fb",
                        handle=22,
                        properties=["write", "write-without-response"],
                    ),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# btsnoop parsing
# ---------------------------------------------------------------------------


def test_parse_btsnoop_rejects_bad_magic(tmp_path: Path) -> None:
    path = tmp_path / "bad.log"
    path.write_bytes(b"notsnoop" + struct.pack(">II", 1, 1002))
    with pytest.raises(ValueError, match="not a btsnoop file"):
        list(parse_btsnoop(path))


def test_parse_btsnoop_rejects_unsupported_datalink(tmp_path: Path) -> None:
    path = tmp_path / "bad.log"
    path.write_bytes(_btsnoop_header(datalink=9999))
    with pytest.raises(ValueError, match="unsupported btsnoop datalink type"):
        list(parse_btsnoop(path))


def test_parse_btsnoop_yields_records_with_direction(tmp_path: Path) -> None:
    path = tmp_path / "test.log"
    _write_snoop(path, [
        (_acl_packet(0x0040, _att_write_request(0x0016, b"\xea\x81\x8a\x8b")), 1, 1000),
        (_acl_packet(0x0040, _att_notification(0x0013, b"\xea\x81\x01")), 0, 2000),
    ])
    records = list(parse_btsnoop(path))
    assert len(records) == 2
    assert records[0].direction == 1  # sent by host
    assert records[1].direction == 0  # received by host
    assert records[0].data[0] == HCI_ACL


# ---------------------------------------------------------------------------
# ATT extraction
# ---------------------------------------------------------------------------


def test_extract_att_records_filters_to_writes_and_notifications(tmp_path: Path) -> None:
    path = tmp_path / "test.log"
    _write_snoop(path, [
        (_acl_packet(0x0040, _att_write_request(0x0016, b"\xea\x81\x8a\x8b")), 1, 1000),
        (_acl_packet(0x0040, _att_write_command(0x0016, b"\x71\x23\x0f")), 1, 2000),
        (_acl_packet(0x0040, _att_notification(0x0013, b"\xea\x81\x01\x00")), 0, 3000),
    ])
    att_records = list(extract_att_records(parse_btsnoop(path)))
    assert len(att_records) == 3
    assert att_records[0].direction == "tx"
    assert att_records[0].opcode == ATT_WRITE_REQUEST
    assert att_records[0].attribute_handle == 0x0016
    assert att_records[0].value == b"\xea\x81\x8a\x8b"
    assert att_records[1].opcode == ATT_WRITE_COMMAND
    assert att_records[2].direction == "rx"
    assert att_records[2].opcode == ATT_HANDLE_VALUE_NOTIFICATION
    assert att_records[2].attribute_handle == 0x0013


def test_extract_att_records_ignores_non_att_cid(tmp_path: Path) -> None:
    path = tmp_path / "test.log"
    # L2CAP packet on CID 0x0001 (signaling), not ATT.
    l2cap = struct.pack("<HH", 2, 0x0001) + b"\x00\x00"
    acl = bytes((HCI_ACL,)) + struct.pack("<HH", 0x0040, len(l2cap)) + l2cap
    _write_snoop(path, [(acl, 1, 1000)])
    att_records = list(extract_att_records(parse_btsnoop(path)))
    assert att_records == []


def test_extract_att_timestamps_are_iso(tmp_path: Path) -> None:
    path = tmp_path / "test.log"
    _write_snoop(path, [
        (_acl_packet(0x0040, _att_write_request(0x0016, b"\x01")), 1, 0),
    ])
    att_records = list(extract_att_records(parse_btsnoop(path)))
    assert att_records[0].timestamp == BTSNOOP_EPOCH.isoformat()


# ---------------------------------------------------------------------------
# Connection summarization
# ---------------------------------------------------------------------------


def test_summarize_connections_groups_by_handle(tmp_path: Path) -> None:
    path = tmp_path / "test.log"
    _write_snoop(path, [
        (_acl_packet(0x0040, _att_write_request(0x0016, b"\x01")), 1, 1000),
        (_acl_packet(0x0040, _att_notification(0x0013, b"\x02")), 0, 2000),
        (_acl_packet(0x0041, _att_write_request(0x0016, b"\x03")), 1, 3000),
    ])
    att_records = list(extract_att_records(parse_btsnoop(path)))
    summaries = summarize_connections(att_records)
    assert len(summaries) == 2
    assert summaries[0].connection_handle == 0x0040
    assert summaries[0].write_count == 1
    assert summaries[0].notification_count == 1
    assert summaries[1].connection_handle == 0x0041
    assert summaries[1].write_count == 1
    assert summaries[1].notification_count == 0


# ---------------------------------------------------------------------------
# Handle → UUID mapping and TrafficRecord conversion
# ---------------------------------------------------------------------------


def test_att_to_traffic_records_maps_handles_to_uuids() -> None:
    profile = _dx23012_profile()
    handle_map = build_handle_to_uuid(profile)
    att_records = [
        AttRecord("2026-01-01T00:00:00+00:00", "tx", ATT_WRITE_REQUEST, 22,
                  b"\xea\x81\x8a\x8b", 0x0040),
        AttRecord("2026-01-01T00:00:01+00:00", "rx", ATT_HANDLE_VALUE_NOTIFICATION, 19,
                  b"\xea\x81\x01", 0x0040),
    ]
    traffic = att_to_traffic_records(att_records, device_id="DEV", handle_map=handle_map)
    assert len(traffic) == 2
    assert traffic[0].direction == "tx"
    assert traffic[0].characteristic == "0000ff01-0000-1000-8000-00805f9b34fb"
    assert traffic[0].payload_hex == "ea818a8b"
    assert traffic[1].direction == "rx"
    assert traffic[1].characteristic == "0000ff02-0000-1000-8000-00805f9b34fb"


def test_att_to_traffic_records_labels_unknown_handles() -> None:
    att_records = [
        AttRecord("2026-01-01T00:00:00+00:00", "tx", ATT_WRITE_REQUEST, 0x0099,
                  b"\x01", 0x0040),
    ]
    traffic = att_to_traffic_records(att_records, device_id="DEV", handle_map=None)
    assert traffic[0].characteristic == "handle:0x0099"
    assert "unknown handle" in (traffic[0].note or "")


def test_att_to_traffic_records_filters_by_connection_handle() -> None:
    att_records = [
        AttRecord("2026-01-01T00:00:00+00:00", "tx", ATT_WRITE_REQUEST, 22,
                  b"\x01", 0x0040),
        AttRecord("2026-01-01T00:00:01+00:00", "tx", ATT_WRITE_REQUEST, 22,
                  b"\x02", 0x0041),
    ]
    traffic = att_to_traffic_records(
        att_records, device_id="DEV", handle_map=None, connection_handle=0x0040
    )
    assert len(traffic) == 1
    assert traffic[0].payload_hex == "01"


def test_att_to_traffic_records_labels_cccd_writes() -> None:
    profile = _dx23012_profile()
    handle_map = build_handle_to_uuid(profile)
    # CCCD for FF02 is handle 19 + 1 = 20 (0x0014).
    att_records = [
        AttRecord("2026-01-01T00:00:00+00:00", "tx", ATT_WRITE_REQUEST, 20,
                  b"\x01\x00", 0x0040),
    ]
    traffic = att_to_traffic_records(att_records, device_id="DEV", handle_map=handle_map)
    assert traffic[0].characteristic == "0000ff02-0000-1000-8000-00805f9b34fb"
    assert "cccd enable" in (traffic[0].note or "")


# ---------------------------------------------------------------------------
# GATT profile JSON loading
# ---------------------------------------------------------------------------


def test_load_gatt_profile_from_inspect_json(tmp_path: Path) -> None:
    gatt_json = {
        "device_id": "DEVICE-UUID",
        "services": [
            {
                "uuid": "0000ffff-0000-1000-8000-00805f9b34fb",
                "handle": 18,
                "characteristics": [
                    {
                        "uuid": "0000ff01-0000-1000-8000-00805f9b34fb",
                        "handle": 22,
                        "properties": ["write", "write-without-response"],
                        "descriptors": [],
                    }
                ],
            }
        ],
    }
    path = tmp_path / "gatt.json"
    path.write_text(json.dumps(gatt_json), encoding="utf-8")
    profile = load_gatt_profile(path)
    assert profile.device_id == "DEVICE-UUID"
    assert profile.writable[0].uuid == "0000ff01-0000-1000-8000-00805f9b34fb"
    assert profile.writable[0].handle == 22


# ---------------------------------------------------------------------------
# End-to-end: synthetic snoop → JSONL TrafficRecords
# ---------------------------------------------------------------------------


def test_end_to_end_snoop_to_traffic_records(tmp_path: Path) -> None:
    snoop_path = tmp_path / "btsnoop_hci.log"
    _write_snoop(snoop_path, [
        # App writes the classic query to FF01 (handle 0x16=22).
        (_acl_packet(0x0040, _att_write_request(22, b"\x81\x8a\x8b\x96")), 1, 1000),
        # Lamp responds with EA 81 state on FF02 (handle 0x13=19).
        (_acl_packet(0x0040, _att_notification(19, b"\xea\x81\x01\x00\xaa\x09\x23")), 0, 2000),
    ])
    profile = _dx23012_profile()
    handle_map = build_handle_to_uuid(profile)
    att_records = list(extract_att_records(parse_btsnoop(snoop_path)))
    traffic = att_to_traffic_records(att_records, device_id="DEVICE-UUID", handle_map=handle_map)

    assert len(traffic) == 2
    assert traffic[0].direction == "tx"
    assert traffic[0].characteristic == "0000ff01-0000-1000-8000-00805f9b34fb"
    assert traffic[0].payload_hex == "818a8b96"
    assert traffic[1].direction == "rx"
    assert traffic[1].characteristic == "0000ff02-0000-1000-8000-00805f9b34fb"
    assert traffic[1].payload_hex == "ea810100aa0923"
