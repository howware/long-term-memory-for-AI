from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402

V1 = {
    "description": "Pedido simple",
    "states": [{"name": "Recibido", "pending": "role:clerk"}, {"name": "Por pagar", "pending": "role:cashier"},
               {"name": "Pagado", "pending": "role:clerk"}, {"name": "Entregado", "terminal": True}],
    "transitions": [{"from": None, "to": "Recibido", "roles": ["role:clerk"]},
                    {"from": "Recibido", "to": "Por pagar", "roles": ["role:clerk"]},
                    {"from": "Por pagar", "to": "Pagado", "roles": ["role:cashier"]},
                    {"from": "Pagado", "to": "Entregado", "roles": ["role:clerk"]}],
    "override_roles": ["role:supervisor", "role:owner"],
}
# Version 2 adds a review between payment and delivery.
V2 = {**V1, "states": [*V1["states"][:3], {"name": "Revisado", "pending": "role:clerk"}, V1["states"][3]],
      "transitions": [*V1["transitions"][:3], {"from": "Pagado", "to": "Revisado", "roles": ["role:supervisor"]},
                      {"from": "Revisado", "to": "Entregado", "roles": ["role:clerk"]}]}


def at(day: int, hour: int) -> str:
    return f"2026-03-{day:02d}T{hour:02d}:00:00Z"


class ProcessTests(unittest.TestCase):
    """A process is versioned data; an instance keeps the version it started
    under; staff are held to the process; supervisors may step outside it,
    leaving a record; only the owner changes the process."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Olga", "person:olga")
        people = {"person:carl": "role:clerk", "person:cata": "role:cashier", "person:sam": "role:supervisor",
                  "person:olga": "role:owner"}
        self.memory.write({
            "source": {"type": "test_fixture", "content": "equipo y roles"}, "performed_by": "person:olga",
            "entities": [*({"key": role, "type": "role", "name": role.split(":")[1]} for role in sorted(set(people.values()))),
                         *({"key": person, "type": "person", "name": person.split(":")[1].title()}
                           for person in people if person != "person:olga"),
                         *({"key": f"order:{n}", "type": "order", "name": f"Pedido {n}"} for n in range(1, 5))],
            "facts": [{"subject": person, "predicate": "holds_role", "value": role, "valid_from": at(1, 0)}
                      for person, role in people.items()],
        })
        self.memory.define_process("process:order", "Pedido", V1, "person:olga", owner="person:olga", effective_from=at(1, 1))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_steps_follow_the_process_and_its_roles(self) -> None:
        status = self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        self.assertEqual((status["step"], status["version"], status["pending_for"]), ("Recibido", 1, "clerk"))
        self.memory.advance_process("order:1", "Por pagar", "person:carl", at(2, 10))
        with self.assertRaisesRegex(MemoryError, "may not move Por pagar -> Pagado; allowed roles: role:cashier"):
            self.memory.advance_process("order:1", "Pagado", "person:carl", at(2, 11))
        with self.assertRaisesRegex(MemoryError, "not in version 1 of the process"):
            self.memory.advance_process("order:1", "Entregado", "person:cata", at(2, 11))
        self.memory.advance_process("order:1", "Pagado", "person:cata", at(2, 11))
        done = self.memory.advance_process("order:1", "Entregado", "person:carl", at(2, 12))
        self.assertTrue(done["terminal"])
        self.assertIsNone(done["pending_for"])
        self.assertEqual([item["by"] for item in done["history"]], ["Carl", "Carl", "Cata", "Carl"])
        # One row per step, worked by whoever moved it out; the last one is current.
        steps = self.memory.process_status("order:1")["steps"]
        self.assertEqual([(step["step"], step.get("left_by")) for step in steps],
                         [("Recibido", "Carl"), ("Por pagar", "Cata"), ("Pagado", "Carl"), ("Entregado", None)])
        self.assertTrue(steps[-1]["current"])
        with self.assertRaisesRegex(MemoryError, "not in version 1"):
            self.memory.advance_process("order:1", "Pagado", "person:carl", at(2, 13))

    def test_supervisors_may_step_outside_with_a_reason_and_it_is_logged(self) -> None:
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        with self.assertRaisesRegex(MemoryError, "must give --reason"):
            self.memory.advance_process("order:1", "Pagado", "person:sam", at(2, 10))
        status = self.memory.advance_process("order:1", "Pagado", "person:sam", at(2, 10), reason="Pago previo en efectivo")
        self.assertEqual(status["step"], "Pagado")
        logged = self.memory.list_rows("process_transition", where=["attr:override = true"],
                                       columns=["participant:performed_by", "attr:override_kind", "attr:rationale"])
        self.assertEqual(logged["rows"], [{"participant:performed_by": "Sam", "attr:override_kind": "outside_process",
                                           "attr:rationale": "Pago previo en efectivo"}])

    def test_running_instances_keep_their_version(self) -> None:
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        self.memory.advance_process("order:1", "Por pagar", "person:carl", at(2, 10))
        self.memory.advance_process("order:1", "Pagado", "person:cata", at(2, 11))
        with self.assertRaisesRegex(MemoryError, "Only the process owner"):
            self.memory.define_process("process:order", "Pedido", V2, "person:sam", effective_from=at(3, 0))
        self.assertEqual(self.memory.define_process("process:order", "Pedido", V2, "person:olga", effective_from=at(3, 0),
                                                    reason="Revisión antes de entregar")["version"], 2)
        # Started under v1: it still goes straight to delivery.
        old = self.memory.advance_process("order:1", "Entregado", "person:carl", at(3, 9))
        self.assertEqual((old["version"], old["latest_version"], old["step"]), (1, 2, "Entregado"))
        new = self.memory.start_process("order:2", "process:order", "person:carl", at(3, 9))
        self.assertEqual(new["version"], 2)
        for step, person, hour in (("Por pagar", "person:carl", 10), ("Pagado", "person:cata", 11)):
            self.memory.advance_process("order:2", step, person, at(3, hour))
        with self.assertRaisesRegex(MemoryError, "may go to: Revisado"):
            self.memory.advance_process("order:2", "Entregado", "person:carl", at(3, 12))
        self.assertEqual(self.memory.process_status("order:2")["next"], [{"to": "Revisado", "roles": ["role:supervisor"]}])
        counts = self.memory.aggregate("process_transition", by=["attr:process_version"])["groups"]
        self.assertEqual({row["attr:process_version"]: row["value"] for row in counts}, {1: 4, 2: 3})

    def test_migration_is_an_explicit_plan_of_the_owner(self) -> None:
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        self.memory.define_process("process:order", "Pedido", V2, "person:olga", effective_from=at(3, 0))
        with self.assertRaisesRegex(MemoryError, "Only the process owner"):
            self.memory.migrate_process("order:1", 2, "person:sam", "plan", occurred_at=at(3, 8))
        status = self.memory.migrate_process("order:1", 2, "person:olga", "Todo lo abierto pasa a v2", occurred_at=at(3, 8))
        self.assertEqual((status["version"], status["step"]), (2, "Recibido"))
        self.assertEqual(status["history"][-1]["event_type"], "process_migrated")

    def test_roles_have_validity(self) -> None:
        # Carl covers the cash desk for one morning.
        self.memory.write({"source": {"type": "test_fixture", "content": "Carl cubre caja el 2 en la mañana"},
                           "performed_by": "person:sam",
                           "facts": [{"subject": "person:carl", "predicate": "holds_role", "value": "role:cashier",
                                      "valid_from": at(2, 8), "valid_to": at(2, 13)}]})
        for number in (1, 2):
            self.memory.start_process(f"order:{number}", "process:order", "person:carl", at(2, 9))
            self.memory.advance_process(f"order:{number}", "Por pagar", "person:carl", at(2, 10))
        self.memory.advance_process("order:1", "Pagado", "person:carl", at(2, 12))
        with self.assertRaisesRegex(MemoryError, "may not move"):
            self.memory.advance_process("order:2", "Pagado", "person:carl", at(2, 14))

    def test_transitions_may_require_facts_on_the_instance(self) -> None:
        coded = {**V1, "transitions": [{**V1["transitions"][0], "requires": ["tracking_code"]}, *V1["transitions"][1:]]}
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))  # v1: no code needed
        self.memory.define_process("process:order", "Pedido", coded, "person:olga", effective_from=at(3, 0))
        with self.assertRaisesRegex(MemoryError, r"requires tracking_code on the instance \(version 2\)"):
            self.memory.start_process("order:2", "process:order", "person:carl", at(3, 9))
        code = self.memory.assign_tracking_code("order:2", "person:carl", occurred_at=at(3, 8))["tracking_code"]
        self.assertEqual(self.memory.start_process("order:2", "process:order", "person:carl", at(3, 9))["version"], 2)
        # The code finds the case, as a key would.
        self.assertEqual(self.memory.process_status(code.lower())["instance"], "order:2")
        self.assertEqual(self.memory.search(code)[0]["external_key"], "order:2")
        # A supervisor may start one without the code, with a reason on record.
        self.memory.start_process("order:3", "process:order", "person:sam", at(3, 10), reason="Sistema de códigos caído")
        kinds = self.memory.list_rows("process_transition", where=["attr:override = true"], columns=["attr:override_kind"])
        self.assertEqual([row["attr:override_kind"] for row in kinds["rows"]], ["role_not_allowed, missing_requirement"])
        self.assertEqual(self.memory.process_status("order:1")["next"][0].get("requires"), None)

    def test_steps_may_demand_the_data_they_capture(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "datos de cobro"}, "performed_by": "person:olga",
                           "predicates": [{"id": "method", "value_type": "text"}, {"id": "cost", "value_type": "real"},
                                          {"id": "received", "value_type": "real"}]})
        paying = {"from": "Por pagar", "to": "Pagado", "roles": ["role:cashier"],
                  "captures": [{"attribute": "method"}, {"attribute": "cost"},
                               {"attribute": "received", "when": {"method": "efectivo"}}]}
        v2 = {**V1, "transitions": [*V1["transitions"][:2], paying, V1["transitions"][3]]}
        with self.assertRaisesRegex(MemoryError, "Unknown predicate"):
            self.memory.define_process("process:order", "Pedido",
                                       {**v2, "transitions": [*v2["transitions"][:2], {**paying, "captures": [{"attribute": "nope"}]},
                                                              v2["transitions"][3]]}, "person:olga")
        self.memory.define_process("process:order", "Pedido", v2, "person:olga", effective_from=at(3, 0))
        for number in (1, 2):
            self.memory.start_process(f"order:{number}", "process:order", "person:carl", at(3, 9))
            self.memory.advance_process(f"order:{number}", "Por pagar", "person:carl", at(3, 10))
        with self.assertRaisesRegex(MemoryError, r"must record method, cost \(version 2\)"):
            self.memory.advance_process("order:1", "Pagado", "person:cata", at(3, 11))
        with self.assertRaisesRegex(MemoryError, r"must record received \(when method=efectivo\)"):
            self.memory.advance_process("order:1", "Pagado", "person:cata", at(3, 11),
                                        attributes={"method": "efectivo", "cost": 45.0})
        self.memory.advance_process("order:1", "Pagado", "person:cata", at(3, 11),
                                    attributes={"method": "efectivo", "cost": 45.0, "received": 50.0})
        self.memory.advance_process("order:2", "Pagado", "person:cata", at(3, 11), attributes={"method": "tarjeta", "cost": 45.0})
        self.assertEqual(self.memory.process_status("order:1")["history"][-1]["by"], "Cata")

    def test_fact_attributes_are_visible_and_reachable(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "documento"}, "performed_by": "person:carl",
                           "predicates": [{"id": "shown", "value_type": "entity", "cardinality": "many"},
                                          {"id": "number", "value_type": "text"}],
                           "entities": [{"key": "doc:id", "type": "document_type", "name": "Identificación"}],
                           "facts": [{"subject": "order:1", "predicate": "shown", "value": "doc:id",
                                      "attributes": {"number": "A-77"}}]})
        facts = [fact for fact in self.memory.describe("order:1")["facts"] if fact["predicate_id"] == "shown"]
        self.assertEqual(facts[0]["attributes"], {"number": "A-77"})
        rows = self.memory.list_rows(entity_type="order", where=["fact:shown>edge:number = A-77"],
                                     columns=["key", "fact:shown>edge:number"])["rows"]
        self.assertEqual(rows, [{"key": "order:1", "fact:shown>edge:number": "A-77"}])

    def test_how_long_an_instance_has_been_in_its_step(self) -> None:
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        self.memory.start_process("order:2", "process:order", "person:carl", at(2, 11))
        self.memory.advance_process("order:2", "Por pagar", "person:carl", at(2, 12))
        rows = self.memory.list_rows(entity_type="order", where=["has:fact:process_step"],
                                     columns=["key", "fact:process_step>since"], order_by=["fact:process_step>since"])["rows"]
        self.assertEqual([(row["key"], row["fact:process_step>since"]) for row in rows],
                         [("order:1", "2026-03-02T09:00:00Z"), ("order:2", "2026-03-02T12:00:00Z")])
        self.assertEqual(self.memory.aggregate(None, entity_type="order",
                                               where=["fact:process_step>since < 2026-03-02T10:00:00Z"])["total_rows"], 1)
        with self.assertRaisesRegex(MemoryError, "must follow a fact step"):
            self.memory.list_rows(entity_type="order", columns=["since"])

    def test_assigned_codes_keep_their_evidence(self) -> None:
        code = self.memory.assign_tracking_code("order:1", "person:carl", source_text="Carl: dale número al pedido 1",
                                                occurred_at=at(2, 8))["tracking_code"]
        fact = next(fact for fact in self.memory.describe("order:1")["facts"] if fact["predicate_id"] == "tracking_code")
        self.assertEqual((fact["value_text"], fact["evidence_count"]), (code, 1))
        self.assertIn("[dale] [número]", self.memory.recall("dale número al pedido")[0]["snippet"])

    def test_exceptions_leave_a_visible_trace_of_skipped_data(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "dato de cobro"}, "performed_by": "person:olga",
                           "predicates": [{"id": "method", "value_type": "text"}]})
        paying = {"from": "Por pagar", "to": "Pagado", "roles": ["role:cashier"], "captures": [{"attribute": "method"}]}
        v2 = {**V1, "transitions": [*V1["transitions"][:2], paying, V1["transitions"][3]]}
        self.memory.define_process("process:order", "Pedido", v2, "person:olga", effective_from=at(3, 0))
        self.memory.start_process("order:1", "process:order", "person:carl", at(3, 9))
        status = self.memory.advance_process("order:1", "Pagado", "person:sam", at(3, 10), reason="Pagó por adelantado")
        self.assertEqual(len(status["warnings"]), 1)
        self.assertIn("Sam made an exception that left unrecorded: Por pagar -> Pagado: method", status["warnings"][0])

    def test_a_captured_value_may_have_to_match_an_earlier_step(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "recibo"}, "performed_by": "person:olga",
                           "predicates": [{"id": "receipt", "value_type": "text"}]})
        paying = {**V1["transitions"][2], "captures": [{"attribute": "receipt"}]}
        delivering = {**V1["transitions"][3], "captures": [{"attribute": "receipt", "equals_earlier": "receipt"}]}
        v2 = {**V1, "transitions": [*V1["transitions"][:2], paying, delivering]}
        self.memory.define_process("process:order", "Pedido", v2, "person:olga", effective_from=at(3, 0))
        self.memory.start_process("order:1", "process:order", "person:carl", at(3, 9))
        self.memory.advance_process("order:1", "Por pagar", "person:carl", at(3, 10))
        self.memory.advance_process("order:1", "Pagado", "person:cata", at(3, 11), attributes={"receipt": "R-1"})
        with self.assertRaisesRegex(MemoryError, "receipt 'R-9' does not match receipt 'R-1' recorded earlier"):
            self.memory.advance_process("order:1", "Entregado", "person:carl", at(3, 12), attributes={"receipt": "R-9"})
        self.assertTrue(self.memory.advance_process("order:1", "Entregado", "person:carl", at(3, 12),
                                                    attributes={"receipt": "R-1"})["terminal"])

    def test_types_nobody_could_start_are_reported(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "tipos y documentos"}, "performed_by": "person:olga",
                           "predicates": [{"id": "order_type", "value_type": "entity", "cardinality": "one", "is_temporal": False},
                                          {"id": "needs", "value_type": "entity", "cardinality": "many"},
                                          {"id": "shown", "value_type": "entity", "cardinality": "many"}],
                           "entities": [{"key": "kind:license", "type": "order_kind", "name": "Licencia"},
                                        {"key": "kind:plates", "type": "order_kind", "name": "Placas"},
                                        {"key": "kind:renewal", "type": "order_kind", "name": "Renovación"},
                                        {"key": "doc:license", "type": "document_type", "name": "Licencia vigente"},
                                        {"key": "doc:plates", "type": "document_type", "name": "Placas anteriores"},
                                        {"key": "doc:registry", "type": "document_type", "name": "Registro vehicular"}],
                           "facts": [{"subject": "doc:license", "predicate": "produced_by", "value": "kind:license"},
                                     {"subject": "doc:plates", "predicate": "produced_by", "value": "kind:plates"},
                                     {"subject": "doc:plates", "predicate": "produced_by", "value": "kind:renewal"},
                                     {"subject": "doc:registry", "predicate": "produced_by", "value": "kind:plates"},
                                     {"subject": "kind:plates", "predicate": "needs", "value": "doc:license"},
                                     # Renewal needs what it also produces, but plates produce it too: a way in exists.
                                     {"subject": "kind:renewal", "predicate": "needs", "value": "doc:plates"}]})
        checking = {**V1["transitions"][1], "requires_each": [{"of": "order_type>needs", "in": "shown"}]}
        self.memory.define_process("process:order", "Pedido", {**V1, "transitions": [V1["transitions"][0], checking,
                                                                                     *V1["transitions"][2:]]}, "person:olga")
        self.assertNotIn("unstartable_types", self.memory.catalog()["processes"][0])
        report = self.memory.write({"source": {"type": "test_fixture", "content": "licencia pide registro"},
                                    "performed_by": "person:olga",
                                    "facts": [{"subject": "kind:license", "predicate": "needs", "value": "doc:registry"}]})
        blocked = [warning for warning in report["warnings"] if warning.startswith("Nobody could start")]
        self.assertEqual(sorted(blocked), [
            "Nobody could start Licencia: every producer of Registro vehicular is itself blocked",
            "Nobody could start Placas: every producer of Licencia vigente is itself blocked",
            "Nobody could start Renovación: every producer of Placas anteriores is itself blocked",
        ])
        listed = self.memory.catalog()["processes"][0]["unstartable_types"]
        self.assertEqual({item["type"] for item in listed}, {"Licencia", "Placas", "Renovación"})

    def test_exception_requests_are_part_of_the_process(self) -> None:
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        asked = self.memory.request_exception("order:1", "Pagado", "person:carl", "Pagó por transferencia",
                                              occurred_at=at(2, 10))
        self.assertEqual((asked["request"], asked["pending_for"]), ("exreq:order:1:1", "role:supervisor"))
        # The supervisor sees it among their pending actions through their role.
        self.assertEqual([item["entity_key"] for item in self.memory.pending("person:sam")], ["order:1"])
        with self.assertRaisesRegex(MemoryError, "Only role:owner, role:supervisor may resolve"):
            self.memory.resolve_exception("exreq:order:1:1", True, "person:carl", "yo mismo", occurred_at=at(2, 11))
        status = self.memory.resolve_exception("exreq:order:1:1", True, "person:sam", "Comprobante revisado",
                                               occurred_at=at(2, 11))
        self.assertEqual((status["step"], status["exception_requests"][0]["state"]), ("Pagado", "approved"))
        with self.assertRaisesRegex(MemoryError, "already resolved"):
            self.memory.resolve_exception("exreq:order:1:1", False, "person:sam", "otra vez", occurred_at=at(2, 12))
        # A denial returns the next action to whoever holds the current step.
        self.memory.start_process("order:2", "process:order", "person:carl", at(2, 9))
        self.memory.request_exception("order:2", "Entregado", "person:carl", "Urge", occurred_at=at(2, 10))
        denied = self.memory.resolve_exception("exreq:order:2:1", False, "person:sam", "No procede", occurred_at=at(2, 11))
        self.assertEqual((denied["step"], denied["pending_for"], denied["exception_requests"][0]["state"]),
                         ("Recibido", "clerk", "denied"))

    def test_an_older_version_that_lets_a_step_pass_is_noted(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "recibo"}, "performed_by": "person:olga",
                           "predicates": [{"id": "receipt", "value_type": "text"}]})
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        stricter = {**V1, "transitions": [*V1["transitions"][:3], {**V1["transitions"][3], "captures": [{"attribute": "receipt"}]}]}
        self.memory.define_process("process:order", "Pedido", stricter, "person:olga", effective_from=at(2, 9, ))
        for step, person, hour in (("Por pagar", "person:carl", 10), ("Pagado", "person:cata", 11), ("Entregado", "person:carl", 12)):
            status = self.memory.advance_process("order:1", step, person, at(2, hour))
        self.assertEqual(status["version"], 1)
        self.assertEqual(len(status["warnings"]), 1)
        self.assertIn("moved it under the rules of v1, but the version in force would have stopped it: v2:", status["warnings"][0])

    def test_a_migration_is_noted_on_the_step_not_worked(self) -> None:
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        self.memory.define_process("process:order", "Pedido", V2, "person:olga", effective_from=at(2, 10))
        status = self.memory.migrate_process("order:1", 2, "person:olga", "Plan: todo a v2", occurred_at=at(2, 11),
                                             pending_for="person:sam")
        self.assertEqual(len(status["steps"]), 1)
        self.assertEqual((status["steps"][0]["step"], status["pending_for"]), ("Recibido", "Sam"))
        self.assertIn("v1 → v2 (Olga", status["steps"][0]["migrations"][0])

    def test_steps_may_declare_how_long_they_may_wait(self) -> None:
        waiting = {**V1, "states": [{**V1["states"][0], "due_hours": 2}, *V1["states"][1:]]}
        self.memory.define_process("process:order", "Pedido", waiting, "person:olga", effective_from=at(2, 0))
        self.memory.start_process("order:1", "process:order", "person:carl", at(2, 9))
        self.memory.start_process("order:2", "process:order", "person:carl", at(2, 9))
        self.memory.advance_process("order:2", "Por pagar", "person:carl", at(2, 10))
        late = self.memory.overdue_instances("process:order", at="2026-03-02T12:00:00Z")
        self.assertEqual([(item["instance"], item["hours_waiting"]) for item in late], [("order:1", 3.0)])
        self.assertTrue(self.memory.process_status("order:1")["due"]["overdue"])
        with self.assertRaisesRegex(MemoryError, "positive number of hours"):
            self.memory.define_process("process:order", "Pedido",
                                       {**V1, "states": [{**V1["states"][0], "due_hours": -1}, *V1["states"][1:]]}, "person:olga")

    def test_change_rules_reserve_what_defines_every_case(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "tipos"}, "performed_by": "person:olga",
                           "predicates": [{"id": "fee", "value_type": "real"}],
                           "entities": [{"key": "kind:basic", "type": "order_kind", "name": "Básico"}]})
        rule = {"subject_type": "order_kind", "predicates": "*", "roles": ["role:owner"], "create": True}
        self.memory.define_change_rule("change:order-kinds", "Tipos de pedido", rule, "person:olga")
        change = {"source": {"type": "test_fixture", "content": "nueva cuota"},
                  "facts": [{"subject": "kind:basic", "predicate": "fee", "value": 10.0}]}
        with self.assertRaisesRegex(MemoryError, r"Sam \(roles: role:supervisor\) may not fact fee of kind:basic: "
                                                 r"change:order-kinds reserves it to role:owner. Nothing was written"):
            self.memory.write({**change, "performed_by": "person:sam"})
        with self.assertRaisesRegex(MemoryError, "may not create order_kind"):
            self.memory.write({"source": {"type": "test_fixture", "content": "tipo nuevo"}, "performed_by": "person:sam",
                               "entities": [{"key": "kind:rush", "type": "order_kind", "name": "Urgente"}]})
        self.memory.write({**change, "performed_by": "person:olga"})
        self.assertEqual(self.memory.list_rows(entity_type="order_kind", columns=["fact:fee"])["rows"], [{"fact:fee": 10.0}])
        # Ordinary cases stay open to the team: a supervisor adjusts one order.
        self.memory.write({"source": {"type": "test_fixture", "content": "ajuste"}, "performed_by": "person:sam",
                           "facts": [{"subject": "order:1", "predicate": "fee", "value": 5.0}]})
        with self.assertRaisesRegex(MemoryError, "Only role:owner may change change:order-kinds"):
            self.memory.define_change_rule("change:order-kinds", "Tipos", {**rule, "roles": ["role:supervisor"]}, "person:sam")
        self.assertEqual(self.memory.catalog()["change_rules"][0]["key"], "change:order-kinds")

    def test_definitions_are_validated(self) -> None:
        for broken, message in (({**V1, "states": []}, "non-empty"),
                                ({**V1, "transitions": [{"from": "Recibido", "to": "Nada", "roles": ["role:clerk"]}]},
                                 "declared state"),
                                ({**V1, "transitions": [{"from": "Recibido", "to": "Pagado", "roles": ["role:clerk"]}]},
                                 "start transition"),
                                ({**V1, "override_roles": ["role:ghost"]}, "not a known role")):
            with self.assertRaisesRegex(MemoryError, message):
                self.memory.define_process("process:order", "Pedido", broken, "person:olga")


if __name__ == "__main__":
    unittest.main()
