from __future__ import annotations

from pathlib import Path
import shutil
import threading
import time

from common.event_logger import EventLogger
from peer.metainfo import Metainfo
from peer.peer_server import PeerServer
from peer.piece_manager import PieceManager
from peer.tracker_client import TrackerClient
from peer.torrent_worker import TorrentJob, TorrentThreadPool
from tools.create_metainfo import create_metainfo
from tracker.tracker_server import TrackerRuntime


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test_output" / "torrent_thread_pool"


def main() -> None:
    print("=== Torrent Worker and Thread Pool tests ===")

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    tracker = TrackerRuntime(
        host="127.0.0.1",
        port=0,
        announce_interval=1,
        peer_timeout=60.0,
        cleanup_interval=5.0,
        log_path=OUTPUT / "tracker.log",
    )
    tracker.start()

    seed_a_id = b"-DN0001-SEED90000001"
    seed_b_id = b"-DN0001-SEED90000002"
    app_peer_id = b"-DN0001-APP900000001"
    assert len(seed_a_id) == 20
    assert len(seed_b_id) == 20
    assert len(app_peer_id) == 20

    seed_servers: list[PeerServer] = []
    seed_clients: list[tuple[TrackerClient, bytes]] = []
    delayed_errors: list[str] = []
    delayed_ready = threading.Event()

    try:
        # First torrent
        seed_a_root = OUTPUT / "seed_a"
        seed_a_root.mkdir()
        data_a = bytes((index * 11 + 7) % 256 for index in range(1501))
        file_a = seed_a_root / "alpha.bin"
        file_a.write_bytes(data_a)
        torrent_a = OUTPUT / "alpha.torrent.json"

        create_metainfo(
            [file_a],
            tracker_url=tracker.announce_url,
            output_path=torrent_a,
            piece_length=192,
        )
        meta_a = Metainfo(torrent_a).load()
        assert meta_a.info_hash is not None

        manager_a = PieceManager(meta_a, seed_a_root)
        server_a = PeerServer(
            host="127.0.0.1",
            port=0,
            peer_id=seed_a_id,
            piece_manager=manager_a,
            logger=EventLogger(OUTPUT / "seed_a.log"),
        )
        server_a.start()
        seed_servers.append(server_a)

        client_a = TrackerClient.from_metainfo(
            meta_a,
            peer_id=seed_a_id,
            port=server_a.port,
            logger=EventLogger(OUTPUT / "seed_a_tracker.log"),
        )
        client_a.announce(
            info_hash=meta_a.info_hash,
            uploaded=0,
            downloaded=0,
            left=0,
            event="started",
        )
        seed_clients.append((client_a, meta_a.info_hash))

        # The second seeder starts a little later.
        seed_b_root = OUTPUT / "seed_b"
        seed_b_root.mkdir()
        data_b = bytes((index * 19 + 3) % 256 for index in range(2107))
        file_b = seed_b_root / "beta.bin"
        file_b.write_bytes(data_b)
        torrent_b = OUTPUT / "beta.torrent.json"

        create_metainfo(
            [file_b],
            tracker_url=tracker.announce_url,
            output_path=torrent_b,
            piece_length=256,
        )
        meta_b = Metainfo(torrent_b).load()
        assert meta_b.info_hash is not None

        manager_b = PieceManager(meta_b, seed_b_root)
        server_b = PeerServer(
            host="127.0.0.1",
            port=0,
            peer_id=seed_b_id,
            piece_manager=manager_b,
            logger=EventLogger(OUTPUT / "seed_b.log"),
        )

        client_b_holder: list[TrackerClient] = []

        def delayed_seed_b_registration() -> None:
            try:
                time.sleep(0.25)
                server_b.start()
                seed_servers.append(server_b)

                client_b = TrackerClient.from_metainfo(
                    meta_b,
                    peer_id=seed_b_id,
                    port=server_b.port,
                    logger=EventLogger(OUTPUT / "seed_b_tracker.log"),
                )
                client_b.announce(
                    info_hash=meta_b.info_hash,
                    uploaded=0,
                    downloaded=0,
                    left=0,
                    event="started",
                )
                client_b_holder.append(client_b)
                seed_clients.append((client_b, meta_b.info_hash))
                delayed_ready.set()
            except Exception as exc:
                delayed_errors.append(f"{type(exc).__name__}: {exc}")
                delayed_ready.set()

        delayed_thread = threading.Thread(
            target=delayed_seed_b_registration,
            name="delayed-seed-b",
            daemon=True,
        )
        delayed_thread.start()

        app_logger = EventLogger(OUTPUT / "peer_app.log")
        jobs = [
            TorrentJob(
                torrent_path=torrent_a,
                storage_root=OUTPUT / "leecher_a",
                listen_port=0,
            ),
            TorrentJob(
                torrent_path=torrent_b,
                storage_root=OUTPUT / "leecher_b",
                listen_port=0,
            ),
        ]

        pool = TorrentThreadPool(
            peer_id=app_peer_id,
            jobs=jobs,
            log_directory=OUTPUT / "worker_logs",
            tracker_timeout=3.0,
            peer_timeout=3.0,
            max_cycles=30,
            interval_cap=0.05,
            exit_on_complete=True,
            application_logger=app_logger,
        )

        results = pool.run()
        delayed_thread.join(timeout=3.0)

        assert delayed_ready.is_set()
        assert not delayed_errors, delayed_errors
        assert len(results) == 2

        result_a, result_b = results

        assert result_a.complete
        assert result_b.complete
        assert result_a.final_left == 0
        assert result_b.final_left == 0
        assert result_a.started_sent and result_b.started_sent
        assert result_a.completed_sent and result_b.completed_sent
        assert result_a.stopped_sent and result_b.stopped_sent
        assert result_a.barrier_synchronized
        assert result_b.barrier_synchronized
        assert result_a.thread_name != result_b.thread_name
        assert result_a.discovered_peers >= 1
        assert result_b.discovered_peers >= 1
        assert result_a.successful_pings >= 1
        assert result_b.successful_pings >= 1
        assert result_a.pieces_downloaded == meta_a.piece_count
        assert result_b.pieces_downloaded == meta_b.piece_count
        assert result_a.bytes_downloaded == meta_a.total_length
        assert result_b.bytes_downloaded == meta_b.total_length
        assert result_a.session_downloaded == meta_a.total_length
        assert result_b.session_downloaded == meta_b.total_length
        assert result_a.session_uploaded == 0
        assert result_b.session_uploaded == 0
        assert result_b.periodic_announces >= 1
        assert result_b.announce_cycles >= 2
        assert result_a.errors == ()
        assert result_b.errors == ()

        downloaded_a = OUTPUT / "leecher_a" / "alpha.bin"
        downloaded_b = OUTPUT / "leecher_b" / "beta.bin"

        assert downloaded_a.read_bytes() == data_a
        assert downloaded_b.read_bytes() == data_b

        # Finished workers should be gone now.
        stats_a = tracker.state.get_statistics(meta_a.info_hash)
        stats_b = tracker.state.get_statistics(meta_b.info_hash)
        assert stats_a == {"complete": 1, "incomplete": 0}
        assert stats_b == {"complete": 1, "incomplete": 0}

        app_events = {
            event["event_type"]
            for event in app_logger.read_events()
        }
        assert {
            "TORRENT_THREAD_POOL_STARTED",
            "TORRENT_THREAD_POOL_STOPPED",
        }.issubset(app_events)

        worker_log_files = sorted((OUTPUT / "worker_logs").glob("*.log"))
        assert len(worker_log_files) == 2

        for log_file in worker_log_files:
            events = {
                event["event_type"]
                for event in EventLogger(log_file).read_events()
            }
            assert {
                "TORRENT_WORKER_THREAD_STARTED",
                "TORRENT_WORKER_BARRIER_PASSED",
                "TORRENT_WORKER_STARTED",
                "TRACKER_PEERS_UPDATED",
                "TORRENT_CYCLE_COMPLETED",
                "TORRENT_WORKER_COMPLETED",
                "TORRENT_WORKER_STOPPED",
                "PING_SENT",
                "PONG_RECEIVED",
                "PIECE_RECEIVED",
            }.issubset(events)

        print(f"Tracker URL: {tracker.announce_url}")
        print(
            "Torrent A: "
            f"bytes={meta_a.total_length}, pieces={meta_a.piece_count}, "
            f"cycles={result_a.announce_cycles}"
        )
        print(
            "Torrent B: "
            f"bytes={meta_b.total_length}, pieces={meta_b.piece_count}, "
            f"cycles={result_b.announce_cycles}, "
            f"periodic={result_b.periodic_announces}"
        )
        print("One worker thread per torrent: PASS")
        print("Thread-pool start synchronization: PASS")
        print("Independent Metainfo parsing and PieceManagers: PASS")
        print("Session counters in Tracker announces: PASS")
        print("Started and periodic Tracker announces: PASS")
        print("Tracker-discovered peer Ping/Pong per worker: PASS")
        print("Concurrent multi-torrent piece downloads: PASS")
        print("Completed and graceful stopped announces: PASS")
        print("Downloaded files reconstructed byte-for-byte: PASS")
        print("Per-torrent and application event logs: PASS")
        print("TORRENT WORKER TESTS RESULT: PASS")

    finally:
        for client, info_hash in seed_clients:
            try:
                client.announce(
                    info_hash=info_hash,
                    uploaded=0,
                    downloaded=0,
                    left=0,
                    event="stopped",
                )
            except Exception:
                pass

        for server in reversed(seed_servers):
            server.stop()

        tracker.stop()


def test_torrent_thread_pool_component() -> None:
    main()

if __name__ == "__main__":
    main()
