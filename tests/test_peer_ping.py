from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import socket
import struct
import threading
import time

from common.event_logger import EventLogger
from peer.peer_protocol import (
    PeerProtocolError,
    encode_message,
    receive_message,
    send_message,
)
from peer.peer_server import PeerServer
from peer.ping_client import PeerPingService, ping_all_peers, ping_peer
from peer.tracker_client import TrackerClient, TrackerPeer
from tracker.tracker_server import TrackerRuntime


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test_output" / "peer_ping"


def assert_protocol_error(callable_object, message_part: str) -> None:
    try:
        callable_object()
    except PeerProtocolError as exc:
        assert message_part in str(exc), (
            f"Expected {message_part!r}, got {str(exc)!r}"
        )
        return
    raise AssertionError("Expected PeerProtocolError")


def test_protocol_frames() -> None:
    left, right = socket.socketpair()
    try:
        message = {
            "type": "PING",
            "peer_id": "00" * 20,
            "nonce": "fragment-test",
            "sent_at": 123.5,
        }
        frame = encode_message(message)

        # Send it one byte at a time.
        for byte in frame:
            left.sendall(bytes([byte]))

        decoded = receive_message(right)
        assert decoded == message
    finally:
        left.close()
        right.close()

    oversized_left, oversized_right = socket.socketpair()
    try:
        oversized_left.sendall(struct.pack("!I", 5000))
        assert_protocol_error(
            lambda: receive_message(oversized_right, max_message_size=100),
            "exceeds maximum",
        )
    finally:
        oversized_left.close()
        oversized_right.close()

    invalid_left, invalid_right = socket.socketpair()
    try:
        invalid_payload = b"not-json"
        invalid_left.sendall(struct.pack("!I", len(invalid_payload)) + invalid_payload)
        assert_protocol_error(
            lambda: receive_message(invalid_right),
            "not valid JSON",
        )
    finally:
        invalid_left.close()
        invalid_right.close()


def main() -> None:
    print("=== Peer TCP Ping/Pong tests ===")

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    test_protocol_frames()

    tracker = TrackerRuntime(
        host="127.0.0.1",
        port=0,
        announce_interval=1,
        peer_timeout=60.0,
        cleanup_interval=5.0,
        log_path=OUTPUT / "tracker.log",
    )

    peer_1_id = b"-DN0001-PEER70000001"
    peer_2_id = b"-DN0001-PEER70000002"
    assert len(peer_1_id) == 20
    assert len(peer_2_id) == 20

    peer_1_logger = EventLogger(OUTPUT / "peer_1.log")
    peer_2_logger = EventLogger(OUTPUT / "peer_2.log")

    peer_1_server = PeerServer(
        host="127.0.0.1",
        port=0,
        peer_id=peer_1_id,
        logger=peer_1_logger,
    )
    peer_2_server = PeerServer(
        host="127.0.0.1",
        port=0,
        peer_id=peer_2_id,
        logger=peer_2_logger,
    )

    tracker.start()
    peer_1_server.start()
    peer_2_server.start()

    torrent_hash = bytes.fromhex("1234567890abcdef1234567890abcdef12345678")

    try:
        client_1 = TrackerClient(
            announce_url=tracker.announce_url,
            peer_id=peer_1_id,
            port=peer_1_server.port,
            logger=peer_1_logger,
        )
        client_2 = TrackerClient(
            announce_url=tracker.announce_url,
            peer_id=peer_2_id,
            port=peer_2_server.port,
            logger=peer_2_logger,
        )

        first = client_1.announce(
            info_hash=torrent_hash,
            uploaded=0,
            downloaded=0,
            left=1000,
            event="started",
        )
        assert first.peers == ()

        second = client_2.announce(
            info_hash=torrent_hash,
            uploaded=1000,
            downloaded=0,
            left=0,
            event="started",
        )
        assert len(second.peers) == 1
        assert second.peers[0].peer_id == peer_1_id
        assert second.peers[0].port == peer_1_server.port

        peer_2_to_peer_1 = ping_all_peers(
            second.peers,
            local_peer_id=peer_2_id,
            timeout=3.0,
            logger=peer_2_logger,
        )
        assert len(peer_2_to_peer_1) == 1
        assert peer_2_to_peer_1[0].success
        assert peer_2_to_peer_1[0].rtt_ms is not None

        refreshed = client_1.announce(
            info_hash=torrent_hash,
            uploaded=0,
            downloaded=0,
            left=1000,
            event=None,
        )
        assert len(refreshed.peers) == 1
        assert refreshed.peers[0].peer_id == peer_2_id
        assert refreshed.peers[0].port == peer_2_server.port

        peer_1_to_peer_2 = ping_all_peers(
            refreshed.peers,
            local_peer_id=peer_1_id,
            timeout=3.0,
            logger=peer_1_logger,
        )
        assert len(peer_1_to_peer_2) == 1
        assert peer_1_to_peer_2[0].success

        target_peer_1 = TrackerPeer(
            peer_id=peer_1_id,
            ip="127.0.0.1",
            port=peer_1_server.port,
        )
        concurrent_ping_count = 40
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [
                executor.submit(
                    ping_peer,
                    target_peer_1,
                    local_peer_id=peer_2_id,
                    timeout=3.0,
                    logger=peer_2_logger,
                )
                for _ in range(concurrent_ping_count)
            ]
            concurrent_results = [future.result() for future in futures]
        assert all(result.success for result in concurrent_results)

        wrong_identity = TrackerPeer(
            peer_id=b"-DN0001-WRONG0000001",
            ip="127.0.0.1",
            port=peer_1_server.port,
        )
        assert len(wrong_identity.peer_id) == 20
        mismatch = ping_peer(
            wrong_identity,
            local_peer_id=peer_2_id,
            timeout=3.0,
            logger=peer_2_logger,
        )
        assert not mismatch.success
        assert mismatch.error is not None
        assert "does not match" in mismatch.error

        cycles: list[tuple] = []
        cycle_lock = threading.Lock()
        cycle_event = threading.Event()

        def receive_cycle(results: tuple) -> None:
            with cycle_lock:
                cycles.append(results)
                if len(cycles) >= 2:
                    cycle_event.set()

        periodic = PeerPingService(
            local_peer_id=peer_2_id,
            peer_provider=lambda: second.peers,
            interval=0.10,
            timeout=2.0,
            logger=peer_2_logger,
            on_results=receive_cycle,
        )
        periodic.start()
        try:
            assert cycle_event.wait(3.0), "periodic ping service did not run twice"
        finally:
            periodic.stop()

        with cycle_lock:
            assert len(cycles) >= 2
            assert all(
                result.success
                for cycle in cycles
                for result in cycle
            )

        peer_1_events = {
            event["event_type"]
            for event in peer_1_logger.read_events()
        }
        peer_2_events = {
            event["event_type"]
            for event in peer_2_logger.read_events()
        }

        assert {
            "PEER_SERVER_STARTED",
            "CONNECTION_ACCEPTED",
            "PING_RECEIVED",
            "PONG_SENT",
        }.issubset(peer_1_events)

        assert {
            "PING_SENT",
            "PONG_RECEIVED",
            "PING_FAILED",
            "PING_SERVICE_STARTED",
            "PING_SERVICE_STOPPED",
        }.issubset(peer_2_events)

        print(f"Tracker URL: {tracker.announce_url}")
        print(
            "Peer 1 TCP address: "
            f"127.0.0.1:{peer_1_server.port} id={peer_1_id.decode('ascii')}"
        )
        print(
            "Peer 2 TCP address: "
            f"127.0.0.1:{peer_2_server.port} id={peer_2_id.decode('ascii')}"
        )
        print(f"Concurrent successful pings: {concurrent_ping_count}")
        print(f"Periodic ping cycles observed: {len(cycles)}")
        print("Length-prefixed JSON framing: PASS")
        print("Fragmented TCP receive handling: PASS")
        print("Multithreaded Peer TCP servers: PASS")
        print("Tracker-discovered peer Ping/Pong: PASS")
        print("PONG peer identity and nonce validation: PASS")
        print("Concurrent TCP Ping/Pong exchanges: PASS")
        print("Periodic peer ping service: PASS")
        print("Peer TCP event logging: PASS")
        print("PEER PING TESTS RESULT: PASS")

    finally:
        try:
            client_1.announce(
                info_hash=torrent_hash,
                uploaded=0,
                downloaded=0,
                left=1000,
                event="stopped",
            )
        except Exception:
            pass
        try:
            client_2.announce(
                info_hash=torrent_hash,
                uploaded=1000,
                downloaded=0,
                left=0,
                event="stopped",
            )
        except Exception:
            pass
        peer_1_server.stop()
        peer_2_server.stop()
        tracker.stop()


def test_peer_ping_component() -> None:
    main()

if __name__ == "__main__":
    main()
