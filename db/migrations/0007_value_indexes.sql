-- kind: additive
-- Value indexes so conditions can start from the matching values instead of
-- checking every row of a type: "which events have due_date in this range",
-- "which entities have floor = 5". Partial indexes keep each one to the rows
-- that actually hold that kind of value. Only indexes: no data changes.

BEGIN IMMEDIATE;

CREATE INDEX record_attributes_datetime_value ON record_attributes(predicate_id, value_datetime, record_type, record_id)
    WHERE value_datetime IS NOT NULL;
CREATE INDEX record_attributes_real_value ON record_attributes(predicate_id, value_real, record_type, record_id)
    WHERE value_real IS NOT NULL;
CREATE INDEX record_attributes_integer_value ON record_attributes(predicate_id, value_integer, record_type, record_id)
    WHERE value_integer IS NOT NULL;
CREATE INDEX record_attributes_text_value ON record_attributes(predicate_id, value_text, record_type, record_id)
    WHERE value_text IS NOT NULL;
CREATE INDEX record_attributes_boolean_value ON record_attributes(predicate_id, value_boolean, record_type, record_id)
    WHERE value_boolean IS NOT NULL;
CREATE INDEX record_attributes_entity_value ON record_attributes(predicate_id, value_entity_id, record_type, record_id)
    WHERE value_entity_id IS NOT NULL;

-- Current facts only: history stays reachable through the existing indexes.
CREATE INDEX assertions_current_datetime_value ON assertions(predicate_id, value_datetime, subject_entity_id)
    WHERE assertion_state = 'asserted' AND valid_to IS NULL AND value_datetime IS NOT NULL;
CREATE INDEX assertions_current_real_value ON assertions(predicate_id, value_real, subject_entity_id)
    WHERE assertion_state = 'asserted' AND valid_to IS NULL AND value_real IS NOT NULL;
CREATE INDEX assertions_current_integer_value ON assertions(predicate_id, value_integer, subject_entity_id)
    WHERE assertion_state = 'asserted' AND valid_to IS NULL AND value_integer IS NOT NULL;
CREATE INDEX assertions_current_text_value ON assertions(predicate_id, value_text, subject_entity_id)
    WHERE assertion_state = 'asserted' AND valid_to IS NULL AND value_text IS NOT NULL;
CREATE INDEX assertions_current_boolean_value ON assertions(predicate_id, value_boolean, subject_entity_id)
    WHERE assertion_state = 'asserted' AND valid_to IS NULL AND value_boolean IS NOT NULL;
CREATE INDEX assertions_current_entity_value ON assertions(predicate_id, value_entity_id, subject_entity_id)
    WHERE assertion_state = 'asserted' AND valid_to IS NULL AND value_entity_id IS NOT NULL;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0007_value_indexes', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
