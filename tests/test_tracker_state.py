from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil

from common.event_logger import EventLogger
from tracker.tracker_state import TrackerState, TrackerStateError


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test_output" / "tracker_state"


def make_peer_id(index: int) -> bytes:
    return f"P{index:019d}".encode("ascii")


def make_info_hash(label: str) -> bytes:
    return hashlib.sha1(label.encode("utf-8")).digest()


def assert_invalid(callable_object, expected_message: str) -> None:
    try:
        callable_object()
    except TrackerStateError as exc:
        assert expected_message in str(exc), (
            f"Expected {expected_message!r}, got {str(exc)!r}"
        )
        return

    raise AssertionError("Expected TrackerStateError")


def register_concurrent_peer(
    state: TrackerState,
    info_hash: bytes,
    index: int,
) -> None:
    state.announce(
        info_hash=info_hash,
        peer_id=make_peer_id(index),
        ip="127.0.0.1",
        port=10000 + index,
        uploaded=index * 10,
        downloaded=index * 20,
        left=0 if index % 3 == 0 else 500,
        event="started",
        now=1000 + index,
    )


def main() -> None:
    print("=== Tracker State tests ===")

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    logger = EventLogger(OUTPUT / "tracker_state.log")
    state = TrackerState(logger=logger)

    first_event_hash = make_info_hash("first-event")
    assert_invalid(
        lambda: state.announce(
            info_hash=first_event_hash,
            peer_id=make_peer_id(99),
            ip="127.0.0.1",
            port=6999,
            uploaded=0,
            downloaded=0,
            left=1,
            event=None,
            now=50,
        ),
        "first announce for a peer must use event=started",
    )

    torrent_a = make_info_hash("torrent-a")
    torrent_b = make_info_hash("torrent-b")

    peer_1 = make_peer_id(1)
    peer_2 = make_peer_id(2)
    peer_3 = make_peer_id(3)
    peer_4 = make_peer_id(4)

    response_1 = state.announce(
        info_hash=torrent_a,
        peer_id=peer_1,
        ip="127.0.0.1",
        port=6881,
        uploaded=0,
        downloaded=0,
        left=1000,
        event="started",
        now=100,
    )
    assert response_1 == {
        "complete": 0,
        "incomplete": 1,
        "peers": [],
    }

    response_2 = state.announce(
        info_hash=torrent_a,
        peer_id=peer_2,
        ip="127.0.0.1",
        port=6882,
        uploaded=500,
        downloaded=1000,
        left=0,
        event="started",
        now=100,
    )
    assert response_2["complete"] == 1
    assert response_2["incomplete"] == 1
    assert response_2["peers"] == [
        {
            "peer_id": peer_1,
            "ip": "127.0.0.1",
            "port": 6881,
        }
    ]

    state.announce(
        info_hash=torrent_b,
        peer_id=peer_3,
        ip="127.0.0.1",
        port=6883,
        uploaded=0,
        downloaded=0,
        left=2000,
        event="started",
        now=100,
    )

    assert state.swarm_count() == 2
    assert state.peer_count(torrent_a) == 2
    assert state.peer_count(torrent_b) == 1

    peers_for_peer_1 = state.get_peers(torrent_a, peer_1)
    assert peers_for_peer_1 == [
        {
            "peer_id": peer_2,
            "ip": "127.0.0.1",
            "port": 6882,
        }
    ]

    completed_response = state.announce(
        info_hash=torrent_a,
        peer_id=peer_1,
        ip="127.0.0.1",
        port=6881,
        uploaded=250,
        downloaded=1000,
        left=999,
        event="completed",
        now=110,
    )
    assert completed_response["complete"] == 2
    assert completed_response["incomplete"] == 0

    stopped_response = state.announce(
        info_hash=torrent_a,
        peer_id=peer_2,
        ip="127.0.0.1",
        port=6882,
        uploaded=500,
        downloaded=1000,
        left=0,
        event="stopped",
        now=111,
    )
    assert stopped_response["complete"] == 1
    assert stopped_response["incomplete"] == 0
    assert state.peer_count(torrent_a) == 1

    state.announce(
        info_hash=torrent_a,
        peer_id=peer_4,
        ip="127.0.0.1",
        port=6884,
        uploaded=0,
        downloaded=0,
        left=400,
        event="started",
        now=100,
    )

    # Keep peer_1 alive.
    state.announce(
        info_hash=torrent_a,
        peer_id=peer_1,
        ip="127.0.0.1",
        port=6881,
        uploaded=300,
        downloaded=1000,
        left=0,
        event=None,
        now=120,
    )

    expired = state.remove_expired_peers(timeout=30, now=131)
    expired_pairs = {
        (item["info_hash"], item["peer_id"])
        for item in expired
    }

    assert (torrent_a, peer_4) in expired_pairs
    assert (torrent_b, peer_3) in expired_pairs
    assert (torrent_a, peer_1) not in expired_pairs
    assert state.peer_count(torrent_a) == 1
    assert state.peer_count(torrent_b) == 0
    assert state.swarm_count() == 1

    assert_invalid(
        lambda: state.announce(
            info_hash=b"short",
            peer_id=peer_1,
            ip="127.0.0.1",
            port=6881,
            uploaded=0,
            downloaded=0,
            left=0,
            event="started",
        ),
        "20 bytes",
    )

    assert_invalid(
        lambda: state.announce(
            info_hash=torrent_a,
            peer_id=b"short",
            ip="127.0.0.1",
            port=6881,
            uploaded=0,
            downloaded=0,
            left=0,
            event="started",
        ),
        "peer_id must be exactly 20 bytes",
    )

    assert_invalid(
        lambda: state.announce(
            info_hash=torrent_a,
            peer_id=peer_1,
            ip="not-an-ip",
            port=6881,
            uploaded=0,
            downloaded=0,
            left=0,
            event="started",
        ),
        "invalid IP address",
    )

    assert_invalid(
        lambda: state.announce(
            info_hash=torrent_a,
            peer_id=peer_1,
            ip="127.0.0.1",
            port=70000,
            uploaded=0,
            downloaded=0,
            left=0,
            event="started",
        ),
        "must not exceed 65535",
    )

    assert_invalid(
        lambda: state.announce(
            info_hash=torrent_a,
            peer_id=peer_1,
            ip="127.0.0.1",
            port=6881,
            uploaded=0,
            downloaded=0,
            left=0,
            event="unknown",
        ),
        "invalid event",
    )

    concurrent_state = TrackerState()
    concurrent_hash = make_info_hash("concurrent-torrent")
    peer_total = 120

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [
            executor.submit(
                register_concurrent_peer,
                concurrent_state,
                concurrent_hash,
                index,
            )
            for index in range(peer_total)
        ]

        for future in futures:
            future.result()

    concurrent_stats = concurrent_state.get_statistics(concurrent_hash)
    expected_complete = len(
        [index for index in range(peer_total) if index % 3 == 0]
    )

    assert concurrent_state.peer_count(concurrent_hash) == peer_total
    assert concurrent_stats == {
        "complete": expected_complete,
        "incomplete": peer_total - expected_complete,
    }
    assert len(
        concurrent_state.get_peers(
            concurrent_hash,
            make_peer_id(0),
        )
    ) == peer_total - 1

    event_types = {
        event["event_type"]
        for event in logger.read_events()
    }
    assert {
        "PEER_REGISTERED",
        "PEER_UPDATED",
        "PEER_COMPLETED",
        "PEER_STOPPED",
        "PEER_EXPIRED",
    }.issubset(event_types)

    print(f"Swarm count after cleanup: {state.swarm_count()}")
    print(f"Active peers after cleanup: {state.peer_count()}")
    print(
        "Concurrent swarm statistics: "
        f"complete={concurrent_stats['complete']}, "
        f"incomplete={concurrent_stats['incomplete']}"
    )
    print("First announce requires started: PASS")
    print("Registration and periodic update: PASS")
    print("Seeder/leecher statistics: PASS")
    print("Requester exclusion from peer list: PASS")
    print("Completed and stopped events: PASS")
    print("Expired-peer cleanup: PASS")
    print("Multiple-swarm isolation: PASS")
    print("Input validation: PASS")
    print("Thread-safe concurrent registration: PASS")
    print("Tracker event logging: PASS")
    print("TRACKER STATE TESTS RESULT: PASS")


def test_tracker_state_component() -> None:
    main()

if __name__ == "__main__":
    main()
