"""Shared ground: errors, UTC instants and display zones, identifiers, where the memory lives."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# Version of the helper and its skill (semantic versioning); the schema has its own
# version: the migrations applied to each memory.
__version__ = "0.2.0"


# The helper travels with its migrations (db/ in this repository, scripts/ in
# an installed skill), so a new project needs nothing else to start a memory.
KIT_DIR = Path(__file__).resolve().parent.parent


PROJECT_ROOT = KIT_DIR.parent


MEMORY_DIRNAME = ".memory"


DATABASE_FILENAME = "memory.sqlite"


HELPER_AGENT = "agent:memory-helper"


def parse_instant(text: str) -> datetime:
    """datetime.fromisoformat that also reads a trailing Z on Python < 3.11."""
    text = text.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def julianday_le(first: str | None, second: str) -> bool:
    """first <= second for stored instants (None means since always)."""
    if first is None:
        return True
    return parse_instant(first) <= parse_instant(second)


def shift_instant(instant: str, seconds: int) -> str:
    moment = parse_instant(instant) + timedelta(seconds=seconds)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


VOID_EVENT = "event_voided"


# Event types that are high-volume unless a person declares otherwise
# (define-event-type): they stay out of the brief's "most recent" list, which
# shows a one-line count per type instead. Everything else still counts them.
DEFAULT_HIGH_VOLUME = ("workflow_transition", "memory_backed_up")


def entity_label(alias: str, at: str | None = None) -> str:
    """SQL expression: the name an entity goes by. A rename is a 'name' fact
    (temporal, with evidence); canonical_name keeps the name it was created
    with. With at (an SQL expression), the name in force at that instant."""
    if at is None:
        valid = "label.assertion_state = 'asserted' AND label.valid_to IS NULL"
    else:
        valid = (f"label.assertion_state <> 'retracted' AND julianday(label.valid_from) <= julianday({at}) "
                 f"AND (label.valid_to IS NULL OR julianday(label.valid_to) > julianday({at}))")
    return (f"COALESCE((SELECT label.value_text FROM assertions AS label WHERE label.subject_entity_id = {alias}.entity_id "
            f"AND label.predicate_id = 'name' AND {valid} ORDER BY julianday(label.valid_from) DESC LIMIT 1), "
            f"{alias}.canonical_name)")


def active_event_sql(alias: str) -> str:
    """SQL condition: the event is not voided by a void that is itself in force.

    Voiding a void restores the original; longer chains are rejected on write.
    """
    return f"""NOT EXISTS (
        SELECT 1 FROM record_links AS void_link
          JOIN events AS void_event
            ON void_event.event_id = void_link.source_record_id AND void_event.event_type = '{VOID_EVENT}'
         WHERE void_link.target_record_type = 'event' AND void_link.target_record_id = {alias}.event_id
           AND void_link.predicate_id = 'voids'
           AND NOT EXISTS (SELECT 1 FROM record_links AS undo
                            WHERE undo.target_record_type = 'event' AND undo.target_record_id = void_event.event_id
                              AND undo.predicate_id = 'voids'))"""


# Agents record and observe; people perform and decide. An agent may register
# lessons, notes, questions and milestones it observed, never a decision on a
# person's behalf.
AGENT_NAMES = {"agent:claude-code": "Claude Code", "agent:codex": "Codex", "agent:memory-helper": "Memory helper"}

# Who can act: anything that produces data is an actor with its own identity,
# so every record names the specific person, agent, robot, sensor, system or
# webhook behind it. More kinds are data: define-values --predicate actor_kind.
ACTOR_KINDS = ("person", "agent", "robot", "sensor", "system", "webhook", "device", "service")


AGENT_KINDS = ("lesson", "note", "open_question", "milestone")


AGENT_SOURCE = "agent_observation"


def detect_agent() -> str | None:
    """The agent running the helper: MEMORY_AGENT, or Claude Code by its environment."""
    if os.environ.get("MEMORY_AGENT"):
        return os.environ["MEMORY_AGENT"]
    if os.environ.get("CLAUDECODE"):
        return "agent:claude-code"
    return None


# Kinds of project knowledge captured with `remember`. They are ordinary event
# types; the list only gives the agent a shared, small vocabulary.
MEMORY_KINDS = {
    "decision": "A decision and why it was made",
    "convention": "An agreed convention (names, formats, workflow)",
    "preference": "How the user prefers to work",
    "open_question": "An open question or pending decision",
    "answer": "An answer that closes an open question (--resolves)",
    "milestone": "Hito alcanzado",
    "lesson": "A lesson learned: what failed or worked, and why",
    "note": "A useful fact or context that fits nowhere else",
}


class MemoryError(RuntimeError):
    """Raised when a requested memory operation violates an invariant."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def normalize_instant(value: str, field: str = "instant") -> str:
    """Return a canonical UTC instant ending in Z.

    A value without Z or an offset is rejected instead of assuming a zone:
    guessing would silently shift the stored moment.
    """
    if isinstance(value, str) and value.strip().lower() == "now":
        return utc_now()
    try:
        parsed = parse_instant(value.strip())
    except (AttributeError, ValueError) as error:
        raise MemoryError(f"{field} is not ISO-8601: {value!r}") from error
    if parsed.tzinfo is None:
        raise MemoryError(f"{field} needs Z or a UTC offset (for example {value}Z or {value}-06:00): {value!r}")
    parsed = parsed.astimezone(timezone.utc)
    text = parsed.strftime("%Y-%m-%dT%H:%M:%S")
    if parsed.microsecond:
        text += f".{parsed.microsecond:06d}"
    return text + "Z"


INSTANT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


FIXED_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")


DATE_LITERAL = re.compile(r"\d{4}-\d{2}-\d{2}")


def same_zone(first: Any, second: Any) -> bool:
    """Whether two zones agree now (a named zone equals itself in every season)."""
    if getattr(first, "key", None) and getattr(first, "key", None) == getattr(second, "key", None):
        return True
    now = datetime.now(timezone.utc)
    return (now.astimezone(first) if first else now.astimezone()).utcoffset() ==         (now.astimezone(second) if second else now.astimezone()).utcoffset()


def display_zone(name: str | None) -> Any:
    """Zone used to show and query data. None or 'system' means the operating
    system's local zone, applied per instant so daylight-saving rules hold."""
    if not name or name.lower() == "system":
        return None
    if name.upper() == "UTC":
        return timezone.utc
    offset = FIXED_OFFSET.match(name)
    if offset:
        sign = -1 if offset.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(offset.group(2)), minutes=int(offset.group(3))))
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as error:  # zoneinfo raises several error types
        hint = ""
        try:
            from zoneinfo import available_timezones

            if not available_timezones():
                hint = " This Python has no time zone database: install it with 'pip install tzdata'."
        except Exception:
            pass
        raise MemoryError(f"Unknown time zone {name!r}; use an IANA name such as America/Mexico_City, UTC, "
                          f"a fixed offset such as -06:00, or system.{hint}") from error


def zone_label(zone: Any) -> str:
    now = datetime.now(zone) if zone else datetime.now().astimezone()
    offset = now.strftime("%z")
    name = getattr(zone, "key", None) or now.tzname()
    return f"{name} (UTC{offset[:3]}:{offset[3:]} now)"


def to_display(instant: str, zone: Any) -> str:
    """Show a stored UTC instant in the display zone, with its offset."""
    if zone is timezone.utc:
        return instant
    moment = parse_instant(instant)
    local = moment.astimezone(zone) if zone else moment.astimezone()
    return local.isoformat(timespec="microseconds" if local.microsecond else "seconds")


def localize(value: Any, zone: Any) -> Any:
    """Convert every stored UTC instant inside a result for presentation."""
    if isinstance(value, str) and INSTANT_PATTERN.match(value):
        return to_display(value, zone)
    if isinstance(value, dict):
        return {key: localize(item, zone) for key, item in value.items()}
    if isinstance(value, list):
        return [localize(item, zone) for item in value]
    return value


def query_bound(value: str, zone: Any, field: str) -> str:
    """Query limits may omit the zone: they then mean the display zone.

    Stored data still rejects zone-less instants (normalize_instant); a query
    bound is presentation-side, so the reader's zone is the natural meaning.
    """
    try:
        parsed = parse_instant(value.strip())
    except ValueError as error:
        raise MemoryError(f"{field} is not ISO-8601: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone) if zone else parsed.astimezone()
    return normalize_instant(parsed.isoformat(), field)


def content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_project_root(start: Path) -> Path:
    """Return the nearest ancestor holding .git, or start when there is none."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return start


def resolve_database(explicit: str | Path | None = None, cwd: str | Path | None = None) -> Path:
    """Locate the memory of the current project without leaking into another one.

    Order: explicit path, MEMORY_DB, the nearest .memory/ between the working
    directory and its project root, and finally <project root>/.memory/memory.sqlite.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("MEMORY_DB"):
        return Path(os.environ["MEMORY_DB"]).expanduser().resolve()
    start = Path(cwd or Path.cwd()).resolve()
    root = find_project_root(start)
    for directory in (start, *start.parents):
        candidate = directory / MEMORY_DIRNAME / DATABASE_FILENAME
        if candidate.exists():
            return candidate
        if directory == root:
            break
    return root / MEMORY_DIRNAME / DATABASE_FILENAME


# Links are read through one CTE so every read model sees an event whether the
# entity is a participant or the target of a record link.
LINKED_EVENTS_CTE = """
WITH linked(event_id, via) AS (
    SELECT event_id, role FROM event_participants WHERE entity_id = :entity
    UNION
    SELECT source_record_id, predicate_id FROM record_links
     WHERE source_record_type = 'event' AND target_record_type = 'entity'
       AND target_record_id = :entity
)
"""


def event_row(row: sqlite3.Row) -> dict[str, Any]:
    """Expose JSON summaries as attributes so agents do not parse strings."""
    result = dict(row)
    summary = result.get("summary")
    if isinstance(summary, str) and summary.startswith("{"):
        try:
            result["attributes"] = json.loads(summary)
            del result["summary"]
        except json.JSONDecodeError:
            pass
    return result


def recall_term(term: str) -> str:
    """One full-text term matching its singular and plural forms: the word
    loses a plural ending and becomes a prefix ("decisiones" → decision*,
    "transformadora" → transformadora*, which also finds "transformadoras").
    Short words stay exact so they do not match half the memory."""
    word = term.replace('"', "")
    if len(word) < 4:
        return f'"{word}"'
    lowered = word.lower()
    if len(word) > 5 and lowered.endswith("es"):
        word = word[:-2]
    elif lowered.endswith("s"):
        word = word[:-1]
    return f'"{word}"*'


def without_nulls(row: sqlite3.Row) -> dict[str, Any]:
    return {key: value for key, value in dict(row).items() if value is not None}
