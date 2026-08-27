from custom_components.surplife_ble.core.framing import (
    NotificationReassembler,
    SequenceCounter,
    additive_checksum,
    candidate_payloads,
    checksum_valid,
    unwrap_magichome,
    with_checksum,
    wrap_magichome,
)


def test_checksum_round_trip() -> None:
    payload = bytes.fromhex("818a8b")
    assert additive_checksum(payload) == 0x96
    assert with_checksum(payload) == bytes.fromhex("818a8b96")
    assert checksum_valid(bytes.fromhex("818a8b96"))
    assert not checksum_valid(bytes.fromhex("818a8b97"))
    assert not checksum_valid(b"")


def test_magichome_wrappers_are_explicit() -> None:
    inner = bytes.fromhex("ea818a8b")
    standard = wrap_magichome(inner, 3)
    plus_two = wrap_magichome(inner, 4, length_bias=2)
    family_0b = wrap_magichome(inner, 5, command_family=0x0B)

    assert standard.hex() == "000380000004050aea818a8b"
    assert plus_two.hex() == "000480000006050aea818a8b"
    assert family_0b.hex() == "000580000004050bea818a8b"
    assert unwrap_magichome(standard) == (inner, "magichome2:length_delta=0")
    assert unwrap_magichome(plus_two) == (inner, "magichome2:length_delta=2")
    assert any(payload == inner for payload, _description in candidate_payloads(standard))


def test_sequence_rollover() -> None:
    sequence = SequenceCounter(0xFE)
    assert sequence.next() == 0xFF
    assert sequence.next() == 0
    assert sequence.next() == 1


def test_wrapped_notification_reassembly() -> None:
    inner = bytes.fromhex("ea810100b60924660164f000000000640500640000002002010003")
    packet = wrap_magichome(inner, 7)
    reassembler = NotificationReassembler()
    assert reassembler.feed(packet[:11]) == []
    assert reassembler.feed(packet[11:]) == [packet]


def test_json_wrapped_iotbt_payload_is_extracted() -> None:
    packet = bytes.fromhex(
        "041e8000004d4e0a7b22636f6465223a302c227061796c6f6164223a"
        "2245413831303130304141303932333235364635304630303036343634"
        "3030363430353030314446464646464646463031303030303030227d"
    )
    expected = bytes.fromhex("ea810100aa0923256f50f0006464006405001dffffffff01000000")
    assert any(payload == expected for payload, _description in candidate_payloads(packet))


def test_extended_notification_reassembly() -> None:
    packet = bytes.fromhex("ea810100b60924660164f00000000064050064000000200201000003")
    reassembler = NotificationReassembler()
    assert reassembler.feed(packet[:20]) == []
    assert reassembler.feed(packet[20:]) == [packet]


def test_unknown_notifications_are_preserved() -> None:
    reassembler = NotificationReassembler()
    assert reassembler.feed(bytes.fromhex("010203")) == [bytes.fromhex("010203")]
