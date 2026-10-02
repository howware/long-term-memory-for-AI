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


class StructuredDocumentTests(unittest.TestCase):
    """A document is entities joined by part_of and ordered by position, with
    text as versioned facts; every change belongs to a work session
    (changeset); nothing is lost, so any earlier state can be read again."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Mara", "person:mara")
        nodes = [("doc:book", "book", "Book", None, None),
                 ("doc:c1", "chapter", "Chapter 1", "doc:book", 1), ("doc:c2", "chapter", "Chapter 2", "doc:book", 2),
                 ("doc:p1", "paragraph", "P1", "doc:c1", 1), ("doc:p2", "paragraph", "P2", "doc:c1", 2),
                 ("doc:p3", "paragraph", "P3", "doc:c2", 1)]
        texts = {"doc:p1": "Projects start with a charter.", "doc:p2": "Scope defines the work.",
                 "doc:p3": "Schedules sequence activities."}
        self.memory.write({
            "source": {"type": "test_fixture", "content": "first draft"}, "performed_by": "person:mara",
            "changeset": "S1",
            "predicates": [{"id": "body", "value_type": "text", "cardinality": "one"}],
            "entities": [{"key": key, "type": kind, "name": name} for key, kind, name, _, _ in nodes],
            "facts": [*({"subject": key, "predicate": "part_of", "value": parent, "valid_from": at(1)}
                        for key, _, _, parent, _ in nodes if parent),
                      *({"subject": key, "predicate": "position", "value": float(order), "valid_from": at(1)}
                        for key, _, _, parent, order in nodes if parent),
                      *({"subject": key, "predicate": "body", "value": text, "valid_from": at(1)} for key, text in texts.items())],
            "events": [{"key": "S1", "type": "work_session", "occurred_at": at(1, 9), "ended_at": at(1, 13)}],
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def session(self, key: str, day: int, changes: dict) -> dict:
        return self.memory.write({"source": {"type": "test_fixture", "content": f"session {key}"},
                                  "performed_by": "person:mara", "changeset": key,
                                  "events": [{"key": key, "type": "work_session", "occurred_at": at(day, 9)}], **changes})

    def texts(self, **kwargs) -> list[tuple[str, list[str]]]:
        tree = self.memory.outline("doc:book", show=["body"], **kwargs)["root"]
        return [(chapter["name"], [paragraph.get("body") for paragraph in chapter.get("children", [])])
                for chapter in tree["children"]]

    def test_outline_reads_the_structure_in_order(self) -> None:
        self.assertEqual(self.texts(), [("Chapter 1", ["Projects start with a charter.", "Scope defines the work."]),
                                        ("Chapter 2", ["Schedules sequence activities."])])
        self.assertEqual(self.memory.outline("doc:book", depth=1)["truncated_at_depth"], 1)

    def test_edits_moves_and_removals_keep_every_earlier_state(self) -> None:
        report = self.session("S2", 5, {"facts": [
            {"subject": "doc:p1", "predicate": "body", "value": "Every project starts with a charter.", "valid_from": at(5)},
            # Move P2 to the end of chapter 2.
            {"subject": "doc:p2", "predicate": "part_of", "value": "doc:c2", "valid_from": at(5)},
            {"subject": "doc:p2", "predicate": "position", "value": 2.0, "valid_from": at(5)}]})
        # Position 2.0 is the value it already had: the same fact, kept, not duplicated.
        self.assertEqual(report["changeset"], {"key": "S2", "asserted": 2, "closed": 2, "retracted": 0})
        self.session("S3", 8, {"close": [{"subject": "doc:p3", "predicate": "part_of", "at": at(8)}]})
        self.assertEqual(self.texts(), [("Chapter 1", ["Every project starts with a charter."]),
                                        ("Chapter 2", ["Scope defines the work."])])
        # The first draft reads exactly as it was.
        self.assertEqual(self.texts(as_of=at(2)), [("Chapter 1", ["Projects start with a charter.", "Scope defines the work."]),
                                                   ("Chapter 2", ["Schedules sequence activities."])])
        self.assertEqual(self.texts(as_of=at(6)), [("Chapter 1", ["Every project starts with a charter."]),
                                                   ("Chapter 2", ["Schedules sequence activities.", "Scope defines the work."])])

    def test_history_and_changesets_say_who_changed_what(self) -> None:
        self.session("S2", 5, {"facts": [{"subject": "doc:p1", "predicate": "body",
                                          "value": "Every project starts with a charter.", "valid_from": at(5)}]})
        versions = self.memory.fact_history("doc:p1", "body")
        self.assertEqual([(item["value"], item["changeset"], item.get("closed_in")) for item in versions],
                         [("Projects start with a charter.", "S1", "S2"), ("Every project starts with a charter.", "S2", None)])
        self.assertEqual(versions[1]["by"], "Mara")
        contents = self.memory.changeset_contents("S2")
        self.assertEqual(([item["subject"] for item in contents["asserted"]], [item["subject"] for item in contents["closed"]]),
                         (["doc:p1"], ["doc:p1"]))
        with self.assertRaisesRegex(MemoryError, "must be the key of an event"):
            self.memory.write({"source": {"type": "test_fixture", "content": "x"}, "performed_by": "person:mara",
                               "changeset": "NOPE", "facts": []})

    def test_recall_finds_current_and_earlier_text(self) -> None:
        self.session("S2", 5, {"facts": [{"subject": "doc:p1", "predicate": "body",
                                          "value": "Every initiative starts with a charter.", "valid_from": at(5)}]})
        found = {(item["external_key"], item["version"]) for item in self.memory.recall("charter")
                 if item["record_type"] == "assertion"}
        self.assertEqual(found, {("doc:p1", "current"), ("doc:p1", "superseded (ended)")})
        old = [item for item in self.memory.recall("projects start") if item["record_type"] == "assertion"]
        self.assertEqual(old[0]["version"], "superseded (ended)")


if __name__ == "__main__":
    unittest.main()
