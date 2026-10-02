"""Processes: versioned definitions as data, instances bound to their version, roles and exceptions."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from .base import MemoryError, active_event_sql, julianday_le, normalize_instant, parse_instant, utc_now, without_nulls
from .rules import bypassed_captures, split_path, missing_captures, process_roles, steps_view, validate_process


class ProcessMixin:
    """Processes: versioned definitions as data, instances bound to their version, roles and exceptions."""

    def transition(
        self,
        instance: str,
        to_state: str,
        performed_by: str,
        pending_for: str | None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """Apply a validated workflow transition and write its audit event."""
        with self.connection() as connection:
            now = utc_now()
            occurred_at = normalize_instant(occurred_at, "occurred_at") if occurred_at else now
            return self._apply_transition(
                connection,
                self.resolve_entity(connection, instance),
                self.resolve_entity(connection, to_state),
                self._performer(connection, performed_by),
                pending_for,
                occurred_at,
            )

    @staticmethod
    def _state_flag(connection: sqlite3.Connection, state_id: str, predicate_id: str) -> bool | None:
        row = connection.execute(
            """SELECT value_boolean FROM assertions WHERE subject_entity_id = ? AND predicate_id = ?
               AND assertion_state = 'asserted' AND valid_to IS NULL""",
            (state_id, predicate_id),
        ).fetchone()
        return None if row is None else bool(row["value_boolean"])

    def _apply_transition(
        self,
        connection: sqlite3.Connection,
        subject: sqlite3.Row,
        target: sqlite3.Row,
        actor: sqlite3.Row,
        pending_for: str | None,
        occurred_at: str,
    ) -> dict[str, Any]:
        current = connection.execute(
            """
            SELECT value_entity_id, valid_from FROM assertions
             WHERE subject_entity_id = ? AND predicate_id = 'workflow_status'
               AND assertion_state = 'asserted' AND valid_to IS NULL
            """,
            (subject["entity_id"],),
        ).fetchone()
        if current is None:
            raise MemoryError(f"{subject['canonical_name']} has no current workflow_status")
        if current["valid_from"] and parse_instant(occurred_at) <= parse_instant(current["valid_from"]):
            raise MemoryError(f"{subject['canonical_name']}: transition at {occurred_at} is not after {current['valid_from']}")
        source_id = current["value_entity_id"]
        if self._state_flag(connection, source_id, "is_terminal"):
            raise MemoryError("Terminal states cannot be transitioned")
        allowed = connection.execute(
            """
            SELECT 1 FROM entities transition
            JOIN assertions source ON source.subject_entity_id = transition.entity_id
            JOIN assertions destination ON destination.subject_entity_id = transition.entity_id
            WHERE transition.entity_type = 'workflow_transition'
              AND source.predicate_id = 'from_workflow_state' AND source.value_entity_id = ?
              AND destination.predicate_id = 'to_workflow_state' AND destination.value_entity_id = ?
            """,
            (source_id, target["entity_id"]),
        ).fetchone()
        if allowed is None:
            raise MemoryError("The requested workflow transition is not allowed")

        # Terminal states never keep a pending action. Other states require one
        # unless the state declares requires_pending_action = false (for
        # example an available lot, which waits on nobody).
        if self._state_flag(connection, target["entity_id"], "is_terminal"):
            pending_for = None
        elif pending_for is None and self._state_flag(connection, target["entity_id"], "requires_pending_action") is not False:
            raise MemoryError(f"State {target['canonical_name']} requires pending_for")
        pending = self._performer(connection, pending_for) if pending_for else None

        self._close_current(connection, subject["entity_id"], "workflow_status", occurred_at)
        self._close_current(connection, subject["entity_id"], "pending_action_for", occurred_at)
        self._insert_entity_assertion(connection, subject["entity_id"], "workflow_status", target["entity_id"], occurred_at)
        if pending:
            self._insert_entity_assertion(connection, subject["entity_id"], "pending_action_for", pending["entity_id"], occurred_at)

        event = self.record_event_in_transaction(
            connection,
            "workflow_transition",
            f"Transitioned {subject['canonical_name']} to {target['canonical_name']}.",
            actor,
            occurred_at,
            [
                (subject["entity_id"], "workflow_instance"),
                (source_id, "from_state"),
                (target["entity_id"], "to_state"),
                *(([(pending["entity_id"], "pending_action_holder")] if pending else [])),
            ],
        )
        return {"event_id": event, "state": target["canonical_name"], "pending_for": pending["canonical_name"] if pending else None}

    def _process_summaries(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        unstartable = self.unstartable_types(connection)
        """Each process with its owner, current version, steps and who may make exceptions."""
        summaries = []
        for process in connection.execute(
            "SELECT entity_id, external_key, canonical_name FROM entities WHERE entity_type = 'process' ORDER BY external_key"
        ).fetchall():
            versions = self._process_versions(connection, process["entity_id"])
            if not versions:
                continue
            current = versions[-1]
            owner = self._current_value(connection, process["entity_id"], "process_owner")
            labels = self._entity_labels(connection, {owner} if owner else set())
            by_version = dict(connection.execute(
                """SELECT version.value_integer, COUNT(*) FROM assertions AS follows
                     JOIN assertions AS version ON version.subject_entity_id = follows.subject_entity_id
                      AND version.predicate_id = 'process_version' AND version.assertion_state = 'asserted'
                      AND version.valid_to IS NULL
                    WHERE follows.predicate_id = 'follows_process' AND follows.value_entity_id = ?
                      AND follows.assertion_state = 'asserted'
                    GROUP BY version.value_integer""",
                (process["entity_id"],),
            ).fetchall())
            summaries.append({
                "key": process["external_key"], "name": process["canonical_name"],
                "owner": labels.get(owner, owner), "current_version": current["version"],
                "since": current["valid_from"], "versions": len(versions),
                "steps": [state["name"] + (" (terminal)" if state.get("terminal") else "") for state in current["definition"]["states"]],
                "exceptions_by": current["definition"].get("override_roles", []),
                "instances_by_version": by_version,
                **({"unstartable_types": unstartable} if unstartable else {}),
            })
        return summaries

    # ------------------------------------------------------------------
    # Processes: versioned definitions as data; each instance keeps the
    # version it started under; exceptions by authorized roles are logged.
    # ------------------------------------------------------------------

    def define_process(
        self, key: str, name: str, definition: dict[str, Any], performed_by: str, owner: str | None = None,
        effective_from: str | None = None, reason: str | None = None, source_text: str | None = None,
    ) -> dict[str, Any]:
        """Create a process (with its owner) or publish a new version of it.

        Only the process owner publishes versions. Instances already running
        keep their version; moving them is an explicit migration."""
        validate_process(definition)
        if not key.startswith("process:"):
            raise MemoryError("Process keys look like process:<name>")
        with self.connection() as connection:
            actor = self._performer(connection, performed_by, person_only="Publishing a process")
            for role in sorted(process_roles(definition)):
                row = connection.execute("SELECT entity_type FROM entities WHERE external_key = ?", (role,)).fetchone()
                if row is None or row["entity_type"] != "role":
                    raise MemoryError(f"{role} is not a known role entity; create it first (type role)")
            for transition in definition["transitions"]:
                for capture in transition.get("captures", []):
                    self._predicate(connection, capture["attribute"])
            process = connection.execute("SELECT * FROM entities WHERE external_key = ?", (key,)).fetchone()
            versions = self._process_versions(connection, process["entity_id"]) if process else []
            if process:
                current_owner = self._current_value(connection, process["entity_id"], "process_owner")
                if current_owner != actor["entity_id"]:
                    owner_row = connection.execute("SELECT canonical_name FROM entities WHERE entity_id = ?",
                                                   (current_owner,)).fetchone()
                    raise MemoryError(f"Only the process owner ({owner_row['canonical_name'] if owner_row else 'none'}) "
                                      f"may change {key}; {actor['canonical_name']} may adjust single instances instead")
                if owner:
                    raise MemoryError("The owner is set when the process is created; change it with a process_owner fact")
            elif not owner:
                raise MemoryError("A new process needs --owner (the person who may change it)")
            last = versions[-1] if versions else None
        start = normalize_instant(effective_from, "effective_from") if effective_from else None
        if start is None:
            while last and utc_now()[:19] <= last["valid_from"][:19]:
                time.sleep(0.05)
            start = utc_now()
        stored = {**{k: v for k, v in definition.items() if k != "version"}, "version": (last["version"] + 1) if last else 1}
        literal = source_text or json.dumps({"key": key, "definition": stored}, ensure_ascii=False, sort_keys=True)
        report = self.write({
            "source": {"type": "process_definition" if not source_text else "conversation", "content": literal},
            "performed_by": performed_by,
            "entities": [{"key": key, "type": "process", "name": name, "description": definition.get("description")}],
            "facts": [
                *([{"subject": key, "predicate": "process_owner", "value": owner, "valid_from": start}] if owner else []),
                {"subject": key, "predicate": "process_definition", "value": stored, "valid_from": start},
            ],
            "events": [{
                "type": "process_defined", "occurred_at": start,
                "summary": f"{name} v{stored['version']}" + (f": {reason}" if reason else ""),
                "attributes": {"process_version": stored["version"], **({"rationale": reason} if reason else {})},
                "links": [{"predicate": "under_process", "entity": key}],
            }],
        })
        return {"key": key, "version": stored["version"], "valid_from": start, "warnings": report["warnings"]}

    @staticmethod
    def _current_value(connection: sqlite3.Connection, entity_id: str, predicate_id: str, at: str | None = None) -> Any:
        """The value of a one-valued fact now, or in force at an instant."""
        # Now: the asserted value. At an instant: whatever held then, even if
        # later superseded; only retracted (wrong) values never count.
        state = "assertion_state = 'asserted'" if at is None else "assertion_state <> 'retracted'"
        timing = ("AND valid_to IS NULL" if at is None else
                  "AND (valid_from IS NULL OR julianday(valid_from) <= julianday(:at)) "
                  "AND (valid_to IS NULL OR julianday(valid_to) > julianday(:at))")
        row = connection.execute(
            f"""SELECT COALESCE(value_entity_id, value_text, value_integer, value_real, value_json, value_boolean,
                                value_datetime) AS value, valid_from
                  FROM assertions WHERE subject_entity_id = :subject AND predicate_id = :predicate
                   AND {state} {timing}
                 ORDER BY julianday(valid_from) DESC LIMIT 1""",
            {"subject": entity_id, "predicate": predicate_id, "at": at},
        ).fetchone()
        return row["value"] if row else None

    @staticmethod
    def _process_versions(connection: sqlite3.Connection, process_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT value_json, valid_from, valid_to FROM assertions
                WHERE subject_entity_id = ? AND predicate_id = 'process_definition' AND assertion_state <> 'retracted'
                ORDER BY julianday(valid_from)""",
            (process_id,),
        ).fetchall()
        versions = []
        for row in rows:
            definition = json.loads(row["value_json"])
            versions.append({"version": definition["version"], "definition": definition,
                             "valid_from": row["valid_from"], "valid_to": row["valid_to"]})
        return versions

    def _roles_at(self, connection: sqlite3.Connection, person_id: str, at: str) -> set[str]:
        return {row[0] for row in connection.execute(
            """SELECT role.external_key FROM assertions AS fact JOIN entities AS role ON role.entity_id = fact.value_entity_id
                WHERE fact.subject_entity_id = ? AND fact.predicate_id = 'holds_role' AND fact.assertion_state <> 'retracted'
                  AND (fact.valid_from IS NULL OR julianday(fact.valid_from) <= julianday(?))
                  AND (fact.valid_to IS NULL OR julianday(fact.valid_to) > julianday(?))""",
            (person_id, at, at),
        )}

    def _instance_state(self, connection: sqlite3.Connection, instance: sqlite3.Row) -> dict[str, Any] | None:
        process_id = self._current_value(connection, instance["entity_id"], "follows_process")
        if process_id is None:
            return None
        process = connection.execute("SELECT * FROM entities WHERE entity_id = ?", (process_id,)).fetchone()
        version = self._current_value(connection, instance["entity_id"], "process_version")
        step = connection.execute(
            """SELECT value_text, valid_from FROM assertions WHERE subject_entity_id = ? AND predicate_id = 'process_step'
                AND assertion_state = 'asserted' AND valid_to IS NULL""",
            (instance["entity_id"],),
        ).fetchone()
        definition = next((item["definition"] for item in self._process_versions(connection, process_id)
                           if item["version"] == version), None)
        if definition is None:
            raise MemoryError(f"{process['external_key']} has no version {version}")
        pending = self._current_value(connection, instance["entity_id"], "pending_action_for")
        return {"process": process, "version": version, "definition": definition, "pending_id": pending,
                "step": step["value_text"] if step else None, "since": step["valid_from"] if step else None}

    def _entity_values_at(self, connection: sqlite3.Connection, entity_ids: set[str], predicate_id: str,
                          at: str) -> set[str]:
        """Entities reached from these entities through a fact in force at an instant."""
        if not entity_ids:
            return set()
        marks = ",".join("?" * len(entity_ids))
        return {row[0] for row in connection.execute(
            f"""SELECT value_entity_id FROM assertions
                 WHERE subject_entity_id IN ({marks}) AND predicate_id = ? AND value_entity_id IS NOT NULL
                   AND assertion_state <> 'retracted'
                   AND (valid_from IS NULL OR julianday(valid_from) <= julianday(?))
                   AND (valid_to IS NULL OR julianday(valid_to) > julianday(?))""",
            (*entity_ids, predicate_id, at, at),
        )}

    def _missing_requirements(self, connection: sqlite3.Connection, transition: dict[str, Any], instance_id: str,
                              at: str) -> list[str]:
        """What the instance still lacks for a transition, at an instant."""
        missing = [predicate for predicate in transition.get("requires", [])
                   if self._current_value(connection, instance_id, predicate, at) is None]
        for rule in transition.get("requires_each", []):
            needed = {instance_id}
            for step in split_path(rule["of"]):
                needed = self._entity_values_at(connection, needed, step, at)
            held = self._entity_values_at(connection, {instance_id}, rule["in"], at)
            labels = self._entity_labels(connection, needed - held)
            missing += sorted(f"{rule['in']}: {labels.get(item, item)}" for item in needed - held)
        return missing

    def _check_transition(
        self, connection: sqlite3.Connection, definition: dict[str, Any], source: str | None, target: str,
        actor: sqlite3.Row, at: str, reason: str | None, instance_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Allowed if the process has the transition, the actor holds one of its
        roles and the instance holds the facts it requires; otherwise only an
        override role may do it, with a reason."""
        if target not in {state["name"] for state in definition["states"]}:
            raise MemoryError(f"{target!r} is not a step of version {definition['version']}; "
                              "a new step is a change to the process (its owner publishes a new version)")
        roles = self._roles_at(connection, actor["entity_id"], at)
        transition = next((item for item in definition["transitions"]
                           if item.get("from") == source and item["to"] == target), None)
        missing = self._missing_requirements(connection, transition, instance_id, at) if transition and instance_id else []
        uncaptured = missing_captures(transition, attributes or {}) if transition else []
        mismatched = (self._mismatched_captures(connection, transition, instance_id, attributes or {})
                      if transition and instance_id else [])
        allowed_role = bool(transition and roles & set(transition["roles"]))
        if allowed_role and not missing and not uncaptured and not mismatched:
            return {"override": False}
        # Every reason the step falls outside the process goes on record.
        kind = "outside_process" if not transition else ", ".join(
            [*(["role_not_allowed"] if not allowed_role else []), *(["missing_requirement"] if missing else []),
             *(["missing_capture"] if uncaptured else []), *(["capture_mismatch"] if mismatched else [])])
        overriders = set(definition.get("override_roles", []))
        if roles & overriders:
            if not reason:
                raise MemoryError(f"{actor['canonical_name']} may make this exception ({kind}) but must give --reason")
            # Data the step should have recorded stays visibly missing.
            skipped = ([f"{source or '(start)'} -> {target}: {item}" for item in uncaptured] if transition
                       else bypassed_captures(definition, source, target))
            return {"override": True, "override_kind": kind, **({"skipped_data": skipped} if skipped else {})}
        if allowed_role and mismatched and not missing and not uncaptured:
            raise MemoryError(f"{source or '(start)'} -> {target}: {'; '.join(mismatched)} "
                              f"(version {definition['version']}). Exceptions: {', '.join(sorted(overriders)) or 'nobody'}")
        if allowed_role and uncaptured and not missing:
            raise MemoryError(f"{source or '(start)'} -> {target} must record {', '.join(uncaptured)} "
                              f"(version {definition['version']}); pass them with --attr. "
                              f"Exceptions: {', '.join(sorted(overriders)) or 'nobody'}")
        if allowed_role:
            raise MemoryError(f"{source or '(start)'} -> {target} requires {', '.join(missing + uncaptured)} on the instance "
                              f"(version {definition['version']}); record it first. "
                              f"Exceptions: {', '.join(sorted(overriders)) or 'nobody'}")
        if transition:
            raise MemoryError(f"{actor['canonical_name']} (roles: {', '.join(sorted(roles)) or 'none'}) may not move "
                              f"{source} -> {target}; allowed roles: {', '.join(transition['roles'])}; "
                              f"exceptions: {', '.join(sorted(overriders)) or 'nobody'}")
        allowed = [item["to"] for item in definition["transitions"] if item.get("from") == source]
        raise MemoryError(f"{source} -> {target} is not in version {definition['version']} of the process "
                          f"(from {source} it may go to: {', '.join(allowed) or 'nothing'}); "
                          f"only {', '.join(sorted(overriders)) or 'nobody'} may make an exception")

    def _mismatched_captures(self, connection: sqlite3.Connection, transition: dict[str, Any], instance_id: str,
                             attributes: dict[str, Any]) -> list[str]:
        """Captured values that must equal what an earlier step of the same
        instance recorded (the receipt copy against the receipt issued)."""
        problems = []
        for capture in transition.get("captures", []):
            earlier_name = capture.get("equals_earlier")
            given = attributes.get(capture["attribute"])
            if not earlier_name or given in (None, ""):
                continue
            earlier = connection.execute(
                """SELECT COALESCE(attribute.value_text, attribute.value_integer, attribute.value_real) AS value
                     FROM record_links AS link JOIN events AS event ON event.event_id = link.source_record_id
                     JOIN record_attributes AS attribute ON attribute.record_type = 'event'
                      AND attribute.record_id = event.event_id AND attribute.predicate_id = ?
                    WHERE link.target_record_type = 'entity' AND link.target_record_id = ?
                      AND link.predicate_id = 'process_instance' AND link.source_record_type = 'event'
                      AND """ + active_event_sql("event") + """
                    ORDER BY julianday(event.occurred_at) DESC LIMIT 1""",
                (earlier_name, instance_id),
            ).fetchone()
            if earlier is None:
                problems.append(f"{capture['attribute']} {given!r} has nothing to match: no earlier step recorded {earlier_name}")
            elif str(earlier["value"]) != str(given):
                problems.append(f"{capture['attribute']} {given!r} does not match {earlier_name} {earlier['value']!r} "
                                "recorded earlier")
        return problems

    def unstartable_types(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        """Types nobody could start: a type is startable when every item it
        requires (requires_each of "…>P") is either produced by nobody here
        (it comes from outside) or produced_by some startable type. What never
        becomes startable is reported, never enforced: the owner decides."""
        requirements: dict[str, set[str]] = {}
        producers: dict[str, set[str]] = {}
        for process in connection.execute("SELECT entity_id FROM entities WHERE entity_type = 'process'").fetchall():
            versions = self._process_versions(connection, process["entity_id"])
            for transition in (versions[-1]["definition"]["transitions"] if versions else []):
                for rule in transition.get("requires_each", []):
                    for row in connection.execute(
                        """SELECT subject_entity_id AS kind, value_entity_id AS item FROM assertions
                            WHERE predicate_id = ? AND assertion_state = 'asserted' AND valid_to IS NULL
                              AND value_entity_id IS NOT NULL""",
                        (split_path(rule["of"])[-1],),
                    ):
                        requirements.setdefault(row["kind"], set()).add(row["item"])
        for row in connection.execute(
            """SELECT subject_entity_id AS item, value_entity_id AS kind FROM assertions
                WHERE predicate_id = 'produced_by' AND assertion_state = 'asserted' AND valid_to IS NULL"""
        ):
            producers.setdefault(row["item"], set()).add(row["kind"])
        kinds = set(requirements) | {kind for makers in producers.values() for kind in makers}
        startable: set[str] = set()
        changed = True
        while changed:
            changed = False
            for kind in kinds - startable:
                if all(not producers.get(item) or producers[item] & startable for item in requirements.get(kind, ())):
                    startable.add(kind)
                    changed = True
        blocked = sorted(kinds - startable)
        labels = self._entity_labels(connection, set(blocked) | {item for kind in blocked for item in requirements.get(kind, ())})
        return [{"type": labels.get(kind, kind),
                 "blocked_by": sorted(labels.get(item, item) for item in requirements.get(kind, ())
                                      if producers.get(item) and not producers[item] & startable)}
                for kind in blocked]

    def requirement_cycles(self, connection: sqlite3.Connection) -> list[list[str]]:
        """Loops in what requires what (kept for reference; unstartable_types
        is what matters: a loop with a way in from outside blocks nobody)."""
        edges: dict[str, set[str]] = {}
        for process in connection.execute("SELECT entity_id FROM entities WHERE entity_type = 'process'").fetchall():
            versions = self._process_versions(connection, process["entity_id"])
            if not versions:
                continue
            for transition in versions[-1]["definition"]["transitions"]:
                for rule in transition.get("requires_each", []):
                    predicate = split_path(rule["of"])[-1]
                    for row in connection.execute(
                        """SELECT requirement.subject_entity_id AS kind, producer.value_entity_id AS producer
                             FROM assertions AS requirement JOIN assertions AS producer
                               ON producer.subject_entity_id = requirement.value_entity_id
                              AND producer.predicate_id = 'produced_by' AND producer.assertion_state = 'asserted'
                              AND producer.valid_to IS NULL
                            WHERE requirement.predicate_id = ? AND requirement.assertion_state = 'asserted'
                              AND requirement.valid_to IS NULL""",
                        (predicate,),
                    ):
                        edges.setdefault(row["kind"], set()).add(row["producer"])
        cycles, seen = [], set()

        def visit(node: str, path: list[str]) -> None:
            for following in sorted(edges.get(node, ())):
                if following in path:
                    cycle = path[path.index(following):]
                    key = frozenset(cycle)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(cycle + [following])
                elif len(path) < 20:
                    visit(following, path + [following])

        for node in sorted(edges):
            visit(node, [node])
        labels = self._entity_labels(connection, {node for cycle in cycles for node in cycle})
        return [[labels.get(node, node) for node in cycle] for cycle in cycles]

    def _pending_holder(self, connection: sqlite3.Connection, definition: dict[str, Any], target: str,
                        pending_for: str | None) -> str | None:
        state = next(state for state in definition["states"] if state["name"] == target)
        if state.get("terminal"):
            return None
        holder = pending_for or state.get("pending")
        if holder:
            row = self.resolve_entity(connection, holder)
            if row["entity_type"] not in ("person", "role"):
                raise MemoryError("pending_for must be a person or a role")
            return row["external_key"] or row["entity_id"]
        return None

    def _step_document(
        self, instance: sqlite3.Row, process: sqlite3.Row, version: int, source: str | None, target: str,
        actor: sqlite3.Row, at: str, holder: str | None, had_pending: bool, check: dict[str, Any],
        reason: str | None, source_text: str | None, event_type: str = "process_transition",
        extra_attributes: dict[str, Any] | None = None, extra_facts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        reference = instance["external_key"] or instance["entity_id"]
        label = f"{instance['canonical_name']}: {source or '(start)'} → {target}"
        if check.get("override"):
            label += f" [exception: {reason}]"
        literal = source_text or label
        attributes = {"to_step": target, "process_version": version, "override": bool(check.get("override")),
                      **({"from_step": source} if source else {}),
                      **({"override_kind": check["override_kind"]} if check.get("override_kind") else {}),
                      **({"skipped_data": "; ".join(check["skipped_data"])} if check.get("skipped_data") else {}),
                      **({"rationale": reason} if reason else {}), **(extra_attributes or {})}
        facts = [*(extra_facts or []), {"subject": reference, "predicate": "process_step", "value": target, "valid_from": at}]
        if holder:
            facts.append({"subject": reference, "predicate": "pending_action_for", "value": holder, "valid_from": at})
        return {
            "source": {"type": "conversation" if source_text else "process_command", "content": literal},
            "performed_by": actor["external_key"] or actor["entity_id"],
            "close": ([{"subject": reference, "predicate": "pending_action_for", "at": at}]
                      if had_pending and not holder else []),
            "facts": facts,
            "events": [{
                "type": event_type, "occurred_at": at, "summary": label, "attributes": attributes,
                "participants": [{"entity": holder, "role": "pending_action_holder"}] if holder else [],
                "links": [{"predicate": "process_instance", "entity": reference},
                          {"predicate": "under_process", "entity": process["external_key"]}],
            }],
        }

    def start_process(
        self, instance: str, process: str, performed_by: str, occurred_at: str | None = None,
        to_step: str | None = None, pending_for: str | None = None, reason: str | None = None,
        source_text: str | None = None, attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an instance under the process version in force at that instant.
        attributes are data captured at this step (typed predicates)."""
        with self.connection() as connection:
            at = normalize_instant(occurred_at, "occurred_at") if occurred_at else utc_now()
            subject = self.resolve_entity(connection, instance)
            if self._current_value(connection, subject["entity_id"], "follows_process"):
                raise MemoryError(f"{instance} already follows a process")
            process_row = self.resolve_entity(connection, process)
            version = next((item for item in self._process_versions(connection, process_row["entity_id"])
                            if julianday_le(item["valid_from"], at) and (not item["valid_to"] or at < item["valid_to"])), None)
            if version is None:
                raise MemoryError(f"No version of {process} was in force at {at}")
            definition = version["definition"]
            starts = [item["to"] for item in definition["transitions"] if item.get("from") is None]
            target = to_step or (starts[0] if len(starts) == 1 else None)
            if target is None:
                raise MemoryError(f"Choose the first step with --to: {', '.join(starts)}")
            actor = self._performer(connection, performed_by)
            check = self._check_transition(connection, definition, None, target, actor, at, reason, subject["entity_id"],
                                           attributes)
            holder = self._pending_holder(connection, definition, target, pending_for)
            reference = subject["external_key"] or subject["entity_id"]
            document = self._step_document(
                subject, process_row, definition["version"], None, target, actor, at, holder, False, check, reason,
                source_text, extra_attributes=attributes, extra_facts=[
                    {"subject": reference, "predicate": "follows_process", "value": process_row["external_key"]},
                    {"subject": reference, "predicate": "process_version", "value": definition["version"], "valid_from": at},
                ])
        self.write(document)
        return self.process_status(instance)

    def advance_process(
        self, instance: str, to_step: str, performed_by: str, occurred_at: str | None = None,
        pending_for: str | None = None, reason: str | None = None, source_text: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Move an instance one step, validated against the version it follows.
        attributes are data captured at this step (typed predicates)."""
        with self.connection() as connection:
            at = normalize_instant(occurred_at, "occurred_at") if occurred_at else utc_now()
            subject = self.resolve_entity(connection, instance)
            state = self._instance_state(connection, subject)
            if state is None:
                raise MemoryError(f"{instance} does not follow a process; use start-process")
            if state["since"] and at[:19] <= state["since"][:19]:
                raise MemoryError(f"{instance}: {at} is not after its current step began ({state['since']})")
            actor = self._performer(connection, performed_by)
            check = self._check_transition(connection, state["definition"], state["step"], to_step, actor, at, reason,
                                           subject["entity_id"], attributes)
            holder = self._pending_holder(connection, state["definition"], to_step, pending_for)
            stricter = None if check.get("override") else self._stricter_note(
                connection, state, to_step, actor, at, subject["entity_id"], attributes)
            document = self._step_document(subject, state["process"], state["version"], state["step"], to_step, actor,
                                           at, holder, state["pending_id"] is not None, check, reason, source_text,
                                           extra_attributes={**(attributes or {}),
                                                             **({"stricter_version_note": stricter} if stricter else {})})
        self.write(document)
        return self.process_status(instance)

    def _stricter_note(self, connection: sqlite3.Connection, state: dict[str, Any], to_step: str, actor: sqlite3.Row,
                       at: str, instance_id: str, attributes: dict[str, Any] | None) -> str | None:
        """An instance keeps its version, but when the version in force would
        have refused this step, say so: the old rules let it pass."""
        latest = self._process_versions(connection, state["process"]["entity_id"])[-1]["definition"]
        if latest["version"] == state["version"] or to_step not in {item["name"] for item in latest["states"]}:
            return None
        try:
            check = self._check_transition(connection, latest, state["step"], to_step, actor, at, None, instance_id,
                                           attributes)
        except MemoryError as error:
            return f"v{latest['version']}: {error}"
        return None if not check.get("override") else f"v{latest['version']} would need an exception"

    def migrate_process(
        self, instance: str, to_version: int, performed_by: str, reason: str, step_map: dict[str, str] | None = None,
        occurred_at: str | None = None, source_text: str | None = None, pending_for: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly move a running instance to another version of its process
        (a migration plan decided by the process owner): its step is mapped."""
        if not reason:
            raise MemoryError("A migration needs --reason (the migration plan)")
        with self.connection() as connection:
            at = normalize_instant(occurred_at, "occurred_at") if occurred_at else utc_now()
            subject = self.resolve_entity(connection, instance)
            state = self._instance_state(connection, subject)
            if state is None:
                raise MemoryError(f"{instance} does not follow a process")
            actor = self._performer(connection, performed_by, person_only="A migration plan")
            if self._current_value(connection, state["process"]["entity_id"], "process_owner", at) != actor["entity_id"]:
                raise MemoryError("Only the process owner decides a migration plan")
            target_version = next((item["definition"] for item in self._process_versions(
                connection, state["process"]["entity_id"]) if item["version"] == to_version), None)
            if target_version is None or to_version == state["version"]:
                raise MemoryError(f"Version {to_version} is not another version of {state['process']['external_key']}")
            target = (step_map or {}).get(state["step"], state["step"])
            if target not in {item["name"] for item in target_version["states"]}:
                raise MemoryError(f"Step {state['step']!r} does not exist in version {to_version}; map it with --map OLD=NEW")
            holder = self._pending_holder(connection, target_version, target, pending_for)
            reference = subject["external_key"] or subject["entity_id"]
            document = self._step_document(
                subject, state["process"], to_version, state["step"], target, actor, at, holder,
                state["pending_id"] is not None, {"override": False}, reason, source_text, event_type="process_migrated",
                extra_attributes={"from_version": state["version"]},
                extra_facts=[{"subject": reference, "predicate": "process_version", "value": to_version, "valid_from": at}])
            if target == state["step"]:
                document["facts"] = [fact for fact in document["facts"] if fact["predicate"] != "process_step"]
        self.write(document)
        return self.process_status(instance)

    def request_exception(
        self, instance: str, to_step: str, performed_by: str, reason: str, occurred_at: str | None = None,
        source_text: str | None = None, attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ask for a step the requester may not take: the request is an event of
        the instance and its next action passes to the first exception role."""
        if not reason:
            raise MemoryError("An exception request needs --reason (what is missing or why)")
        with self.connection() as connection:
            at = normalize_instant(occurred_at, "occurred_at") if occurred_at else utc_now()
            subject = self.resolve_entity(connection, instance)
            state = self._instance_state(connection, subject)
            if state is None:
                raise MemoryError(f"{instance} does not follow a process")
            if to_step not in {item["name"] for item in state["definition"]["states"]}:
                raise MemoryError(f"{to_step!r} is not a step of version {state['version']}")
            approvers = state["definition"].get("override_roles", [])
            if not approvers:
                raise MemoryError(f"Version {state['version']} names nobody who may make exceptions")
            actor = self._performer(connection, performed_by)
            reference = subject["external_key"] or subject["entity_id"]
            count = connection.execute(
                """SELECT COUNT(*) FROM record_links AS link JOIN events ON events.event_id = link.source_record_id
                    WHERE link.target_record_type = 'entity' AND link.target_record_id = ? AND link.predicate_id = 'process_instance'
                      AND events.event_type = 'exception_requested'""",
                (subject["entity_id"],),
            ).fetchone()[0]
        key = f"exreq:{reference}:{count + 1}"
        summary = f"{subject['canonical_name']}: asks to move from {state['step']} to {to_step}: {reason}"
        self.write({
            "source": {"type": "conversation" if source_text else "process_command", "content": source_text or summary},
            "performed_by": actor["external_key"] or actor["entity_id"],
            "facts": [{"subject": reference, "predicate": "pending_action_for", "value": approvers[0], "valid_from": at}],
            "events": [{"key": key, "type": "exception_requested", "occurred_at": at, "summary": summary,
                        "attributes": {"requested_step": to_step, "rationale": reason,
                                       **({"requested_attributes": attributes} if attributes else {})},
                        "links": [{"predicate": "process_instance", "entity": reference},
                                  {"predicate": "under_process", "entity": state["process"]["external_key"]}]}],
        })
        return {"request": key, "pending_for": approvers[0], "status": self.process_status(instance)}

    def resolve_exception(
        self, request: str, approve: bool, performed_by: str, reason: str, occurred_at: str | None = None,
        source_text: str | None = None, attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Approve (the step is taken now, as an exception, in the same
        transaction) or deny (the next action returns to the step's holder)."""
        if not reason:
            raise MemoryError("Resolving an exception needs --reason")
        with self.connection() as connection:
            at = normalize_instant(occurred_at, "occurred_at") if occurred_at else utc_now()
            asked = connection.execute(
                "SELECT event_id, external_key FROM events WHERE (external_key = ? OR event_id = ?) AND event_type = 'exception_requested'",
                (request, request),
            ).fetchone()
            if asked is None:
                raise MemoryError(f"Unknown exception request {request}")
            if connection.execute(
                """SELECT 1 FROM record_links WHERE target_record_type = 'event' AND target_record_id = ?
                    AND predicate_id = 'resolves'""", (asked["event_id"],)).fetchone():
                raise MemoryError(f"{request} is already resolved")
            details = self._attributes_of(connection, "event", [asked["event_id"]]).get(asked["event_id"], {})
            instance_id = connection.execute(
                """SELECT target_record_id FROM record_links WHERE source_record_type = 'event' AND source_record_id = ?
                    AND predicate_id = 'process_instance'""", (asked["event_id"],)).fetchone()[0]
            subject = connection.execute("SELECT * FROM entities WHERE entity_id = ?", (instance_id,)).fetchone()
            state = self._instance_state(connection, subject)
            actor = self._performer(connection, performed_by, person_only="Resolving an exception")
            approvers = set(state["definition"].get("override_roles", []))
            if not self._roles_at(connection, actor["entity_id"], at) & approvers:
                raise MemoryError(f"Only {', '.join(sorted(approvers))} may resolve exceptions")
            if state["since"] and at[:19] <= state["since"][:19]:
                raise MemoryError(f"{request}: {at} is not after the instance's current step began ({state['since']})")
            reference = subject["external_key"] or subject["entity_id"]
            request_key = asked["external_key"] or asked["event_id"]
            links = [{"predicate": "resolves", "event": request_key}, {"predicate": "process_instance", "entity": reference}]
            resolution = {"type": "exception_resolved", "occurred_at": at,
                          "summary": f"{request_key} {'approved' if approve else 'denied'}: {reason}",
                          "attributes": {"decision": "approved" if approve else "denied", "rationale": reason},
                          "links": links}
            if approve:
                target = details["requested_step"]
                data = {**(details.get("requested_attributes") or {}), **(attributes or {})}
                check = self._check_transition(connection, state["definition"], state["step"], target, actor, at,
                                               f"{request_key}: {reason}", instance_id, data)
                holder = self._pending_holder(connection, state["definition"], target, None)
                document = self._step_document(subject, state["process"], state["version"], state["step"], target, actor,
                                               at, holder, state["pending_id"] is not None, check,
                                               f"{request_key}: {reason}", source_text, extra_attributes=data)
                document["events"][0]["links"].append({"predicate": "resolves", "event": request_key})
                document["events"].append(resolution)
            else:
                holder = self._pending_holder(connection, state["definition"], state["step"], None)
                document = {
                    "source": {"type": "conversation" if source_text else "process_command",
                               "content": source_text or resolution["summary"]},
                    "performed_by": actor["external_key"] or actor["entity_id"],
                    "facts": [{"subject": reference, "predicate": "pending_action_for", "value": holder, "valid_from": at}]
                    if holder else [],
                    "close": [] if holder else [{"subject": reference, "predicate": "pending_action_for", "at": at,
                                                 "if_present": True}],
                    "events": [resolution],
                }
        self.write(document)
        return self.process_status(reference)

    def _exception_requests(self, connection: sqlite3.Connection, instance_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT asked.external_key AS request, asked.occurred_at,
                      (SELECT value_text FROM record_attributes WHERE record_type = 'event' AND record_id = asked.event_id
                          AND predicate_id = 'requested_step') AS requested_step,
                      (SELECT entities.canonical_name FROM event_participants AS actor JOIN entities
                          ON entities.entity_id = actor.entity_id
                        WHERE actor.event_id = asked.event_id AND actor.role = 'performed_by') AS requested_by,
                      (SELECT value_text FROM record_links AS answer JOIN record_attributes AS verdict
                          ON verdict.record_type = 'event' AND verdict.record_id = answer.source_record_id
                         AND verdict.predicate_id = 'decision'
                        WHERE answer.target_record_type = 'event' AND answer.target_record_id = asked.event_id
                          AND answer.predicate_id = 'resolves' LIMIT 1) AS decision
                 FROM record_links AS link JOIN events AS asked ON asked.event_id = link.source_record_id
                WHERE link.target_record_type = 'entity' AND link.target_record_id = ?
                  AND link.predicate_id = 'process_instance' AND asked.event_type = 'exception_requested'
                  AND """ + active_event_sql("asked") + """
                ORDER BY julianday(asked.occurred_at)""",
            (instance_id,),
        ).fetchall()
        return [without_nulls({**dict(row), "state": row["decision"] or "open", "decision": None}) for row in rows]

    def overdue_instances(self, process: str | None = None, at: str | None = None) -> list[dict[str, Any]]:
        """Instances whose current step has waited longer than its due_hours
        (declared per state in their own version), at an instant (default now)."""
        at = normalize_instant(at, "at") if at else utc_now()
        now = parse_instant(at)
        overdue = []
        with self.connection() as connection:
            query = "SELECT subject_entity_id FROM assertions WHERE predicate_id = 'follows_process' AND assertion_state = 'asserted'"
            parameters: tuple[Any, ...] = ()
            if process:
                query += " AND value_entity_id = ?"
                parameters = (self.resolve_entity(connection, process)["entity_id"],)
            for (instance_id,) in connection.execute(query, parameters).fetchall():
                subject = connection.execute("SELECT * FROM entities WHERE entity_id = ?", (instance_id,)).fetchone()
                state = self._instance_state(connection, subject)
                due = next((item.get("due_hours") for item in state["definition"]["states"]
                            if item["name"] == state["step"]), None)
                if not due or not state["since"]:
                    continue
                waited = (now - parse_instant(state["since"])).total_seconds() / 3600
                if waited > due:
                    labels = self._entity_labels(connection, {state["pending_id"]} if state["pending_id"] else set())
                    overdue.append({"instance": subject["external_key"] or subject["entity_id"], "step": state["step"],
                                    "since": state["since"], "due_hours": due, "hours_waiting": round(waited, 1),
                                    "pending_for": labels.get(state["pending_id"], state["pending_id"])})
        return sorted(overdue, key=lambda item: -item["hours_waiting"])

    def process_definition(self, key: str, version: int | None = None) -> dict[str, Any]:
        """A process definition as JSON (the current one, or a given version),
        ready to edit and publish again with define-process."""
        with self.connection() as connection:
            process = self.resolve_entity(connection, key)
            versions = self._process_versions(connection, process["entity_id"])
        if not versions:
            raise MemoryError(f"{key} has no definition")
        chosen = versions[-1] if version is None else next((item for item in versions if item["version"] == version), None)
        if chosen is None:
            raise MemoryError(f"{key} has no version {version}; versions: {', '.join(str(v['version']) for v in versions)}")
        return chosen["definition"]

    def process_status(self, instance: str) -> dict[str, Any]:
        """Where an instance is, under which version, who holds it and what may come next."""
        with self.connection() as connection:
            subject = self.resolve_entity(connection, instance)
            state = self._instance_state(connection, subject)
            if state is None:
                raise MemoryError(f"{instance} does not follow a process")
            definition = state["definition"]
            names = self._entity_labels(connection, {state["pending_id"]} if state["pending_id"] else set())
            latest = self._process_versions(connection, state["process"]["entity_id"])[-1]["version"]
            history = connection.execute(
                """SELECT event.occurred_at, event.event_type, event.summary,
                          (SELECT value_integer FROM record_attributes WHERE record_type = 'event'
                              AND record_id = event.event_id AND predicate_id = 'from_version') AS from_version,
                          (SELECT value_integer FROM record_attributes WHERE record_type = 'event'
                              AND record_id = event.event_id AND predicate_id = 'process_version') AS process_version,
                          (SELECT value_text FROM record_attributes WHERE record_type = 'event'
                              AND record_id = event.event_id AND predicate_id = 'stricter_version_note') AS stricter,
                          (SELECT entities.canonical_name FROM event_participants AS actor JOIN entities
                              ON entities.entity_id = actor.entity_id
                            WHERE actor.event_id = event.event_id AND actor.role = 'performed_by') AS by,
                          (SELECT value_text FROM record_attributes WHERE record_type = 'event'
                              AND record_id = event.event_id AND predicate_id = 'to_step') AS to_step,
                          (SELECT value_boolean FROM record_attributes WHERE record_type = 'event'
                              AND record_id = event.event_id AND predicate_id = 'override') AS override,
                          (SELECT value_text FROM record_attributes WHERE record_type = 'event'
                              AND record_id = event.event_id AND predicate_id = 'rationale') AS reason,
                          (SELECT value_text FROM record_attributes WHERE record_type = 'event'
                              AND record_id = event.event_id AND predicate_id = 'skipped_data') AS skipped_data
                     FROM record_links AS link JOIN events AS event ON event.event_id = link.source_record_id
                    WHERE link.target_record_type = 'entity' AND link.target_record_id = ?
                      AND link.predicate_id = 'process_instance' AND link.source_record_type = 'event'
                      AND """ + active_event_sql("event") + """
                    ORDER BY julianday(event.occurred_at)""",
                (subject["entity_id"],),
            ).fetchall()
            terminal = any(item["name"] == state["step"] and item.get("terminal") for item in definition["states"])
            return {
                "instance": subject["external_key"] or subject["entity_id"],
                "process": state["process"]["external_key"],
                "version": state["version"],
                "latest_version": latest,
                "step": state["step"],
                "since": state["since"],
                "terminal": terminal,
                "pending_for": names.get(state["pending_id"], state["pending_id"]),
                "next": [{"to": item["to"], "roles": item["roles"],
                          **({"captures": item["captures"]} if item.get("captures") else {}),
                          **({"missing": missing} if (missing := self._missing_requirements(
                              connection, item, subject["entity_id"], utc_now())) else {})}
                         for item in definition["transitions"]
                         if item.get("from") == state["step"]],
                "exceptions_by": definition.get("override_roles", []),
                # One row per step: when it began, who moved it there, when and by
                # whom it left (the person who worked it), and the step it is in now.
                "steps": steps_view([dict(row) for row in history], state["pending_id"] and
                                    names.get(state["pending_id"], state["pending_id"])),
                "history": [without_nulls({key: value for key, value in dict(row).items()
                                           if key not in ("to_step", "override", "reason", "skipped_data", "from_version",
                                                          "process_version", "stricter")})
                            for row in history],
                **({"exception_requests": requests} if (requests := self._exception_requests(
                    connection, subject["entity_id"])) else {}),
                **({"due": due} if (due := self._due(definition, state)) else {}),
                # What exceptions left unrecorded, and what older rules let pass.
                **({"warnings": warnings} if (warnings := [
                    *(f"{row['occurred_at']} {row['by']} made an exception that left unrecorded: {row['skipped_data']}"
                      for row in history if row["skipped_data"]),
                    *(f"{row['occurred_at']} {row['by']} moved it under the rules of v{row['process_version']}, but the "
                      f"version in force would have stopped it: {row['stricter']}" for row in history if row["stricter"]),
                ]) else {}),
            }

    @staticmethod
    def _due(definition: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        hours = next((item.get("due_hours") for item in definition["states"] if item["name"] == state["step"]), None)
        if not hours or not state["since"]:
            return None
        since = parse_instant(state["since"])
        waited = (datetime.now(timezone.utc) - since).total_seconds() / 3600
        return {"due_hours": hours, "hours_waiting": round(waited, 1), "overdue": waited > hours}
