"""Small SQL-backed producer demonstrating the transactional outbox boundary."""

from __future__ import annotations

import hashlib
from typing import Any

from toolframe_audit import drain, enqueue
from toolframe_sdk import Plugin, PluginError

from .plugin import SERVICE_NAME


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise PluginError("audit_example_validation", f"{field} must be text.")
    value = value.strip()
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise PluginError(
            "audit_example_validation",
            f"{field} must be nonblank text of at most {maximum} characters.",
        )
    return value


def _limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise PluginError("audit_example_validation", "limit must be an integer from 1 to 100.")
    return value


def _event_id(record_id: str) -> str:
    """Keep the deterministic event identity within Audit's 128-character bound."""
    digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
    return f"record-created:{digest}"


class Records(Plugin):
    """Business records whose writes and audit outbox enqueue share one transaction."""

    async def activate(self) -> None:
        await self.health()

    async def create_record(self, record_id: str, label: str) -> dict[str, str]:
        record_id = _text(record_id, "record_id", 128)
        label = _text(label, "label", 80)
        async with self.context.sql().transaction() as transaction:
            await transaction.execute(
                "INSERT INTO business_records(id, label) VALUES (?, ?)",
                (record_id, label),
            )
            await enqueue(
                self.context,
                transaction,
                event_id=_event_id(record_id),
                action="record.created",
                entity_type="record",
                entity_id=record_id,
                metadata={},
            )
        return {"record_id": record_id, "label": label}

    async def list_records(self, limit: int = 100) -> dict[str, Any]:
        limit = _limit(limit)
        rows = await self.context.sql().query(
            "SELECT id AS record_id, label FROM business_records ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        records = [dict(row) for row in rows]
        return {"records": records, "count": len(records), "limit": limit}

    async def drain(self) -> dict[str, Any]:
        return await drain(self.context)

    async def _drain_once(self) -> dict[str, Any]:
        return await self.drain()

    async def drain_background(self) -> dict[str, str]:
        self.context.task(self._drain_once, name="audit-example-drain")
        return {"status": "queued"}

    async def health(self) -> dict[str, str]:
        await self.context.sql().query(
            "SELECT id FROM business_records ORDER BY id LIMIT 1"
        )
        return {"runtime": "ready", "business": "ready"}

    async def invoke(self, service: str, method: str, params: dict) -> Any:
        if service != SERVICE_NAME:
            raise PluginError("method_not_found", f"Unknown method {service}/{method}")
        methods = {
            "create_record": self.create_record,
            "list_records": self.list_records,
            "drain": self.drain,
            "drain_background": self.drain_background,
        }
        handler = methods.get(method)
        if handler is None:
            raise PluginError("method_not_found", f"Unknown method {service}/{method}")
        return await handler(**params)


__all__ = ["SERVICE_NAME", "Records"]
