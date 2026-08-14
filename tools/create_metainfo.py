from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable, Sequence


class MetainfoGenerationError(ValueError):
    """Raised when Metainfo generation cannot be completed."""


def calculate_piece_hashes(
    file_paths: Sequence[Path],
    piece_length: int,
) -> list[str]:
    """
    Hash a virtual byte stream formed by concatenating all files in order.

    This intentionally allows a piece to begin in one file and finish in the
    next file, which is required for multi-file torrents.
    """
    if piece_length <= 0:
        raise MetainfoGenerationError("piece_length must be positive")

    piece_hashes: list[str] = []
    buffer = bytearray()

    for file_path in file_paths:
        try:
            with file_path.open("rb") as file:
                while True:
                    chunk = file.read(64 * 1024)
                    if not chunk:
                        break

                    buffer.extend(chunk)

                    while len(buffer) >= piece_length:
                        piece = bytes(buffer[:piece_length])
                        del buffer[:piece_length]
                        piece_hashes.append(hashlib.sha1(piece).hexdigest())
        except OSError as exc:
            raise MetainfoGenerationError(
                f"Could not read {file_path}: {exc}"
            ) from exc

    if buffer:
        piece_hashes.append(hashlib.sha1(bytes(buffer)).hexdigest())

    return piece_hashes


def create_metainfo(
    input_paths: Iterable[str | Path],
    tracker_url: str,
    output_path: str | Path,
    piece_length: int = 262144,
    torrent_name: str | None = None,
) -> dict:
    paths = [Path(path).resolve() for path in input_paths]

    if not paths:
        raise MetainfoGenerationError("At least one input file is required")

    if not tracker_url or not tracker_url.strip():
        raise MetainfoGenerationError("tracker_url must not be empty")

    if piece_length <= 0:
        raise MetainfoGenerationError("piece_length must be positive")

    for path in paths:
        if not path.exists():
            raise MetainfoGenerationError(f"Input file does not exist: {path}")
        if not path.is_file():
            raise MetainfoGenerationError(f"Input path is not a file: {path}")

    piece_hashes = calculate_piece_hashes(paths, piece_length)

    if len(paths) == 1:
        source = paths[0]
        info = {
            "name": torrent_name or source.name,
            "length": source.stat().st_size,
            "piece length": piece_length,
            "pieces": piece_hashes,
        }
    else:
        if not torrent_name or not torrent_name.strip():
            raise MetainfoGenerationError(
                "--name is required when sharing multiple files"
            )

        file_entries = [
            {
                "length": path.stat().st_size,
                "path": [path.name],
            }
            for path in paths
        ]

        info = {
            "name": torrent_name,
            "files": file_entries,
            "piece length": piece_length,
            "pieces": piece_hashes,
        }

    metainfo = {
        "announce": tracker_url,
        "info": info,
    }

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_suffix(destination.suffix + ".tmp")

    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(
                metainfo,
                file,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            file.write("\n")
        temporary.replace(destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise MetainfoGenerationError(
            f"Could not write {destination}: {exc}"
        ) from exc

    return metainfo


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a human-readable JSON Metainfo file."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="One or more files to share, in concatenation order",
    )
    parser.add_argument(
        "--tracker",
        required=True,
        help="Tracker announce URL",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output .torrent.json path",
    )
    parser.add_argument(
        "--piece-length",
        type=int,
        default=262144,
        help="Piece length in bytes (default: 262144)",
    )
    parser.add_argument(
        "--name",
        help="Torrent name; required for multiple input files",
    )
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()

    try:
        metainfo = create_metainfo(
            input_paths=args.inputs,
            tracker_url=args.tracker,
            output_path=args.output,
            piece_length=args.piece_length,
            torrent_name=args.name,
        )
    except MetainfoGenerationError as exc:
        parser.error(str(exc))
        return 2

    info = metainfo["info"]
    total_length = (
        info["length"]
        if "length" in info
        else sum(entry["length"] for entry in info["files"])
    )

    print(f"Created: {Path(args.output)}")
    print(f"Mode: {'multi-file' if 'files' in info else 'single-file'}")
    print(f"Total bytes: {total_length}")
    print(f"Piece length: {info['piece length']}")
    print(f"Piece count: {len(info['pieces'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
