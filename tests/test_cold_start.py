from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, recall_term, render_brief  # noqa: E402


class ColdStartTests(unittest.TestCase):
    """Findings of the second cold test: what a new agent needs to find
    without guessing (truncated brief, agreements, summaries, plurals, vocabularies)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_brief_says_what_it_leaves_out_and_lists_agreements(self) -> None:
        for number in range(10):
            self.memory.remember("decision", f"Decisión número {number}", key=f"decision:d{number}")
        self.memory.remember("open_question", "¿Cómo se respaldan las bases?", key="question:backups")
        self.memory.remember("answer", "Se copian a backups/ antes de migrar.", key="answer:backups",
                             resolves="question:backups")
        brief = self.memory.brief()
        decisions = brief["decisions"]
        self.assertEqual((decisions["open"], len(decisions["latest"])), (11, 8))
        self.assertEqual(decisions["latest"][0]["key"], "answer:backups")
        self.assertEqual(decisions["latest"][0]["resolves"], "question:backups")
        text = render_brief(brief)
        self.assertIn("showing 8 of 11", text)
        self.assertIn("(answers question:backups)", text)
        self.assertIn("See all: memory.py list --event-type decision", text)
        # The suggested listing really returns every decision in force.
        listed = self.memory.list_rows("decision", columns=["key", "summary"], where=["no:link-in:supersedes"], limit=100)
        self.assertEqual(listed["total"], 10)
        self.assertEqual(brief["open_questions"]["open"], 0)

    def test_an_agents_answer_to_its_own_question_is_not_an_agreement(self) -> None:
        self.memory.remember("open_question", "¿Falta un índice?", key="question:index", as_agent=True, agent="agent:codex")
        self.memory.remember("answer", "No hacía falta.", key="answer:index", resolves="question:index",
                             as_agent=True, agent="agent:codex")
        self.assertEqual(self.memory.brief()["decisions"]["open"], 0)

    def test_list_shows_event_summaries_and_entity_descriptions(self) -> None:
        self.memory.remember("decision", "Usar UTC al guardar", key="decision:utc")
        rows = self.memory.list_rows("decision", columns=["key", "summary"])["rows"]
        self.assertEqual(rows, [{"key": "decision:utc", "summary": "Usar UTC al guardar"}])
        people = self.memory.list_rows(entity_type="person", columns=["key", "summary"])["rows"]
        self.assertEqual(people[0]["summary"], "Owner of this memory.")

    def test_recall_finds_singular_and_plural(self) -> None:
        self.memory.remember("note", "Las migraciones transformadoras exigen respaldo", key="note:plural")
        for query in ("transformadora", "transformadoras", "migración", "respaldos"):
            keys = [item.get("external_key") for item in self.memory.recall(query)]
            self.assertIn("note:plural", keys, query)
        self.assertEqual(recall_term("mes"), '"mes"')
        self.assertEqual(recall_term("decisiones"), '"decision"*')

    def test_catalog_shows_vocabularies_states_and_meaningful_people(self) -> None:
        self.memory.write({
            "source": {"type": "test_fixture", "content": "vocabulario"}, "performed_by": "person:ana",
            "predicates": [{"id": "job_title", "value_type": "text"}, {"id": "channel", "value_type": "entity"},
                           {"id": "concerns", "value_type": "entity"}],
            "entities": [{"key": "channel:web", "type": "channel", "name": "Web"},
                         {"key": "channel:phone", "type": "channel", "name": "Teléfono"},
                         *({"key": f"person:guest{n}", "type": "person", "name": f"Huésped {n}"} for n in range(60)),
                         {"key": "person:clerk", "type": "person", "name": "Recepción 1"}],
            "facts": [{"subject": "person:clerk", "predicate": "job_title", "value": "recepción"}],
            "events": [{"key": f"B{n}", "type": "booking", "occurred_at": "2026-01-01T00:00:00Z",
                        "attributes": {"channel": "channel:web" if n % 2 else "channel:phone"}} for n in range(4)],
        })
        self.memory.define_status_rule("status:booking", "Estatus", {
            "applies_to": {"event_type": "booking"}, "follow_link": "concerns",
            "states": [{"name": "Cancelada", "event_type": "booking_cancelled"}], "otherwise": "Abierta"})
        catalog = self.memory.catalog()
        types = {item["entity_type"]: item for item in catalog["entity_types"]}
        self.assertEqual(types["channel"]["names"], ["Teléfono", "Web"])
        self.assertNotIn("names", types["person"])
        predicates = {item["predicate_id"]: item for item in catalog["predicates"]}
        self.assertEqual(predicates["job_title"]["values"], {"recepción": 1})
        self.assertEqual(predicates["channel"]["values"], {"Teléfono": 2, "Web": 2})
        rule = next(item for item in catalog["semantic_rules_and_recipes"] if item["external_key"] == "status:booking")
        self.assertEqual(rule["states"], ["Cancelada", "Abierta"])
        self.assertEqual(catalog["people_total"], 62)
        self.assertEqual({person["external_key"] for person in catalog["people"]}, {"person:ana", "person:clerk"})


if __name__ == "__main__":
    unittest.main()
