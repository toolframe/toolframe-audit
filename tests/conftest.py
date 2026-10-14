from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from toolframe_sdk import (
    Migration,
    PluginError,
    RuntimeContext,
    StoreSchema,
    call_context,
)
from toolframe_storage_sqlite.plugin import ADMIN_SERVICE, SQL_SERVICE, SqliteProvider

from toolframe_audit.outbox import outbox_migration
from toolframe_audit.plugin import SERVICE_NAME
from toolframe_audit.plugin import plugin as audit_definition
from toolframe_audit.service import Audit

APP_ID = "test-application"


def _event(
    event_id: str = "event-1",
    *,
    action: str = "record.created",
    entity_type: str = "record",
    entity_id: str = "record-1",
    occurred_at: str = "2026-01-01T00:00:00Z",
    metadata: dict[str, Any] | None = None,
    actor: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "metadata": {} if metadata is None else metadata,
        "source_attested_actor": actor or {"kind": "user", "user_id": "user-1"},
    }


class _Peer:
    """Small Core-shaped router around the public SDK runtime context.

    The provider is real. This peer only supplies the Core boundary needed to
    attach source identity to nested calls and route storage by owning plugin.
    """

    def __init__(self, harness: Harness, plugin_id: str):
        self.harness = harness
        self.plugin_id = plugin_id

    def _info(self) -> dict[str, Any]:
        current = self.harness.context_info()
        return {
            "root_id": current.get("root_id") or f"root-{self.plugin_id}",
            "deadline": current.get("deadline") or time.time() + 30,
            "chain": current.get("chain", [self.plugin_id]),
            "owner": self.plugin_id,
            "owner_generation": current.get(
                "owner_generation", f"{self.plugin_id}-generation"
            ),
            "application_id": current.get(
                "application_id", self.harness.application_id
            ),
            "principal": current.get("principal"),
            "scope_catalog": current.get("scope_catalog", {}),
        }

    @asynccontextmanager
    async def _bound(self, info: dict[str, Any]):
        token = call_context.set(info)
        try:
            yield
        finally:
            call_context.reset(token)

    async def request(
        self, method: str, params: dict[str, Any], *, timeout: float = 30
    ):
        del timeout
        if method == "management.call":
            info = {
                "management": True,
                "application_id": self.harness.application_id,
                "deadline": time.time() + 30,
            }
            async with self._bound(info):
                return await self.harness.provider.invoke(
                    ADMIN_SERVICE,
                    params["action"],
                    params.get("params", {}),
                )
        if method != "service.call":
            raise PluginError("fake_peer_method", f"Unsupported peer request {method}")

        alias = params["alias"]
        if alias == "storage":
            info = self._info()
            if (
                self.plugin_id == "producer"
                and params["method"] == "execute_returning"
                and "DELETE FROM audit_outbox"
                in params.get("params", {}).get("sql", "")
                and self.harness.fail_ack_once
            ):
                self.harness.fail_ack_once = False
                raise PluginError("simulated_ack_failure", "acknowledgment failed")
            async with self._bound(info):
                return await self.harness.provider.invoke(
                    SQL_SERVICE,
                    params["method"],
                    {
                        **params.get("params", {}),
                        "transaction": params.get("params", {}).get("transaction"),
                    },
                )
        if alias == "audit":
            info = self._info()
            async with self._bound(info):
                return await self.harness.audit.invoke(
                    SERVICE_NAME,
                    params["method"],
                    params.get("params", {}),
                )
        raise PluginError("fake_peer_alias", f"Unsupported dependency alias {alias}")


class Harness:
    def __init__(self, root: Path):
        self.root = root
        self.application_id = APP_ID
        self.provider: SqliteProvider | None = None
        self.audit: Audit | None = None
        self.audit_context: RuntimeContext | None = None
        self.producer_context: RuntimeContext | None = None
        self._current_info: dict[str, Any] = {}
        self.fail_ack_once = False
        self.audit_store = audit_definition.store.model_dump()
        self.producer_store = StoreSchema(
            revision="outbox-v1",
            migrations=(
                Migration(
                    revision="business-v1",
                    sql=(
                        "CREATE TABLE business_records ("
                        "id TEXT PRIMARY KEY, value TEXT NOT NULL);"
                    ),
                    dialects={
                        "postgresql": (
                            "CREATE TABLE business_records ("
                            "id TEXT PRIMARY KEY, value TEXT NOT NULL);"
                        )
                    },
                ),
                outbox_migration(revision="outbox-v1", parent="business-v1"),
            ),
        )

    def context_info(self) -> dict[str, Any]:
        return self._current_info

    @asynccontextmanager
    async def as_context(self, info: dict[str, Any]):
        previous = self._current_info
        self._current_info = info
        token = call_context.set(info)
        try:
            yield
        finally:
            call_context.reset(token)
            self._current_info = previous

    def info(
        self,
        owner: str = "producer",
        *,
        generation: str | None = None,
        root_id: str | None = None,
        principal: dict[str, Any] | None = None,
        application_id: str | None = None,
        complete: bool = True,
    ) -> dict[str, Any]:
        value = {
            "root_id": root_id or f"root-{owner}",
            "deadline": time.time() + 30,
            "chain": [owner],
        }
        if complete:
            value.update(
                {
                    "owner": owner,
                    "owner_generation": generation or f"{owner}-generation",
                    "application_id": application_id or self.application_id,
                    "principal": principal,
                    "scope_catalog": {"audit.read": {"category": "business"}},
                }
            )
        return value

    async def ensure_store(self, owner: str, schema: dict[str, Any] | StoreSchema):
        assert self.provider is not None
        if isinstance(schema, StoreSchema):
            schema = schema.model_dump()
        info = {
            "management": True,
            "application_id": self.application_id,
            "deadline": time.time() + 30,
        }
        token = call_context.set(info)
        try:
            return await self.provider.invoke(
                ADMIN_SERVICE,
                "ensure_store",
                {"owner": owner, "schema": schema},
            )
        finally:
            call_context.reset(token)

    async def start(self):
        provider_context = RuntimeContext(
            _Peer(self, "storage-sqlite"),
            plugin_id="storage-sqlite",
            generation="storage-generation",
            state_directory=str(self.root / "storage"),
        )
        self.provider = SqliteProvider(provider_context)
        await self.provider.activate()
        await self.ensure_store("audit", self.audit_store)
        await self.ensure_store("producer", self.producer_store)

        self.audit_context = RuntimeContext(
            _Peer(self, "audit"),
            plugin_id="audit",
            generation="audit-generation",
            config={"max_events": 100_000},
        )
        self.audit = Audit(self.audit_context)
        self.producer_context = RuntimeContext(
            _Peer(self, "producer"),
            plugin_id="producer",
            generation="producer-generation",
        )

    async def close(self):
        if self.audit_context is not None:
            await self.audit_context.close()
        if self.producer_context is not None:
            await self.producer_context.close()
        if self.provider is not None:
            await self.provider.dispose()

    async def reopen_provider(self):
        assert self.provider is not None
        await self.provider.dispose()
        provider_context = RuntimeContext(
            _Peer(self, "storage-sqlite"),
            plugin_id="storage-sqlite",
            generation="storage-generation-2",
            state_directory=str(self.root / "storage"),
        )
        self.provider = SqliteProvider(provider_context)
        await self.provider.activate()
        await self.ensure_store("audit", self.audit_store)
        await self.ensure_store("producer", self.producer_store)


@pytest.fixture
def event_factory():
    return _event


@pytest_asyncio.fixture
async def harness(tmp_path):
    value = Harness(tmp_path)
    await value.start()
    try:
        yield value
    finally:
        await value.close()
