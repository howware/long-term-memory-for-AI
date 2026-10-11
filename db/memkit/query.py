"""Queries: the path compiler, status rules, recipes, aggregate and list."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

from .base import DATE_LITERAL, NOW_SQL, MemoryError, active_event_sql, in_force_sql, display_zone, parse_instant, query_bound, same_zone, shift_instant, utc_now, without_nulls, zone_label
from .rules import CONDITION_OPERATORS, FIRST_CHECKS, MULTIPLE, PLACEHOLDER, STATUS_OPERATORS, VALUE_COLUMNS, column_for, parse_condition, split_path, validate_recipe, validate_status_rule


class QueryMixin:
    """Queries: the path compiler, status rules, recipes, aggregate and list."""

    @staticmethod
    def _recipe_summaries(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT entities.external_key AS key, entities.canonical_name AS name, assertion.value_json
                 FROM entities JOIN assertions AS assertion ON assertion.subject_entity_id = entities.entity_id
                WHERE entities.entity_type = 'query_recipe' AND assertion.predicate_id = 'recipe_definition'
                  AND assertion.assertion_state = 'asserted' AND assertion.valid_to IS NULL
                ORDER BY entities.external_key"""
        ).fetchall()
        summaries = []
        for row in rows:
            definition = json.loads(row["value_json"])
            summaries.append(without_nulls({
                "key": row["key"], "name": row["name"], "kind": definition.get("kind"),
                "description": definition.get("description"),
                "parameters": {name: {k: v for k, v in spec.items() if k in ("type", "default", "description")}
                               for name, spec in (definition.get("parameters") or {}).items()},
            }))
        return summaries

    # ------------------------------------------------------------------
    # Declarative aggregation: parameters compile to SQL; no stored SQL runs.
    # ------------------------------------------------------------------

    PERIODS = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m", "year": "%Y"}

    def define_status_rule(self, key: str, name: str, definition: dict[str, Any], description: str | None = None) -> dict[str, Any]:
        """Store (or version) a declarative status rule as data."""
        validate_status_rule(definition)
        if not key.startswith("status:"):
            raise MemoryError("Status rule keys look like status:<name>")
        with self.connection() as connection:
            self._predicate(connection, definition["follow_link"])
            current = connection.execute(
                """SELECT assertion.valid_from FROM assertions AS assertion
                     JOIN entities ON entities.entity_id = assertion.subject_entity_id
                    WHERE entities.external_key = ? AND assertion.predicate_id = 'rule_definition'
                      AND assertion.assertion_state = 'asserted' AND assertion.valid_to IS NULL""",
                (key,),
            ).fetchone()
        # Validity is compared per second: a redefinition in the same second
        # waits for the next one instead of inventing a later start time.
        while current and current["valid_from"] and utc_now()[:19] <= current["valid_from"][:19]:
            time.sleep(0.05)
        report = self.write({
            "source": {"type": "rule_definition", "content": json.dumps({"key": key, "definition": definition},
                                                                        ensure_ascii=False, sort_keys=True)},
            "entities": [{"key": key, "type": "status_rule", "name": name, "description": description}],
            "facts": [{"subject": key, "predicate": "rule_definition", "value": definition, "valid_from": utc_now()}],
        })
        return {"key": key, "facts": report["facts"], "warnings": report["warnings"], "rule": self.describe(key)}

    def _status_rule(self, connection: sqlite3.Connection, key: str) -> dict[str, Any]:
        row = connection.execute(
            """SELECT assertion.value_json FROM assertions AS assertion
                 JOIN entities ON entities.entity_id = assertion.subject_entity_id
                WHERE entities.external_key = ? AND entities.entity_type = 'status_rule'
                  AND assertion.predicate_id = 'rule_definition' AND assertion.assertion_state = 'asserted'
                  AND assertion.valid_to IS NULL""",
            (key,),
        ).fetchone()
        if row is None:
            raise MemoryError(f"Unknown status rule {key}; see catalog (semantic_rules_and_recipes)")
        return validate_status_rule(json.loads(row["value_json"]))

    def _status_sql(
        self, connection: sqlite3.Connection, key: str, subject: str, parameters: dict[str, Any], prefix: str,
        rows_type: str | None, as_of: str | None,
    ) -> str:
        """Compile a status rule into a CASE over the subject's follow-up events."""
        rule = self._status_rule(connection, key)
        kind, subject_type = next(iter(rule["applies_to"].items()))
        if rows_type is not None and rows_type != subject_type:
            raise MemoryError(f"{key} applies to {kind} {subject_type}, not to {rows_type}")
        record_kind = "event" if kind == "event_type" else "entity"
        parameters[f"{prefix}link"] = rule["follow_link"]
        if as_of:
            parameters[f"{prefix}asof"] = as_of
        cases = []
        for index, state in enumerate(rule["states"]):
            follow = f"{prefix}f{index}"
            parameters[f"{prefix}e{index}"] = state["event_type"]
            parameters[f"{prefix}n{index}"] = state["name"]
            conditions = [
                f"{prefix}l{index}.source_record_type = 'event'",
                f"{prefix}l{index}.target_record_type = '{record_kind}'",
                f"{prefix}l{index}.target_record_id = {subject}",
                f"{prefix}l{index}.predicate_id = :{prefix}link",
                active_event_sql(follow),
            ]
            if as_of:
                conditions.append(f"julianday({follow}.occurred_at) < julianday(:{prefix}asof)")
            for position, (path, comparison) in enumerate((state.get("where") or {}).items()):
                # Keys are paths from the follow-up event; a bare name means attr:NAME.
                path = path if ":" in path else f"attr:{path}"
                operator, operand = next(iter(comparison.items()))
                tag = f"{prefix}c{index}_{position}"
                if isinstance(operand, dict):
                    # Compare with the subject's own attribute (or current fact).
                    compiled = self._compile_path(connection, path, parameters, tag, f"{follow}.event_id", "'event'",
                                                  f"{follow}.occurred_at", at=f":{prefix}asof" if as_of else None)
                    subject_predicate = self._predicate(connection, operand["subject_attr"])
                    parameters[f"{tag}s"] = operand["subject_attr"]
                    column = column_for("subject_value", subject_predicate["value_type"])
                    if record_kind == "event":
                        right = (f"(SELECT {column} FROM record_attributes AS subject_value "
                                 f"WHERE subject_value.record_type = 'event' AND subject_value.record_id = {subject} "
                                 f"AND subject_value.predicate_id = :{tag}s LIMIT 1)")
                    else:
                        right = (f"(SELECT {column} FROM assertions AS subject_value "
                                 f"WHERE subject_value.subject_entity_id = {subject} AND subject_value.predicate_id = :{tag}s "
                                 f"AND {in_force_sql('subject_value', f':{prefix}asof' if as_of else None)} LIMIT 1)")
                    left = compiled["value"]
                    if compiled["value_type"] == "datetime":
                        left, right = f"julianday({left})", f"julianday({right})"
                    conditions.append(f"EXISTS (SELECT 1 {compiled['joins']} WHERE {left} {STATUS_OPERATORS[operator]} {right})")
                else:
                    conditions.append(self._condition_sql(
                        connection, {"path": path, "op": operator, "value": operand}, parameters, tag,
                        f"{follow}.event_id", "'event'", f"{follow}.occurred_at", from_index=False,
                        at=f":{prefix}asof" if as_of else None,
                    ))
            cases.append(
                f"WHEN EXISTS (SELECT 1 FROM record_links AS {prefix}l{index} "
                f"JOIN events AS {follow} ON {follow}.event_id = {prefix}l{index}.source_record_id "
                f"AND {follow}.event_type = :{prefix}e{index} WHERE {' AND '.join(conditions)}) THEN :{prefix}n{index}"
            )
        parameters[f"{prefix}other"] = rule["otherwise"]
        return f"(CASE {' '.join(cases)} ELSE :{prefix}other END)"

    def define_recipe(self, key: str, name: str, definition: dict[str, Any], description: str | None = None) -> dict[str, Any]:
        """Store (or version) a saved query with typed parameters."""
        validate_recipe(definition)
        if not key.startswith("recipe:"):
            raise MemoryError("Recipe keys look like recipe:<name>")
        with self.connection() as connection:
            current = connection.execute(
                """SELECT assertion.valid_from FROM assertions AS assertion
                     JOIN entities ON entities.entity_id = assertion.subject_entity_id
                    WHERE entities.external_key = ? AND assertion.predicate_id = 'recipe_definition'
                      AND assertion.assertion_state = 'asserted' AND assertion.valid_to IS NULL""",
                (key,),
            ).fetchone()
        while current and current["valid_from"] and utc_now()[:19] <= current["valid_from"][:19]:
            time.sleep(0.05)
        report = self.write({
            "source": {"type": "recipe_definition", "content": json.dumps({"key": key, "definition": definition},
                                                                          ensure_ascii=False, sort_keys=True)},
            "entities": [{"key": key, "type": "query_recipe", "name": name,
                          "description": description or definition.get("description")}],
            "facts": [{"subject": key, "predicate": "recipe_definition", "value": definition, "valid_from": utc_now()}],
        })
        return {"key": key, "facts": report["facts"], "warnings": report["warnings"]}

    def _recipe(self, connection: sqlite3.Connection, key: str) -> dict[str, Any]:
        row = connection.execute(
            """SELECT assertion.value_json FROM assertions AS assertion
                 JOIN entities ON entities.entity_id = assertion.subject_entity_id
                WHERE entities.external_key = ? AND entities.entity_type = 'query_recipe'
                  AND assertion.predicate_id = 'recipe_definition' AND assertion.assertion_state = 'asserted'
                  AND assertion.valid_to IS NULL""",
            (key,),
        ).fetchone()
        if row is None:
            raise MemoryError(f"Unknown recipe {key}; see catalog (recipes)")
        return validate_recipe(json.loads(row["value_json"]))

    def _parameter_value(self, connection: sqlite3.Connection, name: str, spec: dict[str, Any], raw: Any) -> Any:
        """Validate one recipe parameter by type; relative dates use the display zone."""
        kind = spec["type"]
        if kind in ("date", "datetime"):
            text = str(raw).strip().lower()
            match = re.fullmatch(r"(today|now)([+-]\d+)?", text)
            if match:
                base = datetime.now(self.zone) if self.zone else datetime.now().astimezone()
                moment = base + timedelta(days=int(match.group(2) or 0))
                return moment.date() if match.group(1) == "today" or kind == "date" else moment.replace(microsecond=0)
            try:
                parsed = parse_instant(str(raw).strip())
            except ValueError as error:
                raise MemoryError(f"Parameter {name} expects a date, got {raw!r}") from error
            return parsed.date() if kind == "date" or len(str(raw).strip()) == 10 else parsed
        if kind in ("integer", "real"):
            try:
                number = float(raw)
            except (TypeError, ValueError) as error:
                raise MemoryError(f"Parameter {name} expects a number, got {raw!r}") from error
            return int(number) if kind == "integer" else number
        if kind == "entity":
            entity = self.resolve_entity(connection, str(raw))
            return entity["external_key"] or entity["entity_id"]
        text = str(raw)
        # A text value cannot change the structure of a condition.
        if any(token in text for token in (*CONDITION_OPERATORS, "|", "..", "\n", "{", "}")):
            raise MemoryError(f"Parameter {name} contains operator characters: {raw!r}")
        return text

    def run_recipe(self, key: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a saved query with typed, validated parameters."""
        given = dict(parameters or {})
        with self.connection() as connection:
            recipe = self._recipe(connection, key)
            declared = recipe.get("parameters") or {}
            unknown = set(given) - set(declared)
            if unknown:
                raise MemoryError(f"{key} has no parameter {', '.join(sorted(unknown))}; it takes {', '.join(declared) or 'none'}")
            values: dict[str, Any] = {}
            for name, spec in declared.items():
                if name in given:
                    raw = given[name]
                elif "default" in spec:
                    raw = spec["default"]
                else:
                    raise MemoryError(f"{key} needs --param {name}=... ({spec['type']})")
                values[name] = self._parameter_value(connection, name, spec, raw)

        def render(match: re.Match) -> str:
            value, offset = values[match.group(1)], int(match.group(2) or 0)
            if isinstance(value, datetime):
                return (value + timedelta(days=offset)).isoformat()
            if hasattr(value, "isoformat"):
                return (value + timedelta(days=offset)).isoformat()
            if isinstance(value, (int, float)):
                return str(value + offset)
            if offset:
                raise MemoryError(f"{match.group(0)}: only dates and numbers take an offset")
            return str(value)

        def fill(item: Any) -> Any:
            if isinstance(item, str):
                return PLACEHOLDER.sub(render, item)
            if isinstance(item, list):
                return [fill(element) for element in item]
            if isinstance(item, dict):
                return {name: fill(element) for name, element in item.items()}
            return item

        query = fill(recipe["query"])
        if recipe["kind"] == "aggregate":
            event_type = query.pop("event_type", None)
            result = self.aggregate(event_type, **query)
        else:
            result = self.list_rows(**query)
        return {"recipe": key, "parameters": {name: (value.isoformat() if hasattr(value, "isoformat") else value)
                                              for name, value in values.items()},
                "query": query, "result": result}

    def _statuses_of(self, connection: sqlite3.Connection, record_kind: str, record_id: str, subject_type: str) -> dict[str, str]:
        """Current value of every status rule that applies to one event or entity."""
        kind = "event_type" if record_kind == "event" else "entity_type"
        statuses = {}
        for rule in connection.execute(
            """SELECT entities.external_key, assertion.value_json FROM entities
                 JOIN assertions AS assertion ON assertion.subject_entity_id = entities.entity_id
                WHERE entities.entity_type = 'status_rule' AND assertion.predicate_id = 'rule_definition'
                  AND assertion.assertion_state = 'asserted' AND assertion.valid_to IS NULL"""
        ).fetchall():
            if json.loads(rule["value_json"]).get("applies_to", {}).get(kind) != subject_type:
                continue
            parameters: dict[str, Any] = {"subject": record_id}
            sql = self._status_sql(connection, rule["external_key"], ":subject", parameters, "s", None, None)
            statuses[rule["external_key"]] = connection.execute(f"SELECT {sql}", parameters).fetchone()[0]
        return statuses

    def _compile_path(
        self,
        connection: sqlite3.Connection,
        path: str,
        parameters: dict[str, Any],
        prefix: str,
        start: str,
        start_type: str,
        instant: str | None,
        line_target: str | None = None,
        row: tuple[str, str] | None = None,
        at: str | None = None,
    ) -> dict[str, Any]:
        """Compile a path into correlated joins ending in an entity or a typed value.

        Steps: link:P (forward), link-in:P[TYPE] (events pointing here),
        fact:P (value in force: now, or at the query's --as-of instant `at`),
        fact@row:P (value in force at the row's instant), participant:ROLE,
        attr:A (attribute of the current record), edge:A (attribute of the
        link or fact just followed: a line's quantity, a presented document's
        number), since / until (when the fact just followed began or ended:
        how long a case has been in its step); 'line' starts at the line
        target; first(A | B | ...) takes the first of several paths that has
        a value. Returns joins, the value expression, whether it is an entity
        or a value, its type, whether it may yield several values, the
        predicate it ends in and, when its several values are events, the
        alias that orders them.
        """
        if path.startswith("first("):
            return self._compile_first(connection, path, parameters, prefix, start, start_type, instant,
                                       line_target, row, at)
        steps = split_path(path)
        # Inside a path every attribute or fact belongs to one known record, so
        # the per-record indexes are pinned (INDEXED BY): with a value
        # condition SQLite could otherwise pick a value index and scan it per row.
        # CROSS JOIN pins the hop order: SQLite otherwise may start from the
        # last hop's predicate and scan all its facts for every row.
        joins = ["FROM (SELECT 1) AS seed"]
        if steps[0] == "line":
            if line_target is None:
                raise MemoryError("a path starting at line needs --line PREDICATE")
            current, current_type, steps = line_target, "'entity'", steps[1:]
            row = None
        else:
            current, current_type = start, start_type
        multiple, last_edge, last_fact = False, None, None
        # Several values that come from events (link-in) can be ordered by
        # time; any other source of several values cannot.
        event_order, other_multiple = None, False

        def result(value: str, kind: str, value_type: str, several: bool, predicate: str | None = None) -> dict[str, Any]:
            # record_type: SQL expression of what an entity-kind value is ('entity' or 'event'),
            # so a path can continue from it (first(A | B)/key).
            return {"joins": " ".join(joins), "value": value, "kind": kind, "value_type": value_type,
                    "multiple": several, "predicate": predicate, "event_order": event_order,
                    "other_multiple": other_multiple or (several and event_order is None),
                    "record_type": current_type if kind == "entity" else None}

        for index, step in enumerate(steps):
            final = index == len(steps) - 1
            kind, _, name = step.partition(":")
            alias, parameter = f"{prefix}h{index}", f"{prefix}p{index}"
            if step in ("key", "name", "summary"):
                if not final:
                    raise MemoryError(f"{step} is a value; the path cannot continue after it")
                if current is None:
                    raise MemoryError(f"{step} must follow an entity or an event")
                column = {"key": ("COALESCE(external_key, entity_id)", "COALESCE(external_key, event_id)"),
                          "name": ("name", "summary"), "summary": ("description", "summary")}[step]
                # Aliased: the row itself may come from entities or events, and an
                # unaliased inner table would shadow it. Names come from
                # entity_labels: a renamed entity reads by its name in force.
                source_table = "entity_labels" if step == "name" else "entities"
                value = (f"(CASE {current_type} WHEN 'entity' THEN (SELECT {column[0]} FROM {source_table} AS {alias}k "
                         f"WHERE {alias}k.entity_id = {current}) WHEN 'event' THEN (SELECT {column[1]} FROM events AS {alias}v "
                         f"WHERE {alias}v.event_id = {current}) END)")
                return result(value, "value", "text", multiple)
            if step in ("since", "until"):
                if last_fact is None:
                    raise MemoryError(f"{step} must follow a fact step, for example fact:process_step/{step}")
                if not final:
                    raise MemoryError(f"{step} is a value; the path cannot continue after it")
                column = "valid_from" if step == "since" else "valid_to"
                return result(f"{last_fact}.{column}", "value", "datetime", multiple)
            if not name:
                raise MemoryError(f"Path step {step!r} needs a name, for example link:{step}")
            if kind == "link":
                predicate = self._predicate(connection, name)
                parameters[parameter] = name
                joins.append(
                    f"CROSS JOIN record_links AS {alias} ON {alias}.source_record_type = {current_type} "
                    f"AND {alias}.source_record_id = {current} AND {alias}.predicate_id = :{parameter}"
                )
                # When every stored link of P points to one kind of record, say so: a path that
                # continues (first(A | B)/key) then needs no per-row test of what it reached.
                targets = [row[0] for row in connection.execute(
                    "SELECT DISTINCT target_record_type FROM record_links WHERE predicate_id = ? LIMIT 2", (name,))]
                current = f"{alias}.target_record_id"
                current_type = f"'{targets[0]}'" if len(targets) == 1 else f"{alias}.target_record_type"
                last_edge, last_fact = ("'record_link'", f"{alias}.record_link_id"), None
                if predicate["cardinality"] != "one":
                    multiple = other_multiple = True
            elif kind == "link-in":
                name, _, event_type = name.partition("[")
                self._predicate(connection, name)
                parameters[parameter] = name
                event_filter = ""
                if event_type:
                    parameters[f"{parameter}t"] = event_type.rstrip("]")
                    event_filter = f" AND {alias}e.event_type = :{parameter}t"
                if at is not None:
                    # As of an instant, only events that had already happened.
                    event_filter += f" AND julianday({alias}e.occurred_at) < julianday({at})"
                joins.append(
                    f"CROSS JOIN record_links AS {alias} ON {alias}.target_record_type = {current_type} "
                    f"AND {alias}.target_record_id = {current} AND {alias}.predicate_id = :{parameter} "
                    f"AND {alias}.source_record_type = 'event'"
                )
                joins.append(
                    f"CROSS JOIN events AS {alias}e ON {alias}e.event_id = {alias}.source_record_id{event_filter} "
                    f"AND {active_event_sql(alias + 'e')}"
                )
                current, current_type, multiple, event_order = f"{alias}e.event_id", "'event'", True, f"{alias}e"
                last_edge, last_fact = ("'record_link'", f"{alias}.record_link_id"), None
            elif kind in ("fact", "fact@row"):
                predicate = self._predicate(connection, name)
                parameters[parameter] = name
                if kind == "fact":
                    validity = in_force_sql(alias, at)
                else:
                    if instant is None:
                        raise MemoryError("fact@row needs rows with an instant (events or their lines)")
                    validity = in_force_sql(alias, instant)
                joins.append(
                    f"CROSS JOIN assertions AS {alias} INDEXED BY assertions_subject_predicate_time ON {alias}.subject_entity_id = {current} "
                    f"AND {alias}.predicate_id = :{parameter} AND {validity}"
                )
                if predicate["cardinality"] != "one":
                    multiple = other_multiple = True
                last_edge, last_fact = ("'assertion'", f"{alias}.assertion_id"), alias
                if predicate["value_type"] == "entity":
                    current, current_type = f"{alias}.value_entity_id", "'entity'"
                elif not final and (steps[index + 1].startswith("edge:") or steps[index + 1] in ("since", "until")):
                    current, current_type = None, None  # only the fact's own attributes may follow
                else:
                    if not final:
                        raise MemoryError(f"{name} holds {predicate['value_type']} values; the path cannot continue after it")
                    return result(column_for(alias, predicate["value_type"]), "value", predicate["value_type"],
                                  multiple, name)
            elif kind == "participant":
                parameters[parameter] = name
                joins.append(
                    f"CROSS JOIN event_participants AS {alias} ON {alias}.event_id = {current} AND {alias}.role = :{parameter}"
                )
                current, current_type, last_edge, last_fact = f"{alias}.entity_id", "'entity'", None, None
                multiple = other_multiple = True
            elif kind in ("attr", "edge"):
                if not final:
                    raise MemoryError(f"{step} is a value; the path cannot continue after it")
                predicate = self._predicate(connection, name)
                parameters[parameter] = name
                if kind == "edge":
                    if last_edge is None:
                        raise MemoryError("edge:A must follow a link, link-in or fact step")
                    record_type, record_id = last_edge
                elif index == 0 and row is not None:
                    record_type, record_id = row
                elif current_type == "'entity'":
                    raise MemoryError(f"{current} is an entity: use fact:{name} for its values")
                else:
                    record_type, record_id = current_type, current
                if record_type == "'entity'":
                    joins.append(
                        f"CROSS JOIN assertions AS {alias} INDEXED BY assertions_subject_predicate_time ON {alias}.subject_entity_id = {record_id} "
                        f"AND {alias}.predicate_id = :{parameter} AND {in_force_sql(alias, at)}"
                    )
                else:
                    joins.append(
                        f"CROSS JOIN record_attributes AS {alias} INDEXED BY record_attributes_record_lookup ON {alias}.record_type = {record_type} "
                        f"AND {alias}.record_id = {record_id} AND {alias}.predicate_id = :{parameter}"
                    )
                value_type = predicate["value_type"]
                # An event's or link's attributes hold one value per name (they are
                # written as a mapping); an entity's facts follow the predicate.
                if record_type == "'entity'" and predicate["cardinality"] != "one":
                    multiple = other_multiple = True
                return result(column_for(alias, value_type), "entity" if value_type == "entity" else "value",
                              value_type, multiple, name)
            else:
                raise MemoryError(
                    f"Unknown path step {step!r}; use link:P, link-in:P[TYPE], fact:P, fact@row:P, participant:ROLE, "
                    "attr:A, edge:A, since, until, key, name, summary, first(A | B) or start with line"
                )
        return result(current, "entity", "entity", multiple)

    def _run_first_checks(self, connection: sqlite3.Connection, ctes: str, parameters: dict[str, Any]) -> None:
        """Reject a first(...) whose chosen path reaches several values for some
        row of this query (the same rule as --by), before running it."""
        for path, expression in parameters.pop(FIRST_CHECKS, []):
            if connection.execute(f"{ctes} SELECT 1 FROM rows WHERE {expression} = :multiple LIMIT 1",
                                  parameters).fetchone():
                raise MemoryError(
                    f"{path} reaches several values for some rows (a path other than link-in gave more than one); "
                    "filter first, narrow that path or use a single-valued one")

    def _compile_first(
        self, connection: sqlite3.Connection, path: str, parameters: dict[str, Any], prefix: str, start: str,
        start_type: str, instant: str | None, line_target: str | None, row: tuple[str, str] | None, at: str | None,
    ) -> dict[str, Any]:
        """first(A | B | ...): the value of the first path that has one for the
        row. Every path must end in the same kind of value. A path whose
        several values come from events (link-in) gives the most recent of
        them (voided events never count); any other path that may give
        several values is rejected. The path may continue after the closing
        parenthesis when it ends in an entity: first(A | B)/fact:part_of."""
        depth, close = 0, None
        for position, character in enumerate(path):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    close = position
                    break
        if close is None:
            raise MemoryError(f"first( needs its closing parenthesis: {path!r}")
        inner, rest = path[len("first("):close], path[close + 1:]
        alternatives, depth, current = [], 0, ""
        for character in inner:
            depth += (character == "(") - (character == ")")
            if character == "|" and depth == 0:
                alternatives.append(current.strip())
                current = ""
            else:
                current += character
        alternatives.append(current.strip())
        if len(alternatives) < 2 or not all(alternatives):
            raise MemoryError("first() takes two or more paths separated by |, for example first(link:a | link:b)")
        values, kinds, runtime_check, record_types = [], set(), False, set()
        for number, alternative in enumerate(alternatives):
            compiled = self._compile_path(connection, alternative, parameters, f"{prefix}a{number}", start, start_type,
                                          instant, line_target, row, at)
            kinds.add((compiled["kind"], compiled["value_type"]))
            record_types.add(compiled["record_type"])
            if compiled["other_multiple"]:
                # Like --by: rejected only when some row really reaches several values. That is
                # checked before the query runs (_run_first_checks), which needs the query's rows;
                # conditions and status rules have none, so there it is rejected now.
                if start != "rows.event_id":
                    raise MemoryError(
                        f"first(): {alternative} may give several values for a row; in a condition only several "
                        "events (link-in) can be chosen from (the most recent wins). Narrow the path, or use it "
                        "in --by, --column or a measure, where it is checked row by row")
                values.append(f"(SELECT CASE WHEN COUNT(DISTINCT {compiled['value']}) > 1 THEN :multiple "
                              f"ELSE MIN({compiled['value']}) END {compiled['joins']} "
                              f"WHERE {compiled['value']} IS NOT NULL)")
                runtime_check = True
            else:
                order = compiled["event_order"]
                ordering = f" ORDER BY julianday({order}.occurred_at) DESC, {order}.rowid DESC" if order else ""
                values.append(f"(SELECT {compiled['value']} {compiled['joins']} WHERE {compiled['value']} IS NOT NULL"
                              f"{ordering} LIMIT 1)")
            last_predicate = compiled["predicate"]
        if len(kinds) > 1:
            found = ", ".join(sorted(value_type for _, value_type in kinds))
            raise MemoryError(f"first(): every path must end in the same kind of value; these end in {found}")
        kind, value_type = kinds.pop()
        expression = f"COALESCE({', '.join(values)})"
        if runtime_check:
            parameters.setdefault(FIRST_CHECKS, []).append((path, expression))
        rest = rest.lstrip("/>")
        if not rest:
            return {"joins": "FROM (SELECT 1) AS seed", "value": expression, "kind": kind, "value_type": value_type,
                    "multiple": False, "predicate": last_predicate, "event_order": None, "other_multiple": False}
        if kind != "entity":
            raise MemoryError(f"first(...) gives {value_type} values; a path can only continue after an entity")
        # The chosen value may be an entity or an event (link:P to an event, link-in:P).
        # One known type continues as it is; mixed ones are told apart per row.
        record_type = next(iter(record_types)) if len(record_types) == 1 else None
        if record_type not in ("'entity'", "'event'"):
            record_type = f"(CASE WHEN EXISTS (SELECT 1 FROM events WHERE event_id = {expression}) THEN 'event' ELSE 'entity' END)"
        return self._compile_path(connection, rest, parameters, f"{prefix}r", expression, record_type, instant,
                                  None, None, at)

    def _literal(self, connection: sqlite3.Connection, value_type: str, raw: Any) -> Any:
        """Convert a condition value to the path's type."""
        if value_type == "entity":
            return self.resolve_entity(connection, str(raw))["entity_id"]
        if value_type == "datetime":
            return query_bound(str(raw), self.zone, "value")
        if value_type in ("integer", "real"):
            try:
                number = float(raw)
            except (TypeError, ValueError) as error:
                raise MemoryError(f"{raw!r} is not a number") from error
            return int(number) if value_type == "integer" and number.is_integer() else number
        if value_type == "boolean":
            if isinstance(raw, bool):
                return int(raw)
            text = str(raw).strip().lower()
            if text in ("true", "1", "sí", "si", "yes"):
                return 1
            if text in ("false", "0", "no"):
                return 0
            raise MemoryError(f"{raw!r} is not true or false")
        if value_type == "json":
            raise MemoryError("JSON values can only be tested with has: or no:")
        if value_type == "any" and not isinstance(raw, (int, float)):
            try:
                return float(raw)
            except (TypeError, ValueError):
                return raw
        return raw

    def _anchored_condition_sql(
        self, connection: sqlite3.Connection, condition: dict[str, Any], parameters: dict[str, Any], prefix: str,
        start: str, start_type: str, at: str | None = None,
    ) -> str | None:
        """Positive conditions on the row's own attribute, an entity's current
        fact or a direct link are answered from the value indexes first:
        start IN (rows whose value matches) instead of a lookup per row."""
        path, operator = condition["path"], condition["op"]
        if len(split_path(path)) > 1 or condition.get("quantifier") == "all" or operator in ("!=", "no"):
            return None
        kind, _, name = path.partition(":")
        entity_rows = start_type == "'entity'"
        if kind == "link" and operator in ("=", "in"):
            options = condition["value"] if isinstance(condition["value"], list) else (
                str(condition["value"]).split("|") if operator == "in" else [condition["value"]])
            self._predicate(connection, name)
            parameters[f"{prefix}p"] = name
            names = []
            for position, option in enumerate(options):
                parameters[f"{prefix}v{position}"] = self.resolve_entity(connection, str(option))["entity_id"]
                names.append(f":{prefix}v{position}")
            return (f"{start} IN (SELECT source_record_id FROM record_links WHERE target_record_type = 'entity' "
                    f"AND target_record_id IN ({', '.join(names)}) AND predicate_id = :{prefix}p "
                    f"AND source_record_type = {start_type})")
        if kind == "fact" and entity_rows or kind == "attr":
            predicate = self._predicate(connection, name)
            value_type = predicate["value_type"]
            if value_type not in VALUE_COLUMNS or value_type == "json":
                return None
            parameters[f"{prefix}p"] = name
            facts = kind == "fact" or entity_rows
            if not facts:
                table, key = "record_attributes", "record_id"
                scope = f"record_type = {start_type}"
            column = VALUE_COLUMNS[value_type]
            tests = []
            if operator == "has":
                tests.append(f"{column} IS NOT NULL")
            elif operator == "between":
                low, separator, high = str(condition["value"]).partition("..")
                if not separator:
                    raise MemoryError("between needs LOW..HIGH (the upper bound is excluded)")
                parameters[f"{prefix}lo"] = self._literal(connection, value_type, low)
                parameters[f"{prefix}hi"] = self._literal(connection, value_type, high)
                tests += self._range_tests(column, value_type, ">=", f"{prefix}lo", parameters)
                tests += self._range_tests(column, value_type, "<", f"{prefix}hi", parameters)
            elif operator == "in":
                options = condition["value"] if isinstance(condition["value"], list) else str(condition["value"]).split("|")
                if value_type == "datetime":
                    return None
                names = []
                for position, option in enumerate(options):
                    parameters[f"{prefix}v{position}"] = self._literal(connection, value_type, option)
                    names.append(f":{prefix}v{position}")
                tests.append(f"{column} IN ({', '.join(names)})")
            elif operator in STATUS_OPERATORS:
                parameters[f"{prefix}v"] = self._literal(connection, value_type, condition["value"])
                tests += self._range_tests(column, value_type, operator, f"{prefix}v", parameters)
            else:
                return None
            if facts:
                # In force at the instant: open facts that already began (the
                # current-value indexes) plus facts that end later than it.
                instant = at or NOW_SQL
                started = f"(valid_from IS NULL OR julianday(valid_from) <= julianday({instant}))"
                return (f"{start} IN (SELECT subject_entity_id FROM assertions WHERE predicate_id = :{prefix}p "
                        f"AND assertion_state = 'asserted' AND valid_to IS NULL AND {started} AND {' AND '.join(tests)} "
                        f"UNION ALL SELECT subject_entity_id FROM assertions WHERE predicate_id = :{prefix}p "
                        f"AND valid_to IS NOT NULL AND assertion_state <> 'retracted' AND valid_to >= substr({instant}, 1, 19) "
                        f"AND julianday(valid_to) > julianday({instant}) AND {started} AND {' AND '.join(tests)})")
            return f"{start} IN (SELECT {key} FROM {table} WHERE predicate_id = :{prefix}p AND {scope} AND {' AND '.join(tests)})"
        return None

    @staticmethod
    def _range_tests(column: str, value_type: str, operator: str, parameter: str, parameters: dict[str, Any]) -> list[str]:
        """Index-friendly comparison. Stored instants are canonical UTC text, so
        a text range (widened one second for fractions) narrows the index and
        julianday() keeps the exact comparison."""
        sql_operator = STATUS_OPERATORS[operator]
        if value_type != "datetime":
            return [f"{column} {sql_operator} :{parameter}"]
        exact = f"julianday({column}) {sql_operator} julianday(:{parameter})"
        tests = []
        if operator in ("=", ">", ">="):
            parameters[f"{parameter}f"] = shift_instant(parameters[parameter], -1)
            tests.append(f"{column} >= :{parameter}f")
        if operator in ("=", "<", "<="):
            parameters[f"{parameter}c"] = shift_instant(parameters[parameter], 1)
            tests.append(f"{column} < :{parameter}c")
        return tests + [exact]

    def _condition_sql(
        self, connection: sqlite3.Connection, clause: Any, parameters: dict[str, Any], prefix: str,
        start: str, start_type: str, instant: str | None, from_index: bool = True, at: str | None = None,
    ) -> str:
        """from_index: start from the value indexes (only at the top of a query;
        inside a per-row context the IN list would be rebuilt for every row)."""
        condition = parse_condition(clause)
        if condition["path"].startswith("line"):
            raise MemoryError("where conditions start at the event or entity, not at a line")
        anchored = from_index and self._anchored_condition_sql(connection, condition, parameters, prefix, start,
                                                               start_type, at)
        if anchored:
            return anchored
        compiled = self._compile_path(connection, condition["path"], parameters, prefix, start, start_type, instant, at=at)
        joins, value, value_type = compiled["joins"], compiled["value"], compiled["value_type"]
        operator = condition["op"]
        if operator in ("has", "no"):
            exists = f"EXISTS (SELECT 1 {joins} WHERE {value} IS NOT NULL)"
            return exists if operator == "has" else f"NOT {exists}"

        def wrap(expression: str) -> str:
            return f"julianday({expression})" if value_type == "datetime" else expression

        if operator == "between":
            low, separator, high = str(condition["value"]).partition("..")
            if not separator:
                raise MemoryError("between needs LOW..HIGH (the upper bound is excluded)")
            parameters[f"{prefix}lo"] = self._literal(connection, value_type, low)
            parameters[f"{prefix}hi"] = self._literal(connection, value_type, high)
            test = f"{wrap(value)} >= {wrap(':' + prefix + 'lo')} AND {wrap(value)} < {wrap(':' + prefix + 'hi')}"
        elif operator == "in":
            options = condition["value"] if isinstance(condition["value"], list) else str(condition["value"]).split("|")
            names = []
            for position, option in enumerate(options):
                parameters[f"{prefix}v{position}"] = self._literal(connection, value_type, option)
                names.append(wrap(f":{prefix}v{position}"))
            test = f"{wrap(value)} IN ({', '.join(names)})"
        elif operator in STATUS_OPERATORS:
            parameters[f"{prefix}v"] = self._literal(connection, value_type, condition["value"])
            if operator == "!=":
                # "Is not X": no reachable value equals X (rows without a value qualify).
                return f"NOT EXISTS (SELECT 1 {joins} WHERE {wrap(value)} = {wrap(':' + prefix + 'v')})"
            test = f"{wrap(value)} {STATUS_OPERATORS[operator]} {wrap(':' + prefix + 'v')}"
        else:
            raise MemoryError(f"Unknown operator {operator!r}")
        any_match = f"EXISTS (SELECT 1 {joins} WHERE {test})"
        if condition.get("quantifier") == "all":
            return f"({any_match} AND NOT EXISTS (SELECT 1 {joins} WHERE NOT ({test}) OR {value} IS NULL))"
        return any_match

    def _row_source(
        self, connection: sqlite3.Connection, event_type: str | None, entity_type: str | None,
        where: list[Any] | None, line: str | None, since: str | None, until: str | None,
        as_of: str | None, include_voided: bool,
    ) -> dict[str, Any]:
        """The rows every read model shares: events of a type (or their lines,
        or current entities of a type), filtered by time, conditions and
        status rules, as CTEs named base and rows."""
        if bool(event_type) == bool(entity_type):
            raise MemoryError("give exactly one of event_type or entity_type")
        entities_mode = bool(entity_type)
        if entities_mode and (line or since or until):
            raise MemoryError("entity rows have no time or lines; use facts and paths")
        start = ("entities.entity_id", "'entity'") if entities_mode else ("events.event_id", "'event'")
        as_of_bound = query_bound(as_of, self.zone, "as_of") if as_of else None
        at = ":asof" if as_of_bound else None
        parameters: dict[str, Any] = {
            "type": entity_type or event_type,
            "since": query_bound(since, self.zone, "since") if since else None,
            "until": query_bound(until, self.zone, "until") if until else None,
            "line": line,
            "asof": as_of_bound,
            "multiple": MULTIPLE,
        }
        rows_type = entity_type or event_type
        conditions: list[str] = []
        status_conditions: list[str] = []
        for index, clause in enumerate(where or []):
            if isinstance(clause, str) and clause.startswith("status:"):
                rule, _, state = clause.partition("=")
                # "status:x=Open" and "status:x = Open" mean the same.
                rule, state = rule.strip(), state.strip()
                if not state:
                    raise MemoryError(f"where needs status:RULE=STATE: {clause!r}")
                status = self._status_sql(connection, rule, start[0], parameters, f"w{index}", rows_type, as_of_bound)
                parameters[f"w{index}state"] = state
                status_conditions.append(f"{status} = :w{index}state")
                continue
            conditions.append(self._condition_sql(
                connection, clause, parameters, f"w{index}", start[0], start[1],
                None if entities_mode else "events.occurred_at", at=at,
            ))
        # Status rules evaluate several lookups per row: filter by cheaper conditions first.
        conditions += status_conditions
        if entities_mode and not as_of_bound:
            base_select = ("SELECT entity_id AS event_id, NULL AS occurred_at FROM entities "
                           "WHERE entity_type = :type AND retired_at IS NULL")
        elif entities_mode:
            # As of an instant: entities that existed then (recorded by then, or
            # holding a fact that had begun: history recorded later counts) and
            # that no retirement had taken out of use yet.
            base_select = (
                "SELECT entity_id AS event_id, NULL AS occurred_at FROM entities WHERE entity_type = :type "
                "AND (julianday(created_at) <= julianday(:asof) OR EXISTS (SELECT 1 FROM assertions AS began "
                "INDEXED BY assertions_subject_predicate_time WHERE began.subject_entity_id = entities.entity_id "
                "AND began.assertion_state <> 'retracted' AND julianday(began.valid_from) <= julianday(:asof))) "
                "AND (retired_at IS NULL OR NOT EXISTS (SELECT 1 FROM event_participants AS retired "
                "JOIN events AS retirement ON retirement.event_id = retired.event_id "
                "AND retirement.event_type = 'entity_retired' "
                "WHERE retired.entity_id = entities.entity_id AND retired.role IN ('subject', 'retired_part') "
                "AND julianday(retirement.occurred_at) <= julianday(:asof) AND " + active_event_sql("retirement") + "))"
            )
        else:
            base_select = (
                "SELECT event_id, occurred_at FROM events WHERE event_type = :type "
                "AND (:since IS NULL OR julianday(occurred_at) >= julianday(:since)) "
                "AND (:until IS NULL OR julianday(occurred_at) < julianday(:until)) "
                # As of an instant, only events that already existed count.
                "AND (:asof IS NULL OR julianday(occurred_at) < julianday(:asof))"
                + ("" if include_voided else " AND " + active_event_sql("events"))
            )
        row_start = start[1]
        if line:
            rows_cte = """rows AS (
                SELECT base.event_id, base.occurred_at, link.record_link_id AS row_id, 'record_link' AS row_type,
                       link.target_record_id AS line_target
                  FROM base JOIN record_links AS link
                    ON link.source_record_type = 'event' AND link.source_record_id = base.event_id
                   AND link.predicate_id = :line)"""
        else:
            rows_cte = (f"rows AS (SELECT event_id, occurred_at, event_id AS row_id, {row_start} AS row_type, "
                        "NULL AS line_target FROM base)")
        return {
            "parameters": parameters,
            "ctes": f"base AS ({base_select}{''.join(' AND ' + condition for condition in conditions)}),\n{rows_cte}",
            "entities_mode": entities_mode,
            "rows_type": rows_type,
            "row_start": row_start,
            "row_instant": None if entities_mode else "rows.occurred_at",
            # attr:A on the row reads the row's attribute; on an entity row, its current fact.
            "row_record": ("'entity'", "rows.row_id") if entities_mode else ("rows.row_type", "rows.row_id"),
            "line_target": "rows.line_target" if line else None,
            "as_of": as_of_bound,
            "at": at,
        }

    @staticmethod
    def _entity_labels(connection: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
        """Names for entity (or event) ids; homonyms are told apart by key."""
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        found = connection.execute(
            f"""SELECT entities.entity_id AS id, labels.name, external_key AS key FROM entities
                  JOIN entity_labels AS labels ON labels.entity_id = entities.entity_id WHERE entities.entity_id IN ({marks})
                UNION ALL SELECT event_id, COALESCE(external_key, event_id), external_key FROM events WHERE event_id IN ({marks})""",
            (*ids, *ids),
        ).fetchall()
        names = {row["id"]: (row["name"], row["key"]) for row in found}
        # A name shared by any other entity in memory always carries its key,
        # so the same entity reads the same in every result.
        distinct = {name for name, _ in names.values()}
        shared = {row[0] for row in connection.execute(
            f"""SELECT name FROM entity_labels WHERE name IN ({','.join('?' * len(distinct))})
                 GROUP BY name HAVING COUNT(*) > 1""",
            tuple(distinct),
        )} if distinct else set()
        return {key: (f"{name} [{external}]" if name in shared and external else name)
                for key, (name, external) in names.items()}

    def _column_sql(
        self, connection: sqlite3.Connection, spec: str, parameters: dict[str, Any], prefix: str, source: dict[str, Any],
        several: str,
    ) -> tuple[str, str]:
        """SQL for one column or group: returns (expression, value type).

        several='reject' marks rows reaching several values (groups);
        several='list' returns them as a JSON array (listings)."""
        kind, _, name = spec.partition(":")
        if kind == "period":
            period, _, over = name.partition("@")
            if period not in self.PERIODS:
                raise MemoryError(f"period must be one of {', '.join(self.PERIODS)}")
            parameters[prefix] = self.PERIODS[period]
            if not over:
                if source["entities_mode"]:
                    raise MemoryError("entity rows have no instant to group by period")
                # Periods follow the display zone: "2025" is the reader's 2025.
                return f"local_time(:{prefix}, rows.occurred_at)", "text"
            compiled = self._compile_path(connection, over, parameters, prefix, "rows.event_id", source["row_start"],
                                          source["row_instant"], "rows.line_target", source["row_record"], source["at"])
            if compiled["value_type"] != "datetime":
                raise MemoryError(f"period:{period}@ needs a date; {over} holds {compiled['value_type']}")
            expression, joins, multiple = f"local_time(:{prefix}, {compiled['value']})", compiled["joins"], compiled["multiple"]
            value_type = "text"
        elif kind == "status":
            return self._status_sql(connection, spec, "rows.event_id", parameters, prefix, source["rows_type"], source["as_of"]), "text"
        elif spec == "line":
            if not source["line_target"]:
                raise MemoryError("line needs --line PREDICATE")
            return "rows.line_target", "entity"
        elif spec == "key":
            table, column = ("entities", "entity_id") if source["entities_mode"] else ("events", "event_id")
            return f"(SELECT COALESCE(external_key, {column}) FROM {table} WHERE {column} = rows.event_id)", "text"
        elif spec == "name":
            if source["entities_mode"]:
                return "(SELECT name FROM entity_labels WHERE entity_id = rows.event_id)", "text"
            return "(SELECT summary FROM events WHERE event_id = rows.event_id)", "text"
        elif spec == "summary":
            # The text of an event (a decision, a question…) or an entity's description.
            if source["entities_mode"]:
                return "(SELECT description FROM entities WHERE entity_id = rows.event_id)", "text"
            return "(SELECT summary FROM events WHERE event_id = rows.event_id)", "text"
        elif spec == "occurred_at":
            if source["entities_mode"]:
                raise MemoryError("entity rows have no occurred_at")
            return "rows.occurred_at", "datetime"
        else:
            compiled = self._compile_path(connection, spec, parameters, prefix, "rows.event_id", source["row_start"],
                                          source["row_instant"], source["line_target"], source["row_record"], source["at"])
            expression, joins, multiple, value_type = (compiled["value"], compiled["joins"], compiled["multiple"],
                                                       compiled["value_type"])
        if not multiple:
            return f"(SELECT {expression} {joins} LIMIT 1)", value_type
        if several == "list":
            return f"(SELECT json_group_array(DISTINCT {expression}) {joins})", f"list:{value_type}"
        # A row that reaches several values cannot be placed in one group.
        return (f"(SELECT CASE WHEN COUNT(DISTINCT {expression}) > 1 THEN :multiple "
                f"ELSE MIN({expression}) END {joins})"), value_type

    def _operating_zones(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        """Entities that declare where they operate (fact time_zone), and
        whether that zone differs from the one this session displays."""
        zones = []
        for row in connection.execute(
            """SELECT (SELECT shown.name FROM entity_labels AS shown WHERE shown.entity_id = entity.entity_id) AS name, entity.external_key AS key, fact.value_text AS zone
                 FROM assertions AS fact JOIN entities AS entity ON entity.entity_id = fact.subject_entity_id
                WHERE fact.predicate_id = 'time_zone' AND """ + in_force_sql("fact") + """
                  AND fact.value_text IS NOT NULL
                ORDER BY entity.canonical_name LIMIT 20"""
        ):
            try:
                differs = not same_zone(display_zone(row["zone"]), self.zone)
            except MemoryError:
                differs = None  # an unreadable zone is shown, never guessed
            zones.append({**without_nulls(row), "differs_from_display": differs})
        return zones

    def _zone_notice(self, connection: sqlite3.Connection, specs: list[Any]) -> str | None:
        """Warn when a query cuts days or dates in a zone other than the one
        its data was operated in: the same night can fall on another day."""
        if not any(isinstance(spec, str) and ("period:" in spec or DATE_LITERAL.search(spec)) for spec in specs):
            return None
        others = [zone for zone in self._operating_zones(connection) if zone["differs_from_display"] is not False]
        if not others:
            return None
        where = "; ".join(f"{zone['name']} operates in {zone['zone']}" for zone in others)
        return (f"{where}. Days, periods and zone-less dates in this query use {zone_label(self.zone)}; "
                f"to see them as the operation lives them use --tz={others[0]['zone']}.")

    def aggregate(
        self,
        event_type: str | None,
        measure: str = "count",
        by: list[str] | None = None,
        where: list[Any] | None = None,
        line: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        order: str = "value",
        entity_type: str | None = None,
        include_voided: bool = False,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate events (or their lines, or current entities) without writing SQL.

        measure: count | sum:ATTR | avg:ATTR | min:ATTR | max:ATTR, read from
        the row's attributes (an entity row's current facts).
        by: period:day|week|month|year[@PATH], line, status:RULE, or any path.
        where: typed conditions (see parse_condition) and status:RULE=STATE.
        Rows lacking the measure are reported, never counted as zero.
        """
        if order not in ("value", "group"):
            raise MemoryError("order must be value or group")
        with self.connection() as connection:
            source = self._row_source(connection, event_type, entity_type, where, line, since, until, as_of, include_voided)
            parameters = source["parameters"]
            groups, group_kinds = [], {}
            for index, spec in enumerate(by or []):
                expression, value_type = self._column_sql(connection, spec, parameters, f"g{index}", source, "reject")
                groups.append((spec, expression))
                group_kinds[index] = value_type
            function, _, attribute = measure.partition(":")
            measured_predicate, measured_multiple = None, False
            if function == "count":
                value_sql, aggregate_sql = "1", "COUNT(*)"
            elif function in ("sum", "avg", "min", "max") and attribute:
                # A bare name is the row's own attribute (an entity row's fact in force).
                path = attribute if (":" in attribute or "(" in attribute) else f"attr:{attribute}"
                compiled = self._compile_path(connection, path, parameters, "m", "rows.event_id", source["row_start"],
                                              source["row_instant"], source["line_target"], source["row_record"],
                                              source["at"])
                if compiled["kind"] == "entity" or compiled["value_type"] not in ("integer", "real", "any"):
                    raise MemoryError(f"measure {measure}: {attribute} holds {compiled['value_type']} values; "
                                      "sum, avg, min and max need numbers")
                measured_predicate, measured_multiple = compiled["predicate"], compiled["multiple"]
                expression = compiled["value"]
                if compiled["value_type"] == "any":
                    expression = expression.replace("value_text", "value_integer, value_real")
                if measured_multiple:
                    value_sql = (f"(SELECT CASE WHEN COUNT({expression}) > 1 THEN :multiple ELSE MIN({expression}) END "
                                 f"{compiled['joins']})")
                else:
                    value_sql = f"(SELECT {expression} {compiled['joins']} LIMIT 1)"
                aggregate_sql = f"{function.upper()}(measured.value)"
            else:
                raise MemoryError("measure must be count, or sum|avg|min|max:ATTRIBUTE or :PATH ending in a number")
            group_columns = ", ".join(f"{sql} AS g{index}" for index, (_, sql) in enumerate(groups))
            group_names = ", ".join(f"g{index}" for index in range(len(groups)))
            ctes = (f"WITH {source['ctes']},\n"
                    f"measured AS (SELECT {value_sql} AS value{', ' + group_columns if groups else ''} FROM rows)\n")
            if order == "group" and groups:
                ordering = ", ".join(
                    f"(SELECT COALESCE(name, g{index}) FROM entity_labels WHERE entity_id = g{index}), g{index}"
                    if group_kinds.get(index) == "entity" else f"g{index}"
                    for index in range(len(groups))
                )
            else:
                ordering = "value DESC"
            parameters["limit"] = limit
            self._run_first_checks(connection, ctes, parameters)
            if measured_multiple and connection.execute(
                    ctes + "SELECT 1 FROM measured WHERE value = :multiple LIMIT 1", parameters).fetchone():
                raise MemoryError(f"measure {measure} reaches several values for some rows, so it cannot be added "
                                  "up per row; filter first, use --line, or measure a single-valued path")
            result_rows = connection.execute(
                ctes + f"""
                SELECT {group_names + ', ' if groups else ''}{aggregate_sql} AS value,
                       COUNT(*) AS rows, SUM(measured.value IS NULL) AS rows_without_measure
                  FROM measured
                 {'GROUP BY ' + group_names if groups else ''}
                 ORDER BY {ordering}
                 LIMIT :limit
                """,
                parameters,
            ).fetchall()
            if len(result_rows) < limit:
                # Every group is present, so totals follow without a second pass.
                totals = {"rows": sum(row["rows"] for row in result_rows),
                          "missing": sum(row["rows_without_measure"] or 0 for row in result_rows)}
            else:
                totals = connection.execute(
                    ctes + "SELECT COUNT(*) AS rows, SUM(value IS NULL) AS missing FROM measured", parameters
                ).fetchone()
            units = []
            if measured_predicate:
                # The unit of the predicate the measured path ends in.
                units = [row[0] for row in connection.execute(
                    "SELECT unit FROM predicates WHERE predicate_id = ? AND unit IS NOT NULL "
                    "UNION SELECT DISTINCT unit FROM record_attributes WHERE predicate_id = ? AND unit IS NOT NULL",
                    (measured_predicate, measured_predicate),
                )]
            for index, (spec, _) in enumerate(groups):
                if any(row[f"g{index}"] == MULTIPLE for row in result_rows):
                    raise MemoryError(
                        f"{spec} reaches several values for some rows, so they cannot be placed in one group; "
                        "filter first, list the rows, or group by a single-valued path"
                    )
            labels: dict[int, dict[Any, Any]] = {}
            for index, value_type in group_kinds.items():
                keys = {row[f"g{index}"] for row in result_rows if row[f"g{index}"] is not None}
                if value_type == "entity":
                    labels[index] = self._entity_labels(connection, keys)
                elif value_type == "boolean":
                    labels[index] = {key: bool(key) for key in keys}
            output_rows = []
            for row in result_rows:
                item = {spec: labels.get(index, {}).get(row[f"g{index}"], row[f"g{index}"])
                        for index, (spec, _) in enumerate(groups)}
                item.update(value=row["value"], rows=row["rows"], rows_without_measure=row["rows_without_measure"])
                output_rows.append(item)
            return {
                **({"entity_type": entity_type} if source["entities_mode"] else {"event_type": event_type}),
                "measure": measure,
                "unit": units[0] if len(units) == 1 else units or None,
                "line": line,
                "total_rows": totals["rows"],
                "rows_without_measure": totals["missing"] or 0,
                "groups": output_rows,
                "truncated": len(output_rows) == limit,
                **({"time_zone_notice": notice} if (notice := self._zone_notice(
                    connection, [*(by or []), *(where or []), since, until, as_of])) else {}),
            }

    def list_rows(
        self,
        event_type: str | None = None,
        entity_type: str | None = None,
        columns: list[str] | None = None,
        where: list[Any] | None = None,
        line: str | None = None,
        since: str | None = None,
        until: str | None = None,
        as_of: str | None = None,
        order_by: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        include_voided: bool = False,
    ) -> dict[str, Any]:
        """List rows with chosen columns instead of totals.

        Rows and conditions are exactly those of aggregate. A column may be
        key, name, occurred_at, line, status:RULE, period:X[@PATH] or any
        path; a column reaching several values returns them as a list.
        order_by takes column specs, '-' first for descending.
        """
        with self.connection() as connection:
            source = self._row_source(connection, event_type, entity_type, where, line, since, until, as_of, include_voided)
            parameters = source["parameters"]
            if columns is None:
                columns = (["key", "name"] if source["entities_mode"]
                           else ["key", "occurred_at", "participant:performed_by"] + (["line"] if line else []))
            selected, kinds = [], {}
            for index, spec in enumerate(columns):
                expression, value_type = self._column_sql(connection, spec, parameters, f"c{index}", source, "list")
                selected.append(f"{expression} AS c{index}")
                kinds[index] = value_type
            ordering = []
            for index, spec in enumerate(order_by or []):
                # desc:SPEC / asc:SPEC read well on a command line; -SPEC also works.
                descending = spec.startswith(("-", "desc:"))
                spec = spec[1:] if spec.startswith("-") else spec.removeprefix("desc:").removeprefix("asc:")
                expression, value_type = self._column_sql(connection, spec, parameters, f"o{index}", source, "reject")
                if value_type == "entity":
                    expression = f"(SELECT COALESCE(name, {expression}) FROM entity_labels WHERE entity_id = {expression})"
                ordering.append(f"{expression} {'DESC' if descending else 'ASC'}")
            if not ordering and not source["entities_mode"]:
                ordering.append("julianday(rows.occurred_at) ASC")
            ordering.append("rows.row_id")
            parameters.update(limit=limit, offset=offset)
            ctes = f"WITH {source['ctes']}\n"
            self._run_first_checks(connection, ctes, parameters)
            total = connection.execute(ctes + "SELECT COUNT(*) FROM rows", parameters).fetchone()[0]
            result = connection.execute(
                ctes + f"SELECT {', '.join(selected) or 'rows.row_id'} FROM rows ORDER BY {', '.join(ordering)} "
                "LIMIT :limit OFFSET :offset",
                parameters,
            ).fetchall()
            values: dict[int, list[Any]] = {index: [] for index in kinds}
            for row in result:
                for index, value_type in kinds.items():
                    value = row[f"c{index}"]
                    if value_type.startswith("list:"):
                        value = [item for item in json.loads(value) if item is not None] if value else []
                    values[index].append(value)
            # A column that may reach several values is a list only if some row
            # really does; otherwise it keeps plain values.
            for index, value_type in kinds.items():
                if value_type.startswith("list:") and all(len(value) <= 1 for value in values[index]):
                    values[index] = [value[0] if value else None for value in values[index]]
            labels: dict[int, dict[Any, Any]] = {}
            for index, value_type in kinds.items():
                base_type = value_type.removeprefix("list:")
                flat = {item for value in values[index] for item in (value if isinstance(value, list) else [value])
                        if item is not None}
                if base_type == "entity":
                    labels[index] = self._entity_labels(connection, flat)
                elif base_type == "boolean":
                    labels[index] = {item: bool(item) for item in flat}

            def show(index: int, value: Any) -> Any:
                mapping = labels.get(index, {})
                if isinstance(value, list):
                    return [mapping.get(item, item) for item in value]
                return mapping.get(value, value)

            rows = [{spec: show(index, values[index][position]) for index, spec in enumerate(columns)}
                    for position in range(len(result))]
            return {
                **({"entity_type": entity_type} if source["entities_mode"] else {"event_type": event_type}),
                "columns": columns,
                "total": total,
                "offset": offset,
                "returned": len(rows),
                "truncated": offset + len(rows) < total,
                **({"time_zone_notice": notice} if (notice := self._zone_notice(
                    connection, [*columns, *(order_by or []), *(where or []), since, until, as_of])) else {}),
                "rows": rows,
            }
