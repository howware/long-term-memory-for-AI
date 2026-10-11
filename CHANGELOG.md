# Changelog

All notable changes are listed here. Versions follow
[Semantic Versioning](https://semver.org/). The schema has its own history:
the migrations in `db/migrations/`, applied to each memory and recorded in it.

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
