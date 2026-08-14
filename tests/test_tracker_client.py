from __future__ import annotations

from pathlib import Path
import shutil
from urllib.parse import urlsplit

from common.bencode import bencode
from common.event_logger import EventLogger
from peer.metainfo import Metainfo
from peer.tracker_client import (
    TrackerClient,
    TrackerClientError,
    TrackerFailure,
    generate_peer_id,
    parse_tracker_response,
)
from tools.create_metainfo import create_metainfo
from tracker.tracker_server import TrackerRuntime


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test_output" / "tracker_client"


def assert_raises(
    exception_type: type[BaseException],
    callable_object,
    message_part: str,
) -> None:
    try:
        callable_object()
    except exception_type as exc:
        assert message_part in str(exc), (
            f"Expected message containing {message_part!r}, got {str(exc)!r}"
        )
        return

    raise AssertionError(f"Expected {exception_type.__name__}")


def main() -> None:
    print("=== Peer Tracker Client tests ===")

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    runtime = TrackerRuntime(
        host="127.0.0.1",
        port=0,
        announce_interval=19,
        peer_timeout=60.0,
        cleanup_interval=5.0,
        log_path=OUTPUT / "tracker.log",
    )
    runtime.start()

    try:
        shared_file = OUTPUT / "shared.bin"
        shared_file.write_bytes(bytes(range(256)) * 3 + b"tracker-client")

        torrent_path = OUTPUT / "sample.torrent.json"
        create_metainfo(
            [shared_file],
            tracker_url=runtime.announce_url,
            output_path=torrent_path,
            piece_length=128,
        )

        metainfo = Metainfo(torrent_path).load()
        assert metainfo.info_hash is not None
        assert metainfo.announce == runtime.announce_url

        generated = generate_peer_id()
        assert isinstance(generated, bytes)
        assert len(generated) == 20
        assert generated.startswith(b"-DN0001-")

        peer_1_id = b"-DN0001-PEER00000001"
        peer_2_id = b"-DN0001-PEER00000002"
        assert len(peer_1_id) == 20
        assert len(peer_2_id) == 20

        peer_1_logger = EventLogger(OUTPUT / "peer_1.log")
        peer_2_logger = EventLogger(OUTPUT / "peer_2.log")

        client_1 = TrackerClient.from_metainfo(
            metainfo,
            peer_id=peer_1_id,
            port=6881,
            logger=peer_1_logger,
        )
        client_2 = TrackerClient.from_metainfo(
            metainfo,
            peer_id=peer_2_id,
            port=6882,
            logger=peer_2_logger,
        )

        first = client_1.announce(
            info_hash=metainfo.info_hash,
            uploaded=0,
            downloaded=0,
            left=metainfo.total_length,
            event="started",
        )
        assert first.interval == 19
        assert first.complete == 0
        assert first.incomplete == 1
        assert first.peers == ()

        second = client_2.announce(
            info_hash=metainfo.info_hash,
            uploaded=metainfo.total_length,
            downloaded=0,
            left=0,
            event="started",
        )
        assert second.complete == 1
        assert second.incomplete == 1
        assert len(second.peers) == 1
        assert second.peers[0].peer_id == peer_1_id
        assert second.peers[0].ip == "127.0.0.1"
        assert second.peers[0].port == 6881

        periodic = client_1.announce(
            info_hash=metainfo.info_hash,
            uploaded=0,
            downloaded=128,
            left=metainfo.total_length - 128,
            event=None,
        )
        assert len(periodic.peers) == 1
        assert periodic.peers[0].peer_id == peer_2_id
        assert periodic.peers[0].port == 6882

        completed = client_1.announce(
            info_hash=metainfo.info_hash,
            uploaded=0,
            downloaded=metainfo.total_length,
            left=0,
            event="completed",
        )
        assert completed.complete == 2
        assert completed.incomplete == 0

        stopped = client_2.announce(
            info_hash=metainfo.info_hash,
            uploaded=metainfo.total_length,
            downloaded=0,
            left=0,
            event="stopped",
        )
        assert stopped.complete == 1
        assert stopped.incomplete == 0
        assert len(stopped.peers) == 1
        assert stopped.peers[0].peer_id == peer_1_id

        binary_hash = bytes(
            [0, 1, 2, 3, 4, 5, 37, 38, 43, 47, 61, 63, 127, 128, 200, 255, 9, 10, 11, 12]
        )
        binary_url = client_1.build_announce_url(
            info_hash=binary_hash,
            uploaded=0,
            downloaded=0,
            left=1,
            event="started",
        )
        query = urlsplit(binary_url).query
        for encoded in ("%00", "%25", "%26", "%2B", "%2F", "%3D", "%3F", "%80", "%FF"):
            assert encoded in query

        binary_response = client_1.announce(
            info_hash=binary_hash,
            uploaded=0,
            downloaded=0,
            left=1,
            event="started",
        )
        assert binary_response.incomplete == 1

        failure_body = bencode({"failure reason": "synthetic tracker failure"})
        assert_raises(
            TrackerFailure,
            lambda: parse_tracker_response(failure_body),
            "synthetic tracker failure",
        )

        malformed_body = bencode(
            {
                "interval": 10,
                "complete": 0,
                "incomplete": 0,
                "peers": [
                    {
                        "peer_id": b"too-short",
                        "ip": "127.0.0.1",
                        "port": 6881,
                    }
                ],
            }
        )
        assert_raises(
            TrackerClientError,
            lambda: parse_tracker_response(malformed_body),
            "exactly 20 bytes",
        )

        assert_raises(
            ValueError,
            lambda: client_1.announce(
                info_hash=b"short",
                uploaded=0,
                downloaded=0,
                left=0,
                event="started",
            ),
            "info_hash must be exactly 20 bytes",
        )

        assert_raises(
            ValueError,
            lambda: client_1.announce(
                info_hash=metainfo.info_hash,
                uploaded=0,
                downloaded=0,
                left=-1,
                event="started",
            ),
            "left must be non-negative",
        )

        assert_raises(
            ValueError,
            lambda: client_1.announce(
                info_hash=metainfo.info_hash,
                uploaded=0,
                downloaded=0,
                left=0,
                event="invalid",
            ),
            "event must be",
        )

        peer_1_events = {
            event["event_type"]
            for event in peer_1_logger.read_events()
        }
        assert {"TRACKER_REQUEST", "TRACKER_RESPONSE"}.issubset(peer_1_events)

        tracker_events = {
            event["event_type"]
            for event in runtime.logger.read_events()
        }
        assert {
            "ANNOUNCE_RECEIVED",
            "HTTP_RESPONSE",
            "PEER_REGISTERED",
            "PEER_UPDATED",
            "PEER_COMPLETED",
            "PEER_STOPPED",
        }.issubset(tracker_events)

        print(f"Tracker announce URL: {runtime.announce_url}")
        print(f"Generated peer ID: {generated.decode('ascii')}")
        print(
            "Metainfo request values: "
            f"bytes={metainfo.total_length}, "
            f"pieces={metainfo.piece_count}, "
            f"info_hash={metainfo.info_hash_hex}"
        )
        print("20-byte peer ID generation: PASS")
        print("Tracker URL construction and percent encoding: PASS")
        print("HTTP request and Bencode response parsing: PASS")
        print("Started, periodic, completed, stopped requests: PASS")
        print("Typed peer-list response handling: PASS")
        print("Tracker failure-response handling: PASS")
        print("Client-side argument validation: PASS")
        print("Peer-side Tracker event logging: PASS")
        print("TRACKER CLIENT TESTS RESULT: PASS")

    finally:
        runtime.stop()


def test_tracker_client_component() -> None:
    main()

if __name__ == "__main__":
    main()
