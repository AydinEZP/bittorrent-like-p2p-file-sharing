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
import shutil
from typing import Any

from tools.create_metainfo import create_metainfo


DEFAULT_TRACKER = "http://127.0.0.1:8000/announce"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            chunk = file.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def prepare_demo_workspace(
    workspace: str | Path,
    *,
    tracker_url: str = DEFAULT_TRACKER,
    reset: bool = False,
) -> dict[str, Any]:
    """
    Create deterministic single-file and multi-file demo torrents.

    The directory layout is deliberately compatible with `peer.peer`:

        <workspace>/seed_storage/<torrent-file-stem>/<Metainfo paths>
        <workspace>/leecher_storage/<torrent-file-stem>/<Metainfo paths>
    """
    workspace_path = Path(workspace).resolve()

    if reset and workspace_path.exists():
        shutil.rmtree(workspace_path)

    torrents_dir = workspace_path / "torrents"
    seed_storage = workspace_path / "seed_storage"
    leecher_storage = workspace_path / "leecher_storage"
    logs_dir = workspace_path / "logs"

    for directory in (
        torrents_dir,
        seed_storage,
        leecher_storage,
        logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    single_torrent = torrents_dir / "demo_single.torrent.json"
    multi_torrent = torrents_dir / "demo_multi.torrent.json"

    single_root = seed_storage / single_torrent.stem
    multi_root = seed_storage / multi_torrent.stem
    single_root.mkdir(parents=True, exist_ok=True)
    (multi_root / "demo_bundle").mkdir(parents=True, exist_ok=True)

    single_data = bytes(
        (index * 29 + 17) % 256
        for index in range(4099)
    )
    intro_data = (
        "Data Networks BitTorrent final project demo.\n"
        "This text is intentionally deterministic.\n"
    ).encode("utf-8")
    payload_data = bytes(
        (index * 41 + 5) % 256
        for index in range(3077)
    )

    single_file = single_root / "demo_single.bin"
    intro_file = multi_root / "demo_bundle" / "intro.txt"
    payload_file = multi_root / "demo_bundle" / "payload.bin"

    single_file.write_bytes(single_data)
    intro_file.write_bytes(intro_data)
    payload_file.write_bytes(payload_data)

    create_metainfo(
        [single_file],
        tracker_url=tracker_url,
        output_path=single_torrent,
        piece_length=512,
    )

    create_metainfo(
        [intro_file, payload_file],
        tracker_url=tracker_url,
        output_path=multi_torrent,
        piece_length=384,
        torrent_name="demo_bundle",
    )

    # Clear the old downloads.
    if leecher_storage.exists():
        shutil.rmtree(leecher_storage)
    leecher_storage.mkdir(parents=True, exist_ok=True)

    files = [
        {
            "torrent": single_torrent.name,
            "mode": "single-file",
            "source": str(single_file.relative_to(workspace_path)),
            "leecher": str(
                (
                    leecher_storage
                    / single_torrent.stem
                    / "demo_single.bin"
                ).relative_to(workspace_path)
            ),
            "size": single_file.stat().st_size,
            "sha256": _sha256(single_file),
        },
        {
            "torrent": multi_torrent.name,
            "mode": "multi-file",
            "source": str(intro_file.relative_to(workspace_path)),
            "leecher": str(
                (
                    leecher_storage
                    / multi_torrent.stem
                    / "demo_bundle"
                    / "intro.txt"
                ).relative_to(workspace_path)
            ),
            "size": intro_file.stat().st_size,
            "sha256": _sha256(intro_file),
        },
        {
            "torrent": multi_torrent.name,
            "mode": "multi-file",
            "source": str(payload_file.relative_to(workspace_path)),
            "leecher": str(
                (
                    leecher_storage
                    / multi_torrent.stem
                    / "demo_bundle"
                    / "payload.bin"
                ).relative_to(workspace_path)
            ),
            "size": payload_file.stat().st_size,
            "sha256": _sha256(payload_file),
        },
    ]

    manifest = {
        "tracker_url": tracker_url,
        "torrents": [
            str(single_torrent.relative_to(workspace_path)),
            str(multi_torrent.relative_to(workspace_path)),
        ],
        "seed_storage": str(seed_storage.relative_to(workspace_path)),
        "leecher_storage": str(leecher_storage.relative_to(workspace_path)),
        "files": files,
    }

    manifest_path = workspace_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    return {
        "workspace": workspace_path,
        "manifest_path": manifest_path,
        "single_torrent": single_torrent,
        "multi_torrent": multi_torrent,
        "seed_storage": seed_storage,
        "leecher_storage": leecher_storage,
        "logs_dir": logs_dir,
        "manifest": manifest,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare deterministic data for the manual BitTorrent demo."
    )
    parser.add_argument(
        "--workspace",
        default="demo_workspace",
        help="Demo workspace directory",
    )
    parser.add_argument(
        "--tracker",
        default=DEFAULT_TRACKER,
        help="Tracker announce URL written into Metainfo files",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete and recreate the demo workspace",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    result = prepare_demo_workspace(
        args.workspace,
        tracker_url=args.tracker,
        reset=args.reset,
    )

    manifest = result["manifest"]

    print(f"Workspace: {result['workspace']}")
    print(f"Tracker URL: {manifest['tracker_url']}")
    print("Torrents:")
    for torrent in manifest["torrents"]:
        print(f"  - {torrent}")
    print(f"Files in manifest: {len(manifest['files'])}")
    print("DEMO PREPARATION RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
