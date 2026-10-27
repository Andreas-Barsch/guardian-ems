"""Build-time writer; uses the runtime version from the copied application."""

import json
import re
import sys
from pathlib import Path

from version import GUARDIAN_VERSION


def seal_build_info(build_version: str, marker: Path, destination: Path) -> None:
    revision = marker.read_text(encoding="ascii").strip()
    if build_version != GUARDIAN_VERSION:
        raise ValueError("BUILD_VERSION must match the Battery runtime version")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("packaged source commit must be a full lowercase SHA")
    # Never overwrite an existing claim of provenance.
    with destination.open("x", encoding="utf-8") as output:
        json.dump({"guardian_version": build_version, "source_commit": revision}, output)
        output.write("\n")


if __name__ == "__main__":
    seal_build_info(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
