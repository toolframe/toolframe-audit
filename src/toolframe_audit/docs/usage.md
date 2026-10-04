# Audit event history

The Audit plugin keeps a durable, read-only event history for an application.
Use the `audit.events@1` service or the read-only `audit__list_events` MCP tool
to inspect events; both require the `audit.read` scope. The plugin does not
expose public caller fields for ingestion. Producers enqueue events through the
package's transactional outbox helper while committing their business mutation,
then use the bounded drain to deliver them to Audit.

## Reading events

Call `list_events` with an optional filter object:

```python
service = context.services.require("audit.events", version=1)
page = service.call(
    "list_events",
    {
        "limit": 50,
        "source_owner": "billing",
        "action": "invoice.paid",
        "entity_type": "invoice",
        "entity_id": "inv_123",
        "since": "2026-01-01T00:00:00Z",
        "until": "2026-01-31T23:59:59Z",
    },
)
```

`limit` accepts 1–100 and defaults to 50. The Dashboard uses 25, 50, or 100
rows per page. `source_owner`, `action`, `entity_type`, and `entity_id` are
exact filters. `since` and `until` are UTC ISO 8601 timestamps. Omit empty
filters. Results are ordered newest first and contain `events`, the returned
row `count`, the effective `limit`, and `next_before`. The service may return
fewer than `limit` rows when its event-byte budget is reached. Pass a non-null
`next_before` back as `before` to continue toward older rows; stop when it is
`null`.

Each event includes its sequence, event ID, application and source identity,
occurred and received timestamps, action, entity identity, bounded metadata,
`source_attested_actor`, and `delivery_actor`. The source-attested actor is
what the producer attested when it created the event; it is not a cryptographic
verification of that historical actor. The delivery actor comes from the
host-admitted delivery context that accepted the event. Replay is safe for a
previously accepted event ID, while a conflicting payload for the same logical
ID is rejected.

The Dashboard's JSONL button exports only the currently displayed page (up to
100 events) and labels the file as a partial export. Continue through the
`next_before` cursor when a complete review is required.

Operators with the Manage plugins permission (`system.plugins.manage`) can use the
Dashboard Retention panel. First
preview a cutoff and batch limit. The preview records a `through_sequence`
high-water mark; the Dashboard preserves those inputs and requires explicit
confirmation before applying. Direct service callers supply the cutoff, limit
and `through_sequence` cap explicitly; the service does not store or bind preview
tokens. It never substitutes a newer sequence cap on apply. The persisted cutoff rejects later events whose
occurred timestamp is older than the cutoff, so pending producer outbox items
need attention.

## Producer and outbox integration

Use the public outbox helper exported by the package and the fixture under
`examples/outbox-producer/` as the integration reference. The business write
and enqueue must use the same owner SQL transaction. A finite drain delivers
each pending item to Audit and acknowledges it only after Audit accepts or
deduplicates it. If the process crashes between those two steps, replay uses
the event ID and does not create a second row. The helper captures the original
actor from trusted context; a replaying drain cannot turn that historical actor
into a new delivery identity, and callers cannot override the source owner.

Keep event metadata to the minimum needed for investigation. Redact tokens,
passwords, credentials, session material, and full request or response bodies
before enqueueing. Audit provides durable operational history with source and
delivery attribution; it is not a tamper-proof ledger or cryptographic proof
of the historical actor.
