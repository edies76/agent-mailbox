"""Enable the repository's automatic version bump hook for this checkout."""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=ROOT, check=True)
print("Enabled .githooks for this checkout. Each commit now stages VERSION +0.0.1.")
