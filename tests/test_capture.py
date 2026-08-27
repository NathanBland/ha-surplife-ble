from __future__ import annotations

import json

import pytest

from custom_components.surplife_ble.core.capture import JsonlCapture, load_replay
from custom_components.surplife_ble.core.models import TrafficRecord


def test_jsonl_capture_and_guarded_replay(tmp_path) -> None:
    path = tmp_path / "traffic.jsonl"
    capture = JsonlCapture(path)
    capture.write(
        TrafficRecord(
            device_id="device-a",
            direction="tx",
            payload_hex="0102",
            characteristic="ff01",
        )
    )
    capture.write(TrafficRecord(device_id="device-a", direction="rx", payload_hex="0304"))

    records = load_replay(path, "device-a")
    assert len(records) == 1
    assert records[0].payload_hex == "0102"

    with pytest.raises(ValueError, match="does not match"):
        load_replay(path, "device-b")


def test_invalid_replay_hex_is_rejected(tmp_path) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text(
        json.dumps({"device_id": "d", "direction": "tx", "payload_hex": "zz"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_replay(path, "d")
