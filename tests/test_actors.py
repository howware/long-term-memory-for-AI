from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError  # noqa: E402


class ActorTests(unittest.TestCase):
    """Anything that produces data is an actor with its own identity (person,
    agent, robot, sensor, system, webhook…); decisions stay with people."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite"
        self.memory = Memory(self.path, time_zone="UTC")
        self.memory.init("Ana", "person:ana")
        self.memory.write({
            "source": {"type": "test_fixture", "content": "planta y sensores"}, "performed_by": "person:ana",
            "predicates": [{"id": "temperature", "value_type": "real", "unit": "C"}],
            "entities": [{"key": "machine:oven", "type": "machine", "name": "Horno 1"},
                         {"key": "sensor:t1", "type": "sensor", "name": "Termopar 1"},
                         {"key": "sensor:t2", "type": "sensor", "name": "Termopar 2"},
                         {"key": "webhook:bank", "type": "webhook", "name": "Aviso de pago del banco"}],
        })

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_non_human_actors_perform_events(self) -> None:
        self.memory.write({"source": {"type": "webhook_payload", "content": '{"paid": 45}'}, "performed_by": "webhook:bank",
                           "events": [{"key": "PAY-1", "type": "payment_received", "occurred_at": "now"}]})
        self.assertEqual(self.memory.event("PAY-1")["participants"][0]["name"], "Aviso de pago del banco")
        catalog = self.memory.catalog()
        self.assertEqual(catalog["integrity"]["events_without_actor"], 0)
        self.assertEqual({item["kind"]: item["count"] for item in catalog["actors"]}, {"sensor": 2, "webhook": 1})

    def test_new_actor_kinds_are_data(self) -> None:
        self.memory.write({"source": {"type": "test_fixture", "content": "dron"}, "performed_by": "person:ana",
                           "entities": [{"key": "drone:d1", "type": "drone", "name": "Dron 1"}]})
        flight = {"source": {"type": "test_fixture", "content": "vuelo"}, "performed_by": "drone:d1",
                  "events": [{"key": "FLY-1", "type": "flight", "occurred_at": "now"}]}
        with self.assertRaisesRegex(MemoryError, "performed_by must be an actor .* drone:d1 is a drone"):
            self.memory.write(flight)
        self.memory.define_values("actor_kind", ["drone"], "person:ana")
        self.memory.write(flight)
        self.assertEqual(self.memory.event("FLY-1")["event_type"], "flight")

    def test_decisions_stay_with_people(self) -> None:
        with self.assertRaisesRegex(MemoryError, "A decision is a decision of a person; sensor:t1 is a sensor"):
            self.memory.remember("decision", "Subir la temperatura", performed_by="sensor:t1")
        self.memory.write({"source": {"type": "sensor", "content": "alarma"}, "performed_by": "sensor:t1",
                           "events": [{"key": "AL-1", "type": "alarm", "occurred_at": "now"}]})
        with self.assertRaisesRegex(MemoryError, "Voiding an event is a decision of a person"):
            self.memory.void("AL-1", "falsa alarma", performed_by="sensor:t2")
        self.memory.void("AL-1", "falsa alarma", performed_by="person:ana")

    def test_observations_name_the_sensor_that_produced_them(self) -> None:
        report = self.memory.write({
            "source": {"type": "sensor_batch", "content": "lecturas del horno"},
            "observations": [{"subject": "machine:oven", "predicate": "temperature", "value": value,
                              "occurred_at": f"2026-10-01T10:0{minute}:00Z", "by": sensor}
                             for minute, (value, sensor) in enumerate([(180.5, "sensor:t1"), (181.0, "sensor:t2"),
                                                                        (182.2, "sensor:t1")])],
        })
        self.assertEqual(report["observations"], 3)
        with closing(sqlite3.connect(self.path)) as connection:
            by_source = dict(connection.execute(
                """SELECT entities.external_key, COUNT(*) FROM observations
                     JOIN entities ON entities.entity_id = observations.source_entity_id GROUP BY entities.external_key"""))
        self.assertEqual(by_source, {"sensor:t1": 2, "sensor:t2": 1})
        self.assertEqual(self.memory.catalog()["observations"][0]["sources"], 2)
        with self.assertRaisesRegex(MemoryError, "observations need by"):
            self.memory.write({"source": {"type": "sensor_batch", "content": "sin fuente"},
                               "observations": [{"subject": "machine:oven", "predicate": "temperature", "value": 1.0,
                                                 "occurred_at": "now"}]})


if __name__ == "__main__":
    unittest.main()
