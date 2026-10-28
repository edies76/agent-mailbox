"""Unit tests for the ZCode wake adapter scripts shipped in ``adapters/``.

They cover the invariant that broke two chats of the same agent on 27-10-2026: the
wake directory must be derived **per instance**. When a second chat (a fork) inherited
the shared directory, both chats shared ``wake.json`` and ``watch.pid``, so one chat's
watcher could fire on the other chat's mail, and the "is it armed?" check reported the
other chat's pid, leaving this chat unable to arm its own.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ADAPTERS = Path(__file__).resolve().parent / "adapters"


def load(name):
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), ADAPTERS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ZcodeWakeTests(unittest.TestCase):
    def test_wake_directory_is_per_instance_when_the_config_has_none(self):
        wake = load("zcode_wake.py")
        common = {"mailbox_bank_dir": str(Path(tempfile.gettempdir()) / "bank")}
        first = wake.wake_dir_of({**common, "agent_id": "aaaaaaaa-1111-2222-3333-444444444444"})
        second = wake.wake_dir_of({**common, "agent_id": "bbbbbbbb-1111-2222-3333-444444444444"})
        self.assertNotEqual(first, second)
        self.assertTrue(str(first).endswith("aaaaaaaa"))

    def test_explicit_wake_directory_wins(self):
        wake = load("zcode_wake.py")
        explicit = Path(tempfile.gettempdir()) / "zcode-wake" / "explicit"
        config = {"agent_id": "aaaaaaaa-1111-2222-3333-444444444444", "wake_dir": str(explicit),
                  "mailbox_bank_dir": str(Path(tempfile.gettempdir()) / "bank")}
        self.assertEqual(wake.wake_dir_of(config), explicit)

    def test_watcher_and_ensure_agree_on_the_same_directory(self):
        watcher = load("zcode_wake_watch.py")
        ensure = load("zcode_wake_ensure.py")
        with tempfile.TemporaryDirectory() as tmp:
            config = {"agent_id": "cccccccc-1111-2222-3333-444444444444",
                      "mailbox_bank_dir": str(Path(tmp) / "bank")}
            pid_path, seen_path, wake_path = watcher.state_paths(config)
            self.assertEqual(ensure.wake_dir_of(config), pid_path.parent)
            self.assertEqual(seen_path.parent, pid_path.parent)
            self.assertEqual(wake_path.parent, pid_path.parent)

    def test_pid_file_is_not_shared_between_instances(self):
        watcher = load("zcode_wake_watch.py")
        with tempfile.TemporaryDirectory() as tmp:
            base = {"mailbox_bank_dir": str(Path(tmp) / "bank")}
            first = watcher.state_paths({**base, "agent_id": "aaaaaaaa-1111-2222-3333-444444444444"})[0]
            second = watcher.state_paths({**base, "agent_id": "bbbbbbbb-1111-2222-3333-444444444444"})[0]
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
