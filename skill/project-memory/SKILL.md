---
name: project-memory
description: Persistent project memory in a local SQLite database. Use it at the start of any conversation in a project (cold start), when the user asks to remember, record or look up facts, decisions, people, events, pending actions or history, when they ask "what do we know / what did we decide / what happened with…?", or when an answer depends on something said in earlier conversations. Also to start the memory in a project that does not have one yet.
---

# Persistent project memory

The memory lives in `.memory/memory.sqlite` at the project root (the folder
with `.git`, or the working directory). SQLite is the source of truth: never
rebuild from chat history what can be looked up there.

Do not use Markdown files (or the agent's automatic memory) to keep facts,
decisions or context: everything goes into this database through the helper.

The helper sits next to this file. In the commands, `MEM` means:

```text
python "<this skill's folder>/scripts/memory.py"
```

In this skill's source repository (`mem`) the same helper is `db/memory.py`.
Run it from the project root so it finds the right memory and not another
project's. All output is JSON in UTF-8.

## Language

Everything internal is in **English**: keys, predicate names, stored codes and
values, summaries and notes you write, process and rule definitions. Talk to
each person in **the language they are using in this interaction**; it can
change with the context (someone may use Spanish for personal matters and
English for work), so follow the conversation, not a fixed preference.
Literal sources (`source.content`, `--source-text`) keep the person's exact
words in whatever language they used.

## 1. Cold start (always, at the beginning)

1. `MEM brief --format text` → what matters now: the memory's owner, the
   project, **decisions and agreements in force** (superseded ones no longer
   appear; an agreement is a person's answer to an open question, marked
   "answers question:…"), conventions, **open questions** (answered ones no
   longer appear), lessons, pending actions and the latest records. Each
   section shows up to 8; when it says "showing 8 of N", the "See all" line
   has the `list` command that returns the rest. **High-volume** event types
   (imported movements, readings, workflow transitions, backups) stay out of
   "Most recent"; "High-volume activity" gives one line per type with how many
   were recorded in the last 7 days. If a session hook already injected it, do
   not repeat it. If it says there is no memory, go to **Start the memory**.
2. For the shape of the data: `MEM catalog`. Note:
   - `entity_types`: small types (channels, categories, seasons) carry their
     `names`, the values to compare against;
   - `predicates`: text or entity predicates with few values carry `values`
     with counts (roles, plans); predicates with a vocabulary carry
     `allowed_values` and, in `outside_vocabulary`, older stored values that
     are no longer allowed;
   - `semantic_rules_and_recipes`: status rules carry their `states`;
   - `processes`, `change_rules`, `recipes`;
   - `people` (above 50, only those with current facts; see `people_total`),
     `actors` (sensors, systems, robots… by kind), `agents`, `event_types`
     (periods, attributes, relations and `high_volume`) and `integrity`.
3. For a specific entity: `MEM search "<text>"` (entities; also finds tracking
   codes and identifiers stored as facts) and then `MEM describe <key>`; for
   its history, `MEM timeline <key>`. To search decisions, notes, sources and
   everything said, `MEM recall "<words>"` (finds singular and plural).

Do not explore tables or migrations to find your way: if the catalog is not
enough, say so; it is a gap in the helper worth recording.

## 2. Start the memory in a new project

1. Ask the user their name and how to identify them (for example
   `person:ana`). Do not invent it or infer it from an email or the system.
2. `MEM init --owner-name "<name>" --owner-key person:<id>`
3. Record the project:
   `MEM create-entity --type project --name "<name>" --key project:<id> --description "<purpose in one sentence>"`
4. Confirm with `MEM catalog` before saying it is ready.

`init` is idempotent: it applies pending migrations and never deletes data. It
creates `.memory/.gitignore` so the database never enters the repository.

Updating the helper needs nothing else: additive migrations (they only add
indexes, tables, columns or views) apply by themselves when the memory is
opened. If one is transformative, commands ask for `MEM init`, which first
copies the database to `.memory/backups/`. Every schema change is recorded as
a `schema_migrated` event and the `brief` shows it for a week. If the `brief`
says **READ ONLY**, a newer helper updated this memory: read, but do not write
until the skill is reinstalled. `MEM where` shows the schema state without
changing anything.

## 3. Look up

| Need | Command |
|---|---|
| What the memory holds | `MEM catalog` |
| Find an entity by name, alias, key, tracking code or an identifier stored as a fact (plate, document number) | `MEM search "<text>" [--type T]` |
| Search everything captured, even unstructured | `MEM recall "<words>"` |
| Facts (with their attributes), incoming relations and activity | `MEM describe <ref> [--sample N]` |
| History of an entity | `MEM timeline <ref> [--limit N] [--since D] [--until D] [--type T]` |
| Everything about an event (attributes, links, literal source) | `MEM event <key or id>` |
| Totals, counts and rankings | `MEM aggregate ...` (below) |
| Which rows | `MEM list ...` (below) |
| Current state and holder | `MEM detail <ref>` |
| Pending actions (for a person: also what waits on the roles they hold) | `MEM pending [--person <ref>]` |

`<ref>` accepts an id, an external key or an exact name. If a name is
ambiguous the helper lists candidates: ask the user instead of choosing.

`timeline` returns the latest 50 events by default and reports `total`; use
filters instead of fetching everything.

### Aggregate without SQL

```text
MEM aggregate --event-type <type> | --entity-type <type>
              [--measure count|sum:X|avg:X|min:X|max:X] [--line PRED]
              [--by ...]... [--where CONDITION]... [--since D] [--until D] [--as-of D]
              [--order value|group] [--limit N] [--include-voided]
```

**Rows.** Events of a type; their lines with `--line PRED` (each PRED link of
the event, with its attributes); or the entities of a type with
`--entity-type` (there `attr:P` and `sum:P` read their fact in force). With
`--as-of D`, event rows are those that occurred before D and entity rows are
those that existed at D (recorded by then, or holding a fact that had begun)
and had not been retired by then.

**Measures.** `count`, or `sum|avg|min|max:X` where X is an attribute of the
row (a bare name: `sum:amount`) or **any path ending in a number**:
`sum:link:recategorizes/attr:amount`, `max:fact:order_type/fact:fee`. The
answer carries the unit of the predicate the path ends in. A path that gives
several values for a row is an error (as in `--by`); an unknown step or a
non-numeric value is an error, never an empty result.

**In force.** A fact is in force at an instant when it began at or before it
(or has no start), had not ended by then and was never retracted (a
retraction means it was never true, so it counts at no instant; a superseded
or closed fact counts over its own period). `fact:P` reads the fact in force
**now**, or at the `--as-of` instant when one is given; a value whose
`valid_from` is in the future is not in force yet, and the value it replaces
stays in force until then. `describe` marks each fact `in_force`.

**Paths.** Steps separated by `/` or `>` (prefer `/`: an unquoted `>` in a
shell redirects the output to a file instead of passing it to the helper):

| Step | Reaches |
|---|---|
| `link:P` | what the record links to with P |
| `link-in:P` or `link-in:P[TYPE]` | the events (of that type) that link to this record with P (with `--as-of`, only those that occurred before it) |
| `fact:P` | the entity's fact **in force now** (or at `--as-of`) |
| `fact@row:P` | the fact **in force at the row's instant** (correct history) |
| `participant:ROLE` | the participant with that role |
| `attr:A` | an attribute of the current record (at the start: of the row) |
| `edge:A` | an attribute of the link or fact just followed (quantity on a line; a presented document's number: `fact:presented_document/edge:document_number`) |
| `since` / `until` | since / until when the fact just followed holds: `fact:process_step/since` is how long it has been in its step (sort, filter, group by period) |
| `key` / `name` | key or name (the name in force, after renames) of the entity or event the path reached (at the start: of the row): `fact:for_customer/key`, `--where "key = order:1007"` |
| `line` | at the start: the target of the line |
| `first(A \| B \| …)` | at the start: the value of the first path that has one for the row |

A path ends in an entity or a typed value (date, number, text, boolean).
Nothing can follow a value.

**`first(A | B | …)`** chooses between paths: the value of a row comes from
one path unless another one overrides it. For example, the category of a
movement comes from its rule unless an exception event points to it:
`first(link-in:recategorizes/link:to_category | link:classified_by/fact:categorized_as)`.
- Works in `--by`, `--column`, `--where`, `--order-by`, `--measure` and recipes.
- Every path must end in the same kind of value (entities, or the same type).
- If a path reaches **several events** (several exceptions), the most recent
  one (by `occurred_at`) wins; voided events never count. Any other path that
  may give several values (a `many` fact, a participant) is checked row by
  row, like `--by`: in `--by`, `--column`, `--order-by` and measures the query
  fails only if some row really reaches several. In `--where` (and status
  rules) it is rejected up front; declare such links `cardinality: one`.
- It may continue when it ends in an entity or an event:
  `first(A | B)/fact:part_of`, `first(A | B)/key`.
  It starts a path; it cannot appear after another step.

**`--by`:** `period:day|week|month|year` (the event's date),
`period:month@PATH` (any reachable date), `line`, `status:<rule>` or any path.
If a path gives **several values** for a row (an order with several products),
grouping by it is an error: filter first, group per line with `--line`, or use
a single-valued path. Namesake entities are shown apart with their key.

**`--where`** (spaces around the operator, because `>` can also separate steps):

| Form | Meaning |
|---|---|
| `PATH = V`, `!=`, `>`, `>=`, `<`, `<=` | compares with the predicate's type; `!=` means "no value is V" |
| `PATH in A|B|C` | any of the values |
| `PATH between A..B` | A ≤ value < B (the upper bound is excluded) |
| `has:PATH` / `no:PATH` | a value exists / does not exist |
| `all:PATH OP V` | **every** value matches (by default one is enough) |
| `status:<rule>=STATE` | the state derived by a status rule |
| `PATH=ENTITY` | older short form, still works |

Dates without a zone mean your display zone; booleans accept true/false;
entities resolve by key or name.

Generic examples: `--where "attr:due_date between 2026-10-01..2026-11-01"`,
`--where "link:contains/fact:active = true"`, `--where "no:link-in:ships"`,
`--line contains --by "line/fact@row:price"`, `--by "period:month@attr:due_date"`.

Forms that are easy to get wrong:
- "has the data" is `has:attr:receipt_number` (not `exists` or `is not null`).
- The current value of another entity takes full steps:
  `fact:order_type/fact:fee` (not `fact:order_type/fee`).
- The key of a linked entity: `fact:for_customer/key`; the row's own key:
  `--column key` or `--where "key = X"`.

The answer reports `rows_without_measure`: rows without the measured
attribute. That is missing data, not zero; say so when answering.

### List rows

When the question asks **which** (not how many), list rows with the same rows
and conditions as `aggregate`:

```text
MEM list --event-type <type> | --entity-type <type> [--line PRED]
         [--column COL]... [--where CONDITION]... [--since D] [--until D] [--as-of D]
         [--order-by COL | --order-by desc:COL]... [--limit N] [--offset N]
```

- `COL`: `key`, `name`, `summary` (the event's text; for entities, their
  description), `occurred_at`, `line`, `status:<rule>`, `period:X[@PATH]` or
  any path. Without `--column`: key, date and who performed it (for entities:
  key and name).
- A column reaching several values in some row (an order's products) comes
  back as a list; otherwise as a plain value.
- The answer carries `total` (matching rows), `returned` and `truncated`; page
  with `--offset`. If `truncated` is true, say so: you have not seen it all.
- Generic example: `MEM list --event-type order --where "attr:due_date between
  2026-10-01..2026-10-08" --column key --column attr:total --column link:contains
  --order-by desc:attr:total`.

### Current state with status rules

When the state of something follows from its follow-up events (a booking that
was cancelled, checked in or completed), do not store it as duplicated data:
declare a **status rule** and the helper computes it on the fly.

```text
MEM define-status --key status:<name> --name "<readable name>" --file rule.json
```

```json
{"applies_to": {"event_type": "reservation_created"},
 "follow_link": "concerns_reservation",
 "states": [
   {"name": "Cancelled with fee", "event_type": "reservation_cancelled", "where": {"cancellation_fee": {">": 0}}},
   {"name": "Early checkout", "event_type": "guest_checked_out",
    "where": {"nights_stayed": {"<": {"subject_attr": "nights_booked"}}}},
   {"name": "In house", "event_type": "guest_checked_in"}],
 "otherwise": "Confirmed"}
```

- States are evaluated **in order**; the first match wins. Each condition is a
  **path from the follow-up event** (a bare name means `attr:NAME`) with `=`,
  `!=`, `>`, `>=`, `<`, `<=`, `in` or `between` against a value, or with
  `{"subject_attr": ATTR}` against an attribute of the subject.
- Use: `aggregate --by status:<name>`, `aggregate --where status:<name>=<state>`,
  `--as-of <date>` for the state at a past date (only what already existed
  counts). `event` and `describe` show the current state.
- Voided follow-up events do not count. Redefining the rule stores a new
  version; the previous one stays in the history.
- Rules appear in `catalog` (`semantic_rules_and_recipes`) with their
  `states`: the only valid values in `status:<name>=<state>`.

### Saved recipes

A question that repeats is saved once as a recipe and reused. Before writing a
query, check `recipes` in `catalog`.

```text
MEM define-recipe --key recipe:<name> --name "<readable name>" --file recipe.json
MEM recipe run recipe:<name> [--param NAME=VALUE]...
```

```json
{"kind": "list",
 "description": "Orders due in the week that starts at start.",
 "parameters": {"start": {"type": "date", "default": "today"}},
 "query": {"event_type": "order", "columns": ["key", "attr:total"],
           "where": ["attr:due_date between {start}..{start+7}"],
           "order_by": ["attr:due_date"]}}
```

- `kind`: `aggregate` or `list`; `query` takes the same arguments as the command.
- Typed parameters: `date`, `datetime`, `integer`, `real`, `text`, `entity`.
  Dates accept `today`, `today+N`, `today-N` (in your zone). In the query,
  `{name}` and `{name+N}` (N days or units).
- Each value is validated by type before substitution; text cannot contain
  operators. The answer shows the resolved parameters and the final query:
  mention them when answering.
- Redefining stores a new version; the previous one stays in the history.

Use SQL only to read, and only when `aggregate`/`list` cannot express the
question; then point out the gap.

## 4. Record

Everything the user tells you is stored with `MEM write`, which takes a JSON
document (stdin or `--file`) and applies it in **one transaction**: if
anything is invalid, nothing is stored. Sending the same document again does
not duplicate.

```json
{
  "source": {"type": "user_statement", "content": "<the user's literal words>",
             "provided_by": "person:<id>", "captured_at": "now"},
  "performed_by": "person:<id>",
  "predicates": [{"id": "quantity", "value_type": "integer", "unit": "piece",
                  "cardinality": "many", "label": "Quantity", "description": "..."}],
  "entities": [{"key": "product:045", "type": "product", "name": "Candy 045", "aliases": ["C45"]}],
  "close": [{"subject": "person:x", "predicate": "holds_role", "value": "role:y", "at": "now"}],
  "facts": [{"subject": "product:045", "predicate": "supplied_by", "value": "supplier:07",
             "valid_from": "2026-01-01T00:00:00Z"}],
  "retract": [{"subject": "...", "predicate": "...", "value": "...", "reason": "wrong data"}],
  "retire": [{"entity": "product:044", "at": "now", "reason": "discontinued", "with_parts": false}],
  "events": [{"key": "RCV-0001", "type": "shipment_received", "occurred_at": "2026-01-05T11:00:00-06:00",
              "summary": "...", "attributes": {"total_units": 200},
              "participants": [{"entity": "person:x", "role": "receiver"}],
              "pending_for": "person:y",
              "links": [{"predicate": "fulfills", "event": "PO-0001"},
                        {"predicate": "contains", "entity": "product:045",
                         "attributes": {"quantity": {"value": 120, "unit": "piece"}}}]}],
  "observations": [{"subject": "machine:oven", "predicate": "temperature", "value": 180.5,
                    "occurred_at": "now", "by": "sensor:t1"}]
}
```

- **Source first:** `source.content` keeps the literal text, in its original
  language. Even what is not structured yet stays retrievable with `recall`.
- **Vocabulary:** declare a predicate only if no equivalent exists. The helper
  rejects names that contain or nearly repeat another (`units` vs
  `units_received`); reuse the existing one or set `distinct_from_similar:
  true` if it really means something else. Check `catalog` before declaring.
- **Types and units:** values are validated against the predicate; a unit
  other than the declared one is rejected.
- **Changes over time:** a `one`-cardinality fact that changes needs
  `valid_from`; the previous value is closed and kept. If the previous value
  was wrong, use `retract` with `reason` (an audited event, and a decision of a
  person).
- **Different names** for one key are kept as aliases; nothing is overwritten.
  Creating an entity with the name of another of the same type leaves a
  `warning`: if it is the same, reuse its key; if not, store something that
  tells them apart (an identifier as a fact, which `search` finds).
- **Renaming** (a section retitled, a company renamed): send the entity with
  `"rename": true` (and `valid_from` if it is not now), or write the standard
  fact `name`. Every output shows the name in force; the entity keeps the name
  it was created with (`describe` shows it as `created_as`), `--as-of` reads
  the name of that time, and both names find it.
- **Retiring** something that is no longer in use (a removed paragraph, a
  closed branch, a discontinued product) is the `retire` section:
  `{"entity", "at", "reason", "with_parts"?}`. Every current fact about it ends
  at `at` and an `entity_retired` event records who and why. If it still has
  parts (`part_of`), it is rejected unless you move them first or set
  `"with_parts": true`. Facts of other entities that point to it are reported
  in `warnings`, not closed. Nothing is deleted: `--as-of` still shows it.
- **Requests that wait on someone** (a comment to answer, a task for another
  person): give the event `"pending_for": "<actor or role>"` and, if useful,
  the attribute `requested_action`. `pending` lists it until an event in force
  links to it with `resolves` (the answer, the work done). Do not leave such a
  request only in free text: nobody would find it as pending. A fact
  `pending_action_for` on an entity (a case waiting on someone) can also carry
  `requested_action` as an attribute.
- **Ending a fact** that was true until some moment (a role, a coverage, a
  pending action) is the `close` section: `{"subject", "predicate",
  "value"?, "at"}` (without `value` it closes the current value). Retracting is
  different: the data was wrong.
- **Instants:** any instant you write (`occurred_at`, `valid_from`, `at`,
  `captured_at`) accepts `"now"`; otherwise ISO-8601 with a zone.
- **Allowed values** (vocabulary): `MEM define-values --predicate P --value A
  --value B --by <person>` sets the only texts P accepts (the list replaces the
  previous one; removed values are closed, not deleted); `--entity-type T` sets
  which entity type an entity predicate points to. `catalog` shows
  `allowed_values` and `outside_vocabulary`.
- **Observations** are high-volume readings (temperature, position, level) of
  a subject, each with the actor that produced it (`by`: the specific sensor or
  system). They carry no per-row evidence.
- **Append-only:** the database rejects (with triggers) deleting rows and
  editing values. A fact is corrected by closing it (the new value's
  `valid_from`) or retracting it with a reason; an empty description may be
  filled once. If a data error cannot be corrected that way, do not patch it
  with SQL: report it to the user.
- **Voiding an event** recorded by mistake (a person decides it):
  `MEM void --event <key> --reason "<why>" [--by person:<id>]`, or the `void`
  section of `write`. The event stays stored but no longer counts in
  `aggregate`, `timeline`, `describe` and `brief`; `--include-voided` shows it
  and `MEM event <key>` says who voided it and why. Voiding the void restores
  it. Nothing is voided in cascade: read the `warnings` about events that
  depended on the voided one. A workflow transition is not voided; it is
  corrected with a compensating transition.
- Read the result's `warnings` (for example attributes that differ from
  earlier events of the same type) and mention them to the user.

`record-event`, `create-entity` and `transition` (the old mechanism, without
versions) also exist for simple cases.

### High-volume event types

When events of a type arrive by the hundred (imported bank movements, sensor
readings, log lines), they would fill the brief's "Most recent" and hide the
decisions and questions a session needs first. A person declares the type
high-volume:

```text
MEM define-event-type --type <type> --high-volume --by person:<id> [--reason "<why>"]
MEM define-event-type --type <type> --normal --by person:<id>     # back to the recent list
MEM event-types [--as-of D]                                       # how each type is treated
```

- It only changes the brief: those events leave "Most recent" and appear as
  one line per type in "High-volume activity". `aggregate`, `list`,
  `timeline`, `describe`, `pending` and `recall` count them as always.
- It is versioned data: a `high_volume` fact on the entity
  `event_type:<type>` and an `event_type_declared` event with who decided it
  and why. Declaring it normal again keeps the earlier value in the history.
- `workflow_transition` and `memory_backed_up` are high-volume by default;
  declaring them `--normal` lists them again.
- An importer should declare its type before the first import.

### Money and exact quantities

Store money, and any quantity that must add up exactly, as an **integer in
its smallest unit**, with the unit declared on the predicate. Never use `real`
for money: binary fractions drift by cents when added.

```json
{"predicates": [{"id": "amount", "value_type": "integer", "unit": "MXN_cent",
                 "description": "Signed amount in cents: outflows negative, inflows positive"}]}
```

```text
MEM aggregate --event-type <type> --measure sum:amount --by period:month
```

- `sum`, `min` and `max` over an integer attribute are exact integers (the
  answer carries `unit`), with negative and positive values, per line
  (`--line`) and per period. Convert when presenting: 1234567 `MXN_cent` is
  $12,345.67 MXN.
- `avg` returns a decimal number: round it when presenting, never when
  storing.
- A value with decimals is rejected by an integer predicate: convert it
  before writing (round half away from zero, the way the source rounds).

### Event keys and re-imports

An event's `key` identifies it for good. Writing an event whose key already
exists is a no-op for that event: it is reported in `events.existing` and the
stored event is **not** changed, even if the new attributes differ (memory is
append-only). So an importer can resend a file safely. If the source later
corrects a record, record the correction as what it is: void the old event
with a reason and write the corrected one under a new key, or write a
follow-up event linked to it. Compare `existing` against what you sent to
notice keys that were already there.

The one thing a resent event adds is **links it does not have yet**: each
link is written once, and links already there are skipped. That is how
several importers share one record (a bank transaction imported by two
tools): the first stores the event, and each one adds its own reading of it
as links (`classified_by`, `covers`…) without duplicating anything.

### Structured documents (books, manuals, contracts…)

A document is entities joined by the standard facts `part_of` (the parent)
and `position` (order among siblings, a number: 2.5 fits between 2 and 3).
Text lives in facts you declare once, **with `"cardinality": "one"`** so an
edit replaces the previous version instead of adding a second value (for
example `title`, `body`, `caption`).

- **Read:** `MEM outline <root> [--show body] [--depth N] [--as-of D]
  [--format text]` returns the tree in order with the facts you ask for.
  `--as-of` reads the structure **and** the text as they were at that instant:
  an earlier draft reads exactly as it was.
- **Write in work sessions:** a session is an event (for example
  `work_session`, `performed_by` the person, `ended_at`). Every `write` made in
  it declares `"changeset": "<session key>"`; facts asserted, closed or
  retracted and events written then carry it. `MEM changeset <key>` lists what
  a session changed and `MEM history <entity> [--predicate body]` lists every
  version of a fact with the session and the actor behind it.
- **Edit** = a new `body` with `valid_from`. **Move** = a new `part_of` and/or
  `position`. **Remove** = `retire` the node (all its facts end; with
  `with_parts` its paragraphs go too). **Retitle** = `rename` (outputs follow
  the new title). **Split** = a new node and moves. Nothing is deleted:
  `--as-of` and `history` still see it.
- **Editor comments and suggestions** are events with `pending_for` the person
  who must answer; the answer (accepted, rejected, done) is an event that
  `resolves` it, so `pending` shows what is still open.
- **Search:** `MEM recall "<words>"` finds text facts, current and earlier
  versions (`version`: current, superseded…), and long text attributes.

### Actors

- Every record names **the specific actor behind it**. An actor is an entity
  of an actor kind: `person`, `agent`, `robot`, `sensor`, `system`, `webhook`,
  `device`, `service`; more kinds are data:
  `MEM define-values --predicate actor_kind --value drone --by <person>`.
- `performed_by` is the actor that did it: the person who made a payment, the
  webhook that notified it, the sensor that raised an alarm. If you do not
  know who or what did it, ask.
- **Decisions belong to people:** decisions, conventions, preferences and
  answers (`remember`), voids, retractions, change rules, publishing a process,
  migration plans and resolving exceptions require a `person`.
- The agent that writes is recorded as `recorded_by` automatically (Claude
  Code is detected; another agent is named with `MEMORY_AGENT=agent:<name>`).
  An agent is `performed_by` only for what it did itself, never for what a
  person said or decided.
- What **you** (the agent) observe or learn is stored with
  `MEM remember --as-agent --kind lesson|note|open_question|milestone ...`.
  It is recorded as `observed_by` (agent) with source `agent_observation` and
  is never attributed to a person. You may answer (`--kind answer --resolves
  ...`) only the open questions you raised yourself.

### Versioned processes

A **process** defines steps and transitions; an **instance** (a case, an
order) follows it. They are not the same: changing the process does not touch
running instances.

```text
MEM define-process --key process:<id> --name N --by <owner> [--owner <person>] [--from D] [--reason R] --file def.json
MEM start-process <instance> --process process:<id> --by <actor> [--at D] [--attr K=V]...
MEM advance <instance> --to "<step>" --by <actor> [--at D] [--pending-for <person|role>] [--reason R] [--attr K=V]...
MEM process-status <instance>
MEM process-definition process:<id> [--version N]   # JSON ready to edit and publish
MEM migrate-process <instance> --to-version N --by <owner> --reason "<plan>" [--map OLD=NEW]... [--pending-for <person|role>]
MEM request-exception <instance> --to "<step>" --by <actor> --reason "<what is missing or why>" [--attr K=V]...
MEM resolve-exception exreq:<instance>:<n> --approve|--deny --by <person> --reason "..." [--attr K=V]...
MEM process-overdue [--process process:<id>] [--at D]
```

- Definition: `{"states": [{"name", "terminal"?, "pending"?: "role:…",
  "due_hours"?: N}], "transitions": [{"from": step|null, "to": step, "roles":
  ["role:…"]}], "override_roles": ["role:…"]}`. `from: null` starts an
  instance; `pending` is who holds the action in that step (role or person).
- Roles are `role:…` entities; actors hold them through `holds_role` facts
  with validity (a temporary coverage has `valid_to`; a role that ends is
  closed with `close`).
- Only the owner (`process_owner`) publishes versions; each is numbered
  automatically. An instance is bound to the version in force when it started
  and is always validated against it (`process-status` shows `version` and
  `latest_version`).
- An actor without the transition's role is rejected. One holding an
  `override_roles` role may take it, or jump to another step of its version,
  with `--reason`; the event keeps `override = true`, `override_kind`
  (`role_not_allowed`, `outside_process`, `missing_requirement`,
  `missing_capture`, `capture_mismatch`) and the reason. A step that does not
  exist in the version is a change to the process, not an exception.
- A transition may require data on the instance: `"requires":
  ["tracking_code"]` asks for that current fact before the step.
- `"requires_each": [{"of": "order_type>requires_document", "in":
  "presented_document"}]` reads a list on another entity (the documents the
  instance's type requires) and asks for each item among the instance's `in`
  facts. The list is data: it changes without a new process version.
  `process-status` shows what each next step still lacks in `next[].missing`.
- `"captures": [{"attribute": P}, {"attribute": P2, "when": {"P": "value"}},
  {"attribute": R, "equals_earlier": R}]` makes the step record that data with
  `--attr` (P2 only when P has that value; R must equal what an earlier step
  of the same instance recorded). Predicates must exist when publishing.
- If an exception skipped data (it jumped over a step with `captures`), the
  event keeps it in `skipped_data` and `process-status` warns about it: do not
  assume that data exists.
- To tell an instance's history use `process-status` → `steps`: one row per
  step (entered, left, who worked it = who moved it out of that step,
  exceptions, migrations noted on the step) and the current step last with
  who holds it. Do not present transitions as "target step + who moved it".
- **Exceptions are part of the process:** whoever cannot take a step asks with
  `request-exception` (an event of the instance; the action passes to the
  first `override_roles` role, and `pending --person` of someone holding that
  role shows it). `resolve-exception --approve` takes the step in the same
  transaction, linked to the request; `--deny` returns the action to whoever
  holds the step. `process-status` lists `exception_requests` with their state
  (`open`, `approved`, `denied`).
- **Older, laxer version:** an instance follows its version, but when the
  version in force would have stopped the step, the event notes it and
  `process-status` warns about it.
- **Migrating** is an explicit plan of the owner, noted on the step (it does
  not split the history). `--pending-for` hands the migrated instance to whoever
  will handle it.
- **Due times:** a state may declare `"due_hours": 48`. `process-status` shows
  `due` and `process-overdue` lists instances past the due time of their step.
- **Types nobody could start:** with `produced_by` declared (which type
  produces each required item), `write` and `catalog` (`unstartable_types`)
  warn about types nobody could start because every producer of something they
  require is itself blocked. A loop with a way in from outside is not reported.
- **Change rules:** `MEM define-change-rule --key change:<id> --name N --by
  <person> --file rule.json` with `{"subject_type": T, "predicates": "*" |
  [P…], "roles": ["role:…"], "create"?: true}`. From then on every `write` that
  changes, closes or retracts those facts of entities of type T (or creates
  one, with `create`) requires `performed_by` to hold one of those roles; if
  not, the whole document is rejected. A rule is changed only by whoever holds
  its roles. Before recording an order to change something, check
  `change_rules` in `catalog`; if it is rejected, do not record it as a
  decision: tell the user who can.
- Tracking code: `MEM assign-code --entity <instance> --performed-by <actor>
  [--at D] [--source "<words>"]` generates a unique 8-character code without
  ambiguous letters, with its evidence. The code works as a reference in
  `describe`, `process-status` and `search`.
- Queries: `list --entity-type <type> --column fact:process_step --column
  fact:process_step/since --column fact:pending_action_for`; exceptions:
  `list --event-type process_transition --where "attr:override = true"
  --column participant:performed_by --column attr:rationale`; by version:
  `aggregate --by attr:process_version`. Processes appear in `catalog`
  (`processes`).

Rules:

- Instants with `Z` or an offset (`-06:00`), or `"now"`; stored in UTC.
  Without a zone they are rejected: ask for the zone instead of assuming it.
  `occurred_at` is when it happened; the record keeps separately when it was
  written.
- Do not invent actors, dates, identifiers, quantities or states.
- Do not create tables per domain: everything is an entity, predicate, event,
  link, attribute, assertion or observation.
- Verify with a query (`event`, `describe`, `aggregate`) before saying
  something was stored.

### What is worth remembering in a project

Record what a new chat would need and cannot infer from the code: decisions
and their why (`decision`), agreed conventions (`convention`), open questions
(`open_question`), user preferences (`preference`) and milestones
(`milestone`), with `remember --about project:<id>`. Do not record what is
already in the code or the Git history.

### Time zone

Everything is stored in UTC. The helper's output is shown in the system's
time zone (`brief` and `catalog` report it in `time_zone`); change it with
`--tz <IANA|UTC>` or `MEMORY_TZ`. In queries, `--since/--until` without a zone
mean the display zone and `period:*` groups in that zone. When **writing**, a
date without a zone is rejected: ask in which zone it happened.

Where an organization (or any entity) operates is stored as a `time_zone`
fact (an IANA name or a fixed offset such as `-05:00`). If it differs from the
display zone, the `brief` says so and `aggregate`/`list` with dates or periods
carry `time_zone_notice`. To answer "how many arrive on day X?" as the
operation lives it, repeat with `--tz=<its zone>` (with `=` when it starts
with `-`) and say which zone you used.

## 5. When answering

Always separate **retrieved facts** (with date and actor), **absences** (what
the memory does not have) and **inferences**. If the catalog shows different
attributes for one event type in different periods, warn before aggregating.
A period without records is missing data, not zero. Answer in the language the
person is using.

## Security

The database is local and private. Do not copy, upload or share it without
explicit authorization. Recording data does not authorize sending emails,
making payments or changing external systems.
