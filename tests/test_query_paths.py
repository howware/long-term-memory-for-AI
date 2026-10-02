from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


class QueryPathTests(unittest.TestCase):
    """Phase 2a: paths reach typed values, facts in force at the row's instant,
    events pointing back, link attributes; multi-valued groups are rejected."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.memory = Memory(Path(self.temp.name) / "memory.sqlite", time_zone="-06:00")  # Mexico City has no daylight saving time
        self.memory.init("Ana", "person:ana")
        predicates = [
            {"id": "category", "value_type": "entity", "cardinality": "one", "is_temporal": False},
            {"id": "price", "value_type": "real", "cardinality": "one", "unit": "MXN"},
            {"id": "active", "value_type": "boolean", "cardinality": "one", "is_temporal": False},
            {"id": "contains", "value_type": "entity"},
            {"id": "total", "value_type": "real"},
            {"id": "due_date", "value_type": "datetime"},
            {"id": "channel", "value_type": "text"},
            {"id": "qty", "value_type": "integer"},
            {"id": "ships", "value_type": "any"},
            {"id": "carrier", "value_type": "text"},
        ]
        entities = [
            {"key": "category:candy", "type": "category", "name": "Dulces"},
            {"key": "category:drinks", "type": "category", "name": "Bebidas"},
            {"key": "product:a", "type": "product", "name": "Producto A"},
            {"key": "product:b", "type": "product", "name": "Producto B"},
            {"key": "product:a2", "type": "product", "name": "Producto A"},  # a homonym, on purpose
        ]
        facts = [
            {"subject": "product:a", "predicate": "category", "value": "category:candy"},
            {"subject": "product:b", "predicate": "category", "value": "category:drinks"},
            {"subject": "product:a2", "predicate": "category", "value": "category:candy"},
            {"subject": "product:a", "predicate": "active", "value": True},
            {"subject": "product:b", "predicate": "active", "value": False},
            {"subject": "product:a2", "predicate": "active", "value": True},
            {"subject": "product:a", "predicate": "price", "value": 10.0, "valid_from": "2026-01-01T00:00:00Z"},
            {"subject": "product:a", "predicate": "price", "value": 12.0, "valid_from": "2026-02-01T00:00:00Z"},
            {"subject": "product:b", "predicate": "price", "value": 20.0, "valid_from": "2026-01-01T00:00:00Z"},
            {"subject": "product:a2", "predicate": "price", "value": 5.0, "valid_from": "2026-01-01T00:00:00Z"},
        ]

        def order(key, at, total, due, channel, lines):
            attributes = {"total": total, "channel": channel, **({"due_date": due} if due else {})}
            return {"key": key, "type": "order", "occurred_at": at, "attributes": attributes,
                    "links": [{"predicate": "contains", "entity": product, "attributes": {"qty": qty}} for product, qty in lines]}

        events = [
            order("O1", "2026-01-15T18:00:00Z", 100.0, "2026-02-10T12:00:00Z", "web", [("product:a", 2)]),
            order("O2", "2026-02-15T18:00:00Z", 300.0, "2026-03-05T12:00:00Z", "tienda", [("product:a", 5), ("product:b", 1)]),
            order("O3", "2026-02-20T18:00:00Z", 50.0, "2026-03-20T12:00:00Z", "web", [("product:b", 1)]),
            order("O4", "2026-03-01T18:00:00Z", 80.0, None, "tel", [("product:a2", 3)]),
            {"key": "S1", "type": "order_shipped", "occurred_at": "2026-01-16T18:00:00Z", "attributes": {"carrier": "DHL"},
             "links": [{"predicate": "ships", "event": "O1"}]},
            {"key": "S3", "type": "order_shipped", "occurred_at": "2026-02-21T18:00:00Z", "attributes": {"carrier": "UPS"},
             "links": [{"predicate": "ships", "event": "O3"}]},
        ]
        self.memory.write({"source": {"type": "test_fixture", "content": "pedidos neutros"}, "performed_by": "person:ana",
                           "predicates": predicates, "entities": entities, "facts": facts, "events": events})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def count(self, *where: str, **kwargs) -> int:
        return self.memory.aggregate(kwargs.pop("event_type", "order"), where=list(where), **kwargs)["total_rows"]

    def groups(self, spec: str, **kwargs) -> dict:
        result = self.memory.aggregate(kwargs.pop("event_type", "order"), by=[spec], **kwargs)
        return {row[spec]: row["value"] for row in result["groups"]}

    def test_typed_attribute_conditions(self) -> None:
        self.assertEqual(self.count("attr:total >= 100"), 2)
        self.assertEqual(self.count("attr:total between 50..100"), 2)  # upper bound excluded
        self.assertEqual(self.count("attr:channel in web|tienda"), 3)
        self.assertEqual(self.count("attr:channel != web"), 2)
        self.assertEqual(self.count("has:attr:due_date"), 3)
        self.assertEqual(self.count("no:attr:due_date"), 1)
        self.assertEqual(self.count("attr:total >= 100", "attr:channel = web"), 1)

    def test_dates_use_the_display_zone(self) -> None:
        self.assertEqual(self.count("attr:due_date >= 2026-03-01"), 2)
        self.assertEqual(self.groups("period:month@attr:due_date"), {"2026-02": 1, "2026-03": 2, None: 1})

    def test_paths_end_in_values(self) -> None:
        self.assertEqual(self.count("link:contains>fact:active = true"), 3)  # any product active
        self.assertEqual(self.count("all:link:contains>fact:active = true"), 2)  # every product active
        self.assertEqual(self.groups("line>fact:active", line="contains"), {True: 3, False: 2})
        self.assertEqual(self.count("link:contains>fact:category = Dulces"), 3)
        self.assertEqual(self.groups("line>fact:category", line="contains"), {"Dulces": 3, "Bebidas": 2})

    def test_fact_at_the_row_instant(self) -> None:
        self.assertEqual(self.groups("line>fact@row:price", line="contains"), {10.0: 1, 12.0: 1, 20.0: 2, 5.0: 1})
        self.assertEqual(self.groups("line>fact:price", line="contains"), {12.0: 2, 20.0: 2, 5.0: 1})
        with self.assertRaisesRegex(MemoryError, "fact@row needs rows with an instant"):
            self.memory.aggregate(None, entity_type="product", by=["fact@row:price"])

    def test_events_pointing_back_and_link_attributes(self) -> None:
        self.assertEqual(self.count("has:link-in:ships"), 2)
        self.assertEqual(self.count("no:link-in:ships[order_shipped]"), 2)
        self.assertEqual(self.groups("link-in:ships>attr:carrier"), {"DHL": 1, "UPS": 1, None: 2})
        self.assertEqual(self.count("link:contains>edge:qty >= 5"), 1)

    def test_multi_valued_groups_are_rejected(self) -> None:
        for spec in ("link:contains", "link:contains>fact:category"):
            with self.assertRaisesRegex(MemoryError, "several values"):
                self.memory.aggregate("order", by=[spec])

    def test_homonyms_stay_apart(self) -> None:
        self.assertEqual(self.groups("line", line="contains"),
                         {"Producto A [product:a]": 2, "Producto B": 2, "Producto A [product:a2]": 1})

    def test_entity_rows_compare_typed_facts(self) -> None:
        self.assertEqual(self.count("fact:active = true", event_type=None, entity_type="product"), 2)
        self.assertEqual(self.count("fact:price > 10", event_type=None, entity_type="product"), 2)

    def test_list_rows_with_columns_order_and_paging(self) -> None:
        listed = self.memory.list_rows("order", columns=["key", "attr:total", "link:contains"],
                                       order_by=["-attr:total"], limit=2)
        self.assertEqual((listed["total"], listed["returned"], listed["truncated"]), (4, 2, True))
        self.assertEqual(listed["rows"], [
            {"key": "O2", "attr:total": 300.0, "link:contains": ["Producto A [product:a]", "Producto B"]},
            {"key": "O1", "attr:total": 100.0, "link:contains": ["Producto A [product:a]"]},
        ])
        rest = self.memory.list_rows("order", columns=["key"], order_by=["-attr:total"], limit=2, offset=2)
        self.assertEqual([row["key"] for row in rest["rows"]], ["O4", "O3"])
        self.assertFalse(rest["truncated"])

    def test_list_defaults_conditions_and_lines(self) -> None:
        default = self.memory.list_rows("order", where=["no:link-in:ships"])
        self.assertEqual(default["columns"], ["key", "occurred_at", "participant:performed_by"])
        self.assertEqual([(row["key"], row["participant:performed_by"]) for row in default["rows"]],
                         [("O2", "Ana"), ("O4", "Ana")])
        lines = self.memory.list_rows("order", line="contains", columns=["key", "line", "attr:qty", "line>fact@row:price"],
                                      where=["attr:channel = web"])
        self.assertEqual([tuple(row.values()) for row in lines["rows"]],
                         [("O1", "Producto A [product:a]", 2, 10.0), ("O3", "Producto B", 1, 20.0)])

    def test_list_entities(self) -> None:
        products = self.memory.list_rows(entity_type="product", columns=["key", "fact:active", "fact:price"],
                                         where=["fact:category = Dulces"], order_by=["-fact:price"])
        self.assertEqual(products["rows"], [{"key": "product:a", "fact:active": True, "fact:price": 12.0},
                                            {"key": "product:a2", "fact:active": True, "fact:price": 5.0}])

    def test_anchored_conditions_use_the_value_indexes(self) -> None:
        with self.memory.connection() as connection:
            for clause, index in (("attr:due_date >= 2026-03-01", "record_attributes_datetime_value"),
                                  ("attr:total >= 100", "record_attributes_real_value"),
                                  ("link:contains = product:b", "record_links_target_lookup")):
                parameters: dict = {}
                condition = self.memory._condition_sql(connection, clause, parameters, "t", "events.event_id", "'event'",
                                                       "events.occurred_at")
                plan = connection.execute(
                    f"EXPLAIN QUERY PLAN SELECT event_id FROM events WHERE event_type = 'order' AND {condition}", parameters
                ).fetchall()
                self.assertTrue(any(index in row[3] for row in plan), (clause, [row[3] for row in plan]))

    def test_fractional_instants_at_range_edges(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "fracción de segundo"}, "performed_by": "person:ana",
                           "events": [{"key": "O5", "type": "order", "occurred_at": "2026-03-02T00:00:00Z",
                                       "attributes": {"due_date": "2026-03-01T06:00:00.500000Z", "total": 1.0}}]})
        # Midnight of March 1 in Mexico City is 06:00Z; the half second after it belongs to March.
        self.assertEqual(self.count("attr:due_date between 2026-03-01..2026-03-02", "attr:total = 1"), 1)
        self.assertEqual(self.count("attr:due_date < 2026-03-01", "attr:total = 1"), 0)
        self.assertEqual(self.count("attr:due_date >= 2026-03-01T06:00:00.500000Z", "attr:total = 1"), 1)

    def test_malformed_queries_explain_themselves(self) -> None:
        for where, message in (("attr:total >= mucho", "not a number"),
                               ("attr:total>100", "Cannot read condition"),
                               ("attr:active = quizá", "Unknown predicate active|not true or false"),
                               ("wat:thing = 1", "Unknown path step")):
            with self.assertRaisesRegex(MemoryError, message):
                self.memory.aggregate("order", where=[where])
        with self.assertRaisesRegex(MemoryError, "cannot continue"):
            self.memory.aggregate("order", by=["attr:total>fact:category"])


if __name__ == "__main__":
    unittest.main()
