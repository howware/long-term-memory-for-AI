-- kind: transformative
-- A tracking code is a reusable generic predicate, not a domain-specific
-- table column. Its current value must be globally unique and boarding-pass-like.

BEGIN IMMEDIATE;

CREATE UNIQUE INDEX assertions_current_tracking_code_unique
    ON assertions(value_text)
    WHERE predicate_id = 'tracking_code'
      AND assertion_state = 'asserted'
      AND valid_to IS NULL;

CREATE TRIGGER assertions_validate_tracking_code_insert
BEFORE INSERT ON assertions
FOR EACH ROW
WHEN NEW.predicate_id = 'tracking_code'
 AND (
    NEW.value_type <> 'text'
    OR length(NEW.value_text) < 5
    OR upper(NEW.value_text) <> NEW.value_text
    OR NEW.value_text GLOB '*[^A-Z0-9]*'
 )
BEGIN
    SELECT RAISE(ABORT, 'tracking_code must be at least 5 uppercase alphanumeric characters');
END;

CREATE TRIGGER assertions_validate_tracking_code_update
BEFORE UPDATE OF predicate_id, value_type, value_text ON assertions
FOR EACH ROW
WHEN NEW.predicate_id = 'tracking_code'
 AND (
    NEW.value_type <> 'text'
    OR length(NEW.value_text) < 5
    OR upper(NEW.value_text) <> NEW.value_text
    OR NEW.value_text GLOB '*[^A-Z0-9]*'
 )
BEGIN
    SELECT RAISE(ABORT, 'tracking_code must be at least 5 uppercase alphanumeric characters');
END;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0002_tracking_code_constraint', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
