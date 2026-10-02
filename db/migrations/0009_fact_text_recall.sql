-- kind: additive
-- Full-text recall reaches text stored as facts (a paragraph, a definition),
-- every version of it, and long text attributes (a suggested rewrite). It only
-- adds triggers and index rows: no stored row is changed.

BEGIN IMMEDIATE;

INSERT INTO memory_text(record_type, record_id, body)
SELECT 'assertion', assertion_id, value_text FROM assertions WHERE value_text IS NOT NULL;

INSERT INTO memory_text(record_type, record_id, body)
SELECT 'attribute', attribute_id, value_text FROM record_attributes
 WHERE value_text IS NOT NULL AND length(value_text) >= 20;

CREATE TRIGGER assertions_index_text AFTER INSERT ON assertions
WHEN NEW.value_text IS NOT NULL
BEGIN
    INSERT INTO memory_text(record_type, record_id, body) VALUES ('assertion', NEW.assertion_id, NEW.value_text);
END;

CREATE TRIGGER record_attributes_index_text AFTER INSERT ON record_attributes
WHEN NEW.value_text IS NOT NULL AND length(NEW.value_text) >= 20
BEGIN
    INSERT INTO memory_text(record_type, record_id, body) VALUES ('attribute', NEW.attribute_id, NEW.value_text);
END;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0009_fact_text_recall', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
