from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from common.bencode import bencode


class MetainfoError(ValueError):
    """Raised when a Metainfo file is missing required or valid fields."""


def _validate_path_component(value: Any, field_name: str) -> str:
    """Accept a relative path component and reject traversal/absolute paths."""
    if not isinstance(value, str) or not value.strip():
        raise MetainfoError(f"{field_name} must be a non-empty string")

    if value != value.strip():
        raise MetainfoError(f"{field_name} must not start or end with whitespace")

    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or Path(value).is_absolute()
    ):
        raise MetainfoError(
            f"{field_name} must be a safe relative path component"
        )

    return value


class Metainfo:
    """
    Parse and validate the project's human-readable JSON Metainfo format.

    ``pieces`` may be either a list of 40-character SHA-1 hexadecimal values
    or one concatenated hexadecimal string whose length is a multiple of 40.
    The info hash is calculated from the exact JSON ``info`` representation
    after Bencoding; therefore it is stable inside this project, but is not
    intended to match a standard binary .torrent file byte-for-byte.
    """

    REQUIRED_INFO_KEYS = {"name", "piece length", "pieces"}

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.raw: dict[str, Any] | None = None
        self.announce: str | None = None
        self.announce_urls: tuple[str, ...] = ()
        self.info: dict[str, Any] | None = None
        self.name: str | None = None
        self.piece_length: int | None = None
        self.piece_hashes: list[str] = []
        self.total_length: int = 0
        self.piece_count: int = 0
        self.info_hash: bytes | None = None

    def load(self) -> "Metainfo":
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError as exc:
            raise MetainfoError(f"Metainfo file not found: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise MetainfoError(
                f"Invalid JSON in {self.path}: line {exc.lineno}, column {exc.colno}"
            ) from exc
        except OSError as exc:
            raise MetainfoError(f"Could not read {self.path}: {exc}") from exc

        if not isinstance(data, dict):
            raise MetainfoError("Metainfo root must be a JSON object")

        self.raw = data
        self.validate()
        self.calculate_info_hash()
        return self

    def validate(self) -> None:
        if self.raw is None:
            raise MetainfoError("Metainfo data has not been loaded")

        announce = self.raw.get("announce")
        info = self.raw.get("info")

        if isinstance(announce, str) and announce.strip():
            announce_urls = (announce.strip(),)
        elif (
            isinstance(announce, list)
            and announce
            and all(isinstance(url, str) and url.strip() for url in announce)
        ):
            announce_urls = tuple(url.strip() for url in announce)
        else:
            raise MetainfoError(
                "'announce' must be a non-empty string or list of strings"
            )

        if not isinstance(info, dict):
            raise MetainfoError("'info' must be an object")

        missing = self.REQUIRED_INFO_KEYS - set(info)
        if missing:
            raise MetainfoError(
                "Missing info field(s): " + ", ".join(sorted(missing))
            )

        name = _validate_path_component(info["name"], "'info.name'")
        piece_length = info["piece length"]

        if (
            isinstance(piece_length, bool)
            or not isinstance(piece_length, int)
            or piece_length <= 0
        ):
            raise MetainfoError("'info.piece length' must be a positive integer")

        has_single_length = "length" in info
        has_multiple_files = "files" in info

        if has_single_length == has_multiple_files:
            raise MetainfoError(
                "'info' must contain exactly one of 'length' or 'files'"
            )

        if has_single_length:
            total_length = self._validate_single_file(info["length"])
        else:
            total_length = self._validate_multiple_files(info["files"])

        expected_piece_count = (
            0
            if total_length == 0
            else (total_length + piece_length - 1) // piece_length
        )

        pieces = self._normalize_pieces(info["pieces"])

        if len(pieces) != expected_piece_count:
            raise MetainfoError(
                "Piece count mismatch: "
                f"expected {expected_piece_count}, found {len(pieces)}"
            )

        normalized_hashes: list[str] = []
        for index, piece_hash in enumerate(pieces):
            if not isinstance(piece_hash, str):
                raise MetainfoError(
                    f"Piece hash at index {index} must be a string"
                )

            normalized = piece_hash.lower()

            if len(normalized) != 40:
                raise MetainfoError(
                    f"Piece hash at index {index} must contain 40 hex characters"
                )

            try:
                bytes.fromhex(normalized)
            except ValueError as exc:
                raise MetainfoError(
                    f"Piece hash at index {index} is not valid hexadecimal"
                ) from exc

            normalized_hashes.append(normalized)

        self.announce = announce_urls[0]
        self.announce_urls = announce_urls
        self.info = info
        self.name = name
        self.piece_length = piece_length
        self.piece_hashes = normalized_hashes
        self.total_length = total_length
        self.piece_count = expected_piece_count

    @staticmethod
    def _normalize_pieces(pieces: Any) -> list[Any]:
        if isinstance(pieces, list):
            return pieces

        if isinstance(pieces, str):
            if len(pieces) % 40 != 0:
                raise MetainfoError(
                    "'info.pieces' concatenated string length must be a multiple of 40"
                )
            return [
                pieces[offset:offset + 40]
                for offset in range(0, len(pieces), 40)
            ]

        raise MetainfoError(
            "'info.pieces' must be a list or a concatenated hexadecimal string"
        )

    @staticmethod
    def _validate_single_file(length: Any) -> int:
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or length < 0
        ):
            raise MetainfoError("'info.length' must be a non-negative integer")
        return length

    @staticmethod
    def _validate_multiple_files(files: Any) -> int:
        if not isinstance(files, list) or not files:
            raise MetainfoError("'info.files' must be a non-empty list")

        total_length = 0
        seen_paths: set[tuple[str, ...]] = set()

        for index, entry in enumerate(files):
            if not isinstance(entry, dict):
                raise MetainfoError(
                    f"File entry at index {index} must be an object"
                )

            length = entry.get("length")
            path_parts = entry.get("path")

            if (
                isinstance(length, bool)
                or not isinstance(length, int)
                or length < 0
            ):
                raise MetainfoError(
                    f"File length at index {index} must be a non-negative integer"
                )

            if not isinstance(path_parts, list) or not path_parts:
                raise MetainfoError(
                    f"File path at index {index} must be a non-empty list of strings"
                )

            safe_parts = tuple(
                _validate_path_component(
                    part,
                    f"File path component at index {index}",
                )
                for part in path_parts
            )

            if safe_parts in seen_paths:
                raise MetainfoError(f"Duplicate file path: {path_parts!r}")

            seen_paths.add(safe_parts)
            total_length += length

        return total_length

    def calculate_info_hash(self) -> bytes:
        if self.info is None:
            raise MetainfoError("Metainfo has not been validated")

        self.info_hash = hashlib.sha1(bencode(self.info)).digest()
        return self.info_hash

    @property
    def info_hash_hex(self) -> str:
        if self.info_hash is None:
            raise MetainfoError("Info hash has not been calculated")
        return self.info_hash.hex()

    @property
    def is_multi_file(self) -> bool:
        if self.info is None:
            raise MetainfoError("Metainfo has not been loaded")
        return "files" in self.info

    def summary(self) -> dict[str, Any]:
        return {
            "announce": self.announce,
            "name": self.name,
            "mode": "multi-file" if self.is_multi_file else "single-file",
            "total_length": self.total_length,
            "piece_length": self.piece_length,
            "piece_count": self.piece_count,
            "info_hash": self.info_hash_hex,
        }
