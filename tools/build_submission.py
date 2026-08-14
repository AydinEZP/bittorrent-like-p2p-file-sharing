from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

import argparse
import re
import zipfile

from tools.submission_audit import audit_submission


def validate_student_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise argparse.ArgumentTypeError(
            "student ID may contain letters, digits, '_' and '-' only"
        )
    return value


def build_submission(
    project_root: str | Path,
    *,
    student_id: str,
    output_directory: str | Path,
    require_report: bool = True,
) -> Path:
    root = Path(project_root).resolve()
    audit_submission(root, require_report=require_report)

    output_dir = Path(output_directory).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    archive_path = output_dir / f"DN_FinalProject_{student_id}.zip"

    excluded_parts = {
        "__pycache__",
        ".git",
        ".idea",
        ".vscode",
        ".pytest_cache",
        "test_output",
        "test_data",
        "submission_build",
        "demo_workspace",
        "demo_workspace_integrated",
    }
    excluded_suffixes = {
        ".md",
        ".pyc",
        ".pyo",
    }

    if archive_path.exists():
        archive_path.unlink()

    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in excluded_parts for part in path.parts):
                continue
            if path.suffix.lower() in excluded_suffixes:
                continue
            if path == archive_path:
                continue

            relative = path.relative_to(root)
            archive_name = (
                Path(f"DN_FinalProject_{student_id}") / relative
            )
            archive.write(path, archive_name)

    return archive_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the final course-project ZIP archive."
    )
    parser.add_argument(
        "student_id",
        type=validate_student_id,
    )
    parser.add_argument(
        "--root",
        default=".",
    )
    parser.add_argument(
        "--output-dir",
        default="submission_build",
    )
    parser.add_argument(
        "--allow-missing-report",
        action="store_true",
        help="Testing only; final submission must contain report.pdf",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    archive = build_submission(
        args.root,
        student_id=args.student_id,
        output_directory=args.output_dir,
        require_report=not args.allow_missing_report,
    )

    print(f"Created: {archive}")
    print("SUBMISSION BUILD RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
