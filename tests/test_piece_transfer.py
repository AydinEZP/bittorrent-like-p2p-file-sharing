from __future__ import annotations

from pathlib import Path
import shutil

from common.event_logger import EventLogger
from peer.metainfo import Metainfo
from peer.peer_server import PeerServer
from peer.piece_client import PieceClient, PieceClientError
from peer.piece_manager import PieceHashMismatch, PieceManager
from peer.tracker_client import TrackerClient
from peer.transfer_stats import TransferStats
from tools.create_metainfo import create_metainfo
from tracker.tracker_server import TrackerRuntime


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test_output" / "piece_transfer"


def main() -> None:
    print("=== Piece Manager and File Transfer tests ===")

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    tracker = TrackerRuntime(
        host="127.0.0.1",
        port=0,
        announce_interval=2,
        peer_timeout=60.0,
        cleanup_interval=5.0,
        log_path=OUTPUT / "tracker.log",
    )

    seed_id = b"-DN0001-SEED80000001"
    leecher_id = b"-DN0001-LEECH8000001"
    assert len(seed_id) == 20
    assert len(leecher_id) == 20

    seed_logger = EventLogger(OUTPUT / "seed.log")
    leecher_logger = EventLogger(OUTPUT / "leecher.log")

    seed_root = OUTPUT / "single_seed"
    leecher_root = OUTPUT / "single_leecher"
    seed_root.mkdir(parents=True)

    source_bytes = bytes((index * 17 + 11) % 256 for index in range(1803))
    source_path = seed_root / "shared_payload.bin"
    source_path.write_bytes(source_bytes)

    single_torrent = OUTPUT / "single.torrent.json"

    tracker.start()
    seed_server: PeerServer | None = None

    try:
        create_metainfo(
            [source_path],
            tracker_url=tracker.announce_url,
            output_path=single_torrent,
            piece_length=256,
        )
        metainfo = Metainfo(single_torrent).load()
        assert metainfo.info_hash is not None

        seed_manager = PieceManager(
            metainfo,
            seed_root,
            logger=seed_logger,
        )
        leecher_manager = PieceManager(
            metainfo,
            leecher_root,
            create_missing=True,
            logger=leecher_logger,
        )

        assert seed_manager.is_complete()
        assert seed_manager.bitfield == (True,) * metainfo.piece_count
        assert not leecher_manager.is_complete()
        assert leecher_manager.bitfield == (False,) * metainfo.piece_count
        assert leecher_manager.left == metainfo.total_length

        seed_stats = TransferStats()
        leecher_stats = TransferStats()

        seed_server = PeerServer(
            host="127.0.0.1",
            port=0,
            peer_id=seed_id,
            logger=seed_logger,
            piece_manager=seed_manager,
            transfer_stats=seed_stats,
        )
        seed_server.start()

        seed_tracker_client = TrackerClient(
            announce_url=tracker.announce_url,
            peer_id=seed_id,
            port=seed_server.port,
            logger=seed_logger,
        )
        leecher_tracker_client = TrackerClient(
            announce_url=tracker.announce_url,
            peer_id=leecher_id,
            port=6889,
            logger=leecher_logger,
        )

        seed_announce = seed_tracker_client.announce(
            info_hash=metainfo.info_hash,
            uploaded=0,
            downloaded=0,
            left=0,
            event="started",
        )
        assert seed_announce.complete == 1
        assert seed_announce.incomplete == 0

        leecher_announce = leecher_tracker_client.announce(
            info_hash=metainfo.info_hash,
            uploaded=0,
            downloaded=0,
            left=metainfo.total_length,
            event="started",
        )
        assert leecher_announce.complete == 1
        assert leecher_announce.incomplete == 1
        assert len(leecher_announce.peers) == 1
        assert leecher_announce.peers[0].peer_id == seed_id
        assert leecher_announce.peers[0].port == seed_server.port

        piece_client = PieceClient(
            local_peer_id=leecher_id,
            info_hash=metainfo.info_hash,
            piece_manager=leecher_manager,
            transfer_stats=leecher_stats,
            logger=leecher_logger,
        )

        try:
            piece_client.request_piece(leecher_announce.peers[0], 0)
        except PieceClientError as exc:
            assert "INTERESTED/UNCHOKE" in str(exc)
        else:
            raise AssertionError("REQUEST before INTERESTED should be rejected")

        result = piece_client.download_from_peer(leecher_announce.peers[0])

        assert result.requested == metainfo.piece_count
        assert result.downloaded == metainfo.piece_count
        assert result.skipped == 0
        assert result.bytes_downloaded == metainfo.total_length
        assert result.complete
        assert result.errors == ()
        assert seed_stats.snapshot().uploaded == metainfo.total_length
        assert seed_stats.snapshot().downloaded == 0
        assert leecher_stats.snapshot().uploaded == 0
        assert leecher_stats.snapshot().downloaded == metainfo.total_length
        assert leecher_manager.is_complete()
        assert leecher_manager.left == 0

        downloaded_path = leecher_root / "shared_payload.bin"
        assert downloaded_path.read_bytes() == source_bytes

        completed = leecher_tracker_client.announce(
            info_hash=metainfo.info_hash,
            uploaded=0,
            downloaded=metainfo.total_length,
            left=0,
            event="completed",
        )
        assert completed.complete == 2
        assert completed.incomplete == 0

        # A bad piece should not be saved.
        corrupt_root = OUTPUT / "corrupt_leecher"
        corrupt_manager = PieceManager(
            metainfo,
            corrupt_root,
            create_missing=True,
            logger=leecher_logger,
        )
        good_piece = seed_manager.read_piece(0)
        corrupted_piece = bytes([good_piece[0] ^ 0xFF]) + good_piece[1:]

        try:
            corrupt_manager.write_piece(0, corrupted_piece)
        except PieceHashMismatch:
            pass
        else:
            raise AssertionError("corrupted piece should have been rejected")

        assert not corrupt_manager.has_piece(0)

        multi_seed_root = OUTPUT / "multi_seed"
        multi_bundle = multi_seed_root / "bundle"
        multi_bundle.mkdir(parents=True)
        file_1_bytes = bytes((index * 3) % 256 for index in range(150))
        file_2_bytes = bytes((255 - index * 5) % 256 for index in range(290))
        file_1_path = multi_bundle / "first.bin"
        file_2_path = multi_bundle / "second.bin"
        file_1_path.write_bytes(file_1_bytes)
        file_2_path.write_bytes(file_2_bytes)

        multi_torrent = OUTPUT / "multi.torrent.json"
        create_metainfo(
            [file_1_path, file_2_path],
            tracker_url=tracker.announce_url,
            output_path=multi_torrent,
            piece_length=128,
            torrent_name="bundle",
        )
        multi_meta = Metainfo(multi_torrent).load()
        multi_seed_manager = PieceManager(multi_meta, multi_seed_root)
        multi_leecher_root = OUTPUT / "multi_leecher"
        multi_leecher_manager = PieceManager(
            multi_meta,
            multi_leecher_root,
            create_missing=True,
        )

        assert multi_seed_manager.is_complete()
        assert not multi_leecher_manager.is_complete()

        # This piece crosses the file boundary.
        boundary_piece = multi_seed_manager.read_piece(1)
        assert boundary_piece[:22] == file_1_bytes[128:150]
        assert boundary_piece[22:] == file_2_bytes[:106]

        for index in range(multi_meta.piece_count):
            multi_leecher_manager.write_piece(
                index,
                multi_seed_manager.read_piece(index),
            )

        assert multi_leecher_manager.is_complete()
        assert (
            multi_leecher_root / "bundle" / "first.bin"
        ).read_bytes() == file_1_bytes
        assert (
            multi_leecher_root / "bundle" / "second.bin"
        ).read_bytes() == file_2_bytes

        seed_events = {
            event["event_type"]
            for event in seed_logger.read_events()
        }
        leecher_events = {
            event["event_type"]
            for event in leecher_logger.read_events()
        }

        assert {
            "BITFIELD_SENT",
            "INTERESTED_RECEIVED",
            "UNCHOKE_SENT",
            "PIECE_REQUESTED",
            "PIECE_SENT",
        }.issubset(seed_events)

        assert {
            "BITFIELD_RECEIVED",
            "INTERESTED_SENT",
            "UNCHOKE_RECEIVED",
            "PIECE_RECEIVED",
            "HASH_VALID",
            "HASH_INVALID",
            "PIECE_STORED",
            "DOWNLOAD_COMPLETED",
        }.issubset(leecher_events)

        print(f"Tracker URL: {tracker.announce_url}")
        print(
            "Single-file transfer: "
            f"bytes={metainfo.total_length}, pieces={metainfo.piece_count}"
        )
        print(
            "Multi-file virtual stream: "
            f"bytes={multi_meta.total_length}, pieces={multi_meta.piece_count}"
        )
        print("Single-file PieceManager scan and bitfield: PASS")
        print("BITFIELD / INTERESTED / UNCHOKE exchange: PASS")
        print("REQUEST requires INTERESTED/UNCHOKE: PASS")
        print("Session upload/download counters: PASS")
        print("REQUEST / PIECE TCP transfer: PASS")
        print("Per-piece SHA-1 verification: PASS")
        print("Corrupted-piece rejection: PASS")
        print("Downloaded file byte-for-byte reconstruction: PASS")
        print("Tracker completion update after download: PASS")
        print("Multi-file cross-boundary piece storage: PASS")
        print("Piece-transfer event logging: PASS")
        print("PIECE TRANSFER TESTS RESULT: PASS")

    finally:
        if seed_server is not None:
            seed_server.stop()
        tracker.stop()


def test_piece_transfer_component() -> None:
    main()

if __name__ == "__main__":
    main()
