from __future__ import annotations

import base64
from pathlib import Path
import socket
import sys
import threading
import time
from typing import Any

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

from common.event_logger import EventLogger
from peer.peer_protocol import (
    DEFAULT_MAX_MESSAGE_SIZE,
    PeerConnectionClosed,
    PeerProtocolError,
    receive_message,
    send_message,
)
from peer.piece_manager import PieceManager, PieceUnavailable
from peer.transfer_stats import TransferStats


class PeerServerError(RuntimeError):
    """Raised when the peer TCP server cannot start or process a request."""


def _decode_fixed_hex(
    value: Any,
    *,
    field_name: str,
    byte_length: int,
) -> bytes:
    expected_characters = byte_length * 2
    if not isinstance(value, str) or len(value) != expected_characters:
        raise PeerProtocolError(
            f"{field_name} must be a {expected_characters}-character hexadecimal string"
        )
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise PeerProtocolError(f"{field_name} is not valid hexadecimal") from exc
    if len(decoded) != byte_length:
        raise PeerProtocolError(
            f"{field_name} must decode to exactly {byte_length} bytes"
        )
    return decoded


def _decode_peer_id_hex(value: Any, field_name: str = "peer_id") -> bytes:
    return _decode_fixed_hex(
        value,
        field_name=field_name,
        byte_length=20,
    )


def _decode_info_hash_hex(value: Any) -> bytes:
    return _decode_fixed_hex(
        value,
        field_name="info_hash",
        byte_length=20,
    )


class PeerServer:
    """
    Multithreaded TCP service for PING/PONG and piece exchange.

    Supported request messages:

    - PING
    - BITFIELD_REQUEST
    - INTERESTED
    - REQUEST
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        peer_id: bytes,
        logger: EventLogger | None = None,
        piece_manager: PieceManager | None = None,
        transfer_stats: TransferStats | None = None,
        client_timeout: float = 3.0,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
    ):
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("port must be an integer")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if not isinstance(peer_id, bytes) or len(peer_id) != 20:
            raise ValueError("peer_id must be exactly 20 bytes")
        if client_timeout <= 0:
            raise ValueError("client_timeout must be positive")
        if max_message_size <= 0:
            raise ValueError("max_message_size must be positive")

        self.host = host
        self.requested_port = port
        self.peer_id = peer_id
        self.logger = logger
        self.piece_manager = piece_manager
        self.transfer_stats = transfer_stats or TransferStats()
        self.client_timeout = float(client_timeout)
        self.max_message_size = int(max_message_size)

        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._worker_threads: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()
        self._interest_lock = threading.Lock()
        self._interested_peers: set[tuple[bytes, bytes]] = set()
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._started = False

    @property
    def address(self) -> tuple[str, int]:
        listener = self._listener
        if listener is None:
            return self.host, self.requested_port
        host, port = listener.getsockname()[:2]
        return str(host), int(port)

    @property
    def port(self) -> int:
        return self.address[1]

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return

            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(0.2)

            try:
                listener.bind((self.host, self.requested_port))
                listener.listen(128)
            except OSError as exc:
                listener.close()
                raise PeerServerError(
                    f"could not listen on {self.host}:{self.requested_port}: {exc}"
                ) from exc

            self._listener = listener
            self._stop_event.clear()
            self._accept_thread = threading.Thread(
                target=self._accept_loop,
                name=f"peer-accept-{self.port}",
                daemon=True,
            )
            self._started = True
            self._accept_thread.start()

            piece_count = (
                self.piece_manager.metainfo.piece_count
                if self.piece_manager is not None
                else 0
            )
            self._log(
                "PEER_SERVER_STARTED",
                (
                    f"peer_id={self.peer_id.hex()} "
                    f"listening={self.address[0]}:{self.port} "
                    f"piece_count={piece_count}"
                ),
            )

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return

            self._stop_event.set()
            listener = self._listener
            self._listener = None

            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass

            accept_thread = self._accept_thread
            if accept_thread is not None:
                accept_thread.join(timeout=3.0)

            with self._workers_lock:
                workers = list(self._worker_threads)

            for worker in workers:
                worker.join(timeout=self.client_timeout + 1.0)

            self._started = False
            self._log(
                "PEER_SERVER_STOPPED",
                f"peer_id={self.peer_id.hex()}",
            )

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            listener = self._listener
            if listener is None:
                return

            try:
                client_socket, client_address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    return
                self._log("PEER_ACCEPT_ERROR", "listener accept failed")
                continue

            worker = threading.Thread(
                target=self._handle_connection,
                args=(client_socket, client_address),
                name=f"peer-client-{client_address[0]}-{client_address[1]}",
                daemon=True,
            )

            with self._workers_lock:
                self._worker_threads.add(worker)

            worker.start()

    def _handle_connection(
        self,
        client_socket: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        current_thread = threading.current_thread()
        remote = f"{client_address[0]}:{client_address[1]}"

        self._log("CONNECTION_ACCEPTED", f"remote={remote}")

        try:
            client_socket.settimeout(self.client_timeout)
            message = receive_message(
                client_socket,
                max_message_size=self.max_message_size,
            )
            response = self._dispatch(message, remote)
            uploaded_bytes = int(response.pop("_account_uploaded", 0))
            send_message(
                client_socket,
                response,
                max_message_size=self.max_message_size,
            )

            if uploaded_bytes:
                self.transfer_stats.add_uploaded(uploaded_bytes)
                self._log(
                    "PIECE_SENT",
                    (
                        f"remote={remote} piece_index={response.get('piece_index')} "
                        f"bytes={uploaded_bytes}"
                    ),
                )

        except (PeerProtocolError, PeerConnectionClosed, TimeoutError) as exc:
            self._log(
                "PEER_PROTOCOL_ERROR",
                f"remote={remote} error={type(exc).__name__}: {exc}",
            )
            try:
                send_message(
                    client_socket,
                    {"type": "ERROR", "message": str(exc)},
                    max_message_size=self.max_message_size,
                )
            except Exception:
                pass
        except OSError as exc:
            self._log(
                "PEER_SOCKET_ERROR",
                f"remote={remote} error={exc}",
            )
        except Exception as exc:
            self._log(
                "PEER_SERVER_ERROR",
                f"remote={remote} error={type(exc).__name__}: {exc}",
            )
            try:
                send_message(
                    client_socket,
                    {"type": "ERROR", "message": "internal peer error"},
                    max_message_size=self.max_message_size,
                )
            except Exception:
                pass
        finally:
            try:
                client_socket.close()
            except OSError:
                pass
            self._log("CONNECTION_CLOSED", f"remote={remote}")
            with self._workers_lock:
                self._worker_threads.discard(current_thread)

    def _dispatch(self, message: dict[str, Any], remote: str) -> dict[str, Any]:
        message_type = message["type"]

        if message_type == "PING":
            return self._handle_ping(message, remote)
        if message_type == "BITFIELD_REQUEST":
            return self._handle_bitfield_request(message, remote)
        if message_type == "INTERESTED":
            return self._handle_interested(message, remote)
        if message_type == "REQUEST":
            return self._handle_piece_request(message, remote)

        self._log(
            "UNSUPPORTED_MESSAGE",
            f"remote={remote} type={message_type}",
        )
        return {
            "type": "ERROR",
            "message": f"unsupported message type: {message_type}",
        }

    def _handle_ping(
        self,
        message: dict[str, Any],
        remote: str,
    ) -> dict[str, Any]:
        remote_peer_id = _decode_peer_id_hex(message.get("peer_id"))
        nonce = message.get("nonce")
        sent_at = message.get("sent_at")

        if not isinstance(nonce, str) or not nonce:
            raise PeerProtocolError("PING nonce must be a non-empty string")
        if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)):
            raise PeerProtocolError("PING sent_at must be a number")

        self._log(
            "PING_RECEIVED",
            (
                f"remote={remote} peer_id={remote_peer_id.hex()} "
                f"nonce={nonce}"
            ),
        )

        response = {
            "type": "PONG",
            "peer_id": self.peer_id.hex(),
            "nonce": nonce,
            "ping_sent_at": float(sent_at),
            "pong_sent_at": time.time(),
        }

        self._log(
            "PONG_SENT",
            f"remote={remote} peer_id={remote_peer_id.hex()} nonce={nonce}",
        )
        return response

    def _require_piece_manager(
        self,
        requested_info_hash: bytes,
    ) -> PieceManager:
        manager = self.piece_manager
        if manager is None:
            raise PeerProtocolError("this peer does not serve torrent pieces")

        if manager.metainfo.info_hash != requested_info_hash:
            raise PeerProtocolError("requested info_hash is not served by this peer")

        return manager

    def _validate_torrent_request(
        self,
        message: dict[str, Any],
    ) -> tuple[bytes, bytes, PieceManager]:
        remote_peer_id = _decode_peer_id_hex(message.get("peer_id"))
        info_hash = _decode_info_hash_hex(message.get("info_hash"))
        manager = self._require_piece_manager(info_hash)
        return remote_peer_id, info_hash, manager

    def _handle_bitfield_request(
        self,
        message: dict[str, Any],
        remote: str,
    ) -> dict[str, Any]:
        remote_peer_id, info_hash, manager = self._validate_torrent_request(message)
        bitfield = list(manager.bitfield)

        self._log(
            "BITFIELD_SENT",
            (
                f"remote={remote} peer_id={remote_peer_id.hex()} "
                f"info_hash={info_hash.hex()} "
                f"available={sum(bitfield)} total={len(bitfield)}"
            ),
        )

        return {
            "type": "BITFIELD",
            "peer_id": self.peer_id.hex(),
            "info_hash": info_hash.hex(),
            "pieces": bitfield,
        }

    def _handle_interested(
        self,
        message: dict[str, Any],
        remote: str,
    ) -> dict[str, Any]:
        remote_peer_id, info_hash, _ = self._validate_torrent_request(message)

        with self._interest_lock:
            self._interested_peers.add((info_hash, remote_peer_id))

        self._log(
            "INTERESTED_RECEIVED",
            (
                f"remote={remote} peer_id={remote_peer_id.hex()} "
                f"info_hash={info_hash.hex()}"
            ),
        )
        self._log(
            "UNCHOKE_SENT",
            f"remote={remote} peer_id={remote_peer_id.hex()}",
        )

        return {
            "type": "UNCHOKE",
            "peer_id": self.peer_id.hex(),
            "info_hash": info_hash.hex(),
        }

    def _handle_piece_request(
        self,
        message: dict[str, Any],
        remote: str,
    ) -> dict[str, Any]:
        remote_peer_id, info_hash, manager = self._validate_torrent_request(message)
        piece_index = message.get("piece_index")

        with self._interest_lock:
            is_interested = (info_hash, remote_peer_id) in self._interested_peers

        if not is_interested:
            raise PeerProtocolError(
                "REQUEST requires an earlier INTERESTED/UNCHOKE exchange"
            )

        if isinstance(piece_index, bool) or not isinstance(piece_index, int):
            raise PeerProtocolError("REQUEST piece_index must be an integer")

        self._log(
            "PIECE_REQUESTED",
            (
                f"remote={remote} peer_id={remote_peer_id.hex()} "
                f"piece_index={piece_index}"
            ),
        )

        try:
            data = manager.read_piece(piece_index)
        except (ValueError, PieceUnavailable) as exc:
            self._log(
                "PIECE_UNAVAILABLE",
                f"remote={remote} piece_index={piece_index} reason={exc}",
            )
            return {
                "type": "ERROR",
                "message": str(exc),
            }

        encoded = base64.b64encode(data).decode("ascii")

        return {
            "type": "PIECE",
            "peer_id": self.peer_id.hex(),
            "info_hash": info_hash.hex(),
            "piece_index": piece_index,
            "sha1": manager.expected_hash(piece_index),
            "data": encoded,
            "_account_uploaded": len(data),
        }

    def _log(self, event_type: str, description: str) -> None:
        if self.logger is not None:
            self.logger.log(event_type, description)

    def __enter__(self) -> "PeerServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()
