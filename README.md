# Toolframe Audit

Audit stores bounded, durable event history from trusted Toolframe producers. It
provides a filtered Dashboard page, a read-only MCP tool, JSONL page exports and
explicit retention management. A reusable transactional outbox helper keeps a
producer's business mutation and pending audit event in the same SQL transaction.

## Quick start

Python 3.14 or later, Toolframe SDK 0.4.0 and a running Toolframe application with
a SQL storage provider are required. Install from PyPI:

```sh
toolframe --home ./my-app plugin add pypi:toolframe-audit==0.2.0 --enable
toolframe --home ./my-app plugin status audit --check
```

An application with Dashboard, MCP and llms providers publishes the Audit page,
`audit__list_events` tool and usage document. Grant `audit.read` for read access and
the existing Manage plugins permission (`system.plugins.manage`) only to retention
operators. No client ingestion endpoint exists;
events arrive from a plugin declaring the `audit.events` dependency. Public
access policy still applies: configure authentication before exposing audit data.

For a complete local producer walkthrough, see
[the independently built example](examples/outbox-producer/README.md).
[Producer integration](docs/producer-integration.md) describes the reusable
helper, transaction boundary and replay behavior.

## Support limits

Version 0.1.0 uses only public SDK service and storage contracts. SQLite is tested
with real managed stores and Core worker processes. PostgreSQL migration SQL is
provided, but PostgreSQL runtime behavior requires separate verification.
Producers are trusted Python plugins: this is traceable operational history,
not tamper-proof storage or cryptographic proof of historical actor identity.

Events have a 16 KiB metadata limit and a 20 KiB total payload limit. The default
capacity is 100,000 retained events; `max_events` accepts 1–1,000,000. Full stores
reject new events while pending outbox items remain for retry. Reads and exports
contain at most 100 rows per page. Retention previews and explicit prune calls
handle at most 1,000 rows per batch; no automatic purge runs. Data survives plugin
reload, disable and application restart. Pruning permanently removes selected
history and advances a cutoff that rejects late older events.

See [architecture and trust](docs/architecture.md),
[usage](src/toolframe_audit/docs/usage.md) and
[verification](docs/verification.md) for contracts and reproducible checks.

For release validation and Trusted Publisher setup, see [releasing](docs/releasing.md).

For development hooks and contribution rules, see [Contributing](CONTRIBUTING.md).
