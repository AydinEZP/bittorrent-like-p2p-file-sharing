from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading
from typing import Any


class EventLoggerError(RuntimeError):
    """Raised when a log file cannot be created, parsed, or updated safely."""


class EventLogger:
    """
    Thread-safe event logger shared by Tracker and Peer components.

    File layout:

        File Name: <name>
        Creation Date/Time: <timestamp>
        Last Modified Date/Time: <timestamp>

        Event Type | Date/Time | Description
        <event> | <timestamp> | <description>
    """

    _locks_guard = threading.Lock()
    _locks: dict[Path, threading.RLock] = {}

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self._get_lock(self.path)

        with self._lock:
            if not self.path.exists():
                self._initialize_file()
            else:
                self._validate_existing_file()

    @classmethod
    def _get_lock(cls, path: Path) -> threading.RLock:
        with cls._locks_guard:
            lock = cls._locks.get(path)
            if lock is None:
                lock = threading.RLock()
                cls._locks[path] = lock
            return lock

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _sanitize_field(value: Any) -> str:
        text = str(value)
        text = text.replace("\r", " ").replace("\n", " ")
        text = text.replace(" | ", " / ")
        return " ".join(text.split())

    def _initial_lines(self, timestamp: str) -> list[str]:
        return [
            f"File Name: {self.path.name}",
            f"Creation Date/Time: {timestamp}",
            f"Last Modified Date/Time: {timestamp}",
            "",
            "Event Type | Date/Time | Description",
        ]

    def _initialize_file(self) -> None:
        timestamp = self._timestamp()
        self._atomic_write(self._initial_lines(timestamp))

    def _validate_existing_file(self) -> None:
        lines = self._read_lines()

        if len(lines) < 5:
            raise EventLoggerError(
                f"Existing log file has an invalid header: {self.path}"
            )

        expected_prefixes = (
            "File Name: ",
            "Creation Date/Time: ",
            "Last Modified Date/Time: ",
        )

        for index, prefix in enumerate(expected_prefixes):
            if not lines[index].startswith(prefix):
                raise EventLoggerError(
                    f"Invalid log header line {index + 1} in {self.path}"
                )

        if lines[4] != "Event Type | Date/Time | Description":
            raise EventLoggerError(
                f"Invalid event column header in {self.path}"
            )

    def _read_lines(self) -> list[str]:
        try:
            return self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise EventLoggerError(
                f"Could not read log file {self.path}: {exc}"
            ) from exc

    def _atomic_write(self, lines: list[str]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        content = "\n".join(lines) + "\n"

        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            temporary.replace(self.path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise EventLoggerError(
                f"Could not write log file {self.path}: {exc}"
            ) from exc

    def log(self, event_type: str, description: Any) -> None:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")

        event = self._sanitize_field(event_type).upper().replace(" ", "_")
        details = self._sanitize_field(description)
        timestamp = self._timestamp()

        with self._lock:
            lines = self._read_lines()
            self._validate_lines_for_update(lines)

            lines[2] = f"Last Modified Date/Time: {timestamp}"
            lines.append(f"{event} | {timestamp} | {details}")
            self._atomic_write(lines)

    def _validate_lines_for_update(self, lines: list[str]) -> None:
        if len(lines) < 5:
            raise EventLoggerError(
                f"Log file became invalid before update: {self.path}"
            )
        if not lines[1].startswith("Creation Date/Time: "):
            raise EventLoggerError(
                f"Creation timestamp is missing in {self.path}"
            )
        if lines[4] != "Event Type | Date/Time | Description":
            raise EventLoggerError(
                f"Event header is missing in {self.path}"
            )

    def read_header(self) -> dict[str, str]:
        with self._lock:
            lines = self._read_lines()
            self._validate_lines_for_update(lines)

            return {
                "file_name": lines[0].split(": ", 1)[1],
                "creation_time": lines[1].split(": ", 1)[1],
                "last_modified_time": lines[2].split(": ", 1)[1],
            }

    def read_events(self) -> list[dict[str, str]]:
        with self._lock:
            lines = self._read_lines()
            self._validate_lines_for_update(lines)

            events: list[dict[str, str]] = []

            for line_number, line in enumerate(lines[5:], start=6):
                if not line.strip():
                    continue

                parts = line.split(" | ", 2)
                if len(parts) != 3:
                    raise EventLoggerError(
                        f"Malformed event line {line_number} in {self.path}"
                    )

                event_type, timestamp, description = parts
                events.append(
                    {
                        "event_type": event_type,
                        "timestamp": timestamp,
                        "description": description,
                    }
                )

            return events
