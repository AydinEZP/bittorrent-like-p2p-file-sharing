from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_from_bytes
from urllib.request import Request, urlopen

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

from common.bencode import BencodeError, bdecode
from common.event_logger import EventLogger
from peer.metainfo import Metainfo


class TrackerClientError(RuntimeError):
    """Raised when a Tracker request or response cannot be processed."""


class TrackerFailure(TrackerClientError):
    """Raised when the Tracker returns a Bencoded failure reason."""


@dataclass(frozen=True)
class TrackerPeer:
    peer_id: bytes
    ip: str
    port: int


@dataclass(frozen=True)
class TrackerResponse:
    interval: int
    complete: int
    incomplete: int
    peers: tuple[TrackerPeer, ...]


def generate_peer_id(prefix: str = "-DN0001-") -> bytes:
    """Generate an exact 20-byte peer ID."""
    try:
        prefix_bytes = prefix.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("peer ID prefix must contain ASCII characters") from exc

    if len(prefix_bytes) > 20:
        raise ValueError("peer ID prefix must not exceed 20 bytes")

    remaining = 20 - len(prefix_bytes)
    random_text = secrets.token_hex((remaining + 1) // 2)[:remaining]
    peer_id = prefix_bytes + random_text.encode("ascii")

    if len(peer_id) != 20:
        raise AssertionError("generated peer_id is not exactly 20 bytes")

    return peer_id


def _require_int(payload: dict[bytes, Any], key: bytes, *, minimum: int = 0) -> int:
    if key not in payload:
        raise TrackerClientError(
            f"Tracker response is missing {key.decode('ascii')!r}"
        )

    value = payload[key]

    if isinstance(value, bool) or not isinstance(value, int):
        raise TrackerClientError(
            f"Tracker response field {key.decode('ascii')!r} must be an integer"
        )

    if value < minimum:
        raise TrackerClientError(
            f"Tracker response field {key.decode('ascii')!r} must be at least {minimum}"
        )

    return value


def _decode_text(value: Any, field_name: str) -> str:
    if not isinstance(value, bytes):
        raise TrackerClientError(
            f"Tracker response field {field_name!r} must be bytes"
        )

    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TrackerClientError(
            f"Tracker response field {field_name!r} is not valid UTF-8"
        ) from exc


def parse_tracker_response(data: bytes) -> TrackerResponse:
    """Decode and validate a non-compact Tracker response."""
    try:
        payload = bdecode(data)
    except (BencodeError, TypeError) as exc:
        raise TrackerClientError(f"Invalid Bencoded Tracker response: {exc}") from exc

    if not isinstance(payload, dict):
        raise TrackerClientError("Tracker response root must be a dictionary")

    if b"failure reason" in payload:
        if len(payload) != 1:
            raise TrackerClientError(
                "Tracker failure response must not contain additional keys"
            )
        reason = _decode_text(payload[b"failure reason"], "failure reason")
        raise TrackerFailure(reason)

    interval = _require_int(payload, b"interval", minimum=1)
    complete = _require_int(payload, b"complete")
    incomplete = _require_int(payload, b"incomplete")

    raw_peers = payload.get(b"peers")
    if not isinstance(raw_peers, list):
        raise TrackerClientError("Tracker response field 'peers' must be a list")

    peers: list[TrackerPeer] = []
    for index, raw_peer in enumerate(raw_peers):
        if not isinstance(raw_peer, dict):
            raise TrackerClientError(
                f"Tracker peer entry at index {index} must be a dictionary"
            )

        peer_id = raw_peer.get(b"peer_id")
        raw_ip = raw_peer.get(b"ip")
        port = raw_peer.get(b"port")

        if not isinstance(peer_id, bytes) or len(peer_id) != 20:
            raise TrackerClientError(
                f"Tracker peer_id at index {index} must be exactly 20 bytes"
            )

        ip = _decode_text(raw_ip, f"peers[{index}].ip")

        if isinstance(port, bool) or not isinstance(port, int):
            raise TrackerClientError(
                f"Tracker peer port at index {index} must be an integer"
            )
        if not 1 <= port <= 65535:
            raise TrackerClientError(
                f"Tracker peer port at index {index} is outside 1..65535"
            )

        peers.append(TrackerPeer(peer_id=peer_id, ip=ip, port=port))

    return TrackerResponse(
        interval=interval,
        complete=complete,
        incomplete=incomplete,
        peers=tuple(peers),
    )


class TrackerClient:
    """Peer-side HTTP client for Tracker announce requests."""

    VALID_EVENTS = {None, "", "started", "completed", "stopped"}

    def __init__(
        self,
        *,
        announce_url: str,
        peer_id: bytes,
        port: int,
        timeout: float = 5.0,
        logger: EventLogger | None = None,
    ):
        if not isinstance(announce_url, str) or not announce_url.strip():
            raise ValueError("announce_url must be a non-empty string")
        if not isinstance(peer_id, bytes) or len(peer_id) != 20:
            raise ValueError("peer_id must be exactly 20 bytes")
        if isinstance(port, bool) or not isinstance(port, int):
            raise ValueError("port must be an integer")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        self.announce_url = announce_url
        self.peer_id = peer_id
        self.port = port
        self.timeout = float(timeout)
        self.logger = logger

    @classmethod
    def from_metainfo(
        cls,
        metainfo: Metainfo,
        *,
        peer_id: bytes,
        port: int,
        timeout: float = 5.0,
        logger: EventLogger | None = None,
    ) -> "TrackerClient":
        if metainfo.announce is None:
            raise ValueError("Metainfo must be loaded before creating a client")
        return cls(
            announce_url=metainfo.announce,
            peer_id=peer_id,
            port=port,
            timeout=timeout,
            logger=logger,
        )

    def build_announce_url(
        self,
        *,
        info_hash: bytes,
        uploaded: int,
        downloaded: int,
        left: int,
        event: str | None = None,
    ) -> str:
        self._validate_announce_arguments(
            info_hash=info_hash,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left,
            event=event,
        )

        fields = [
            ("info_hash", quote_from_bytes(info_hash, safe="")),
            ("peer_id", quote_from_bytes(self.peer_id, safe="")),
            ("port", str(self.port)),
            ("uploaded", str(uploaded)),
            ("downloaded", str(downloaded)),
            ("left", str(left)),
        ]
        if event is not None and event != "":
            fields.append(("event", event))

        separator = "&" if "?" in self.announce_url else "?"
        query = "&".join(f"{name}={value}" for name, value in fields)
        return self.announce_url + separator + query

    def announce(
        self,
        *,
        info_hash: bytes,
        uploaded: int,
        downloaded: int,
        left: int,
        event: str | None = None,
    ) -> TrackerResponse:
        url = self.build_announce_url(
            info_hash=info_hash,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left,
            event=event,
        )

        self._log(
            "TRACKER_REQUEST",
            (
                f"announce={self.announce_url} info_hash={info_hash.hex()} "
                f"peer_id={self.peer_id.hex()} port={self.port} "
                f"uploaded={uploaded} downloaded={downloaded} left={left} "
                f"event={event or '<empty>'}"
            ),
        )

        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "text/plain",
                "User-Agent": "DN-BitTorrent-Client/1.0",
                "Connection": "close",
            },
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = response.status
                content_type = response.headers.get_content_type()
                body = response.read()
        except HTTPError as exc:
            body = exc.read()
            self._log("TRACKER_HTTP_ERROR", f"status={exc.code} url={self.announce_url}")
            try:
                return parse_tracker_response(body)
            except TrackerFailure:
                raise
            except TrackerClientError as parse_exc:
                raise TrackerClientError(
                    f"Tracker HTTP error {exc.code}: {parse_exc}"
                ) from exc
        except URLError as exc:
            self._log(
                "TRACKER_CONNECTION_ERROR",
                f"url={self.announce_url} reason={exc.reason}",
            )
            raise TrackerClientError(
                f"Could not connect to Tracker: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            self._log(
                "TRACKER_CONNECTION_ERROR",
                f"url={self.announce_url} reason=timeout",
            )
            raise TrackerClientError("Tracker request timed out") from exc

        if status != 200:
            raise TrackerClientError(f"Unexpected Tracker HTTP status: {status}")
        if content_type != "text/plain":
            raise TrackerClientError(
                f"Unexpected Tracker content type: {content_type!r}"
            )

        try:
            parsed = parse_tracker_response(body)
        except TrackerFailure as exc:
            self._log("TRACKER_FAILURE", f"reason={exc}")
            raise
        except TrackerClientError as exc:
            self._log("TRACKER_RESPONSE_ERROR", f"reason={exc}")
            raise

        self._log(
            "TRACKER_RESPONSE",
            (
                f"interval={parsed.interval} complete={parsed.complete} "
                f"incomplete={parsed.incomplete} peer_count={len(parsed.peers)}"
            ),
        )
        return parsed

    @staticmethod
    def _validate_announce_arguments(
        *,
        info_hash: bytes,
        uploaded: int,
        downloaded: int,
        left: int,
        event: str | None,
    ) -> None:
        if not isinstance(info_hash, bytes) or len(info_hash) != 20:
            raise ValueError("info_hash must be exactly 20 bytes")

        for name, value in (
            ("uploaded", uploaded),
            ("downloaded", downloaded),
            ("left", left),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

        if event not in TrackerClient.VALID_EVENTS:
            raise ValueError(
                "event must be started, completed, stopped, empty, or None"
            )

    def _log(self, event_type: str, description: str) -> None:
        if self.logger is not None:
            self.logger.log(event_type, description)
