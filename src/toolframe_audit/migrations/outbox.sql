CREATE TABLE audit_outbox (
    event_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    enqueued_at TEXT NOT NULL
);
CREATE INDEX audit_outbox_order ON audit_outbox(enqueued_at, event_id);
CREATE TABLE audit_outbox_capacity (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), pending_count BIGINT NOT NULL);
INSERT INTO audit_outbox_capacity(singleton, pending_count) VALUES (1, 0);
