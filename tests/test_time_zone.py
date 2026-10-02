from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "db"))

from memory import Memory, MemoryError, display_zone, localize, query_bound, render_brief  # noqa: E402


def _zone_data_available() -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("America/Mexico_City")
        return True
    except Exception:
        return False


# Named zones need the IANA database: built into Linux and macOS; on Windows
# it comes from the tzdata package (pip install tzdata).
ZONE_DATA = _zone_data_available()


@unittest.skipUnless(ZONE_DATA, "IANA time zone data missing (on Windows: pip install tzdata)")
class TimeZoneTests(unittest.TestCase):
    """Storage is UTC; presentation, zone-less query bounds and periods use the display zone."""

    def test_display_follows_daylight_saving_per_instant(self) -> None:
        zone = display_zone("America/Tijuana")
        shown = localize({"summer": "2026-07-01T12:00:00Z", "winter": ["2026-01-15T12:00:00Z"], "text": "2026 Z"}, zone)
        self.assertEqual(shown, {"summer": "2026-07-01T05:00:00-07:00", "winter": ["2026-01-15T04:00:00-08:00"], "text": "2026 Z"})
        self.assertEqual(localize("2026-07-01T12:00:00Z", display_zone("UTC")), "2026-07-01T12:00:00Z")
        with self.assertRaisesRegex(MemoryError, "Unknown time zone"):
            display_zone("Marte/Olympus")

    def test_query_bounds_without_zone_mean_the_display_zone(self) -> None:
        zone = display_zone("America/Mexico_City")
        self.assertEqual(query_bound("2025-01-01", zone, "since"), "2025-01-01T06:00:00Z")
        self.assertEqual(query_bound("2025-01-01T00:00:00Z", zone, "since"), "2025-01-01T00:00:00Z")

    def test_periods_group_in_the_display_zone_and_writes_stay_strict(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            memory = Memory(Path(folder) / "memory.sqlite", time_zone="America/Mexico_City")
            memory.init("Ana", "person:ana")
            # 03:00 UTC on Jan 1 is still Dec 31 in Mexico City.
            memory.write({"source": {"type": "test", "content": "evento de fin de año"}, "performed_by": "person:ana",
                          "events": [{"type": "sale", "occurred_at": "2025-01-01T03:00:00Z"}]})
            local = memory.aggregate("sale", by=["period:year"])["groups"]
            self.assertEqual(local[0]["period:year"], "2024")
            utc = Memory(memory.database_path, time_zone="UTC").aggregate("sale", by=["period:year"])["groups"]
            self.assertEqual(utc[0]["period:year"], "2025")
            self.assertEqual(memory.aggregate("sale", since="2024-12-31", until="2025-01-01")["total_rows"], 1)
            with self.assertRaisesRegex(MemoryError, "needs Z or a UTC offset"):
                memory.write({"source": {"type": "test", "content": "sin zona"}, "performed_by": "person:ana",
                              "events": [{"type": "sale", "occurred_at": "2025-01-01T03:00:00"}]})
            self.assertIn("America/Mexico_City", memory.brief()["time_zone"]["display"])

    def test_queries_warn_when_the_operation_lives_in_another_zone(self) -> None:
        self.assertEqual(display_zone("-05:00").utcoffset(None).total_seconds(), -5 * 3600)
        with tempfile.TemporaryDirectory() as folder:
            memory = Memory(Path(folder) / "memory.sqlite", time_zone="America/Tijuana")
            memory.init("Ana", "person:ana")
            memory.write({"source": {"type": "test", "content": "la tienda opera en Cancún"}, "performed_by": "person:ana",
                          "entities": [{"key": "organization:shop", "type": "organization", "name": "Tienda"}],
                          "facts": [{"subject": "organization:shop", "predicate": "time_zone", "value": "America/Cancun"}],
                          "events": [{"type": "sale", "occurred_at": "2025-01-01T03:00:00Z"}]})
            notice = memory.aggregate("sale", by=["period:day"])["time_zone_notice"]
            self.assertIn("Tienda operates in America/Cancun", notice)
            self.assertIn("--tz=America/Cancun", notice)
            self.assertIn("time_zone_notice", memory.list_rows("sale", since="2024-12-01"))
            self.assertNotIn("time_zone_notice", memory.aggregate("sale"))  # no dates, nothing to warn
            self.assertIn("Tienda operates in America/Cancun", render_brief(memory.brief()))
            same = Memory(memory.database_path, time_zone="America/Cancun")
            self.assertNotIn("time_zone_notice", same.aggregate("sale", by=["period:day"]))
            self.assertIs(same.catalog()["time_zone"]["operating"][0]["differs_from_display"], False)


if __name__ == "__main__":
    unittest.main()
