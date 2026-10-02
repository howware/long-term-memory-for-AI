# Changelog

All notable changes are listed here. Versions follow
[Semantic Versioning](https://semver.org/). The schema has its own history:
the migrations in `db/migrations/`, applied to each memory and recorded in it.

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
