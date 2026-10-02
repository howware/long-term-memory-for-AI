from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


class AcceptanceCasesV1(unittest.TestCase):
    """Publication gates for the local, generic memory model."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "acceptance.sqlite"
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL) STRICT"
            )
            for migration in sorted((PROJECT_ROOT / "db" / "migrations").glob("*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))
        finally:
            connection.close()
        self.memory = Memory(self.database)
        self.memory.bootstrap_predicates()
        self.alicia = self.memory.create_entity("person", "Alicia", "person:alicia")["entity_id"]
        self.beto = self.memory.create_entity("person", "Beto", "person:beto")["entity_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _workflow(self) -> dict[str, str]:
        todo = self.memory.create_entity("workflow_state", "To Do", "state:todo")["entity_id"]
        progress = self.memory.create_entity("workflow_state", "In Progress", "state:progress")["entity_id"]
        done = self.memory.create_entity("workflow_state", "Done", "state:done")["entity_id"]
        item = self.memory.create_entity("process_instance", "Acceptance item", "instance:acceptance")["entity_id"]
        transitions = [
            (
                source,
                target,
                key,
                self.memory.create_entity("workflow_transition", key, f"transition:{key}")["entity_id"],
            )
            for source, target, key in ((todo, progress, "todo-progress"), (progress, done, "progress-done"))
        ]
        now = "2026-01-01T08:00:00Z"
        with self.memory.connection() as connection:
            def entity_fact(subject: str, predicate: str, value: str, fact_id: str) -> None:
                connection.execute(
                    """
                    INSERT INTO assertions
                    (assertion_id, subject_entity_id, predicate_id, value_type, value_entity_id,
                     valid_from, assertion_state, confidence, recorded_at)
                    VALUES (?, ?, ?, 'entity', ?, ?, 'asserted', 1.0, ?)
                    """,
                    (fact_id, subject, predicate, value, now, now),
                )

            entity_fact(item, "workflow_status", todo, "initial-status")
            entity_fact(item, "pending_action_for", self.alicia, "initial-pending")
            for source, target, key, transition in transitions:
                entity_fact(transition, "from_workflow_state", source, f"from-{key}")
                entity_fact(transition, "to_workflow_state", target, f"to-{key}")
            connection.execute(
                """
                INSERT INTO assertions
                (assertion_id, subject_entity_id, predicate_id, value_type, value_boolean,
                 assertion_state, confidence, recorded_at)
                VALUES ('done-terminal', ?, 'is_terminal', 'boolean', 1, 'asserted', 1.0, ?)
                """,
                (done, now),
            )
        return {"item": item, "todo": todo, "progress": progress, "done": done}

    def test_ac01_reopen_recovers_written_event_and_performer(self) -> None:
        item = self.memory.create_entity("task", "Cold-start fact", "task:cold-start")["entity_id"]
        event = self.memory.record_event(
            "fact_recorded",
            "Alicia recorded a durable fact.",
            self.alicia,
            participants=[(item, "subject")],
            occurred_at="2026-01-01T09:00:00Z",
        )
        reopened = Memory(self.database)
        timeline = reopened.timeline(item)
        self.assertEqual(timeline[0]["event_type"], "fact_recorded")
        self.assertEqual(timeline[0]["performed_by_name"], "Alicia")
        self.assertEqual(event["performed_by"], "Alicia")

    def test_ac02_current_state_and_history_are_preserved(self) -> None:
        workflow = self._workflow()
        self.memory.transition(workflow["item"], workflow["progress"], self.alicia, self.beto, "2026-01-01T09:00:00Z")
        self.memory.transition(workflow["item"], workflow["done"], self.beto, None, "2026-01-01T10:00:00Z")
        self.assertEqual(self.memory.detail(workflow["item"])["state"], "Done")
        with self.memory.connection() as connection:
            states = connection.execute(
                """
                SELECT value_entity_id, valid_from, valid_to FROM assertions
                 WHERE subject_entity_id = ? AND predicate_id = 'workflow_status'
                 ORDER BY valid_from
                """,
                (workflow["item"],),
            ).fetchall()
        self.assertEqual(len(states), 3)
        self.assertIsNotNone(states[0]["valid_to"])
        self.assertIsNotNone(states[1]["valid_to"])
        self.assertIsNone(states[2]["valid_to"])

    def test_ac03_invalid_transition_is_rejected_without_mutation(self) -> None:
        workflow = self._workflow()
        with self.assertRaises(MemoryError):
            self.memory.transition(workflow["item"], workflow["done"], self.alicia, None, "2026-01-01T09:00:00Z")
        self.assertEqual(self.memory.detail(workflow["item"])["state"], "To Do")
        self.assertEqual(len(self.memory.timeline(workflow["item"])), 0)

    def test_ac04_conflicting_evidence_is_preserved_as_two_facts(self) -> None:
        subject = self.memory.create_entity("item", "Conflicting fact", "item:conflict")["entity_id"]
        now = "2026-01-01T09:00:00Z"
        with self.memory.connection() as connection:
            connection.execute(
                "INSERT INTO predicates(predicate_id, label, value_type, cardinality, is_temporal, description, created_at) VALUES ('reported_status', 'Reported status', 'text', 'many', 1, NULL, ?)",
                (now,),
            )
            for value, source in (("approved", "source-a"), ("rejected", "source-b")):
                evidence_id, assertion_id = f"evidence-{source}", f"assertion-{source}"
                connection.execute(
                    "INSERT INTO evidence VALUES (?, 'acceptance_test', ?, ?, ?, ?, NULL, NULL)",
                    (evidence_id, source, value, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO assertions
                    (assertion_id, subject_entity_id, predicate_id, value_type, value_text,
                     valid_from, assertion_state, confidence, recorded_at)
                    VALUES (?, ?, 'reported_status', 'text', ?, ?, 'asserted', 1.0, ?)
                    """,
                    (assertion_id, subject, value, now, now),
                )
                connection.execute(
                    "INSERT INTO assertion_evidence VALUES (?, ?, 'supports', ?)",
                    (assertion_id, evidence_id, now),
                )
            facts = connection.execute(
                "SELECT value_text FROM assertions WHERE subject_entity_id = ? AND predicate_id = 'reported_status' ORDER BY value_text",
                (subject,),
            ).fetchall()
        self.assertEqual([row["value_text"] for row in facts], ["approved", "rejected"])

    def test_ac05_empty_period_returns_no_events(self) -> None:
        item = self.memory.create_entity("task", "Dated fact", "task:dated")["entity_id"]
        self.memory.record_event("fact_recorded", "January only.", self.alicia, [(item, "subject")], "2026-01-15T09:00:00Z")
        with self.memory.connection() as connection:
            rows = connection.execute(
                "SELECT event_id FROM events WHERE occurred_at >= ? AND occurred_at < ?",
                ("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"),
            ).fetchall()
        self.assertEqual(rows, [])

    def test_ac06_time_query_has_supporting_index(self) -> None:
        with self.memory.connection() as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT event_id FROM events WHERE event_type = ? AND occurred_at >= ? AND occurred_at < ?",
                ("fact_recorded", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
            ).fetchall()
        self.assertTrue(any("events_type_time_lookup" in row[3] for row in plan), plan)

    def test_catalog_and_describe_expose_explicit_semantics(self) -> None:
        route = self.memory.create_entity(
            "route", "Ruta de prueba", "route:example:1", "Ruta usada por la aceptación."
        )["entity_id"]
        location = self.memory.create_entity("location", "Ciudad de prueba", "city:example")["entity_id"]
        now = "2026-01-01T08:00:00Z"
        with self.memory.connection() as connection:
            connection.execute(
                "INSERT INTO predicates(predicate_id, label, value_type, cardinality, is_temporal, description, created_at) VALUES ('operates_in', 'Opera en', 'entity', 'many', 1, NULL, ?)",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO assertions
                (assertion_id, subject_entity_id, predicate_id, value_type, value_entity_id,
                 valid_from, assertion_state, confidence, recorded_at)
                VALUES ('route-city', ?, 'operates_in', 'entity', ?, ?, 'asserted', 1.0, ?)
                """,
                (route, location, now, now),
            )
        catalog = self.memory.catalog()
        description = self.memory.describe(route)
        self.assertIn("memory_current_states", catalog["views"])
        self.assertTrue(any(item["entity_type"] == "route" for item in catalog["entity_types"]))
        self.assertEqual(description["entity"]["name"], "Ruta de prueba")
        self.assertEqual(description["facts"][0]["value_entity_name"], "Ciudad de prueba")


if __name__ == "__main__":
    unittest.main()
