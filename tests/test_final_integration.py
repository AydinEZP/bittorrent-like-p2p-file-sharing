from __future__ import annotations

from pathlib import Path
import shutil
import zipfile

from tools.build_submission import build_submission
from tools.end_to_end_demo import run_integrated_demo
from tools.prepare_demo import prepare_demo_workspace
from tools.submission_audit import audit_submission
from tools.verify_demo import verify_demo_workspace


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "test_output" / "final_integration"


def main() -> None:
    print("=== Final Integration and Submission tests ===")

    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    prepared_only = prepare_demo_workspace(
        OUTPUT / "prepared_manual_demo",
        tracker_url="http://127.0.0.1:8000/announce",
        reset=True,
    )

    assert prepared_only["single_torrent"].is_file()
    assert prepared_only["multi_torrent"].is_file()
    assert prepared_only["manifest_path"].is_file()

    integrated = run_integrated_demo(
        OUTPUT / "integrated_demo",
        reset=True,
    )

    verification = verify_demo_workspace(
        OUTPUT / "integrated_demo"
    )

    assert len(integrated["leecher_results"]) == 2
    assert len(verification["verified_files"]) == 3
    assert all(
        result.complete
        for result in integrated["leecher_results"]
    )
    assert len(integrated["seed_results"]) == 2
    assert all(result.session_downloaded == 0 for result in integrated["seed_results"])
    assert sum(result.session_uploaded for result in integrated["seed_results"]) == 7263
    assert all(result.session_uploaded == 0 for result in integrated["leecher_results"])
    assert sum(result.session_downloaded for result in integrated["leecher_results"]) == 7263

    audit = audit_submission(ROOT, require_report=True)

    assert len(audit.checks) >= 6
    assert "report/report.pdf" in audit.checks
    assert not audit.warnings

    build_dir = OUTPUT / "submission_build"
    archive = build_submission(
        ROOT,
        student_id="TEST12345",
        output_directory=build_dir,
        require_report=True,
    )

    assert archive.name == "DN_FinalProject_TEST12345.zip"
    assert archive.is_file()

    with zipfile.ZipFile(archive) as zip_file:
        names = set(zip_file.namelist())

    prefix = "DN_FinalProject_TEST12345/"

    required_archive_entries = {
        prefix + "common/bencode.py",
        prefix + "tracker/tracker_server.py",
        prefix + "peer/peer.py",
        prefix + "peer/torrent_worker.py",
        prefix + "peer/torrents/sample_single.torrent.json",
        prefix + "peer/torrents/sample_multi.torrent.json",
        prefix + "requirements.txt",
        prefix + "report/report.pdf",
    }

    assert required_archive_entries.issubset(names)
    assert not any("__pycache__" in name for name in names)
    assert not any(".pytest_cache" in name for name in names)
    assert not any("/test_output/" in name for name in names)
    assert not any("/test_data/" in name for name in names)
    assert not any("/demo_workspace/" in name for name in names)
    assert not any(name.endswith(".pyc") for name in names)
    assert not any(name.lower().endswith(".md") for name in names)

    manual_scripts = [
        ROOT / "demo" / "01_prepare_demo.cmd",
        ROOT / "demo" / "02_run_tracker.cmd",
        ROOT / "demo" / "03_run_seed_peer.cmd",
        ROOT / "demo" / "04_run_leecher_peer.cmd",
        ROOT / "demo" / "05_verify_demo.cmd",
    ]

    assert all(path.is_file() for path in manual_scripts)

    print(
        f"Integrated Tracker URL: {integrated['tracker_url']}"
    )
    print(
        "Integrated downloads: "
        + ", ".join(
            f"{result.name}={result.bytes_downloaded} bytes"
            for result in integrated["leecher_results"]
        )
    )
    print(
        f"Verified output files: "
        f"{len(verification['verified_files'])}"
    )
    print(f"Submission archive test: {archive.name}")
    print("Deterministic two-torrent demo preparation: PASS")
    print("Session uploaded/downloaded announce counters: PASS")
    print("Tracker + Seeder + Leecher end-to-end execution: PASS")
    print("Single-file and multi-file reconstruction: PASS")
    print("Final project structure audit: PASS")
    print("Standard-library-only dependency audit: PASS")
    print("Submission ZIP naming and contents: PASS")
    print("Windows multi-terminal command scripts: PASS")
    print("FINAL INTEGRATION TESTS RESULT: PASS")


def test_final_integration_component() -> None:
    main()

if __name__ == "__main__":
    main()
