"""Every static file the pages need must travel in the Docker image.

The 28-10-2026 incident: the new per-message page worked locally and answered
``500 Internal Server Error`` in production, because the ``Dockerfile`` copies files by
name and ``message.html`` / ``message.js`` were missing. This test reads the served
paths out of ``server.py`` and the copied names out of the ``Dockerfile`` and fails when
a page or script would not exist inside the image.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
def served_files():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    names = set()
    for match in re.finditer(r'FilePath\(__file__\)\.parent\s*/\s*"([^"]+)"', source):
        names.add(match.group(1))
    for match in re.finditer(r'with_name\("([^"]+)"\)', source):
        names.add(match.group(1))
    return names


def copied_names():
    lines = [line for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
             if line.startswith("COPY ")]
    names = set()
    for line in lines:
        parts = line.split()[1:-1]  # drop the instruction and the destination
        names.update(part.rstrip("/") + "/" for part in parts if part not in (".", "requirements.txt"))
        names.update(part for part in parts if not part.endswith("/"))
    return names


class PackagingTests(unittest.TestCase):
    def test_chat_assets_are_copied(self):
        self.assertTrue({'chat.html', 'chat.js', 'chat.css'} <= copied_names())

    def test_every_served_static_file_is_copied_into_the_image(self):
        copied = copied_names()
        missing = []
        for name in sorted(served_files()):
            first = name.split("/")[0]
            if name in copied or first + "/" in copied:
                continue
            missing.append(name)
        self.assertEqual(missing, [], f"el Dockerfile no copia estos archivos servidos: {missing}")

    def test_the_build_guard_lists_the_pages_it_protects(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        guard = next((line for line in dockerfile.splitlines() if line.startswith("RUN python -c")), "")
        self.assertIn("message.html", guard)
        self.assertIn("message.js", guard)
        self.assertIn("dashboard.html", guard)

    def test_the_guard_runs_before_the_image_is_tagged(self):
        lines = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
        guard_index = next(index for index, line in enumerate(lines) if line.startswith("RUN python -c"))
        command_index = next(index for index, line in enumerate(lines) if line.startswith("CMD "))
        self.assertLess(guard_index, command_index)


if __name__ == "__main__":
    unittest.main()
