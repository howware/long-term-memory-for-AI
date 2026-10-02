-- kind: transformative
-- Lossless, consistent capture for any domain without domain tables:
-- typed attributes on events, links and assertions (for example the quantity
-- of one product inside a shipment), idempotent event keys, predicate units,
-- canonical UTC timestamps and full-text recall over sources and summaries.

BEGIN IMMEDIATE;

ALTER TABLE predicates ADD COLUMN unit TEXT;

ALTER TABLE events ADD COLUMN external_key TEXT;

CREATE UNIQUE INDEX events_external_key_unique
    ON events(external_key)
    WHERE external_key IS NOT NULL;

CREATE UNIQUE INDEX evidence_content_hash_lookup
    ON evidence(source_type, content_hash)
    WHERE content_hash IS NOT NULL;

CREATE TABLE record_attributes (
    attribute_id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL
        CHECK (record_type IN ('event', 'record_link', 'assertion')),
    record_id TEXT NOT NULL,
    predicate_id TEXT NOT NULL REFERENCES predicates(predicate_id),
    value_type TEXT NOT NULL
        CHECK (value_type IN ('text', 'integer', 'real', 'boolean', 'datetime', 'json', 'entity')),
    value_text TEXT,
    value_integer INTEGER,
    value_real REAL,
    value_boolean INTEGER CHECK (value_boolean IN (0, 1)),
    value_datetime TEXT,
    value_json TEXT,
    value_entity_id TEXT REFERENCES entities(entity_id),
    unit TEXT,
    evidence_id TEXT REFERENCES evidence(evidence_id),
    recorded_at TEXT NOT NULL,
    CHECK (
        (value_type = 'text' AND value_text IS NOT NULL
            AND value_integer IS NULL AND value_real IS NULL AND value_boolean IS NULL
            AND value_datetime IS NULL AND value_json IS NULL AND value_entity_id IS NULL)
        OR (value_type = 'integer' AND value_text IS NULL
            AND value_integer IS NOT NULL AND value_real IS NULL AND value_boolean IS NULL
            AND value_datetime IS NULL AND value_json IS NULL AND value_entity_id IS NULL)
        OR (value_type = 'real' AND value_text IS NULL AND value_integer IS NULL
            AND value_real IS NOT NULL AND value_boolean IS NULL
            AND value_datetime IS NULL AND value_json IS NULL AND value_entity_id IS NULL)
        OR (value_type = 'boolean' AND value_text IS NULL AND value_integer IS NULL
            AND value_real IS NULL AND value_boolean IS NOT NULL
            AND value_datetime IS NULL AND value_json IS NULL AND value_entity_id IS NULL)
        OR (value_type = 'datetime' AND value_text IS NULL AND value_integer IS NULL
            AND value_real IS NULL AND value_boolean IS NULL
            AND value_datetime IS NOT NULL AND value_json IS NULL AND value_entity_id IS NULL)
        OR (value_type = 'json' AND value_text IS NULL AND value_integer IS NULL
            AND value_real IS NULL AND value_boolean IS NULL
            AND value_datetime IS NULL AND value_json IS NOT NULL AND value_entity_id IS NULL)
        OR (value_type = 'entity' AND value_text IS NULL AND value_integer IS NULL
            AND value_real IS NULL AND value_boolean IS NULL
            AND value_datetime IS NULL AND value_json IS NULL AND value_entity_id IS NOT NULL)
    )
) STRICT;

CREATE INDEX record_attributes_record_lookup
    ON record_attributes(record_type, record_id, predicate_id);

CREATE INDEX record_attributes_predicate_lookup
    ON record_attributes(predicate_id, record_type, record_id);

CREATE TRIGGER record_attributes_validate_predicate_value_type
BEFORE INSERT ON record_attributes
FOR EACH ROW
WHEN (SELECT value_type FROM predicates WHERE predicate_id = NEW.predicate_id) <> 'any'
 AND (SELECT value_type FROM predicates WHERE predicate_id = NEW.predicate_id) <> NEW.value_type
BEGIN
    SELECT RAISE(ABORT, 'attribute value type does not match predicate definition');
END;

-- Canonical instants: 'YYYY-MM-DDTHH:MM:SS[.fraction]Z'. Mixed formats compare
-- incorrectly as text, so new rows must already be normalized by the helper.
CREATE TRIGGER events_require_utc_instants
BEFORE INSERT ON events
FOR EACH ROW
WHEN NOT (NEW.occurred_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*Z'
          AND julianday(NEW.occurred_at) IS NOT NULL)
  OR (NEW.ended_at IS NOT NULL AND NOT (
          NEW.ended_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*Z'
          AND julianday(NEW.ended_at) IS NOT NULL))
BEGIN
    SELECT RAISE(ABORT, 'event instants must be ISO-8601 UTC ending in Z');
END;

CREATE TRIGGER assertions_require_utc_validity
BEFORE INSERT ON assertions
FOR EACH ROW
WHEN (NEW.valid_from IS NOT NULL AND NOT (
          NEW.valid_from GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*Z'
          AND julianday(NEW.valid_from) IS NOT NULL))
  OR (NEW.valid_to IS NOT NULL AND NOT (
          NEW.valid_to GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*Z'
          AND julianday(NEW.valid_to) IS NOT NULL))
BEGIN
    SELECT RAISE(ABORT, 'assertion validity must be ISO-8601 UTC ending in Z');
END;

-- Full-text recall of anything captured, including what has not been
-- structured yet. Accents are ignored so "decision" finds "decisión".
CREATE VIRTUAL TABLE memory_text USING fts5(
    record_type UNINDEXED,
    record_id UNINDEXED,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

INSERT INTO memory_text(record_type, record_id, body)
SELECT 'evidence', evidence_id, content FROM evidence WHERE content IS NOT NULL;

INSERT INTO memory_text(record_type, record_id, body)
SELECT 'event', event_id, summary FROM events WHERE summary IS NOT NULL;

INSERT INTO memory_text(record_type, record_id, body)
SELECT 'entity', entity_id, canonical_name || COALESCE(' — ' || description, '') FROM entities;

CREATE TRIGGER evidence_index_text AFTER INSERT ON evidence
WHEN NEW.content IS NOT NULL
BEGIN
    INSERT INTO memory_text(record_type, record_id, body) VALUES ('evidence', NEW.evidence_id, NEW.content);
END;

CREATE TRIGGER events_index_text AFTER INSERT ON events
WHEN NEW.summary IS NOT NULL
BEGIN
    INSERT INTO memory_text(record_type, record_id, body) VALUES ('event', NEW.event_id, NEW.summary);
END;

CREATE TRIGGER entities_index_text AFTER INSERT ON entities
BEGIN
    INSERT INTO memory_text(record_type, record_id, body)
    VALUES ('entity', NEW.entity_id, NEW.canonical_name || COALESCE(' — ' || NEW.description, ''));
END;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0005_generic_capture', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
