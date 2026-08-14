from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class TransferSnapshot:
    uploaded: int
    downloaded: int


class TransferStats:
    """Thread-safe byte counters for the current Peer announce session."""

    def __init__(self) -> None:
        self._uploaded = 0
        self._downloaded = 0
        self._lock = threading.Lock()

    @staticmethod
    def _validate_size(size: int) -> int:
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError("transfer size must be an integer")
        if size < 0:
            raise ValueError("transfer size must be non-negative")
        return size

    def add_uploaded(self, size: int) -> None:
        amount = self._validate_size(size)
        with self._lock:
            self._uploaded += amount

    def add_downloaded(self, size: int) -> None:
        amount = self._validate_size(size)
        with self._lock:
            self._downloaded += amount

    def snapshot(self) -> TransferSnapshot:
        with self._lock:
            return TransferSnapshot(
                uploaded=self._uploaded,
                downloaded=self._downloaded,
            )
