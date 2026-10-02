from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402

DUE_SOON = {
    "kind": "list",
    "description": "Pedidos que vencen en la semana que empieza en start.",
    "parameters": {"start": {"type": "date", "default": "today"}},
    "query": {"event_type": "order", "columns": ["key", "attr:total"],
              "where": ["attr:due_date between {start}..{start+7}"], "order_by": ["attr:due_date"]},
}
BY_PRODUCT = {
    "kind": "aggregate",
    "parameters": {"product": {"type": "entity"}, "minimum": {"type": "real", "default": 0}},
    "query": {"event_type": "order", "measure": "sum:total",
              "where": ["link:contains = {product}", "attr:total >= {minimum}"]},
}


class RecipeTests(unittest.TestCase):
    """Phase 2d: saved queries with typed parameters, run by the helper."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        # The memory displays UTC here, so 'today' is today's UTC date.
        self.today = datetime.now(timezone.utc).date()
        due = lambda days: f"{(self.today + timedelta(days=days)).isoformat()}T12:00:00Z"
        self.memory.write({
            "source": {"type": "test_fixture", "content": "pedidos para recetas"}, "performed_by": "person:ana",
            "predicates": [{"id": "contains", "value_type": "entity"}, {"id": "total", "value_type": "real"},
                           {"id": "due_date", "value_type": "datetime"}],
            "entities": [{"key": "product:a", "type": "product", "name": "Producto A"},
                         {"key": "product:b", "type": "product", "name": "Producto B"}],
            "events": [{"key": key, "type": "order", "occurred_at": "2026-01-01T00:00:00Z",
                        "attributes": {"total": total, "due_date": due(days)},
                        "links": [{"predicate": "contains", "entity": product}]}
                       for key, total, days, product in (("O1", 100.0, 1, "product:a"), ("O2", 300.0, 3, "product:a"),
                                                         ("O3", 50.0, 20, "product:b"))],
        })
        self.memory.define_recipe("recipe:due-soon", "Pedidos por vencer", DUE_SOON)
        self.memory.define_recipe("recipe:sales-by-product", "Ventas de un producto", BY_PRODUCT)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_defaults_are_relative_to_today(self) -> None:
        run = self.memory.run_recipe("recipe:due-soon")
        self.assertEqual(run["parameters"], {"start": self.today.isoformat()})
        self.assertEqual([row["key"] for row in run["result"]["rows"]], ["O1", "O2"])
        later = self.memory.run_recipe("recipe:due-soon", {"start": "today+15"})
        self.assertEqual([row["key"] for row in later["result"]["rows"]], ["O3"])

    def test_typed_parameters(self) -> None:
        run = self.memory.run_recipe("recipe:sales-by-product", {"product": "Producto A", "minimum": "150"})
        self.assertEqual((run["result"]["total_rows"], run["result"]["groups"][0]["value"]), (1, 300.0))
        self.assertEqual(run["parameters"], {"product": "product:a", "minimum": 150.0})
        for given, message in (({}, "needs --param product"),
                               ({"product": "product:a", "minimum": "mucho"}, "expects a number"),
                               ({"product": "product:zzz"}, "Unknown entity"),
                               ({"product": "product:a", "color": "rojo"}, "has no parameter color")):
            with self.assertRaisesRegex(MemoryError, message):
                self.memory.run_recipe("recipe:sales-by-product", given)

    def test_text_parameters_cannot_change_a_condition(self) -> None:
        recipe = {"kind": "aggregate", "parameters": {"name": {"type": "text"}},
                  "query": {"event_type": "order", "where": ["attr:total = {name}"]}}
        self.memory.define_recipe("recipe:by-text", "Por texto", recipe)
        with self.assertRaisesRegex(MemoryError, "operator characters"):
            self.memory.run_recipe("recipe:by-text", {"name": "1 | 2"})

    def test_definitions_are_validated_versioned_and_listed(self) -> None:
        for broken, message in (({**DUE_SOON, "kind": "report"}, "kind"),
                                ({**DUE_SOON, "query": {**DUE_SOON["query"], "measure": "count"}}, "do not take"),
                                ({**DUE_SOON, "parameters": {}}, "without a declared parameter"),
                                ({**DUE_SOON, "parameters": {"start": {"type": "color"}}}, "needs a type")):
            with self.assertRaisesRegex(MemoryError, message):
                self.memory.define_recipe("recipe:broken", "Rota", broken)
        wider = {**DUE_SOON, "query": {**DUE_SOON["query"], "where": ["attr:due_date between {start}..{start+30}"]}}
        self.memory.define_recipe("recipe:due-soon", "Pedidos por vencer", wider)
        self.assertEqual(len(self.memory.run_recipe("recipe:due-soon")["result"]["rows"]), 3)
        history = [fact["assertion_state"] for fact in self.memory.describe("recipe:due-soon")["facts"]
                   if fact["predicate_id"] == "recipe_definition"]
        self.assertEqual(history, ["superseded", "asserted"])
        listed = {recipe["key"]: recipe for recipe in self.memory.catalog()["recipes"]}
        self.assertEqual(listed["recipe:due-soon"]["parameters"], {"start": {"type": "date", "default": "today"}})
        self.assertEqual(listed["recipe:sales-by-product"]["kind"], "aggregate")


if __name__ == "__main__":
    unittest.main()
