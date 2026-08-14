from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from peer.metainfo import Metainfo
from peer.piece_manager import PieceManager


class DemoVerificationError(RuntimeError):
    """Raised when the manual demo output is incomplete or corrupted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            chunk = file.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_demo_workspace(
    workspace: str | Path,
) -> dict[str, Any]:
    workspace_path = Path(workspace).resolve()
    manifest_path = workspace_path / "manifest.json"

    if not manifest_path.is_file():
        raise DemoVerificationError(
            f"manifest not found: {manifest_path}"
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    verified_files: list[dict[str, Any]] = []

    for entry in manifest["files"]:
        source = workspace_path / entry["source"]
        leecher = workspace_path / entry["leecher"]

        if not source.is_file():
            raise DemoVerificationError(
                f"source file is missing: {source}"
            )

        if not leecher.is_file():
            raise DemoVerificationError(
                f"downloaded file is missing: {leecher}"
            )

        source_hash = _sha256(source)
        leecher_hash = _sha256(leecher)

        if source_hash != entry["sha256"]:
            raise DemoVerificationError(
                f"source SHA-256 changed: {source}"
            )

        if leecher_hash != source_hash:
            raise DemoVerificationError(
                f"downloaded file does not match source: {leecher}"
            )

        if leecher.stat().st_size != entry["size"]:
            raise DemoVerificationError(
                f"downloaded size mismatch: {leecher}"
            )

        verified_files.append(
            {
                "path": str(leecher),
                "bytes": leecher.stat().st_size,
                "sha256": leecher_hash,
            }
        )

    torrent_summaries = []

    for torrent_relative in manifest["torrents"]:
        torrent_path = workspace_path / torrent_relative
        metainfo = Metainfo(torrent_path).load()
        storage_root = (
            workspace_path
            / manifest["leecher_storage"]
            / torrent_path.stem
        )
        manager = PieceManager(metainfo, storage_root)

        if not manager.is_complete():
            raise DemoVerificationError(
                f"PieceManager reports incomplete torrent: {torrent_path}"
            )

        torrent_summaries.append(
            {
                "name": metainfo.name,
                "piece_count": metainfo.piece_count,
                "total_length": metainfo.total_length,
                "info_hash": metainfo.info_hash_hex,
            }
        )

    return {
        "workspace": workspace_path,
        "verified_files": verified_files,
        "torrents": torrent_summaries,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the manual Seeder-to-Leecher demo."
    )
    parser.add_argument(
        "--workspace",
        default="demo_workspace",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        result = verify_demo_workspace(args.workspace)
    except DemoVerificationError as exc:
        print(f"DEMO VERIFICATION RESULT: FAIL")
        print(str(exc))
        return 1

    print(f"Workspace: {result['workspace']}")
    print(f"Verified files: {len(result['verified_files'])}")
    for torrent in result["torrents"]:
        print(
            f"Torrent {torrent['name']}: "
            f"bytes={torrent['total_length']} "
            f"pieces={torrent['piece_count']} "
            f"info_hash={torrent['info_hash']}"
        )
    print("Byte-for-byte source/download comparison: PASS")
    print("PieceManager completion scan: PASS")
    print("DEMO VERIFICATION RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
