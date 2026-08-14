from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil
import time
from urllib.error import HTTPError
from urllib.parse import quote_from_bytes, urlencode
from urllib.request import Request, urlopen

from common.bencode import bdecode
from tracker.tracker_server import TrackerRuntime


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test_output" / "tracker_http"


def make_info_hash(label: str) -> bytes:
    return hashlib.sha1(label.encode("utf-8")).digest()


def make_peer_id(index: int) -> bytes:
    return f"H{index:019d}".encode("ascii")


def make_announce_url(
    base_url: str,
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int,
    downloaded: int,
    left: int,
    event: str | None = None,
) -> str:
    fields = [
        ("info_hash", quote_from_bytes(info_hash, safe="")),
        ("peer_id", quote_from_bytes(peer_id, safe="")),
        ("port", str(port)),
        ("uploaded", str(uploaded)),
        ("downloaded", str(downloaded)),
        ("left", str(left)),
    ]

    if event is not None:
        fields.append(("event", event))

    query = "&".join(f"{name}={value}" for name, value in fields)
    return f"{base_url}/announce?{query}"


def fetch_bencoded(url: str) -> tuple[int, str, dict[bytes, object]]:
    request = Request(url, method="GET")

    try:
        with urlopen(request, timeout=5) as response:
            status = response.status
            content_type = response.headers.get_content_type()
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        content_type = exc.headers.get_content_type()
        body = exc.read()

    decoded = bdecode(body)

    assert isinstance(decoded, dict)
    return status, content_type, decoded


def announce(
    base_url: str,
    *,
    info_hash: bytes,
    peer_id: bytes,
    port: int,
    uploaded: int = 0,
    downloaded: int = 0,
    left: int = 1000,
    event: str | None = "started",
) -> dict[bytes, object]:
    url = make_announce_url(
        base_url,
        info_hash=info_hash,
        peer_id=peer_id,
        port=port,
        uploaded=uploaded,
        downloaded=downloaded,
        left=left,
        event=event,
    )

    status, content_type, payload = fetch_bencoded(url)

    assert status == 200
    assert content_type == "text/plain"
    assert b"failure reason" not in payload

    return payload


def register_peer_concurrently(
    base_url: str,
    info_hash: bytes,
    index: int,
) -> None:
    payload = announce(
        base_url,
        info_hash=info_hash,
        peer_id=make_peer_id(index),
        port=12000 + index,
        left=0 if index % 4 == 0 else 100,
    )
    assert payload[b"interval"] == 17


def main() -> None:
    print("=== Tracker HTTP Server tests ===")

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    runtime = TrackerRuntime(
        host="127.0.0.1",
        port=0,
        announce_interval=17,
        peer_timeout=60.0,
        cleanup_interval=0.05,
        log_path=OUTPUT / "tracker.log",
    )

    runtime.start()

    try:
        base_url = runtime.base_url
        torrent_hash = bytes(
            [0, 1, 2, 3, 4, 5, 37, 38, 61, 43, 47, 127, 128, 200, 255, 9, 10, 11, 12, 13]
        )
        peer_1 = make_peer_id(1)
        peer_2 = make_peer_id(2)

        first = announce(
            base_url,
            info_hash=torrent_hash,
            peer_id=peer_1,
            port=6881,
            left=1000,
            event="started",
        )
        assert first[b"interval"] == 17
        assert first[b"complete"] == 0
        assert first[b"incomplete"] == 1
        assert first[b"peers"] == []

        second = announce(
            base_url,
            info_hash=torrent_hash,
            peer_id=peer_2,
            port=6882,
            uploaded=1000,
            downloaded=1000,
            left=0,
            event="started",
        )
        assert second[b"complete"] == 1
        assert second[b"incomplete"] == 1
        assert second[b"peers"] == [
            {
                b"peer_id": peer_1,
                b"ip": b"127.0.0.1",
                b"port": 6881,
            }
        ]

        periodic = announce(
            base_url,
            info_hash=torrent_hash,
            peer_id=peer_1,
            port=6881,
            downloaded=500,
            left=500,
            event=None,
        )
        assert periodic[b"peers"] == [
            {
                b"peer_id": peer_2,
                b"ip": b"127.0.0.1",
                b"port": 6882,
            }
        ]

        completed = announce(
            base_url,
            info_hash=torrent_hash,
            peer_id=peer_1,
            port=6881,
            downloaded=1000,
            left=500,
            event="completed",
        )
        assert completed[b"complete"] == 2
        assert completed[b"incomplete"] == 0

        stopped = announce(
            base_url,
            info_hash=torrent_hash,
            peer_id=peer_2,
            port=6882,
            uploaded=1000,
            downloaded=1000,
            left=0,
            event="stopped",
        )
        assert stopped[b"complete"] == 1
        assert stopped[b"incomplete"] == 0
        assert stopped[b"peers"] == [
            {
                b"peer_id": peer_1,
                b"ip": b"127.0.0.1",
                b"port": 6881,
            }
        ]

        missing_port_url = (
            f"{base_url}/announce?"
            f"info_hash={quote_from_bytes(torrent_hash, safe='')}&"
            f"peer_id={quote_from_bytes(peer_1, safe='')}&"
            "uploaded=0&downloaded=0&left=0&event=started"
        )
        status, content_type, failure = fetch_bencoded(missing_port_url)
        assert status == 200
        assert content_type == "text/plain"
        assert set(failure) == {b"failure reason"}
        assert b"missing parameter: port" in failure[b"failure reason"]

        duplicate_url = (
            f"{base_url}/announce?"
            f"info_hash={quote_from_bytes(torrent_hash, safe='')}&"
            f"peer_id={quote_from_bytes(peer_1, safe='')}&"
            "port=6881&port=6882&uploaded=0&downloaded=0&left=0"
        )
        status, _, duplicate_failure = fetch_bencoded(duplicate_url)
        assert status == 200
        assert set(duplicate_failure) == {b"failure reason"}
        assert b"duplicate parameter: port" in duplicate_failure[b"failure reason"]

        status, _, wrong_endpoint = fetch_bencoded(base_url + "/wrong")
        assert status == 404
        assert set(wrong_endpoint) == {b"failure reason"}

        concurrent_hash = make_info_hash("http-concurrent")
        peer_total = 48

        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [
                executor.submit(
                    register_peer_concurrently,
                    base_url,
                    concurrent_hash,
                    index,
                )
                for index in range(peer_total)
            ]

            for future in futures:
                future.result()

        stats = runtime.state.get_statistics(concurrent_hash)
        expected_complete = len(
            [index for index in range(peer_total) if index % 4 == 0]
        )
        assert stats == {
            "complete": expected_complete,
            "incomplete": peer_total - expected_complete,
        }

        expiring_hash = make_info_hash("expires")
        expiring_peer = make_peer_id(900)

        # Register before switching to the short timeout.
        announce(
            base_url,
            info_hash=expiring_hash,
            peer_id=expiring_peer,
            port=16900,
            left=100,
            event="started",
        )
        assert runtime.state.peer_count(expiring_hash) == 1
        runtime.peer_timeout = 0.35

        deadline = time.monotonic() + 3.0
        while (
            runtime.state.peer_count(expiring_hash) != 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)

        assert runtime.state.peer_count(expiring_hash) == 0

        event_types = {
            event["event_type"]
            for event in runtime.logger.read_events()
        }
        assert {
            "TRACKER_STARTED",
            "ANNOUNCE_RECEIVED",
            "HTTP_RESPONSE",
            "INVALID_REQUEST",
            "PEER_REGISTERED",
            "PEER_UPDATED",
            "PEER_COMPLETED",
            "PEER_STOPPED",
            "PEER_EXPIRED",
        }.issubset(event_types)

        print(f"Tracker test URL: {runtime.announce_url}")
        print(
            "Binary query parameters: "
            "20-byte info_hash and peer_id preserved"
        )
        print(
            "Concurrent HTTP swarm statistics: "
            f"complete={stats['complete']}, "
            f"incomplete={stats['incomplete']}"
        )
        print("Multithreaded GET /announce service: PASS")
        print("Bencoded text/plain responses: PASS")
        print("Started, periodic, completed, stopped: PASS")
        print("Peer list and swarm statistics: PASS")
        print("Failure-reason responses: PASS")
        print("Binary URL decoding: PASS")
        print("Concurrent HTTP requests: PASS")
        print("Background expired-peer cleanup: PASS")
        print("HTTP and Tracker event logging: PASS")
        print("TRACKER HTTP TESTS RESULT: PASS")

    finally:
        runtime.stop()


def test_tracker_http_component() -> None:
    main()

if __name__ == "__main__":
    main()
