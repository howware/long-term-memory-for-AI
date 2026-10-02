from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


def at(day: int, hour: int = 12) -> str:
    return f"2026-05-{day:02d}T{hour:02d}:00:00Z"


class RenameRetireRequestTests(unittest.TestCase):
    """Findings of the writer run: an entity can be renamed with history, a
    node can be taken out of use with all its facts, a request recorded as an
    event waits on someone until it is resolved, and paths accept '/'."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Mara", "person:mara")
        nodes = [("doc:book", "book", "Book", None, None), ("doc:c1", "chapter", "1.1 Estimates", "doc:book", 1),
                 ("doc:c2", "chapter", "Chapter 2", "doc:book", 2), ("doc:p1", "paragraph", "P1", "doc:c1", 1),
                 ("doc:p2", "paragraph", "P2", "doc:c1", 2), ("doc:p3", "paragraph", "P3", "doc:c2", 1)]
        self.memory.write({
            "source": {"type": "test_fixture", "content": "draft"}, "performed_by": "person:mara",
            "predicates": [{"id": "body", "value_type": "text", "cardinality": "one"},
                           {"id": "cites", "value_type": "entity", "cardinality": "many"}],
            "entities": [{"key": key, "type": kind, "name": name} for key, kind, name, _, _ in nodes]
            + [{"key": "person:leo", "type": "person", "name": "Leo"}, {"key": "role:editor", "type": "role", "name": "Editor"}],
            "facts": [*({"subject": key, "predicate": "part_of", "value": parent, "valid_from": at(1)}
                        for key, _, _, parent, _ in nodes if parent),
                      *({"subject": key, "predicate": "position", "value": float(order), "valid_from": at(1)}
                        for key, _, _, parent, order in nodes if parent),
                      {"subject": "doc:p1", "predicate": "body", "value": "Estimates are ranges.", "valid_from": at(1)},
                      {"subject": "doc:p3", "predicate": "cites", "value": "doc:p2", "valid_from": at(1)}],
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, **sections) -> dict:
        return self.memory.write({"source": {"type": "test_fixture", "content": repr(sections)},
                                  "performed_by": "person:mara", **sections})

    def test_a_rename_is_a_name_fact_and_every_output_follows_it(self) -> None:
        report = self.write(entities=[{"key": "doc:c1", "type": "chapter", "name": "1.1 Estimating Ranges",
                                       "rename": True, "valid_from": at(3)}])
        self.assertEqual(report["warnings"], [])
        tree = self.memory.outline("doc:book")["root"]
        self.assertEqual([child["name"] for child in tree["children"]], ["1.1 Estimating Ranges", "Chapter 2"])
        # As it was before the rename, it reads with the old name.
        self.assertEqual(self.memory.outline("doc:book", as_of=at(2))["root"]["children"][0]["name"], "1.1 Estimates")
        entity = self.memory.describe("doc:c1")["entity"]
        self.assertEqual((entity["name"], entity["created_as"]), ("1.1 Estimating Ranges", "1.1 Estimates"))
        # Both names find it; paths and lists read the name in force.
        self.assertEqual(self.memory.search("Estimating Ranges")[0]["external_key"], "doc:c1")
        self.assertEqual(self.memory.search("1.1 Estimates")[0]["external_key"], "doc:c1")
        rows = self.memory.list_rows(entity_type="paragraph", where=["key = doc:p1"], columns=["fact:part_of/name"])["rows"]
        self.assertEqual(rows, [{"fact:part_of/name": "1.1 Estimating Ranges"}])
        # A name sent without rename stays an alias, and the warning says how to rename.
        warning = self.write(entities=[{"key": "doc:c1", "type": "chapter", "name": "Estimates"}])["warnings"][0]
        self.assertIn("named '1.1 Estimating Ranges'", warning)
        self.assertIn('"rename": true', warning)

    def test_retire_closes_every_fact_and_keeps_the_history(self) -> None:
        with self.assertRaisesRegex(MemoryError, r"doc:c1 still has 2 part\(s\) \(doc:p1, doc:p2\)"):
            self.write(retire=[{"entity": "doc:c1", "at": at(4), "reason": "Merged into chapter 2"}])
        report = self.write(retire=[{"entity": "doc:c1", "at": at(4), "reason": "Merged into chapter 2", "with_parts": True}])
        self.assertEqual(report["retired"], ["doc:c1", "doc:p1", "doc:p2"])
        # part_of, position and body of the three nodes end at the same instant.
        self.assertEqual(report["facts"]["closed"], 7)
        self.assertTrue(any("1 current cites fact(s) of other entities still point to doc:c1" in warning
                            for warning in report["warnings"]))
        self.assertEqual([child["key"] for child in self.memory.outline("doc:book")["root"]["children"]], ["doc:c2"])
        self.assertEqual([child["key"] for child in self.memory.outline("doc:book", as_of=at(3))["root"]["children"]],
                         ["doc:c1", "doc:c2"])
        described = self.memory.describe("doc:p1")
        self.assertTrue(described["entity"]["retired"])
        self.assertFalse([fact for fact in described["facts"] if "valid_to" not in fact])
        event = self.memory.timeline("doc:c1")[-1]
        self.assertEqual((event["event_type"], event["performed_by_name"]), ("entity_retired", "Mara"))
        self.assertIn("already retired", self.write(retire=[{"entity": "doc:c1", "at": at(5), "reason": "again"}])["warnings"][0])

    def test_a_request_waits_on_its_actor_until_an_event_resolves_it(self) -> None:
        self.write(events=[
            {"key": "note:1", "type": "comment", "occurred_at": at(2), "summary": "Confirm figure 1.1 matches the text",
             "pending_for": "person:leo", "attributes": {"requested_action": "Confirm the figure"},
             "participants": [{"entity": "doc:p1", "role": "subject"}]},
            {"key": "note:2", "type": "comment", "occurred_at": at(2, 13), "summary": "Shorten chapter 2",
             "pending_for": "role:editor"}])
        waiting = self.memory.pending("person:leo")
        self.assertEqual([(item["event_key"], item["requested_action"], item["about"]) for item in waiting],
                         [("note:1", "Confirm the figure", "doc:p1")])
        self.assertEqual(self.memory.brief()["pending_actions"]["open"], 2)
        self.memory.write({"source": {"type": "test_fixture", "content": "done"}, "performed_by": "person:leo",
                           "events": [{"key": "reply:1", "type": "comment_addressed", "occurred_at": at(3),
                                       "links": [{"predicate": "resolves", "event": "note:1"}]}]})
        self.assertEqual(self.memory.pending("person:leo"), [])
        self.assertEqual([item["event_key"] for item in self.memory.pending()], ["note:2"])
        with self.assertRaisesRegex(MemoryError, "pending_for must be an actor or a role"):
            self.write(events=[{"type": "comment", "occurred_at": at(4), "pending_for": "doc:p1"}])

    def test_paths_accept_a_shell_safe_separator(self) -> None:
        for path in ("fact:part_of>key", "fact:part_of/key"):
            rows = self.memory.list_rows(entity_type="paragraph", where=[f"{path} = doc:c2"], columns=["key"])["rows"]
            self.assertEqual(rows, [{"key": "doc:p3"}])


if __name__ == "__main__":
    unittest.main()
