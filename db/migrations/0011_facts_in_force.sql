-- kind: additive
-- "In force" is one definition everywhere: a fact counts at an instant when
-- it began at or before it, had not ended, and was never retracted. A value
-- that starts in the future is not current yet, and the value it replaces
-- stays current until then. The read views are recreated with that
-- definition (views hold no rows, so nothing stored changes) and one index
-- finds facts that end later than a given instant. Only views and an index.

BEGIN IMMEDIATE;

DROP VIEW memory_pending;
DROP VIEW memory_current_states;
DROP VIEW entity_labels;

CREATE VIEW memory_current_states AS
SELECT
    a.subject_entity_id AS entity_id,
    state.entity_id AS state_entity_id,
    state.canonical_name AS state_name,
    a.valid_from
FROM assertions AS a
JOIN entities AS state ON state.entity_id = a.value_entity_id
WHERE a.predicate_id = 'workflow_status'
  AND a.assertion_state <> 'retracted'
  AND (a.valid_from IS NULL OR julianday(a.valid_from) <= julianday('now'))
  AND (a.valid_to IS NULL OR julianday(a.valid_to) > julianday('now'));

CREATE VIEW memory_pending AS
SELECT
    pending.subject_entity_id AS entity_id,
    entity.canonical_name AS entity_name,
    person.entity_id AS pending_for_entity_id,
    person.canonical_name AS pending_for_name,
    state.state_name,
    pending.valid_from
FROM assertions AS pending
JOIN entities AS entity ON entity.entity_id = pending.subject_entity_id
JOIN entities AS person ON person.entity_id = pending.value_entity_id
LEFT JOIN memory_current_states AS state ON state.entity_id = pending.subject_entity_id
WHERE pending.predicate_id = 'pending_action_for'
  AND pending.assertion_state <> 'retracted'
  AND (pending.valid_from IS NULL OR julianday(pending.valid_from) <= julianday('now'))
  AND (pending.valid_to IS NULL OR julianday(pending.valid_to) > julianday('now'));

CREATE VIEW entity_labels AS
SELECT entity.entity_id,
       COALESCE((SELECT label.value_text FROM assertions AS label
                  WHERE label.subject_entity_id = entity.entity_id AND label.predicate_id = 'name'
                    AND label.assertion_state <> 'retracted'
                    AND (label.valid_from IS NULL OR julianday(label.valid_from) <= julianday('now'))
                    AND (label.valid_to IS NULL OR julianday(label.valid_to) > julianday('now'))
                  ORDER BY julianday(label.valid_from) DESC LIMIT 1),
                entity.canonical_name) AS name,
       entity.canonical_name AS created_as
  FROM entities AS entity;

-- Facts closed at a later instant (a value replaced from a future date) are
-- found by predicate without scanning every closed fact.
CREATE INDEX assertions_open_until
    ON assertions(predicate_id, valid_to, subject_entity_id)
    WHERE valid_to IS NOT NULL AND assertion_state <> 'retracted';

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0011_facts_in_force', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
