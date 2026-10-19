"""Require each commit to advance the patch or bump exactly one SemVer component."""

import re
import subprocess
import sys


VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
LEGACY_VERSION_PATTERN = re.compile(r'version\s*=\s*"(\d+\.\d+\.\d+)"')


def git(*args, quiet=False):
    return subprocess.check_output(
        ["git", *args], text=True,
        stderr=subprocess.DEVNULL if quiet else None,
    ).strip()


def read_version(revision):
    try:
        value = git("show", f"{revision}:VERSION", quiet=True)
    except subprocess.CalledProcessError:
        # First versioned commit: infer the baseline from the prior hard-coded app version.
        source = git("show", f"{revision}:server.py")
        match = LEGACY_VERSION_PATTERN.search(source)
        if not match:
            raise ValueError(f"{revision}: VERSION is missing and no legacy app version was found")
        value = match.group(1)
    match = VERSION_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"{revision}: VERSION is not numeric MAJOR.MINOR.PATCH ({value!r})")
    return tuple(map(int, match.groups()))


def main(base, head):
    commits = git("rev-list", "--reverse", f"{base}..{head}").splitlines()
    if not commits:
        print("No commits to validate.")
        return 0
    for commit in commits:
        parents = git("rev-list", "--parents", "-n", "1", commit).split()
        if len(parents) < 2:
            raise ValueError(f"Commit {commit[:12]} has no parent VERSION to compare")
        previous = read_version(parents[1])
        current = read_version(commit)
        patch_bump = current[:2] == previous[:2] and current[2] == previous[2] + 1
        minor_bump = current[0] == previous[0] and current[1] == previous[1] + 1
        major_bump = current[0] == previous[0] + 1
        if not (patch_bump or minor_bump or major_bump):
            old = ".".join(map(str, previous))
            new = ".".join(map(str, current))
            raise ValueError(f"{commit[:12]}: VERSION must increment the patch or bump one SemVer component; moved from {old} to {new}")
        print(f"{commit[:12]}: {'.'.join(map(str, current))} OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1], sys.argv[2]))
    except (IndexError, subprocess.CalledProcessError, ValueError) as error:
        print(f"Version history check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
