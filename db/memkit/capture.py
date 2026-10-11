"""Capture: the write document and everything that stores facts, events, links and corrections."""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from datetime import datetime
from typing import Any

from .base import ACTOR_KINDS, AGENT_KINDS, AGENT_NAMES, AGENT_SOURCE, DEFAULT_HIGH_VOLUME, MEMORY_KINDS, MemoryError, VOID_EVENT, active_event_sql, content_hash, detect_agent, entity_label, new_id, normalize_instant, parse_instant, query_bound, utc_now
from .rules import split_path, validate_change_rule


class CaptureMixin:
    """Capture: the write document and everything that stores facts, events, links and corrections."""

    def resolve_entity(self, connection: sqlite3.Connection, reference: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM entities WHERE entity_id = ? OR external_key = ?", (reference, reference)
        ).fetchone()
        if row is not None:
            return row
        # A tracking code (the number people follow a case by) finds its entity.
        if reference.isalnum() and len(reference) >= 5:
            row = connection.execute(
                """SELECT entity.* FROM assertions AS code JOIN entities AS entity ON entity.entity_id = code.subject_entity_id
                    WHERE code.predicate_id = 'tracking_code' AND code.value_text = ? AND code.assertion_state = 'asserted'
                      AND code.valid_to IS NULL""",
                (reference.upper(),),
            ).fetchone()
            if row is not None:
                return row
        # A name or alias resolves only when it is unambiguous; otherwise the
        # caller must choose, never the helper.
        rows = connection.execute(
            """
            SELECT DISTINCT entity.* FROM entities AS entity
            LEFT JOIN entity_names AS alias ON alias.entity_id = entity.entity_id
             WHERE entity.canonical_name = ? COLLATE NOCASE OR alias.name = ? COLLATE NOCASE
             LIMIT 6
            """,
            (reference, reference),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        if rows:
            candidates = ", ".join(f"{r['external_key'] or r['entity_id']} ({r['entity_type']})" for r in rows)
            raise MemoryError(f"Ambiguous entity '{reference}': {candidates}")
        raise MemoryError(f"Unknown entity: {reference}. Try: memory.py search \"{reference}\"")

    def create_entity(
        self, entity_type: str, name: str, external_key: str | None = None, description: str | None = None
    ) -> dict[str, str]:
        with self.connection() as connection:
            now = utc_now()
            entity_id = new_id()
            connection.execute(
                """
                INSERT INTO entities(entity_id, entity_type, canonical_name, external_key, description, created_at, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (entity_id, entity_type, name, external_key, description, now, now),
            )
            return {"entity_id": entity_id, "external_key": external_key or "", "name": name}

    def record_event(
        self,
        event_type: str,
        summary: str,
        performed_by: str,
        participants: list[tuple[str, str]] | None = None,
        occurred_at: str | None = None,
        source_type: str = "helper_command",
    ) -> dict[str, str]:
        """Record an event atomically, requiring a person as the performer."""
        with self.connection() as connection:
            now = utc_now()
            occurred_at = normalize_instant(occurred_at, "occurred_at") if occurred_at else now
            actor = self.resolve_entity(connection, performed_by)
            if actor["entity_type"] != "person":
                raise MemoryError("performed_by must reference an entity of type person")

            evidence_id, event_id = new_id(), new_id()
            connection.execute(
                """INSERT INTO evidence(evidence_id, source_type, source_reference, content, captured_at, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (evidence_id, source_type, "memory-helper", summary, occurred_at, now),
            )
            connection.execute(
                """INSERT INTO events(event_id, event_type, summary, occurred_at, recorded_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, event_type, summary, occurred_at, now),
            )
            connection.execute(
                "INSERT INTO event_evidence(event_id, evidence_id, role, recorded_at) VALUES (?, ?, 'supports', ?)",
                (event_id, evidence_id, now),
            )
            connection.execute(
                "INSERT INTO event_participants(event_id, entity_id, role, recorded_at) VALUES (?, ?, 'performed_by', ?)",
                (event_id, actor["entity_id"], now),
            )
            for entity_reference, role in participants or []:
                participant = self.resolve_entity(connection, entity_reference)
                if participant["entity_id"] == actor["entity_id"] and role == "performed_by":
                    continue
                connection.execute(
                    "INSERT INTO event_participants(event_id, entity_id, role, recorded_at) VALUES (?, ?, ?, ?)",
                    (event_id, participant["entity_id"], role, now),
                )
            return {"event_id": event_id, "performed_by": actor["canonical_name"]}

    @staticmethod
    def _close_current(connection: sqlite3.Connection, subject_id: str, predicate_id: str, at: str) -> None:
        connection.execute(
            """
            UPDATE assertions
               SET valid_to = ?, assertion_state = 'superseded'
             WHERE subject_entity_id = ?
               AND predicate_id = ?
               AND assertion_state = 'asserted'
               AND valid_to IS NULL
            """,
            (at, subject_id, predicate_id),
        )

    @staticmethod
    def _insert_entity_assertion(
        connection: sqlite3.Connection, subject_id: str, predicate_id: str, object_id: str, at: str
    ) -> None:
        connection.execute(
            """
            INSERT INTO assertions
            (assertion_id, subject_entity_id, predicate_id, value_type, value_entity_id,
             valid_from, assertion_state, confidence, recorded_at)
            VALUES (?, ?, ?, 'entity', ?, ?, 'asserted', 1.0, ?)
            """,
            (new_id(), subject_id, predicate_id, object_id, at, at),
        )

    def record_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        summary: str,
        actor: sqlite3.Row,
        occurred_at: str,
        participants: list[tuple[str, str]],
    ) -> str:
        now, evidence_id, event_id = utc_now(), new_id(), new_id()
        connection.execute(
            "INSERT INTO evidence(evidence_id, source_type, source_reference, content, captured_at, recorded_at) VALUES (?, 'helper_command', 'memory-helper', ?, ?, ?)",
            (evidence_id, summary, occurred_at, now),
        )
        connection.execute(
            "INSERT INTO events(event_id, event_type, summary, occurred_at, recorded_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, event_type, summary, occurred_at, now),
        )
        connection.execute("INSERT INTO event_evidence(event_id, evidence_id, role, recorded_at) VALUES (?, ?, 'supports', ?)", (event_id, evidence_id, now))
        connection.execute("INSERT INTO event_participants(event_id, entity_id, role, recorded_at) VALUES (?, ?, 'performed_by', ?)", (event_id, actor["entity_id"], now))
        for entity_id, role in participants:
            connection.execute("INSERT INTO event_participants(event_id, entity_id, role, recorded_at) VALUES (?, ?, ?, ?)", (event_id, entity_id, role, now))
        return event_id

    def generate_tracking_code(self, prefix: str = "") -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        with self.connection() as connection:
            return self._new_tracking_code(connection, prefix, alphabet)

    @staticmethod
    def _new_tracking_code(connection: sqlite3.Connection, prefix: str, alphabet: str = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789") -> str:
        while True:
            code = (prefix.upper() + "".join(secrets.choice(alphabet) for _ in range(8)))[:12]
            exists = connection.execute(
                """SELECT 1 FROM assertions WHERE predicate_id = 'tracking_code' AND value_text = ?
                   AND assertion_state = 'asserted' AND valid_to IS NULL""",
                (code,),
            ).fetchone()
            if not exists:
                return code

    def assign_tracking_code(
        self, entity_reference: str, performed_by: str, code: str | None = None, prefix: str = "",
        occurred_at: str | None = None, source_text: str | None = None,
    ) -> dict[str, str]:
        """Assign a unique tracking code through write(): the fact keeps its
        evidence (the literal instruction when given) and the event who did it.
        occurred_at is when it was assigned (default now)."""
        with self.connection() as connection:
            entity = self.resolve_entity(connection, entity_reference)
            code = (code or self._new_tracking_code(connection, prefix)).upper()
        at = normalize_instant(occurred_at, "occurred_at") if occurred_at else utc_now()
        reference = entity["external_key"] or entity["entity_id"]
        summary = f"Assigned tracking code {code} to {entity['canonical_name']}."
        self.write({
            "source": {"type": "conversation" if source_text else "helper_command", "content": source_text or summary},
            "performed_by": performed_by,
            "facts": [{"subject": reference, "predicate": "tracking_code", "value": code, "valid_from": at}],
            "events": [{"type": "tracking_code_assigned", "occurred_at": at, "summary": summary,
                        "participants": [{"entity": reference, "role": "identified_entity"}]}],
        })
        with self.connection() as connection:
            stored = self._current_value(connection, entity["entity_id"], "tracking_code")
        if stored != code:
            raise MemoryError("Tracking code was not persisted")
        return {"entity_id": entity["entity_id"], "tracking_code": code}

    # ------------------------------------------------------------------
    # Generic capture: one validated document, one transaction.
    # ------------------------------------------------------------------

    def write(self, document: dict[str, Any]) -> dict[str, Any]:
        """Store any structured information without domain tables.

        The document carries its verbatim source (kept as evidence before
        anything is structured), optional new predicates, entities, facts,
        retractions and events with typed attributes and links. Everything is
        validated and applied atomically; a failure leaves memory unchanged.
        Re-sending the same document is idempotent for keyed records.
        """
        unknown = set(document) - {"source", "performed_by", "recorded_by", "changeset", "predicates", "entities", "close",
                                   "facts", "retract", "retire", "events", "observations", "transitions", "void"}
        if unknown:
            raise MemoryError(f"Unknown document sections: {', '.join(sorted(unknown))}")
        with self.connection() as connection:
            now = utc_now()
            report: dict[str, Any] = {
                "evidence": None,
                "predicates": {"created": [], "existing": []},
                "entities": {"created": [], "existing": []},
                "facts": {"created": 0, "corroborated": 0, "superseded": 0, "closed": 0},
                "retracted": 0,
                "voided": [],
                "events": {"created": [], "existing": []},
                "transitions": 0,
                "links": 0,
                "attributes": 0,
                "observations": 0,
                "warnings": [],
            }
            self._vocabulary_cache = {}
            # A changeset groups every change of a work session (an event):
            # facts asserted, closed or retracted and events written here carry it.
            changeset = document.get("changeset")
            report["_assertions"], report["_closed"], report["_retracted"] = [], [], []
            if changeset:
                in_document = {spec.get("key") for spec in document.get("events", [])}
                if changeset not in in_document and not connection.execute(
                        "SELECT 1 FROM events WHERE external_key = ? OR event_id = ?", (changeset, changeset)).fetchone():
                    raise MemoryError(f"changeset {changeset!r} must be the key of an event (the work session), "
                                      "existing or in this document")
                document = {**document, "events": [
                    {**spec, "attributes": {**spec.get("attributes", {}), "changeset": changeset}}
                    if spec.get("key") != changeset else spec for spec in document.get("events", [])]}
            # A rename is a 'name' fact: the entity keeps the name it was created with.
            renames = [{"subject": spec["key"], "predicate": "name", "value": spec["name"],
                        "valid_from": spec.get("valid_from") or "now"}
                       for spec in document.get("entities", []) if spec.get("rename") and spec.get("key")]
            if renames:
                document = {**document, "facts": [*renames, *document.get("facts", [])]}
            evidence_id = self._capture_source(connection, document.get("source"), now, report)
            recorder_ref = document.get("recorded_by") or detect_agent()
            self._recorder = self._ensure_agent(connection, recorder_ref) if recorder_ref else None
            self._source_type = document["source"]["type"]
            report["recorded_by"] = self._recorder["external_key"] if self._recorder else None
            for spec in document.get("predicates", []):
                self._declare_predicate(connection, spec, now, report)
            self._authorize_changes(connection, document, now)
            for spec in document.get("entities", []):
                self._upsert_entity(connection, spec, now, report)
            for spec in document.get("close", []):
                self._close_fact(connection, spec, evidence_id, now, report)
            for spec in document.get("facts", []):
                self._write_fact(connection, spec, evidence_id, now, report)
            self._vocabulary_cache = {}
            default_performer = document.get("performed_by")
            for spec in document.get("retract", []):
                self._retract(connection, spec, default_performer, evidence_id, now, report)
            written: list[tuple[str, dict[str, Any]]] = []
            attribute_history: dict[str, set[str] | None] = {}
            for spec in document.get("retire", []):
                self._retire(connection, spec, default_performer, changeset, evidence_id, now, report, attribute_history)
            for spec in document.get("events", []):
                event_id = self._write_event(connection, spec, default_performer, evidence_id, now, report, attribute_history)
                if event_id:
                    written.append((event_id, spec))
            # Links run after every event exists, so events in one document
            # may reference each other by key.
            for event_id, spec in written:
                for link in spec.get("links", []):
                    self._write_link(connection, event_id, link, evidence_id, now, report)
            for spec in document.get("observations", []):
                self._write_observation(connection, spec, now, report)
            for spec in document.get("void", []):
                self._void(connection, spec, default_performer, evidence_id, now, report, attribute_history)
            # Transitions run last and in document order, validated one by one.
            for spec in document.get("transitions", []):
                result = self._apply_transition(
                    connection,
                    self.resolve_entity(connection, spec["instance"]),
                    self.resolve_entity(connection, spec["to_state"]),
                    self._performer(connection, spec.get("performed_by") or default_performer),
                    spec.get("pending_for"),
                    normalize_instant(spec["occurred_at"], "occurred_at"),
                )
                connection.execute(
                    "INSERT INTO event_evidence(event_id, evidence_id, role, recorded_at) VALUES (?, ?, 'source', ?)",
                    (result["event_id"], evidence_id, now),
                )
                report["transitions"] += 1
            if changeset:
                for marker, ids in (("changeset", report["_assertions"]), ("closed_in_changeset", report["_closed"]),
                                    ("retracted_in_changeset", report["_retracted"])):
                    for assertion_id in ids:
                        self._write_attributes(connection, "assertion", assertion_id, {marker: changeset}, evidence_id,
                                               now, report)
                report["changeset"] = {"key": changeset, "asserted": len(report["_assertions"]),
                                       "closed": len(report["_closed"]), "retracted": len(report["_retracted"])}
            for internal in ("_assertions", "_closed", "_retracted"):
                report.pop(internal, None)
            touched = {spec.get("predicate") for spec in [*document.get("facts", []), *document.get("close", [])]}
            if touched & ({"produced_by"} | self._requirement_predicates(connection)):
                for blocked in self.unstartable_types(connection):
                    report["warnings"].append(f"Nobody could start {blocked['type']}: every producer of "
                                              f"{', '.join(blocked['blocked_by'])} is itself blocked")
            return report

    def _requirement_predicates(self, connection: sqlite3.Connection) -> set[str]:
        """Predicates that hold requirement lists read by some process (…>P)."""
        predicates = set()
        for process in connection.execute("SELECT entity_id FROM entities WHERE entity_type = 'process'").fetchall():
            versions = self._process_versions(connection, process["entity_id"])
            for transition in (versions[-1]["definition"]["transitions"] if versions else []):
                predicates |= {split_path(rule["of"])[-1] for rule in transition.get("requires_each", [])}
        return predicates

    def _capture_source(
        self, connection: sqlite3.Connection, source: dict[str, Any] | None, now: str, report: dict[str, Any]
    ) -> str:
        if not source or not source.get("type") or not (source.get("content") or source.get("reference")):
            raise MemoryError("source needs a type and its verbatim content (or a reference to it)")
        body = source.get("content") or source["reference"]
        digest = content_hash(body)
        existing = connection.execute(
            "SELECT evidence_id FROM evidence WHERE source_type = ? AND content_hash = ?", (source["type"], digest)
        ).fetchone()
        if existing:
            report["evidence"] = {"evidence_id": existing["evidence_id"], "created": False}
            return existing["evidence_id"]
        provider = self.resolve_entity(connection, source["provided_by"])["entity_id"] if source.get("provided_by") else None
        captured_at = normalize_instant(source["captured_at"], "source.captured_at") if source.get("captured_at") else now
        evidence_id = new_id()
        connection.execute(
            """INSERT INTO evidence(evidence_id, source_type, source_reference, content, captured_at, recorded_at,
                                    captured_by_entity_id, content_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (evidence_id, source["type"], source.get("reference"), source.get("content"), captured_at, now, provider, digest),
        )
        report["evidence"] = {"evidence_id": evidence_id, "created": True}
        return evidence_id

    def _declare_predicate(self, connection: sqlite3.Connection, spec: dict[str, Any], now: str, report: dict[str, Any]) -> None:
        predicate_id = spec.get("id")
        if not predicate_id or not spec.get("value_type"):
            raise MemoryError("predicates need id and value_type")
        wanted = {
            "value_type": spec["value_type"],
            "cardinality": spec.get("cardinality", "many"),
            "is_temporal": int(spec.get("is_temporal", True)),
            "unit": spec.get("unit"),
        }
        existing = connection.execute("SELECT * FROM predicates WHERE predicate_id = ?", (predicate_id,)).fetchone()
        if existing:
            differences = [key for key, value in wanted.items() if existing[key] != value]
            if differences:
                raise MemoryError(f"Predicate {predicate_id} already exists with different {', '.join(differences)}")
            report["predicates"]["existing"].append(predicate_id)
            return
        similar = self._similar_predicates(connection, predicate_id, wanted["value_type"])
        if similar and not spec.get("distinct_from_similar"):
            raise MemoryError(
                f"Predicate {predicate_id} looks like existing {', '.join(similar)}. "
                "Reuse one of them, or set distinct_from_similar: true if it really means something else."
            )
        connection.execute(
            """INSERT INTO predicates(predicate_id, label, value_type, cardinality, is_temporal, description, created_at, unit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (predicate_id, spec.get("label", predicate_id), wanted["value_type"], wanted["cardinality"],
             wanted["is_temporal"], spec.get("description"), now, wanted["unit"]),
        )
        report["predicates"]["created"].append(predicate_id)

    @staticmethod
    def _similar_predicates(connection: sqlite3.Connection, predicate_id: str, value_type: str | None = None) -> list[str]:
        """Existing predicates that probably mean the same as predicate_id.

        Flagged: a misspelled word (quantiy / quantity, solves / resolves),
        and one name containing the other (units / units_received) when both
        hold the same kind of value. Not flagged: names that only share a
        word but hold different kinds of value (draft_of, an entity, and
        draft_number, an integer), or that relate in another direction
        (draft_of / in_draft, dispatched_to / dispatched_from)."""
        import difflib

        existing = {row[0]: row[1] for row in connection.execute("SELECT predicate_id, value_type FROM predicates")}
        glue = {"is", "has", "es", "la", "el"}
        relations = {"of", "in", "to", "from", "by", "for", "on", "at", "with", "into",
                     "de", "del", "en", "para", "por", "con", "a"}
        tokens = set(predicate_id.split("_")) - glue

        def nested(other: str) -> bool:
            other_tokens = set(other.split("_")) - glue
            if not (tokens and other_tokens) or not (tokens <= other_tokens or other_tokens <= tokens):
                return False
            if value_type and existing[other] not in (value_type, "any") and value_type != "any":
                return False
            # Different relation words on both sides mean different directions.
            mine, theirs = tokens & relations, other_tokens & relations
            if mine and theirs and mine != theirs:
                return False
            # A name made only of relation words around a shared noun is not a measure variant.
            return bool((tokens | other_tokens) - relations)

        def typo(other: str) -> bool:
            # Only a misspelled word counts: 'quantiy' vs 'quantity', never 'to' vs 'from'.
            mine, theirs = set(predicate_id.split("_")), set(other.split("_"))
            only_mine, only_theirs = mine - theirs, theirs - mine
            return (len(only_mine) == len(only_theirs) == 1
                    and difflib.SequenceMatcher(None, *only_mine, *only_theirs).ratio() >= 0.8)

        by_token = [other for other in existing if other != predicate_id and nested(other)]
        by_spelling = [other for other in existing if other != predicate_id and typo(other)]
        return sorted(set(by_token + by_spelling))

    def _predicate(self, connection: sqlite3.Connection, predicate_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM predicates WHERE predicate_id = ?", (predicate_id,)).fetchone()
        if row is None:
            similar = self._similar_predicates(connection, predicate_id)
            hint = f" Similar: {', '.join(similar)}." if similar else ""
            raise MemoryError(f"Unknown predicate {predicate_id}; declare it in 'predicates' first.{hint}")
        return row

    def _typed_value(self, connection: sqlite3.Connection, predicate: sqlite3.Row, value: Any) -> tuple[str, dict[str, Any]]:
        """Convert a JSON value to (value_type, column values) as the predicate dictates."""
        value_type = predicate["value_type"]
        if value_type == "any":
            if isinstance(value, bool):
                value_type = "boolean"
            elif isinstance(value, int):
                value_type = "integer"
            elif isinstance(value, float):
                value_type = "real"
            elif isinstance(value, (dict, list)):
                value_type = "json"
            else:
                value_type = "text"
        name = predicate["predicate_id"]
        if value is None:
            raise MemoryError(f"{name} has no value")
        vocabulary = self._vocabulary(connection, name)
        if value_type == "integer":
            if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, float) and value.is_integer())):
                raise MemoryError(f"{name} expects an integer, got {value!r}")
            return value_type, {"value_integer": int(value)}
        if value_type == "real":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MemoryError(f"{name} expects a number, got {value!r}")
            return value_type, {"value_real": float(value)}
        if value_type == "boolean":
            if not isinstance(value, bool):
                raise MemoryError(f"{name} expects true or false, got {value!r}")
            return value_type, {"value_boolean": int(value)}
        if value_type == "datetime":
            return value_type, {"value_datetime": normalize_instant(str(value), name)}
        if value_type == "json":
            return value_type, {"value_json": json.dumps(value, ensure_ascii=False, sort_keys=True)}
        if value_type == "entity":
            target = self.resolve_entity(connection, str(value))
            expected = vocabulary.get("entity_type")
            if expected and target["entity_type"] != expected:
                raise MemoryError(f"{name} points to {expected} entities; {value!r} is a {target['entity_type']} "
                                  f"(vocabulary predicate:{name})")
            return value_type, {"value_entity_id": target["entity_id"]}
        if not isinstance(value, str):
            raise MemoryError(f"{name} expects text, got {value!r}")
        allowed = vocabulary.get("values")
        if allowed and value not in allowed:
            raise MemoryError(f"{name} only accepts {', '.join(repr(item) for item in sorted(allowed))}; got {value!r} "
                              f"(vocabulary predicate:{name}, changed with define-values)")
        return value_type, {"value_text": value}

    def _vocabulary(self, connection: sqlite3.Connection, predicate_id: str) -> dict[str, Any]:
        """The allowed values (text) or entity type (entity) of a predicate, if
        declared: facts on the entity predicate:<id>, so they have history,
        evidence and change rules like any other data. Cached per write."""
        cache = getattr(self, "_vocabulary_cache", None)
        if cache is None:
            cache = self._vocabulary_cache = {}
        if predicate_id not in cache:
            rows = connection.execute(
                """SELECT fact.predicate_id, fact.value_text FROM entities AS profile
                     JOIN assertions AS fact ON fact.subject_entity_id = profile.entity_id
                    WHERE profile.external_key = ? AND fact.predicate_id IN ('allowed_value', 'value_entity_type')
                      AND fact.assertion_state = 'asserted' AND fact.valid_to IS NULL""",
                (f"predicate:{predicate_id}",),
            ).fetchall()
            cache[predicate_id] = {
                "values": {row["value_text"] for row in rows if row["predicate_id"] == "allowed_value"},
                "entity_type": next((row["value_text"] for row in rows if row["predicate_id"] == "value_entity_type"), None),
            }
        return cache[predicate_id]

    def define_values(self, predicate_id: str, values: list[str] | None, performed_by: str,
                      entity_type: str | None = None, source_text: str | None = None) -> dict[str, Any]:
        """Declare the vocabulary of a predicate: the only text values it accepts
        (values) or the type of entity it points to (entity_type). Values left
        out are closed, not deleted, so earlier data keeps its meaning."""
        with self.connection() as connection:
            predicate = self._predicate(connection, predicate_id)
            if values and predicate["value_type"] not in ("text", "any"):
                raise MemoryError(f"{predicate_id} holds {predicate['value_type']} values; allowed values are for text")
            if entity_type and predicate["value_type"] != "entity":
                raise MemoryError(f"{predicate_id} holds {predicate['value_type']} values; entity_type is for entity predicates")
            current = self._vocabulary(connection, predicate_id)
            self._vocabulary_cache = None
            latest = connection.execute(
                """SELECT MAX(fact.valid_from) FROM entities AS profile JOIN assertions AS fact
                    ON fact.subject_entity_id = profile.entity_id WHERE profile.external_key = ?""",
                (f"predicate:{predicate_id}",),
            ).fetchone()[0]
        # Validity is compared per second: a change in the same second as the
        # previous one waits for the next second instead of colliding.
        while latest and utc_now()[:19] <= latest[:19]:
            time.sleep(0.05)
        key, now = f"predicate:{predicate_id}", utc_now()
        wanted = set(values or [])
        document = {
            "source": {"type": "conversation" if source_text else "vocabulary",
                       "content": source_text or f"Vocabulary of {predicate_id}: {sorted(wanted) or entity_type}"},
            "performed_by": performed_by,
            "entities": [{"key": key, "type": "predicate_profile", "name": f"Vocabulary of {predicate_id}",
                          "description": f"Allowed values or entity type of the predicate {predicate_id}."}],
            "close": [{"subject": key, "predicate": "allowed_value", "value": value, "at": now}
                      for value in sorted(current["values"] - wanted)] if values is not None else [],
            "facts": [{"subject": key, "predicate": "allowed_value", "value": value, "valid_from": now}
                      for value in sorted(wanted - current["values"])],
        }
        if entity_type and entity_type != current["entity_type"]:
            document["facts"].append({"subject": key, "predicate": "value_entity_type", "value": entity_type, "valid_from": now})
        report = self.write(document)
        with self.connection() as connection:
            self._vocabulary_cache = None
            return {"predicate": predicate_id, **{k: sorted(v) if isinstance(v, set) else v
                                                  for k, v in self._vocabulary(connection, predicate_id).items()},
                    "warnings": report["warnings"]}

    def _high_volume_types(self, connection: sqlite3.Connection, at: str | None = None) -> dict[str, bool]:
        """Event types declared high-volume (True) or normal (False), now or at
        an instant: the high_volume fact of each event_type:<type> profile,
        over the defaults (DEFAULT_HIGH_VOLUME) that no person has changed."""
        if at is None:
            valid = "fact.assertion_state = 'asserted' AND fact.valid_to IS NULL"
        else:
            valid = ("fact.assertion_state <> 'retracted' AND julianday(fact.valid_from) <= julianday(:at) "
                     "AND (fact.valid_to IS NULL OR julianday(fact.valid_to) > julianday(:at))")
        declared = {row["key"][len("event_type:"):]: bool(row["value_boolean"]) for row in connection.execute(
            f"""SELECT profile.external_key AS key, fact.value_boolean FROM entities AS profile
                  JOIN assertions AS fact ON fact.subject_entity_id = profile.entity_id
                 WHERE profile.entity_type = 'event_type_profile' AND fact.predicate_id = 'high_volume' AND {valid}""",
            {"at": at})}
        return {**{kind: True for kind in DEFAULT_HIGH_VOLUME}, **declared}

    def high_volume_types(self, as_of: str | None = None) -> dict[str, bool]:
        """Every event type with a declared (or default) treatment, now or as of an instant."""
        with self.connection() as connection:
            return self._high_volume_types(connection, query_bound(as_of, self.zone, "as_of") if as_of else None)

    def define_event_type(self, event_type: str, high_volume: bool, performed_by: str, reason: str | None = None,
                          source_text: str | None = None) -> dict[str, Any]:
        """Declare how an event type is treated: high-volume types (imported
        movements, readings, transitions) stay out of the brief's recent list,
        which shows a count per type instead. A versioned fact on the profile
        entity event_type:<type>; declaring it normal again closes nothing
        away: the earlier value stays in the history (--as-of)."""
        if not event_type or not event_type.replace("_", "").isalnum():
            raise MemoryError(f"Event types are words joined by underscores, not {event_type!r}")
        key = f"event_type:{event_type}"
        with self.connection() as connection:
            self._performer(connection, performed_by, person_only="Declaring how an event type is treated")
            current = self._high_volume_types(connection).get(event_type, False)
            latest = connection.execute(
                """SELECT MAX(fact.valid_from) FROM entities AS profile JOIN assertions AS fact
                    ON fact.subject_entity_id = profile.entity_id WHERE profile.external_key = ?""", (key,)).fetchone()[0]
        if current == high_volume and latest is not None:
            return {"event_type": event_type, "high_volume": high_volume, "changed": False}
        # Validity is compared per second, as in define_values.
        while latest and utc_now()[:19] <= latest[:19]:
            time.sleep(0.05)
        now = utc_now()
        label = "a high-volume event type" if high_volume else "a normal event type"
        summary = f"Declared {event_type} {label}" + (f": {reason}" if reason else ".")
        self.write({
            "source": {"type": "conversation" if source_text else "helper_command", "content": source_text or summary},
            "performed_by": performed_by,
            "entities": [{"key": key, "type": "event_type_profile", "name": f"Event type {event_type}",
                          "description": f"How events of type {event_type} are treated by the memory."}],
            "facts": [{"subject": key, "predicate": "high_volume", "value": high_volume, "valid_from": now}],
            "events": [{"type": "event_type_declared", "occurred_at": now, "summary": summary,
                        "attributes": {"rationale": reason} if reason else {},
                        "participants": [{"entity": key, "role": "subject"}]}],
        })
        with self.connection() as connection:
            stored = self._high_volume_types(connection).get(event_type, False)
        if stored != high_volume:
            raise MemoryError("The event type declaration was not persisted")
        return {"event_type": event_type, "high_volume": stored, "changed": True}

    def _upsert_entity(self, connection: sqlite3.Connection, spec: dict[str, Any], now: str, report: dict[str, Any]) -> None:
        key, entity_type, name = spec.get("key"), spec.get("type"), spec.get("name")
        if not (key and entity_type and name):
            raise MemoryError("entities need key, type and name")
        existing = connection.execute("SELECT * FROM entities WHERE external_key = ?", (key,)).fetchone()
        if existing is None:
            namesakes = [row[0] for row in connection.execute(
                "SELECT external_key FROM entities WHERE entity_type = ? AND canonical_name = ? COLLATE NOCASE LIMIT 5",
                (entity_type, name),
            )]
            if namesakes:
                report["warnings"].append(
                    f"{key} has the same name as {', '.join(namesakes)} ({entity_type}): if it is the same, reuse that key; "
                    "if not, record something that tells them apart (an identifier fact)")
            entity_id = new_id()
            connection.execute(
                """INSERT INTO entities(entity_id, entity_type, canonical_name, external_key, description, created_at, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (entity_id, entity_type, name, key, spec.get("description"), now, now),
            )
            report["entities"]["created"].append(key)
        else:
            entity_id = existing["entity_id"]
            if existing["entity_type"] != entity_type:
                raise MemoryError(f"{key} is a {existing['entity_type']}, not a {entity_type}")
            report["entities"]["existing"].append(key)
            current_name = connection.execute(f"SELECT {entity_label('entity')} FROM entities AS entity WHERE entity_id = ?",
                                              (entity_id,)).fetchone()[0]
            if name != current_name and not spec.get("rename"):
                # Keep both names: the new one becomes an alias, nothing is overwritten.
                spec = {**spec, "aliases": [*spec.get("aliases", []), name]}
                report["warnings"].append(f"{key} is named '{current_name}'; '{name}' kept as alias "
                                          "(to rename it, send the entity with \"rename\": true)")
            description = spec.get("description")
            if description and existing["description"] is None:
                connection.execute("UPDATE entities SET description = ? WHERE entity_id = ?", (description, entity_id))
            elif description and description != existing["description"]:
                report["warnings"].append(f"{key} keeps its description; the new one differs: {description!r}")
        for alias in spec.get("aliases", []):
            present = connection.execute(
                "SELECT 1 FROM entity_names WHERE entity_id = ? AND name = ? COLLATE NOCASE", (entity_id, alias)
            ).fetchone()
            if not present:
                connection.execute(
                    "INSERT INTO entity_names(entity_name_id, entity_id, name, name_kind, recorded_at) VALUES (?, ?, ?, 'alias', ?)",
                    (new_id(), entity_id, alias, now),
                )

    def _write_fact(
        self, connection: sqlite3.Connection, spec: dict[str, Any], evidence_id: str, now: str, report: dict[str, Any]
    ) -> None:
        subject = self.resolve_entity(connection, spec["subject"])
        predicate = self._predicate(connection, spec["predicate"])
        value_type, columns = self._typed_value(connection, predicate, spec.get("value"))
        valid_from = normalize_instant(spec["valid_from"], "valid_from") if spec.get("valid_from") else None
        if subject["retired_at"]:
            report["warnings"].append(f"{spec['subject']} is retired; {predicate['predicate_id']} was recorded anyway")
        if predicate["predicate_id"] == "name" and not connection.execute(
                "SELECT 1 FROM entity_names WHERE entity_id = ? AND name = ? COLLATE NOCASE",
                (subject["entity_id"], spec["value"])).fetchone():
            # The new name finds the entity like any alias (search, references).
            connection.execute(
                "INSERT INTO entity_names(entity_name_id, entity_id, name, name_kind, valid_from, recorded_at) "
                "VALUES (?, ?, ?, 'renamed', ?, ?)", (new_id(), subject["entity_id"], spec["value"], valid_from, now))
        valid_to = normalize_instant(spec["valid_to"], "valid_to") if spec.get("valid_to") else None
        column, stored = next(iter(columns.items()))
        same = connection.execute(
            f"""SELECT assertion_id FROM assertions
                 WHERE subject_entity_id = ? AND predicate_id = ? AND assertion_state = 'asserted'
                   AND {column} = ? AND valid_to IS ?""",
            (subject["entity_id"], predicate["predicate_id"], stored, valid_to),
        ).fetchone()
        if same:
            # The same fact from another source adds support instead of a duplicate.
            connection.execute(
                "INSERT OR IGNORE INTO assertion_evidence(assertion_id, evidence_id, role, recorded_at) VALUES (?, ?, 'supports', ?)",
                (same["assertion_id"], evidence_id, now),
            )
            report["facts"]["corroborated"] += 1
            return
        if predicate["cardinality"] == "one":
            current = connection.execute(
                """SELECT assertion_id, valid_from FROM assertions
                    WHERE subject_entity_id = ? AND predicate_id = ? AND assertion_state = 'asserted' AND valid_to IS NULL""",
                (subject["entity_id"], predicate["predicate_id"]),
            ).fetchone()
            if current and predicate["is_temporal"]:
                if valid_from is None:
                    raise MemoryError(
                        f"{spec['subject']} already has a current {predicate['predicate_id']}; a change needs valid_from "
                        "(when the new value started). If the old value was wrong, retract it instead."
                    )
                if current["valid_from"] and parse_instant(valid_from) <= parse_instant(current["valid_from"]):
                    raise MemoryError(f"valid_from {valid_from} is not after the current value's {current['valid_from']}")
                if current["valid_from"] and valid_from[:19] == current["valid_from"][:19]:
                    # The schema compares validity at one-second resolution.
                    raise MemoryError(f"valid_from {valid_from} must be at least one second after {current['valid_from']}")
                report.setdefault("_closed", []).extend(row[0] for row in connection.execute(
                    """SELECT assertion_id FROM assertions WHERE subject_entity_id = ? AND predicate_id = ?
                        AND assertion_state = 'asserted' AND valid_to IS NULL""",
                    (subject["entity_id"], predicate["predicate_id"])))
                self._close_current(connection, subject["entity_id"], predicate["predicate_id"], valid_from)
                report["facts"]["superseded"] += 1
            elif current:
                report["warnings"].append(
                    f"Conflict kept: {spec['subject']} {predicate['predicate_id']} now has two values with their own evidence"
                )
        assertion_id = new_id()
        recorder = getattr(self, "_recorder", None)
        connection.execute(
            f"""INSERT INTO assertions(assertion_id, subject_entity_id, predicate_id, value_type, {column},
                                       valid_from, valid_to, assertion_state, confidence, recorded_at,
                                       recorded_by_entity_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'asserted', ?, ?, ?)""",
            (assertion_id, subject["entity_id"], predicate["predicate_id"], value_type, stored,
             valid_from, valid_to, float(spec.get("confidence", 1.0)), now,
             recorder["entity_id"] if recorder else None),
        )
        connection.execute(
            "INSERT INTO assertion_evidence(assertion_id, evidence_id, role, recorded_at) VALUES (?, ?, 'supports', ?)",
            (assertion_id, evidence_id, now),
        )
        self._write_attributes(connection, "assertion", assertion_id, spec.get("attributes", {}), evidence_id, now, report)
        report["facts"]["created"] += 1
        report.setdefault("_assertions", []).append(assertion_id)

    def _write_observation(self, connection: sqlite3.Connection, spec: dict[str, Any], now: str,
                           report: dict[str, Any]) -> None:
        """A reading (temperature, position, level…) of a subject at an instant,
        with the specific actor that produced it (which sensor, which system)."""
        missing = [field for field in ("subject", "predicate", "value", "occurred_at", "by") if spec.get(field) is None]
        if missing:
            raise MemoryError(f"observations need {', '.join(missing)} (by: the actor that produced the reading)")
        subject = self.resolve_entity(connection, spec["subject"])
        predicate = self._predicate(connection, spec["predicate"])
        source = self._performer(connection, spec["by"])
        value_type, columns = self._typed_value(connection, predicate, spec["value"])
        column, stored = next(iter(columns.items()))
        connection.execute(
            f"""INSERT INTO observations(observation_id, subject_entity_id, predicate_id, value_type, {column},
                                         occurred_at, recorded_at, quality, source_entity_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_id(), subject["entity_id"], predicate["predicate_id"], value_type, stored,
             normalize_instant(spec["occurred_at"], "occurred_at"), now, spec.get("quality", "observed"),
             source["entity_id"]),
        )
        report["observations"] += 1

    def _change_rules(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        return [{"key": row["external_key"], **json.loads(row["value_json"])} for row in connection.execute(
            """SELECT entity.external_key, rule.value_json FROM entities AS entity
                 JOIN assertions AS rule ON rule.subject_entity_id = entity.entity_id
                WHERE entity.entity_type = 'change_rule' AND rule.predicate_id = 'change_rule_definition'
                  AND rule.assertion_state = 'asserted' AND rule.valid_to IS NULL"""
        )]

    def _authorize_changes(self, connection: sqlite3.Connection, document: dict[str, Any], now: str) -> None:
        """Reject a document that changes governed data without the authority.

        Change rules are data (define-change-rule): a manager may adjust one
        case, while only the owner's role may change what defines every case."""
        rules = self._change_rules(connection)
        if not rules:
            return
        new_types = {spec.get("key"): spec.get("type") for spec in document.get("entities", []) if spec.get("key")}
        changes = [(spec, spec.get("valid_from"), "fact") for spec in document.get("facts", [])]
        changes += [(spec, spec.get("at"), "close") for spec in document.get("close", [])]
        changes += [(spec, None, "retract") for spec in document.get("retract", [])]
        changes += [({"subject": spec.get("entity"), "predicate": None, "performed_by": spec.get("performed_by")},
                     spec.get("at"), "retire") for spec in document.get("retire", [])]
        checks = []
        for spec, when, action in changes:
            existing = connection.execute("SELECT entity_type FROM entities WHERE external_key = ? OR entity_id = ?",
                                          (spec.get("subject"), spec.get("subject"))).fetchone()
            subject_type = existing["entity_type"] if existing else new_types.get(spec.get("subject"))
            for rule in rules:
                predicates = rule.get("predicates", "*")
                if rule["subject_type"] == subject_type and (predicates == "*" or spec.get("predicate") in predicates):
                    what = (f"retire {spec.get('subject')}" if action == "retire"
                            else f"{action} {spec.get('predicate')} of {spec.get('subject')}")
                    checks.append((rule, spec.get("performed_by"), when, what))
        for spec in document.get("entities", []):
            exists = connection.execute("SELECT 1 FROM entities WHERE external_key = ?", (spec.get("key"),)).fetchone()
            for rule in rules:
                if rule.get("create") and rule["subject_type"] == spec.get("type") and not exists:
                    checks.append((rule, None, None, f"create {spec.get('type')} {spec.get('key')}"))
        for rule, performer, when, what in checks:
            reference = performer or document.get("performed_by")
            if not reference:
                raise MemoryError(f"{what} is governed by {rule['key']}: say who orders it (performed_by)")
            actor = self._performer(connection, reference)
            at = normalize_instant(when, "valid_from") if when else now
            roles = self._roles_at(connection, actor["entity_id"], at)
            if not roles & set(rule["roles"]):
                raise MemoryError(
                    f"{actor['canonical_name']} (roles: {', '.join(sorted(roles)) or 'none'}) may not {what}: "
                    f"{rule['key']} reserves it to {', '.join(rule['roles'])}. Nothing was written."
                )

    def define_change_rule(self, key: str, name: str, definition: dict[str, Any], performed_by: str,
                           source_text: str | None = None) -> dict[str, Any]:
        """Declare (or version) who may change a kind of data. A rule governs
        itself: changing an existing rule needs one of its own roles."""
        validate_change_rule(definition)
        if not key.startswith("change:"):
            raise MemoryError("Change rule keys look like change:<name>")
        with self.connection() as connection:
            actor = self._performer(connection, performed_by, person_only="A change rule")
            for role in definition["roles"]:
                row = connection.execute("SELECT entity_type FROM entities WHERE external_key = ?", (role,)).fetchone()
                if row is None or row["entity_type"] != "role":
                    raise MemoryError(f"{role} is not a known role entity")
            current = next((rule for rule in self._change_rules(connection) if rule["key"] == key), None)
            if current and not self._roles_at(connection, actor["entity_id"], utc_now()) & set(current["roles"]):
                raise MemoryError(f"Only {', '.join(current['roles'])} may change {key}")
            last = connection.execute(
                """SELECT rule.valid_from FROM assertions AS rule JOIN entities ON entities.entity_id = rule.subject_entity_id
                    WHERE entities.external_key = ? AND rule.predicate_id = 'change_rule_definition'
                      AND rule.assertion_state = 'asserted' AND rule.valid_to IS NULL""",
                (key,),
            ).fetchone()
        while last and last["valid_from"] and utc_now()[:19] <= last["valid_from"][:19]:
            time.sleep(0.05)
        report = self.write({
            "source": {"type": "conversation" if source_text else "change_rule",
                       "content": source_text or json.dumps({"key": key, "definition": definition}, ensure_ascii=False)},
            "performed_by": performed_by,
            "entities": [{"key": key, "type": "change_rule", "name": name, "description": definition.get("description")}],
            "facts": [{"subject": key, "predicate": "change_rule_definition", "value": definition, "valid_from": utc_now()}],
        })
        return {"key": key, "facts": report["facts"], "warnings": report["warnings"]}

    def _close_fact(
        self, connection: sqlite3.Connection, spec: dict[str, Any], evidence_id: str, now: str, report: dict[str, Any]
    ) -> None:
        """End a current fact at an instant: it was true until then (a role
        that ended, an action no longer pending). Unlike retract, it was not wrong."""
        subject = self.resolve_entity(connection, spec["subject"])
        predicate = self._predicate(connection, spec["predicate"])
        at = normalize_instant(spec["at"], "close.at") if spec.get("at") else None
        if not at:
            raise MemoryError("close needs at: when the fact stopped being true")
        query = """SELECT assertion_id, valid_from FROM assertions WHERE subject_entity_id = ? AND predicate_id = ?
                   AND assertion_state = 'asserted' AND valid_to IS NULL"""
        parameters: list[Any] = [subject["entity_id"], predicate["predicate_id"]]
        if "value" in spec:
            _, columns = self._typed_value(connection, predicate, spec["value"])
            column, stored = next(iter(columns.items()))
            query += f" AND {column} = ?"
            parameters.append(stored)
        rows = connection.execute(query, parameters).fetchall()
        if not rows and not spec.get("if_present"):
            raise MemoryError(f"Nothing current to close for {spec['subject']} {predicate['predicate_id']}")
        for row in rows:
            if row["valid_from"] and at[:19] <= row["valid_from"][:19]:
                raise MemoryError(f"close at {at} is not after {spec['subject']} {predicate['predicate_id']} began")
            connection.execute("UPDATE assertions SET valid_to = ? WHERE assertion_id = ?", (at, row["assertion_id"]))
            report.setdefault("_closed", []).append(row["assertion_id"])
            connection.execute(
                "INSERT OR IGNORE INTO assertion_evidence(assertion_id, evidence_id, role, recorded_at) VALUES (?, ?, 'supports', ?)",
                (row["assertion_id"], evidence_id, now),
            )
            report["facts"]["closed"] += 1

    def _retract(
        self, connection: sqlite3.Connection, spec: dict[str, Any], default_performer: str | None,
        evidence_id: str, now: str, report: dict[str, Any],
    ) -> None:
        subject = self.resolve_entity(connection, spec["subject"])
        predicate = self._predicate(connection, spec["predicate"])
        actor = self._performer(connection, spec.get("performed_by") or default_performer, person_only="A retraction")
        if not spec.get("reason"):
            raise MemoryError("A retraction needs a reason")
        query = """SELECT assertion_id FROM assertions WHERE subject_entity_id = ? AND predicate_id = ?
                   AND assertion_state = 'asserted' AND valid_to IS NULL"""
        parameters: list[Any] = [subject["entity_id"], predicate["predicate_id"]]
        if "value" in spec:
            _, columns = self._typed_value(connection, predicate, spec["value"])
            column, stored = next(iter(columns.items()))
            query += f" AND {column} = ?"
            parameters.append(stored)
        rows = connection.execute(query, parameters).fetchall()
        if not rows:
            raise MemoryError(f"Nothing current to retract for {spec['subject']} {predicate['predicate_id']}")
        for row in rows:
            connection.execute("UPDATE assertions SET assertion_state = 'retracted' WHERE assertion_id = ?", (row["assertion_id"],))
            report.setdefault("_retracted", []).append(row["assertion_id"])
            connection.execute(
                "INSERT INTO assertion_evidence(assertion_id, evidence_id, role, recorded_at) VALUES (?, ?, 'refutes', ?)",
                (row["assertion_id"], evidence_id, now),
            )
        event_id = self.record_event_in_transaction(
            connection, "assertion_retracted",
            f"Retracted {predicate['predicate_id']} of {subject['canonical_name']}: {spec['reason']}",
            actor, now, [(subject["entity_id"], "subject")],
        )
        connection.execute(
            "INSERT INTO event_evidence(event_id, evidence_id, role, recorded_at) VALUES (?, ?, 'source', ?)",
            (event_id, evidence_id, now),
        )
        report["retracted"] += len(rows)

    @staticmethod
    def _ensure_agent(connection: sqlite3.Connection, reference: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM entities WHERE external_key = ? OR entity_id = ?", (reference, reference)
        ).fetchone()
        if row is not None:
            if row["entity_type"] != "agent":
                raise MemoryError(f"{reference} is a {row['entity_type']}, not an agent")
            return row
        if not reference.startswith("agent:"):
            raise MemoryError("Agent keys look like agent:<name>, for example agent:claude-code")
        now = utc_now()
        entity_id = new_id()
        connection.execute(
            """INSERT INTO entities(entity_id, entity_type, canonical_name, external_key, description, created_at, recorded_at)
               VALUES (?, 'agent', ?, ?, 'Agent that records and observes; it does not decide for people.', ?, ?)""",
            (entity_id, AGENT_NAMES.get(reference, reference.split(":", 1)[1]), reference, now, now),
        )
        return connection.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()

    def _void(
        self, connection: sqlite3.Connection, spec: dict[str, Any], default_performer: str | None,
        evidence_id: str, now: str, report: dict[str, Any], attribute_history: dict[str, set[str] | None],
    ) -> None:
        """Void an event without touching it: a person records why it no longer counts."""
        reference, reason = spec.get("event"), spec.get("reason")
        if not reference or not reason:
            raise MemoryError("A void needs the event (key or id) and a reason")
        target = connection.execute(
            "SELECT * FROM events WHERE external_key = ? OR event_id = ?", (reference, reference)
        ).fetchone()
        if target is None:
            raise MemoryError(f"Unknown event {reference}")
        actor = self._performer(connection, spec.get("performed_by") or default_performer, person_only="Voiding an event")
        if target["event_type"] == "workflow_transition":
            raise MemoryError("A workflow transition is not voided; record a compensating transition instead")
        if target["event_type"] == VOID_EVENT:
            chained = connection.execute(
                f"""SELECT 1 FROM record_links JOIN events ON events.event_id = record_links.target_record_id
                     WHERE record_links.source_record_id = ? AND record_links.predicate_id = 'voids'
                       AND events.event_type = '{VOID_EVENT}'""",
                (target["event_id"],),
            ).fetchone()
            if chained:
                raise MemoryError("Only one undo is supported: this void already restores another void")
        in_force = connection.execute(
            f"SELECT 1 FROM events AS target WHERE target.event_id = ? AND {active_event_sql('target')}",
            (target["event_id"],),
        ).fetchone()
        label = target["external_key"] or target["event_id"]
        if in_force is None:
            report["warnings"].append(f"{label} was already voided")
            return
        void_id = self._write_event(
            connection,
            {"type": VOID_EVENT, "occurred_at": spec.get("occurred_at") or now,
             "summary": f"Voided {label} ({target['event_type']}): {reason}",
             "performed_by": actor["external_key"] or actor["entity_id"], "attributes": {"rationale": reason}},
            None, evidence_id, now, report, attribute_history,
        )
        self._write_link(connection, void_id, {"predicate": "voids", "event": target["event_id"]}, evidence_id, now, report)
        # Nothing cascades: events that depend on the voided one are reported.
        dependents = connection.execute(
            f"""SELECT link.predicate_id, COUNT(*) AS count FROM record_links AS link
                  JOIN events AS source ON source.event_id = link.source_record_id
                 WHERE link.target_record_type = 'event' AND link.target_record_id = ?
                   AND link.predicate_id <> 'voids' AND {active_event_sql('source')}
                 GROUP BY link.predicate_id""",
            (target["event_id"],),
        ).fetchall()
        for row in dependents:
            report["warnings"].append(
                f"{row['count']} active event(s) link to {label} through {row['predicate_id']}; void them too if they depended on it"
            )
        report["voided"].append(label)

    def _actor_kinds(self, connection: sqlite3.Connection) -> set[str]:
        """Entity types that can act: the standard kinds plus any declared as
        data (define-values --predicate actor_kind --value drone)."""
        return set(ACTOR_KINDS) | self._vocabulary(connection, "actor_kind")["values"]

    def _performer(self, connection: sqlite3.Connection, reference: str | None,
                   person_only: str | None = None) -> sqlite3.Row:
        """The actor behind a record: a person, agent, robot, sensor, system,
        webhook… With person_only (the act's name) it must be a person: some
        acts are decisions only people make."""
        if not reference:
            raise MemoryError("performed_by is required: ask who or what did it (a person, system, sensor…)")
        actor = self.resolve_entity(connection, reference)
        if person_only and actor["entity_type"] != "person":
            raise MemoryError(f"{person_only} is a decision of a person; {reference} is a {actor['entity_type']}")
        kinds = self._actor_kinds(connection)
        if actor["entity_type"] not in kinds:
            raise MemoryError(f"performed_by must be an actor ({', '.join(sorted(kinds))}); {reference} is a "
                              f"{actor['entity_type']}. Declare new actor kinds with define-values --predicate actor_kind")
        return actor

    def _write_event(
        self, connection: sqlite3.Connection, spec: dict[str, Any], default_performer: str | None,
        evidence_id: str, now: str, report: dict[str, Any], attribute_history: dict[str, set[str] | None],
    ) -> str | None:
        event_type, occurred_at = spec.get("type"), spec.get("occurred_at")
        if not (event_type and occurred_at):
            raise MemoryError("events need type and occurred_at")
        key = spec.get("key")
        if key and connection.execute("SELECT 1 FROM events WHERE external_key = ?", (key,)).fetchone():
            report["events"]["existing"].append(key)
            return None
        observer = None
        if spec.get("observed_by"):
            # An agent observation, not an operational act by a person.
            if spec.get("performed_by"):
                raise MemoryError("An event is performed by a person or observed by an agent, not both")
            if getattr(self, "_source_type", None) != AGENT_SOURCE:
                raise MemoryError(f"Agent observations need source type '{AGENT_SOURCE}'")
            observer = self._ensure_agent(connection, spec["observed_by"])
            actor = None
        else:
            actor = self._performer(connection, spec.get("performed_by") or default_performer)
        attributes = spec.get("attributes", {})
        self._check_attribute_drift(connection, event_type, set(attributes), attribute_history, report)
        event_id = new_id()
        connection.execute(
            """INSERT INTO events(event_id, event_type, summary, occurred_at, ended_at, recorded_at, confidence, external_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, event_type, spec.get("summary"), normalize_instant(occurred_at, "occurred_at"),
             normalize_instant(spec["ended_at"], "ended_at") if spec.get("ended_at") else None,
             now, float(spec.get("confidence", 1.0)), key),
        )
        role, who = ("observed_by", observer) if observer else ("performed_by", actor)
        connection.execute(
            "INSERT INTO event_participants(event_id, entity_id, role, recorded_at) VALUES (?, ?, ?, ?)",
            (event_id, who["entity_id"], role, now),
        )
        recorder = getattr(self, "_recorder", None)
        if recorder and not (observer and observer["entity_id"] == recorder["entity_id"]):
            connection.execute(
                "INSERT INTO event_participants(event_id, entity_id, role, recorded_at) VALUES (?, ?, 'recorded_by', ?)",
                (event_id, recorder["entity_id"], now),
            )
        connection.execute(
            "INSERT INTO event_evidence(event_id, evidence_id, role, recorded_at) VALUES (?, ?, 'source', ?)",
            (event_id, evidence_id, now),
        )
        # An event may wait on an actor (a request, a comment to answer) until
        # another event resolves it; pending lists it meanwhile.
        participants = list(spec.get("participants", []))
        if spec.get("pending_for"):
            participants.append({"entity": spec["pending_for"], "role": "pending_action_for"})
        for participant in participants:
            entity = self.resolve_entity(connection, participant["entity"])
            if participant["role"] == "pending_action_for" and entity["entity_type"] not in (
                    self._actor_kinds(connection) | {"role"}):
                raise MemoryError(f"pending_for must be an actor or a role; {participant['entity']} is a {entity['entity_type']}")
            if participant["role"] in ("performed_by", "observed_by", "recorded_by"):
                raise MemoryError(f"Use the event's {participant['role']} field instead of a participant with that role")
            connection.execute(
                "INSERT OR IGNORE INTO event_participants(event_id, entity_id, role, recorded_at) VALUES (?, ?, ?, ?)",
                (event_id, entity["entity_id"], participant["role"], now),
            )
        self._write_attributes(connection, "event", event_id, attributes, evidence_id, now, report)
        report["events"]["created"].append(key or event_id)
        return event_id

    def _retire(
        self, connection: sqlite3.Connection, spec: dict[str, Any], default_performer: str | None, changeset: str | None,
        evidence_id: str, now: str, report: dict[str, Any], attribute_history: dict[str, set[str] | None],
    ) -> None:
        """Take an entity out of use: every current fact about it ends at the
        given instant (nothing is deleted; as_of still shows it) and an
        entity_retired event records who did it and why. Its parts (part_of)
        go with it only when asked (with_parts); otherwise they block it."""
        reference, reason = spec.get("entity"), spec.get("reason")
        if not reference or not reason or not spec.get("at"):
            raise MemoryError("retire needs entity, at (when it stopped being in use) and reason")
        at = normalize_instant(spec["at"], "retire.at")
        top = self.resolve_entity(connection, reference)
        if top["retired_at"]:
            report["warnings"].append(f"{reference} was already retired")
            return

        def parts(entity_id: str) -> list[sqlite3.Row]:
            return connection.execute(
                """SELECT entity.* FROM assertions AS link JOIN entities AS entity ON entity.entity_id = link.subject_entity_id
                    WHERE link.predicate_id = 'part_of' AND link.value_entity_id = ? AND link.assertion_state = 'asserted'
                      AND link.valid_to IS NULL ORDER BY entity.external_key""", (entity_id,)).fetchall()

        retiring, frontier = [top], [top]
        while frontier:
            children = [child for entity in frontier for child in parts(entity["entity_id"])]
            if children and not spec.get("with_parts"):
                sample = ", ".join(child["external_key"] or child["entity_id"] for child in children[:5])
                raise MemoryError(f"{reference} still has {len(children)} part(s) ({sample}{'...' if len(children) > 5 else ''}): "
                                  "move them first, or retire them too with with_parts: true. Nothing was written.")
            retiring += children
            frontier = children
        ids = [entity["entity_id"] for entity in retiring]
        marks = ",".join("?" * len(ids))
        facts = connection.execute(
            f"""SELECT assertion_id, valid_from FROM assertions WHERE subject_entity_id IN ({marks})
                  AND assertion_state = 'asserted' AND valid_to IS NULL""", ids).fetchall()
        for fact in facts:
            if fact["valid_from"] and at[:19] <= fact["valid_from"][:19]:
                raise MemoryError(f"retire at {at} is not after a current fact of {reference} began ({fact['valid_from']})")
        for fact in facts:
            connection.execute("UPDATE assertions SET valid_to = ? WHERE assertion_id = ?", (at, fact["assertion_id"]))
            connection.execute(
                "INSERT OR IGNORE INTO assertion_evidence(assertion_id, evidence_id, role, recorded_at) VALUES (?, ?, 'supports', ?)",
                (fact["assertion_id"], evidence_id, now))
            report.setdefault("_closed", []).append(fact["assertion_id"])
        report["facts"]["closed"] += len(facts)
        # retired_at marks when the memory took it out of use; the event keeps when it happened.
        connection.execute(f"UPDATE entities SET retired_at = ? WHERE entity_id IN ({marks}) AND retired_at IS NULL",
                           [now, *ids])
        label = top["external_key"] or top["entity_id"]
        self._write_event(
            connection,
            {"type": "entity_retired", "occurred_at": at, "performed_by": spec.get("performed_by"),
             "summary": f"Retired {label}" + (f" with {len(retiring) - 1} part(s)" if len(retiring) > 1 else "") + f": {reason}",
             "attributes": {"rationale": reason, **({"changeset": changeset} if changeset else {})},
             "participants": [{"entity": entity["entity_id"], "role": "subject" if entity is top else "retired_part"}
                              for entity in retiring]},
            default_performer, evidence_id, now, report, attribute_history)
        # Facts of other entities that point here stay: they may be history worth keeping.
        incoming = connection.execute(
            f"""SELECT fact.predicate_id, COUNT(*) AS count FROM assertions AS fact
                 WHERE fact.value_entity_id IN ({marks}) AND fact.subject_entity_id NOT IN ({marks})
                   AND fact.assertion_state = 'asserted' AND fact.valid_to IS NULL GROUP BY fact.predicate_id""",
            [*ids, *ids]).fetchall()
        for row in incoming:
            report["warnings"].append(f"{row['count']} current {row['predicate_id']} fact(s) of other entities still point "
                                      f"to {label}; close them if they ended too")
        report.setdefault("retired", []).extend(entity["external_key"] or entity["entity_id"] for entity in retiring)

    @staticmethod
    def _check_attribute_drift(
        connection: sqlite3.Connection, event_type: str, keys: set[str],
        history: dict[str, set[str] | None], report: dict[str, Any],
    ) -> None:
        if event_type not in history:
            rows = connection.execute(
                """SELECT DISTINCT attribute.predicate_id FROM (
                       SELECT event_id FROM events WHERE event_type = ? ORDER BY occurred_at DESC LIMIT 50) AS recent
                   CROSS JOIN record_attributes AS attribute
                     ON attribute.record_type = 'event' AND attribute.record_id = recent.event_id""",
                (event_type,),
            ).fetchall()
            history[event_type] = {row[0] for row in rows} or None
        previous = history[event_type]
        # Optional attributes may be absent (a cash sale has no installments);
        # a never-seen attribute is what signals drift such as a renamed measure.
        if previous is not None and keys - previous:
            added, missing = sorted(keys - previous), sorted(previous - keys)
            report["warnings"].append(
                f"{event_type}: attributes differ from earlier events; new: {', '.join(added)}"
                + (f"; missing: {', '.join(missing)}" if missing else "")
            )
            history[event_type] = previous | keys

    def _write_link(
        self, connection: sqlite3.Connection, event_id: str, spec: dict[str, Any],
        evidence_id: str, now: str, report: dict[str, Any],
    ) -> None:
        predicate = self._predicate(connection, spec["predicate"])
        if "entity" in spec:
            target_type, target_id = "entity", self.resolve_entity(connection, spec["entity"])["entity_id"]
        elif "event" in spec:
            target = connection.execute(
                "SELECT event_id FROM events WHERE external_key = ? OR event_id = ?", (spec["event"], spec["event"])
            ).fetchone()
            if target is None:
                raise MemoryError(f"Unknown event {spec['event']}")
            target_type, target_id = "event", target["event_id"]
        else:
            raise MemoryError("links need an entity or an event target")
        link_id = new_id()
        connection.execute(
            """INSERT INTO record_links(record_link_id, source_record_type, source_record_id, predicate_id,
                                        target_record_type, target_record_id, valid_from, valid_to, recorded_at)
               VALUES (?, 'event', ?, ?, ?, ?, NULL, NULL, ?)""",
            (link_id, event_id, predicate["predicate_id"], target_type, target_id, now),
        )
        self._write_attributes(connection, "record_link", link_id, spec.get("attributes", {}), evidence_id, now, report)
        report["links"] += 1

    def _write_attributes(
        self, connection: sqlite3.Connection, record_type: str, record_id: str,
        attributes: dict[str, Any], evidence_id: str, now: str, report: dict[str, Any],
    ) -> None:
        for predicate_id, raw in attributes.items():
            predicate = self._predicate(connection, predicate_id)
            value, unit = (raw.get("value"), raw.get("unit")) if isinstance(raw, dict) and "value" in raw else (raw, None)
            if predicate["unit"] and unit and unit != predicate["unit"]:
                raise MemoryError(f"{predicate_id} is measured in {predicate['unit']}, not {unit}")
            value_type, columns = self._typed_value(connection, predicate, value)
            column, stored = next(iter(columns.items()))
            connection.execute(
                f"""INSERT INTO record_attributes(attribute_id, record_type, record_id, predicate_id, value_type,
                                                  {column}, unit, evidence_id, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id(), record_type, record_id, predicate_id, value_type, stored,
                 unit or predicate["unit"], evidence_id, now),
            )
            report["attributes"] += 1

    # ------------------------------------------------------------------
    # Project memory: low-friction capture and a start-of-session brief.
    # ------------------------------------------------------------------

    @staticmethod
    def _owner_key(connection: sqlite3.Connection) -> str | None:
        rows = connection.execute(
            """SELECT entity.external_key, entity.entity_id FROM assertions
                 JOIN entities AS entity ON entity.entity_id = assertions.subject_entity_id
                WHERE predicate_id = 'is_memory_owner' AND value_boolean = 1
                  AND assertion_state = 'asserted' AND valid_to IS NULL"""
        ).fetchall()
        return (rows[0]["external_key"] or rows[0]["entity_id"]) if len(rows) == 1 else None

    def remember(
        self,
        kind: str,
        text: str,
        why: str | None = None,
        about: list[str] | None = None,
        performed_by: str | None = None,
        source_text: str | None = None,
        occurred_at: str | None = None,
        key: str | None = None,
        resolves: str | None = None,
        supersedes: str | None = None,
        as_agent: bool = False,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Capture one piece of project knowledge in a single call.

        With as_agent the item is the agent's own observation (lesson, note,
        open question or milestone): it carries observed_by instead of a
        person, so nothing the agent noticed is attributed to the user.

        Builds a write() document: the literal source, an event of the given
        kind attributed to a person (the memory owner by default), its reason,
        what it is about, and links that close an open question or replace an
        earlier decision. The key derives from kind and text, so repeating the
        same memory does not duplicate it. The stored event is read back.
        """
        if kind not in MEMORY_KINDS:
            raise MemoryError(f"kind must be one of {', '.join(MEMORY_KINDS)}")
        if not text or not text.strip():
            raise MemoryError("remember needs the text to keep")
        if kind == "answer" and not resolves:
            raise MemoryError("an answer needs --resolves <open question key>")
        observer = None
        if as_agent:
            # An agent may close only the questions it raised itself; a
            # person's question needs that person's (or another person's) answer.
            if kind not in AGENT_KINDS and not (kind == "answer" and resolves):
                raise MemoryError(f"An agent may record {', '.join(AGENT_KINDS)}; a {kind} belongs to a person")
            if performed_by:
                raise MemoryError("Use --by for what a person said, or --as-agent for your own observation, not both")
            observer = agent or detect_agent()
            if not observer:
                raise MemoryError("No agent detected; pass --agent agent:<name> or set MEMORY_AGENT")
        with self.connection() as connection:
            if observer and kind == "answer":
                raised_by = connection.execute(
                    """SELECT entities.external_key FROM events
                         JOIN event_participants AS actor ON actor.event_id = events.event_id AND actor.role = 'observed_by'
                         JOIN entities ON entities.entity_id = actor.entity_id
                        WHERE events.external_key = ? OR events.event_id = ?""",
                    (resolves, resolves),
                ).fetchone()
                if raised_by is None or raised_by["external_key"] != observer:
                    raise MemoryError(f"{resolves} was not raised by {observer}; only the person who owns it can answer it")
            performer = None if observer else (performed_by or self._owner_key(connection))
            if not observer and not performer:
                raise MemoryError("performed_by is required: who decided or said this? (no single memory owner found)")
            if performer:
                # Decisions, conventions and answers come from people; other
                # actors produce data, not agreements.
                self._performer(connection, performer, person_only=f"A {kind}")
            for target, allowed in ((resolves, ("open_question",)), (supersedes, (kind,))):
                if not target:
                    continue
                row = connection.execute(
                    "SELECT event_type FROM events WHERE external_key = ? OR event_id = ?", (target, target)
                ).fetchone()
                if row is None:
                    raise MemoryError(f"Unknown event {target}")
                if row["event_type"] not in allowed:
                    raise MemoryError(f"{target} is a {row['event_type']}; expected {' or '.join(allowed)}")
        key = key or f"{kind}:{content_hash(kind + '|' + text.strip())[:12]}"
        links = [{"predicate": "resolves", "event": resolves}] if resolves else []
        links += [{"predicate": "supersedes", "event": supersedes}] if supersedes else []
        literal = source_text or (text if not why else f"{text}\nWhy: {why}")
        event_actor = {"observed_by": observer} if observer else {"performed_by": performer}
        report = self.write({
            "source": {"type": AGENT_SOURCE if observer else "conversation", "content": literal},
            **({"recorded_by": agent} if agent else {}),
            "events": [{
                **event_actor,
                "key": key,
                "type": kind,
                "occurred_at": occurred_at or utc_now(),
                "summary": text.strip(),
                "attributes": {"rationale": why} if why else {},
                "participants": [{"entity": reference, "role": "subject"} for reference in about or []],
                "links": links,
            }],
        })
        return {"key": key, "created": bool(report["events"]["created"]), "stored": self.event(key)}

    def void(self, event: str, reason: str, performed_by: str | None = None, source_text: str | None = None) -> dict[str, Any]:
        """Void one event in its own atomic document and read the result back."""
        with self.connection() as connection:
            performer = performed_by or self._owner_key(connection)
        if not performer:
            raise MemoryError("performed_by is required: who decided to void it?")
        report = self.write({
            "source": {"type": "conversation", "content": source_text or f"Void {event}: {reason}"},
            "performed_by": performer,
            "void": [{"event": event, "reason": reason}],
        })
        return {"voided": report["voided"], "warnings": report["warnings"], "event": self.event(event)}
