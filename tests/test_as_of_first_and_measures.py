from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


def later(days: int) -> str:
    """An instant days from now: future-dated facts stay in the future."""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")


class AsOfTests(unittest.TestCase):
    """--as-of reads facts and entity rows as they were at that instant; with
    no --as-of, "in force" means now: a fact that starts later is not yet."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        self.write(predicates=[{"id": "monthly_budget", "value_type": "integer", "unit": "cent", "cardinality": "one"},
                               {"id": "opened_on", "value_type": "datetime", "cardinality": "one"},
                               {"id": "allocates", "value_type": "entity"}],
                   entities=[{"key": key, "type": "category", "name": key.split(":")[1]}
                             for key in ("category:y", "category:z", "category:r", "category:old", "category:new")],
                   facts=[*({"subject": key, "predicate": "opened_on", "value": "2026-01-01T00:00:00Z",
                             "valid_from": "2026-01-01T00:00:00Z"}
                            for key in ("category:y", "category:z", "category:r", "category:old")),
                          {"subject": "category:new", "predicate": "opened_on", "value": "2026-10-01T00:00:00Z",
                           "valid_from": "2026-10-01T00:00:00Z"},
                          {"subject": "category:y", "predicate": "monthly_budget", "value": 400000,
                           "valid_from": "2026-09-01T00:00:00Z"},
                          {"subject": "category:z", "predicate": "monthly_budget", "value": 300000,
                           "valid_from": "2026-07-01T00:00:00Z"},
                          {"subject": "category:r", "predicate": "monthly_budget", "value": 500000,
                           "valid_from": "2026-07-01T00:00:00Z"}])
        self.write(facts=[{"subject": "category:z", "predicate": "monthly_budget", "value": 400000,
                           "valid_from": "2026-09-01T00:00:00Z"}],
                   retract=[{"subject": "category:r", "predicate": "monthly_budget", "reason": "typed by mistake"}],
                   retire=[{"entity": "category:old", "at": "2026-08-15T00:00:00Z", "reason": "merged"}])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, **sections) -> dict:
        return self.memory.write({"source": {"type": "test_fixture", "content": repr(sections)},
                                  "performed_by": "person:ana", **sections})

    def budgets(self, **kwargs) -> dict:
        rows = self.memory.list_rows(entity_type="category", columns=["key", "fact:monthly_budget"], **kwargs)["rows"]
        return {row["key"]: row["fact:monthly_budget"] for row in rows}

    def test_reproduction_from_the_report(self) -> None:
        rows = self.memory.list_rows(entity_type="category", where=["has:fact:monthly_budget"], as_of="2026-08-01",
                                     columns=["key", "fact:monthly_budget"])["rows"]
        self.assertEqual([row["key"] for row in rows], ["category:z"])
        self.assertEqual(rows[0]["fact:monthly_budget"], 300000)

    def test_facts_and_rows_as_of_an_instant(self) -> None:
        # z replaced, r retracted (never true), y not yet, old retired on 08-15, new not existing yet.
        self.assertEqual(self.budgets(as_of="2026-08-01"),
                         {"category:old": None, "category:r": None, "category:y": None, "category:z": 300000})
        self.assertEqual(self.budgets(as_of="2026-09-02"), {"category:r": None, "category:y": 400000, "category:z": 400000})
        self.assertEqual(self.budgets(), {"category:new": None, "category:r": None, "category:y": 400000,
                                          "category:z": 400000})
        # Conditions (index path and per-row path), groups and since follow the instant too.
        where = self.memory.list_rows(entity_type="category", where=["fact:monthly_budget = 300000"], as_of="2026-08-01",
                                      columns=["key"])["rows"]
        self.assertEqual(where, [{"key": "category:z"}])
        self.assertEqual(self.memory.list_rows(entity_type="category", where=["no:fact:monthly_budget"],
                                               as_of="2026-08-01", columns=["key"])["total"], 3)
        since = self.memory.list_rows(entity_type="category", where=["key = category:z"], as_of="2026-08-01",
                                      columns=["fact:monthly_budget/since"])["rows"][0]
        self.assertTrue(since["fact:monthly_budget/since"].startswith("2026-07-01"))
        groups = self.memory.aggregate(None, entity_type="category", by=["fact:monthly_budget"], as_of="2026-08-01",
                                       order="group")["groups"]
        self.assertEqual({group["fact:monthly_budget"]: group["rows"] for group in groups}, {None: 3, 300000: 1})
        total = self.memory.aggregate(None, entity_type="category", measure="sum:monthly_budget", as_of="2026-08-01")
        self.assertEqual(total["groups"][0]["value"], 300000)

    def test_link_in_counts_only_events_before_the_instant(self) -> None:
        self.write(events=[{"type": "assignment", "occurred_at": day, "links": [{"predicate": "allocates", "entity": "category:z"}]}
                           for day in ("2026-07-10T00:00:00Z", "2026-09-10T00:00:00Z")])
        def count(as_of=None):
            row = self.memory.list_rows(entity_type="category", where=["key = category:z"], as_of=as_of,
                                        columns=["link-in:allocates/key"])["rows"][0]
            value = row["link-in:allocates/key"]
            return len(value) if isinstance(value, list) else int(value is not None)
        self.assertEqual((count("2026-08-01"), count()), (1, 2))

    def test_a_future_value_waits_for_its_start_and_keeps_the_current_one(self) -> None:
        start = later(40)
        self.write(facts=[{"subject": "category:y", "predicate": "monthly_budget", "value": 450000, "valid_from": start}])
        self.assertEqual(self.budgets()["category:y"], 400000)
        self.assertEqual(self.budgets(as_of=later(41))["category:y"], 450000)
        facts = [fact for fact in self.memory.describe("category:y")["facts"] if fact["predicate_id"] == "monthly_budget"]
        self.assertEqual([(fact["value_integer"], fact["in_force"], fact.get("valid_to")) for fact in facts],
                         [(400000, True, start), (450000, False, None)])
        self.assertEqual(self.memory.list_rows(entity_type="category", where=["fact:monthly_budget = 450000"],
                                               columns=["key"])["total"], 0)
        # A pending action that starts later is not pending yet.
        self.write(facts=[{"subject": "category:z", "predicate": "pending_action_for", "value": "person:ana",
                           "valid_from": later(3)}])
        self.assertEqual([item for item in self.memory.pending() if item.get("entity_key") == "category:z"], [])


class FirstAndMeasurePathTests(unittest.TestCase):
    """first(A | B): the first path with a value for the row; measures may
    follow a path to a number."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        self.memory.write({
            "source": {"type": "test_fixture", "content": "setup"}, "performed_by": "person:ana",
            "predicates": [{"id": "amount", "value_type": "integer", "unit": "MXN_cent"},
                           {"id": "memo", "value_type": "text"},
                           {"id": "classified_by", "value_type": "entity", "cardinality": "one"},
                           {"id": "categorized_as", "value_type": "entity", "cardinality": "one"},
                           {"id": "recategorizes", "value_type": "entity", "cardinality": "one"},
                           {"id": "to_category", "value_type": "entity", "cardinality": "one"},
                           {"id": "covers", "value_type": "entity", "cardinality": "many"}],
            "entities": [{"key": "category:home", "type": "category", "name": "Home"},
                         {"key": "category:food", "type": "category", "name": "Food"},
                         {"key": "category:fun", "type": "category", "name": "Fun"},
                         {"key": "category:basics", "type": "category", "name": "Basics"},
                         {"key": "rule:market", "type": "rule", "name": "Market rule"}],
            "facts": [{"subject": "rule:market", "predicate": "categorized_as", "value": "category:food",
                       "valid_from": "2026-01-01T00:00:00Z"},
                      {"subject": "category:food", "predicate": "part_of", "value": "category:basics",
                       "valid_from": "2026-01-01T00:00:00Z"},
                      {"subject": "category:home", "predicate": "part_of", "value": "category:basics",
                       "valid_from": "2026-01-01T00:00:00Z"}],
            "events": [
                *({"key": key, "type": "movement", "occurred_at": day, "attributes": {"amount": amount, "memo": key},
                   "links": [{"predicate": "classified_by", "entity": "rule:market"}] if ruled else []}
                  for key, day, amount, ruled in (("M1", "2026-01-05T12:00:00Z", -1000, True),
                                                  ("M2", "2026-01-06T12:00:00Z", -2000, False),
                                                  ("M3", "2026-02-07T12:00:00Z", -3000, True),
                                                  ("M4", "2026-02-08T12:00:00Z", -4000, False),
                                                  ("M5", "2026-02-09T12:00:00Z", -5000, True),
                                                  ("M6", "2026-02-10T12:00:00Z", -6000, False))),
                {"key": "X2", "type": "recategorization", "occurred_at": "2026-03-01T00:00:00Z",
                 "links": [{"predicate": "recategorizes", "event": "M2"}, {"predicate": "to_category", "entity": "category:home"}]},
                {"key": "X3", "type": "recategorization", "occurred_at": "2026-03-01T00:00:00Z",
                 "links": [{"predicate": "recategorizes", "event": "M3"}, {"predicate": "to_category", "entity": "category:home"}]},
                {"key": "X5", "type": "recategorization", "occurred_at": "2026-03-01T00:00:00Z",
                 "links": [{"predicate": "recategorizes", "event": "M5"}, {"predicate": "to_category", "entity": "category:fun"}]},
                {"key": "X6a", "type": "recategorization", "occurred_at": "2026-03-01T00:00:00Z",
                 "links": [{"predicate": "recategorizes", "event": "M6"}, {"predicate": "to_category", "entity": "category:fun"}]},
                {"key": "X6b", "type": "recategorization", "occurred_at": "2026-03-05T00:00:00Z",
                 "links": [{"predicate": "recategorizes", "event": "M6"}, {"predicate": "to_category", "entity": "category:home"}]},
                {"key": "C1", "type": "settlement", "occurred_at": "2026-04-01T00:00:00Z",
                 "links": [{"predicate": "covers", "event": "M1"}, {"predicate": "covers", "event": "M2"}]},
            ],
        })
        self.memory.void("X5", "Entered by mistake", performed_by="person:ana")
        self.category = "first(link-in:recategorizes/link:to_category | link:classified_by/fact:categorized_as)"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_in_columns_and_groups(self) -> None:
        rows = self.memory.list_rows("movement", columns=["key", self.category], limit=10)["rows"]
        # M1 rule only, M2 exception only, M3 both (exception wins), M4 neither,
        # M5 rule with a voided exception, M6 two exceptions (the latest wins).
        self.assertEqual({row["key"]: row[self.category] for row in rows},
                         {"M1": "Food", "M2": "Home", "M3": "Home", "M4": None, "M5": "Food", "M6": "Home"})
        groups = self.memory.aggregate("movement", measure="sum:amount", by=[self.category], order="group")["groups"]
        self.assertEqual({group[self.category]: group["value"] for group in groups},
                         {None: -4000, "Food": -6000, "Home": -11000})
        rows = self.memory.list_rows("movement", where=[f"{self.category} = category:home"], columns=["key"])["rows"]
        self.assertEqual([row["key"] for row in rows], ["M2", "M3", "M6"])
        ordered = self.memory.list_rows("movement", columns=["key"], order_by=[self.category, "key"])["rows"]
        self.assertEqual(ordered[0]["key"], "M4")

    def test_first_can_continue_and_works_in_recipes(self) -> None:
        parent = f"{self.category}/fact:part_of"
        rows = self.memory.list_rows("movement", columns=["key", parent], limit=10)["rows"]
        self.assertEqual({row["key"]: row[parent] for row in rows},
                         {"M1": "Basics", "M2": "Basics", "M3": "Basics", "M4": None, "M5": "Basics", "M6": "Basics"})
        self.memory.define_recipe("recipe:by-category", "By category", {
            "kind": "aggregate", "query": {"event_type": "movement", "measure": "sum:amount", "by": [self.category]}})
        groups = self.memory.run_recipe("recipe:by-category")["result"]["groups"]
        self.assertEqual({group[self.category]: group["value"] for group in groups}["Home"], -11000)

    def test_first_rejects_mixed_kinds_and_several_non_event_values(self) -> None:
        with self.assertRaisesRegex(MemoryError, "same kind of value"):
            self.memory.list_rows("movement", columns=["first(attr:amount | link:classified_by)", "key"])
        # Like --by, a path that may give several values is checked row by row:
        # C1 covers two movements, C2 only one.
        self.memory.write({"source": {"type": "test_fixture", "content": "C2"}, "performed_by": "person:ana",
                           "events": [{"key": "C2", "type": "settlement", "occurred_at": "2026-04-02T00:00:00Z",
                                       "links": [{"predicate": "covers", "event": "M3"}]}]})
        path = "first(link:covers | participant:performed_by)"
        with self.assertRaisesRegex(MemoryError, "reaches several values for some rows"):
            self.memory.list_rows("settlement", columns=[path])
        with self.assertRaisesRegex(MemoryError, "reaches several values for some rows"):
            self.memory.aggregate("settlement", by=[path])
        rows = self.memory.list_rows("settlement", where=["key = C2"], columns=["key", f"{path}/key"])["rows"]
        self.assertEqual(rows, [{"key": "C2", f"{path}/key": "M3"}])
        groups = self.memory.aggregate("settlement", by=[f"{path}/key"], where=["key = C2"])["groups"]
        self.assertEqual([(group[f"{path}/key"], group["value"]) for group in groups], [("M3", 1)])
        # In a condition there are no rows to check first: rejected while compiling.
        with self.assertRaisesRegex(MemoryError, "in a condition"):
            self.memory.list_rows("settlement", where=[f"{path} = M3"])

    def test_measure_through_a_path(self) -> None:
        result = self.memory.aggregate("recategorization", measure="sum:link:recategorizes/attr:amount",
                                       by=["period:month"], order="group")
        # X5 is voided: X2 + X3 + X6a + X6b.
        self.assertEqual((result["groups"][0]["value"], result["unit"]), (-17000, "MXN_cent"))
        with self.assertRaisesRegex(MemoryError, "several values for some rows"):
            self.memory.aggregate("settlement", measure="sum:link:covers/attr:amount")
        with self.assertRaisesRegex(MemoryError, "holds text values"):
            self.memory.aggregate("movement", measure="sum:memo")
        with self.assertRaisesRegex(MemoryError, "Unknown predicate"):
            self.memory.aggregate("movement", measure="sum:amonut")
        with self.assertRaisesRegex(MemoryError, "Unknown path step"):
            self.memory.aggregate("recategorization", measure="sum:lnk:recategorizes/attr:amount")


if __name__ == "__main__":
    unittest.main()


class MinorFixesTests(unittest.TestCase):
    """Far --as-of bounds, and the reads in capture use the one "in force" definition."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite")  # system zone: the path that broke
        self.memory.init("Ana", "person:ana")
        self.memory.write({"source": {"type": "test_fixture", "content": "setup"}, "performed_by": "person:ana",
                           "predicates": [{"id": "level", "value_type": "integer", "cardinality": "one"},
                                          {"id": "color", "value_type": "text", "cardinality": "one"}],
                           "entities": [{"key": "tank:a", "type": "tank", "name": "A"}],
                           "facts": [{"subject": "tank:a", "predicate": "level", "value": 1,
                                      "valid_from": "2026-01-01T00:00:00Z"},
                                     {"subject": "tank:a", "predicate": "level", "value": 2, "valid_from": later(30)}]})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_far_as_of_reads_the_latest_value(self) -> None:
        for bound in ("2100-01-01", "9999-12-31"):
            rows = self.memory.list_rows(entity_type="tank", as_of=bound, columns=["fact:level"])["rows"]
            self.assertEqual(rows, [{"fact:level": 2}], bound)
        self.assertEqual(self.memory.list_rows(entity_type="tank", columns=["fact:level"])["rows"], [{"fact:level": 1}])

    def test_future_vocabulary_does_not_apply_yet(self) -> None:
        self.memory.define_values("color", ["red"], "person:ana")
        self.memory.write({"source": {"type": "test_fixture", "content": "future vocabulary"},
                           "performed_by": "person:ana",
                           "close": [{"subject": "predicate:color", "predicate": "allowed_value", "value": "red",
                                      "at": later(30)}],
                           "facts": [{"subject": "predicate:color", "predicate": "allowed_value", "value": "blue",
                                      "valid_from": later(30)}]})
        self.memory._vocabulary_cache = {}
        self.memory.write({"source": {"type": "test_fixture", "content": "red"}, "performed_by": "person:ana",
                           "facts": [{"subject": "tank:a", "predicate": "color", "value": "red"}]})
        self.memory._vocabulary_cache = {}
        with self.assertRaises(MemoryError):
            self.memory.write({"source": {"type": "test_fixture", "content": "blue"}, "performed_by": "person:ana",
                               "facts": [{"subject": "tank:a", "predicate": "color", "value": "blue",
                                          "valid_from": "now"}]})

    def test_future_high_volume_declaration_waits(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "later"}, "performed_by": "person:ana",
                           "entities": [{"key": "event_type:reading", "type": "event_type_profile",
                                         "name": "Event type reading"}],
                           "facts": [{"subject": "event_type:reading", "predicate": "high_volume", "value": True,
                                      "valid_from": later(30)}]})
        self.assertNotIn("reading", self.memory.high_volume_types())
        self.assertTrue(self.memory.high_volume_types(as_of=later(31))["reading"])
