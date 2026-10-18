"""Set VERSION to exactly one patch above the current commit's version."""

import re
import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
LEGACY_VERSION_PATTERN = re.compile(r'version\s*=\s*"(\d+\.\d+\.\d+)"')


def current_committed_version(base_revision):
    try:
        value = subprocess.check_output(
            ["git", "show", f"{base_revision}:VERSION"], cwd=ROOT, text=True,
            stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError:
        # Bootstrap the first VERSION file from this repo's last hard-coded app version.
        source = subprocess.check_output(
            ["git", "show", f"{base_revision}:server.py"], cwd=ROOT, text=True
        )
        match = LEGACY_VERSION_PATTERN.search(source)
        if not match:
            raise SystemExit("HEAD has no VERSION file or legacy server version to bump from")
        value = match.group(1)
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        raise SystemExit(f"HEAD:VERSION is not numeric MAJOR.MINOR.PATCH: {value!r}")
    return tuple(map(int, match.groups()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="HEAD", help="revision whose version this commit follows")
    args = parser.parse_args()
    major, minor, patch = current_committed_version(args.base)
    next_version = f"{major}.{minor}.{patch + 1}\n"
    VERSION_FILE.write_text(next_version, encoding="ascii", newline="\n")
    subprocess.run(["git", "add", "VERSION"], cwd=ROOT, check=True)
    print(f"Version for this commit: {next_version.strip()}")


if __name__ == "__main__":
    main()
