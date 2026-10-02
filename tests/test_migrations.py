from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

import memkit.schema as memory_module  # noqa: E402  (where migrations and standard predicates live)
from memory import Memory, MemoryError, render_brief  # noqa: E402


def migration(stem: str, kind: str | None, body: str) -> str:
    header = f"-- kind: {kind}\n" if kind else ""
    return (f"{header}-- Test migration.\n\nBEGIN IMMEDIATE;\n\n{body}\n\n"
            f"INSERT INTO schema_migrations (migration_id, applied_at)\n"
            f"VALUES ('{stem}', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));\n\nCOMMIT;\n")


class MigrationPolicyTests(unittest.TestCase):
    """Additive migrations apply on open; transformative ones need init and a
    backup; a helper older than its memory may read but not write."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.migrations = root / "migrations"
        shutil.copytree(memory_module.MIGRATIONS_DIR, self.migrations)
        patcher = mock.patch.object(memory_module, "MIGRATIONS_DIR", self.migrations)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = root / ".memory" / "memory.sqlite"
        self.memory = Memory(self.path, time_zone="UTC")
        self.memory.init("Ana", "person:ana")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add(self, stem: str, kind: str | None, body: str) -> None:
        (self.migrations / f"{stem}.sql").write_text(migration(stem, kind, body), encoding="utf-8")

    def applied(self) -> set[str]:
        with closing(sqlite3.connect(self.path)) as connection:
            return {row[0] for row in connection.execute("SELECT migration_id FROM schema_migrations")}

    def test_shipped_migrations_declare_their_kind(self) -> None:
        kinds = {path.stem: memory_module.migration_kind(path) for path in memory_module.MIGRATIONS_DIR.glob("*.sql")}
        self.assertEqual(kinds["0005_generic_capture"], "transformative")
        self.assertEqual(kinds["0007_value_indexes"], "additive")
        for path in memory_module.MIGRATIONS_DIR.glob("*.sql"):
            self.assertRegex(path.read_text(encoding="utf-8").splitlines()[0], r"^-- kind: (additive|transformative)$")

    def test_additive_migrations_apply_when_the_memory_opens(self) -> None:
        self.add("0900_test_index", "additive", "CREATE INDEX events_summary_test ON events(summary);")
        brief = Memory(self.path, time_zone="UTC").brief()
        self.assertIn("0900_test_index", self.applied())
        logged = brief["schema"]["recent_migrations"]
        self.assertEqual(len(logged), 1)
        self.assertIn("automatically", logged[0]["text"])
        self.assertIn("0900_test_index", render_brief(brief))
        event = self.memory.event("schema_migrated:0900_test_index")
        self.assertEqual(event["event_type"], "schema_migrated")
        self.assertFalse((self.path.parent / "backups").exists())

    def test_transformative_migrations_wait_for_init_and_back_up_first(self) -> None:
        self.add("0900_test_rewrite", "transformative", "CREATE TABLE test_rewrite (value TEXT);")
        with self.assertRaisesRegex(MemoryError, "Run: memory.py init"):
            Memory(self.path, time_zone="UTC").brief()
        self.assertNotIn("0900_test_rewrite", self.applied())
        result = Memory(self.path, time_zone="UTC").init()
        self.assertEqual(result["migrations_applied"], ["0900_test_rewrite"])
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, self.path.parent / "backups")
        with closing(sqlite3.connect(backup)) as copy:
            self.assertNotIn("0900_test_rewrite", {row[0] for row in copy.execute("SELECT migration_id FROM schema_migrations")})
        event = self.memory.event("schema_migrated:0900_test_rewrite")
        self.assertIn(str(backup), event["summary"])

    def test_undeclared_migrations_count_as_transformative(self) -> None:
        self.add("0900_test_unknown", None, "CREATE TABLE test_unknown (value TEXT);")
        with self.assertRaisesRegex(MemoryError, "outdated"):
            Memory(self.path, time_zone="UTC").brief()

    def test_an_older_helper_reads_but_does_not_write(self) -> None:
        self.add("0900_test_future", "additive", "CREATE TABLE test_future (value TEXT);")
        Memory(self.path, time_zone="UTC").brief()
        (self.migrations / "0900_test_future.sql").unlink()  # this helper predates 0900
        older = Memory(self.path, time_zone="UTC")
        brief = older.brief()
        self.assertTrue(brief["schema"]["read_only"])
        self.assertIn("READ ONLY", render_brief(brief))
        with self.assertRaisesRegex(MemoryError, "can be read but not written"):
            older.remember("note", "No debe guardarse")
        with self.assertRaisesRegex(MemoryError, "update the helper"):
            older.init()

    def test_missing_standard_predicates_are_added_on_open(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM predicates WHERE predicate_id = 'backup_path'").fetchone()[0]
        self.assertEqual(count, 1)
        with mock.patch.object(memory_module, "STANDARD_PREDICATES",
                               memory_module.STANDARD_PREDICATES + (("test_marker", "Marca", "text", "one", 0),)):
            Memory(self.path, time_zone="UTC").brief()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertTrue(connection.execute("SELECT 1 FROM predicates WHERE predicate_id = 'test_marker'").fetchone())


if __name__ == "__main__":
    unittest.main()
