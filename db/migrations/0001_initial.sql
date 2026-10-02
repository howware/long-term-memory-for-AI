-- kind: additive
-- The operational memory is domain-neutral. Product concepts such as tickets,
-- habits and contacts are represented as entities and assertions, never tables.
-- All timestamps must use ISO-8601 UTC text (for example 2026-09-28T18:30:00Z).

BEGIN IMMEDIATE;

CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    external_key TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    retired_at TEXT,
    CHECK (retired_at IS NULL OR datetime(retired_at) >= datetime(created_at))
) STRICT;

CREATE UNIQUE INDEX entities_external_key_unique
    ON entities(external_key)
    WHERE external_key IS NOT NULL;

CREATE INDEX entities_name_lookup
    ON entities(canonical_name COLLATE NOCASE);

CREATE INDEX entities_type_lookup
    ON entities(entity_type, canonical_name COLLATE NOCASE);

CREATE TABLE entity_names (
    entity_name_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    name TEXT NOT NULL,
    name_kind TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_from IS NULL
           OR datetime(valid_to) > datetime(valid_from))
) STRICT;

CREATE UNIQUE INDEX entity_names_unique
    ON entity_names(entity_id, name COLLATE NOCASE, name_kind, valid_from);

CREATE INDEX entity_names_lookup
    ON entity_names(name COLLATE NOCASE);

CREATE TABLE predicates (
    predicate_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'any'
        CHECK (value_type IN ('any', 'text', 'integer', 'real', 'boolean',
                             'datetime', 'json', 'entity')),
    cardinality TEXT NOT NULL DEFAULT 'many'
        CHECK (cardinality IN ('one', 'many')),
    is_temporal INTEGER NOT NULL DEFAULT 1
        CHECK (is_temporal IN (0, 1)),
    description TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_reference TEXT,
    content TEXT,
    captured_at TEXT,
    recorded_at TEXT NOT NULL,
    captured_by_entity_id TEXT REFERENCES entities(entity_id),
    content_hash TEXT,
    CHECK (content IS NOT NULL OR source_reference IS NOT NULL)
) STRICT;

CREATE INDEX evidence_captured_at_lookup
    ON evidence(captured_at);

CREATE TABLE assertions (
    assertion_id TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    predicate_id TEXT NOT NULL REFERENCES predicates(predicate_id),
    value_type TEXT NOT NULL
        CHECK (value_type IN ('text', 'integer', 'real', 'boolean',
                             'datetime', 'json', 'entity')),
    value_text TEXT,
    value_integer INTEGER,
    value_real REAL,
    value_boolean INTEGER CHECK (value_boolean IN (0, 1)),
    value_datetime TEXT,
    value_json TEXT,
    value_entity_id TEXT REFERENCES entities(entity_id),
    valid_from TEXT,
    valid_to TEXT,
    assertion_state TEXT NOT NULL DEFAULT 'asserted'
        CHECK (assertion_state IN ('asserted', 'retracted', 'superseded')),
    confidence REAL NOT NULL DEFAULT 1.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    recorded_at TEXT NOT NULL,
    recorded_by_entity_id TEXT REFERENCES entities(entity_id),
    CHECK (valid_to IS NULL OR valid_from IS NULL
           OR datetime(valid_to) > datetime(valid_from)),
    CHECK (
        (value_type = 'text' AND value_text IS NOT NULL
            AND value_integer IS NULL AND value_real IS NULL
            AND value_boolean IS NULL AND value_datetime IS NULL
            AND value_json IS NULL AND value_entity_id IS NULL)
        OR
        (value_type = 'integer' AND value_text IS NULL
            AND value_integer IS NOT NULL AND value_real IS NULL
            AND value_boolean IS NULL AND value_datetime IS NULL
            AND value_json IS NULL AND value_entity_id IS NULL)
        OR
        (value_type = 'real' AND value_text IS NULL
            AND value_integer IS NULL AND value_real IS NOT NULL
            AND value_boolean IS NULL AND value_datetime IS NULL
            AND value_json IS NULL AND value_entity_id IS NULL)
        OR
        (value_type = 'boolean' AND value_text IS NULL
            AND value_integer IS NULL AND value_real IS NULL
            AND value_boolean IS NOT NULL AND value_datetime IS NULL
            AND value_json IS NULL AND value_entity_id IS NULL)
        OR
        (value_type = 'datetime' AND value_text IS NULL
            AND value_integer IS NULL AND value_real IS NULL
            AND value_boolean IS NULL AND value_datetime IS NOT NULL
            AND value_json IS NULL AND value_entity_id IS NULL)
        OR
        (value_type = 'json' AND value_text IS NULL
            AND value_integer IS NULL AND value_real IS NULL
            AND value_boolean IS NULL AND value_datetime IS NULL
            AND value_json IS NOT NULL AND value_entity_id IS NULL)
        OR
        (value_type = 'entity' AND value_text IS NULL
            AND value_integer IS NULL AND value_real IS NULL
            AND value_boolean IS NULL AND value_datetime IS NULL
            AND value_json IS NULL AND value_entity_id IS NOT NULL)
    )
) STRICT;

CREATE INDEX assertions_subject_predicate_time
    ON assertions(subject_entity_id, predicate_id, valid_from, valid_to);

CREATE INDEX assertions_entity_value_lookup
    ON assertions(value_entity_id)
    WHERE value_entity_id IS NOT NULL;

CREATE INDEX assertions_current_lookup
    ON assertions(predicate_id, subject_entity_id)
    WHERE assertion_state = 'asserted' AND valid_to IS NULL;

CREATE TABLE assertion_evidence (
    assertion_id TEXT NOT NULL REFERENCES assertions(assertion_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    role TEXT NOT NULL DEFAULT 'supports',
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (assertion_id, evidence_id, role)
) STRICT;

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    summary TEXT,
    occurred_at TEXT NOT NULL,
    ended_at TEXT,
    recorded_at TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (ended_at IS NULL OR datetime(ended_at) >= datetime(occurred_at))
) STRICT;

CREATE INDEX events_time_lookup
    ON events(occurred_at, ended_at);

CREATE INDEX events_type_time_lookup
    ON events(event_type, occurred_at);

CREATE TABLE event_participants (
    event_id TEXT NOT NULL REFERENCES events(event_id),
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    role TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (event_id, entity_id, role)
) STRICT;

CREATE INDEX event_participants_entity_lookup
    ON event_participants(entity_id, role, event_id);

CREATE TABLE event_evidence (
    event_id TEXT NOT NULL REFERENCES events(event_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    role TEXT NOT NULL DEFAULT 'supports',
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (event_id, evidence_id, role)
) STRICT;

-- Links may connect any durable record, including an event that follows a
-- procedure step or a comment that answers another comment. Referential
-- integrity for polymorphic endpoints is validated by the memory engine.
CREATE TABLE record_links (
    record_link_id TEXT PRIMARY KEY,
    source_record_type TEXT NOT NULL
        CHECK (source_record_type IN ('entity', 'assertion', 'event', 'evidence', 'context')),
    source_record_id TEXT NOT NULL,
    predicate_id TEXT NOT NULL REFERENCES predicates(predicate_id),
    target_record_type TEXT NOT NULL
        CHECK (target_record_type IN ('entity', 'assertion', 'event', 'evidence', 'context')),
    target_record_id TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_from IS NULL
           OR datetime(valid_to) > datetime(valid_from))
) STRICT;

CREATE INDEX record_links_source_lookup
    ON record_links(source_record_type, source_record_id, predicate_id);

CREATE INDEX record_links_target_lookup
    ON record_links(target_record_type, target_record_id, predicate_id);

CREATE TABLE contexts (
    context_id TEXT PRIMARY KEY,
    context_type TEXT NOT NULL,
    name TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_from IS NULL
           OR datetime(valid_to) > datetime(valid_from))
) STRICT;

CREATE TABLE context_members (
    context_id TEXT NOT NULL REFERENCES contexts(context_id),
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    weight REAL NOT NULL DEFAULT 1.0 CHECK (weight >= 0.0),
    reason TEXT,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (context_id, entity_id, valid_from),
    CHECK (valid_to IS NULL OR valid_from IS NULL
           OR datetime(valid_to) > datetime(valid_from))
) STRICT;

CREATE INDEX context_members_entity_lookup
    ON context_members(entity_id, valid_from, valid_to);

CREATE VIEW current_assertions AS
SELECT a.*
FROM assertions AS a
WHERE a.assertion_state = 'asserted'
  AND (a.valid_from IS NULL OR datetime(a.valid_from) <= datetime('now'))
  AND (a.valid_to IS NULL OR datetime(a.valid_to) > datetime('now'));

CREATE TRIGGER assertions_validate_predicate_value_type
BEFORE INSERT ON assertions
FOR EACH ROW
WHEN (SELECT value_type FROM predicates WHERE predicate_id = NEW.predicate_id) <> 'any'
 AND (SELECT value_type FROM predicates WHERE predicate_id = NEW.predicate_id) <> NEW.value_type
BEGIN
    SELECT RAISE(ABORT, 'assertion value type does not match predicate definition');
END;

CREATE TRIGGER assertions_prevent_overlapping_single_values
BEFORE INSERT ON assertions
FOR EACH ROW
WHEN NEW.assertion_state = 'asserted'
 AND EXISTS (
    SELECT 1 FROM predicates
    WHERE predicate_id = NEW.predicate_id
      AND cardinality = 'one'
      AND is_temporal = 1
 )
 AND EXISTS (
    SELECT 1
    FROM assertions AS existing
    WHERE existing.subject_entity_id = NEW.subject_entity_id
      AND existing.predicate_id = NEW.predicate_id
      AND existing.assertion_state = 'asserted'
      AND COALESCE(datetime(existing.valid_from), datetime('0001-01-01'))
          < COALESCE(datetime(NEW.valid_to), datetime('9999-12-31'))
      AND COALESCE(datetime(NEW.valid_from), datetime('0001-01-01'))
          < COALESCE(datetime(existing.valid_to), datetime('9999-12-31'))
 )
BEGIN
    SELECT RAISE(ABORT, 'overlapping intervals for a single-valued temporal predicate');
END;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0001_initial', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
