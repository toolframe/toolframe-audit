# Audit outbox producer example

This independently packaged Toolframe plugin demonstrates a business write and
its Audit event entering one owner SQL transaction. It creates small records,
stores a pending outbox row beside each record, and delivers a finite batch to
the `audit.events@1` service. The example has no dashboard page and is intended
for local integration checks and as a producer reference.

## Quick start

Build the Audit wheel into a local wheelhouse, then build and install this
example from this directory. When working from the Toolframe workspace, the
commented command also builds the SDK wheel locally if it is not already there:

```sh
cd ../..
uv build --no-sources --out-dir build/wheels
# (cd ../toolframe-sdk && uv build --no-sources --out-dir ../toolframe-audit/build/wheels)
cd examples/outbox-producer
uv build --no-sources --out-dir dist
uv venv .venv
uv pip install --python .venv/bin/python \
  --find-links ../../build/wheels --find-links dist \
  toolframe-audit-example==0.1.0
```

Install the resulting wheel into a Toolframe application with a SQL storage
provider and Audit enabled. For the local layout below, the application home is
`examples/outbox-producer/my-app`. Before starting it, add the local wheelhouse
to its existing `[packages]` configuration without replacing other settings:

```toml
[packages]
find_links = ["../../../build/wheels"]
```

This lets the example's isolated environment resolve `toolframe-audit==0.2.1`.
Then, while that application is running:

```sh
toolframe --home ./my-app plugin add ./dist/toolframe_audit_example-0.1.0-py3-none-any.whl --enable
toolframe --home ./my-app plugin status audit-example --check
```

Grant `audit-example.write` to create records. A call to
`audit-example.records/create_record` stores the business row and pending event
together. A trusted producer root can invoke the internal `drain` method for a
finite synchronous batch; callers with `audit-example.write` can invoke
`drain_background`, which schedules exactly one finite drain and returns
`{"status": "queued"}`.

## What the example demonstrates

The store declares `v1` for `business_records` and chains the reusable Audit
outbox migration as `v2`. `create_record` calls `enqueue` with the live
transaction client, and the helper captures the actor from the admitted
Toolframe context. The event contains the record identity as a bounded,
deterministic event ID and the action only; the label is deliberately omitted
from metadata. A drain acknowledges an item only after Audit accepts or
deduplicates it, so replay after a lost acknowledgment is safe for the same
event ID.

Audit records preserve both the source-attested actor captured during the
original write and the actor admitted for the later delivery. A replay does not
reconstruct the original visitor principal, and neither actor is a cryptographic
proof of historical behavior. Producers must redact secrets and keep event
metadata minimal. This example also assumes a configured SQL provider and an
installed `toolframe-audit==0.2.1`; PostgreSQL migration SQL is supplied, while
the runnable walkthrough uses the real SQLite provider.
