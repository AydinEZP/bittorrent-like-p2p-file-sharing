from __future__ import annotations

from dataclasses import dataclass
import base64
from pathlib import Path
import socket
import sys
from typing import Any

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

from common.event_logger import EventLogger
from peer.peer_protocol import PeerProtocolError, receive_message, send_message
from peer.piece_manager import PieceHashMismatch, PieceManager
from peer.tracker_client import TrackerPeer
from peer.transfer_stats import TransferStats


class PieceClientError(RuntimeError):
    """Raised when peer piece exchange fails."""


@dataclass(frozen=True)
class PieceDownloadResult:
    requested: int
    downloaded: int
    skipped: int
    bytes_downloaded: int
    complete: bool
    errors: tuple[str, ...]


class PieceClient:
    """Simple peer-to-peer downloader using BITFIELD/REQUEST/PIECE messages."""

    def __init__(
        self,
        *,
        local_peer_id: bytes,
        info_hash: bytes,
        piece_manager: PieceManager,
        transfer_stats: TransferStats | None = None,
        timeout: float = 5.0,
        logger: EventLogger | None = None,
    ):
        if not isinstance(local_peer_id, bytes) or len(local_peer_id) != 20:
            raise ValueError("local_peer_id must be exactly 20 bytes")
        if not isinstance(info_hash, bytes) or len(info_hash) != 20:
            raise ValueError("info_hash must be exactly 20 bytes")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self.local_peer_id = local_peer_id
        self.info_hash = info_hash
        self.piece_manager = piece_manager
        self.transfer_stats = transfer_stats or TransferStats()
        self.timeout = float(timeout)
        self.logger = logger

    def fetch_bitfield(self, peer: TrackerPeer) -> tuple[bool, ...]:
        response = self._exchange(
            peer,
            {
                "type": "BITFIELD_REQUEST",
                "peer_id": self.local_peer_id.hex(),
                "info_hash": self.info_hash.hex(),
            },
        )

        if response.get("type") == "ERROR":
            raise PieceClientError(str(response.get("message", "peer error")))
        if response.get("type") != "BITFIELD":
            raise PieceClientError(
                f"expected BITFIELD, received {response.get('type')!r}"
            )
        if response.get("info_hash") != self.info_hash.hex():
            raise PieceClientError("BITFIELD info_hash does not match requested torrent")

        pieces = response.get("pieces")
        if not isinstance(pieces, list):
            raise PieceClientError("BITFIELD pieces must be a list")
        if len(pieces) != self.piece_manager.metainfo.piece_count:
            raise PieceClientError(
                "BITFIELD piece count does not match local Metainfo"
            )
        if not all(isinstance(value, bool) for value in pieces):
            raise PieceClientError("BITFIELD values must be booleans")

        bitfield = tuple(pieces)
        self._log(
            "BITFIELD_RECEIVED",
            (
                f"remote={peer.ip}:{peer.port} "
                f"available={sum(bitfield)} total={len(bitfield)}"
            ),
        )
        return bitfield

    def express_interest(self, peer: TrackerPeer) -> None:
        self._log(
            "INTERESTED_SENT",
            f"remote={peer.ip}:{peer.port} info_hash={self.info_hash.hex()}",
        )

        response = self._exchange(
            peer,
            {
                "type": "INTERESTED",
                "peer_id": self.local_peer_id.hex(),
                "info_hash": self.info_hash.hex(),
            },
        )

        if response.get("type") == "ERROR":
            raise PieceClientError(str(response.get("message", "peer error")))
        if response.get("type") != "UNCHOKE":
            raise PieceClientError(
                f"expected UNCHOKE, received {response.get('type')!r}"
            )

        self._log(
            "UNCHOKE_RECEIVED",
            f"remote={peer.ip}:{peer.port}",
        )

    def request_piece(self, peer: TrackerPeer, index: int) -> bytes:
        self._log(
            "PIECE_REQUEST_SENT",
            f"remote={peer.ip}:{peer.port} piece_index={index}",
        )

        response = self._exchange(
            peer,
            {
                "type": "REQUEST",
                "peer_id": self.local_peer_id.hex(),
                "info_hash": self.info_hash.hex(),
                "piece_index": index,
            },
        )

        if response.get("type") == "ERROR":
            raise PieceClientError(str(response.get("message", "peer error")))
        if response.get("type") != "PIECE":
            raise PieceClientError(
                f"expected PIECE, received {response.get('type')!r}"
            )
        if response.get("info_hash") != self.info_hash.hex():
            raise PieceClientError("PIECE info_hash does not match requested torrent")
        if response.get("piece_index") != index:
            raise PieceClientError("PIECE index does not match requested index")

        encoded_data = response.get("data")
        if not isinstance(encoded_data, str):
            raise PieceClientError("PIECE data must be a Base64 string")

        try:
            data = base64.b64decode(encoded_data, validate=True)
        except Exception as exc:
            raise PieceClientError("PIECE data is not valid Base64") from exc

        self._log(
            "PIECE_RECEIVED",
            f"remote={peer.ip}:{peer.port} piece_index={index} bytes={len(data)}",
        )

        try:
            self.piece_manager.write_piece(index, data)
        except PieceHashMismatch as exc:
            raise PieceClientError(str(exc)) from exc

        self.transfer_stats.add_downloaded(len(data))
        return data

    def download_from_peer(self, peer: TrackerPeer) -> PieceDownloadResult:
        bitfield = self.fetch_bitfield(peer)
        missing = self.piece_manager.missing_piece_indices()
        available_missing = [index for index in missing if bitfield[index]]
        skipped = len(missing) - len(available_missing)

        if available_missing:
            self.express_interest(peer)

        downloaded = 0
        bytes_downloaded = 0
        errors: list[str] = []

        for index in available_missing:
            try:
                data = self.request_piece(peer, index)
            except (PieceClientError, OSError, TimeoutError, PeerProtocolError) as exc:
                errors.append(f"piece {index}: {exc}")
                self._log(
                    "PIECE_DOWNLOAD_FAILED",
                    f"remote={peer.ip}:{peer.port} piece_index={index} error={exc}",
                )
                continue

            downloaded += 1
            bytes_downloaded += len(data)

        complete = self.piece_manager.is_complete()
        if complete:
            self._log(
                "DOWNLOAD_COMPLETED",
                (
                    f"remote={peer.ip}:{peer.port} "
                    f"bytes={self.piece_manager.metainfo.total_length}"
                ),
            )

        return PieceDownloadResult(
            requested=len(available_missing),
            downloaded=downloaded,
            skipped=skipped,
            bytes_downloaded=bytes_downloaded,
            complete=complete,
            errors=tuple(errors),
        )

    def _exchange(
        self,
        peer: TrackerPeer,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            with socket.create_connection(
                (peer.ip, peer.port),
                timeout=self.timeout,
            ) as sock:
                sock.settimeout(self.timeout)
                send_message(sock, message)
                return receive_message(sock)
        except OSError as exc:
            raise PieceClientError(
                f"could not communicate with {peer.ip}:{peer.port}: {exc}"
            ) from exc

    def _log(self, event_type: str, description: str) -> None:
        if self.logger is not None:
            self.logger.log(event_type, description)
