from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import threading
from typing import Any

from common.event_logger import EventLogger
from peer.metainfo import Metainfo


class PieceManagerError(RuntimeError):
    """Base error for piece storage and verification operations."""


class PieceUnavailable(PieceManagerError):
    """Raised when a requested piece is not available locally."""


class PieceHashMismatch(PieceManagerError):
    """Raised when received piece bytes do not match the Metainfo SHA-1."""


@dataclass(frozen=True)
class FileSegment:
    relative_path: Path
    length: int
    start: int
    end: int


class PieceManager:
    """
    Manage BitTorrent pieces over a single file or a virtual multi-file stream.

    For a multi-file torrent, files are stored under::

        <storage_root>/<torrent name>/<path components>

    For a single-file torrent, the file is stored under::

        <storage_root>/<file name>
    """

    def __init__(
        self,
        metainfo: Metainfo,
        storage_root: str | Path,
        *,
        create_missing: bool = False,
        logger: EventLogger | None = None,
    ):
        if metainfo.info is None or metainfo.info_hash is None:
            raise ValueError("Metainfo must be loaded before creating PieceManager")

        self.metainfo = metainfo
        self.storage_root = Path(storage_root).resolve()
        self.create_missing = bool(create_missing)
        self.logger = logger
        self._lock = threading.RLock()

        self._segments = self._build_segments()
        self._bitfield = [False] * self.metainfo.piece_count

        self.storage_root.mkdir(parents=True, exist_ok=True)

        if self.create_missing:
            self._prepare_storage()

        self.scan_existing()

    def _build_segments(self) -> tuple[FileSegment, ...]:
        assert self.metainfo.info is not None
        offset = 0
        segments: list[FileSegment] = []

        if "files" not in self.metainfo.info:
            length = int(self.metainfo.info["length"])
            relative_path = Path(str(self.metainfo.info["name"]))
            segments.append(
                FileSegment(
                    relative_path=relative_path,
                    length=length,
                    start=0,
                    end=length,
                )
            )
            return tuple(segments)

        torrent_directory = Path(str(self.metainfo.info["name"]))

        for entry in self.metainfo.info["files"]:
            path_parts = [str(part) for part in entry["path"]]
            length = int(entry["length"])
            relative_path = torrent_directory.joinpath(*path_parts)
            segments.append(
                FileSegment(
                    relative_path=relative_path,
                    length=length,
                    start=offset,
                    end=offset + length,
                )
            )
            offset += length

        return tuple(segments)

    @property
    def segments(self) -> tuple[FileSegment, ...]:
        return self._segments

    @property
    def bitfield(self) -> tuple[bool, ...]:
        with self._lock:
            return tuple(self._bitfield)

    @property
    def downloaded_bytes(self) -> int:
        with self._lock:
            return sum(
                self.piece_size(index)
                for index, available in enumerate(self._bitfield)
                if available
            )

    @property
    def left(self) -> int:
        return self.metainfo.total_length - self.downloaded_bytes

    def is_complete(self) -> bool:
        with self._lock:
            return all(self._bitfield)

    def has_piece(self, index: int) -> bool:
        self._validate_piece_index(index)
        with self._lock:
            return self._bitfield[index]

    def missing_piece_indices(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                index
                for index, available in enumerate(self._bitfield)
                if not available
            )

    def piece_size(self, index: int) -> int:
        self._validate_piece_index(index)

        if index < self.metainfo.piece_count - 1:
            assert self.metainfo.piece_length is not None
            return self.metainfo.piece_length

        assert self.metainfo.piece_length is not None
        consumed = self.metainfo.piece_length * (self.metainfo.piece_count - 1)
        return self.metainfo.total_length - consumed

    def expected_hash(self, index: int) -> str:
        self._validate_piece_index(index)
        return self.metainfo.piece_hashes[index]

    def scan_existing(self) -> tuple[bool, ...]:
        """Rebuild the bitfield by hashing all readable local pieces."""
        with self._lock:
            for index in range(self.metainfo.piece_count):
                try:
                    data = self._read_piece_bytes(index)
                except (FileNotFoundError, OSError, PieceUnavailable):
                    self._bitfield[index] = False
                    continue

                self._bitfield[index] = self.verify_piece(index, data)

            self._log(
                "BITFIELD_SCANNED",
                (
                    f"info_hash={self.metainfo.info_hash_hex} "
                    f"available={sum(self._bitfield)} "
                    f"total={self.metainfo.piece_count}"
                ),
            )
            return tuple(self._bitfield)

    def verify_piece(self, index: int, data: bytes) -> bool:
        self._validate_piece_index(index)
        if not isinstance(data, bytes):
            raise TypeError("piece data must be bytes")

        if len(data) != self.piece_size(index):
            return False

        return hashlib.sha1(data).hexdigest() == self.expected_hash(index)

    def read_piece(self, index: int) -> bytes:
        self._validate_piece_index(index)

        with self._lock:
            if not self._bitfield[index]:
                raise PieceUnavailable(f"piece {index} is not available")

            data = self._read_piece_bytes(index)

            if not self.verify_piece(index, data):
                self._bitfield[index] = False
                self._log(
                    "HASH_INVALID",
                    f"piece_index={index} source=local_storage",
                )
                raise PieceHashMismatch(
                    f"local piece {index} no longer matches its SHA-1"
                )

            self._log(
                "PIECE_READ",
                f"piece_index={index} bytes={len(data)}",
            )
            return data

    def write_piece(self, index: int, data: bytes) -> None:
        self._validate_piece_index(index)

        if not isinstance(data, bytes):
            raise TypeError("piece data must be bytes")

        expected_size = self.piece_size(index)
        if len(data) != expected_size:
            self._log(
                "HASH_INVALID",
                (
                    f"piece_index={index} reason=wrong_length "
                    f"expected={expected_size} actual={len(data)}"
                ),
            )
            raise PieceHashMismatch(
                f"piece {index} has length {len(data)}; expected {expected_size}"
            )

        actual_hash = hashlib.sha1(data).hexdigest()
        expected_hash = self.expected_hash(index)

        if actual_hash != expected_hash:
            self._log(
                "HASH_INVALID",
                (
                    f"piece_index={index} expected={expected_hash} "
                    f"actual={actual_hash}"
                ),
            )
            raise PieceHashMismatch(
                f"piece {index} SHA-1 mismatch: expected {expected_hash}, "
                f"got {actual_hash}"
            )

        with self._lock:
            self._write_virtual(
                index * int(self.metainfo.piece_length),
                data,
            )
            self._bitfield[index] = True

            self._log(
                "HASH_VALID",
                f"piece_index={index} sha1={actual_hash}",
            )
            self._log(
                "PIECE_STORED",
                (
                    f"piece_index={index} bytes={len(data)} "
                    f"left={self.left}"
                ),
            )

            if all(self._bitfield):
                self._log(
                    "DOWNLOAD_COMPLETED",
                    (
                        f"info_hash={self.metainfo.info_hash_hex} "
                        f"bytes={self.metainfo.total_length}"
                    ),
                )

    def _storage_path(self, relative_path: Path) -> Path:
        if relative_path.is_absolute():
            raise PieceManagerError("Metainfo path must be relative")

        candidate = (self.storage_root / relative_path).resolve()
        if not candidate.is_relative_to(self.storage_root):
            raise PieceManagerError(
                f"Metainfo path escapes storage root: {relative_path}"
            )
        return candidate

    def _prepare_storage(self) -> None:
        for segment in self._segments:
            path = self._storage_path(segment.relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            if not path.exists():
                with path.open("wb") as file:
                    file.truncate(segment.length)
                continue

            current_size = path.stat().st_size
            if current_size < segment.length:
                with path.open("r+b") as file:
                    file.truncate(segment.length)

    def _read_piece_bytes(self, index: int) -> bytes:
        assert self.metainfo.piece_length is not None
        start = index * self.metainfo.piece_length
        size = self.piece_size(index)
        data = self._read_virtual(start, size)

        if len(data) != size:
            raise PieceUnavailable(
                f"piece {index} is incomplete: expected {size}, got {len(data)}"
            )

        return data

    def _read_virtual(self, start: int, size: int) -> bytes:
        end = start + size
        output = bytearray()

        for segment in self._segments:
            if segment.length == 0:
                continue
            if segment.end <= start or segment.start >= end:
                continue

            overlap_start = max(start, segment.start)
            overlap_end = min(end, segment.end)
            local_offset = overlap_start - segment.start
            read_size = overlap_end - overlap_start
            path = self._storage_path(segment.relative_path)

            if not path.exists():
                raise FileNotFoundError(path)

            with path.open("rb") as file:
                file.seek(local_offset)
                chunk = file.read(read_size)

            if len(chunk) != read_size:
                raise PieceUnavailable(
                    f"file {path} is shorter than declared Metainfo length"
                )

            output.extend(chunk)

        return bytes(output)

    def _write_virtual(self, start: int, data: bytes) -> None:
        end = start + len(data)
        consumed = 0

        for segment in self._segments:
            if segment.length == 0:
                continue
            if segment.end <= start or segment.start >= end:
                continue

            overlap_start = max(start, segment.start)
            overlap_end = min(end, segment.end)
            local_offset = overlap_start - segment.start
            chunk_size = overlap_end - overlap_start
            chunk = data[consumed:consumed + chunk_size]
            path = self._storage_path(segment.relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            mode = "r+b" if path.exists() else "w+b"
            with path.open(mode) as file:
                if path.stat().st_size < segment.length:
                    file.truncate(segment.length)
                file.seek(local_offset)
                file.write(chunk)
                file.flush()

            consumed += chunk_size

        if consumed != len(data):
            raise PieceManagerError(
                f"virtual write consumed {consumed} of {len(data)} bytes"
            )

    def _validate_piece_index(self, index: Any) -> None:
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("piece index must be an integer")
        if not 0 <= index < self.metainfo.piece_count:
            raise ValueError(
                f"piece index {index} is outside 0..{self.metainfo.piece_count - 1}"
            )

    def _log(self, event_type: str, description: str) -> None:
        if self.logger is not None:
            self.logger.log(event_type, description)
