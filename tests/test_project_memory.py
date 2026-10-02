from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


class ProjectMemoryTests(unittest.TestCase):
    """Capture and briefing of the knowledge a new session needs first."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / ".memory" / "memory.sqlite")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def start(self) -> None:
        self.memory.init("Ana", "person:ana")
        self.memory.write({"source": {"type": "conversation", "content": "Proyecto de memoria."},
                           "entities": [{"key": "project:mem", "type": "project", "name": "mem"}]})

    def test_brief_without_memory_explains_how_to_start(self) -> None:
        brief = self.memory.brief()
        self.assertFalse(brief["exists"])
        self.assertIn("init", brief["next_step"])
        self.assertFalse(self.memory.database_path.exists())

    def test_remembered_decision_appears_in_brief_once(self) -> None:
        self.start()
        first = self.memory.remember("decision", "Las fechas se guardan en UTC con Z.",
                                     why="Mezclar formatos compara mal como texto.", about=["project:mem"])
        again = self.memory.remember("decision", "Las fechas se guardan en UTC con Z.")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["stored"]["participants"][0]["name"], "Ana")
        brief = self.memory.brief()
        self.assertEqual(brief["owner"], {"name": "Ana", "key": "person:ana"})
        self.assertEqual(brief["projects"][0]["external_key"], "project:mem")
        self.assertEqual(brief["decisions"]["open"], 1)
        decision = brief["decisions"]["latest"][0]
        self.assertEqual((decision["why"], decision["by"], decision["about"]),
                         ("Mezclar formatos compara mal como texto.", "Ana", "project:mem"))
        # The literal source and the rationale (a long text attribute) are both found.
        self.assertEqual({item["record_type"] for item in self.memory.recall("formatos")}, {"evidence", "attribute"})

    def test_superseded_decisions_and_answered_questions_leave_the_brief(self) -> None:
        self.start()
        old = self.memory.remember("decision", "La memoria vive en data/memory.sqlite.")
        self.memory.remember("decision", "La memoria vive en .memory/memory.sqlite.", supersedes=old["key"])
        question = self.memory.remember("open_question", "¿Aplicamos append-only con triggers?")
        with self.assertRaisesRegex(MemoryError, "needs --resolves"):
            self.memory.remember("answer", "Sí, después de las pruebas.")
        with self.assertRaisesRegex(MemoryError, "expected open_question"):
            self.memory.remember("answer", "Sí.", resolves=old["key"])
        brief = self.memory.brief()
        self.assertEqual([item["text"] for item in brief["decisions"]["latest"]], ["La memoria vive en .memory/memory.sqlite."])
        self.assertEqual(brief["open_questions"]["open"], 1)
        self.memory.remember("answer", "Sí, después de las pruebas.", resolves=question["key"])
        self.assertEqual(self.memory.brief()["open_questions"]["open"], 0)
        # History is kept: the replaced decision is still recallable.
        hits = self.memory.recall("data memory sqlite")
        self.assertTrue(any("data/memory.sqlite" in hit["snippet"].replace("[", "").replace("]", "") for hit in hits))

    def test_agent_records_and_observes_but_never_decides(self) -> None:
        with mock.patch.dict(os.environ, {"MEMORY_AGENT": "agent:test", "CLAUDECODE": ""}):
            self.start()
            decision = self.memory.remember("decision", "Usar SQLite.", why="Local y portable.")
            roles = {(item["role"], item["name"]) for item in decision["stored"]["participants"]}
            self.assertIn(("performed_by", "Ana"), roles)
            self.assertIn(("recorded_by", "test"), roles)
            lesson = self.memory.remember("lesson", "SQLite elige mal el orden de saltos; fijarlo con CROSS JOIN.",
                                          as_agent=True)
            with self.assertRaisesRegex(MemoryError, "belongs to a person"):
                self.memory.remember("decision", "El agente decide algo.", as_agent=True)
            own = self.memory.remember("open_question", "¿Conviene un índice nuevo?", as_agent=True)
            person_question = self.memory.remember("open_question", "¿Aplicamos append-only?")
            with self.assertRaisesRegex(MemoryError, "only the person who owns it"):
                self.memory.remember("answer", "Sí.", resolves=person_question["key"], as_agent=True)
            self.memory.remember("answer", "Sí, agregado en la migración 0007.", resolves=own["key"], as_agent=True)
            open_keys = [item["key"] for item in self.memory.brief()["open_questions"]["latest"]]
            self.assertEqual(open_keys, [person_question["key"]])
            with self.assertRaisesRegex(MemoryError, "need source type"):
                self.memory.write({"source": {"type": "conversation", "content": "x"},
                                   "events": [{"type": "note", "occurred_at": "2026-01-01T00:00:00Z", "observed_by": "agent:test"}]})
            with self.assertRaisesRegex(MemoryError, "not both"):
                self.memory.write({"source": {"type": "agent_observation", "content": "y"},
                                   "events": [{"type": "note", "occurred_at": "2026-01-01T00:00:00Z",
                                               "observed_by": "agent:test", "performed_by": "person:ana"}]})
            self.memory.write({"source": {"type": "conversation", "content": "hecho"},
                               "facts": [{"subject": "project:mem", "predicate": "is_memory_owner", "value": False}]})
        stored = lesson["stored"]
        self.assertEqual([(item["role"], item["name"]) for item in stored["participants"]], [("observed_by", "test")])
        self.assertEqual(stored["sources"][0]["source_type"], "agent_observation")
        lessons = self.memory.brief()["lessons"]["latest"]
        self.assertEqual(lessons[0]["by"], "test (agent observation)")
        catalog = self.memory.catalog()
        self.assertEqual(catalog["integrity"]["events_without_actor"], 0)
        self.assertEqual(catalog["agents"], [{"name": "test", "external_key": "agent:test"}])
        with self.memory.connection() as connection:
            recorder = connection.execute(
                """SELECT entities.external_key FROM assertions JOIN entities ON entities.entity_id = recorded_by_entity_id
                    WHERE predicate_id = 'is_memory_owner' AND value_boolean = 0"""
            ).fetchone()[0]
        self.assertEqual(recorder, "agent:test")

    def test_attribution_is_never_guessed(self) -> None:
        self.memory.init()
        with self.assertRaisesRegex(MemoryError, "who decided or said this"):
            self.memory.remember("note", "Sin propietaria no se atribuye.")
        with self.assertRaisesRegex(MemoryError, "kind must be one of"):
            self.memory.remember("gossip", "x", performed_by="person:none")


if __name__ == "__main__":
    unittest.main()
