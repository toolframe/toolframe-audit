from __future__ import annotations

import math

import pytest
from toolframe_sdk import PluginError, call_context


async def _ingest(harness, info, event):
    async with harness.as_context(info):
        return await harness.audit.ingest(event)


async def _list(harness, info, **filters):
    async with harness.as_context(info):
        return await harness.audit.list_events(**filters)


@pytest.mark.asyncio
async def test_ingest_deduplicates_and_preserves_admitted_delivery_actor(
    harness, event_factory
):
    info = harness.info(
        owner="producer",
        principal={"kind": "user", "user_id": "caller-1", "client_id": "client-1"},
    )
    event = event_factory("same-event")

    accepted = await _ingest(harness, info, event)
    duplicate = await _ingest(harness, info, event)
    page = await _list(harness, info)

    assert accepted["status"] == "accepted"
    assert duplicate["status"] == "duplicate"
    assert duplicate["sequence"] == accepted["sequence"]
    assert page["count"] == 1
    row = page["events"][0]
    assert row["source_owner"] == "producer"
    assert row["source_attested_actor"] == {
        "kind": "user",
        "user_id": "user-1",
    }
    assert row["delivery_actor"] == {
        "kind": "user",
        "user_id": "caller-1",
        "client_id": "client-1",
    }


@pytest.mark.asyncio
async def test_same_event_id_is_scoped_to_application_and_source_owner(
    harness, event_factory
):
    event = event_factory("source-scoped")
    first = await _ingest(harness, harness.info("producer-a"), event)
    second = await _ingest(harness, harness.info("producer-b"), event)

    assert first["status"] == second["status"] == "accepted"
    assert {first["source_owner"], second["source_owner"]} == {
        "producer-a",
        "producer-b",
    }
    page = await _list(harness, harness.info("producer"), limit=10)
    assert {row["source_owner"] for row in page["events"]} == {
        "producer-a",
        "producer-b",
    }


@pytest.mark.asyncio
async def test_conflicting_content_for_existing_source_event_id_is_rejected(
    harness, event_factory
):
    info = harness.info("producer")
    await _ingest(harness, info, event_factory("conflict"))

    changed = event_factory("conflict", action="record.deleted")
    with pytest.raises(PluginError) as error:
        await _ingest(harness, info, changed)

    assert error.value.code == "audit_event_conflict"
    page = await _list(harness, info)
    assert page["count"] == 1
    assert page["events"][0]["action"] == "record.created"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_case", "event_change", "code"),
    [
        ("missing", {}, "audit_untrusted_context"),
        ({"owner": "audit"}, {}, "audit_untrusted_context"),
        ({"owner": "Producer"}, {}, "audit_untrusted_context"),
        ({"application_id": ""}, {}, "audit_untrusted_context"),
        ({"owner_generation": None}, {}, "audit_untrusted_context"),
        ({}, {"source_owner": "other-source"}, "audit_validation"),
        (
            {},
            {"source_attested_actor": {"kind": "user", "credential_id": "secret"}},
            "audit_validation",
        ),
    ],
)
async def test_ingest_rejects_missing_or_spoofed_context_and_event_identity(
    harness, event_factory, context_case, event_change, code
):
    complete = harness.info("producer")
    if context_case == "missing":
        complete = {}
    else:
        for key, value in context_case.items():
            if value is None:
                complete.pop(key, None)
            else:
                complete[key] = value
    event = event_factory("invalid")
    event.update(event_change)
    with pytest.raises(PluginError) as error:
        await _ingest(harness, complete, event)
    assert error.value.code == code


@pytest.mark.asyncio
async def test_missing_admitted_principal_is_background_but_missing_context_is_not(
    harness, event_factory
):
    info = harness.info("producer", principal=None)
    accepted = await _ingest(harness, info, event_factory("background"))
    page = await _list(harness, info)

    assert accepted["status"] == "accepted"
    assert page["events"][0]["delivery_actor"] == {"kind": "background"}

    token = call_context.set({})
    try:
        with pytest.raises(PluginError) as error:
            await harness.audit.ingest(event_factory("missing-context"))
    finally:
        call_context.reset(token)
    assert error.value.code == "audit_untrusted_context"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"secret": "do-not-store"},
        {"nested": {"password": "do-not-store"}},
        {"values": list(range(1025))},
        {"nested": [[[[[[[[[{"value": True}]]]]]]]]]},
        {"value": math.nan},
    ],
)
async def test_metadata_rejects_secrets_nonfinite_values_and_resource_exhaustion(
    harness, event_factory, metadata
):
    with pytest.raises(PluginError) as error:
        await _ingest(
            harness,
            harness.info("producer"),
            event_factory("bad-metadata", metadata=metadata),
        )
    assert error.value.code == "audit_validation"


@pytest.mark.asyncio
async def test_metadata_wire_limit_and_maximum_event_fields_are_enforced(
    harness, event_factory
):
    with pytest.raises(PluginError, match="Metadata exceeds"):
        await _ingest(
            harness,
            harness.info("producer"),
            event_factory("metadata-bytes", metadata={"text": "x" * 16_500}),
        )
    accepted = await _ingest(
        harness,
        harness.info("producer"),
        event_factory(
            "maximum-fields",
            action="a" * 128,
            entity_type="t" * 128,
            entity_id="e" * 256,
            metadata={"text": "x" * 16_000},
            actor={"kind": "user", "user_id": "u" * 256, "client_id": "c" * 256},
        ),
    )
    assert accepted["status"] == "accepted"

    with pytest.raises(PluginError, match="Event exceeds"):
        await _ingest(
            harness,
            harness.info("producer"),
            event_factory(
                "😀" * 128,
                action="😀" * 128,
                entity_type="😀" * 128,
                entity_id="😀" * 256,
                metadata={"text": "x" * 16_000},
                actor={
                    "kind": "user",
                    "user_id": "😀" * 256,
                    "client_id": "😀" * 256,
                },
            ),
        )


@pytest.mark.asyncio
async def test_event_requires_exact_documented_fields(harness, event_factory):
    event = event_factory("extra-field")
    event["source_owner"] = "not-admitted"
    with pytest.raises(PluginError) as error:
        await _ingest(harness, harness.info("producer"), event)
    assert error.value.code == "audit_validation"
