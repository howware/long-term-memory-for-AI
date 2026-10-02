-- kind: additive
-- Every observation names the actor that produced it (which sensor, which
-- system): readings of many sources about one subject stay distinguishable.
-- Only a new nullable column and an index: no row is rewritten.

BEGIN IMMEDIATE;

ALTER TABLE observations ADD COLUMN source_entity_id TEXT REFERENCES entities(entity_id);

CREATE INDEX observations_source_time
    ON observations(source_entity_id, occurred_at)
    WHERE source_entity_id IS NOT NULL;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0008_observation_source', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
