"""Packet framing, unwrapping, and notification reassembly helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass

MAGIC_MARKER = b"\x80\x00"
FLUX_WRAPPER = b"\xb0\xb1\xb2\xb3"

# MagicHome2 BLE transport control byte, from the Surplife 1.6.5 APK
# (com.zengge.hagallbjarkan.protocol.zgble.LowerTransportLayerEncoder/Decoder).
CTRL_SEGMENTED = 0x40
CTRL_PROTECT = 0x20
CTRL_ACK_REQUESTED = 0x10
CTRL_TYPE_MASK = 0x0C
CTRL_TYPE_ACK = 0x08
CTRL_VERSION_MASK = 0x03
FRAME_HEADER_LENGTH = 8
CONTINUATION_HEADER_LENGTH = 5
LAST_SEGMENT_FLAG = 0x8000


def additive_checksum(payload: bytes | bytearray) -> int:
    """Return the low byte of the additive LEDENET checksum."""
    return sum(payload) & 0xFF


def with_checksum(payload: bytes | bytearray) -> bytes:
    """Append an additive checksum."""
    data = bytes(payload)
    return data + bytes((additive_checksum(data),))


def checksum_valid(payload: bytes | bytearray) -> bool:
    """Check an additive checksum without raising on an empty packet."""
    return bool(payload) and additive_checksum(payload[:-1]) == payload[-1]


@dataclass(slots=True)
class SequenceCounter:
    """Incrementing unsigned 8-bit MagicHome2 packet sequence."""

    value: int = 0

    def next(self) -> int:
        self.value = (self.value + 1) & 0xFF
        return self.value


def wrap_magichome(
    payload: bytes,
    sequence: int,
    *,
    command_family: int = 0x0A,
    length_bias: int = 0,
) -> bytes:
    """Wrap an inner command in the observed MagicHome2 v0 BLE envelope.

    The eight-byte header is ``flags, sequence, 0x8000, total length,
    payload length + 1, command family``. Two public captures disagree on the
    total-length field, so ``length_bias`` deliberately exposes that one
    distinction without changing the rest of the known-good header.
    """
    if not 0 <= sequence <= 0xFF:
        raise ValueError("sequence must be between 0 and 255")
    declared_length = len(payload) + length_bias
    if not 0 <= declared_length <= 0xFFFF:
        raise ValueError("framed payload is too long")
    if not 0 <= command_family <= 0xFF:
        raise ValueError("command_family must be between 0 and 255")
    return bytes((0, sequence)) + MAGIC_MARKER + declared_length.to_bytes(2, "big") + bytes(
        (len(payload) + 1, command_family)
    ) + payload


def segment_magichome(
    payload: bytes,
    sequence: int,
    *,
    command_family: int,
    max_frame_length: int,
) -> list[bytes]:
    """Split a command into MagicHome2 v0 frames no longer than ``max_frame_length``.

    A command that fits is one ordinary frame from ``wrap_magichome``. A longer
    one follows the app's segmented layout:

    - first frame: ``40 seq 00 00 <total len BE16> <chunk len + 1> <family>``
      followed by ``max_frame_length - 8`` payload bytes
    - each later frame: ``40 seq <index BE16> <chunk len>`` followed by up to
      ``max_frame_length - 5`` payload bytes; the last index has bit 15 set.
    """
    if len(payload) + FRAME_HEADER_LENGTH <= max_frame_length:
        return [wrap_magichome(payload, sequence, command_family=command_family)]
    if max_frame_length <= FRAME_HEADER_LENGTH:
        raise ValueError("max_frame_length is too small to carry any payload")
    if not 0 <= sequence <= 0xFF:
        raise ValueError("sequence must be between 0 and 255")
    if len(payload) > 0xFFFF:
        raise ValueError("framed payload is too long")
    first_chunk = max_frame_length - FRAME_HEADER_LENGTH
    frames = [
        bytes((CTRL_SEGMENTED, sequence, 0x00, 0x00))
        + len(payload).to_bytes(2, "big")
        + bytes((first_chunk + 1, command_family))
        + payload[:first_chunk]
    ]
    chunk_size = max_frame_length - CONTINUATION_HEADER_LENGTH
    offset = first_chunk
    index = 1
    while offset < len(payload):
        chunk = payload[offset : offset + chunk_size]
        offset += len(chunk)
        marker = index | (LAST_SEGMENT_FLAG if offset >= len(payload) else 0)
        if index > 0x7FFF:
            raise ValueError("framed payload needs too many segments")
        frames.append(
            bytes((CTRL_SEGMENTED, sequence))
            + marker.to_bytes(2, "big")
            + bytes((len(chunk),))
            + chunk
        )
        index += 1
    return frames


def is_ack_frame(frame: bytes) -> bool:
    """True for a transport-level ack packet, which carries no command data."""
    return (
        len(frame) >= 7
        and frame[2:4] == MAGIC_MARKER
        and frame[0] & CTRL_TYPE_MASK == CTRL_TYPE_ACK
    )


def unwrap_magichome(frame: bytes) -> tuple[bytes, str] | None:
    """Return the inner payload and framing description when a frame is recognized."""
    if len(frame) < 8 or frame[2:4] != MAGIC_MARKER:
        return None
    declared = int.from_bytes(frame[4:6], "big")
    payload_length = frame[6] - 1
    if payload_length < 0 or len(frame) < 8 + payload_length:
        return None
    body = frame[8 : 8 + payload_length]
    delta = declared - payload_length
    if delta not in (-2, 0, 2):
        return None
    return body, f"magichome2:length_delta={delta}"


def unwrap_flux(frame: bytes) -> bytes | None:
    """Extract an inner command from a B0 B1 B2 B3 LAN wrapper."""
    if len(frame) < 11 or not frame.startswith(FLUX_WRAPPER):
        return None
    inner_length = int.from_bytes(frame[8:10], "big")
    end = 10 + inner_length
    if end > len(frame):
        return None
    return frame[10:end]


def candidate_payloads(packet: bytes) -> list[tuple[bytes, str]]:
    """Return raw and safely unwrapped interpretations of a packet."""
    candidates: list[tuple[bytes, str]] = [(packet, "raw")]
    seen = {packet}
    index = 0
    while index < len(candidates):
        current, description = candidates[index]
        index += 1
        magic = unwrap_magichome(current)
        if magic and magic[0] not in seen:
            seen.add(magic[0])
            candidates.append((magic[0], f"{description}/{magic[1]}"))
        flux = unwrap_flux(current)
        if flux and flux not in seen:
            seen.add(flux)
            candidates.append((flux, f"{description}/flux-wrapper"))
        if current.startswith(b"\x0a") and current[1:] not in seen:
            seen.add(current[1:])
            candidates.append((current[1:], f"{description}/command-family"))
        if current.startswith((b"{", b"[")):
            try:
                document = json.loads(current.decode("utf-8"))
                encoded = document.get("payload") if isinstance(document, dict) else None
                decoded = bytes.fromhex(encoded) if isinstance(encoded, str) else None
            except (UnicodeDecodeError, ValueError, TypeError):
                decoded = None
            if decoded and decoded not in seen:
                seen.add(decoded)
                candidates.append((decoded, f"{description}/json-payload"))
    return candidates


class NotificationReassembler:
    """Reassemble common state responses split across BLE notifications."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        # (sequence, declared total length, next expected index, data so far)
        self._segment: tuple[int, int, int, bytearray] | None = None

    def reset(self) -> None:
        self._buffer.clear()
        self._segment = None

    def _feed_segment(self, chunk: bytes) -> list[bytes] | None:
        """Handle a segmented transport frame, or return None when it is not one."""
        if not self._buffer and len(chunk) >= FRAME_HEADER_LENGTH and (
            chunk[0] & 0xC0 == CTRL_SEGMENTED and chunk[2:4] == b"\x00\x00"
        ):
            total = int.from_bytes(chunk[4:6], "big")
            data = bytearray(chunk[FRAME_HEADER_LENGTH : FRAME_HEADER_LENGTH + chunk[6] - 1])
            self._segment = (chunk[1], total, 1, data)
            return []
        segment = self._segment
        if (
            segment is None
            or len(chunk) < CONTINUATION_HEADER_LENGTH
            or chunk[0] & 0xC0 != CTRL_SEGMENTED
            or chunk[1] != segment[0]
        ):
            return None
        sequence, total, expected, data = segment
        marker = int.from_bytes(chunk[2:4], "big")
        if marker & 0x7FFF != expected:
            # A lost or reordered segment makes the whole command unusable.
            self._segment = None
            return []
        data.extend(chunk[CONTINUATION_HEADER_LENGTH : CONTINUATION_HEADER_LENGTH + chunk[4]])
        if not marker & LAST_SEGMENT_FLAG:
            self._segment = (sequence, total, expected + 1, data)
            return []
        self._segment = None
        return [bytes(data[:total])] if len(data) >= total else []

    def feed(self, chunk: bytes) -> list[bytes]:
        """Append one notification and return any complete recognizable packets."""
        if not chunk:
            return []
        if not self._buffer and is_ack_frame(chunk):
            return []
        segmented = self._feed_segment(chunk)
        if segmented is not None:
            return segmented
        if not self._buffer:
            self._buffer.extend(chunk)
        else:
            self._buffer.extend(chunk)

        data = bytes(self._buffer)
        if data.startswith(b"\xea\x81"):
            if len(data) >= 27:
                # Captures exist with both a 27-byte state and an additional
                # trailing footer byte. Preserve the footer unless byte 27 is
                # clearly the beginning of the next state frame.
                packet_length = 27
                if len(data) >= 28 and data[27:29] != b"\xea\x81":
                    packet_length = 28
                packet = data[:packet_length]
                del self._buffer[:packet_length]
                return [packet]
            if len(data) == 21 and len(chunk) == 21:
                self._buffer.clear()
                return [data]
            return []
        if data.startswith(b"\x81"):
            if len(data) < 14:
                return []
            packet = data[:14]
            del self._buffer[:14]
            return [packet]
        if len(data) >= 8 and data[2:4] == MAGIC_MARKER:
            payload_length = data[6] - 1
            if payload_length < 0:
                self._buffer.clear()
                return [data]
            total = 8 + payload_length
            if len(data) >= total:
                packet = data[:total]
                del self._buffer[:total]
                return [packet]
            return []

        self._buffer.clear()
        return [data]
