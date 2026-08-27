import argparse
import json

import pytest

from surplife_ble.cli import _hex_bytes, _replay, _rgb, build_parser


def test_cli_surface() -> None:
    parser = build_parser()
    args = parser.parse_args(["color", "--device", "ABC", "--rgb", "1,2,3"])
    assert args.rgb == (1, 2, 3)
    assert args.brightness == 100


def test_raw_write_flag_is_required() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["raw-write", "--device", "ABC", "--characteristic", "ff01", "--hex", "01"]
        )


def test_cli_value_validation() -> None:
    assert _hex_bytes("01:02") == b"\x01\x02"
    with pytest.raises(argparse.ArgumentTypeError):
        _rgb("256,0,0")


def test_hci_convert_subcommand_parses() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "hci-convert", "--snoop", "foo.log", "--device", "DEV",
        "--gatt", "gatt.json", "--jsonl", "out.jsonl",
    ])
    assert args.snoop == "foo.log"
    assert args.device == "DEV"
    assert args.gatt == "gatt.json"
    assert args.jsonl == "out.jsonl"
    assert args.list_connections is False


def test_replay_dry_run_and_limit_parse() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "replay", "--device", "DEV", "--capture", "c.jsonl",
        "--allow-raw-write", "--dry-run", "--limit", "3",
    ])
    assert args.dry_run is True
    assert args.limit == 3


def test_diff_subcommand_parses_hex() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "diff", "--a", "ea810100aa", "--b", "ea810100ab",
        "--label-a", "on", "--label-b", "off",
    ])
    assert args.hex_a == "ea810100aa"
    assert args.hex_b == "ea810100ab"
    assert args.label_a == "on"
    assert args.label_b == "off"


def test_diff_subcommand_parses_captures() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "diff", "--a-capture", "a.jsonl", "--b-capture", "b.jsonl",
    ])
    assert args.capture_a == "a.jsonl"
    assert args.capture_b == "b.jsonl"
    assert args.hex_a is None


@pytest.mark.asyncio
async def test_replay_dry_run_does_not_connect(tmp_path) -> None:
    capture_path = tmp_path / "traffic.jsonl"
    capture_path.write_text(
        json.dumps({
            "device_id": "DEV",
            "direction": "tx",
            "payload_hex": "71230f",
            "characteristic": "0000ff01-0000-1000-8000-00805f9b34fb",
            "note": "power on",
        }) + "\n",
        encoding="utf-8",
    )
    parser = build_parser()
    args = parser.parse_args([
        "replay", "--device", "DEV", "--capture", str(capture_path),
        "--allow-raw-write", "--dry-run",
    ])
    result = await _replay(args)
    assert result == 0
