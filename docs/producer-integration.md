# Producer integration

The Audit package includes a small transactional outbox helper for business
plugins. It lets a producer commit its business mutation and the pending audit
event in one SQL transaction, then deliver events to Audit with finite,
at-least-once retries.

## Add the producer migration

Append the migration returned by `outbox_migration()` to the producer's own
linear SQL store schema. The default revision is `audit_outbox_v1`; pass a
different revision or `parent` when that is required by the producer's
migration history.

```python
from toolframe_audit import outbox_migration

# Append this value after the producer's existing store migration.
audit_outbox = outbox_migration(parent="producer_v1")
```

The migration creates the producer-local pending outbox and its bounded
capacity counter. It does not create a second database or claim a cross-store
transaction.

## Enqueue with the business write

Use the producer's live `SqlClient` transaction for both operations. The
helper must receive the same transaction context; it never opens a second
transaction.

Pass the exact client used for the business mutation, including any custom
storage alias. The helper rejects a client from another runtime context. If the
producer uses a nondefault alias, pass that same alias as `sql_alias` when
draining; Core still fences each store by producer ownership.

```python
from toolframe_audit import enqueue

async with context.sql().transaction() as tx:
    await tx.execute(
        "UPDATE invoices SET status = ? WHERE invoice_id = ?",
        ("paid", invoice_id),
    )
    await enqueue(
        context,
        tx,
        event_id=f"invoice:{invoice_id}:paid:v1",
        action="invoice.paid",
        entity_type="invoice",
        entity_id=invoice_id,
        metadata={"payment_method": "card"},
    )
```

`event_id` is the producer's stable logical identifier. `action`,
`entity_type`, and `entity_id` are required. `metadata` defaults to `{}` and
`occurred_at` defaults to the current UTC time; both are validated against the
event limits. `max_pending` defaults to 1,000 and accepts 1 through 100,000.
Reuse the same timestamp and payload when enqueueing an existing logical ID;
changing `occurred_at` is conflicting content, even if the action is unchanged.
If pending capacity is full, enqueue raises and the surrounding business
transaction rolls back, leaving no business state without its audit intent.

The collector takes the source owner from the admitted producer context. The
helper snapshots the original actor and accepts no source or actor overrides. An
admitted background task without a visitor principal records `background`. The captured
`source_attested_actor` is an original snapshot vouched for by the producer;
it is not cryptographic proof supplied by Core. Redact passwords, tokens,
credentials, cookies, session material, and full request or response bodies
before calling the helper. The key-name check in Audit is only a basic defense,
not a complete redaction system.

## Drain a finite batch

Run the drain from an admitted producer root, such as a bounded maintenance or
worker invocation:

```python
from toolframe_audit import drain

result = await drain(context, limit=25, budget_seconds=10)
```

`limit` defaults to 25 and is capped at 100. `budget_seconds` defaults to 10
and is capped at 30. The drain reads pending rows in enqueue order, calls the
internal `audit.events/ingest` service, and accepts only an `accepted` or
`duplicate` response for the same event ID and producer source. It then deletes
that exact pending payload in a new producer-store transaction and decrements
the pending counter. A successful delivery and acknowledgment are counted in
the returned `delivered` and `acknowledged` values.

The collector's delivery identity comes from the admitted context at the
Audit boundary. A background drain may therefore have a `background` delivery
actor when no visitor principal is present. The original actor remains the
source-attested snapshot captured during enqueue; replay does not replace it
with the actor that happens to run the drain.

There is no cross-store transaction. If the process stops after Audit inserts
the event but before the producer deletes its pending row, the next drain
replays the event. The `(application_id, source_owner, event_id)` uniqueness
key and canonical content hash make that replay a safe duplicate. A conflicting
payload for the same logical ID is rejected and requires producer attention.

## Operational limits

The drain is deliberately short and finite. Retry remaining rows with another
admitted root rather than turning one invocation into an unbounded worker. Audit
capacity rejects new events while full; pending rows stay at the producer for
retry. If an operator prunes past a pending event's `occurred_at`, that item
may be rejected by the retention cutoff and needs explicit operational
handling.

The independently built fixture under
[`examples/outbox-producer`](../examples/outbox-producer/README.md) demonstrates
this pattern without becoming part of the Audit wheel.
