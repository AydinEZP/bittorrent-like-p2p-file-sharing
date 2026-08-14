from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import shutil

from common.event_logger import EventLogger


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test_output" / "logs"


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def write_events(logger: EventLogger, worker_id: int, count: int) -> None:
    for event_index in range(count):
        logger.log(
            "worker_event",
            f"worker={worker_id}, event={event_index}",
        )


def main() -> None:
    print("=== Logger tests ===")

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    tracker_path = OUTPUT / "tracker.log"
    peer_path = OUTPUT / "peer.log"

    tracker_logger = EventLogger(tracker_path)
    peer_logger = EventLogger(peer_path)

    initial_header = tracker_logger.read_header()
    tracker_logger.log(
        "tracker_started",
        "Listening on 127.0.0.1:8000",
    )
    peer_logger.log(
        "peer_started",
        "Listening on 127.0.0.1:6881",
    )

    worker_count = 8
    events_per_worker = 25

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                write_events,
                tracker_logger,
                worker_id,
                events_per_worker,
            )
            for worker_id in range(worker_count)
        ]

        for future in futures:
            future.result()

    tracker_logger.log(
        "multiline_event",
        "first line\nsecond line | safe description",
    )

    reopened_logger = EventLogger(tracker_path)
    reopened_logger.log(
        "tracker_reopened",
        "Existing log file reopened successfully",
    )

    final_header = reopened_logger.read_header()
    tracker_events = reopened_logger.read_events()
    peer_events = peer_logger.read_events()

    expected_tracker_events = 1 + (worker_count * events_per_worker) + 1 + 1

    assert len(tracker_events) == expected_tracker_events
    assert len(peer_events) == 1

    assert initial_header["file_name"] == "tracker.log"
    assert final_header["file_name"] == "tracker.log"
    assert (
        initial_header["creation_time"]
        == final_header["creation_time"]
    )

    creation_time = parse_timestamp(final_header["creation_time"])
    modified_time = parse_timestamp(final_header["last_modified_time"])
    assert modified_time >= creation_time

    worker_events = [
        event
        for event in tracker_events
        if event["event_type"] == "WORKER_EVENT"
    ]
    assert len(worker_events) == worker_count * events_per_worker

    multiline_event = next(
        event
        for event in tracker_events
        if event["event_type"] == "MULTILINE_EVENT"
    )
    assert "\n" not in multiline_event["description"]
    assert " | " not in multiline_event["description"]

    raw_text = tracker_path.read_text(encoding="utf-8")
    assert raw_text.startswith(
        "File Name: tracker.log\n"
        "Creation Date/Time: "
    )
    assert "\n\nEvent Type | Date/Time | Description\n" in raw_text

    print(f"Tracker log path: {tracker_path}")
    print(f"Peer log path: {peer_path}")
    print(f"Tracker event count: {len(tracker_events)}")
    print(f"Peer event count: {len(peer_events)}")
    print("Required header fields: PASS")
    print("Thread-safe concurrent writes: PASS")
    print("Creation timestamp preservation: PASS")
    print("Last-modified timestamp update: PASS")
    print("Log field sanitization: PASS")
    print("LOGGER TESTS RESULT: PASS")


def test_logger_component() -> None:
    main()

if __name__ == "__main__":
    main()
