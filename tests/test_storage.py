from __future__ import annotations

import json

import pytest
from toolframe_sdk import PluginError, RuntimeContext

from toolframe_audit import drain, enqueue


async def _ingest(harness, info, event):
    async with harness.as_context(info):
        return await harness.audit.ingest(event)


async def _list(harness, info, **filters):
    async with harness.as_context(info):
        return await harness.audit.list_events(**filters)


async def _producer_query(harness, info, sql, parameters=()):
    async with harness.as_context(info):
        return await harness.producer_context.storage().query(sql, parameters)


@pytest.mark.asyncio
async def test_real_sqlite_owner_stores_are_isolated_and_outbox_migration_is_applied(
    harness,
):
    producer_info = harness.info("producer")
    audit_info = harness.info("producer")
    producer_tables = await _producer_query(
        harness,
        producer_info,
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
    )
    async with harness.as_context(audit_info):
        audit_tables = await harness.audit.context.storage().query(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )

    producer_names = {row["name"] for row in producer_tables}
    audit_names = {row["name"] for row in audit_tables}
    assert {
        "business_records",
        "audit_outbox",
        "audit_outbox_capacity",
    } <= producer_names
    assert {"audit_events", "audit_capacity"} <= audit_names
    assert "audit_events" not in producer_names
    assert "business_records" not in audit_names


@pytest.mark.asyncio
async def test_list_filters_keyset_pagination_and_hard_bounds(harness, event_factory):
    info = harness.info("producer")
    await _ingest(
        harness,
        info,
        event_factory(
            "first",
            action="record.created",
            entity_type="record",
            entity_id="one",
            occurred_at="2026-01-01T00:00:00Z",
        ),
    )
    await _ingest(
        harness,
        info,
        event_factory(
            "second",
            action="record.updated",
            entity_type="record",
            entity_id="two",
            occurred_at="2026-01-02T00:00:00Z",
        ),
    )
    await _ingest(
        harness,
        info,
        event_factory(
            "third",
            action="account.created",
            entity_type="account",
            entity_id="three",
            occurred_at="2026-01-03T00:00:00Z",
        ),
    )

    newest = await _list(harness, info, limit=2)
    older = await _list(harness, info, limit=2, before=newest["next_before"])
    filtered = await _list(
        harness,
        info,
        limit=100,
        source_owner="producer",
        action="record.updated",
        entity_type="record",
        entity_id="two",
        since="2026-01-01T00:00:00Z",
        until="2026-01-02T00:00:00Z",
    )

    assert [row["event_id"] for row in newest["events"]] == ["third", "second"]
    assert newest["next_before"] == 2
    assert [row["event_id"] for row in older["events"]] == ["first"]
    assert older["next_before"] is None
    assert [row["event_id"] for row in filtered["events"]] == ["second"]

    for limit in (0, 101):
        with pytest.raises(PluginError) as error:
            await _list(harness, info, limit=limit)
        assert error.value.code == "audit_validation"
    with pytest.raises(PluginError, match="since"):
        await _list(
            harness,
            info,
            since="2026-02-01T00:00:00Z",
            until="2026-01-01T00:00:00Z",
        )


@pytest.mark.asyncio
async def test_large_non_ascii_pages_are_bounded_and_continue_with_keyset(
    harness, event_factory
):
    info = harness.info("producer")
    metadata = {"description": "界" * 4_000}
    for index in range(50):
        await _ingest(
            harness,
            info,
            event_factory(
                f"large-{index:02d}", metadata=metadata, entity_id=f"e-{index}"
            ),
        )

    pages = []
    before = None
    while True:
        page = await _list(
            harness,
            info,
            limit=100,
            **({"before": before} if before is not None else {}),
        )
        pages.append(page)
        if page["next_before"] is None:
            break
        before = page["next_before"]

    assert 0 < pages[0]["count"] < 50
    assert pages[0]["next_before"] is not None
    assert sum(page["count"] for page in pages) == 50
    assert pages[-1]["next_before"] is None


@pytest.mark.asyncio
async def test_capacity_allows_duplicate_but_rejects_new_event_and_rolls_back(
    harness, event_factory
):
    harness.audit.context.config["max_events"] = 1
    info = harness.info("producer")
    event = event_factory("capacity-first")
    assert (await _ingest(harness, info, event))["status"] == "accepted"
    assert (await _ingest(harness, info, event))["status"] == "duplicate"

    with pytest.raises(PluginError) as error:
        await _ingest(harness, info, event_factory("capacity-second"))
    assert error.value.code == "audit_capacity"
    page = await _list(harness, info)
    assert page["count"] == 1


@pytest.mark.asyncio
async def test_prune_preview_apply_cutoff_and_late_replay_fence(harness, event_factory):
    info = harness.info("producer")
    old = event_factory("old", occurred_at="2026-01-01T00:00:00Z")
    new = event_factory("new", occurred_at="2026-02-01T00:00:00Z")
    await _ingest(harness, info, old)
    await _ingest(harness, info, new)

    async with harness.as_context(info):
        preview = await harness.audit.prune_events(
            cutoff="2026-02-01T00:00:00Z", limit=1, preview=True
        )
    assert preview == {
        "cutoff": "2026-02-01T00:00:00.000000Z",
        "through_sequence": 2,
        "matched_count": 1,
        "pruned_count": 0,
        "limit": 1,
        "has_more": False,
        "preview": True,
    }
    assert (await _list(harness, info))["count"] == 2

    async with harness.as_context(info):
        applied = await harness.audit.prune_events(
            cutoff=preview["cutoff"],
            limit=preview["limit"],
            preview=False,
            through_sequence=preview["through_sequence"],
        )
    assert applied["pruned_count"] == 1
    assert [row["event_id"] for row in (await _list(harness, info))["events"]] == [
        "new"
    ]

    with pytest.raises(PluginError) as error:
        await _ingest(harness, info, old)
    assert error.value.code == "audit_retention"
    assert (await _list(harness, info))["count"] == 1

    async with harness.as_context(info):
        with pytest.raises(PluginError):
            await harness.audit.prune_events(
                cutoff="2026-03-01T00:00:00Z", preview=False
            )


@pytest.mark.asyncio
async def test_retention_empty_preview_can_apply_zero_high_water_mark(harness):
    info = harness.info("producer")
    for sequence in (False, 0.0, -1, 2**63):
        with pytest.raises(PluginError) as error:
            async with harness.as_context(info):
                await harness.audit.prune_events(
                    cutoff="2026-03-01T00:00:00Z", through_sequence=sequence
                )
        assert error.value.code == "audit_validation"
    for limit in (0, 1001):
        async with harness.as_context(info):
            with pytest.raises(PluginError) as error:
                await harness.audit.prune_events(
                    cutoff="2026-03-01T00:00:00Z", limit=limit
                )
        assert error.value.code == "audit_validation"

    async with harness.as_context(info):
        with pytest.raises(PluginError) as error:
            await harness.audit.prune_events(
                cutoff="2026-03-01T00:00:00Z", preview="yes"
            )
    assert error.value.code == "audit_validation"

    async with harness.as_context(info):
        preview = await harness.audit.prune_events(
            cutoff="2026-03-01T00:00:00Z", through_sequence=0
        )
        applied = await harness.audit.prune_events(
            cutoff=preview["cutoff"], through_sequence=0, preview=False
        )
    assert preview["matched_count"] == 0
    assert applied["pruned_count"] == 0

    with pytest.raises(PluginError) as error:
        await _ingest(
            harness,
            info,
            {
                "event_id": "late",
                "occurred_at": "2026-02-01T00:00:00Z",
                "action": "record.created",
                "entity_type": "record",
                "entity_id": "late",
                "metadata": {},
                "source_attested_actor": {"kind": "background"},
            },
        )
    assert error.value.code == "audit_retention"


@pytest.mark.asyncio
async def test_provider_restart_preserves_audit_history(harness, event_factory):
    info = harness.info("producer")
    await _ingest(harness, info, event_factory("survives-restart"))
    await harness.reopen_provider()

    page = await _list(harness, info)
    assert [row["event_id"] for row in page["events"]] == ["survives-restart"]
    async with harness.as_context(info):
        assert await harness.audit.health() == {
            "runtime": "ready",
            "business": "ready",
        }


@pytest.mark.asyncio
async def test_enqueue_uses_same_business_transaction_and_captures_original_actor(
    harness,
):
    info = harness.info(
        "producer", principal={"kind": "user", "user_id": "original-user"}
    )
    async with (
        harness.as_context(info),
        harness.producer_context.storage().transaction() as transaction,
    ):
        await transaction.execute(
            "INSERT INTO business_records(id, value) VALUES (?, ?)",
            ("business-1", "saved"),
        )
        await enqueue(
            harness.producer_context,
            transaction,
            event_id="outbox-1",
            action="record.created",
            entity_type="record",
            entity_id="business-1",
            metadata={"declared_actor": "untrusted-input"},
        )

    outbox = await _producer_query(
        harness, info, "SELECT event_id, payload FROM audit_outbox"
    )
    business = await _producer_query(
        harness, info, "SELECT id, value FROM business_records"
    )
    assert business == [{"id": "business-1", "value": "saved"}]
    payload = json.loads(outbox[0]["payload"])
    assert payload["source_attested_actor"] == {
        "kind": "user",
        "user_id": "original-user",
    }
    assert payload["metadata"] == {"declared_actor": "untrusted-input"}

    with pytest.raises(TypeError):
        async with (
            harness.as_context(info),
            harness.producer_context.storage().transaction() as transaction,
        ):
            await enqueue(
                harness.producer_context,
                transaction,
                event_id="forged-actor",
                action="record.created",
                entity_type="record",
                entity_id="business-1",
                source_attested_actor={"kind": "operator"},
            )


@pytest.mark.asyncio
async def test_enqueue_from_sdk_task_root_uses_background_actor_in_same_sql_transaction(
    harness,
):
    small_root = harness.info("producer", root_id="task-root", complete=False)
    async with (
        harness.as_context(small_root),
        harness.producer_context.storage().transaction() as transaction,
    ):
        await transaction.execute(
            "INSERT INTO business_records(id, value) VALUES (?, ?)",
            ("background-business", "saved"),
        )
        await enqueue(
            harness.producer_context,
            transaction,
            event_id="background-event",
            action="maintenance.completed",
            entity_type="record",
            entity_id="background-business",
        )

    outbox = await _producer_query(
        harness,
        harness.info("producer"),
        "SELECT event_id, payload FROM audit_outbox",
    )
    assert json.loads(outbox[0]["payload"])["source_attested_actor"] == {
        "kind": "background"
    }
    assert await _producer_query(
        harness,
        harness.info("producer"),
        "SELECT id FROM business_records WHERE id = ?",
        ("background-business",),
    ) == [{"id": "background-business"}]


@pytest.mark.asyncio
async def test_business_and_outbox_rollback_together(harness):
    info = harness.info("producer")
    with pytest.raises(RuntimeError, match="abort business unit"):
        async with (
            harness.as_context(info),
            harness.producer_context.storage().transaction() as transaction,
        ):
            await transaction.execute(
                "INSERT INTO business_records(id, value) VALUES (?, ?)",
                ("rolled-back", "unsaved"),
            )
            await enqueue(
                harness.producer_context,
                transaction,
                event_id="rolled-back-event",
                action="record.created",
                entity_type="record",
                entity_id="rolled-back",
            )
            raise RuntimeError("abort business unit")

    assert await _producer_query(harness, info, "SELECT id FROM business_records") == []
    assert (
        await _producer_query(harness, info, "SELECT event_id FROM audit_outbox") == []
    )
    assert await _producer_query(
        harness, info, "SELECT pending_count FROM audit_outbox_capacity"
    ) == [{"pending_count": 0}]


@pytest.mark.asyncio
async def test_outbox_capacity_and_invalid_transaction_are_rejected(harness):
    info = harness.info("producer")
    with pytest.raises(PluginError) as invalid:
        async with harness.as_context(info):
            await enqueue(
                harness.producer_context,
                harness.producer_context.storage(),
                event_id="without-transaction",
                action="record.created",
                entity_type="record",
                entity_id="record",
            )
    assert invalid.value.code == "audit_outbox_transaction"

    other_context = RuntimeContext(
        None, plugin_id="producer", generation="other-generation"
    )
    with pytest.raises(PluginError) as mismatch:
        async with (
            harness.as_context(info),
            harness.producer_context.storage().transaction() as transaction,
        ):
            await enqueue(
                other_context,
                transaction,
                event_id="wrong-context",
                action="record.created",
                entity_type="record",
                entity_id="record",
            )
    assert mismatch.value.code == "audit_outbox_transaction"

    with pytest.raises(PluginError) as capacity:
        async with (
            harness.as_context(info),
            harness.producer_context.storage().transaction() as transaction,
        ):
            await enqueue(
                harness.producer_context,
                transaction,
                event_id="pending-1",
                action="record.created",
                entity_type="record",
                entity_id="record-1",
                max_pending=1,
            )
            await enqueue(
                harness.producer_context,
                transaction,
                event_id="pending-2",
                action="record.created",
                entity_type="record",
                entity_id="record-2",
                max_pending=1,
            )
    assert capacity.value.code == "audit_outbox_capacity"
    assert (
        await _producer_query(harness, info, "SELECT event_id FROM audit_outbox") == []
    )


@pytest.mark.asyncio
async def test_drain_requires_admitted_root_and_enforces_finite_bounds(harness):
    with pytest.raises(PluginError) as missing_root:
        async with harness.as_context({}):
            await drain(harness.producer_context)
    assert missing_root.value.code == "audit_untrusted_context"

    small_root = harness.info("producer", root_id="bounds-root", complete=False)
    for kwargs in (
        {"limit": 0},
        {"limit": 101},
        {"budget_seconds": 0},
        {"budget_seconds": 31},
    ):
        with pytest.raises(PluginError) as error:
            async with harness.as_context(small_root):
                await drain(harness.producer_context, **kwargs)
        assert error.value.code == "audit_validation"


@pytest.mark.asyncio
async def test_drain_replays_after_ack_failure_and_deduplicates_audit_event(
    harness,
):
    info = harness.info(
        "producer", principal={"kind": "user", "user_id": "enqueue-user"}
    )
    async with (
        harness.as_context(info),
        harness.producer_context.storage().transaction() as transaction,
    ):
        await enqueue(
            harness.producer_context,
            transaction,
            event_id="replay-safe",
            action="record.created",
            entity_type="record",
            entity_id="record-1",
        )

    harness.fail_ack_once = True
    with pytest.raises(PluginError) as error:
        async with harness.as_context(info):
            await drain(harness.producer_context, limit=1, budget_seconds=5)
    assert error.value.code == "simulated_ack_failure"
    assert await _producer_query(
        harness, info, "SELECT event_id FROM audit_outbox"
    ) == [{"event_id": "replay-safe"}]

    small_root = harness.info("producer", root_id="retry-root", complete=False)
    async with harness.as_context(small_root):
        result = await drain(harness.producer_context, limit=1, budget_seconds=5)
    assert result["delivered"] == result["acknowledged"] == 1
    assert (
        await _producer_query(harness, info, "SELECT event_id FROM audit_outbox") == []
    )
    page = await _list(harness, info)
    assert page["count"] == 1
    assert page["events"][0]["event_id"] == "replay-safe"
    assert page["events"][0]["source_attested_actor"] == {
        "kind": "user",
        "user_id": "enqueue-user",
    }
