-- kind: additive
-- Append-only memory: nothing is deleted and stored values are never edited.
-- Corrections keep history: a fact is closed (valid_to), superseded or
-- retracted; a new name becomes an alias; an empty description may be filled.
-- These triggers guard against accidental loss by scripts or ad hoc SQL; they
-- are not a security boundary against someone who drops them on purpose.
-- A future migration that must transform data drops and recreates them.

BEGIN IMMEDIATE;

CREATE TRIGGER entities_append_only_delete
BEFORE DELETE ON entities
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: entities rows are never deleted');
END;

CREATE TRIGGER predicates_append_only_delete
BEFORE DELETE ON predicates
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: predicates rows are never deleted');
END;

CREATE TRIGGER assertions_append_only_delete
BEFORE DELETE ON assertions
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: assertions rows are never deleted');
END;

CREATE TRIGGER entity_names_append_only_delete
BEFORE DELETE ON entity_names
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: entity_names rows are never deleted');
END;

CREATE TRIGGER evidence_append_only_delete
BEFORE DELETE ON evidence
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: evidence rows are never deleted');
END;

CREATE TRIGGER assertion_evidence_append_only_delete
BEFORE DELETE ON assertion_evidence
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: assertion_evidence rows are never deleted');
END;

CREATE TRIGGER events_append_only_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: events rows are never deleted');
END;

CREATE TRIGGER event_participants_append_only_delete
BEFORE DELETE ON event_participants
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: event_participants rows are never deleted');
END;

CREATE TRIGGER event_evidence_append_only_delete
BEFORE DELETE ON event_evidence
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: event_evidence rows are never deleted');
END;

CREATE TRIGGER record_links_append_only_delete
BEFORE DELETE ON record_links
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: record_links rows are never deleted');
END;

CREATE TRIGGER record_attributes_append_only_delete
BEFORE DELETE ON record_attributes
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: record_attributes rows are never deleted');
END;

CREATE TRIGGER observations_append_only_delete
BEFORE DELETE ON observations
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: observations rows are never deleted');
END;

CREATE TRIGGER contexts_append_only_delete
BEFORE DELETE ON contexts
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: contexts rows are never deleted');
END;

CREATE TRIGGER context_members_append_only_delete
BEFORE DELETE ON context_members
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: context_members rows are never deleted');
END;

CREATE TRIGGER entity_names_append_only_update
BEFORE UPDATE ON entity_names
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: entity_names rows are never edited');
END;

CREATE TRIGGER evidence_append_only_update
BEFORE UPDATE ON evidence
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: evidence rows are never edited');
END;

CREATE TRIGGER assertion_evidence_append_only_update
BEFORE UPDATE ON assertion_evidence
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: assertion_evidence rows are never edited');
END;

CREATE TRIGGER events_append_only_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: events rows are never edited');
END;

CREATE TRIGGER event_participants_append_only_update
BEFORE UPDATE ON event_participants
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: event_participants rows are never edited');
END;

CREATE TRIGGER event_evidence_append_only_update
BEFORE UPDATE ON event_evidence
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: event_evidence rows are never edited');
END;

CREATE TRIGGER record_links_append_only_update
BEFORE UPDATE ON record_links
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: record_links rows are never edited');
END;

CREATE TRIGGER record_attributes_append_only_update
BEFORE UPDATE ON record_attributes
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: record_attributes rows are never edited');
END;

CREATE TRIGGER observations_append_only_update
BEFORE UPDATE ON observations
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: observations rows are never edited');
END;

CREATE TRIGGER contexts_append_only_update
BEFORE UPDATE ON contexts
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: contexts rows are never edited');
END;

CREATE TRIGGER context_members_append_only_update
BEFORE UPDATE ON context_members
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: context_members rows are never edited');
END;

-- A fact may only be closed once and move from asserted to superseded or retracted.
CREATE TRIGGER assertions_append_only_update
BEFORE UPDATE ON assertions
WHEN NOT (
         NEW.assertion_id IS OLD.assertion_id
     AND NEW.subject_entity_id IS OLD.subject_entity_id
     AND NEW.predicate_id IS OLD.predicate_id
     AND NEW.value_type IS OLD.value_type
     AND NEW.value_text IS OLD.value_text
     AND NEW.value_integer IS OLD.value_integer
     AND NEW.value_real IS OLD.value_real
     AND NEW.value_boolean IS OLD.value_boolean
     AND NEW.value_datetime IS OLD.value_datetime
     AND NEW.value_json IS OLD.value_json
     AND NEW.value_entity_id IS OLD.value_entity_id
     AND NEW.valid_from IS OLD.valid_from
     AND NEW.confidence IS OLD.confidence
     AND NEW.recorded_at IS OLD.recorded_at
     AND NEW.recorded_by_entity_id IS OLD.recorded_by_entity_id
     AND (NEW.valid_to IS OLD.valid_to OR OLD.valid_to IS NULL)
     AND (NEW.assertion_state IS OLD.assertion_state
          OR (OLD.assertion_state = 'asserted' AND NEW.assertion_state IN ('superseded', 'retracted')))
)
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: a fact is closed, superseded or retracted, never edited');
END;

-- An entity keeps its identity; only an empty description or retirement may be filled in.
CREATE TRIGGER entities_append_only_update
BEFORE UPDATE ON entities
WHEN NOT (
         NEW.entity_id IS OLD.entity_id
     AND NEW.entity_type IS OLD.entity_type
     AND NEW.canonical_name IS OLD.canonical_name
     AND NEW.external_key IS OLD.external_key
     AND NEW.created_at IS OLD.created_at
     AND NEW.recorded_at IS OLD.recorded_at
     AND (NEW.description IS OLD.description OR OLD.description IS NULL)
     AND (NEW.retired_at IS OLD.retired_at OR OLD.retired_at IS NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: an entity keeps its name and key; use aliases and facts for changes');
END;

-- The vocabulary is stable; only an empty description may be filled in.
CREATE TRIGGER predicates_append_only_update
BEFORE UPDATE ON predicates
WHEN NOT (
         NEW.predicate_id IS OLD.predicate_id
     AND NEW.label IS OLD.label
     AND NEW.value_type IS OLD.value_type
     AND NEW.cardinality IS OLD.cardinality
     AND NEW.is_temporal IS OLD.is_temporal
     AND NEW.created_at IS OLD.created_at
     AND NEW.unit IS OLD.unit
     AND (NEW.description IS OLD.description OR OLD.description IS NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'append-only memory: predicates are not redefined; declare a new one');
END;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0006_append_only', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
