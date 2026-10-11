"""Command line: memory.py <command>."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .base import MEMORY_KINDS, MemoryError, __version__, localize, resolve_database
from .reading import hook_output, render_brief
from .core import Memory


def parse_attributes(items: list[str]) -> dict[str, Any]:
    """KEY=VALUE pairs; a value that reads as JSON (numbers, true, false) keeps its type."""
    attributes: dict[str, Any] = {}
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator or not key:
            raise MemoryError(f"--attr expects KEY=VALUE, got {item!r}")
        try:
            attributes[key] = json.loads(raw)
        except json.JSONDecodeError:
            attributes[key] = raw
    return attributes


def parse_participant(value: str) -> tuple[str, str]:
    try:
        reference, role = value.rsplit("|", 1)
    except ValueError as error:
        raise argparse.ArgumentTypeError("participants use ENTITY_REFERENCE|role") from error
    return reference, role


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic local SQLite operational memory helper")
    parser.add_argument("--version", action="version", version=f"memory.py {__version__}")
    parser.add_argument(
        "--db", help="SQLite memory path (default: MEMORY_DB, else the project's .memory/memory.sqlite)"
    )
    parser.add_argument(
        "--tz", help="Display zone: system (default, or MEMORY_TZ), UTC, or an IANA name. Storage is always UTC."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap")
    entity = sub.add_parser("create-entity")
    entity.add_argument("--type", required=True)
    entity.add_argument("--name", required=True)
    entity.add_argument("--key")
    entity.add_argument("--description")
    event = sub.add_parser("record-event")
    event.add_argument("--type", required=True)
    event.add_argument("--summary", required=True)
    event.add_argument("--performed-by", required=True)
    event.add_argument("--participant", action="append", default=[], type=parse_participant)
    event.add_argument("--occurred-at")
    transition = sub.add_parser("transition")
    transition.add_argument("--instance", required=True)
    transition.add_argument("--to-state", required=True)
    transition.add_argument("--performed-by", required=True)
    transition.add_argument("--pending-for")
    transition.add_argument("--occurred-at")
    code = sub.add_parser("generate-code")
    code.add_argument("--prefix", default="")
    assign = sub.add_parser("assign-code")
    assign.add_argument("--entity", required=True)
    assign.add_argument("--performed-by", required=True)
    assign.add_argument("--code")
    assign.add_argument("--prefix", default="")
    assign.add_argument("--at", help="When it was assigned (default now)")
    assign.add_argument("--source", help="Literal words of the instruction")
    detail = sub.add_parser("detail")
    detail.add_argument("entity")
    sub.add_parser("catalog")
    sub.add_parser("where", help="Print the resolved database path without opening it")
    backup = sub.add_parser("backup", help="Verified copy of the memory (safe while agents are writing)")
    backup.add_argument("--to", help="File or existing folder (default: .memory/backups/)")
    init = sub.add_parser("init", help="Create or upgrade this project's memory (idempotent)")
    init.add_argument("--owner-name")
    init.add_argument("--owner-key")
    search = sub.add_parser("search")
    search.add_argument("text")
    search.add_argument("--type")
    search.add_argument("--limit", type=int, default=20)
    describe = sub.add_parser("describe")
    describe.add_argument("entity")
    describe.add_argument("--sample", type=int, default=10, help="Incoming facts shown per predicate")
    timeline = sub.add_parser("timeline")
    timeline.add_argument("entity")
    timeline.add_argument("--limit", type=int, default=50, help="Most recent events to return; 0 for all")
    timeline.add_argument("--since", help="Inclusive ISO-8601 lower bound")
    timeline.add_argument("--until", help="Exclusive ISO-8601 upper bound")
    timeline.add_argument("--type", help="Only this event type")
    timeline.add_argument("--include-voided", action="store_true", help="Also show voided events, marked as such")
    pending = sub.add_parser("pending")
    pending.add_argument("--person")
    remember = sub.add_parser("remember", help="Capture a decision, convention, question, lesson... in one call")
    remember.add_argument("--kind", required=True, choices=sorted(MEMORY_KINDS))
    remember.add_argument("--text", required=True, help="What to remember, in one or two sentences")
    remember.add_argument("--why", help="The reason behind it")
    remember.add_argument("--about", action="append", default=[], help="Entity it concerns (repeatable)")
    remember.add_argument("--by", help="Person who decided or said it (default: memory owner)")
    remember.add_argument("--source-text", help="Literal words of the user, when different from --text")
    remember.add_argument("--occurred-at")
    remember.add_argument("--key")
    remember.add_argument("--resolves", help="Open question key this answer closes")
    remember.add_argument("--supersedes", help="Earlier item of the same kind this one replaces")
    remember.add_argument("--as-agent", action="store_true",
                          help="Your own observation (lesson, note, open_question, milestone), not a person's")
    remember.add_argument("--agent", help="Agent key (default: detected, e.g. agent:claude-code)")
    void = sub.add_parser("void", help="Void an event recorded by mistake (it stays, but stops counting)")
    void.add_argument("--event", required=True, help="Event key or id")
    void.add_argument("--reason", required=True)
    void.add_argument("--by", help="Person who decides the void (default: memory owner)")
    void.add_argument("--source-text", help="Literal words of the user, when different from --reason")
    brief = sub.add_parser("brief", help="Start-of-session summary; safe to run when no memory exists")
    brief.add_argument("--limit", type=int, default=8)
    brief.add_argument("--format", choices=("json", "text", "hook"), default="json",
                       help="hook: SessionStart JSON with the text brief as additionalContext")
    sub.add_parser("reminder", help="UserPromptSubmit hook: one-line capture reminder when a memory exists")
    write = sub.add_parser("write", help="Apply a JSON memory document atomically (see SKILL.md)")
    write.add_argument("--file", help="Document path; '-' or omitted reads stdin")
    show_event = sub.add_parser("event", help="Everything recorded about one event")
    show_event.add_argument("event")
    recall = sub.add_parser("recall", help="Full-text search over sources, events and entities")
    recall.add_argument("text")
    recall.add_argument("--limit", type=int, default=20)
    aggregate = sub.add_parser("aggregate", help="Declarative aggregation of events or their lines")
    rows = aggregate.add_mutually_exclusive_group(required=True)
    rows.add_argument("--event-type")
    rows.add_argument("--entity-type", help="Aggregate current entities of this type by their facts")
    aggregate.add_argument("--measure", default="count", help="count | sum:ATTR | avg:ATTR | min:ATTR | max:ATTR")
    aggregate.add_argument("--by", action="append", default=[], help="period:month | attr:A | line | link:P>fact:Q ...")
    aggregate.add_argument("--where", action="append", default=[], help="PATH=ENTITY_REF")
    aggregate.add_argument("--line", help="Aggregate the event's links with this predicate (for example its products)")
    aggregate.add_argument("--since")
    aggregate.add_argument("--until")
    aggregate.add_argument("--limit", type=int, default=100)
    aggregate.add_argument("--order", default="value", choices=("value", "group"))
    aggregate.add_argument("--include-voided", action="store_true", help="Also count voided events")
    aggregate.add_argument("--as-of", help="Evaluate status rules at this instant (zone-less = display zone)")
    listing = sub.add_parser("list", help="List rows with chosen columns (same rows and conditions as aggregate)")
    list_rows = listing.add_mutually_exclusive_group(required=True)
    list_rows.add_argument("--event-type")
    list_rows.add_argument("--entity-type")
    listing.add_argument("--column", action="append", help="key | name | summary | occurred_at | line | status:RULE | PATH (repeatable)")
    listing.add_argument("--where", action="append", default=[], help="Typed condition (repeatable)")
    listing.add_argument("--line", help="List the event's links with this predicate")
    listing.add_argument("--since")
    listing.add_argument("--until")
    listing.add_argument("--as-of")
    listing.add_argument("--order-by", action="append", default=[], help="Column spec; desc:SPEC for descending")
    listing.add_argument("--limit", type=int, default=50)
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--include-voided", action="store_true")
    define_recipe = sub.add_parser("define-recipe", help="Store or version a saved query with parameters (JSON)")
    define_recipe.add_argument("--key", required=True, help="recipe:<name>")
    define_recipe.add_argument("--name", required=True)
    define_recipe.add_argument("--description")
    define_recipe.add_argument("--file", help="Definition JSON; '-' or omitted reads stdin")
    recipe = sub.add_parser("recipe", help="Run a saved query: recipe run <key> --param NAME=VALUE")
    recipe.add_argument("action", choices=["run"])
    recipe.add_argument("key")
    recipe.add_argument("--param", action="append", default=[], help="NAME=VALUE (repeatable)")
    define_status = sub.add_parser("define-status", help="Store or version a declarative status rule (JSON)")
    define_status.add_argument("--key", required=True, help="status:<name>")
    define_status.add_argument("--name", required=True)
    define_status.add_argument("--description")
    define_status.add_argument("--file", help="Definition JSON; '-' or omitted reads stdin")
    define_process = sub.add_parser("define-process", help="Create a process or publish a new version (owner only)")
    define_process.add_argument("--key", required=True, help="process:<name>")
    define_process.add_argument("--name", required=True)
    define_process.add_argument("--by", required=True, help="Person publishing it (the owner)")
    define_process.add_argument("--owner", help="Owner person, only when the process is created")
    define_process.add_argument("--from", dest="effective_from", help="When the version starts (default now)")
    define_process.add_argument("--reason")
    define_process.add_argument("--source", help="Literal words of the decision")
    define_process.add_argument("--file", help="Definition JSON; '-' or omitted reads stdin")
    for name, help_text in (("start-process", "Start an instance under the process version in force"),
                            ("advance", "Move an instance to another step, validated against its version")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("instance")
        if name == "start-process":
            command.add_argument("--process", required=True)
            command.add_argument("--to", help="First step when the process has several")
        else:
            command.add_argument("--to", required=True)
        command.add_argument("--by", required=True, help="Person who does it")
        command.add_argument("--at", help="When it happened (default now)")
        command.add_argument("--pending-for", help="Person or role holding the next action (default: the step's)")
        command.add_argument("--reason", help="Required for an exception outside the process")
        command.add_argument("--source", help="Literal words of the instruction")
        command.add_argument("--attr", action="append", default=[], help="KEY=VALUE captured at this step (repeatable)")
    migrate = sub.add_parser("migrate-process", help="Move a running instance to another version (owner's plan)")
    migrate.add_argument("instance")
    migrate.add_argument("--to-version", type=int, required=True)
    migrate.add_argument("--by", required=True)
    migrate.add_argument("--reason", required=True)
    migrate.add_argument("--map", action="append", default=[], help="OLD_STEP=NEW_STEP (repeatable)")
    migrate.add_argument("--at")
    migrate.add_argument("--pending-for", help="Person or role who takes the migrated instance")
    migrate.add_argument("--source")
    change_rule = sub.add_parser("define-change-rule", help="Declare who may change a kind of data (JSON)")
    change_rule.add_argument("--key", required=True, help="change:<name>")
    change_rule.add_argument("--name", required=True)
    change_rule.add_argument("--by", required=True)
    change_rule.add_argument("--source", help="Literal words of the decision")
    change_rule.add_argument("--file", help="Definition JSON; '-' or omitted reads stdin")
    outline = sub.add_parser("outline", help="A structure as a tree (part_of/position), now or at an instant")
    outline.add_argument("root")
    outline.add_argument("--parent", default="part_of")
    outline.add_argument("--order", default="position")
    outline.add_argument("--as-of", help="Read the structure and its facts as they were at this instant")
    outline.add_argument("--depth", type=int)
    outline.add_argument("--show", action="append", default=[], help="Fact to include for each node (repeatable)")
    outline.add_argument("--format", choices=("json", "text"), default="json",
                         help="text: an indented reading with the first --show fact as each node's text")
    history = sub.add_parser("history", help="Every version of an entity's facts, with changeset and actor")
    history.add_argument("entity")
    history.add_argument("--predicate")
    changes = sub.add_parser("changeset", help="What a changeset (work session) asserted, closed, retracted")
    changes.add_argument("key")
    request = sub.add_parser("request-exception", help="Ask for a step you may not take; it waits on the exception roles")
    request.add_argument("instance")
    request.add_argument("--to", required=True, help="The step asked for")
    request.add_argument("--by", required=True)
    request.add_argument("--reason", required=True)
    request.add_argument("--at")
    request.add_argument("--source")
    request.add_argument("--attr", action="append", default=[], help="KEY=VALUE the step would record (repeatable)")
    resolve = sub.add_parser("resolve-exception", help="Approve (the step is taken now) or deny an exception request")
    resolve.add_argument("request", help="exreq:<instance>:<n>")
    decision_group = resolve.add_mutually_exclusive_group(required=True)
    decision_group.add_argument("--approve", action="store_true")
    decision_group.add_argument("--deny", action="store_true")
    resolve.add_argument("--by", required=True)
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--at")
    resolve.add_argument("--source")
    resolve.add_argument("--attr", action="append", default=[], help="KEY=VALUE for the step, if it needs data")
    overdue = sub.add_parser("process-overdue", help="Instances waiting longer than their step's due_hours")
    overdue.add_argument("--process")
    overdue.add_argument("--at", help="Evaluate at this instant (default now)")
    definition = sub.add_parser("process-definition", help="A process definition as JSON, ready to edit")
    definition.add_argument("key")
    definition.add_argument("--version", type=int, help="Default: the version in force")
    values = sub.add_parser("define-values", help="Allowed text values (or entity type) of a predicate")
    values.add_argument("--predicate", required=True)
    values.add_argument("--value", action="append", help="An allowed value (repeatable); the list replaces the current one")
    values.add_argument("--entity-type", help="For entity predicates: the type of entity it must point to")
    values.add_argument("--by", required=True)
    values.add_argument("--source", help="Literal words of the decision")
    kind = sub.add_parser("define-event-type", help="Declare an event type high-volume (out of the brief's recent list) or normal")
    kind.add_argument("--type", required=True)
    volume = kind.add_mutually_exclusive_group(required=True)
    volume.add_argument("--high-volume", action="store_true", help="Kept out of the brief's recent list; counted per type")
    volume.add_argument("--normal", action="store_true", help="Listed in the brief's recent list again")
    kind.add_argument("--by", required=True, help="Person who decides it")
    kind.add_argument("--reason")
    kind.add_argument("--source", help="Literal words of the decision")
    kinds = sub.add_parser("event-types", help="How each declared event type is treated (high-volume or normal)")
    kinds.add_argument("--as-of", help="As it was at that instant")
    status = sub.add_parser("process-status", help="Step, version, pending holder, next steps and history of an instance")
    status.add_argument("instance")
    args = parser.parse_args()
    # Windows consoles default to a legacy code page and corrupt accents.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        memory = Memory(resolve_database(args.db), args.tz)
    except MemoryError as error:
        print(f"Memory operation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    # Hook commands must never break a session: they report problems as context.
    if args.command == "reminder":
        if memory.database_path.exists():
            print(hook_output("UserPromptSubmit", (
                "[memory] If this exchange produces a decision, convention, preference, open question, answer or "
                "lesson, record it with memory.py remember (attributed to whoever said it) before ending the turn; "
                "record in English, answer in the user's language."
            )))
        return
    if args.command == "brief" and args.format != "json":
        try:
            text = render_brief(localize(memory.brief(args.limit), memory.zone))
        except (MemoryError, sqlite3.Error) as error:
            text = f"[project memory] The memory could not be read: {error}"
        print(hook_output("SessionStart", text) if args.format == "hook" else text)
        return
    try:
        if args.command == "backup":
            result: Any = memory.backup(args.to)
        elif args.command == "where":
            # Read-only: reports the schema without applying anything.
            result = {"database": str(memory.database_path), "exists": memory.database_path.exists()}
            if result["exists"]:
                result["schema"] = memory.schema_status()
        elif args.command == "init":
            result = memory.init(args.owner_name, args.owner_key)
        elif args.command == "bootstrap":
            memory.bootstrap_predicates(); result = {"status": "ok"}
        elif args.command == "create-entity":
            result = memory.create_entity(args.type, args.name, args.key, args.description)
        elif args.command == "record-event":
            result = memory.record_event(args.type, args.summary, args.performed_by, args.participant, args.occurred_at)
        elif args.command == "transition":
            result = memory.transition(args.instance, args.to_state, args.performed_by, args.pending_for, args.occurred_at)
        elif args.command == "generate-code":
            result = {"tracking_code": memory.generate_tracking_code(args.prefix)}
        elif args.command == "assign-code":
            result = memory.assign_tracking_code(args.entity, args.performed_by, args.code, args.prefix, args.at,
                                                 args.source)
        elif args.command == "detail":
            result = memory.detail(args.entity)
        elif args.command == "catalog":
            result = memory.catalog()
        elif args.command == "remember":
            result = memory.remember(
                args.kind, args.text, args.why, args.about, args.by, args.source_text, args.occurred_at, args.key,
                args.resolves, args.supersedes, args.as_agent, args.agent,
            )
        elif args.command == "brief":
            result = memory.brief(args.limit)
        elif args.command == "void":
            result = memory.void(args.event, args.reason, args.by, args.source_text)
        elif args.command == "write":
            raw = sys.stdin.buffer.read() if args.file in (None, "-") else Path(args.file).read_bytes()
            try:
                document = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MemoryError(f"write expects a UTF-8 JSON document: {error}") from error
            result = memory.write(document)
        elif args.command == "event":
            result = memory.event(args.event)
        elif args.command == "recall":
            result = memory.recall(args.text, args.limit)
        elif args.command == "aggregate":
            result = memory.aggregate(
                args.event_type, args.measure, args.by, args.where, args.line, args.since, args.until, args.limit, args.order,
                args.entity_type, args.include_voided, args.as_of,
            )
        elif args.command == "list":
            result = memory.list_rows(
                args.event_type, args.entity_type, args.column, args.where, args.line, args.since, args.until,
                args.as_of, args.order_by, args.limit, args.offset, args.include_voided,
            )
        elif args.command == "define-recipe":
            raw = sys.stdin.buffer.read() if args.file in (None, "-") else Path(args.file).read_bytes()
            try:
                definition = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MemoryError(f"define-recipe expects a UTF-8 JSON definition: {error}") from error
            result = memory.define_recipe(args.key, args.name, definition, args.description)
        elif args.command == "recipe":
            given = {}
            for item in args.param:
                name, separator, value = item.partition("=")
                if not separator:
                    raise MemoryError(f"--param expects NAME=VALUE, got {item!r}")
                given[name] = value
            result = memory.run_recipe(args.key, given)
        elif args.command == "define-process":
            raw = sys.stdin.buffer.read() if args.file in (None, "-") else Path(args.file).read_bytes()
            try:
                definition = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MemoryError(f"define-process expects a UTF-8 JSON definition: {error}") from error
            result = memory.define_process(args.key, args.name, definition, args.by, args.owner, args.effective_from,
                                           args.reason, args.source)
        elif args.command == "start-process":
            result = memory.start_process(args.instance, args.process, args.by, args.at, args.to, args.pending_for,
                                          args.reason, args.source, parse_attributes(args.attr))
        elif args.command == "advance":
            result = memory.advance_process(args.instance, args.to, args.by, args.at, args.pending_for, args.reason,
                                            args.source, parse_attributes(args.attr))
        elif args.command == "migrate-process":
            mapping = dict(item.split("=", 1) for item in args.map)
            result = memory.migrate_process(args.instance, args.to_version, args.by, args.reason, mapping, args.at,
                                            args.source, args.pending_for)
        elif args.command == "define-change-rule":
            raw = sys.stdin.buffer.read() if args.file in (None, "-") else Path(args.file).read_bytes()
            try:
                definition = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MemoryError(f"define-change-rule expects a UTF-8 JSON definition: {error}") from error
            result = memory.define_change_rule(args.key, args.name, definition, args.by, args.source)
        elif args.command == "outline":
            result = memory.outline(args.root, args.parent, args.order, args.as_of, args.depth, args.show)
            if args.format == "text":
                lines: list[str] = []

                def walk(node: dict, level: int) -> None:
                    label = node.get(args.show[0]) if args.show else None
                    lines.append(f"{'  ' * level}{node['name'] if label is None else label}")
                    for child in node.get("children", []):
                        walk(child, level + 1)

                walk(result["root"], 0)
                print("\n".join(lines))
                return
        elif args.command == "history":
            result = memory.fact_history(args.entity, args.predicate)
        elif args.command == "changeset":
            result = memory.changeset_contents(args.key)
        elif args.command == "request-exception":
            result = memory.request_exception(args.instance, args.to, args.by, args.reason, args.at, args.source,
                                              parse_attributes(args.attr))
        elif args.command == "resolve-exception":
            result = memory.resolve_exception(args.request, args.approve, args.by, args.reason, args.at, args.source,
                                              parse_attributes(args.attr))
        elif args.command == "process-overdue":
            result = memory.overdue_instances(args.process, args.at)
        elif args.command == "process-definition":
            result = memory.process_definition(args.key, args.version)
        elif args.command == "define-values":
            result = memory.define_values(args.predicate, args.value, args.by, args.entity_type, args.source)
        elif args.command == "define-event-type":
            result = memory.define_event_type(args.type, args.high_volume, args.by, args.reason, args.source)
        elif args.command == "event-types":
            result = {"as_of": args.as_of, "high_volume": memory.high_volume_types(args.as_of)}
        elif args.command == "process-status":
            result = memory.process_status(args.instance)
        elif args.command == "define-status":
            raw = sys.stdin.buffer.read() if args.file in (None, "-") else Path(args.file).read_bytes()
            try:
                definition = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MemoryError(f"define-status expects a UTF-8 JSON definition: {error}") from error
            result = memory.define_status_rule(args.key, args.name, definition, args.description)
        elif args.command == "search":
            result = memory.search(args.text, args.type, args.limit)
        elif args.command == "describe":
            result = memory.describe(args.entity, args.sample)
        elif args.command == "timeline":
            result = memory.timeline_page(args.entity, args.limit or None, args.since, args.until, args.type, args.include_voided)
        else:
            result = memory.pending(args.person)
        print(json.dumps(localize(result, memory.zone), ensure_ascii=False, indent=2))
    except (MemoryError, sqlite3.Error) as error:
        print(f"Memory operation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
