-- kind: additive
-- Generic high-volume observations. This is intentionally not a sensor- or
-- telemetry-specific table: any entity can be observed through any predicate.

BEGIN IMMEDIATE;

CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
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
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    quality TEXT NOT NULL DEFAULT 'observed',
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

CREATE INDEX observations_subject_predicate_time
    ON observations(subject_entity_id, predicate_id, occurred_at);

CREATE INDEX observations_predicate_time
    ON observations(predicate_id, occurred_at);

CREATE TRIGGER observations_validate_predicate_value_type
BEFORE INSERT ON observations
FOR EACH ROW
WHEN (SELECT value_type FROM predicates WHERE predicate_id = NEW.predicate_id) <> 'any'
 AND (SELECT value_type FROM predicates WHERE predicate_id = NEW.predicate_id) <> NEW.value_type
BEGIN
    SELECT RAISE(ABORT, 'observation value type does not match predicate definition');
END;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0004_observations', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
