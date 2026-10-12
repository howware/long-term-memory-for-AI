# Changelog

All notable changes are listed here. Versions follow
[Semantic Versioning](https://semver.org/). The schema has its own history:
the migrations in `db/migrations/`, applied to each memory and recorded in it.

## 0.4.0

- **A resent keyed event gains the links it lacks.** The stored event and its
  attributes still never change, but links in the new document that the event
  does not have yet are added, once (links already there are skipped). Several
  importers can now share one record, such as a bank transaction imported by
  two howwares, and each add its own reading of it. Previously the whole event,
  links included, was skipped.

## 0.3.0

- **Behavior change: `fact:` no longer shows values that start in the
  future.** "In force" is now one definition everywhere (paths, conditions,
  `describe`, `detail`, `pending`, `catalog`, the brief and the read views): a
  fact began at or before the instant, had not ended and was never retracted.
  A value written today with a future `valid_from` waits for its start, and
  the value it replaces stays in force until then. `describe` marks each fact
  `in_force`; recall labels a future version `future`.
- **`--as-of` reads facts and entity rows at that instant.** `fact:` steps
  (in columns, `--by`, `--where`, `has:`/`no:`, measures) read the fact in
  force at the instant, `since`/`until` refer to it, `link-in:` counts only
  events that occurred before it, and entity rows are those that existed then
  (recorded, or holding a fact that had begun) and were not yet retired.
  Previously `fact:` always read today's value and entity rows were today's.
- **`first(A | B | ...)`**: the value of the first path that has one, in
  `--by`, `--column`, `--where`, `--order-by`, measures and recipes. Several
  events in a path: the most recent wins; voided events never count. Another
  path that may give several values is checked row by row as in `--by`
  (rejected up front only in `--where` and status rules). It may continue
  after an entity or an event (`first(A | B)/key`).
- `--as-of` and `--since`/`--until` far in the future (`9999-12-31`) no longer
  fail on Windows; an out-of-range bound is a clear error.
- The reads in capture (tracking codes, vocabularies, high-volume types,
  change rules) use the same "in force" definition; the write-side checks
  that close the latest value keep looking at the latest one, even if it
  starts in the future.
- **Measures through a path**: `sum|avg|min|max:PATH` for any path ending in
  a number, with the unit of its last predicate. An unknown step, a
  non-numeric value or several values per row is now an error instead of an
  empty result.
- Migration `0011_facts_in_force` (additive): recreates the read views with
  the new definition and adds an index for facts that end later than an
  instant.

## 0.2.0

- **High-volume event types.** `define-event-type --type T --high-volume --by
  person:<id>` keeps a type out of the brief's "Most recent"; the brief shows
  one line of activity per type instead (`high_volume_activity`). The
  declaration is a versioned fact on `event_type:<type>` with an
  `event_type_declared` event (who and why); `--normal` reverts it and
  `event-types --as-of` reads it at any instant. `catalog` marks
  `high_volume` in `event_types`. Everything else counts these events as
  before.
- `workflow_transition` and `memory_backed_up`, previously hard-coded out of
  the recent list, are now the defaults of the same mechanism; they appear in
  the activity line and can be listed again with `--normal`.
- SKILL.md documents money and exact quantities (integers in the smallest
  unit, `unit` on the predicate) and what happens when an event key is sent
  again.
- Possible future improvement: a native `decimal` value type. For now,
  integers in the smallest unit are exact for sums, minimums and maximums.

## 0.1.0 — first public release

- Persistent project memory in a local SQLite file (`.memory/memory.sqlite`),
  with a portable skill for Claude Code and Codex and session hooks.
- Generic, append-only model: entities, temporal facts, events, links,
  attributes, observations and full-text recall, every record with its
  literal evidence and the actor behind it.
- Capture: `remember` for decisions, conventions, preferences, questions,
  answers, lessons and notes; `write` for any atomic JSON document; renames,
  retirements, retractions and voids that keep the history.
- Queries without SQL: `aggregate`, `list`, query paths, status rules, saved
  recipes, `pending`, `--as-of`.
- Versioned processes with roles, requirements, exceptions, deadlines and
  explicit migrations; change rules for who may change what.
- Structured documents: `outline`, `history` and `changeset` (work sessions).
- Additive migrations apply on open; transformative ones back up first.
- `backup` makes a verified copy; Python 3.9+ standard library only.
