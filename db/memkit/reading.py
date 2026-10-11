"""Reading: brief, catalog, search, describe, timeline, event and recall."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .base import LINKED_EVENTS_CTE, MemoryError, VOID_EVENT, active_event_sql, entity_label, event_row, query_bound, recall_term, without_nulls, zone_label


def render_brief(brief: dict[str, Any]) -> str:
    """Compact text version of brief() for session hooks: fewer tokens than JSON.
    It is written for the agent (internal, in English); the agent answers
    people in the language they are using."""
    if not brief.get("exists"):
        return f"[project memory] {brief['next_step']}"
    lines = ["[project memory] Source of truth: SQLite. Start-of-session brief:",
             f"- Times shown in {brief['time_zone']['display']}; stored in UTC."]
    for zone in brief["time_zone"].get("operating", []):
        if zone["differs_from_display"] is not False:
            lines.append(f"- {zone['name']} operates in {zone['zone']}, not the display zone: days and periods shift; "
                         f"use --tz={zone['zone']} to see them as the operation lives them.")
    owner = brief.get("owner")
    lines.append(f"- Memory owner: {owner['name']} ({owner['key']})" if owner else "- Memory owner: not recorded")
    for project in brief["projects"]:
        lines.append(f"- Project: {project['name']} ({project['external_key']})"
                     + (f" — {project['description']}" if project.get("description") else ""))
    totals = brief["totals"]
    lines.append(f"- {totals['entities']} entities, {totals['events']} events; last recorded {totals['last_recorded_at']}")
    schema = brief.get("schema", {})
    if schema.get("read_only"):
        lines.append(f"- READ ONLY: this memory uses a newer schema than this helper "
                     f"({', '.join(schema['newer_migrations'])}); update the helper before writing.")
    for item in schema.get("recent_migrations", []):
        lines.append(f"- {item['occurred_at'][:10]} {item['text']}")
    for title, section in (("Decisions and agreements in force", "decisions"),
                           ("Conventions and preferences", "conventions_and_preferences"),
                           ("Open questions", "open_questions"), ("Lessons", "lessons")):
        block = brief[section]
        if not block["open"]:
            continue
        shown = len(block["latest"])
        count = f"{block['open']}" if shown >= block["open"] else f"showing {shown} of {block['open']}"
        lines.append(f"{title} ({count}):")
        for item in block["latest"]:
            detail = f" — why: {item['why']}" if item.get("why") else ""
            origin = " (agent observation)" if "(agent observation)" in (item.get("by") or "") else ""
            answers = f" (answers {item['resolves']})" if item.get("resolves") else ""
            lines.append(f"  · [{item['key']}]{answers} {item['occurred_at'][:10]} {item['text']}{detail}{origin}")
        if shown < block["open"]:
            lines.append("    See all: " + " ; ".join(block["all"]))
    if brief["pending_actions"]["open"]:
        lines.append(f"Pending actions: {brief['pending_actions']['open']}")
    backup = brief.get("last_backup")
    lines.append(f"Last backup: {backup['occurred_at'][:10]} ({backup['path']})" if backup
                 else "Last backup: none yet (memory.py backup makes a verified copy)")
    activity = brief.get("high_volume_activity") or []
    if activity:
        lines.append("High-volume activity (kept out of Most recent; use aggregate/list for detail):")
        for item in activity:
            lines.append(f"  · {item['type']}: {item['recorded_last_7_days']} recorded in the last 7 days; "
                         f"last {item['last_recorded_at']}")
    if brief["recent"]:
        lines.append("Most recent:")
        for item in brief["recent"][:5]:
            lines.append(f"  · {item['occurred_at'][:10]} {item['kind']}: {item.get('text', '')}")
    lines.append("Details: search/describe/timeline/recall; record new knowledge with remember. "
                 "Answer people in the language they are using.")
    return "\n".join(lines)


def hook_output(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}, ensure_ascii=False)


class ReadingMixin:
    """Reading: brief, catalog, search, describe, timeline, event and recall."""

    @staticmethod
    def _name_of(connection: sqlite3.Connection, entity_id: str) -> str:
        """The name an entity goes by now (its current 'name' fact, if renamed)."""
        return connection.execute("SELECT name FROM entity_labels WHERE entity_id = ?", (entity_id,)).fetchone()[0]

    def detail(self, entity_reference: str) -> dict[str, Any]:
        with self.connection() as connection:
            entity = self.resolve_entity(connection, entity_reference)
            state = connection.execute("SELECT state_name FROM memory_current_states WHERE entity_id = ?", (entity["entity_id"],)).fetchone()
            pending = connection.execute("SELECT pending_for_name FROM memory_pending WHERE entity_id = ?", (entity["entity_id"],)).fetchone()
            return {
                "entity_id": entity["entity_id"],
                "name": self._name_of(connection, entity["entity_id"]),
                "type": entity["entity_type"],
                "state": state["state_name"] if state else None,
                "pending_for": pending["pending_for_name"] if pending else None,
            }

    def catalog(self, sample_size: int = 100) -> dict[str, Any]:
        """Return a compact, agent-oriented inventory for cold-start discovery.

        Event types expose their period, attribute keys and relations from a
        sample of their earliest and latest events, so schema drift between
        periods is visible instead of silently mixing in aggregates.
        """
        self._vocabulary_cache = {}
        with self.connection() as connection:
            migrations = connection.execute("SELECT migration_id FROM schema_migrations ORDER BY migration_id").fetchall()
            entity_types = connection.execute(
                """
                SELECT entity_type, COUNT(*) AS count
                  FROM entities
                 GROUP BY entity_type
                 ORDER BY entity_type
                """
            ).fetchall()
            # Small entity types are vocabularies (channels, categories, seasons):
            # their names are what conditions compare against.
            small_types: dict[str, list[str]] = {}
            for row in entity_types:
                if row["count"] <= self.CATEGORICAL_LIMIT and row["entity_type"] not in ("person", "agent"):
                    small_types[row["entity_type"]] = [name for (name,) in connection.execute(
                        "SELECT canonical_name FROM entities WHERE entity_type = ? ORDER BY canonical_name",
                        (row["entity_type"],),
                    )]
            people_total = connection.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type = 'person' AND retired_at IS NULL"
            ).fetchone()[0]
            # With many people (customers, guests…) the useful ones are those
            # the memory says something about: people with current facts.
            people = connection.execute(
                f"""
                SELECT canonical_name AS name, external_key, description
                  FROM entities WHERE entity_type = 'person' AND retired_at IS NULL
                   {'' if people_total <= 50 else '''AND entity_id IN (
                       SELECT subject_entity_id FROM assertions
                        WHERE assertion_state = 'asserted' AND valid_to IS NULL)'''}
                 ORDER BY canonical_name LIMIT 50
                """
            ).fetchall()
            predicates = connection.execute(
                """
                SELECT predicate_id, label, value_type, unit, cardinality, is_temporal, description
                  FROM predicates
                 ORDER BY predicate_id
                """
            ).fetchall()
            views = connection.execute(
                """
                SELECT name FROM sqlite_master
                 WHERE type = 'view' AND name NOT LIKE 'sqlite_%'
                 ORDER BY name
                """
            ).fetchall()
            semantic_rules = [without_nulls(row) for row in connection.execute(
                """
                SELECT entity.entity_type, entity.canonical_name, entity.external_key, entity.description,
                       (SELECT value_json FROM assertions WHERE subject_entity_id = entity.entity_id
                           AND predicate_id = 'rule_definition' AND assertion_state = 'asserted'
                           AND valid_to IS NULL) AS rule
                  FROM entities AS entity
                 WHERE entity.entity_type IN ('semantic_rule', 'query_recipe', 'procedure', 'status_rule')
                 ORDER BY entity.entity_type, entity.canonical_name
                """
            ).fetchall()]
            # A status rule is only usable if the agent knows its state names.
            for rule in semantic_rules:
                definition = json.loads(rule.pop("rule")) if rule.get("rule") else None
                if definition:
                    names = [state["name"] for state in definition.get("states", [])]
                    rule["states"] = list(dict.fromkeys([*names, definition.get("otherwise")]))
                    rule["applies_to"] = definition.get("applies_to")
            values = self._categorical_values(connection)
            # Voids are few: count voided events starting from the voids in force,
            # not by checking every event.
            voided = dict(connection.execute(
                f"""SELECT target.event_type, COUNT(DISTINCT target.event_id)
                      FROM events AS void JOIN record_links AS link
                        ON link.source_record_type = 'event' AND link.source_record_id = void.event_id
                       AND link.predicate_id = 'voids' AND link.target_record_type = 'event'
                      JOIN events AS target ON target.event_id = link.target_record_id
                     WHERE void.event_type = '{VOID_EVENT}' AND {active_event_sql('void')}
                     GROUP BY target.event_type"""
            ).fetchall())
            volume = self._high_volume_types(connection)
            event_types = [
                self._event_type_profile(connection, {**dict(row), "voided": voided.get(row["event_type"], 0),
                                                      "high_volume": volume.get(row["event_type"], False)}, sample_size)
                for row in connection.execute(
                    """
                    SELECT event_type, COUNT(*) AS count, MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at
                      FROM events GROUP BY event_type ORDER BY event_type
                    """
                ).fetchall()
            ]
            agents = connection.execute(
                "SELECT canonical_name AS name, external_key FROM entities WHERE entity_type = 'agent' ORDER BY canonical_name"
            ).fetchall()
            # Every event needs a person who performed it or an agent who observed it.
            # Every event names the actor behind it: whoever performed it, or the
            # agent that observed it.
            kinds = sorted(self._actor_kinds(connection))
            without_actor = connection.execute(
                f"""SELECT COUNT(*) FROM events AS event WHERE NOT EXISTS (
                       SELECT 1 FROM event_participants AS actor JOIN entities ON entities.entity_id = actor.entity_id
                        WHERE actor.event_id = event.event_id
                          AND ((actor.role = 'performed_by' AND entities.entity_type IN ({','.join('?' * len(kinds))}))
                            OR (actor.role = 'observed_by' AND entities.entity_type = 'agent')))""",
                kinds,
            ).fetchone()[0]
            # Non-human actors (sensors, systems, robots…) by kind, with a sample.
            actors = [{"kind": row["entity_type"], "count": row["count"], "sample": row["sample"].split("|")[:5]}
                      for row in connection.execute(
                f"""SELECT entity_type, COUNT(*) AS count, group_concat(COALESCE(external_key, canonical_name), '|') AS sample
                      FROM entities WHERE entity_type IN ({','.join('?' * len(kinds))})
                       AND entity_type NOT IN ('person', 'agent') AND retired_at IS NULL
                     GROUP BY entity_type ORDER BY entity_type""",
                kinds,
            )]
            observations = connection.execute(
                """
                SELECT predicate_id, COUNT(*) AS count, MIN(occurred_at) AS first_at, MAX(occurred_at) AS last_at,
                       COUNT(DISTINCT source_entity_id) AS sources
                  FROM observations GROUP BY predicate_id ORDER BY predicate_id
                """
            ).fetchall()
            return {
                "database": str(self.database_path),
                "time_zone": {"storage": "UTC", "display": zone_label(self.zone), "operating": self._operating_zones(connection)},
                "migrations": [row["migration_id"] for row in migrations],
                "entity_types": [{**dict(row), **({"names": small_types[row["entity_type"]]}
                                                  if row["entity_type"] in small_types else {})}
                                 for row in entity_types],
                "people": [without_nulls(row) for row in people],
                **({"people_total": people_total,
                    "people_note": "With more than 50 people, only those with current facts are listed; "
                                   "use search or aggregate --entity-type person --by fact:P."}
                   if people_total > 50 else {}),
                "agents": [dict(row) for row in agents],
                **({"actors": actors} if actors else {}),
                "integrity": {"events_without_actor": without_actor},
                "event_types": event_types,
                "observations": [dict(row) for row in observations],
                "predicates": [self._predicate_entry(connection, row, values) for row in predicates],
                "views": [row["name"] for row in views],
                "semantic_rules_and_recipes": semantic_rules,
                "recipes": self._recipe_summaries(connection),
                "processes": self._process_summaries(connection),
                "change_rules": self._change_rules(connection),
            }

    def _predicate_entry(self, connection: sqlite3.Connection, row: sqlite3.Row,
                         values: dict[str, dict[str, int]]) -> dict[str, Any]:
        """A predicate with its vocabulary (allowed values or entity type) and
        the stored values that fall outside it, so old spellings stay visible."""
        entry = without_nulls(row)
        predicate_id = row["predicate_id"]
        if predicate_id in values:
            entry["values"] = values[predicate_id]
        vocabulary = self._vocabulary(connection, predicate_id)
        if vocabulary["values"]:
            entry["allowed_values"] = sorted(vocabulary["values"])
            outside = {label: count for label, count in values.get(predicate_id, {}).items()
                       if label not in vocabulary["values"]}
            if outside:
                entry["outside_vocabulary"] = outside
        if vocabulary["entity_type"]:
            entry["value_entity_type"] = vocabulary["entity_type"]
        return entry

    CATEGORICAL_LIMIT = 12

    @classmethod
    def _categorical_values(cls, connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
        """Values of text or entity predicates that take few distinct values
        (roles, plans, categories), with how many current facts or attributes
        hold each, so a cold agent can write conditions without guessing."""
        # One source per table and value kind, each served by its partial value index.
        current = "assertion_state = 'asserted' AND valid_to IS NULL AND "
        sources = [
            f"SELECT predicate_id, {column} AS value, {is_entity} AS is_entity FROM {table} WHERE {extra}{column} IS NOT NULL"
            for table, extra in (("assertions", current), ("record_attributes", ""))
            for column, is_entity in (("value_text", 0), ("value_entity_id", 1))
        ]
        result: dict[str, dict[str, int]] = {}
        for source in sources:
            few = [row[0] for row in connection.execute(
                f"SELECT predicate_id FROM ({source}) GROUP BY predicate_id HAVING COUNT(DISTINCT value) <= ?",
                (cls.CATEGORICAL_LIMIT,),
            )]
            for predicate_id in few:
                rows = connection.execute(
                    f"""SELECT COALESCE(entity.canonical_name, value) AS label, COUNT(*) AS count
                          FROM ({source}) AS item LEFT JOIN entities AS entity
                            ON item.is_entity AND entity.entity_id = item.value
                         WHERE item.predicate_id = ? GROUP BY item.value ORDER BY count DESC""",
                    (predicate_id,),
                ).fetchall()
                bucket = result.setdefault(predicate_id, {})
                for row in rows:
                    bucket[row["label"]] = bucket.get(row["label"], 0) + row["count"]
        return result

    @staticmethod
    def _event_type_profile(connection: sqlite3.Connection, row: sqlite3.Row, sample_size: int) -> dict[str, Any]:
        # CROSS JOIN keeps the 200-event sample as the outer loop; otherwise SQLite
        # may scan every link or attribute and probe the sample for each one.
        sample_cte = """
            WITH sample AS (
                SELECT event_id, summary FROM (
                    SELECT event_id, summary FROM events WHERE event_type = :type ORDER BY occurred_at LIMIT :size)
                UNION
                SELECT event_id, summary FROM (
                    SELECT event_id, summary FROM events WHERE event_type = :type ORDER BY occurred_at DESC LIMIT :size)
            )
        """
        parameters = {"type": row["event_type"], "size": sample_size}
        sampled = connection.execute(sample_cte + "SELECT COUNT(*) FROM sample", parameters).fetchone()[0]
        keys = connection.execute(
            sample_cte
            + """
            SELECT attribute.key, COUNT(*) AS seen
              FROM sample,
                   json_each(CASE WHEN json_valid(sample.summary) AND json_type(sample.summary) = 'object'
                                  THEN sample.summary ELSE '{}' END) AS attribute
             GROUP BY attribute.key ORDER BY attribute.key
            """,
            parameters,
        ).fetchall()
        typed_keys = connection.execute(
            sample_cte
            + """
            SELECT attribute.predicate_id AS key, COUNT(DISTINCT sample.event_id) AS seen
              FROM sample CROSS JOIN record_attributes AS attribute
                ON attribute.record_type = 'event' AND attribute.record_id = sample.event_id
             GROUP BY attribute.predicate_id
            """,
            parameters,
        ).fetchall()
        link_keys = connection.execute(
            sample_cte
            + """
            SELECT link.predicate_id || '.' || attribute.predicate_id AS key, COUNT(*) AS seen
              FROM sample CROSS JOIN record_links AS link
                ON link.source_record_type = 'event' AND link.source_record_id = sample.event_id
              CROSS JOIN record_attributes AS attribute
                ON attribute.record_type = 'record_link' AND attribute.record_id = link.record_link_id
             GROUP BY link.predicate_id, attribute.predicate_id
            """,
            parameters,
        ).fetchall()
        relations = connection.execute(
            sample_cte
            + """
            SELECT 'participant' AS kind, participant.role AS via, entity.entity_type AS target_type, COUNT(*) AS seen
              FROM sample CROSS JOIN event_participants AS participant ON participant.event_id = sample.event_id
              CROSS JOIN entities AS entity ON entity.entity_id = participant.entity_id
             GROUP BY participant.role, entity.entity_type
            UNION ALL
            SELECT 'link', link.predicate_id, COALESCE(entity.entity_type, link.target_record_type), COUNT(*)
              FROM sample CROSS JOIN record_links AS link
                ON link.source_record_type = 'event' AND link.source_record_id = sample.event_id
              LEFT JOIN entities AS entity
                ON link.target_record_type = 'entity' AND entity.entity_id = link.target_record_id
             GROUP BY link.predicate_id, COALESCE(entity.entity_type, link.target_record_type)
            ORDER BY 1, 2
            """,
            parameters,
        ).fetchall()
        return {
            **dict(row),
            "sampled_events": sampled,
            "attribute_keys": {key["key"]: key["seen"] for key in [*keys, *typed_keys]},
            "line_attribute_keys": {key["key"]: key["seen"] for key in link_keys},
            "relations": [dict(relation) for relation in relations],
        }

    def search(self, text: str, entity_type: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Find entities by name, alias, key or description; exact matches first."""
        escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT entity.entity_type, (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = entity.entity_id) AS name, entity.external_key,
                       entity.description, group_concat(DISTINCT alias.name) AS aliases,
                       MIN(CASE
                           WHEN entity.canonical_name = :text COLLATE NOCASE OR entity.external_key = :text
                                OR alias.name = :text COLLATE NOCASE THEN 0
                           WHEN entity.canonical_name LIKE :prefix ESCAPE '\\'
                                OR alias.name LIKE :prefix ESCAPE '\\' THEN 1
                           ELSE 2 END) AS rank
                  FROM entities AS entity
             LEFT JOIN entity_names AS alias ON alias.entity_id = entity.entity_id
                 WHERE (entity.canonical_name LIKE :contains ESCAPE '\\'
                        OR entity.external_key LIKE :contains ESCAPE '\\'
                        OR alias.name LIKE :contains ESCAPE '\\'
                        OR entity.description LIKE :contains ESCAPE '\\')
                   AND (:type IS NULL OR entity.entity_type = :type)
                 GROUP BY entity.entity_id
                 ORDER BY rank, entity.canonical_name, entity.external_key
                 LIMIT :limit
                """,
                {"text": text, "prefix": f"{escaped}%", "contains": f"%{escaped}%", "type": entity_type, "limit": limit},
            ).fetchall()
            found = [without_nulls(row) for row in rows]
            # A tracking code is how people ask about a case: an exact code comes first.
            coded = connection.execute(
                """SELECT entity.entity_type, (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = entity.entity_id) AS name, entity.external_key, entity.description,
                          code.value_text AS tracking_code
                     FROM assertions AS code JOIN entities AS entity ON entity.entity_id = code.subject_entity_id
                    WHERE code.predicate_id = 'tracking_code' AND code.value_text = ? AND code.assertion_state = 'asserted'
                      AND code.valid_to IS NULL AND (? IS NULL OR entity.entity_type = ?)""",
                (text.strip().upper(), entity_type, entity_type),
            ).fetchone()
            if coded:
                found = [without_nulls(coded)] + [row for row in found if row.get("external_key") != coded["external_key"]]
            # An identifier (a document number, a plate) finds whoever holds it:
            # an exact text value of a current fact or of an attribute of one.
            text_predicates = [row[0] for row in connection.execute(
                "SELECT predicate_id FROM predicates WHERE value_type IN ('text', 'any')")]
            if text_predicates and len(text.strip()) >= 4:
                marks = ",".join("?" * len(text_predicates))
                holders = connection.execute(
                    f"""SELECT entity.entity_type, (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = entity.entity_id) AS name, entity.external_key, entity.description,
                               fact.predicate_id AS matched
                          FROM assertions AS fact JOIN entities AS entity ON entity.entity_id = fact.subject_entity_id
                         WHERE fact.predicate_id IN ({marks}) AND fact.value_text = ? AND fact.assertion_state = 'asserted'
                           AND fact.valid_to IS NULL
                        UNION
                        SELECT entity.entity_type, entity.canonical_name, entity.external_key, entity.description,
                               fact.predicate_id || '>' || attribute.predicate_id
                          FROM record_attributes AS attribute
                          JOIN assertions AS fact ON attribute.record_type = 'assertion'
                           AND fact.assertion_id = attribute.record_id AND fact.assertion_state = 'asserted'
                          JOIN entities AS entity ON entity.entity_id = fact.subject_entity_id
                         WHERE attribute.predicate_id IN ({marks}) AND attribute.value_text = ?
                         LIMIT 20""",
                    (*text_predicates, text.strip(), *text_predicates, text.strip()),
                ).fetchall()
                known = {row.get("external_key") for row in found}
                found = [without_nulls(row) for row in holders if row["external_key"] not in known] + found
            return found[:limit]

    def describe(self, entity_reference: str, sample: int = 10) -> dict[str, Any]:
        """Explain an entity using explicit facts rather than its identifier alone.

        Includes facts about the entity, facts that point to it (for example
        the branches that belong to a distribution center) and a summary of
        the events it takes part in, directly or through record links.
        """
        with self.connection() as connection:
            entity = self.resolve_entity(connection, entity_reference)
            facts = connection.execute(
                """
                SELECT assertion.assertion_id, predicate.predicate_id, predicate.label AS predicate_label,
                       assertion.value_type, assertion.value_text, assertion.value_integer,
                       assertion.value_real, assertion.value_boolean, assertion.value_datetime,
                       assertion.value_json, (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = value_entity.entity_id) AS value_entity_name,
                       value_entity.external_key AS value_entity_key, assertion.valid_from,
                       assertion.valid_to, assertion.assertion_state, assertion.confidence,
                       (SELECT COUNT(*) FROM assertion_evidence AS support
                         WHERE support.assertion_id = assertion.assertion_id) AS evidence_count
                  FROM assertions AS assertion
                  JOIN predicates AS predicate ON predicate.predicate_id = assertion.predicate_id
             LEFT JOIN entities AS value_entity ON value_entity.entity_id = assertion.value_entity_id
                 WHERE assertion.subject_entity_id = ?
                 ORDER BY predicate.label, assertion.valid_from, assertion.recorded_at
                """,
                (entity["entity_id"],),
            ).fetchall()
            fact_attributes = self._attributes_of(connection, "assertion", [row["assertion_id"] for row in facts])
            incoming_rows = connection.execute(
                """
                SELECT * FROM (
                    SELECT assertion.predicate_id, predicate.label AS predicate_label,
                           (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = subject.entity_id) AS subject_name, subject.external_key AS subject_key,
                           subject.entity_type AS subject_type, assertion.valid_from, assertion.valid_to,
                           assertion.assertion_state,
                           ROW_NUMBER() OVER (PARTITION BY assertion.predicate_id ORDER BY subject.canonical_name) AS position,
                           COUNT(*) OVER (PARTITION BY assertion.predicate_id) AS total
                      FROM assertions AS assertion
                      JOIN predicates AS predicate ON predicate.predicate_id = assertion.predicate_id
                      JOIN entities AS subject ON subject.entity_id = assertion.subject_entity_id
                     WHERE assertion.value_entity_id = ?
                ) WHERE position <= ?
                ORDER BY predicate_id, position
                """,
                # At least one row per predicate so its total survives --sample 0.
                (entity["entity_id"], max(sample, 1)),
            ).fetchall()
            incoming: dict[str, dict[str, Any]] = {}
            for row in incoming_rows:
                group = incoming.setdefault(
                    row["predicate_id"], {"predicate_label": row["predicate_label"], "total": row["total"], "sample": []}
                )
                if len(group["sample"]) >= sample:
                    continue
                group["sample"].append(
                    {
                        key: row[key]
                        for key in ("subject_name", "subject_key", "subject_type", "valid_from", "valid_to", "assertion_state")
                        if row[key] is not None
                    }
                )
            activity = connection.execute(
                LINKED_EVENTS_CTE
                + """
                SELECT event.event_type, linked.via, COUNT(DISTINCT event.event_id) AS events,
                       MIN(event.occurred_at) AS first_at, MAX(event.occurred_at) AS last_at
                  FROM linked JOIN events AS event ON event.event_id = linked.event_id
                 WHERE """ + active_event_sql("event") + """
                 GROUP BY event.event_type, linked.via
                 ORDER BY event.event_type, linked.via
                """,
                {"entity": entity["entity_id"]},
            ).fetchall()
            return {
                "entity": without_nulls({
                    "entity_id": entity["entity_id"],
                    "name": self._name_of(connection, entity["entity_id"]),
                    "created_as": (entity["canonical_name"]
                                   if self._name_of(connection, entity["entity_id"]) != entity["canonical_name"] else None),
                    "type": entity["entity_type"],
                    "external_key": entity["external_key"],
                    "description": entity["description"],
                    "retired": bool(entity["retired_at"]) or None,
                }),
                # Data that qualifies a fact (a document's number, a requirement's
                # mode) travels with it, like an event's attributes.
                "facts": [without_nulls({**{key: row[key] for key in row.keys() if key != "assertion_id"},
                                         "attributes": fact_attributes.get(row["assertion_id"])}) for row in facts],
                "incoming": incoming,
                "activity": [dict(row) for row in activity],
                **({"status": statuses} if (statuses := self._statuses_of(
                    connection, "entity", entity["entity_id"], entity["entity_type"])) else {}),
            }

    def timeline_page(
        self,
        entity_reference: str,
        limit: int | None = 50,
        since: str | None = None,
        until: str | None = None,
        event_type: str | None = None,
        include_voided: bool = False,
    ) -> dict[str, Any]:
        """Return the most recent linked events in chronological order.

        Voided events are left out unless include_voided is set; then they
        carry voided: true.

        Time filters compare with julianday() so values with and without a
        trailing Z denote the same instant.
        """
        with self.connection() as connection:
            entity = self.resolve_entity(connection, entity_reference)
            parameters = {
                "entity": entity["entity_id"],
                "since": query_bound(since, self.zone, "since") if since else None,
                "until": query_bound(until, self.zone, "until") if until else None,
                "type": event_type,
                "limit": -1 if limit is None else limit,
            }
            filters = """
                 WHERE (:since IS NULL OR julianday(event.occurred_at) >= julianday(:since))
                   AND (:until IS NULL OR julianday(event.occurred_at) < julianday(:until))
                   AND (:type IS NULL OR event.event_type = :type)
            """ + ("" if include_voided else " AND " + active_event_sql("event"))
            total = connection.execute(
                LINKED_EVENTS_CTE
                + "SELECT COUNT(DISTINCT event.event_id) FROM linked JOIN events AS event ON event.event_id = linked.event_id"
                + filters,
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                LINKED_EVENTS_CTE
                + """
                SELECT event.event_id, event.occurred_at, event.event_type, event.summary,
                       (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = performer.entity_id) AS performed_by_name,
                       performer.external_key AS performed_by_key,
                       (SELECT entities.canonical_name FROM event_participants AS observation
                          JOIN entities ON entities.entity_id = observation.entity_id
                         WHERE observation.event_id = event.event_id AND observation.role = 'observed_by') AS observed_by_name,
                       group_concat(DISTINCT linked.via) AS linked_as,
                       CASE WHEN """ + active_event_sql("event") + """ THEN NULL ELSE 1 END AS voided
                  FROM linked
                  JOIN events AS event ON event.event_id = linked.event_id
             LEFT JOIN event_participants AS performance
                    ON performance.event_id = event.event_id AND performance.role = 'performed_by'
             LEFT JOIN entities AS performer ON performer.entity_id = performance.entity_id
                """
                + filters
                + """
                 GROUP BY event.event_id
                 ORDER BY julianday(event.occurred_at) DESC, event.recorded_at DESC, event.rowid DESC
                 LIMIT :limit
                """,
                parameters,
            ).fetchall()
            events = [event_row({key: row[key] for key in row.keys() if row[key] is not None}) for row in reversed(rows)]
            typed = self._attributes_of(connection, "event", [event["event_id"] for event in events])
            for event in events:
                if event["event_id"] in typed:
                    event["attributes"] = {**event.get("attributes", {}), **typed[event["event_id"]]}
            return {"entity": entity["canonical_name"], "total": total, "returned": len(events), "events": events}

    def timeline(self, entity_reference: str, **filters: Any) -> list[dict[str, Any]]:
        return self.timeline_page(entity_reference, **{"limit": None, **filters})["events"]

    # ------------------------------------------------------------------
    # Reads over captured records.
    # ------------------------------------------------------------------

    @staticmethod
    def _attributes_of(connection: sqlite3.Connection, record_type: str, record_ids: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(record_ids), 500):
            chunk = record_ids[start:start + 500]
            rows = connection.execute(
                f"""SELECT attribute.record_id, attribute.predicate_id, attribute.value_type, attribute.value_text,
                           attribute.value_integer, attribute.value_real, attribute.value_boolean,
                           attribute.value_datetime, attribute.value_json,
                           (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = entity.entity_id) AS value_entity_name, attribute.unit
                      FROM record_attributes AS attribute
                 LEFT JOIN entities AS entity ON entity.entity_id = attribute.value_entity_id
                     WHERE attribute.record_type = ? AND attribute.record_id IN ({','.join('?' * len(chunk))})
                     ORDER BY attribute.recorded_at""",
                (record_type, *chunk),
            ).fetchall()
            for row in rows:
                value = {
                    "text": row["value_text"], "integer": row["value_integer"], "real": row["value_real"],
                    "boolean": None if row["value_boolean"] is None else bool(row["value_boolean"]),
                    "datetime": row["value_datetime"],
                    "json": json.loads(row["value_json"]) if row["value_json"] else None,
                    "entity": row["value_entity_name"],
                }[row["value_type"]]
                result.setdefault(row["record_id"], {})[row["predicate_id"]] = (
                    {"value": value, "unit": row["unit"]} if row["unit"] else value
                )
        return result

    def event(self, reference: str) -> dict[str, Any]:
        """Everything recorded about one event: attributes, participants, links and sources."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id = ? OR external_key = ?", (reference, reference)
            ).fetchone()
            if row is None:
                raise MemoryError(f"Unknown event: {reference}")
            event_id = row["event_id"]
            participants = connection.execute(
                """SELECT participant.role, (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = entity.entity_id) AS name, entity.external_key
                     FROM event_participants AS participant JOIN entities AS entity ON entity.entity_id = participant.entity_id
                    WHERE participant.event_id = ? ORDER BY participant.role""",
                (event_id,),
            ).fetchall()
            links = connection.execute(
                """SELECT link.record_link_id, link.predicate_id, link.target_record_type,
                          COALESCE(entity.canonical_name, target_event.external_key, link.target_record_id) AS target,
                          entity.external_key AS target_key
                     FROM record_links AS link
                LEFT JOIN entities AS entity ON link.target_record_type = 'entity' AND entity.entity_id = link.target_record_id
                LEFT JOIN events AS target_event ON link.target_record_type = 'event' AND target_event.event_id = link.target_record_id
                    WHERE link.source_record_type = 'event' AND link.source_record_id = ?
                    ORDER BY link.predicate_id, target""",
                (event_id,),
            ).fetchall()
            link_attributes = self._attributes_of(connection, "record_link", [link["record_link_id"] for link in links])
            sources = connection.execute(
                """SELECT evidence.source_type, evidence.source_reference, evidence.content, evidence.captured_at
                     FROM event_evidence JOIN evidence USING (evidence_id) WHERE event_evidence.event_id = ?""",
                (event_id,),
            ).fetchall()
            result = without_nulls(row)
            result["attributes"] = self._attributes_of(connection, "event", [event_id]).get(event_id, {})
            result["participants"] = [without_nulls(participant) for participant in participants]
            result["links"] = [
                without_nulls({
                    "predicate": link["predicate_id"], "target": link["target"], "target_key": link["target_key"],
                    "attributes": link_attributes.get(link["record_link_id"]),
                })
                for link in links
            ]
            result["sources"] = [without_nulls(source) for source in sources]
            statuses = self._statuses_of(connection, "event", event_id, row["event_type"])
            if statuses:
                result["status"] = statuses
            if not connection.execute(
                f"SELECT 1 FROM events AS target WHERE target.event_id = ? AND {active_event_sql('target')}", (event_id,)
            ).fetchone():
                void = connection.execute(
                    f"""SELECT void.external_key, void.event_id, void.occurred_at,
                               (SELECT value_text FROM record_attributes WHERE record_type = 'event'
                                   AND record_id = void.event_id AND predicate_id = 'rationale') AS reason,
                               (SELECT entities.canonical_name FROM event_participants AS actor
                                  JOIN entities ON entities.entity_id = actor.entity_id
                                 WHERE actor.event_id = void.event_id AND actor.role = 'performed_by') AS by
                          FROM record_links AS link JOIN events AS void ON void.event_id = link.source_record_id
                         WHERE link.target_record_type = 'event' AND link.target_record_id = ? AND link.predicate_id = 'voids'
                           AND void.event_type = '{VOID_EVENT}' AND {active_event_sql('void')}""",
                    (event_id,),
                ).fetchone()
                result["voided"] = without_nulls(void) if void else True
            return result

    def recall(self, text: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search over sources, event summaries, entity names, text
        facts (current and earlier versions) and long text attributes."""
        terms = [term for term in text.split() if term]
        if not terms:
            raise MemoryError("recall needs words to search")
        query = " ".join(recall_term(term) for term in terms)
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT record_type, record_id, snippet(memory_text, 2, '[', ']', '…', 16) AS snippet
                     FROM memory_text WHERE memory_text MATCH ? ORDER BY rank LIMIT ?""",
                (query, limit),
            ).fetchall()
            results = []
            for row in rows:
                item: dict[str, Any] = {"record_type": row["record_type"], "snippet": row["snippet"]}
                if row["record_type"] == "event":
                    event = connection.execute(
                        "SELECT event_type, occurred_at, external_key FROM events WHERE event_id = ?", (row["record_id"],)
                    ).fetchone()
                    item.update(without_nulls(event), event_id=row["record_id"])
                elif row["record_type"] == "entity":
                    entity = connection.execute(
                        "SELECT entity_type, canonical_name AS name, external_key FROM entities WHERE entity_id = ?",
                        (row["record_id"],),
                    ).fetchone()
                    item.update(without_nulls(entity))
                elif row["record_type"] == "assertion":
                    fact = connection.execute(
                        """SELECT entity.entity_type, (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = entity.entity_id) AS name, entity.external_key,
                                  fact.predicate_id, fact.valid_from, fact.valid_to,
                                  CASE WHEN fact.assertion_state = 'asserted' AND fact.valid_to IS NULL THEN 'current'
                                       ELSE fact.assertion_state || CASE WHEN fact.valid_to IS NOT NULL THEN ' (ended)' ELSE '' END
                                  END AS version
                             FROM assertions AS fact JOIN entities AS entity ON entity.entity_id = fact.subject_entity_id
                            WHERE fact.assertion_id = ?""",
                        (row["record_id"],),
                    ).fetchone()
                    item.update(without_nulls(fact))
                elif row["record_type"] == "attribute":
                    attribute = connection.execute(
                        "SELECT record_type AS owner_type, record_id AS owner_id, predicate_id FROM record_attributes "
                        "WHERE attribute_id = ?", (row["record_id"],)).fetchone()
                    item.update(without_nulls(attribute) if attribute else {})
                    if attribute and attribute["owner_type"] == "event":
                        event = connection.execute("SELECT event_type, external_key FROM events WHERE event_id = ?",
                                                   (attribute["owner_id"],)).fetchone()
                        item.update(without_nulls(event))
                else:
                    evidence = connection.execute(
                        "SELECT source_type, source_reference, captured_at FROM evidence WHERE evidence_id = ?",
                        (row["record_id"],),
                    ).fetchone()
                    item.update(without_nulls(evidence), evidence_id=row["record_id"])
                results.append(item)
            return results

    def outline(
        self, root: str, parent: str = "part_of", order: str = "position", as_of: str | None = None,
        depth: int | None = None, show: list[str] | None = None, limit: int = 5000,
    ) -> dict[str, Any]:
        """A structure read as a tree: the entities that are part_of the root,
        ordered by position, level by level, as it is now or as it was at an
        instant (as_of). show adds facts (title, body…) of each node; facts
        are read at the same instant, so an earlier draft reads as it was."""
        at = query_bound(as_of, self.zone, "until") if as_of else None

        def valid(alias: str) -> str:
            if at is None:
                return f"{alias}.assertion_state = 'asserted' AND {alias}.valid_to IS NULL"
            return (f"{alias}.assertion_state <> 'retracted' "
                    f"AND ({alias}.valid_from IS NULL OR julianday({alias}.valid_from) <= julianday(:at)) "
                    f"AND ({alias}.valid_to IS NULL OR julianday({alias}.valid_to) > julianday(:at))")

        with self.connection() as connection:
            top = self.resolve_entity(connection, root)
            self._predicate(connection, parent)
            self._predicate(connection, order)
            for name in show or []:
                self._predicate(connection, name)
            name_at = entity_label("child", ":at" if at else None)
            top_name = connection.execute(f"SELECT {entity_label('child', ':at' if at else None)} FROM entities AS child "
                                          "WHERE child.entity_id = :id", {"id": top["entity_id"], "at": at}).fetchone()[0]
            nodes = {top["entity_id"]: {"key": top["external_key"] or top["entity_id"], "type": top["entity_type"],
                                        "name": top_name, "children": []}}
            frontier, level, count = [top["entity_id"]], 0, 1
            while frontier and (depth is None or level < depth):
                marks = ",".join(f":n{i}" for i in range(len(frontier)))
                parameters = {f"n{i}": node for i, node in enumerate(frontier)}
                parameters.update(parent=parent, order=order, at=at)
                rows = connection.execute(
                    f"""SELECT link.value_entity_id AS parent_id, child.entity_id, child.external_key, child.entity_type,
                               {name_at} AS label,
                               (SELECT COALESCE(spot.value_real, spot.value_integer) FROM assertions AS spot
                                 WHERE spot.subject_entity_id = child.entity_id AND spot.predicate_id = :order
                                   AND {valid('spot')} ORDER BY julianday(spot.valid_from) DESC LIMIT 1) AS position
                          FROM assertions AS link JOIN entities AS child ON child.entity_id = link.subject_entity_id
                         WHERE link.predicate_id = :parent AND link.value_entity_id IN ({marks}) AND {valid('link')}
                         ORDER BY link.value_entity_id, position IS NULL, position, label""",
                    parameters,
                ).fetchall()
                count += len(rows)
                if count > limit:
                    raise MemoryError(f"The structure under {root} has more than {limit} nodes; use --depth or a deeper root")
                frontier = []
                for row in rows:
                    node = {"key": row["external_key"] or row["entity_id"], "type": row["entity_type"],
                            "name": row["label"], **({"position": row["position"]} if row["position"] is not None
                                                              else {}), "children": []}
                    nodes[row["entity_id"]] = node
                    nodes[row["parent_id"]]["children"].append(node)
                    frontier.append(row["entity_id"])
                level += 1
            if show:
                ids = list(nodes)
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    marks = ",".join(f":n{i}" for i in range(len(chunk)))
                    parameters = {f"n{i}": node for i, node in enumerate(chunk)}
                    parameters.update({f"s{i}": name for i, name in enumerate(show)}, at=at)
                    shown = ",".join(f":s{i}" for i in range(len(show)))
                    for row in connection.execute(
                        f"""SELECT fact.subject_entity_id, fact.predicate_id,
                                   COALESCE(fact.value_text, fact.value_real, fact.value_integer, fact.value_datetime,
                                            fact.value_json, fact.value_boolean) AS value
                              FROM assertions AS fact
                             WHERE fact.subject_entity_id IN ({marks}) AND fact.predicate_id IN ({shown})
                               AND {valid('fact')}
                             ORDER BY julianday(fact.valid_from)""",
                        parameters,
                    ):
                        nodes[row["subject_entity_id"]][row["predicate_id"]] = row["value"]
            for node in nodes.values():
                if not node["children"]:
                    del node["children"]
            return {"root": nodes[top["entity_id"]], "nodes": count, "as_of": at,
                    **({"truncated_at_depth": depth} if depth is not None and frontier else {})}

    def fact_history(self, entity_reference: str, predicate: str | None = None) -> list[dict[str, Any]]:
        """Every version of an entity's facts (of one predicate, or all), oldest
        first, with when each held, its state and the changeset (work session)
        and actor that wrote, closed or retracted it."""
        with self.connection() as connection:
            entity = self.resolve_entity(connection, entity_reference)
            rows = connection.execute(
                """SELECT fact.assertion_id, fact.predicate_id,
                          COALESCE(fact.value_text, fact.value_real, fact.value_integer, fact.value_datetime,
                                   fact.value_json, fact.value_boolean, target.external_key, target.canonical_name) AS value,
                          fact.valid_from, fact.valid_to, fact.assertion_state, fact.recorded_at,
                          (SELECT value_text FROM record_attributes WHERE record_type = 'assertion'
                              AND record_id = fact.assertion_id AND predicate_id = 'changeset') AS changeset,
                          (SELECT value_text FROM record_attributes WHERE record_type = 'assertion'
                              AND record_id = fact.assertion_id AND predicate_id = 'closed_in_changeset') AS closed_in,
                          (SELECT value_text FROM record_attributes WHERE record_type = 'assertion'
                              AND record_id = fact.assertion_id AND predicate_id = 'retracted_in_changeset') AS retracted_in
                     FROM assertions AS fact LEFT JOIN entities AS target ON target.entity_id = fact.value_entity_id
                    WHERE fact.subject_entity_id = ? AND (? IS NULL OR fact.predicate_id = ?)
                    ORDER BY fact.predicate_id, julianday(fact.valid_from), fact.recorded_at""",
                (entity["entity_id"], predicate, predicate),
            ).fetchall()
            sessions = {key for row in rows for key in (row["changeset"], row["closed_in"], row["retracted_in"]) if key}
            actors = {}
            for key in sessions:
                actor = connection.execute(
                    """SELECT entities.canonical_name FROM events JOIN event_participants AS actor
                         ON actor.event_id = events.event_id AND actor.role = 'performed_by'
                         JOIN entities ON entities.entity_id = actor.entity_id
                        WHERE events.external_key = ? OR events.event_id = ?""", (key, key)).fetchone()
                actors[key] = actor[0] if actor else None
            return [without_nulls({**{k: row[k] for k in row.keys() if k != "assertion_id"},
                                   "by": actors.get(row["changeset"]),
                                   "closed_by": actors.get(row["closed_in"])}) for row in rows]

    def changeset_contents(self, key: str) -> dict[str, Any]:
        """What one changeset (work session) did: facts asserted, closed and
        retracted, and events, with the entities they touched."""
        with self.connection() as connection:
            session = connection.execute(
                "SELECT * FROM events WHERE external_key = ? OR event_id = ?", (key, key)).fetchone()
            if session is None:
                raise MemoryError(f"Unknown changeset {key}: it is the key of a work-session event")
            key = session["external_key"] or session["event_id"]
            groups: dict[str, list[dict[str, Any]]] = {}
            for marker in ("changeset", "closed_in_changeset", "retracted_in_changeset"):
                rows = connection.execute(
                    """SELECT entity.external_key AS subject, entity.entity_type, fact.predicate_id,
                              substr(COALESCE(fact.value_text, CAST(COALESCE(fact.value_real, fact.value_integer) AS TEXT),
                                              target.external_key, fact.value_json), 1, 120) AS value,
                              fact.valid_from, fact.valid_to
                         FROM record_attributes AS mark
                         JOIN assertions AS fact ON mark.record_type = 'assertion' AND fact.assertion_id = mark.record_id
                         JOIN entities AS entity ON entity.entity_id = fact.subject_entity_id
                    LEFT JOIN entities AS target ON target.entity_id = fact.value_entity_id
                        WHERE mark.predicate_id = ? AND mark.value_text = ?
                        ORDER BY entity.external_key, fact.predicate_id""",
                    (marker, key),
                ).fetchall()
                groups[{"changeset": "asserted", "closed_in_changeset": "closed",
                        "retracted_in_changeset": "retracted"}[marker]] = [without_nulls(row) for row in rows]
            events = connection.execute(
                """SELECT events.external_key AS key, events.event_type, events.occurred_at, substr(events.summary, 1, 120) AS summary
                     FROM record_attributes AS mark JOIN events ON mark.record_type = 'event' AND events.event_id = mark.record_id
                    WHERE mark.predicate_id = 'changeset' AND mark.value_text = ? ORDER BY julianday(events.occurred_at)""",
                (key,),
            ).fetchall()
            touched = {item["subject"] for items in groups.values() for item in items}
            return {"changeset": key, "event_type": session["event_type"], "occurred_at": session["occurred_at"],
                    "ended_at": session["ended_at"], **groups, "events": [without_nulls(row) for row in events],
                    "entities_touched": len(touched)}

    def brief(self, limit: int = 8) -> dict[str, Any]:
        """What a new session needs first: who, what project, what is decided,
        what is open, what is pending and what happened last.

        Never fails on a missing memory, so it can run from a session hook.
        """
        if not self.database_path.exists():
            return {
                "exists": False,
                "database": str(self.database_path),
                "next_step": "There is no memory in this project. Ask the user for their name and a key "
                             "(person:<id>) and run: memory.py init --owner-name <name> --owner-key person:<id>",
            }
        with self.connection() as connection:
            owner_key = self._owner_key(connection)
            owner = connection.execute(
                "SELECT canonical_name FROM entities WHERE external_key = ? OR entity_id = ?", (owner_key, owner_key)
            ).fetchone() if owner_key else None

            def open_items(kinds: tuple[str, ...], closing: str) -> tuple[list[dict[str, Any]], int]:
                marks = ",".join("?" * len(kinds))
                condition = f"""event.event_type IN ({marks}) AND NOT EXISTS (
                    SELECT 1 FROM record_links AS closing WHERE closing.target_record_type = 'event'
                       AND closing.target_record_id = event.event_id AND closing.predicate_id = ?)
                    AND {active_event_sql('event')}
                    AND (event.event_type <> 'answer' OR EXISTS (
                        SELECT 1 FROM event_participants AS actor
                         WHERE actor.event_id = event.event_id AND actor.role = 'performed_by'))"""
                total = connection.execute(f"SELECT COUNT(*) FROM events AS event WHERE {condition}", (*kinds, closing)).fetchone()[0]
                rows = connection.execute(
                    f"""SELECT event.external_key AS key, event.event_type AS kind, event.occurred_at, event.summary AS text,
                               (SELECT value_text FROM record_attributes WHERE record_type = 'event'
                                   AND record_id = event.event_id AND predicate_id = 'rationale' LIMIT 1) AS why,
                               (SELECT CASE actor.role WHEN 'performed_by' THEN entities.canonical_name
                                            ELSE entities.canonical_name || ' (agent observation)' END
                                  FROM event_participants AS actor JOIN entities ON entities.entity_id = actor.entity_id
                                 WHERE actor.event_id = event.event_id AND actor.role IN ('performed_by', 'observed_by')) AS by,
                               (SELECT group_concat(COALESCE(entities.external_key, entities.canonical_name), ', ')
                                  FROM event_participants AS subject JOIN entities ON entities.entity_id = subject.entity_id
                                 WHERE subject.event_id = event.event_id AND subject.role = 'subject') AS about,
                               (SELECT target.external_key FROM record_links AS link
                                  JOIN events AS target ON target.event_id = link.target_record_id
                                 WHERE link.source_record_type = 'event' AND link.source_record_id = event.event_id
                                   AND link.predicate_id = 'resolves' AND link.target_record_type = 'event') AS resolves
                          FROM events AS event WHERE {condition}
                         ORDER BY julianday(event.occurred_at) DESC, event.rowid DESC LIMIT ?""",
                    (*kinds, closing, limit),
                ).fetchall()
                return [without_nulls(row) for row in rows], total

            def every(kind: str, closing: str, *columns: str) -> str:
                extra = "".join(f" --column {column}" for column in columns)
                by_person = " --where has:participant:performed_by" if kind == "answer" else ""
                return (f"memory.py list --event-type {kind} --where no:link-in:{closing}{by_person}"
                        f" --column key --column occurred_at{extra} --column summary --limit 100")

            # A person's answer to an open question is an agreement in force,
            # as binding as a decision; an agent's answer to its own question is not.
            decisions, decisions_total = open_items(("decision", "answer"), "supersedes")
            conventions, conventions_total = open_items(("convention", "preference"), "supersedes")
            questions, questions_total = open_items(("open_question",), "resolves")
            lessons, lessons_total = open_items(("lesson",), "supersedes")
            last = connection.execute(
                """SELECT event.occurred_at, attribute.value_text AS path FROM events AS event
                     LEFT JOIN record_attributes AS attribute ON attribute.record_type = 'event'
                      AND attribute.record_id = event.event_id AND attribute.predicate_id = 'backup_path'
                    WHERE event.event_type = 'memory_backed_up' ORDER BY event.occurred_at DESC LIMIT 1""").fetchone()
            last_backup = dict(last) if last else None
            # High-volume types (imports, readings, transitions) would bury what
            # matters: they leave "recent" and get one line of activity each.
            volume = self._high_volume_types(connection)
            quiet = sorted(kind for kind, flag in volume.items() if flag)
            activity = [dict(row) for row in connection.execute(
                f"""SELECT event.event_type AS type,
                           SUM(julianday(event.recorded_at) >= julianday('now', '-7 days')) AS recorded_last_7_days,
                           MAX(event.recorded_at) AS last_recorded_at
                      FROM events AS event
                     WHERE event.event_type IN ({','.join('?' * len(quiet))}) AND {active_event_sql('event')}
                     GROUP BY event.event_type ORDER BY event.event_type""", quiet)
                if row["recorded_last_7_days"]] if quiet else []
            waiting = self.pending()
            pending_total = len(waiting)
            pending = list(reversed(waiting))[:limit]
            recent = connection.execute(
                """SELECT event.external_key AS key, event.event_type AS kind, event.occurred_at,
                          substr(event.summary, 1, 160) AS text,
                          (SELECT CASE actor.role WHEN 'performed_by' THEN entities.canonical_name
                                       ELSE entities.canonical_name || ' (agent observation)' END
                             FROM event_participants AS actor JOIN entities ON entities.entity_id = actor.entity_id
                            WHERE actor.event_id = event.event_id AND actor.role IN ('performed_by', 'observed_by')) AS by
                     FROM events AS event WHERE event.event_type NOT IN (""" + ",".join("?" * len(quiet)) + """)
                      AND """ + active_event_sql("event") + """
                    ORDER BY event.recorded_at DESC, event.rowid DESC LIMIT ?""",
                (*quiet, limit),
            ).fetchall()
            projects = connection.execute(
                "SELECT canonical_name AS name, external_key, description FROM entities WHERE entity_type = 'project' ORDER BY canonical_name"
            ).fetchall()
            totals = connection.execute(
                "SELECT (SELECT COUNT(*) FROM entities), (SELECT COUNT(*) FROM events), (SELECT MAX(recorded_at) FROM events)"
            ).fetchone()
            # Schema changes of the last week, so a session notices an upgrade
            # it did not run itself.
            migrations = connection.execute(
                """SELECT event.occurred_at, event.summary AS text FROM events AS event
                    WHERE event.event_type = 'schema_migrated'
                      AND julianday(event.occurred_at) >= julianday('now', '-7 days')
                    ORDER BY julianday(event.occurred_at) DESC LIMIT 3"""
            ).fetchall()
            newer = self._require_current_schema(connection)
            return {
                "exists": True,
                "database": str(self.database_path),
                "time_zone": {"storage": "UTC", "display": zone_label(self.zone), "operating": self._operating_zones(connection)},
                "owner": {"name": owner["canonical_name"], "key": owner_key} if owner else None,
                "projects": [without_nulls(row) for row in projects],
                "totals": {"entities": totals[0], "events": totals[1], "last_recorded_at": totals[2]},
                "schema": {"read_only": bool(newer), "newer_migrations": newer,
                           "recent_migrations": [dict(row) for row in migrations]},
                # "all" is the command that lists every item when the brief shows only the latest.
                "decisions": {"open": decisions_total, "latest": decisions,
                              "all": [every("decision", "supersedes"), every("answer", "supersedes", "link:resolves")]},
                "conventions_and_preferences": {"open": conventions_total, "latest": conventions,
                                                "all": [every("convention", "supersedes"), every("preference", "supersedes")]},
                "open_questions": {"open": questions_total, "latest": questions, "all": [every("open_question", "resolves")]},
                "lessons": {"open": lessons_total, "latest": lessons, "all": [every("lesson", "supersedes")]},
                "pending_actions": {"open": pending_total, "latest": pending},
                "last_backup": last_backup,
                "high_volume_activity": activity,
                "recent": [without_nulls(row) for row in recent],
                "next_step": "Use search/describe/timeline/recall for detail and catalog for the structure.",
            }

    def pending(self, person_reference: str | None = None) -> list[dict[str, Any]]:
        """What waits on someone: every current pending_action_for, with the
        step of its process (or its workflow state). For a person it also
        includes what waits on the roles that person holds now."""
        with self.connection() as connection:
            holders: tuple[str, ...] = ()
            if person_reference:
                person = self.resolve_entity(connection, person_reference)
                roles = connection.execute(
                    """SELECT value_entity_id FROM assertions WHERE subject_entity_id = ? AND predicate_id = 'holds_role'
                        AND assertion_state = 'asserted' AND valid_to IS NULL""", (person["entity_id"],)).fetchall()
                holders = (person["entity_id"], *(row[0] for row in roles))
            filter_sql = f"AND pending.value_entity_id IN ({','.join('?' * len(holders))})" if holders else ""
            rows = connection.execute(
                f"""SELECT entity.external_key AS entity_key, {entity_label('entity')} AS entity_name,
                           holder.external_key AS pending_for_key, {entity_label('holder')} AS pending_for_name,
                           holder.entity_type AS pending_for_type,
                           COALESCE((SELECT value_text FROM assertions WHERE subject_entity_id = entity.entity_id
                                        AND predicate_id = 'process_step' AND assertion_state = 'asserted'
                                        AND valid_to IS NULL),
                                    (SELECT state_name FROM memory_current_states WHERE entity_id = entity.entity_id))
                               AS step,
                           (SELECT value_text FROM record_attributes WHERE record_type = 'assertion'
                              AND record_id = pending.assertion_id AND predicate_id = 'requested_action') AS requested_action,
                           pending.valid_from
                      FROM assertions AS pending
                      JOIN entities AS entity ON entity.entity_id = pending.subject_entity_id
                      JOIN entities AS holder ON holder.entity_id = pending.value_entity_id
                     WHERE pending.predicate_id = 'pending_action_for' AND pending.assertion_state = 'asserted'
                       AND pending.valid_to IS NULL {filter_sql}""",
                holders,
            ).fetchall()
            # An event waits on its pending_action_for participant until an
            # event in force resolves it (link resolves) or it is voided.
            event_filter = f"AND waiting.entity_id IN ({','.join('?' * len(holders))})" if holders else ""
            events = connection.execute(
                f"""SELECT event.external_key AS event_key, event.event_type, event.summary,
                           holder.external_key AS pending_for_key, {entity_label('holder')} AS pending_for_name,
                           holder.entity_type AS pending_for_type,
                           (SELECT value_text FROM record_attributes WHERE record_type = 'event'
                              AND record_id = event.event_id AND predicate_id = 'requested_action') AS requested_action,
                           (SELECT group_concat(COALESCE(about.external_key, about.entity_id), ', ')
                              FROM event_participants AS role JOIN entities AS about ON about.entity_id = role.entity_id
                             WHERE role.event_id = event.event_id AND role.role = 'subject') AS about,
                           event.occurred_at AS valid_from
                      FROM event_participants AS waiting
                      JOIN events AS event ON event.event_id = waiting.event_id
                      JOIN entities AS holder ON holder.entity_id = waiting.entity_id
                     WHERE waiting.role = 'pending_action_for' {event_filter}
                       AND {active_event_sql('event')}
                       AND NOT EXISTS (SELECT 1 FROM record_links AS answer
                                         JOIN events AS resolver ON resolver.event_id = answer.source_record_id
                                        WHERE answer.target_record_type = 'event' AND answer.target_record_id = event.event_id
                                          AND answer.predicate_id = 'resolves' AND {active_event_sql('resolver')})""",
                holders,
            ).fetchall()
            items = [without_nulls(row) for row in [*rows, *events]]
            return sorted(items, key=lambda item: item["valid_from"] or "")
