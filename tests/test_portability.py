from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from memory import Memory, MemoryError, resolve_database  # noqa: E402
from install_skill import install  # noqa: E402


class StartInNewProject(unittest.TestCase):
    """A new project must be able to start its memory from the skill alone."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_installed_skill_starts_memory_in_empty_project(self) -> None:
        skill = self.root / "skills" / "project-memory"
        install(skill)
        project = self.root / "nuevo-proyecto"
        (project / ".git").mkdir(parents=True)
        (project / "src").mkdir()
        helper = skill / "scripts" / "memory.py"
        env = {key: value for key, value in os.environ.items() if key != "MEMORY_DB"}

        def run(*args: str, cwd: Path) -> dict:
            completed = subprocess.run(
                [sys.executable, str(helper), *args], cwd=cwd, env=env, capture_output=True, check=True
            )
            return json.loads(completed.stdout.decode("utf-8"))

        self.assertFalse(run("where", cwd=project)["exists"])
        result = run("init", "--owner-name", "Ana", "--owner-key", "person:ana", cwd=project)
        database = project / ".memory" / "memory.sqlite"
        self.assertEqual(Path(result["database"]), database)
        self.assertTrue(result["created"])
        self.assertEqual((project / ".memory" / ".gitignore").read_text(encoding="utf-8").splitlines()[-1], "*")
        # A subdirectory of the project finds the same memory.
        catalog = run("catalog", cwd=project / "src")
        self.assertEqual(Path(catalog["database"]), database)
        self.assertEqual(catalog["people"][0]["external_key"], "person:ana")
        self.assertEqual(catalog["event_types"][0]["event_type"], "memory_initialized")
        self.assertEqual(run("timeline", "Ana", cwd=project)["events"][0]["performed_by_name"], "Ana")

    def test_init_is_idempotent_and_requires_complete_owner(self) -> None:
        memory = Memory(self.root / ".memory" / "memory.sqlite")
        with self.assertRaises(MemoryError):
            memory.init("Ana", None)
        first = memory.init("Ana", "person:ana")
        second = memory.init("Ana", "person:ana")
        self.assertTrue(first["owner"]["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["migrations_applied"], [])
        self.assertFalse(second["owner"]["created"])
        self.assertEqual(len(memory.timeline("person:ana")), 1)

    def test_missing_database_is_reported_not_created(self) -> None:
        missing = self.root / ".memory" / "memory.sqlite"
        with self.assertRaises(MemoryError):
            Memory(missing).catalog()
        self.assertFalse(missing.exists())

    def test_resolution_stays_inside_the_project(self) -> None:
        outer = self.root / ".memory"
        outer.mkdir()
        (outer / "memory.sqlite").touch()
        project = self.root / "otro"
        (project / ".git").mkdir(parents=True)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEMORY_DB", None)
            # The outer memory is above the project root and must not be used.
            self.assertEqual(resolve_database(cwd=project), project.resolve() / ".memory" / "memory.sqlite")
            os.environ["MEMORY_DB"] = str(self.root / "explicit.sqlite")
            self.assertEqual(resolve_database(cwd=project), (self.root / "explicit.sqlite").resolve())


class ColdStartReads(unittest.TestCase):
    """Reads must see events linked through record_links, not only participants."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite")
        self.memory.init("Alicia", "person:alicia")
        self.memory.write({
            "source": {"type": "test_fixture", "content": "Tiendas, centro y tres entregas."},
            "performed_by": "person:alicia",
            "predicates": [
                {"id": "served_by", "value_type": "entity"},
                {"id": "delivered_to", "value_type": "entity"},
                {"id": "units", "value_type": "integer"},
            ],
            "entities": [
                {"key": "hub:north", "type": "hub", "name": "Centro Norte"},
                {"key": "shop:1", "type": "shop", "name": "Tienda 1"},
                {"key": "shop:twin-a", "type": "shop", "name": "Tienda Gemela"},
                {"key": "shop:twin-b", "type": "shop", "name": "Tienda Gemela"},
            ],
            "facts": [{"subject": "shop:1", "predicate": "served_by", "value": "hub:north"}],
            # The first two are the same instant, written in UTC and with an offset.
            "events": [
                {"type": "delivery", "occurred_at": occurred_at, "attributes": {"units": units},
                 "links": [{"predicate": "delivered_to", "entity": "shop:1"}]}
                for occurred_at, units in (("2026-02-01T09:00:00Z", 10), ("2026-02-01T03:00:00-06:00", 20),
                                           ("2026-03-01T09:00:00Z", 30))
            ],
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_timeline_reads_record_links_and_normalizes_time(self) -> None:
        page = self.memory.timeline_page("Tienda 1", limit=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual([event["attributes"]["units"] for event in page["events"]], [20, 30])
        self.assertEqual(page["events"][0]["linked_as"], "delivered_to")
        february = self.memory.timeline("shop:1", since="2026-02-01T09:00:00Z", until="2026-02-01T09:00:01Z")
        self.assertEqual([event["occurred_at"] for event in february], ["2026-02-01T09:00:00Z"] * 2)
        with self.assertRaisesRegex(MemoryError, "needs Z or a UTC offset"):
            self.memory.record_event("delivery", "sin zona", "person:alicia", occurred_at="2026-02-01T09:00:00")

    def test_describe_shows_incoming_facts_and_activity(self) -> None:
        described = self.memory.describe("hub:north")
        self.assertEqual(described["incoming"]["served_by"]["total"], 1)
        self.assertEqual(described["incoming"]["served_by"]["sample"][0]["subject_key"], "shop:1")
        activity = self.memory.describe("shop:1")["activity"]
        self.assertEqual(activity, [{"event_type": "delivery", "via": "delivered_to", "events": 3,
                                     "first_at": "2026-02-01T09:00:00Z", "last_at": "2026-03-01T09:00:00Z"}])

    def test_names_resolve_only_when_unambiguous(self) -> None:
        self.assertEqual(self.memory.detail("tienda 1")["name"], "Tienda 1")
        with self.assertRaisesRegex(MemoryError, "Ambiguous"):
            self.memory.detail("Tienda Gemela")
        self.assertEqual([row["external_key"] for row in self.memory.search("Gemela")], ["shop:twin-a", "shop:twin-b"])
        self.assertEqual(self.memory.search("tienda 1")[0]["rank"], 0)

    def test_catalog_profiles_event_types(self) -> None:
        delivery = next(item for item in self.memory.catalog()["event_types"] if item["event_type"] == "delivery")
        self.assertEqual(delivery["count"], 3)
        self.assertEqual(delivery["attribute_keys"], {"units": 3})
        self.assertIn({"kind": "link", "via": "delivered_to", "target_type": "shop", "seen": 3}, delivery["relations"])


if __name__ == "__main__":
    unittest.main()
