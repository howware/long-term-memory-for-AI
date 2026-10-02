"""Definitions stored as data (processes, status rules, recipes, change rules) and query conditions:
validated here, compiled or enforced by the mixins."""

from __future__ import annotations

import json
import re
from typing import Any

from .base import MemoryError


RECIPE_KINDS = {"aggregate", "list"}


RECIPE_QUERY_KEYS = {
    "aggregate": {"event_type", "entity_type", "measure", "by", "where", "line", "since", "until", "as_of", "order",
                  "limit", "include_voided"},
    "list": {"event_type", "entity_type", "columns", "where", "line", "since", "until", "as_of", "order_by", "limit",
             "offset", "include_voided"},
}


PARAMETER_TYPES = {"datetime", "date", "integer", "real", "text", "entity"}


PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)([+-]\d+)?\}")


def validate_process(definition: Any) -> dict[str, Any]:
    """A process is data: {"description"?, "states": [{"name", "terminal"?, "pending"?: ROLE}],
    "transitions": [{"from": STATE | null, "to": STATE, "roles": [ROLE…], "requires"?: [PREDICATE…],
                     "requires_each"?: [{"of": "P1>P2…", "in": PREDICATE}],
                     "captures"?: [{"attribute": PREDICATE, "when"?: {ATTRIBUTE: VALUE}}]}],
    "override_roles": [ROLE…]}. A transition from null starts an instance.
    Roles are entity keys (role:…); "pending" is who holds the next action;
    "requires" lists facts the instance must hold before the transition;
    "requires_each" reads a list elsewhere (the documents its type requires:
    procedure_type>requires_document) and demands each item among the
    instance's facts "in" (presented_document). The list is data, so it can
    change without a new version of the process. "captures" lists the data
    the step itself must record (payment method; amount received only
    "when" the method is cash)."""
    if not isinstance(definition, dict):
        raise MemoryError("A process definition is a JSON object")
    unknown = set(definition) - {"description", "states", "transitions", "override_roles", "version"}
    if unknown:
        raise MemoryError(f"Unknown keys in process definition: {', '.join(sorted(unknown))}")
    states = definition.get("states")
    if not isinstance(states, list) or not states:
        raise MemoryError("states must be a non-empty list")
    names = [state.get("name") if isinstance(state, dict) else None for state in states]
    if not all(isinstance(name, str) and name for name in names) or len(set(names)) != len(names):
        raise MemoryError("every state needs a unique name")
    terminal = {state["name"] for state in states if state.get("terminal")}
    for state in states:
        due = state.get("due_hours")
        if due is not None and (isinstance(due, bool) or not isinstance(due, (int, float)) or due <= 0):
            raise MemoryError(f"due_hours of {state['name']} must be a positive number of hours")
    transitions = definition.get("transitions")
    if not isinstance(transitions, list) or not transitions:
        raise MemoryError("transitions must be a non-empty list")
    seen = set()
    for transition in transitions:
        if not isinstance(transition, dict) or transition.get("to") not in names:
            raise MemoryError(f"transition {transition} must go to a declared state")
        source = transition.get("from")
        if source is not None and source not in names:
            raise MemoryError(f"transition from unknown state {source!r}")
        if source in terminal:
            raise MemoryError(f"{source} is terminal: nothing leaves it inside the process")
        roles = transition.get("roles")
        if not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles):
            raise MemoryError(f"transition {source} -> {transition['to']} needs the roles that may perform it")
        requires = transition.get("requires", [])
        if not isinstance(requires, list) or not all(isinstance(item, str) and item for item in requires):
            raise MemoryError(f"requires of {source} -> {transition['to']} must be a list of predicate ids")
        each = transition.get("requires_each", [])
        if not isinstance(each, list) or not all(
                isinstance(item, dict) and set(item) == {"of", "in"} and all(isinstance(item[k], str) and item[k]
                                                                             for k in ("of", "in")) for item in each):
            raise MemoryError(f'requires_each of {source} -> {transition["to"]} must be [{{"of": "P1>P2", "in": P}}]')
        captures = transition.get("captures", [])
        if not isinstance(captures, list) or not all(
                isinstance(item, dict) and isinstance(item.get("attribute"), str) and item["attribute"]
                and set(item) <= {"attribute", "when", "equals_earlier"} and isinstance(item.get("when", {}), dict)
                for item in captures):
            raise MemoryError(f'captures of {source} -> {transition["to"]} must be '
                              f'[{{"attribute": P, "when"?: {{A: V}}, "equals_earlier"?: P}}]')
        if (source, transition["to"]) in seen:
            raise MemoryError(f"transition {source} -> {transition['to']} is declared twice")
        seen.add((source, transition["to"]))
    if not any(transition.get("from") is None for transition in transitions):
        raise MemoryError('a process needs at least one start transition ("from": null)')
    overrides = definition.get("override_roles", [])
    if not isinstance(overrides, list) or not all(isinstance(role, str) for role in overrides):
        raise MemoryError("override_roles must be a list of role keys")
    return definition


def validate_change_rule(definition: Any) -> dict[str, Any]:
    """Who may change what: {"subject_type": T, "predicates": [P…] | "*", "roles": [ROLE…],
    "create"?: bool, "description"?}. Facts, closes and retractions on entities
    of type T (and, with create, new entities of T) need a person holding one
    of the roles when the change takes effect."""
    if not isinstance(definition, dict):
        raise MemoryError("A change rule is a JSON object")
    unknown = set(definition) - {"subject_type", "predicates", "roles", "create", "description"}
    if unknown:
        raise MemoryError(f"Unknown keys in change rule: {', '.join(sorted(unknown))}")
    if not isinstance(definition.get("subject_type"), str) or not definition["subject_type"]:
        raise MemoryError("subject_type must name an entity type")
    predicates = definition.get("predicates", "*")
    if predicates != "*" and not (isinstance(predicates, list) and predicates and all(isinstance(p, str) for p in predicates)):
        raise MemoryError('predicates must be "*" or a list of predicate ids')
    roles = definition.get("roles")
    if not isinstance(roles, list) or not roles or not all(isinstance(role, str) for role in roles):
        raise MemoryError("roles must list the roles that may make the change")
    return definition


def steps_view(history: list[dict[str, Any]], pending: str | None) -> list[dict[str, Any]]:
    """Turn transitions into the steps an instance went through. A step is
    worked by whoever moves the instance out of it; the last one is current.
    A migration is not work on the step: it is noted on it, and only opens a
    new row when it maps the instance to another step."""
    steps: list[dict[str, Any]] = []
    for event in history:
        if not event.get("to_step"):
            continue
        if event.get("event_type") == "process_migrated":
            note = f"v{event.get('from_version')} → v{event.get('process_version')} ({event['by']}, {event['occurred_at']})"
            if steps and steps[-1]["step"] == event["to_step"]:
                steps[-1].setdefault("migrations", []).append(note)
                continue
            if steps:
                steps[-1].update(left_at=event["occurred_at"], left_by_migration=note)
            steps.append({"step": event["to_step"], "entered_at": event["occurred_at"], "entered_by_migration": note})
            continue
        if steps:
            steps[-1].update(left_at=event["occurred_at"], left_by=event["by"],
                             **({"left_by_exception": event["reason"]} if event.get("override") else {}))
        steps.append({"step": event["to_step"], "entered_at": event["occurred_at"], "entered_by": event["by"],
                      **({"entered_by_exception": event["reason"]} if event.get("override") else {})})
    if steps and "left_at" not in steps[-1]:
        steps[-1]["current"] = True
        if pending:
            steps[-1]["pending_for"] = pending
    return steps


def bypassed_captures(definition: dict[str, Any], source: str | None, target: str) -> list[str]:
    """Data an out-of-process jump leaves behind: the captures of the steps on
    the shortest normal path from source to target (none if there is no path)."""
    paths: dict[str | None, list[dict[str, Any]]] = {source: []}
    frontier = [source]
    while frontier and target not in paths:
        following = []
        for state in frontier:
            for transition in definition["transitions"]:
                if transition.get("from") == state and transition["to"] not in paths:
                    paths[transition["to"]] = paths[state] + [transition]
                    following.append(transition["to"])
        frontier = following
    skipped = []
    for transition in paths.get(target, []):
        for capture in transition.get("captures", []):
            condition = ", ".join(f"{name}={value}" for name, value in capture.get("when", {}).items())
            skipped.append(f"{transition.get('from') or '(start)'} -> {transition['to']}: {capture['attribute']}"
                           + (f" (when {condition})" if condition else ""))
    return skipped


def missing_captures(transition: dict[str, Any], attributes: dict[str, Any]) -> list[str]:
    """Data a step must record that the caller did not give. A capture with
    "when" applies only if the step records those values (compared as text)."""
    missing = []
    for capture in transition.get("captures", []):
        applies = all(str(attributes.get(name)) == str(value) for name, value in capture.get("when", {}).items())
        if applies and attributes.get(capture["attribute"]) in (None, ""):
            condition = ", ".join(f"{name}={value}" for name, value in capture.get("when", {}).items())
            missing.append(capture["attribute"] + (f" (when {condition})" if condition else ""))
    return missing


def process_roles(definition: dict[str, Any]) -> set[str]:
    roles = {role for transition in definition["transitions"] for role in transition["roles"]}
    roles |= set(definition.get("override_roles", []))
    roles |= {state["pending"] for state in definition["states"] if state.get("pending")}
    return roles


def validate_recipe(definition: Any) -> dict[str, Any]:
    """A saved query: {"kind": "aggregate"|"list", "parameters": {NAME: {"type", "default"?,
    "description"?}}, "query": {...aggregate or list arguments with {NAME} placeholders}}."""
    if not isinstance(definition, dict) or definition.get("kind") not in RECIPE_KINDS:
        raise MemoryError('A recipe needs "kind": "aggregate" or "list"')
    unknown = set(definition) - {"kind", "parameters", "query", "description"}
    if unknown:
        raise MemoryError(f"Unknown keys in recipe: {', '.join(sorted(unknown))}")
    query = definition.get("query")
    if not isinstance(query, dict):
        raise MemoryError('A recipe needs a "query" object')
    extra = set(query) - RECIPE_QUERY_KEYS[definition["kind"]]
    if extra:
        raise MemoryError(f"{definition['kind']} recipes do not take: {', '.join(sorted(extra))}")
    if bool(query.get("event_type")) == bool(query.get("entity_type")):
        raise MemoryError("A recipe query needs exactly one of event_type or entity_type")
    parameters = definition.get("parameters") or {}
    for name, spec in parameters.items():
        if not isinstance(spec, dict) or spec.get("type") not in PARAMETER_TYPES:
            raise MemoryError(f"Parameter {name} needs a type among {', '.join(sorted(PARAMETER_TYPES))}")
    used = {match.group(1) for match in PLACEHOLDER.finditer(json.dumps(query, ensure_ascii=False))}
    undeclared = used - set(parameters)
    if undeclared:
        raise MemoryError(f"Placeholders without a declared parameter: {', '.join(sorted(undeclared))}")
    return definition


STATUS_OPERATORS = {"=": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<="}


def value_sql(alias: str) -> str:
    return (f"COALESCE({alias}.value_integer, {alias}.value_real, {alias}.value_boolean, "
            f"{alias}.value_datetime, {alias}.value_text, {alias}.value_entity_id)")


VALUE_COLUMNS = {
    "text": "value_text", "integer": "value_integer", "real": "value_real", "boolean": "value_boolean",
    "datetime": "value_datetime", "json": "value_json", "entity": "value_entity_id",
}


PATH_SEPARATOR = re.compile(r"[>/]")


def split_path(path: str) -> list[str]:
    """Steps of a path. '>' and '/' both separate them: '/' is safe to type
    unquoted in a shell, where '>' redirects output to a file."""
    return PATH_SEPARATOR.split(path)


# Operators need spaces around them: '>' also separates path steps.
CONDITION_OPERATORS = (" between ", " >= ", " <= ", " != ", " in ", " > ", " < ", " = ")


MULTIPLE = "\u0000multiple"


def column_for(alias: str, value_type: str) -> str:
    return f"{alias}.{VALUE_COLUMNS[value_type]}" if value_type in VALUE_COLUMNS else value_sql(alias)


def parse_condition(clause: Any) -> dict[str, Any]:
    """PATH OP VALUE, has:PATH, no:PATH, all:PATH OP VALUE, or the legacy PATH=ENTITY.

    A dict {"path", "op", "value", "quantifier"} is the canonical form.
    """
    if isinstance(clause, dict):
        if "path" not in clause or "op" not in clause:
            raise MemoryError("a structured condition needs path and op")
        return {"quantifier": clause.get("quantifier", "any"), **clause}
    text = clause.strip()
    for prefix, operator in (("has:", "has"), ("no:", "no")):
        if text.startswith(prefix):
            return {"path": text[len(prefix):], "op": operator, "value": None, "quantifier": "any"}
    quantifier = "any"
    if text.startswith("all:"):
        quantifier, text = "all", text[4:]
    for token in CONDITION_OPERATORS:
        position = text.find(token)
        if position > 0:
            return {"path": text[:position], "op": token.strip(), "value": text[position + len(token):].strip(),
                    "quantifier": quantifier}
    path, separator, value = text.rpartition("=")
    if separator and path and " " not in path:
        return {"path": path, "op": "=", "value": value, "quantifier": quantifier}
    raise MemoryError(
        f"Cannot read condition {clause!r}; write 'PATH OP VALUE' with spaces around the operator "
        "(=, !=, >, >=, <, <=, in A|B, between A..B), or has:PATH / no:PATH to test whether a value exists")


def validate_status_rule(definition: Any) -> dict[str, Any]:
    """Check a declarative status rule; it is data, compiled to SQL only by the helper.

    {"applies_to": {"event_type": T} | {"entity_type": T},
     "follow_link": PREDICATE,            # link from follow-up events to the subject
     "states": [{"name": N, "event_type": E,
                 "where": {ATTR: {OP: VALUE | {"subject_attr": ATTR}}}}],  # first match wins
     "otherwise": N}
    """
    if not isinstance(definition, dict):
        raise MemoryError("A status rule definition is a JSON object")
    applies = definition.get("applies_to")
    if not isinstance(applies, dict) or len(applies) != 1 or next(iter(applies)) not in ("event_type", "entity_type"):
        raise MemoryError('applies_to must be {"event_type": T} or {"entity_type": T}')
    if not isinstance(definition.get("follow_link"), str):
        raise MemoryError("follow_link must name the predicate that links follow-up events to the subject")
    states = definition.get("states")
    if not isinstance(states, list) or not states:
        raise MemoryError("states must be a non-empty ordered list")
    for state in states:
        if not isinstance(state, dict) or not isinstance(state.get("name"), str) or not isinstance(state.get("event_type"), str):
            raise MemoryError("each state needs a name and the event_type that produces it")
        for attribute, comparison in (state.get("where") or {}).items():
            allowed = (*STATUS_OPERATORS, "in", "between")
            if not isinstance(comparison, dict) or len(comparison) != 1 or next(iter(comparison)) not in allowed:
                raise MemoryError(f"condition on {attribute} must be one of {', '.join(allowed)} with a value")
            operator, operand = next(iter(comparison.items()))
            if isinstance(operand, dict) and (set(operand) != {"subject_attr"} or operator not in STATUS_OPERATORS):
                raise MemoryError('a computed operand must be {"subject_attr": ATTR} with =, !=, >, >=, < or <=')
    if not isinstance(definition.get("otherwise"), str):
        raise MemoryError("otherwise must name the state when no follow-up event matches")
    unknown = set(definition) - {"applies_to", "follow_link", "states", "otherwise", "description"}
    if unknown:
        raise MemoryError(f"Unknown keys in status rule: {', '.join(sorted(unknown))}")
    return definition
