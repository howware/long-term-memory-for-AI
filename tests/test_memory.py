from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


class MemoryHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "memory.sqlite"
        migrations = sorted((PROJECT_ROOT / "db" / "migrations").glob("*.sql"))
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL) STRICT")
            for path in migrations:
                connection.executescript(path.read_text(encoding="utf-8"))
        finally:
            connection.close()
        self.memory = Memory(self.database)
        self.memory.bootstrap_predicates()
        self.actor = self.memory.create_entity("person", "Alicia", "person:alicia")["entity_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_event_requires_person_performer(self) -> None:
        system = self.memory.create_entity("system", "Bot", "system:bot")["entity_id"]
        with self.assertRaises(MemoryError):
            self.memory.record_event("test", "must fail", system)
        event = self.memory.record_event("test", "works", self.actor)
        self.assertEqual(event["performed_by"], "Alicia")

    def test_transition_validates_and_clears_terminal_pending(self) -> None:
        start = self.memory.create_entity("workflow_state", "Start", "state:start")["entity_id"]
        done = self.memory.create_entity("workflow_state", "Done", "state:done")["entity_id"]
        instance = self.memory.create_entity("process_instance", "Example", "instance:example")["entity_id"]
        transition = self.memory.create_entity("workflow_transition", "Start to Done", "transition:start-done")["entity_id"]
        now = "2026-01-01T00:00:00Z"
        with self.memory.connection() as connection:
            def insert_entity(assertion_id: str, subject: str, predicate: str, value: str, valid_from: str | None = None) -> None:
                connection.execute(
                    """
                    INSERT INTO assertions
                    (assertion_id, subject_entity_id, predicate_id, value_type, value_entity_id,
                     valid_from, assertion_state, confidence, recorded_at)
                    VALUES (?, ?, ?, 'entity', ?, ?, 'asserted', 1.0, ?)
                    """,
                    (assertion_id, subject, predicate, value, valid_from, now),
                )

            insert_entity("status", instance, "workflow_status", start, now)
            insert_entity("pending", instance, "pending_action_for", self.actor, now)
            insert_entity("from", transition, "from_workflow_state", start)
            insert_entity("to", transition, "to_workflow_state", done)
            connection.execute(
                """
                INSERT INTO assertions
                (assertion_id, subject_entity_id, predicate_id, value_type, value_boolean,
                 assertion_state, confidence, recorded_at)
                VALUES ('terminal', ?, 'is_terminal', 'boolean', 1, 'asserted', 1.0, ?)
                """,
                (done, now),
            )
        result = self.memory.transition(instance, done, self.actor, None, "2026-01-01T00:01:00Z")
        self.assertEqual(result["state"], "Done")
        self.assertIsNone(result["pending_for"])
        self.assertEqual(self.memory.detail(instance)["pending_for"], None)

    def test_assigned_tracking_code_is_persisted_and_audited(self) -> None:
        item = self.memory.create_entity("process_instance", "Trackable", "instance:trackable")["entity_id"]
        result = self.memory.assign_tracking_code(item, self.actor, code="MEM7KQ2")
        self.assertEqual(result["tracking_code"], "MEM7KQ2")
        with self.memory.connection() as connection:
            stored = connection.execute(
                "SELECT value_text FROM assertions WHERE subject_entity_id = ? AND predicate_id = 'tracking_code' AND valid_to IS NULL",
                (item,),
            ).fetchone()["value_text"]
        self.assertEqual(stored, "MEM7KQ2")
        self.assertEqual(self.memory.timeline(item)[0]["event_type"], "tracking_code_assigned")


if __name__ == "__main__":
    unittest.main()
