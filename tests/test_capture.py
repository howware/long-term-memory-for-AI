from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


def supply_document() -> dict:
    """Synthetic purchase and arrival with per-product quantities."""
    return {
        "source": {"type": "user_statement", "content": "Compra PO-1 a Proveedor A: 120 piezas de Dulce 1 y 80 de Dulce 2. "
                                                        "Llegó completa a Centro el 5 de enero."},
        "performed_by": "person:ana",
        "predicates": [
            {"id": "ordered_from", "value_type": "entity"},
            {"id": "received_at", "value_type": "entity"},
            {"id": "contains", "value_type": "entity"},
            {"id": "fulfills", "value_type": "entity"},
            {"id": "quantity", "value_type": "integer", "unit": "pieza"},
            {"id": "supplied_by", "value_type": "entity"},
        ],
        "entities": [
            {"key": "supplier:a", "type": "supplier", "name": "Proveedor A"},
            {"key": "hub:center", "type": "hub", "name": "Centro"},
            {"key": "product:1", "type": "product", "name": "Dulce 1"},
            {"key": "product:2", "type": "product", "name": "Dulce 2"},
        ],
        "facts": [
            {"subject": "product:1", "predicate": "supplied_by", "value": "supplier:a"},
            {"subject": "product:2", "predicate": "supplied_by", "value": "supplier:a"},
        ],
        "events": [
            {"key": "PO-1", "type": "purchase", "occurred_at": "2026-01-02T09:00:00Z",
             "links": [{"predicate": "ordered_from", "entity": "supplier:a"},
                       {"predicate": "contains", "entity": "product:1", "attributes": {"quantity": 120}},
                       {"predicate": "contains", "entity": "product:2", "attributes": {"quantity": 80}}]},
            {"key": "ARR-1", "type": "arrival", "occurred_at": "2026-01-05T11:00:00Z",
             "links": [{"predicate": "fulfills", "event": "PO-1"},
                       {"predicate": "received_at", "entity": "hub:center"},
                       {"predicate": "contains", "entity": "product:1", "attributes": {"quantity": 120}},
                       {"predicate": "contains", "entity": "product:2", "attributes": {"quantity": {"value": 80, "unit": "pieza"}}}]},
        ],
    }


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite")
        self.memory.init("Ana", "person:ana")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def count(self, table: str) -> int:
        with self.memory.connection() as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_line_quantities_are_kept_and_aggregated_through_paths(self) -> None:
        self.memory.write(supply_document())
        arrival = self.memory.event("ARR-1")
        lines = {link["target"]: link["attributes"]["quantity"] for link in arrival["links"] if link["predicate"] == "contains"}
        self.assertEqual(lines, {"Dulce 1": {"value": 120, "unit": "pieza"}, "Dulce 2": {"value": 80, "unit": "pieza"}})
        self.assertIn("Llegó completa", arrival["sources"][0]["content"])
        by_product = self.memory.aggregate(
            "arrival", "sum:quantity", by=["line"], line="contains", where=["link:received_at=hub:center"]
        )
        self.assertEqual([(row["line"], row["value"]) for row in by_product["groups"]], [("Dulce 1", 120), ("Dulce 2", 80)])
        self.assertEqual(by_product["unit"], "pieza")
        # Two hops across events, then a fact hop from each line's product.
        by_supplier = self.memory.aggregate(
            "arrival", "sum:quantity", by=["link:fulfills>link:ordered_from", "line>fact:supplied_by"], line="contains"
        )
        self.assertEqual(by_supplier["groups"][0]["value"], 200)
        self.assertEqual(by_supplier["groups"][0]["link:fulfills>link:ordered_from"], "Proveedor A")
        self.assertEqual(by_supplier["groups"][0]["line>fact:supplied_by"], "Proveedor A")

    def test_missing_measures_are_reported_not_zero(self) -> None:
        self.memory.write(supply_document())
        result = self.memory.aggregate("purchase", "sum:quantity")
        self.assertEqual(result["rows_without_measure"], 1)
        self.assertIsNone(result["groups"][0]["value"])

    def test_same_document_twice_is_idempotent(self) -> None:
        self.memory.write(supply_document())
        before = {table: self.count(table) for table in ("events", "evidence", "assertions", "record_links", "record_attributes")}
        report = self.memory.write(supply_document())
        self.assertEqual(report["events"]["existing"], ["PO-1", "ARR-1"])
        self.assertFalse(report["evidence"]["created"])
        self.assertEqual(report["facts"]["corroborated"], 2)
        self.assertEqual({table: self.count(table) for table in before}, before)

    def test_invalid_document_changes_nothing(self) -> None:
        document = supply_document()
        document["events"][1]["links"].append({"predicate": "not_declared", "entity": "hub:center"})
        before = {table: self.count(table) for table in ("entities", "events", "evidence", "predicates", "record_attributes")}
        with self.assertRaisesRegex(MemoryError, "Unknown predicate not_declared"):
            self.memory.write(document)
        self.assertEqual({table: self.count(table) for table in before}, before)

    def test_vocabulary_stays_consistent(self) -> None:
        source = {"type": "user_statement", "content": "vocabulario"}
        self.memory.write({"source": source, "predicates": [{"id": "units", "value_type": "integer", "unit": "pieza"}]})
        with self.assertRaisesRegex(MemoryError, "looks like existing units"):
            self.memory.write({"source": source, "predicates": [{"id": "units_received", "value_type": "integer"}]})
        # Sibling names that share a word but hold other values or relate in
        # another direction are different predicates, not near-duplicates.
        siblings = [{"id": "draft_of", "value_type": "entity"}, {"id": "draft_number", "value_type": "integer"},
                    {"id": "draft_kind", "value_type": "text"}, {"id": "in_draft", "value_type": "entity"}]
        for spec in siblings:
            self.memory.write({"source": source, "predicates": [spec]})
        with self.assertRaisesRegex(MemoryError, "looks like existing draft_of"):
            self.memory.write({"source": source, "predicates": [{"id": "draft", "value_type": "entity"}]})
        with self.assertRaisesRegex(MemoryError, "different value_type"):
            self.memory.write({"source": source, "predicates": [{"id": "units", "value_type": "real", "unit": "pieza"}]})
        event = {"type": "count", "occurred_at": "2026-01-01T00:00:00Z", "attributes": {"units": "doce"}}
        with self.assertRaisesRegex(MemoryError, "expects an integer"):
            self.memory.write({"source": source, "performed_by": "person:ana", "events": [event]})
        event["attributes"] = {"units": {"value": 12, "unit": "caja"}}
        with self.assertRaisesRegex(MemoryError, "measured in pieza"):
            self.memory.write({"source": source, "performed_by": "person:ana", "events": [event]})
        event["attributes"] = {"units": 12}
        self.memory.write({"source": source, "performed_by": "person:ana", "events": [event]})
        self.memory.write({"source": source, "predicates": [{"id": "weight", "value_type": "real"}]})
        drift = self.memory.write({"source": source, "performed_by": "person:ana",
                                   "events": [{**event, "attributes": {"weight": 1.5}}]})
        self.assertIn("count: attributes differ from earlier events; new: weight; missing: units", drift["warnings"])

    def test_events_need_a_person(self) -> None:
        with self.assertRaisesRegex(MemoryError, "performed_by is required"):
            self.memory.write({"source": {"type": "note", "content": "x"},
                               "events": [{"type": "note", "occurred_at": "2026-01-01T00:00:00Z"}]})

    def test_fact_changes_keep_history_and_retractions_keep_the_old_value(self) -> None:
        source = {"type": "user_statement", "content": "Ana vive en Norte desde 2020."}
        base = {
            "source": source,
            "predicates": [{"id": "lives_in", "value_type": "entity", "cardinality": "one"}],
            "entities": [{"key": "city:north", "type": "city", "name": "Norte"},
                         {"key": "city:south", "type": "city", "name": "Sur"}],
            "facts": [{"subject": "person:ana", "predicate": "lives_in", "value": "city:north", "valid_from": "2020-01-01T00:00:00Z"}],
        }
        self.memory.write(base)
        move = {"source": {"type": "user_statement", "content": "Ana se mudó a Sur."},
                "facts": [{"subject": "person:ana", "predicate": "lives_in", "value": "city:south"}]}
        with self.assertRaisesRegex(MemoryError, "needs valid_from"):
            self.memory.write(move)
        move["facts"][0]["valid_from"] = "2026-03-01T00:00:00Z"
        self.assertEqual(self.memory.write(move)["facts"]["superseded"], 1)
        facts = [fact for fact in self.memory.describe("person:ana")["facts"] if fact["predicate_id"] == "lives_in"]
        self.assertEqual([(fact["value_entity_name"], fact.get("valid_to")) for fact in facts],
                         [("Norte", "2026-03-01T00:00:00Z"), ("Sur", None)])
        with self.assertRaisesRegex(MemoryError, "needs a reason"):
            self.memory.write({"source": source, "performed_by": "person:ana",
                               "retract": [{"subject": "person:ana", "predicate": "lives_in"}]})
        self.memory.write({"source": {"type": "user_statement", "content": "Error: nunca vivió en Sur."},
                           "performed_by": "person:ana",
                           "retract": [{"subject": "person:ana", "predicate": "lives_in", "reason": "dato equivocado"}]})
        states = [fact["assertion_state"] for fact in self.memory.describe("person:ana")["facts"] if fact["predicate_id"] == "lives_in"]
        self.assertEqual(states, ["superseded", "retracted"])
        self.assertEqual(self.memory.timeline("person:ana")[-1]["event_type"], "assertion_retracted")

    def test_transitions_in_documents_follow_the_workflow(self) -> None:
        source = {"type": "test_fixture", "content": "workflow de lotes"}
        self.memory.write({
            "source": source,
            "entities": [
                {"key": "state:free", "type": "workflow_state", "name": "Libre"},
                {"key": "state:held", "type": "workflow_state", "name": "Apartado"},
                {"key": "state:closed", "type": "workflow_state", "name": "Cerrado"},
                {"key": "transition:free-held", "type": "workflow_transition", "name": "Libre a apartado"},
                {"key": "transition:held-closed", "type": "workflow_transition", "name": "Apartado a cerrado"},
                {"key": "lot:1", "type": "lot", "name": "Lote 1"},
            ],
            "facts": [
                {"subject": "state:free", "predicate": "requires_pending_action", "value": False},
                {"subject": "state:closed", "predicate": "is_terminal", "value": True},
                {"subject": "transition:free-held", "predicate": "from_workflow_state", "value": "state:free"},
                {"subject": "transition:free-held", "predicate": "to_workflow_state", "value": "state:held"},
                {"subject": "transition:held-closed", "predicate": "from_workflow_state", "value": "state:held"},
                {"subject": "transition:held-closed", "predicate": "to_workflow_state", "value": "state:closed"},
                # An available lot needs no pending action.
                {"subject": "lot:1", "predicate": "workflow_status", "value": "state:free", "valid_from": "2026-01-01T00:00:00Z"},
            ],
        })
        skip = {"source": source, "performed_by": "person:ana", "transitions": [
            {"instance": "lot:1", "to_state": "state:closed", "occurred_at": "2026-01-02T00:00:00Z"}]}
        with self.assertRaisesRegex(MemoryError, "not allowed"):
            self.memory.write(skip)
        held = {"source": source, "performed_by": "person:ana", "transitions": [
            {"instance": "lot:1", "to_state": "state:held", "occurred_at": "2026-01-02T00:00:00Z"}]}
        with self.assertRaisesRegex(MemoryError, "requires pending_for"):
            self.memory.write(held)
        held["transitions"][0]["pending_for"] = "person:ana"
        held["transitions"].append({"instance": "lot:1", "to_state": "state:closed", "occurred_at": "2026-01-03T00:00:00Z"})
        self.assertEqual(self.memory.write(held)["transitions"], 2)
        self.assertEqual(self.memory.detail("lot:1"), {**self.memory.detail("lot:1"), "state": "Cerrado", "pending_for": None})

    def test_memory_is_append_only(self) -> None:
        import sqlite3

        self.memory.write(supply_document())
        with self.memory.connection() as connection:
            for statement in (
                "DELETE FROM events",
                "DELETE FROM entities WHERE external_key = 'product:1'",
                "DELETE FROM assertions",
                "UPDATE events SET summary = 'otro' WHERE external_key = 'PO-1'",
                "UPDATE record_attributes SET value_integer = 1",
                "UPDATE evidence SET content = 'otra'",
                "UPDATE entities SET canonical_name = 'Otro' WHERE external_key = 'product:1'",
                "UPDATE predicates SET value_type = 'text' WHERE predicate_id = 'quantity'",
                "UPDATE assertions SET value_entity_id = NULL, value_type = 'text', value_text = 'x' WHERE predicate_id = 'supplied_by'",
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only", msg=statement):
                    connection.execute(statement)
            # Filling a gap is allowed; overwriting it afterwards is not.
            connection.execute("UPDATE entities SET description = 'Dulce de prueba' WHERE external_key = 'product:1'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("UPDATE entities SET description = 'Otra' WHERE external_key = 'product:1'")
        # The helper's own corrections still work: supersede and retract.
        source = {"type": "user_statement", "content": "cambio de proveedor"}
        self.memory.write({"source": source, "predicates": [{"id": "category", "value_type": "text", "cardinality": "one"}],
                           "facts": [{"subject": "product:1", "predicate": "category", "value": "dulce", "valid_from": "2026-01-01T00:00:00Z"},
                                     {"subject": "product:1", "predicate": "category", "value": "chocolate", "valid_from": "2026-02-01T00:00:00Z"}]})
        self.memory.write({"source": source, "performed_by": "person:ana",
                           "retract": [{"subject": "product:1", "predicate": "category", "reason": "prueba"}]})
        states = [fact["assertion_state"] for fact in self.memory.describe("product:1")["facts"] if fact["predicate_id"] == "category"]
        self.assertEqual(states, ["superseded", "retracted"])

    def test_voided_events_stop_counting_but_stay_recorded(self) -> None:
        self.memory.write(supply_document())
        before = self.memory.aggregate("arrival", "sum:quantity", line="contains")["groups"][0]["value"]
        with self.assertRaisesRegex(MemoryError, "needs the event"):
            self.memory.void("ARR-1", "")
        result = self.memory.void("ARR-1", "Llegada duplicada por error de captura.")
        self.assertEqual(result["voided"], ["ARR-1"])
        self.assertEqual(result["event"]["voided"]["reason"], "Llegada duplicada por error de captura.")
        self.assertEqual(result["event"]["voided"]["by"], "Ana")
        # Excluded from reads by default, still there on request.
        self.assertEqual(self.memory.aggregate("arrival", "sum:quantity", line="contains")["total_rows"], 0)
        self.assertEqual(self.memory.aggregate("arrival", "sum:quantity", line="contains", include_voided=True)["groups"][0]["value"], before)
        self.assertEqual([event["event_type"] for event in self.memory.timeline("hub:center")], [])
        shown = self.memory.timeline("hub:center", include_voided=True)
        self.assertEqual([(event["event_type"], event.get("voided")) for event in shown], [("arrival", 1)])
        voided = next(item for item in self.memory.catalog()["event_types"] if item["event_type"] == "arrival")
        self.assertEqual((voided["count"], voided["voided"]), (1, 1))
        self.assertIn("ARR-1 was already voided", self.memory.void("ARR-1", "otra vez")["warnings"])
        # Voiding the void restores the original; a longer chain is refused.
        void_id = result["event"]["voided"]["event_id"]
        self.memory.void(void_id, "La anulación fue un error.")
        self.assertEqual(self.memory.aggregate("arrival", "sum:quantity", line="contains")["groups"][0]["value"], before)
        undo_id = self.memory.event(void_id)["voided"]["event_id"]
        with self.assertRaisesRegex(MemoryError, "Only one undo"):
            self.memory.void(undo_id, "cadena")

    def test_void_rules(self) -> None:
        self.memory.write(supply_document())
        warnings = self.memory.void("PO-1", "Compra cancelada con el proveedor.")["warnings"]
        self.assertIn("1 active event(s) link to PO-1 through fulfills; void them too if they depended on it", warnings)
        self.memory.write({"source": {"type": "test_fixture", "content": "agente"}, "recorded_by": "agent:test"})
        with self.assertRaisesRegex(MemoryError, "Voiding an event is a decision of a person"):
            self.memory.void("ARR-1", "un agente no anula", performed_by="agent:test")
        with self.memory.connection() as connection:
            transition = connection.execute(
                "INSERT INTO events(event_id, event_type, summary, occurred_at, recorded_at) "
                "VALUES ('t1', 'workflow_transition', 'x', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
            )
        with self.assertRaisesRegex(MemoryError, "compensating transition"):
            self.memory.void("t1", "no se anula")

    def test_renamed_entity_keeps_both_names(self) -> None:
        source = {"type": "user_statement", "content": "nombres"}
        self.memory.write({"source": source, "entities": [{"key": "org:acme", "type": "organization", "name": "Acme"}]})
        report = self.memory.write({"source": source, "entities": [{"key": "org:acme", "type": "organization", "name": "Acme SA"}]})
        self.assertTrue(report["warnings"])
        self.assertEqual(self.memory.detail("Acme SA")["name"], "Acme")

    def test_recall_finds_unstructured_source_text(self) -> None:
        self.memory.write({"source": {"type": "user_statement", "content": "La decisión: usar SQLite y no archivos Markdown."}})
        hits = self.memory.recall("decision markdown")
        self.assertEqual(hits[0]["record_type"], "evidence")
        self.assertIn("[decisión]", hits[0]["snippet"])

    def test_aggregation_uses_the_attribute_index(self) -> None:
        self.memory.write(supply_document())
        with self.memory.connection() as connection:
            plan = connection.execute(
                """EXPLAIN QUERY PLAN SELECT value_integer FROM record_attributes
                    WHERE record_type = 'record_link' AND record_id = 'x' AND predicate_id = 'quantity'"""
            ).fetchall()
        self.assertTrue(any("USING INDEX record_attributes_" in row[3] for row in plan), [tuple(row) for row in plan])


if __name__ == "__main__":
    unittest.main()
