"""Reusable same-owner transactional enqueue and finite at-least-once delivery."""

from __future__ import annotations

import asyncio
import json
import time
from importlib.resources import files

from toolframe_sdk import Migration, PluginError, StorageClient

from .events import admitted, bounded_integer, normalize_event, now


def outbox_migration(*, revision="audit_outbox_v1", parent=None) -> Migration:
    """Append this migration to the producer's own linear store history."""
    sql = files("toolframe_audit").joinpath("migrations/outbox.sql").read_text()
    return Migration(
        revision=revision, parent=parent, sql=sql, dialects={"postgresql": sql}
    )


async def enqueue(
    context,
    transaction: StorageClient,
    *,
    event_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict | None = None,
    occurred_at: str | None = None,
    max_pending: int = 1000,
) -> dict:
    """Enqueue inside the SAME live StorageClient transaction as the business write.

    Identity and actor are captured from the admitted context. This function never
    opens a second transaction and cannot override the producer owner or actor.
    """
    if (
        not isinstance(transaction, StorageClient)
        or not transaction.token
        or transaction.context is not context
    ):
        raise PluginError(
            "audit_outbox_transaction", "Pass the producer's live transaction client."
        )
    info = context.call_info
    if (
        "owner" not in info
        and "application_id" not in info
        and "principal" not in info
        and isinstance(info.get("root_id"), str)
        and info["root_id"]
        and info.get("chain") == [context.plugin_id]
    ):
        # The SDK's newly admitted task root has no visitor principal. Source
        # fencing still happens at Core.invoke on the later collector call.
        original_actor = {"kind": "background"}
    else:
        _, original_actor = admitted(context)
    maximum = bounded_integer(max_pending, "max_pending", 100_000)
    event, payload, content_hash = normalize_event(
        {
            "event_id": event_id,
            "occurred_at": occurred_at if occurred_at is not None else now(),
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata": metadata if metadata is not None else {},
            "source_attested_actor": original_actor,
        }
    )
    result = await transaction.execute_returning(
        "INSERT INTO audit_outbox(event_id, payload, content_hash, enqueued_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(event_id) DO NOTHING RETURNING event_id",
        (event_id, payload, content_hash, now()),
    )
    if not result.get("rows"):
        rows = await transaction.query(
            "SELECT content_hash FROM audit_outbox WHERE event_id = ?", (event_id,)
        )
        if len(rows) != 1 or rows[0]["content_hash"] != content_hash:
            raise PluginError(
                "audit_event_conflict",
                "The pending event ID already has different content.",
            )
        return event
    capacity = await transaction.execute_returning(
        "UPDATE audit_outbox_capacity SET pending_count = pending_count + 1 "
        "WHERE singleton = 1 AND pending_count < ? RETURNING pending_count",
        (maximum,),
    )
    if not capacity.get("rows"):
        raise PluginError(
            "audit_outbox_capacity",
            "Outbox capacity is reached; the business transaction must roll back.",
        )
    return event


async def drain(
    context, *, alias="audit", storage_alias="storage", limit=25, budget_seconds=10
) -> dict:
    """Deliver one short batch; acknowledge only accepted/deduplicated events.

    Failures propagate without logging payloads. Successfully acknowledged prior
    events remain settled; retry the remaining outbox in another admitted root.
    """
    # context.task roots carry only root_id/deadline/chain. Core attaches complete
    # source identity when this producer crosses the collector service boundary.
    if (
        not isinstance(context.call_info.get("root_id"), str)
        or not context.call_info["root_id"]
    ):
        raise PluginError("audit_untrusted_context", "Drain requires an admitted root.")
    limit = bounded_integer(limit, "limit", 100)
    budget_seconds = bounded_integer(budget_seconds, "budget_seconds", 30)
    storage = context.storage(storage_alias)
    deadline = time.monotonic() + budget_seconds
    delivered = acknowledged = 0
    async with asyncio.timeout(budget_seconds):
        rows = await storage.query(
            "SELECT event_id FROM audit_outbox ORDER BY enqueued_at, event_id LIMIT ?",
            (limit,),
        )
        for row in rows:
            pending = await storage.query(
                "SELECT event_id, payload FROM audit_outbox WHERE event_id = ?",
                (row["event_id"],),
            )
            if not pending:  # Another finite drain may have acknowledged it.
                continue
            row = pending[0]
            response = await context.call(
                alias,
                "ingest",
                {"event": json.loads(row["payload"])},
                timeout=max(0.01, deadline - time.monotonic()),
            )
            if (
                not isinstance(response, dict)
                or response.get("status") not in {"accepted", "duplicate"}
                or response.get("event_id") != row["event_id"]
                or response.get("source_owner") != context.plugin_id
            ):
                raise PluginError(
                    "audit_outbox_delivery",
                    "Collector did not acknowledge this producer event.",
                )
            delivered += 1
            async with storage.transaction() as transaction:
                deleted = await transaction.execute_returning(
                    "DELETE FROM audit_outbox WHERE event_id = ? AND payload = ? RETURNING event_id",
                    (row["event_id"], row["payload"]),
                )
                if deleted.get("rows"):
                    await transaction.execute(
                        "UPDATE audit_outbox_capacity SET pending_count = pending_count - 1 WHERE singleton = 1"
                    )
                    acknowledged += 1
    return {"delivered": delivered, "acknowledged": acknowledged, "limit": limit}
