"""The Memory class: one object combining schema, capture, processes, queries and reading."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from .base import display_zone, parse_instant, resolve_database
from .schema import SchemaMixin
from .capture import CaptureMixin
from .processes import ProcessMixin
from .query import QueryMixin
from .reading import ReadingMixin


class Memory(SchemaMixin, CaptureMixin, ProcessMixin, QueryMixin, ReadingMixin):
    """A project's long-term memory in SQLite, used through this one object."""

    def __init__(self, database_path: str | Path | None = None, time_zone: str | None = None) -> None:
        self.database_path = Path(database_path) if database_path else resolve_database()
        # Storage is always UTC; this zone only drives presentation, zone-less
        # query bounds and period grouping. Default: the system's zone.
        self.time_zone = time_zone or os.environ.get("MEMORY_TZ") or "system"
        self.zone = display_zone(self.time_zone)
        self._upgrading = False

    def _local_time(self, pattern: str | None, instant: str | None) -> str | None:
        if pattern is None or instant is None:
            return None
        moment = parse_instant(instant)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (moment.astimezone(self.zone) if self.zone else moment.astimezone()).strftime(pattern)
