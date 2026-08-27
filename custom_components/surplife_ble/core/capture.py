"""Capture serialization and guarded replay loading."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import TrafficRecord


class JsonlCapture:
    """Append-only JSONL traffic logger."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path).expanduser().resolve() if path else None

    def write(self, record: TrafficRecord | dict[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = record.to_dict() if isinstance(record, TrafficRecord) else record
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(data, sort_keys=True) + "\n")


def load_replay(path: str | Path, expected_device_id: str) -> list[TrafficRecord]:
    """Load only explicit TX records for the exact selected device."""
    records: list[TrafficRecord] = []
    with Path(path).expanduser().resolve().open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("direction") != "tx":
                continue
            if raw.get("device_id") != expected_device_id:
                raise ValueError(
                    f"line {line_number}: capture device does not match {expected_device_id}"
                )
            payload_hex = raw.get("payload_hex", "")
            bytes.fromhex(payload_hex)
            records.append(
                TrafficRecord(
                    device_id=expected_device_id,
                    direction="tx",
                    payload_hex=payload_hex,
                    characteristic=raw.get("characteristic"),
                    framing=raw.get("framing"),
                    note=raw.get("note"),
                    timestamp=raw.get("timestamp", ""),
                )
            )
    return records


def write_json(path: str | Path, values: Iterable[dict[str, Any]] | dict[str, Any]) -> None:
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        json.dump(list(values) if not isinstance(values, dict) else values, stream, indent=2)
        stream.write("\n")
