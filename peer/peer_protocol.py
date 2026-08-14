from __future__ import annotations

import json
import socket
import struct
from typing import Any


HEADER_SIZE = 4
DEFAULT_MAX_MESSAGE_SIZE = 8 * 1024 * 1024


class PeerProtocolError(ValueError):
    """Raised when a peer-to-peer frame or JSON message is invalid."""


class PeerConnectionClosed(ConnectionError):
    """Raised when a socket closes before a complete message is received."""


def encode_message(
    message: dict[str, Any],
    *,
    max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
) -> bytes:
    """Serialize one dictionary as a 4-byte-length-prefixed JSON frame."""
    if not isinstance(message, dict):
        raise PeerProtocolError("peer message must be a dictionary")

    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type.strip():
        raise PeerProtocolError("peer message must contain a non-empty string 'type'")

    if isinstance(max_message_size, bool) or not isinstance(max_message_size, int):
        raise ValueError("max_message_size must be an integer")
    if max_message_size <= 0:
        raise ValueError("max_message_size must be positive")

    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PeerProtocolError(f"peer message is not JSON serializable: {exc}") from exc

    if not payload:
        raise PeerProtocolError("peer message payload must not be empty")
    if len(payload) > max_message_size:
        raise PeerProtocolError(
            f"peer message exceeds maximum size of {max_message_size} bytes"
        )

    return struct.pack("!I", len(payload)) + payload


def send_message(
    sock: socket.socket,
    message: dict[str, Any],
    *,
    max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
) -> None:
    """Send one complete length-prefixed JSON message."""
    frame = encode_message(message, max_message_size=max_message_size)
    sock.sendall(frame)


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """Receive exactly ``size`` bytes or raise if the connection closes."""
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("size must be an integer")
    if size < 0:
        raise ValueError("size must be non-negative")

    chunks = bytearray()
    while len(chunks) < size:
        try:
            chunk = sock.recv(size - len(chunks))
        except socket.timeout as exc:
            raise TimeoutError(
                f"timed out after receiving {len(chunks)} of {size} bytes"
            ) from exc

        if not chunk:
            raise PeerConnectionClosed(
                f"connection closed after receiving {len(chunks)} of {size} bytes"
            )
        chunks.extend(chunk)

    return bytes(chunks)


def receive_message(
    sock: socket.socket,
    *,
    max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
) -> dict[str, Any]:
    """Receive, decode, and validate one length-prefixed JSON message."""
    if isinstance(max_message_size, bool) or not isinstance(max_message_size, int):
        raise ValueError("max_message_size must be an integer")
    if max_message_size <= 0:
        raise ValueError("max_message_size must be positive")

    header = recv_exact(sock, HEADER_SIZE)
    (payload_length,) = struct.unpack("!I", header)

    if payload_length == 0:
        raise PeerProtocolError("peer message payload length must be positive")
    if payload_length > max_message_size:
        raise PeerProtocolError(
            f"peer message length {payload_length} exceeds maximum "
            f"{max_message_size}"
        )

    payload = recv_exact(sock, payload_length)

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PeerProtocolError("peer message payload is not valid UTF-8") from exc

    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PeerProtocolError(
            f"peer message payload is not valid JSON: {exc.msg}"
        ) from exc

    if not isinstance(message, dict):
        raise PeerProtocolError("decoded peer message must be a dictionary")

    message_type = message.get("type")
    if not isinstance(message_type, str) or not message_type.strip():
        raise PeerProtocolError(
            "decoded peer message must contain a non-empty string 'type'"
        )

    return message
