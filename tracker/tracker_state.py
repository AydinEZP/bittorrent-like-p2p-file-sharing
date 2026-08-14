from __future__ import annotations

from copy import deepcopy
import ipaddress
import threading
import time
from typing import Any

from common.event_logger import EventLogger


class TrackerStateError(ValueError):
    """Raised when invalid swarm or peer data is supplied."""


class TrackerState:
    """
    Thread-safe in-memory storage for BitTorrent swarms.

    Internal structure:

        {
            info_hash_bytes: {
                peer_id_bytes: {
                    "peer_id": bytes,
                    "ip": str,
                    "port": int,
                    "uploaded": int,
                    "downloaded": int,
                    "left": int,
                    "last_seen": float,
                }
            }
        }
    """

    VALID_EVENTS = {"", "started", "completed", "stopped"}

    def __init__(self, logger: EventLogger | None = None):
        self._swarms: dict[bytes, dict[bytes, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._logger = logger

    @staticmethod
    def _normalize_info_hash(info_hash: bytes | bytearray | str) -> bytes:
        if isinstance(info_hash, str):
            if len(info_hash) != 40:
                raise TrackerStateError(
                    "info_hash string must contain 40 hexadecimal characters"
                )
            try:
                normalized = bytes.fromhex(info_hash)
            except ValueError as exc:
                raise TrackerStateError(
                    "info_hash string is not valid hexadecimal"
                ) from exc
        elif isinstance(info_hash, (bytes, bytearray)):
            normalized = bytes(info_hash)
        else:
            raise TrackerStateError("info_hash must be bytes or a hex string")

        if len(normalized) != 20:
            raise TrackerStateError("info_hash must be exactly 20 bytes")

        return normalized

    @staticmethod
    def _normalize_peer_id(peer_id: bytes | bytearray | str) -> bytes:
        if isinstance(peer_id, str):
            try:
                normalized = peer_id.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise TrackerStateError(
                    "peer_id could not be encoded as UTF-8"
                ) from exc
        elif isinstance(peer_id, (bytes, bytearray)):
            normalized = bytes(peer_id)
        else:
            raise TrackerStateError("peer_id must be bytes or a string")

        if len(normalized) != 20:
            raise TrackerStateError("peer_id must be exactly 20 bytes")

        return normalized

    @staticmethod
    def _validate_ipv4(ip: str) -> str:
        if not isinstance(ip, str) or not ip:
            raise TrackerStateError("ip must be a non-empty IPv4 string")

        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError as exc:
            raise TrackerStateError(f"invalid IP address: {ip!r}") from exc

        if parsed.version != 4:
            raise TrackerStateError("only IPv4 addresses are supported")

        return str(parsed)

    @staticmethod
    def _validate_integer(
        name: str,
        value: Any,
        *,
        minimum: int,
        maximum: int | None = None,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TrackerStateError(f"{name} must be an integer")

        if value < minimum:
            raise TrackerStateError(f"{name} must be at least {minimum}")

        if maximum is not None and value > maximum:
            raise TrackerStateError(
                f"{name} must not exceed {maximum}"
            )

        return value

    @classmethod
    def _validate_event(cls, event: str | None) -> str:
        normalized = "" if event is None else event

        if not isinstance(normalized, str):
            raise TrackerStateError("event must be a string or None")

        if normalized not in cls.VALID_EVENTS:
            allowed = ", ".join(sorted(value or "<empty>" for value in cls.VALID_EVENTS))
            raise TrackerStateError(
                f"invalid event {normalized!r}; expected one of: {allowed}"
            )

        return normalized

    @staticmethod
    def _validate_now(now: float | int | None) -> float:
        timestamp = time.time() if now is None else float(now)

        if timestamp < 0:
            raise TrackerStateError("timestamp must be non-negative")

        return timestamp

    def announce(
        self,
        *,
        info_hash: bytes | bytearray | str,
        peer_id: bytes | bytearray | str,
        ip: str,
        port: int,
        uploaded: int,
        downloaded: int,
        left: int,
        event: str | None = None,
        now: float | int | None = None,
    ) -> dict[str, Any]:
        """
        Register, refresh, complete, or stop a peer.

        Returns a snapshot containing complete/incomplete counts and a peer
        list that excludes the requesting peer.
        """
        normalized_hash = self._normalize_info_hash(info_hash)
        normalized_peer_id = self._normalize_peer_id(peer_id)
        normalized_ip = self._validate_ipv4(ip)
        normalized_port = self._validate_integer(
            "port", port, minimum=1, maximum=65535
        )
        normalized_uploaded = self._validate_integer(
            "uploaded", uploaded, minimum=0
        )
        normalized_downloaded = self._validate_integer(
            "downloaded", downloaded, minimum=0
        )
        normalized_left = self._validate_integer(
            "left", left, minimum=0
        )
        normalized_event = self._validate_event(event)
        timestamp = self._validate_now(now)

        if normalized_event == "completed":
            normalized_left = 0

        with self._lock:
            swarm = self._swarms.get(normalized_hash)

            if normalized_event == "stopped":
                removed = False

                if swarm is not None:
                    removed = swarm.pop(normalized_peer_id, None) is not None

                    if not swarm:
                        self._swarms.pop(normalized_hash, None)

                self._log(
                    "PEER_STOPPED",
                    (
                        f"info_hash={normalized_hash.hex()} "
                        f"peer_id={normalized_peer_id.hex()} "
                        f"removed={removed}"
                    ),
                )

                return self._build_response_locked(
                    normalized_hash,
                    requesting_peer_id=normalized_peer_id,
                )

            is_new = swarm is None or normalized_peer_id not in swarm
            if is_new and normalized_event != "started":
                raise TrackerStateError(
                    "first announce for a peer must use event=started"
                )

            if swarm is None:
                swarm = {}
                self._swarms[normalized_hash] = swarm

            swarm[normalized_peer_id] = {
                "peer_id": normalized_peer_id,
                "ip": normalized_ip,
                "port": normalized_port,
                "uploaded": normalized_uploaded,
                "downloaded": normalized_downloaded,
                "left": normalized_left,
                "last_seen": timestamp,
            }

            if normalized_event == "completed":
                log_event = "PEER_COMPLETED"
            elif is_new:
                log_event = "PEER_REGISTERED"
            else:
                log_event = "PEER_UPDATED"

            self._log(
                log_event,
                (
                    f"info_hash={normalized_hash.hex()} "
                    f"peer_id={normalized_peer_id.hex()} "
                    f"ip={normalized_ip} port={normalized_port} "
                    f"uploaded={normalized_uploaded} "
                    f"downloaded={normalized_downloaded} "
                    f"left={normalized_left}"
                ),
            )

            return self._build_response_locked(
                normalized_hash,
                requesting_peer_id=normalized_peer_id,
            )

    def get_peers(
        self,
        info_hash: bytes | bytearray | str,
        requesting_peer_id: bytes | bytearray | str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_hash = self._normalize_info_hash(info_hash)
        requester = (
            None
            if requesting_peer_id is None
            else self._normalize_peer_id(requesting_peer_id)
        )

        with self._lock:
            return self._get_peers_locked(normalized_hash, requester)

    def get_statistics(
        self,
        info_hash: bytes | bytearray | str,
    ) -> dict[str, int]:
        normalized_hash = self._normalize_info_hash(info_hash)

        with self._lock:
            return self._get_statistics_locked(normalized_hash)

    def remove_peer(
        self,
        info_hash: bytes | bytearray | str,
        peer_id: bytes | bytearray | str,
    ) -> bool:
        normalized_hash = self._normalize_info_hash(info_hash)
        normalized_peer_id = self._normalize_peer_id(peer_id)

        with self._lock:
            swarm = self._swarms.get(normalized_hash)
            if swarm is None:
                return False

            removed = swarm.pop(normalized_peer_id, None) is not None

            if removed:
                self._log(
                    "PEER_REMOVED",
                    (
                        f"info_hash={normalized_hash.hex()} "
                        f"peer_id={normalized_peer_id.hex()}"
                    ),
                )

            if not swarm:
                self._swarms.pop(normalized_hash, None)

            return removed

    def remove_expired_peers(
        self,
        timeout: float,
        *,
        now: float | int | None = None,
    ) -> list[dict[str, Any]]:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TrackerStateError("timeout must be a number")
        if timeout <= 0:
            raise TrackerStateError("timeout must be positive")

        timestamp = self._validate_now(now)
        expired: list[dict[str, Any]] = []

        with self._lock:
            for info_hash, swarm in list(self._swarms.items()):
                for peer_id, peer in list(swarm.items()):
                    if timestamp - peer["last_seen"] > timeout:
                        expired.append(
                            {
                                "info_hash": info_hash,
                                "peer_id": peer_id,
                                "ip": peer["ip"],
                                "port": peer["port"],
                                "last_seen": peer["last_seen"],
                            }
                        )
                        del swarm[peer_id]

                        self._log(
                            "PEER_EXPIRED",
                            (
                                f"info_hash={info_hash.hex()} "
                                f"peer_id={peer_id.hex()} "
                                f"last_seen={peer['last_seen']}"
                            ),
                        )

                if not swarm:
                    del self._swarms[info_hash]

        return expired

    def peer_count(
        self,
        info_hash: bytes | bytearray | str | None = None,
    ) -> int:
        with self._lock:
            if info_hash is None:
                return sum(len(swarm) for swarm in self._swarms.values())

            normalized_hash = self._normalize_info_hash(info_hash)
            return len(self._swarms.get(normalized_hash, {}))

    def swarm_count(self) -> int:
        with self._lock:
            return len(self._swarms)

    def snapshot(self) -> dict[bytes, dict[bytes, dict[str, Any]]]:
        """Return a deep copy for diagnostics and tests."""
        with self._lock:
            return deepcopy(self._swarms)

    def _build_response_locked(
        self,
        info_hash: bytes,
        requesting_peer_id: bytes | None,
    ) -> dict[str, Any]:
        statistics = self._get_statistics_locked(info_hash)

        return {
            "complete": statistics["complete"],
            "incomplete": statistics["incomplete"],
            "peers": self._get_peers_locked(
                info_hash,
                requesting_peer_id,
            ),
        }

    def _get_statistics_locked(self, info_hash: bytes) -> dict[str, int]:
        swarm = self._swarms.get(info_hash, {})

        complete = sum(1 for peer in swarm.values() if peer["left"] == 0)
        incomplete = len(swarm) - complete

        return {
            "complete": complete,
            "incomplete": incomplete,
        }

    def _get_peers_locked(
        self,
        info_hash: bytes,
        requesting_peer_id: bytes | None,
    ) -> list[dict[str, Any]]:
        swarm = self._swarms.get(info_hash, {})

        peers = [
            {
                "peer_id": peer["peer_id"],
                "ip": peer["ip"],
                "port": peer["port"],
            }
            for peer_id, peer in swarm.items()
            if peer_id != requesting_peer_id
        ]

        peers.sort(key=lambda peer: peer["peer_id"])
        return peers

    def _log(self, event_type: str, description: str) -> None:
        if self._logger is not None:
            self._logger.log(event_type, description)
