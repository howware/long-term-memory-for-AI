from __future__ import annotations

import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError, render_brief  # noqa: E402


class BackupTests(unittest.TestCase):
    """The memory is one file and the only copy: backup makes a verified copy
    with SQLite's online backup, so writes still in the -wal file are kept."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.memory = Memory(self.folder / ".memory" / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        self.memory.remember("decision", "Use one database per project", performed_by="person:ana")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_backup_is_a_verified_complete_copy(self) -> None:
        self.assertIn("Last backup: none yet", render_brief(self.memory.brief()))
        result = self.memory.backup()
        copy = Path(result["backup"])
        self.assertEqual(copy.parent, self.folder / ".memory" / "backups")
        self.assertEqual((copy.parent / ".gitignore").read_text(encoding="utf-8"), "*\n")
        self.assertEqual(result["integrity"], "ok")
        with closing(sqlite3.connect(copy)) as connection:
            decisions = connection.execute("SELECT COUNT(*) FROM events WHERE event_type = 'decision'").fetchone()[0]
        self.assertEqual(decisions, 1)
        # The copy opens as a memory of its own, with the same decision.
        restored = Memory(copy, time_zone="UTC")
        self.assertEqual(restored.brief()["decisions"]["open"], 1)
        self.assertIn("Last backup:", render_brief(self.memory.brief()))
        self.assertNotIn("none yet", render_brief(self.memory.brief()))

    def test_backup_to_a_folder_or_file_never_overwrites(self) -> None:
        target = self.folder / "elsewhere"
        target.mkdir()
        first = Path(self.memory.backup(target)["backup"])
        second = Path(self.memory.backup(target)["backup"])
        self.assertNotEqual(first, second)
        with self.assertRaisesRegex(MemoryError, "never overwrites"):
            self.memory.backup(first)
        with self.assertRaisesRegex(MemoryError, "never overwrites"):
            self.memory.backup(self.memory.database_path)


if __name__ == "__main__":
    unittest.main()
