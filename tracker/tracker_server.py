from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

from common.bencode import bencode
from common.event_logger import EventLogger
from tracker.tracker_state import TrackerState, TrackerStateError


class TrackerRequestError(ValueError):
    """Raised when an HTTP announce request is malformed."""


def _decode_query_component(raw_value: str) -> bytes:
    """
    Decode one CGI query component to bytes.

    A literal '+' is interpreted as a space. Binary values such as info_hash
    and peer_id should therefore encode '+' as %2B.
    """
    return unquote_to_bytes(raw_value.replace("+", " "))


def parse_announce_query(raw_query: str) -> dict[str, bytes]:
    """
    Parse a raw query string without converting binary values to Unicode.

    Duplicate parameters are rejected to keep request interpretation
    deterministic.
    """
    parameters: dict[str, bytes] = {}

    if not raw_query:
        return parameters

    for component in raw_query.split("&"):
        if not component:
            continue

        raw_name, separator, raw_value = component.partition("=")

        if not separator:
            raise TrackerRequestError(
                f"query parameter is missing '=': {component!r}"
            )

        try:
            name = _decode_query_component(raw_name).decode("ascii")
        except UnicodeDecodeError as exc:
            raise TrackerRequestError(
                "query parameter names must be ASCII"
            ) from exc

        if not name:
            raise TrackerRequestError("query parameter name must not be empty")

        if name in parameters:
            raise TrackerRequestError(f"duplicate parameter: {name}")

        parameters[name] = _decode_query_component(raw_value)

    return parameters


def _require_bytes(
    parameters: dict[str, bytes],
    name: str,
    *,
    exact_length: int | None = None,
) -> bytes:
    if name not in parameters:
        raise TrackerRequestError(f"missing parameter: {name}")

    value = parameters[name]

    if exact_length is not None and len(value) != exact_length:
        raise TrackerRequestError(
            f"{name} must be exactly {exact_length} bytes"
        )

    return value


def _require_non_negative_integer(
    parameters: dict[str, bytes],
    name: str,
) -> int:
    raw_value = _require_bytes(parameters, name)

    try:
        text = raw_value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TrackerRequestError(f"{name} must contain ASCII digits") from exc

    if not text or not text.isdigit():
        raise TrackerRequestError(
            f"{name} must be a non-negative base-10 integer"
        )

    return int(text, 10)


def parse_announce_parameters(raw_query: str) -> dict[str, Any]:
    parameters = parse_announce_query(raw_query)

    allowed = {
        "info_hash",
        "peer_id",
        "port",
        "uploaded",
        "downloaded",
        "left",
        "event",
    }

    unknown = sorted(set(parameters) - allowed)
    if unknown:
        raise TrackerRequestError(
            "unknown parameter(s): " + ", ".join(unknown)
        )

    info_hash = _require_bytes(
        parameters,
        "info_hash",
        exact_length=20,
    )
    peer_id = _require_bytes(
        parameters,
        "peer_id",
        exact_length=20,
    )

    port = _require_non_negative_integer(parameters, "port")
    uploaded = _require_non_negative_integer(parameters, "uploaded")
    downloaded = _require_non_negative_integer(parameters, "downloaded")
    left = _require_non_negative_integer(parameters, "left")

    raw_event = parameters.get("event", b"")

    try:
        event = raw_event.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TrackerRequestError("event must be ASCII") from exc

    if event not in TrackerState.VALID_EVENTS:
        raise TrackerRequestError(
            "event must be one of: started, completed, stopped, or empty"
        )

    return {
        "info_hash": info_hash,
        "peer_id": peer_id,
        "port": port,
        "uploaded": uploaded,
        "downloaded": downloaded,
        "left": left,
        "event": event,
    }


class TrackerHTTPServer(ThreadingHTTPServer):
    """Multithreaded HTTP server carrying Tracker application state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        state: TrackerState,
        logger: EventLogger,
        announce_interval: int,
    ):
        if announce_interval <= 0:
            raise ValueError("announce_interval must be positive")

        self.tracker_state = state
        self.event_logger = logger
        self.announce_interval = announce_interval

        super().__init__(server_address, TrackerRequestHandler)


class TrackerRequestHandler(BaseHTTPRequestHandler):
    """Handle GET /announce requests and return Bencoded dictionaries."""

    server: TrackerHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        request_url = urlsplit(self.path)

        if request_url.path != "/announce":
            self._send_failure(
                "unknown endpoint; expected /announce",
                http_status=404,
            )
            return

        self.server.event_logger.log(
            "ANNOUNCE_RECEIVED",
            (
                f"client={self.client_address[0]}:{self.client_address[1]} "
                f"path={request_url.path}"
            ),
        )

        try:
            request = parse_announce_parameters(request_url.query)

            state_response = self.server.tracker_state.announce(
                info_hash=request["info_hash"],
                peer_id=request["peer_id"],
                ip=self.client_address[0],
                port=request["port"],
                uploaded=request["uploaded"],
                downloaded=request["downloaded"],
                left=request["left"],
                event=request["event"],
            )

            response = {
                "interval": self.server.announce_interval,
                "complete": state_response["complete"],
                "incomplete": state_response["incomplete"],
                "peers": state_response["peers"],
            }

            self._send_bencoded(response, http_status=200)

        except (TrackerRequestError, TrackerStateError) as exc:
            self.server.event_logger.log(
                "INVALID_REQUEST",
                (
                    f"client={self.client_address[0]}:{self.client_address[1]} "
                    f"reason={exc}"
                ),
            )
            # Tracker errors still use a normal response.
            self._send_failure(str(exc), http_status=200)

        except Exception as exc:
            self.server.event_logger.log(
                "TRACKER_ERROR",
                (
                    f"client={self.client_address[0]}:{self.client_address[1]} "
                    f"error={type(exc).__name__}: {exc}"
                ),
            )
            self._send_failure(
                "internal tracker error",
                http_status=500,
            )

    def do_POST(self) -> None:
        self._send_failure(
            "method not allowed; use GET",
            http_status=405,
        )

    def _send_failure(
        self,
        reason: str,
        *,
        http_status: int,
    ) -> None:
        self._send_bencoded(
            {"failure reason": reason},
            http_status=http_status,
        )

    def _send_bencoded(
        self,
        payload: dict[str, Any],
        *,
        http_status: int,
    ) -> None:
        body = bencode(payload)

        self.send_response(http_status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

        self.server.event_logger.log(
            "HTTP_RESPONSE",
            (
                f"client={self.client_address[0]}:{self.client_address[1]} "
                f"status={http_status} bytes={len(body)}"
            ),
        )

    def log_message(self, format: str, *args: Any) -> None:
        self.server.event_logger.log(
            "HTTP_ACCESS",
            (
                f"client={self.client_address[0]}:{self.client_address[1]} "
                f"message={format % args}"
            ),
        )


class TrackerRuntime:
    """
    Manage the multithreaded HTTP server and expired-peer cleanup thread.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        announce_interval: int = 30,
        peer_timeout: float = 90.0,
        cleanup_interval: float = 30.0,
        log_path: str | Path = "tracker/logs/tracker.log",
    ):
        if peer_timeout <= 0:
            raise ValueError("peer_timeout must be positive")
        if cleanup_interval <= 0:
            raise ValueError("cleanup_interval must be positive")

        self.logger = EventLogger(log_path)
        self.state = TrackerState(logger=self.logger)
        self.server = TrackerHTTPServer(
            (host, port),
            state=self.state,
            logger=self.logger,
            announce_interval=announce_interval,
        )

        self.peer_timeout = float(peer_timeout)
        self.cleanup_interval = float(cleanup_interval)

        self._stop_event = threading.Event()
        self._server_thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._started = False
        self._lifecycle_lock = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    @property
    def base_url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    @property
    def announce_url(self) -> str:
        return self.base_url + "/announce"

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return

            self._stop_event.clear()

            self._server_thread = threading.Thread(
                target=self.server.serve_forever,
                name="tracker-http-server",
                daemon=True,
            )
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="tracker-cleanup",
                daemon=True,
            )

            self._server_thread.start()
            self._cleanup_thread.start()
            self._started = True

            host, port = self.address
            self.logger.log(
                "TRACKER_STARTED",
                (
                    f"listening={host}:{port} "
                    f"announce_interval={self.server.announce_interval} "
                    f"peer_timeout={self.peer_timeout}"
                ),
            )

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return

            self._stop_event.set()
            self.server.shutdown()
            self.server.server_close()

            if self._server_thread is not None:
                self._server_thread.join(timeout=5)

            if self._cleanup_thread is not None:
                self._cleanup_thread.join(timeout=5)

            self.logger.log(
                "TRACKER_STOPPED",
                "HTTP server and cleanup thread stopped",
            )

            self._started = False

    def _cleanup_loop(self) -> None:
        while not self._stop_event.wait(self.cleanup_interval):
            try:
                expired = self.state.remove_expired_peers(
                    self.peer_timeout
                )

                if expired:
                    self.logger.log(
                        "CLEANUP_COMPLETED",
                        f"expired_peer_count={len(expired)}",
                    )

            except Exception as exc:
                self.logger.log(
                    "CLEANUP_ERROR",
                    f"{type(exc).__name__}: {exc}",
                )

    def __enter__(self) -> "TrackerRuntime":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the multithreaded BitTorrent Tracker HTTP service."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--peer-timeout", type=float, default=90.0)
    parser.add_argument("--cleanup-interval", type=float, default=30.0)
    parser.add_argument(
        "--log",
        default="tracker/logs/tracker.log",
        help="Tracker log path",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()

    runtime = TrackerRuntime(
        host=args.host,
        port=args.port,
        announce_interval=args.interval,
        peer_timeout=args.peer_timeout,
        cleanup_interval=args.cleanup_interval,
        log_path=args.log,
    )

    runtime.start()

    print(f"Tracker listening on {runtime.announce_url}")
    print("Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping Tracker...")
    finally:
        runtime.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
