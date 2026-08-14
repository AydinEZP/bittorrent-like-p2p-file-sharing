from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

import argparse
import ast
from dataclasses import dataclass
from typing import Iterable

from peer.metainfo import Metainfo


@dataclass(frozen=True)
class AuditResult:
    checks: tuple[str, ...]
    warnings: tuple[str, ...]


class SubmissionAuditError(RuntimeError):
    """Raised when the project does not satisfy a submission check."""


LOCAL_PACKAGES = {
    "common",
    "peer",
    "tracker",
    "tools",
    "tests",
    "run_all_tests",
}


def _python_files(root: Path) -> list[Path]:
    ignored = {
        "__pycache__",
        "test_output",
        "demo_workspace_integrated",
    }

    return [
        path
        for path in root.rglob("*.py")
        if not any(part in ignored for part in path.parts)
    ]


def _top_level_imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
    except SyntaxError as exc:
        raise SubmissionAuditError(
            f"Python syntax error in {path}: {exc}"
        ) from exc

    imports: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imports.add(node.module.split(".", 1)[0])

    return imports


def audit_submission(
    root: str | Path,
    *,
    require_report: bool = False,
) -> AuditResult:
    project_root = Path(root).resolve()
    checks: list[str] = []
    warnings: list[str] = []

    required_paths = [
        project_root / "common" / "bencode.py",
        project_root / "common" / "event_logger.py",
        project_root / "tracker" / "tracker_server.py",
        project_root / "tracker" / "tracker_state.py",
        project_root / "peer" / "peer.py",
        project_root / "peer" / "tracker_client.py",
        project_root / "peer" / "peer_server.py",
        project_root / "peer" / "piece_manager.py",
        project_root / "peer" / "torrent_worker.py",
        project_root / "peer" / "transfer_stats.py",
        project_root / "tools" / "create_metainfo.py",
        project_root / "requirements.txt",
    ]

    missing = [path for path in required_paths if not path.is_file()]
    if missing:
        raise SubmissionAuditError(
            "Missing required project file(s): "
            + ", ".join(str(path.relative_to(project_root)) for path in missing)
        )

    checks.append("Required Tracker, Peer, common, and tool files")

    metainfo_files = sorted(
        (project_root / "peer" / "torrents").glob("*.torrent.json")
    )

    if len(metainfo_files) < 2:
        raise SubmissionAuditError(
            "At least two Metainfo files are required"
        )

    for path in metainfo_files:
        Metainfo(path).load()

    checks.append(
        f"Human-readable Metainfo files ({len(metainfo_files)})"
    )

    python_files = _python_files(project_root)
    imported_modules: set[str] = set()

    for path in python_files:
        imported_modules.update(_top_level_imports(path))

    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    external = sorted(
        module
        for module in imported_modules
        if module not in stdlib
        and module not in LOCAL_PACKAGES
        and module != "__future__"
    )

    if external:
        raise SubmissionAuditError(
            "Non-standard imported modules found: "
            + ", ".join(external)
        )

    checks.append(
        f"Python standard-library-only dependency scan ({len(python_files)} files)"
    )

    requirements = (
        project_root / "requirements.txt"
    ).read_text(encoding="utf-8").strip()

    if "No external dependencies" not in requirements:
        raise SubmissionAuditError(
            "requirements.txt must explicitly state no external dependencies"
        )

    checks.append("External-library declaration")

    report_path = project_root / "report" / "report.pdf"

    if require_report and not report_path.is_file():
        raise SubmissionAuditError("report.pdf is required but missing")

    if report_path.is_file():
        checks.append("report/report.pdf")
    else:
        warnings.append(
            "report/report.pdf is not present yet; add it before the final submission"
        )

    prohibited_suffixes = {
        ".exe",
        ".dll",
        ".so",
        ".dylib",
    }
    prohibited = [
        path
        for path in project_root.rglob("*")
        if path.is_file() and path.suffix.lower() in prohibited_suffixes
    ]

    if prohibited:
        raise SubmissionAuditError(
            "Prohibited binary files found: "
            + ", ".join(str(path) for path in prohibited)
        )

    checks.append("No compiled executable/library artifacts")

    return AuditResult(
        checks=tuple(checks),
        warnings=tuple(warnings),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit the project before building the submission ZIP."
    )
    parser.add_argument(
        "--root",
        default=".",
    )
    parser.add_argument(
        "--require-report",
        action="store_true",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        result = audit_submission(
            args.root,
            require_report=args.require_report,
        )
    except SubmissionAuditError as exc:
        print(f"SUBMISSION AUDIT RESULT: FAIL")
        print(str(exc))
        return 1

    for check in result.checks:
        print(f"PASS: {check}")

    for warning in result.warnings:
        print(f"WARNING: {warning}")

    print("SUBMISSION AUDIT RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
