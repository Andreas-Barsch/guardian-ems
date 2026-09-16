#!/usr/bin/env python3
"""Create an immutable Guardian Battery add-on context from a clean Git commit."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def prepare_build_context(repository: Path, destination: Path) -> str:
    repository = repository.resolve()
    destination = destination.resolve()
    if _git(repository, "status", "--porcelain=v1"):
        raise RuntimeError("refusing to package a dirty working tree")
    revision = _git(repository, "rev-parse", "HEAD")
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise RuntimeError("Git did not return a full lowercase commit SHA")
    if destination.exists():
        raise RuntimeError("destination already exists")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="guardian-battery-context-",
                                    dir=destination.parent))
    archive = staging / "source.tar"
    context = staging / "context"
    context.mkdir()
    try:
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "-C", str(repository), "archive", "HEAD", "guardian_battery"],
                check=True, stdout=output,
            )
        with tarfile.open(archive) as source:
            source.extractall(context, filter="data")
        packaged = context / "guardian_battery"
        (packaged / ".guardian-source-commit").write_text(
            revision + "\n", encoding="ascii")
        packaged.rename(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return revision


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--repository", type=Path,
                        default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    print(prepare_build_context(args.repository, args.destination))


if __name__ == "__main__":
    main()
