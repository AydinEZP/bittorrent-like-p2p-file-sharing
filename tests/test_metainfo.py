from __future__ import annotations

import hashlib
import json
from pathlib import Path

from peer.metainfo import Metainfo, MetainfoError
from tools.create_metainfo import create_metainfo


ROOT = Path(__file__).resolve().parent.parent
TEST_DATA = ROOT / "test_data"
TORRENTS = ROOT / "peer" / "torrents"
TRACKER_URL = "http://127.0.0.1:8000/announce"
PIECE_LENGTH = 128


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def write_test_files() -> tuple[Path, Path, Path]:
    TEST_DATA.mkdir(parents=True, exist_ok=True)
    TORRENTS.mkdir(parents=True, exist_ok=True)

    single_path = TEST_DATA / "sample_single.bin"
    first_path = TEST_DATA / "file1.bin"
    second_path = TEST_DATA / "file2.bin"

    single_data = bytes(range(256)) * 3 + b"END"
    first_data = bytes((index * 3) % 256 for index in range(150))
    second_data = bytes((255 - index) % 256 for index in range(170))

    single_path.write_bytes(single_data)
    first_path.write_bytes(first_data)
    second_path.write_bytes(second_data)

    return single_path, first_path, second_path


def expected_hashes(data: bytes, piece_length: int) -> list[str]:
    return [
        sha1_hex(data[offset:offset + piece_length])
        for offset in range(0, len(data), piece_length)
    ]


def assert_invalid_metainfo(
    path: Path,
    payload: dict,
    expected_message_part: str,
) -> None:
    path.write_text(
        json.dumps(payload, indent=2),
        encoding="utf-8",
    )

    try:
        Metainfo(path).load()
    except MetainfoError as exc:
        assert expected_message_part in str(exc), (
            f"Expected error containing {expected_message_part!r}, "
            f"got {str(exc)!r}"
        )
        return

    raise AssertionError(f"Expected MetainfoError for {path}")


def main() -> None:
    print("=== Metainfo tests ===")

    single_path, first_path, second_path = write_test_files()

    single_torrent = TORRENTS / "sample_single.torrent.json"
    multi_torrent = TORRENTS / "sample_multi.torrent.json"

    create_metainfo(
        [single_path],
        tracker_url=TRACKER_URL,
        output_path=single_torrent,
        piece_length=PIECE_LENGTH,
    )

    create_metainfo(
        [first_path, second_path],
        tracker_url=TRACKER_URL,
        output_path=multi_torrent,
        piece_length=PIECE_LENGTH,
        torrent_name="sample_bundle",
    )

    single = Metainfo(single_torrent).load()
    multi = Metainfo(multi_torrent).load()

    single_data = single_path.read_bytes()
    combined_multi_data = first_path.read_bytes() + second_path.read_bytes()

    assert single.total_length == 771
    assert single.piece_count == 7
    assert single.piece_hashes == expected_hashes(
        single_data,
        PIECE_LENGTH,
    )
    assert not single.is_multi_file

    assert multi.total_length == 320
    assert multi.piece_count == 3
    assert multi.piece_hashes == expected_hashes(
        combined_multi_data,
        PIECE_LENGTH,
    )
    assert multi.is_multi_file

    boundary_piece = combined_multi_data[128:256]
    assert boundary_piece[:22] == first_path.read_bytes()[128:150]
    assert boundary_piece[22:] == second_path.read_bytes()[:106]
    assert multi.piece_hashes[1] == sha1_hex(boundary_piece)

    first_info_hash = single.info_hash_hex
    second_info_hash = Metainfo(single_torrent).load().info_hash_hex
    assert first_info_hash == second_info_hash
    assert len(bytes.fromhex(first_info_hash)) == 20

    invalid_dir = TEST_DATA / "invalid"
    invalid_dir.mkdir(exist_ok=True)

    announce_list_path = invalid_dir / "announce_list.json"
    announce_list_payload = json.loads(single_torrent.read_text(encoding="utf-8"))
    announce_list_payload["announce"] = [
        TRACKER_URL,
        "http://127.0.0.1:8001/announce",
    ]
    announce_list_path.write_text(
        json.dumps(announce_list_payload, indent=2),
        encoding="utf-8",
    )
    announce_list = Metainfo(announce_list_path).load()
    assert announce_list.announce == TRACKER_URL
    assert len(announce_list.announce_urls) == 2

    assert_invalid_metainfo(
        invalid_dir / "missing_announce.json",
        {
            "info": {
                "name": "x.bin",
                "length": 1,
                "piece length": 128,
                "pieces": [sha1_hex(b"x")],
            }
        },
        "'announce'",
    )

    assert_invalid_metainfo(
        invalid_dir / "wrong_piece_count.json",
        {
            "announce": TRACKER_URL,
            "info": {
                "name": "x.bin",
                "length": 200,
                "piece length": 128,
                "pieces": [sha1_hex(b"x" * 128)],
            },
        },
        "Piece count mismatch",
    )


    concatenated_path = invalid_dir / "concatenated_pieces.json"
    concatenated_payload = json.loads(single_torrent.read_text(encoding="utf-8"))
    concatenated_payload["info"]["pieces"] = "".join(
        concatenated_payload["info"]["pieces"]
    )
    concatenated_path.write_text(
        json.dumps(concatenated_payload, indent=2),
        encoding="utf-8",
    )
    concatenated = Metainfo(concatenated_path).load()
    assert concatenated.piece_hashes == single.piece_hashes

    assert_invalid_metainfo(
        invalid_dir / "unsafe_single_name.json",
        {
            "announce": TRACKER_URL,
            "info": {
                "name": "../outside.bin",
                "length": 1,
                "piece length": 128,
                "pieces": [sha1_hex(b"x")],
            },
        },
        "safe relative path component",
    )

    assert_invalid_metainfo(
        invalid_dir / "unsafe_multi_path.json",
        {
            "announce": TRACKER_URL,
            "info": {
                "name": "bundle",
                "files": [
                    {"length": 1, "path": ["..", "outside.bin"]}
                ],
                "piece length": 128,
                "pieces": [sha1_hex(b"x")],
            },
        },
        "safe relative path component",
    )

    assert_invalid_metainfo(
        invalid_dir / "bad_hash.json",
        {
            "announce": TRACKER_URL,
            "info": {
                "name": "x.bin",
                "length": 1,
                "piece length": 128,
                "pieces": ["not-a-valid-sha1"],
            },
        },
        "40 hex characters",
    )

    print(
        "Single-file: "
        f"bytes={single.total_length}, "
        f"pieces={single.piece_count}, "
        f"info_hash={single.info_hash_hex}"
    )
    print(
        "Multi-file: "
        f"bytes={multi.total_length}, "
        f"pieces={multi.piece_count}, "
        f"info_hash={multi.info_hash_hex}"
    )
    print("Single-file piece hashes: PASS")
    print("Multi-file cross-boundary hashing: PASS")
    print("Metainfo list/concatenated pieces and safe paths: PASS")
    print("Metainfo validation tests: PASS")
    print("Stable 20-byte info_hash: PASS")
    print("METAINFO TESTS RESULT: PASS")


def test_metainfo_component() -> None:
    main()

if __name__ == "__main__":
    main()
