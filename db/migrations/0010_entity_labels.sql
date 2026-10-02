-- kind: additive
-- The name an entity goes by. A rename is a current 'name' fact (temporal,
-- with evidence and actor); canonical_name keeps the name the entity was
-- created with, so the history of names is never lost.

BEGIN IMMEDIATE;

CREATE VIEW entity_labels AS
SELECT entity.entity_id,
       COALESCE((SELECT label.value_text FROM assertions AS label
                  WHERE label.subject_entity_id = entity.entity_id AND label.predicate_id = 'name'
                    AND label.assertion_state = 'asserted' AND label.valid_to IS NULL
                  ORDER BY julianday(label.valid_from) DESC LIMIT 1),
                entity.canonical_name) AS name,
       entity.canonical_name AS created_as
  FROM entities AS entity;

INSERT INTO schema_migrations (migration_id, applied_at)
VALUES ('0010_entity_labels', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
