from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Iterable

from common.event_logger import EventLogger
from peer.metainfo import Metainfo
from peer.peer_server import PeerServer
from peer.piece_client import PieceClient
from peer.piece_manager import PieceManager
from peer.ping_client import ping_all_peers
from peer.tracker_client import TrackerClient, TrackerClientError, TrackerPeer
from peer.transfer_stats import TransferStats


@dataclass(frozen=True)
class TorrentJob:
    """Configuration for one torrent handled by the Peer thread pool."""

    torrent_path: Path
    storage_root: Path
    listen_port: int = 0

    def __init__(
        self,
        torrent_path: str | Path,
        storage_root: str | Path,
        listen_port: int = 0,
    ):
        object.__setattr__(self, "torrent_path", Path(torrent_path).resolve())
        object.__setattr__(self, "storage_root", Path(storage_root).resolve())
        object.__setattr__(self, "listen_port", listen_port)

        if isinstance(listen_port, bool) or not isinstance(listen_port, int):
            raise ValueError("listen_port must be an integer")
        if not 0 <= listen_port <= 65535:
            raise ValueError("listen_port must be between 0 and 65535")


@dataclass(frozen=True)
class TorrentWorkerResult:
    torrent_path: Path
    name: str
    info_hash: str
    thread_name: str
    server_port: int
    barrier_synchronized: bool
    started_sent: bool
    completed_sent: bool
    stopped_sent: bool
    announce_cycles: int
    periodic_announces: int
    discovered_peers: int
    successful_pings: int
    pieces_downloaded: int
    bytes_downloaded: int
    session_uploaded: int
    session_downloaded: int
    final_left: int
    complete: bool
    elapsed_seconds: float
    errors: tuple[str, ...]


class TorrentWorker:
    """
    Execute the complete lifecycle for one torrent.

    Lifecycle:
      1. Parse Metainfo.
      2. Scan local pieces.
      3. Start a TCP PeerServer for upload and Ping/Pong.
      4. Send `started` to the Tracker.
      5. Ping Tracker-discovered peers.
      6. Download and verify missing pieces.
      7. Send periodic announces while incomplete.
      8. Send `completed` when the download finishes.
      9. Send `stopped` during graceful shutdown.
    """

    def __init__(
        self,
        *,
        job: TorrentJob,
        peer_id: bytes,
        logger: EventLogger,
        stop_event: threading.Event,
        start_barrier: threading.Barrier | None = None,
        listen_host: str = "127.0.0.1",
        tracker_timeout: float = 5.0,
        peer_timeout: float = 3.0,
        max_cycles: int = 20,
        interval_cap: float | None = None,
        exit_on_complete: bool = True,
    ):
        if not isinstance(peer_id, bytes) or len(peer_id) != 20:
            raise ValueError("peer_id must be exactly 20 bytes")
        if tracker_timeout <= 0:
            raise ValueError("tracker_timeout must be positive")
        if peer_timeout <= 0:
            raise ValueError("peer_timeout must be positive")
        if isinstance(max_cycles, bool) or not isinstance(max_cycles, int):
            raise ValueError("max_cycles must be an integer")
        if max_cycles <= 0:
            raise ValueError("max_cycles must be positive")
        if interval_cap is not None and interval_cap <= 0:
            raise ValueError("interval_cap must be positive or None")

        self.job = job
        self.peer_id = peer_id
        self.logger = logger
        self.stop_event = stop_event
        self.start_barrier = start_barrier
        self.listen_host = listen_host
        self.tracker_timeout = float(tracker_timeout)
        self.peer_timeout = float(peer_timeout)
        self.max_cycles = max_cycles
        self.interval_cap = interval_cap
        self.exit_on_complete = bool(exit_on_complete)

    def run(self) -> TorrentWorkerResult:
        started_at = time.perf_counter()
        thread_name = threading.current_thread().name

        metainfo: Metainfo | None = None
        manager: PieceManager | None = None
        server: PeerServer | None = None
        tracker_client: TrackerClient | None = None
        transfer_stats = TransferStats()

        barrier_synchronized = False
        started_sent = False
        completed_sent = False
        stopped_sent = False
        announce_cycles = 0
        periodic_announces = 0
        discovered_peer_ids: set[bytes] = set()
        successful_pings = 0
        pieces_downloaded = 0
        bytes_downloaded = 0
        errors: list[str] = []
        was_incomplete = False

        self.logger.log(
            "TORRENT_WORKER_THREAD_STARTED",
            (
                f"thread={thread_name} torrent={self.job.torrent_path} "
                f"storage={self.job.storage_root}"
            ),
        )

        try:
            if self.start_barrier is not None:
                try:
                    self.start_barrier.wait(timeout=10.0)
                    barrier_synchronized = True
                    self.logger.log(
                        "TORRENT_WORKER_BARRIER_PASSED",
                        f"thread={thread_name}",
                    )
                except threading.BrokenBarrierError as exc:
                    raise RuntimeError(
                        "torrent worker start barrier was broken"
                    ) from exc
            else:
                barrier_synchronized = True

            metainfo = Metainfo(self.job.torrent_path).load()
            assert metainfo.info_hash is not None

            manager = PieceManager(
                metainfo,
                self.job.storage_root,
                create_missing=True,
                logger=self.logger,
            )
            was_incomplete = not manager.is_complete()

            server = PeerServer(
                host=self.listen_host,
                port=self.job.listen_port,
                peer_id=self.peer_id,
                logger=self.logger,
                piece_manager=manager,
                transfer_stats=transfer_stats,
                client_timeout=self.peer_timeout,
            )
            server.start()

            tracker_client = TrackerClient.from_metainfo(
                metainfo,
                peer_id=self.peer_id,
                port=server.port,
                timeout=self.tracker_timeout,
                logger=self.logger,
            )
            piece_client = PieceClient(
                local_peer_id=self.peer_id,
                info_hash=metainfo.info_hash,
                piece_manager=manager,
                transfer_stats=transfer_stats,
                timeout=self.peer_timeout,
                logger=self.logger,
            )

            def announce(event: str | None):
                session = transfer_stats.snapshot()
                return tracker_client.announce(
                    info_hash=metainfo.info_hash,
                    uploaded=session.uploaded,
                    downloaded=session.downloaded,
                    left=manager.left,
                    event=event,
                )

            response = announce("started")
            started_sent = True

            self.logger.log(
                "TORRENT_WORKER_STARTED",
                (
                    f"name={metainfo.name} info_hash={metainfo.info_hash_hex} "
                    f"port={server.port} left={manager.left}"
                ),
            )

            while not self.stop_event.is_set():
                announce_cycles += 1

                peers = self._unique_peers(response.peers)
                discovered_peer_ids.update(peer.peer_id for peer in peers)

                self.logger.log(
                    "TRACKER_PEERS_UPDATED",
                    (
                        f"cycle={announce_cycles} peer_count={len(peers)} "
                        f"complete={response.complete} "
                        f"incomplete={response.incomplete}"
                    ),
                )

                if peers:
                    ping_results = ping_all_peers(
                        peers,
                        local_peer_id=self.peer_id,
                        timeout=self.peer_timeout,
                        logger=self.logger,
                    )
                    successful = [
                        result for result in ping_results if result.success
                    ]
                    successful_pings += len(successful)

                    responsive_ids = {
                        result.peer_id for result in successful
                    }

                    for peer in peers:
                        if peer.peer_id not in responsive_ids:
                            continue
                        if manager.is_complete():
                            break

                        download_result = piece_client.download_from_peer(peer)
                        pieces_downloaded += download_result.downloaded
                        bytes_downloaded += download_result.bytes_downloaded
                        errors.extend(download_result.errors)

                session = transfer_stats.snapshot()
                self.logger.log(
                    "TORRENT_CYCLE_COMPLETED",
                    (
                        f"cycle={announce_cycles} "
                        f"session_uploaded={session.uploaded} "
                        f"session_downloaded={session.downloaded} "
                        f"local_valid_bytes={manager.downloaded_bytes} "
                        f"left={manager.left}"
                    ),
                )

                if manager.is_complete():
                    if was_incomplete and not completed_sent:
                        response = announce("completed")
                        completed_sent = True
                        self.logger.log(
                            "TORRENT_WORKER_COMPLETED",
                            (
                                f"name={metainfo.name} "
                                f"bytes={manager.downloaded_bytes}"
                            ),
                        )

                    if self.exit_on_complete:
                        break

                if announce_cycles >= self.max_cycles:
                    errors.append(
                        f"maximum announce cycles reached ({self.max_cycles})"
                    )
                    self.logger.log(
                        "TORRENT_WORKER_MAX_CYCLES",
                        f"cycles={announce_cycles} left={manager.left}",
                    )
                    break

                wait_seconds = float(response.interval)
                if self.interval_cap is not None:
                    wait_seconds = min(wait_seconds, self.interval_cap)

                if self.stop_event.wait(wait_seconds):
                    break

                response = announce(None)
                periodic_announces += 1

        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            self.logger.log(
                "TORRENT_WORKER_ERROR",
                f"{type(exc).__name__}: {exc}",
            )

        finally:
            if (
                tracker_client is not None
                and metainfo is not None
                and metainfo.info_hash is not None
                and manager is not None
                and started_sent
            ):
                try:
                    announce("stopped")
                    stopped_sent = True
                except TrackerClientError as exc:
                    errors.append(f"stopped announce failed: {exc}")
                    self.logger.log(
                        "TORRENT_STOPPED_ANNOUNCE_FAILED",
                        str(exc),
                    )

            if server is not None:
                server.stop()

            elapsed = time.perf_counter() - started_at
            final_left = manager.left if manager is not None else 0
            complete = manager.is_complete() if manager is not None else False
            name = (
                str(metainfo.name)
                if metainfo is not None and metainfo.name is not None
                else self.job.torrent_path.name
            )
            info_hash_hex = (
                metainfo.info_hash_hex
                if metainfo is not None and metainfo.info_hash is not None
                else ""
            )
            server_port = server.port if server is not None else 0
            final_session = transfer_stats.snapshot()

            self.logger.log(
                "TORRENT_WORKER_STOPPED",
                (
                    f"name={name} complete={complete} left={final_left} "
                    f"cycles={announce_cycles} elapsed={elapsed:.3f}"
                ),
            )

        return TorrentWorkerResult(
            torrent_path=self.job.torrent_path,
            name=name,
            info_hash=info_hash_hex,
            thread_name=thread_name,
            server_port=server_port,
            barrier_synchronized=barrier_synchronized,
            started_sent=started_sent,
            completed_sent=completed_sent,
            stopped_sent=stopped_sent,
            announce_cycles=announce_cycles,
            periodic_announces=periodic_announces,
            discovered_peers=len(discovered_peer_ids),
            successful_pings=successful_pings,
            pieces_downloaded=pieces_downloaded,
            bytes_downloaded=bytes_downloaded,
            session_uploaded=final_session.uploaded,
            session_downloaded=final_session.downloaded,
            final_left=final_left,
            complete=complete,
            elapsed_seconds=elapsed,
            errors=tuple(errors),
        )

    @staticmethod
    def _unique_peers(
        peers: Iterable[TrackerPeer],
    ) -> tuple[TrackerPeer, ...]:
        unique: dict[tuple[bytes, str, int], TrackerPeer] = {}

        for peer in peers:
            unique[(peer.peer_id, peer.ip, peer.port)] = peer

        return tuple(
            unique[key]
            for key in sorted(
                unique,
                key=lambda item: (item[0], item[1], item[2]),
            )
        )


class TorrentThreadPool:
    """Run one independent TorrentWorker thread per Metainfo file."""

    def __init__(
        self,
        *,
        peer_id: bytes,
        jobs: Iterable[TorrentJob],
        log_directory: str | Path,
        tracker_timeout: float = 5.0,
        peer_timeout: float = 3.0,
        max_cycles: int = 20,
        interval_cap: float | None = None,
        exit_on_complete: bool = True,
        application_logger: EventLogger | None = None,
    ):
        if not isinstance(peer_id, bytes) or len(peer_id) != 20:
            raise ValueError("peer_id must be exactly 20 bytes")

        self.peer_id = peer_id
        self.jobs = tuple(jobs)

        if not self.jobs:
            raise ValueError("at least one TorrentJob is required")

        self.log_directory = Path(log_directory).resolve()
        self.log_directory.mkdir(parents=True, exist_ok=True)
        self.tracker_timeout = tracker_timeout
        self.peer_timeout = peer_timeout
        self.max_cycles = max_cycles
        self.interval_cap = interval_cap
        self.exit_on_complete = exit_on_complete
        self.application_logger = application_logger
        self.stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._running = False

    def stop(self) -> None:
        self.stop_event.set()
        if self.application_logger is not None:
            self.application_logger.log(
                "TORRENT_THREAD_POOL_STOP_REQUESTED",
                "shared stop event set",
            )

    def run(self) -> tuple[TorrentWorkerResult, ...]:
        with self._run_lock:
            if self._running:
                raise RuntimeError("TorrentThreadPool is already running")
            self._running = True

        self.stop_event.clear()
        barrier = threading.Barrier(len(self.jobs))
        results: dict[int, TorrentWorkerResult] = {}

        if self.application_logger is not None:
            self.application_logger.log(
                "TORRENT_THREAD_POOL_STARTED",
                f"worker_count={len(self.jobs)}",
            )

        try:
            with ThreadPoolExecutor(
                max_workers=len(self.jobs),
                thread_name_prefix="torrent-worker",
            ) as executor:
                future_to_index = {}

                for index, job in enumerate(self.jobs):
                    safe_name = (
                        f"{index + 1:02d}_"
                        + "".join(
                            character
                            if character.isalnum() or character in "-_"
                            else "_"
                            for character in job.torrent_path.stem
                        )
                    )
                    worker_logger = EventLogger(
                        self.log_directory / f"{safe_name}.log"
                    )
                    worker = TorrentWorker(
                        job=job,
                        peer_id=self.peer_id,
                        logger=worker_logger,
                        stop_event=self.stop_event,
                        start_barrier=barrier,
                        tracker_timeout=self.tracker_timeout,
                        peer_timeout=self.peer_timeout,
                        max_cycles=self.max_cycles,
                        interval_cap=self.interval_cap,
                        exit_on_complete=self.exit_on_complete,
                    )
                    future = executor.submit(worker.run)
                    future_to_index[future] = index

                for future in as_completed(future_to_index):
                    index = future_to_index[future]
                    results[index] = future.result()

        finally:
            with self._run_lock:
                self._running = False

            if self.application_logger is not None:
                self.application_logger.log(
                    "TORRENT_THREAD_POOL_STOPPED",
                    f"result_count={len(results)}",
                )

        return tuple(results[index] for index in range(len(self.jobs)))
