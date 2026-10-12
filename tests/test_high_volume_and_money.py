from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError, render_brief, utc_now  # noqa: E402


class HighVolumeEventTypeTests(unittest.TestCase):
    """Imported movements, readings or transitions arrive by the hundred: a
    person may declare their type high-volume, so the brief's recent list
    keeps showing what matters and summarizes them in one line per type."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        self.memory.remember("decision", "Import the bank statements every week", performed_by="person:ana")
        self.memory.write({
            "source": {"type": "test_fixture", "content": "import"}, "performed_by": "person:ana",
            "predicates": [{"id": "amount", "value_type": "integer", "unit": "cent"}],
            "events": [{"key": f"MOV-{index:03d}", "type": "movement", "occurred_at": "2026-01-05T10:00:00Z",
                        "summary": f"Movement {index}", "attributes": {"amount": -100 * index}}
                       for index in range(1, 31)],
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def recent_kinds(self) -> list[str]:
        return [item["kind"] for item in self.memory.brief()["recent"]]

    def test_high_volume_types_leave_recent_and_get_one_line(self) -> None:
        self.assertEqual(set(self.recent_kinds()), {"movement"})
        result = self.memory.define_event_type("movement", True, "person:ana", reason="Imported by the hundred")
        self.assertEqual((result["high_volume"], result["changed"]), (True, True))
        brief = self.memory.brief()
        kinds = [item["kind"] for item in brief["recent"]]
        self.assertNotIn("movement", kinds)
        self.assertIn("decision", kinds)
        self.assertEqual([(item["type"], item["recorded_last_7_days"]) for item in brief["high_volume_activity"]],
                         [("movement", 30)])
        text = render_brief(brief)
        self.assertIn("High-volume activity", text)
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("  · movement:")), 1)
        # They still count everywhere else.
        self.assertEqual(self.memory.aggregate("movement")["total_rows"], 30)
        self.assertEqual(self.memory.list_rows("movement", limit=100)["total"], 30)
        entry = next(item for item in self.memory.catalog()["event_types"] if item["event_type"] == "movement")
        self.assertTrue(entry["high_volume"])
        # Declaring the same treatment again changes nothing.
        self.assertFalse(self.memory.define_event_type("movement", True, "person:ana")["changed"])

    def test_the_declaration_is_versioned_reversible_and_a_persons_decision(self) -> None:
        self.memory.define_event_type("movement", True, "person:ana")
        between = utc_now()
        time.sleep(1.1)
        self.memory.define_event_type("movement", False, "person:ana", reason="Few movements now")
        self.assertIn("movement", self.recent_kinds())
        self.assertEqual(self.memory.brief()["high_volume_activity"], [])
        self.assertTrue(self.memory.high_volume_types(as_of=between)["movement"])
        self.assertFalse(self.memory.high_volume_types()["movement"])
        history = [fact for fact in self.memory.describe("event_type:movement")["facts"]
                   if fact["predicate_id"] == "high_volume"]
        self.assertEqual([("valid_to" in fact, fact["value_boolean"]) for fact in history], [(True, 1), (False, 0)])
        declared = self.memory.list_rows("event_type_declared", columns=["participant:performed_by"])["rows"]
        self.assertEqual(len(declared), 2)
        self.memory.write({"source": {"type": "test_fixture", "content": "system"}, "performed_by": "person:ana",
                           "entities": [{"key": "system:bank", "type": "system", "name": "Bank feed"}]})
        with self.assertRaisesRegex(MemoryError, "decision of a person"):
            self.memory.define_event_type("movement", True, "system:bank")

    def test_transitions_and_backups_are_high_volume_by_default_and_can_be_listed_again(self) -> None:
        self.memory.backup()
        self.assertNotIn("memory_backed_up", self.recent_kinds())
        self.assertEqual([item["type"] for item in self.memory.brief()["high_volume_activity"]], ["memory_backed_up"])
        self.memory.define_event_type("memory_backed_up", False, "person:ana")
        self.assertIn("memory_backed_up", self.recent_kinds())


class ExactMoneyTests(unittest.TestCase):
    """Money is stored as integers in its smallest unit (cents): sums are exact,
    with outflows (negative) and inflows (positive), per line and per month."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        # 0.1 + 0.2 style amounts: as floats these drift; as cents they cannot.
        self.amounts = {"2026-01": [-1010, -2020, 3030, -1, 250000], "2026-02": [-99999, 10, -10, 33333]}
        events = []
        for month, values in self.amounts.items():
            for index, cents in enumerate(values):
                events.append({"key": f"{month}-{index}", "type": "movement", "occurred_at": f"{month}-1{index}T12:00:00Z",
                               "attributes": {"amount": {"value": cents, "unit": "MXN_cent"}}})
        events.append({"key": "STMT-1", "type": "statement", "occurred_at": "2026-02-28T12:00:00Z",
                       "links": [{"predicate": "contains", "entity": "account:a", "attributes": {"amount": -1010}},
                                 {"predicate": "contains", "entity": "account:a", "attributes": {"amount": 2020}},
                                 {"predicate": "contains", "entity": "account:b", "attributes": {"amount": -3}}]})
        self.memory.write({
            "source": {"type": "test_fixture", "content": "movements"}, "performed_by": "person:ana",
            "predicates": [{"id": "amount", "value_type": "integer", "unit": "MXN_cent"},
                           {"id": "contains", "value_type": "entity", "cardinality": "many"}],
            "entities": [{"key": "account:a", "type": "account", "name": "A"}, {"key": "account:b", "type": "account", "name": "B"}],
            "events": events,
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def value(self, measure: str, **kwargs) -> object:
        return self.memory.aggregate("movement", measure=measure, **kwargs)["groups"][0]["value"]

    def test_sum_min_max_are_exact_integers_and_carry_the_unit(self) -> None:
        every = [cents for values in self.amounts.values() for cents in values]
        result = self.memory.aggregate("movement", measure="sum:amount")
        self.assertEqual((result["groups"][0]["value"], result["unit"]), (sum(every), "MXN_cent"))
        self.assertIsInstance(result["groups"][0]["value"], int)
        self.assertEqual(self.value("min:amount"), min(every))
        self.assertEqual(self.value("max:amount"), max(every))
        # avg is a float: round it when presenting, never when storing.
        self.assertAlmostEqual(self.value("avg:amount"), sum(every) / len(every))

    def test_exact_by_month_and_per_line(self) -> None:
        groups = self.memory.aggregate("movement", measure="sum:amount", by=["period:month"], order="group")["groups"]
        self.assertEqual({group["period:month"]: group["value"] for group in groups},
                         {month: sum(values) for month, values in self.amounts.items()})
        lines = self.memory.aggregate("statement", measure="sum:amount", line="contains", by=["line"], order="group")
        self.assertEqual({group["line"]: group["value"] for group in lines["groups"]}, {"A": 1010, "B": -3})
        outflows = self.memory.aggregate("movement", measure="sum:amount", where=["attr:amount < 0"])
        self.assertEqual(outflows["groups"][0]["value"], sum(c for v in self.amounts.values() for c in v if c < 0))

    def test_a_real_amount_is_rejected_by_an_integer_predicate(self) -> None:
        with self.assertRaisesRegex(MemoryError, "expects an integer"):
            self.memory.write({"source": {"type": "test_fixture", "content": "float"}, "performed_by": "person:ana",
                               "events": [{"type": "movement", "occurred_at": "2026-03-01T00:00:00Z",
                                           "attributes": {"amount": 10.5}}]})


class EventKeyIdempotencyTests(unittest.TestCase):
    """What an importer meets when it sends an event key again."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        self.document = {"source": {"type": "test_fixture", "content": "import 1"}, "performed_by": "person:ana",
                         "predicates": [{"id": "amount", "value_type": "integer", "unit": "cent"}],
                         "events": [{"key": "MOV-1", "type": "movement", "occurred_at": "2026-01-01T00:00:00Z",
                                     "attributes": {"amount": -500}}]}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_same_key_is_kept_as_first_recorded(self) -> None:
        self.memory.write(self.document)
        again = self.memory.write(self.document)
        self.assertEqual((again["events"]["created"], again["events"]["existing"]), ([], ["MOV-1"]))
        changed = self.memory.write({**self.document, "source": {"type": "test_fixture", "content": "import 2"},
                                     "events": [{**self.document["events"][0], "attributes": {"amount": -700}}]})
        self.assertEqual((changed["events"]["created"], changed["events"]["existing"]), ([], ["MOV-1"]))
        self.assertEqual(self.memory.event("MOV-1")["attributes"]["amount"]["value"], -500)
        self.assertEqual(self.memory.aggregate("movement")["total_rows"], 1)

    def test_a_second_importer_adds_its_links_once(self) -> None:
        self.memory.write(self.document)
        reading = {"source": {"type": "test_fixture", "content": "import by another howware"},
                   "performed_by": "person:ana",
                   "predicates": [{"id": "covers", "value_type": "entity"}],
                   "entities": [{"key": "period:2026-01", "type": "period", "name": "January"}],
                   "events": [{**self.document["events"][0], "attributes": {"amount": -700},
                               "links": [{"predicate": "covers", "entity": "period:2026-01"}]}]}
        first = self.memory.write(reading)
        again = self.memory.write(reading)
        self.assertEqual((first["events"]["existing"], first["links"]), (["MOV-1"], 1))
        self.assertEqual(again["links"], 0)
        event = self.memory.event("MOV-1")
        self.assertEqual(event["attributes"]["amount"]["value"], -500)
        self.assertEqual([link["target"] for link in event["links"] if link["predicate"] == "covers"], ["January"])


if __name__ == "__main__":
    unittest.main()
