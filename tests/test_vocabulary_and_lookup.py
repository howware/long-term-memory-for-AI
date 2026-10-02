from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


class VocabularyAndLookupTests(unittest.TestCase):
    """Findings of the multi-agent exercise on the model and the helper:
    allowed values as data, key/name in paths, 'now', identifiers, homonyms."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        self.memory.write({
            "source": {"type": "test_fixture", "content": "base"}, "performed_by": "person:ana",
            "predicates": [{"id": "deliverable", "value_type": "text"}, {"id": "for_customer", "value_type": "entity"},
                           {"id": "shown", "value_type": "entity", "cardinality": "many"},
                           {"id": "id_number", "value_type": "text"}, {"id": "plate", "value_type": "text"}],
            "entities": [{"key": "person:juan", "type": "person", "name": "Juan Pérez"},
                         {"key": "doc:id", "type": "document_type", "name": "Identificación"},
                         {"key": "case:1", "type": "case", "name": "Caso 1"}],
            "facts": [{"subject": "case:1", "predicate": "for_customer", "value": "person:juan"},
                      {"subject": "case:1", "predicate": "shown", "value": "doc:id", "attributes": {"id_number": "ID-555"}},
                      {"subject": "person:juan", "predicate": "plate", "value": "ABC1234"}],
            "events": [{"key": "E1", "type": "delivery", "occurred_at": "2026-01-01T10:00:00Z",
                        "attributes": {"deliverable": "licencia"}}],
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def deliver(self, key: str, value: str) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": f"entrega {key}"}, "performed_by": "person:ana",
                           "events": [{"key": key, "type": "delivery", "occurred_at": "now",
                                       "attributes": {"deliverable": value}}]})

    def test_allowed_values_are_data_with_history(self) -> None:
        result = self.memory.define_values("deliverable", ["placas", "licencia de conducir"], "person:ana")
        self.assertEqual(result["values"], ["licencia de conducir", "placas"])
        self.deliver("E2", "placas")
        with self.assertRaisesRegex(MemoryError, r"deliverable only accepts 'licencia de conducir', 'placas'; got 'licencia'"):
            self.deliver("E3", "licencia")
        # Narrowing the list closes a value; earlier data keeps its meaning.
        self.memory.define_values("deliverable", ["licencia de conducir"], "person:ana")
        with self.assertRaisesRegex(MemoryError, "only accepts 'licencia de conducir'"):
            self.deliver("E4", "placas")
        facts = self.memory.describe("predicate:deliverable")["facts"]
        self.assertEqual(sorted((fact["value_text"], "valid_to" in fact) for fact in facts
                                if fact["predicate_id"] == "allowed_value"),
                         [("licencia de conducir", False), ("placas", True)])
        self.assertEqual(self.memory.aggregate("delivery", by=["attr:deliverable"])["total_rows"], 2)
        entry = next(item for item in self.memory.catalog()["predicates"] if item["predicate_id"] == "deliverable")
        self.assertEqual((entry["allowed_values"], entry["outside_vocabulary"]),
                         (["licencia de conducir"], {"licencia": 1, "placas": 1}))

    def test_entity_predicates_may_fix_the_type_they_point_to(self) -> None:
        self.memory.define_values("for_customer", None, "person:ana", entity_type="person")
        with self.assertRaisesRegex(MemoryError, "for_customer points to person entities; 'doc:id' is a document_type"):
            self.memory.write({"source": {"type": "test_fixture", "content": "mal"}, "performed_by": "person:ana",
                               "facts": [{"subject": "case:1", "predicate": "for_customer", "value": "doc:id"}]})
        with self.assertRaisesRegex(MemoryError, "allowed values are for text"):
            self.memory.define_values("for_customer", ["x"], "person:ana")

    def test_key_and_name_work_inside_paths_and_conditions(self) -> None:
        rows = self.memory.list_rows(entity_type="case", where=["key = case:1"],
                                     columns=["fact:for_customer>key", "fact:for_customer>name"])["rows"]
        self.assertEqual(rows, [{"fact:for_customer>key": "person:juan", "fact:for_customer>name": "Juan Pérez"}])
        self.assertEqual(self.memory.aggregate("delivery", where=["key = E1"])["total_rows"], 1)
        self.deliver("E9", "licencia")
        self.memory.write({"source": {"type": "test_fixture", "content": "resumen"}, "performed_by": "person:ana",
                           "events": [{"key": "E10", "type": "delivery", "occurred_at": "now", "summary": "Second batch"}]})
        self.assertEqual([row["key"] for row in self.memory.list_rows("delivery", where=["summary = Second batch"],
                                                                      columns=["key"])["rows"]], ["E10"])
        with self.assertRaisesRegex(MemoryError, "has:PATH / no:PATH"):
            self.memory.aggregate("delivery", where=["attr:deliverable exists"])

    def test_now_is_accepted_where_an_instant_is_written(self) -> None:
        self.deliver("E5", "licencia")
        self.assertTrue(self.memory.event("E5")["occurred_at"].startswith("20"))

    def test_search_finds_identifiers_and_warns_about_namesakes(self) -> None:
        self.assertEqual(self.memory.search("ABC1234")[0]["external_key"], "person:juan")
        by_attribute = self.memory.search("ID-555")[0]
        self.assertEqual((by_attribute["external_key"], by_attribute["matched"]), ("case:1", "shown>id_number"))
        report = self.memory.write({"source": {"type": "test_fixture", "content": "otro Juan"}, "performed_by": "person:ana",
                                    "entities": [{"key": "person:juan2", "type": "person", "name": "juan pérez"}]})
        self.assertTrue(any("same name as person:juan" in warning for warning in report["warnings"]))

    def test_process_definition_is_returned_ready_to_edit(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "rol"}, "performed_by": "person:ana",
                           "entities": [{"key": "role:clerk", "type": "role", "name": "Clerk"}]})
        definition = {"states": [{"name": "A"}, {"name": "B", "terminal": True}],
                      "transitions": [{"from": None, "to": "A", "roles": ["role:clerk"]},
                                      {"from": "A", "to": "B", "roles": ["role:clerk"]}]}
        self.memory.define_process("process:p", "P", definition, "person:ana", owner="person:ana")
        current = self.memory.process_definition("process:p")
        self.assertEqual((current["version"], current["states"][1]["name"]), (1, "B"))
        with self.assertRaisesRegex(MemoryError, "no version 9"):
            self.memory.process_definition("process:p", 9)


if __name__ == "__main__":
    unittest.main()
