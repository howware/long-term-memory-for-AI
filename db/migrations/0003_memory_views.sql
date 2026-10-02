-- kind: additive
-- Standard read models for the local memory helper. They remain domain-neutral
-- and derive their values from generic events, assertions, and participants.

BEGIN IMMEDIATE;

CREATE VIEW memory_current_states AS
SELECT
    a.subject_entity_id AS entity_id,
    state.entity_id AS state_entity_id,
    state.canonical_name AS state_name,
    a.valid_from
FROM assertions AS a
JOIN entities AS state ON state.entity_id = a.value_entity_id
WHERE a.predicate_id = 'workflow_status'
  AND a.assertion_state = 'asserted'
  AND a.valid_to IS NULL;

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
  AND pending.assertion_state = 'asserted'
  AND pending.valid_to IS NULL;

CREATE VIEW memory_timeline AS
SELECT
    event.event_id,
    event.event_type,
    event.summary,
    event.occurred_at,
    event.ended_at,
    performer.entity_id AS performed_by_entity_id,
    performer.canonical_name AS performed_by_name
FROM events AS event
JOIN event_participants AS participant
  ON participant.event_id = event.event_id
 AND participant.role = 'performed_by'
JOIN entities AS performer ON performer.entity_id = participant.entity_id;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0003_memory_views', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
