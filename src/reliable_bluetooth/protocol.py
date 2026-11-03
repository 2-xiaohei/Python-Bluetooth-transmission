from __future__ import annotations

import json
import struct
import zlib
from collections.abc import Mapping
from typing import Any


MAGIC = b"RB"
HEADER = struct.Struct(">2sBI")
CHECKSUM = struct.Struct(">I")


class ProtocolError(ValueError):
    """Raised when a frame cannot be decoded safely."""


def encode_frame(
    message: Mapping[str, Any], *, version: int = 1, max_frame_size: int = 1_048_576
) -> bytes:
    try:
        payload = json.dumps(
            dict(message), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"Message is not JSON serializable: {exc}") from exc
    if len(payload) > max_frame_size:
        raise ProtocolError(
            f"Frame payload is {len(payload)} bytes; limit is {max_frame_size}"
        )
    if not 0 <= version <= 255:
        raise ProtocolError("Protocol version must fit in one byte")
    header = HEADER.pack(MAGIC, version, len(payload))
    checksum = CHECKSUM.pack(zlib.crc32(header + payload) & 0xFFFFFFFF)
    return header + payload + checksum


class FrameDecoder:
    """Incrementally decode framed JSON from arbitrary BLE notification chunks."""

    def __init__(self, *, version: int = 1, max_frame_size: int = 1_048_576):
        self._version = version
        self._max_frame_size = max_frame_size
        self._buffer = bytearray()

    def feed(self, data: bytes | bytearray | memoryview) -> list[dict[str, Any]]:
        self._buffer.extend(data)
        messages: list[dict[str, Any]] = []
        try:
            while len(self._buffer) >= HEADER.size:
                magic, version, payload_length = HEADER.unpack_from(self._buffer)
                if magic != MAGIC:
                    raise ProtocolError("Invalid frame magic")
                if version != self._version:
                    raise ProtocolError(
                        f"Unsupported frame version {version}; expected {self._version}"
                    )
                if payload_length > self._max_frame_size:
                    raise ProtocolError(
                        f"Incoming frame exceeds {self._max_frame_size} bytes"
                    )
                frame_length = HEADER.size + payload_length + CHECKSUM.size
                if len(self._buffer) < frame_length:
                    break
                frame = bytes(self._buffer[:frame_length])
                del self._buffer[:frame_length]
                body_end = HEADER.size + payload_length
                expected_crc = CHECKSUM.unpack(frame[body_end:])[0]
                actual_crc = zlib.crc32(frame[:body_end]) & 0xFFFFFFFF
                if actual_crc != expected_crc:
                    raise ProtocolError("Frame checksum mismatch")
                try:
                    message = json.loads(frame[HEADER.size:body_end].decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProtocolError(f"Invalid JSON payload: {exc}") from exc
                if not isinstance(message, dict):
                    raise ProtocolError("Frame payload must be a JSON object")
                messages.append(message)
        except ProtocolError:
            self._buffer.clear()
            raise
        return messages

