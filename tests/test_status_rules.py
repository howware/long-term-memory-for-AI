from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402

RULE = {
    "applies_to": {"event_type": "booking"},
    "follow_link": "concerns_booking",
    "states": [
        {"name": "Cancelada con cargo", "event_type": "booking_cancelled", "where": {"fee": {">": 0}}},
        {"name": "Cancelada sin cargo", "event_type": "booking_cancelled"},
        {"name": "Salida anticipada", "event_type": "checked_out", "where": {"nights": {"<": {"subject_attr": "nights"}}}},
        {"name": "Completada", "event_type": "checked_out"},
        {"name": "En casa", "event_type": "checked_in"},
    ],
    "otherwise": "Confirmada",
}


class StatusRuleTests(unittest.TestCase):
    """Declarative status rules derive current state from follow-up events."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        bookings = [(f"B{n}", "2026-01-01T10:00:00Z") for n in range(1, 7)]
        follow = [
            ("C1", "booking_cancelled", "B1", "2026-01-02T10:00:00Z", {"fee": 100.0}),
            ("C2", "booking_cancelled", "B2", "2026-01-02T10:00:00Z", {"fee": 0.0}),
            ("I3", "checked_in", "B3", "2026-01-05T15:00:00Z", {}),
            ("O3", "checked_out", "B3", "2026-01-07T11:00:00Z", {"nights": 2}),
            ("I4", "checked_in", "B4", "2026-01-05T15:00:00Z", {}),
            ("O4", "checked_out", "B4", "2026-01-08T11:00:00Z", {"nights": 3}),
            ("I5", "checked_in", "B5", "2026-01-05T15:00:00Z", {}),
        ]
        self.memory.write({
            "source": {"type": "test_fixture", "content": "reservaciones de prueba"},
            "performed_by": "person:ana",
            "predicates": [{"id": "concerns_booking", "value_type": "any"},
                           {"id": "nights", "value_type": "integer"}, {"id": "fee", "value_type": "real"}],
            "events": [{"key": key, "type": "booking", "occurred_at": at, "attributes": {"nights": 3}} for key, at in bookings]
            + [{"key": key, "type": kind, "occurred_at": at, "attributes": attributes,
                "links": [{"predicate": "concerns_booking", "event": target}]}
               for key, kind, target, at, attributes in follow],
        })
        self.memory.define_status_rule("status:booking", "Estado de la reservación", RULE)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def statuses(self, **kwargs) -> dict[str, int]:
        result = self.memory.aggregate("booking", by=["status:booking"], **kwargs)
        return {row["status:booking"]: row["value"] for row in result["groups"]}

    def test_first_matching_state_wins(self) -> None:
        self.assertEqual(self.statuses(), {"Cancelada con cargo": 1, "Cancelada sin cargo": 1, "Salida anticipada": 1,
                                           "Completada": 1, "En casa": 1, "Confirmada": 1})
        self.assertEqual(self.memory.event("B3")["status"], {"status:booking": "Salida anticipada"})
        confirmed = self.memory.aggregate("booking", where=["status:booking=Confirmada"])
        spaced = self.memory.aggregate("booking", where=["status:booking = Confirmada"])
        self.assertEqual(spaced["total_rows"], confirmed["total_rows"])
        self.assertEqual(confirmed["total_rows"], 1)

    def test_voided_follow_up_no_longer_counts(self) -> None:
        self.memory.void("O4", "Salida registrada por error.")
        self.assertEqual(self.memory.event("B4")["status"], {"status:booking": "En casa"})

    def test_status_as_of_a_past_instant(self) -> None:
        past = self.statuses(as_of="2026-01-06T00:00:00Z")
        self.assertEqual(past, {"Cancelada con cargo": 1, "Cancelada sin cargo": 1, "En casa": 3, "Confirmada": 1})
        # Before any booking existed there is nothing to classify.
        self.assertEqual(self.statuses(as_of="2025-12-31T00:00:00Z"), {})

    def test_rule_versions_are_kept_and_invalid_rules_rejected(self) -> None:
        simpler = {**RULE, "states": RULE["states"][1:2], "otherwise": "Vigente"}
        self.memory.define_status_rule("status:booking", "Estado de la reservación", simpler)
        self.assertEqual(self.statuses(), {"Cancelada sin cargo": 2, "Vigente": 4})
        history = [fact for fact in self.memory.describe("status:booking")["facts"] if fact["predicate_id"] == "rule_definition"]
        self.assertEqual([fact["assertion_state"] for fact in history], ["superseded", "asserted"])
        for broken, message in (
            ({**RULE, "states": []}, "non-empty"),
            ({**RULE, "applies_to": {"table": "x"}}, "applies_to"),
            ({**RULE, "states": [{"name": "X", "event_type": "e", "where": {"fee": {"~": 1}}}]}, "must be one of"),
            ({**RULE, "extra": 1}, "Unknown keys"),
        ):
            with self.assertRaisesRegex(MemoryError, message):
                self.memory.define_status_rule("status:broken", "Rota", broken)
        with self.assertRaisesRegex(MemoryError, "applies to event_type booking"):
            self.memory.aggregate("checked_in", by=["status:booking"])


if __name__ == "__main__":
    unittest.main()
