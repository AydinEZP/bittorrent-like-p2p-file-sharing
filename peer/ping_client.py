from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from typing import Callable, Iterable

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

from common.event_logger import EventLogger
from peer.peer_protocol import (
    PeerConnectionClosed,
    PeerProtocolError,
    receive_message,
    send_message,
)
from peer.tracker_client import TrackerPeer


class PeerPingError(RuntimeError):
    """Raised when a PING/PONG exchange fails validation."""


@dataclass(frozen=True)
class PingResult:
    peer_id: bytes
    ip: str
    port: int
    success: bool
    rtt_ms: float | None
    error: str | None


def _decode_peer_id_hex(value: object) -> bytes:
    if not isinstance(value, str) or len(value) != 40:
        raise PeerPingError("PONG peer_id must be a 40-character hex string")
    try:
        peer_id = bytes.fromhex(value)
    except ValueError as exc:
        raise PeerPingError("PONG peer_id is not valid hexadecimal") from exc
    if len(peer_id) != 20:
        raise PeerPingError("PONG peer_id must decode to exactly 20 bytes")
    return peer_id


def ping_peer(
    peer: TrackerPeer,
    *,
    local_peer_id: bytes,
    timeout: float = 3.0,
    logger: EventLogger | None = None,
) -> PingResult:
    """Connect to one Tracker-discovered peer and validate its PONG."""
    if not isinstance(local_peer_id, bytes) or len(local_peer_id) != 20:
        raise ValueError("local_peer_id must be exactly 20 bytes")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    nonce = secrets.token_hex(16)
    sent_at = time.time()
    started = time.perf_counter()

    if logger is not None:
        logger.log(
            "PING_SENT",
            (
                f"peer_id={peer.peer_id.hex()} target={peer.ip}:{peer.port} "
                f"nonce={nonce}"
            ),
        )

    try:
        with socket.create_connection((peer.ip, peer.port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            send_message(
                sock,
                {
                    "type": "PING",
                    "peer_id": local_peer_id.hex(),
                    "nonce": nonce,
                    "sent_at": sent_at,
                },
            )
            response = receive_message(sock)

        if response.get("type") == "ERROR":
            raise PeerPingError(
                f"remote peer returned ERROR: {response.get('message', 'unknown error')}"
            )
        if response.get("type") != "PONG":
            raise PeerPingError(
                f"expected PONG, received {response.get('type')!r}"
            )
        if response.get("nonce") != nonce:
            raise PeerPingError("PONG nonce does not match the PING nonce")

        responder_peer_id = _decode_peer_id_hex(response.get("peer_id"))
        if responder_peer_id != peer.peer_id:
            raise PeerPingError(
                "PONG peer_id does not match the Tracker peer_id"
            )

        rtt_ms = (time.perf_counter() - started) * 1000.0

        if logger is not None:
            logger.log(
                "PONG_RECEIVED",
                (
                    f"peer_id={responder_peer_id.hex()} source={peer.ip}:{peer.port} "
                    f"nonce={nonce} rtt_ms={rtt_ms:.3f}"
                ),
            )

        return PingResult(
            peer_id=peer.peer_id,
            ip=peer.ip,
            port=peer.port,
            success=True,
            rtt_ms=rtt_ms,
            error=None,
        )

    except (OSError, TimeoutError, PeerProtocolError, PeerConnectionClosed, PeerPingError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        if logger is not None:
            logger.log(
                "PING_FAILED",
                (
                    f"peer_id={peer.peer_id.hex()} target={peer.ip}:{peer.port} "
                    f"error={error}"
                ),
            )
        return PingResult(
            peer_id=peer.peer_id,
            ip=peer.ip,
            port=peer.port,
            success=False,
            rtt_ms=None,
            error=error,
        )


def ping_all_peers(
    peers: Iterable[TrackerPeer],
    *,
    local_peer_id: bytes,
    timeout: float = 3.0,
    max_workers: int = 16,
    logger: EventLogger | None = None,
) -> tuple[PingResult, ...]:
    """Ping all supplied peers concurrently and return deterministic results."""
    peer_list = list(peers)

    if not peer_list:
        return ()
    if isinstance(max_workers, bool) or not isinstance(max_workers, int):
        raise ValueError("max_workers must be an integer")
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")

    worker_count = min(max_workers, len(peer_list))
    results: list[PingResult] = []

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                ping_peer,
                peer,
                local_peer_id=local_peer_id,
                timeout=timeout,
                logger=logger,
            ): peer
            for peer in peer_list
        }

        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda item: (item.peer_id, item.ip, item.port))
    return tuple(results)


class PeerPingService:
    """Background service that periodically pings the current peer list."""

    def __init__(
        self,
        *,
        local_peer_id: bytes,
        peer_provider: Callable[[], Iterable[TrackerPeer]],
        interval: float,
        timeout: float = 3.0,
        max_workers: int = 16,
        logger: EventLogger | None = None,
        on_results: Callable[[tuple[PingResult, ...]], None] | None = None,
    ):
        if not isinstance(local_peer_id, bytes) or len(local_peer_id) != 20:
            raise ValueError("local_peer_id must be exactly 20 bytes")
        if not callable(peer_provider):
            raise ValueError("peer_provider must be callable")
        if interval <= 0:
            raise ValueError("interval must be positive")

        self.local_peer_id = local_peer_id
        self.peer_provider = peer_provider
        self.interval = float(interval)
        self.timeout = float(timeout)
        self.max_workers = max_workers
        self.logger = logger
        self.on_results = on_results

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="peer-periodic-pinger",
                daemon=True,
            )
            self._started = True
            self._thread.start()
            if self.logger is not None:
                self.logger.log(
                    "PING_SERVICE_STARTED",
                    f"interval={self.interval} timeout={self.timeout}",
                )

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._stop_event.set()
            thread = self._thread

        if thread is not None:
            thread.join(timeout=self.timeout + self.interval + 2.0)

        with self._lock:
            self._started = False

        if self.logger is not None:
            self.logger.log("PING_SERVICE_STOPPED", "periodic pinger stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                peers = tuple(self.peer_provider())
                results = ping_all_peers(
                    peers,
                    local_peer_id=self.local_peer_id,
                    timeout=self.timeout,
                    max_workers=self.max_workers,
                    logger=self.logger,
                )
                if self.on_results is not None:
                    self.on_results(results)
            except Exception as exc:
                if self.logger is not None:
                    self.logger.log(
                        "PING_SERVICE_ERROR",
                        f"{type(exc).__name__}: {exc}",
                    )

            if self._stop_event.wait(self.interval):
                break

    def __enter__(self) -> "PeerPingService":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()
