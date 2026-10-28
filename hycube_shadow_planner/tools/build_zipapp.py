"""Build a deterministic, source-only zipapp using the standard library."""
import argparse
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo


def build(output):
    root = Path(__file__).resolve().parents[1]
    files = {p.relative_to(root).as_posix(): p.read_bytes()
             for p in (root / "hycube_sim").glob("*.py")}
    files["__main__.py"] = b"from hycube_sim.cli import main\nmain()\n"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        for name, content in sorted(files.items()):
            entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, content)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    print(build(parser.parse_args().output))
