from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

import argparse
import shutil
import threading
import time
from typing import Any

from common.event_logger import EventLogger
from peer.metainfo import Metainfo
from peer.torrent_worker import TorrentJob, TorrentThreadPool
from tools.prepare_demo import prepare_demo_workspace
from tools.verify_demo import verify_demo_workspace
from tracker.tracker_server import TrackerRuntime


class IntegratedDemoError(RuntimeError):
    """Raised when the end-to-end demo cannot complete."""


def _wait_for_seeders(
    tracker: TrackerRuntime,
    metainfo_files: list[Path],
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        ready = True

        for torrent_path in metainfo_files:
            metainfo = Metainfo(torrent_path).load()
            assert metainfo.info_hash is not None
            stats = tracker.state.get_statistics(metainfo.info_hash)

            if stats["complete"] < 1:
                ready = False
                break

        if ready:
            return

        time.sleep(0.05)

    raise IntegratedDemoError(
        "Seeder workers did not register with the Tracker in time"
    )


def run_integrated_demo(
    workspace: str | Path,
    *,
    reset: bool = True,
) -> dict[str, Any]:
    workspace_path = Path(workspace).resolve()

    # Start with a clean demo folder.
    if reset and workspace_path.exists():
        shutil.rmtree(workspace_path)
    workspace_path.mkdir(parents=True, exist_ok=True)

    tracker = TrackerRuntime(
        host="127.0.0.1",
        port=0,
        announce_interval=1,
        peer_timeout=60.0,
        cleanup_interval=5.0,
        log_path=workspace_path / "logs" / "tracker" / "tracker.log",
    )
    tracker.start()

    seed_pool: TorrentThreadPool | None = None
    seed_thread: threading.Thread | None = None
    seed_results_holder: list[Any] = []
    seed_error_holder: list[BaseException] = []

    try:
        prepared = prepare_demo_workspace(
            workspace_path,
            tracker_url=tracker.announce_url,
            reset=False,
        )

        torrent_paths = [
            prepared["single_torrent"],
            prepared["multi_torrent"],
        ]

        seed_jobs = [
            TorrentJob(
                torrent_path=torrent,
                storage_root=prepared["seed_storage"] / torrent.stem,
                listen_port=0,
            )
            for torrent in torrent_paths
        ]

        seed_pool = TorrentThreadPool(
            peer_id=b"-DN0001-SEEDFINAL001",
            jobs=seed_jobs,
            log_directory=prepared["logs_dir"] / "seed",
            tracker_timeout=3.0,
            peer_timeout=3.0,
            max_cycles=10000,
            interval_cap=0.05,
            exit_on_complete=False,
            application_logger=EventLogger(
                prepared["logs_dir"] / "seed_app.log"
            ),
        )

        def run_seed_pool() -> None:
            try:
                seed_results_holder.extend(seed_pool.run())
            except BaseException as exc:
                seed_error_holder.append(exc)

        seed_thread = threading.Thread(
            target=run_seed_pool,
            name="integrated-demo-seed-pool",
            daemon=True,
        )
        seed_thread.start()

        _wait_for_seeders(
            tracker,
            torrent_paths,
            timeout=8.0,
        )

        leecher_jobs = [
            TorrentJob(
                torrent_path=torrent,
                storage_root=prepared["leecher_storage"] / torrent.stem,
                listen_port=0,
            )
            for torrent in torrent_paths
        ]

        leecher_pool = TorrentThreadPool(
            peer_id=b"-DN0001-LEECHFINAL01",
            jobs=leecher_jobs,
            log_directory=prepared["logs_dir"] / "leecher",
            tracker_timeout=3.0,
            peer_timeout=3.0,
            max_cycles=100,
            interval_cap=0.05,
            exit_on_complete=True,
            application_logger=EventLogger(
                prepared["logs_dir"] / "leecher_app.log"
            ),
        )

        leecher_results = leecher_pool.run()

        if not all(result.complete for result in leecher_results):
            raise IntegratedDemoError(
                "one or more Leecher torrent workers did not complete"
            )

        if not all(result.completed_sent for result in leecher_results):
            raise IntegratedDemoError(
                "one or more Leecher workers did not send completed"
            )

        if not all(result.stopped_sent for result in leecher_results):
            raise IntegratedDemoError(
                "one or more Leecher workers did not send stopped"
            )

        verification = verify_demo_workspace(workspace_path)

        # Only the seeders should still be registered.
        swarm_stats = []
        for torrent_path in torrent_paths:
            metainfo = Metainfo(torrent_path).load()
            assert metainfo.info_hash is not None
            stats = tracker.state.get_statistics(metainfo.info_hash)

            if stats != {"complete": 1, "incomplete": 0}:
                raise IntegratedDemoError(
                    f"unexpected post-download swarm stats: {stats}"
                )

            swarm_stats.append(stats)

        return {
            "tracker_url": tracker.announce_url,
            "prepared": prepared,
            "leecher_results": leecher_results,
            "seed_results": seed_results_holder,
            "verification": verification,
            "swarm_stats": swarm_stats,
        }

    finally:
        if seed_pool is not None:
            seed_pool.stop()

        if seed_thread is not None:
            seed_thread.join(timeout=10.0)

        tracker.stop()

        if seed_error_holder:
            raise IntegratedDemoError(
                f"Seeder pool failed: {seed_error_holder[0]}"
            )

        if seed_thread is not None and seed_thread.is_alive():
            raise IntegratedDemoError(
                "Seeder pool did not stop gracefully"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Tracker, two Seeder workers, and two Leecher workers."
    )
    parser.add_argument(
        "--workspace",
        default="demo_workspace_integrated",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    result = run_integrated_demo(
        args.workspace,
        reset=args.reset,
    )

    print(f"Tracker URL: {result['tracker_url']}")
    print(
        f"Completed torrents: {len(result['leecher_results'])}"
    )
    print(
        f"Verified files: "
        f"{len(result['verification']['verified_files'])}"
    )

    for worker in result["leecher_results"]:
        print(
            f"{worker.name}: bytes={worker.bytes_downloaded} "
            f"pieces={worker.pieces_downloaded} "
            f"cycles={worker.announce_cycles}"
        )

    print("Tracker -> Seeder -> Leecher lifecycle: PASS")
    print("Single-file and multi-file transfer: PASS")
    print("Completed and stopped Tracker events: PASS")
    print("Byte-for-byte verification: PASS")
    print("INTEGRATED DEMO RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
