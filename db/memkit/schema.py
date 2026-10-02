"""Schema: migrations, opening a memory (with automatic additive upgrades) and init."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .base import AGENT_SOURCE, HELPER_AGENT, KIT_DIR, MEMORY_DIRNAME, MemoryError, new_id, utc_now


MIGRATIONS_DIR = KIT_DIR / "migrations"


STANDARD_PREDICATES = (
    ("workflow_status", "Workflow status", "entity", "one", 1),
    ("assigned_to", "Assigned to", "entity", "one", 1),
    ("pending_action_for", "Pending action for", "entity", "one", 1),
    ("tracking_code", "Tracking code", "text", "one", 1),
    ("from_workflow_state", "From workflow state", "entity", "one", 0),
    ("to_workflow_state", "To workflow state", "entity", "one", 0),
    ("is_terminal", "Is terminal", "boolean", "one", 0),
    ("requires_pending_action", "Requires a pending action", "boolean", "one", 0),
    ("is_memory_owner", "Owner of the memory", "boolean", "one", 0),
    ("rationale", "Rationale", "text", "many", 0),
    ("supersedes", "Supersedes", "any", "many", 0),
    ("resolves", "Resolves", "any", "many", 0),
    ("voids", "Voids", "any", "many", 0),
    ("rule_definition", "Rule definition", "json", "one", 1),
    ("recipe_definition", "Recipe definition", "json", "one", 1),
    ("time_zone", "Operating time zone", "text", "one", 1),
    ("process_definition", "Process definition", "json", "one", 1),
    ("process_owner", "Process owner", "entity", "one", 1),
    ("holds_role", "Holds role", "entity", "many", 1),
    ("follows_process", "Follows process", "entity", "one", 0),
    ("process_version", "Process version", "integer", "one", 1),
    ("process_step", "Process step", "text", "one", 1),
    ("process_instance", "Process instance", "entity", "many", 0),
    ("under_process", "Under process", "entity", "many", 0),
    ("from_step", "From step", "text", "one", 0),
    ("to_step", "To step", "text", "one", 0),
    ("override", "Outside the process", "boolean", "one", 0),
    ("override_kind", "Kind of exception", "text", "one", 0),
    ("from_version", "From version", "integer", "one", 0),
    ("skipped_data", "Data an exception left unrecorded", "text", "one", 0),
    ("produced_by", "Produced by", "entity", "many", 1),
    ("allowed_value", "Allowed value", "text", "many", 1),
    ("requested_step", "Requested step", "text", "one", 0),
    ("requested_attributes", "Data for the requested step", "json", "one", 0),
    ("decision", "Decision", "text", "one", 0),
    ("stricter_version_note", "What the version in force would have required", "text", "one", 0),
    ("value_entity_type", "Allowed entity type", "text", "one", 1),
    ("actor_kind", "Actor kind (vocabulary of extra actor kinds)", "text", "many", 1),
    ("part_of", "Part of (parent in a structure)", "entity", "one", 1),
    ("position", "Position among siblings", "real", "one", 1),
    ("changeset", "Changeset (work session) that made this", "text", "one", 0),
    ("closed_in_changeset", "Changeset that closed this fact", "text", "one", 0),
    ("retracted_in_changeset", "Changeset that retracted this fact", "text", "one", 0),
    ("change_rule_definition", "Change rule definition", "json", "one", 1),
    ("migration_ids", "Applied migrations", "text", "one", 0),
    ("migration_mode", "How migrations were applied", "text", "one", 0),
    ("backup_path", "Backup taken first", "text", "one", 0),
    ("name", "Name in force (a rename; the entity keeps the name it was created with)", "text", "one", 1),
    ("requested_action", "What the pending actor is asked to do", "text", "one", 0),
)


# Each migration declares its kind in its first line ("-- kind: additive").
# Additive ones only add objects and cannot fail on or rewrite existing rows,
# so opening a memory applies them; transformative ones (or undeclared ones)
# wait for an explicit init, which backs the memory up first.
MIGRATION_KIND = re.compile(r"^--\s*kind:\s*(additive|transformative)\b", re.IGNORECASE)


def migration_kind(path: Path) -> str:
    with path.open(encoding="utf-8") as handle:
        match = MIGRATION_KIND.match(handle.readline())
    return match.group(1).lower() if match else "transformative"


class SchemaMixin:
    """Schema: migrations, opening a memory (with automatic additive upgrades) and init."""

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # sqlite3.connect silently creates an empty file; a missing memory must
        # be reported so the agent runs init instead of reading a blank database.
        if not self.database_path.exists():
            raise MemoryError(
                f"No memory database at {self.database_path}. "
                "Run: memory.py init --owner-name <name> --owner-key person:<id>"
            )
        self._upgrade_on_open()
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        # Random UUID keys touch many index pages; the 2 MB default cache
        # makes writes slow down as the memory grows.
        connection.execute("PRAGMA cache_size = -65536")
        connection.create_function("local_time", 2, self._local_time, deterministic=True)
        newer: list[str] = []
        try:
            newer = self._require_current_schema(connection)
            if newer:
                # A newer helper wrote this schema; this one could break rules
                # it does not know, so it may read but never write.
                connection.execute("PRAGMA query_only = ON")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as error:
            connection.rollback()
            if newer and "readonly" in str(error):
                raise MemoryError(
                    f"This memory uses a newer schema ({', '.join(newer)}) than this helper; "
                    "it can be read but not written. Update the helper (reinstall the skill)."
                ) from error
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _require_current_schema(connection: sqlite3.Connection) -> list[str]:
        """Refuse a schema older than the helper; return migrations newer than it."""
        expected = {path.stem for path in MIGRATIONS_DIR.glob("*.sql")}
        applied = {row[0] for row in connection.execute("SELECT migration_id FROM schema_migrations")}
        missing = sorted(expected - applied)
        if missing:
            raise MemoryError(
                f"Memory schema is outdated (missing {', '.join(missing)}) and needs a transformative migration. "
                "Run: memory.py init (it backs the memory up first)"
            )
        return sorted(applied - expected)

    def schema_status(self) -> dict[str, Any]:
        """Migrations pending (with their kind) and those unknown to this helper."""
        migrations = {path.stem: migration_kind(path) for path in sorted(MIGRATIONS_DIR.glob("*.sql"))}
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "schema_migrations" not in tables:
                return {"initialized": False, "pending": [], "newer": [], "missing_predicates": []}
            applied = {row[0] for row in connection.execute("SELECT migration_id FROM schema_migrations")}
            known = {row[0] for row in connection.execute("SELECT predicate_id FROM predicates")} \
                if "predicates" in tables else set()
        finally:
            connection.close()
        return {
            "initialized": True,
            "pending": [{"migration": stem, "kind": kind} for stem, kind in migrations.items() if stem not in applied],
            "newer": sorted(applied - set(migrations)),
            "missing_predicates": [spec[0] for spec in STANDARD_PREDICATES if spec[0] not in known],
        }

    def _upgrade_on_open(self) -> None:
        """Apply additive migrations and standard predicates when a memory opens.

        Transformative migrations are left for init (which backs up first);
        the schema check then explains what to run. Every schema change is
        recorded as a schema_migrated event, so the next brief shows it.
        """
        if self._upgrading:
            return
        status = self.schema_status()
        if not status["initialized"] or status["newer"]:
            return
        pending = status["pending"]
        if any(item["kind"] != "additive" for item in pending):
            return
        if not pending and not status["missing_predicates"]:
            return
        self._upgrading = True
        try:
            applied = self.apply_migrations() if pending else []
            self.bootstrap_predicates()
            if applied:
                self._refresh_statistics()
                self._log_migration(applied, "auto")
        finally:
            self._upgrading = False

    def _log_migration(self, applied: list[str], mode: str, backup: Path | None = None) -> None:
        detail = f" Backup taken first: {backup}." if backup else ""
        summary = f"Schema updated ({'automatically' if mode == 'auto' else 'by init'}): {', '.join(applied)}.{detail}"
        attributes = {"migration_ids": ", ".join(applied), "migration_mode": mode}
        if backup:
            attributes["backup_path"] = str(backup)
        self.write({
            "source": {"type": AGENT_SOURCE, "content": summary},
            "events": [{"key": f"schema_migrated:{applied[-1]}", "type": "schema_migrated", "observed_by": HELPER_AGENT,
                        "occurred_at": utc_now(), "summary": summary, "attributes": attributes}],
        })

    def apply_migrations(self) -> list[str]:
        """Apply pending migrations in order; already applied ones are skipped.

        Two processes may open an outdated memory at once: each migration runs
        in its own BEGIN IMMEDIATE, and one that fails because the other process
        already applied it is skipped.
        """
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL) STRICT"
            )
            connection.commit()
            applied = {row[0] for row in connection.execute("SELECT migration_id FROM schema_migrations")}
            newly_applied = []
            for migration_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if migration_path.stem in applied:
                    continue
                try:
                    connection.executescript(migration_path.read_text(encoding="utf-8"))
                except sqlite3.Error:
                    if connection.in_transaction:
                        connection.rollback()
                    applied = {row[0] for row in connection.execute("SELECT migration_id FROM schema_migrations")}
                    if migration_path.stem in applied:
                        continue
                    raise
                newly_applied.append(migration_path.stem)
            return newly_applied
        finally:
            connection.close()

    def init(self, owner_name: str | None = None, owner_key: str | None = None) -> dict[str, Any]:
        """Create or upgrade this project's memory. Safe to run more than once.

        The owner is the person who performs events by default; it is never
        invented, so both values must come from the user. Before a
        transformative migration the existing memory is copied to backups/.
        """
        if bool(owner_name) != bool(owner_key):
            raise MemoryError("--owner-name and --owner-key must be given together")
        existed = self.database_path.exists()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.parent.name == MEMORY_DIRNAME:
            ignore = self.database_path.parent / ".gitignore"
            if not ignore.exists():
                ignore.write_text("# Local project memory: never commit.\n*\n", encoding="utf-8")
        backup = None
        if existed:
            status = self.schema_status()
            if status["newer"]:
                raise MemoryError(f"This memory uses a newer schema ({', '.join(status['newer'])}); update the helper instead")
            if any(item["kind"] != "additive" for item in status["pending"]):
                backup = self._backup()
        self._upgrading = True
        try:
            migrations = self.apply_migrations()
            self.bootstrap_predicates()
        finally:
            self._upgrading = False
        self._refresh_statistics()
        if existed and migrations:
            self._log_migration(migrations, "init", backup)
        owner = self._ensure_owner(owner_name, owner_key) if owner_name else None
        return {
            "database": str(self.database_path),
            "created": not existed,
            "migrations_applied": migrations,
            "backup": str(backup) if backup else None,
            "owner": owner,
        }

    def _backup(self, target: Path | None = None) -> Path:
        """Consistent copy of the memory (SQLite's online backup: it includes
        what is still in the -wal file and works while agents are writing).
        Default: backups/ next to the memory (local, never versioned)."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if target is None or target.is_dir():
            folder = target or self.database_path.parent / "backups"
            folder.mkdir(parents=True, exist_ok=True)
            if target is None and not (folder / ".gitignore").exists():
                (folder / ".gitignore").write_text("*\n", encoding="utf-8")
            target = folder / f"{self.database_path.stem}-{stamp}.sqlite"
            counter = 1
            while target.exists():
                counter += 1
                target = folder / f"{self.database_path.stem}-{stamp}-{counter}.sqlite"
        if target.exists():
            raise MemoryError(f"{target} already exists; a backup never overwrites a file")
        if target.resolve() == self.database_path.resolve():
            raise MemoryError("The backup target is the memory itself")
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(self.database_path)
        copy = sqlite3.connect(target)
        try:
            source.execute("PRAGMA busy_timeout = 5000")
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        return target

    def backup(self, target: str | Path | None = None) -> dict[str, Any]:
        """Copy the memory to a file and verify the copy. The memory is a
        single file and the only copy of what it holds; copying the file by
        hand while it is open can miss recent writes (they live in -wal)."""
        if not self.database_path.exists():
            raise MemoryError(f"No memory database at {self.database_path}")
        path = self._backup(Path(target).expanduser() if target else None)
        check = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            counts = {table: check.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("entities", "assertions", "events", "evidence")}
        finally:
            check.close()
        if integrity != "ok":
            raise MemoryError(f"The copy at {path} failed its integrity check: {integrity}")
        summary = f"Memory backed up to {path} ({path.stat().st_size} bytes, integrity ok)."
        logged = True
        try:
            self.write({
                "source": {"type": AGENT_SOURCE, "content": summary},
                "events": [{"type": "memory_backed_up", "observed_by": HELPER_AGENT, "occurred_at": utc_now(),
                            "summary": summary, "attributes": {"backup_path": str(path)}}],
            })
        except (MemoryError, sqlite3.Error):
            logged = False  # A read-only memory (newer schema) is still copied, just not logged.
        return {
            "backup": str(path),
            "bytes": path.stat().st_size,
            "integrity": integrity,
            "rows": counts,
            "logged": logged,
            "restore": "Close every agent session, then replace the memory file with this copy "
                       "and delete its -wal and -shm files if present.",
        }

    def _refresh_statistics(self) -> None:
        """Bounded ANALYZE so the planner knows the real shape of the data."""
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA analysis_limit = 1000")
            connection.execute("ANALYZE")
            connection.commit()
        finally:
            connection.close()

    def _ensure_owner(self, name: str, key: str) -> dict[str, Any]:
        with self.connection() as connection:
            existing = connection.execute("SELECT * FROM entities WHERE external_key = ?", (key,)).fetchone()
            if existing is not None:
                if existing["entity_type"] != "person":
                    raise MemoryError(f"{key} exists but is not a person")
                return {"entity_id": existing["entity_id"], "external_key": key, "name": existing["canonical_name"], "created": False}
            now = utc_now()
            entity_id = new_id()
            connection.execute(
                """INSERT INTO entities(entity_id, entity_type, canonical_name, external_key, description, created_at, recorded_at)
                   VALUES (?, 'person', ?, ?, 'Owner of this memory.', ?, ?)""",
                (entity_id, name, key, now, now),
            )
            owner = connection.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
            owner_fact = new_id()
            connection.execute(
                """INSERT INTO assertions(assertion_id, subject_entity_id, predicate_id, value_type, value_boolean,
                                          assertion_state, confidence, recorded_at)
                   VALUES (?, ?, 'is_memory_owner', 'boolean', 1, 'asserted', 1.0, ?)""",
                (owner_fact, entity_id, now),
            )
            event_id = self.record_event_in_transaction(
                connection, "memory_initialized", f"Memory initialized; owner: {name}.", owner, now, []
            )
            # The owner fact rests on the same evidence as the initialization event.
            connection.execute(
                """INSERT INTO assertion_evidence(assertion_id, evidence_id, role, recorded_at)
                   SELECT ?, evidence_id, 'supports', ? FROM event_evidence WHERE event_id = ?""",
                (owner_fact, now, event_id),
            )
            return {"entity_id": entity_id, "external_key": key, "name": name, "created": True}

    def bootstrap_predicates(self) -> None:
        with self.connection() as connection:
            now = utc_now()
            for predicate_id, label, value_type, cardinality, temporal in STANDARD_PREDICATES:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO predicates
                    (predicate_id, label, value_type, cardinality, is_temporal, description, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        predicate_id,
                        label,
                        value_type,
                        cardinality,
                        temporal,
                        "Standard predicate managed by the local memory helper.",
                        now,
                    ),
                )
