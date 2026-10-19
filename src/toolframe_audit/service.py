"""Source-fenced collector and bounded event queries using only public SDK APIs."""

from __future__ import annotations

import json

from toolframe_sdk import Plugin, PluginError

from .events import (
    MAX_PAGE,
    MAX_SEQUENCE,
    admitted,
    bounded_integer,
    canonical,
    invalid,
    normalize_event,
    now,
    text_field,
    timestamp,
)

SERVICE_NAME = "audit.events"


class Audit(Plugin):
    async def ingest(self, event: dict) -> dict:
        info, delivery_actor = admitted(self.context, producer=True)
        normalized, payload, content_hash = normalize_event(event)
        source, application = info["owner"], info["application_id"]
        maximum = bounded_integer(
            self.context.config.get("max_events", 100_000), "max_events", 1_000_000
        )
        async with self.context.sql().transaction() as transaction:
            result = await transaction.execute_returning(
                "INSERT INTO audit_events (application_id, source_owner, event_id, "
                "source_generation, occurred_at, received_at, action, entity_type, "
                "entity_id, payload, content_hash, delivery_actor) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(application_id, source_owner, event_id) DO NOTHING RETURNING sequence",
                (
                    application,
                    source,
                    normalized["event_id"],
                    info["owner_generation"],
                    normalized["occurred_at"],
                    now(),
                    normalized["action"],
                    normalized["entity_type"],
                    normalized["entity_id"],
                    payload,
                    content_hash,
                    canonical(delivery_actor),
                ),
            )
            rows = result.get("rows", [])
            status = "accepted"
            if not rows:
                rows = await transaction.query(
                    "SELECT sequence, content_hash FROM audit_events "
                    "WHERE application_id = ? AND source_owner = ? AND event_id = ?",
                    (application, source, normalized["event_id"]),
                )
                if len(rows) != 1 or rows[0]["content_hash"] != content_hash:
                    raise PluginError(
                        "audit_event_conflict",
                        "The source event ID already has different content.",
                    )
                status = "duplicate"
            else:
                capacity = await transaction.execute_returning(
                    "UPDATE audit_capacity SET event_count = event_count + 1 "
                    "WHERE singleton = 1 AND event_count < ? "
                    "AND (retention_cutoff IS NULL OR retention_cutoff <= ?) RETURNING event_count",
                    (maximum, normalized["occurred_at"]),
                )
                if not capacity.get("rows"):
                    policy = await transaction.query(
                        "SELECT retention_cutoff FROM audit_capacity WHERE singleton = 1"
                    )
                    if (
                        policy
                        and policy[0]["retention_cutoff"]
                        and normalized["occurred_at"] < policy[0]["retention_cutoff"]
                    ):
                        raise PluginError(
                            "audit_retention",
                            "Event predates the retained audit cutoff; source requires attention.",
                        )
                    raise PluginError(
                        "audit_capacity",
                        "Audit event capacity is reached; new events remain at the source.",
                    )
            return {
                "status": status,
                "event_id": normalized["event_id"],
                "source_owner": source,
                "sequence": rows[0]["sequence"],
            }

    async def list_events(
        self,
        *,
        limit=50,
        before=None,
        source_owner=None,
        action=None,
        entity_type=None,
        entity_id=None,
        since=None,
        until=None,
    ) -> dict:
        limit = bounded_integer(limit, "limit", MAX_PAGE)
        clauses, parameters = [], []
        if before is not None:
            clauses.append("sequence < ?")
            parameters.append(bounded_integer(before, "before", MAX_SEQUENCE))
        for key, value, maximum in (
            ("source_owner", source_owner, 64),
            ("action", action, 128),
            ("entity_type", entity_type, 128),
            ("entity_id", entity_id, 256),
        ):
            if value is not None:
                clauses.append(f"{key} = ?")
                parameters.append(text_field(value, key, maximum))
        start = timestamp(since, "since") if since is not None else None
        end = timestamp(until, "until") if until is not None else None
        if start is not None and end is not None and start > end:
            raise invalid("since must not be after until.")
        for value, comparison in ((start, ">="), (end, "<=")):
            if value is not None:
                clauses.append(f"occurred_at {comparison} ?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        storage = self.context.sql()
        identities = await storage.query(
            "SELECT sequence FROM audit_events"
            + where
            + " ORDER BY sequence DESC LIMIT ?",
            (*parameters, limit + 1),
        )
        has_more = len(identities) > limit
        events = []
        output_bytes = 0
        # Both the storage response and final read must fit SDK's 1 MiB RPC
        # frame even for large/non-ASCII metadata. Small fetches bound each hop.
        for offset in range(0, min(len(identities), limit), 8):
            ids = [
                item["sequence"] for item in identities[offset : min(offset + 8, limit)]
            ]
            placeholders = ",".join("?" for _ in ids)
            rows = await storage.query(
                "SELECT sequence, application_id, source_owner, source_generation, received_at, "
                f"payload, delivery_actor FROM audit_events WHERE sequence IN ({placeholders}) ORDER BY sequence DESC",
                ids,
            )
            for row in rows:
                record = dict(row)
                record.update(json.loads(record.pop("payload")))
                record["delivery_actor"] = json.loads(record["delivery_actor"])
                size = len(
                    json.dumps(record, separators=(",", ":"), allow_nan=False).encode()
                )
                if events and output_bytes + size > 512_000:
                    has_more = True
                    break
                events.append(record)
                output_bytes += size
            else:
                continue
            break
        return {
            "events": events,
            "count": len(events),
            "limit": limit,
            "next_before": events[-1]["sequence"] if has_more and events else None,
        }

    async def prune_events(
        self, *, cutoff, limit=100, preview=True, through_sequence=None
    ):
        cutoff = timestamp(cutoff, "cutoff")
        limit = bounded_integer(limit, "limit", 1000)
        if not isinstance(preview, bool):
            raise invalid("preview must be a boolean.")
        if through_sequence is not None and (
            isinstance(through_sequence, bool)
            or not isinstance(through_sequence, int)
            or not 0 <= through_sequence <= MAX_SEQUENCE
        ):
            raise invalid(
                f"through_sequence must be an integer between 0 and {MAX_SEQUENCE}."
            )
        if not preview and through_sequence is None:
            raise invalid(
                "Applying retention requires through_sequence from a preview."
            )
        async with self.context.sql().transaction() as transaction:
            if through_sequence is None:
                newest = await transaction.query(
                    "SELECT MAX(sequence) AS latest FROM audit_events"
                )
                through_sequence = newest[0]["latest"] or 0
            rows = await transaction.query(
                "SELECT sequence FROM audit_events WHERE occurred_at < ? AND sequence <= ? "
                "ORDER BY sequence LIMIT ?",
                (cutoff, through_sequence, limit + 1),
            )
            selected = [row["sequence"] for row in rows[:limit]]
            pruned = 0
            if not preview:
                # One monotonic cutoff also fences concurrent/late inserts. The
                # shared capacity row serializes this policy with ingestion.
                await transaction.execute(
                    "UPDATE audit_capacity SET retention_cutoff = ? WHERE singleton = 1 "
                    "AND (retention_cutoff IS NULL OR retention_cutoff < ?)",
                    (cutoff, cutoff),
                )
                if selected:
                    placeholders = ",".join("?" for _ in selected)
                    deleted = await transaction.execute_returning(
                        f"DELETE FROM audit_events WHERE sequence IN ({placeholders}) RETURNING sequence",
                        selected,
                    )
                    pruned = len(deleted.get("rows", []))
                    await transaction.execute(
                        "UPDATE audit_capacity SET event_count = event_count - ? WHERE singleton = 1",
                        (pruned,),
                    )
            return {
                "cutoff": cutoff,
                "through_sequence": through_sequence,
                "matched_count": len(selected),
                "pruned_count": pruned,
                "limit": limit,
                "has_more": len(rows) > limit,
                "preview": preview,
            }

    async def health(self):
        await self.context.sql().query(
            "SELECT event_count FROM audit_capacity WHERE singleton = 1"
        )
        return {"runtime": "ready", "business": "ready"}

    async def invoke(self, service: str, method: str, params: dict):
        if service == SERVICE_NAME:
            if method == "ingest":
                return await self.ingest(**params)
            if method == "list_events":
                return await self.list_events(**params)
            if method == "prune_events":
                return await self.prune_events(**params)
        raise PluginError("method_not_found", f"Unknown method {service}/{method}")
