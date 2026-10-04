# Audit architecture and trust model

Toolframe Audit is an ordinary service plugin that stores bounded event history
in the application's SQL storage. Version 0.1.0 requires Python 3.14 or later
and `toolframe-sdk==0.3.0`. It requires the public
`toolframe.storage.sql` service and uses only public SDK and service contracts;
it does not import Core internals or extend the provider-kind registry.

## Service boundary

The plugin exposes `audit.events@1`:

- `ingest` is an internal method available to declared producer dependencies.
  There is no public client ingestion endpoint and no caller-supplied source
  identity field.
- `list_events` is a read-only business method requiring `audit.read`.
- `prune_events` is a system method requiring the existing Manage plugins operator
  permission (`system.plugins.manage`). It previews or applies one explicitly
  bounded retention batch. Public business access does not grant this permission.

The plugin also publishes a Dashboard page, the read-only MCP tool
`audit__list_events`, and an `audit.read`-protected usage document. The
scoped service route is
`/_toolframe/v1/services/audit/audit.events/list_events`; it requires
`audit.read`. The Dashboard shows exact filters, empty and error states,
bounded pages, keyset navigation, and a JSONL download of the displayed page.
A page export is limited to 100 events; callers that need a larger review must
continue with `next_before`.

## Event and storage contract

An event has these fields: `event_id`, UTC `occurred_at`, `action`,
`entity_type`, `entity_id`, `metadata`, and `source_attested_actor`. Text
fields are bounded; metadata is a JSON object of at most 16 KiB, 1,024 JSON
values including container objects, arrays and the root, and eight nesting
levels. The complete canonical event payload is at most 20
KiB. A small set of obvious credential and secret key names is rejected as a
basic defense. Producers must still minimize and redact metadata at the source.

Each stored row receives a monotonic `sequence`, the application and source
identity, the source generation, `received_at`, the original payload, and a
`delivery_actor` snapshot. Indexes support source, action, entity, and
occurred-time queries. Reads accept `limit` 1–100 and can be filtered by
source owner, action, entity type, entity ID, and UTC `since`/`until` times.
The response is additionally bounded to 512,000 encoded bytes, so a page can
contain fewer rows than its requested limit; continue with `next_before`.
The uniqueness key is
`(application_id, source_owner, event_id)`. A retry with the same key and the
same canonical content returns `duplicate` and does not consume capacity. The
same logical ID with different content is rejected. Data and deduplication
state are stored in the managed SQL store and survive reload, disable, and
restart. SQLite and PostgreSQL migration SQL are packaged; PostgreSQL runtime
support requires separate verification.

The configured `max_events` capacity defaults to 100,000 and accepts values
from 1 through 1,000,000. New events are rejected when retained capacity is
full, while producer outbox records remain available for retry. Accepted
duplicates do not consume capacity.

Capacity bounds retained event rows and payloads, rather than the complete
database file size. Storage-provider transaction receipts, indexes and database
allocation have their own overhead. Explicit pruning does not promise that a
SQLite file immediately shrinks; plan disk capacity and provider maintenance
separately.

## Identity and trust

At the ingestion boundary, Audit takes `owner`, `owner_generation`,
`application_id`, `root_id`, and `principal` from the host-admitted call
context. Missing or malformed owner/application context is rejected, and a
producer cannot select another source by putting an override in its event.
When Core invokes Audit through a producer dependency, a missing visitor
principal is represented as the `background` delivery actor; it is not a
reason to reject an otherwise admitted service call.

The reusable producer helper captures the admitted principal at enqueue time
as `source_attested_actor`. Audit records the host-admitted delivery principal as
`delivery_actor` on first insertion; retries do not rewrite it. The historical
source actor is vouched for by the producer that supplied the snapshot. It is
not cryptographically attested by Core, and a delivery actor does not prove
who originally caused the business action. Audit is durable, traceable
operational history, not a tamper-proof ledger.

Audit is intended for trusted Python plugins. A plugin with permission to run
arbitrary code can fabricate an otherwise valid event or alter its own local
outbox before delivery. Never put credentials, tokens, cookies, session
material, or full request and response bodies in event metadata or logs.

## Retention

Retention is explicit and scoped. An operator with `system.plugins.manage` first asks
for a preview with a UTC cutoff and a batch limit of at most 1,000 rows. The
preview returns a `through_sequence` high-water mark. Applying a prune requires
an explicit sequence cap and `preview=False`; there is no automatic purge. The
Dashboard preserves the preview's cutoff, limit and sequence cap and invalidates
the preview when inputs change. The service does not save or bind preview intent:
API operators explicitly choose the apply inputs, and eligible rows can change
under concurrent pruning. An apply never fills in a newer cap automatically. The
persisted cutoff advances monotonically, and the selected rows are deleted in
a bounded transaction.

Pruning removes the corresponding event and deduplication history. A later new
or replayed event older than the retained cutoff is rejected with
`audit_retention`; an already-existing duplicate can still acknowledge a
previously accepted outbox item. Pending producer items that fall behind the
cutoff require operator attention.

Keyset traversal is not a consistent snapshot. Inserts or explicit pruning
between requests can change which rows appear on later pages; callers that
need a point-in-time report must coordinate that operationally.

For the reusable same-store delivery pattern, see
[Producer integration](producer-integration.md). For the public read and
retention call shapes, see the packaged [usage guide](../src/toolframe_audit/docs/usage.md).
