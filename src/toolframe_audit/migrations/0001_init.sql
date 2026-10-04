CREATE TABLE audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL,
    source_owner TEXT NOT NULL,
    event_id TEXT NOT NULL,
    source_generation TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    delivery_actor TEXT NOT NULL,
    UNIQUE(application_id, source_owner, event_id)
);
CREATE INDEX audit_source_sequence ON audit_events(source_owner, sequence);
CREATE INDEX audit_action_sequence ON audit_events(action, sequence);
CREATE INDEX audit_entity_sequence ON audit_events(entity_type, entity_id, sequence);
CREATE INDEX audit_occurred_sequence ON audit_events(occurred_at, sequence);
CREATE TABLE audit_capacity (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), event_count BIGINT NOT NULL, retention_cutoff TEXT);
INSERT INTO audit_capacity(singleton, event_count) VALUES (1, 0);
