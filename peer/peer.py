from __future__ import annotations

import argparse
from pathlib import Path

from common.event_logger import EventLogger
from peer.tracker_client import generate_peer_id
from peer.torrent_worker import TorrentJob, TorrentThreadPool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one Peer worker thread for each Metainfo file."
    )
    parser.add_argument(
        "torrents",
        nargs="+",
        help="One or more .torrent.json files",
    )
    parser.add_argument(
        "--download-root",
        default="peer/downloads",
        help="Root folder used for downloaded/shared data",
    )
    parser.add_argument(
        "--log-dir",
        default="peer/logs/torrents",
        help="Per-torrent log directory",
    )
    parser.add_argument(
        "--port-start",
        type=int,
        default=6881,
        help="First listening port; each torrent uses the next port",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=1000000,
        help="Maximum regular announce cycles",
    )
    parser.add_argument(
        "--exit-on-complete",
        action="store_true",
        help="Exit workers after downloads complete instead of continuing to seed",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    peer_id = generate_peer_id()
    download_root = Path(args.download_root)
    jobs = []

    for index, torrent in enumerate(args.torrents):
        torrent_path = Path(torrent)
        storage = download_root / torrent_path.stem
        jobs.append(
            TorrentJob(
                torrent_path=torrent_path,
                storage_root=storage,
                listen_port=args.port_start + index,
            )
        )

    app_logger = EventLogger(Path(args.log_dir) / "peer_app.log")
    pool = TorrentThreadPool(
        peer_id=peer_id,
        jobs=jobs,
        log_directory=args.log_dir,
        max_cycles=args.max_cycles,
        exit_on_complete=args.exit_on_complete,
        application_logger=app_logger,
    )

    print(f"Peer ID: {peer_id.decode('ascii')}")
    print(f"Torrent worker count: {len(jobs)}")
    print("Press Ctrl+C to stop gracefully.")

    try:
        results = pool.run()
    except KeyboardInterrupt:
        print("\nStopping Peer workers...")
        pool.stop()
        return 130

    for result in results:
        print(
            f"{result.name}: complete={result.complete} "
            f"left={result.final_left} cycles={result.announce_cycles}"
        )

    success = all(
        result.complete
        and result.started_sent
        and result.stopped_sent
        and not result.errors
        for result in results
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
