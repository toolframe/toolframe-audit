"""Native Streamlit dashboard coverage using a public PageContext-shaped bridge."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest
from toolframe_sdk import PluginError

from toolframe_audit.page import _jsonl, _normalise_filters, _request


def _event(event_id: str, *, sequence: int, action: str = "record.created") -> dict[str, Any]:
    return {
        "sequence": sequence,
        "event_id": event_id,
        "application_id": "app-1",
        "source_owner": "producer",
        "source_generation": "producer-generation",
        "occurred_at": "2026-01-01T00:00:00Z",
        "received_at": "2026-01-01T00:00:01Z",
        "action": action,
        "entity_type": "record",
        "entity_id": event_id,
        "source_attested_actor": {"kind": "user", "user_id": "user-1"},
        "delivery_actor": {"kind": "background", "client_id": "audit-drain"},
        "metadata": {"safe": True},
    }


class _FakeService:
    """Deterministic service double at the public PageService.call boundary."""

    def __init__(
        self,
        *,
        pages: Mapping[tuple[tuple[str, Any], ...], dict[str, Any]] | None = None,
        error: PluginError | None = None,
        preview: dict[str, Any] | None = None,
    ) -> None:
        self.pages = dict(pages or {})
        self.error = error
        self.preview = preview or {
            "cutoff": "2025-01-01T00:00:00Z",
            "through_sequence": 42,
            "matched_count": 2,
            "pruned_count": 0,
            "limit": 100,
            "has_more": False,
            "preview": True,
        }
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.mutations = 0

    def call(self, method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        request = dict(params or {})
        self.calls.append((method, request))
        if method == "list_events":
            if self.error is not None:
                raise self.error
            key = tuple(sorted(request.items()))
            return self.pages.get(
                key,
                {
                    "events": [],
                    "count": 0,
                    "limit": request.get("limit", 50),
                    "next_before": None,
                },
            )
        if method == "prune_events":
            if request.get("preview") is True:
                return {**self.preview, "cutoff": request["cutoff"], "limit": request["limit"]}
            self.mutations += 1
            return {**self.preview, "cutoff": request["cutoff"], "limit": request["limit"], "pruned_count": 2, "preview": False}
        raise AssertionError(f"unexpected method {method!r}")


class _FakeServices:
    def __init__(self, service: _FakeService) -> None:
        self.service = service

    def require(self, name: str, *, version: int = 1) -> _FakeService:
        assert name == "audit.events"
        assert version == 1
        return self.service


class _FakePageContext:
    """Only the public page facade used by the dashboard is exposed."""

    def __init__(self, service: _FakeService, scopes: tuple[str, ...] = ()) -> None:
        self.services = _FakeServices(service)
        self.scopes = set(scopes)

    def can(self, scope: str) -> bool:
        return scope in self.scopes


def _app(context: _FakePageContext) -> None:
    from toolframe_audit.page import render

    render(context)


def _run(context: _FakePageContext) -> AppTest:
    # The first import can load pandas/pyarrow in a cold worker, so give the
    # native Streamlit runner a bounded but practical startup allowance.
    return AppTest.from_function(_app, args=(context,), default_timeout=60).run()


def test_empty_page_is_readable_and_hides_retention_without_manage_scope() -> None:
    service = _FakeService(
        pages={
            (("limit", 50),): {
                "events": [],
                "count": 0,
                "limit": 50,
                "next_before": None,
            }
        }
    )

    app = _run(_FakePageContext(service, scopes=("audit.read",)))

    assert not app.exception
    assert "No audit events match these filters." in [item.value for item in app.info]
    assert not app.expander
    assert service.calls == [("list_events", {"limit": 50})]


def test_service_error_is_rendered_without_leaking_a_traceback() -> None:
    service = _FakeService(error=PluginError("audit_unavailable", "storage is offline"))

    app = _run(_FakePageContext(service, scopes=("audit.read",)))

    assert not app.exception
    assert [item.value for item in app.error] == ["storage is offline"]
    assert any("audit_unavailable" in item.value for item in app.caption)


def test_filters_and_pagination_reset_the_keyset_cursor() -> None:
    first = _event("newest", sequence=20)
    older = _event("older", sequence=10, action="record.updated")
    service = _FakeService(
        pages={
            (("limit", 50),): {
                "events": [first],
                "count": 1,
                "limit": 50,
                "next_before": 10,
            },
            (("before", 10), ("limit", 50)): {
                "events": [older],
                "count": 1,
                "limit": 50,
                "next_before": None,
            },
            (("action", "record.updated"), ("limit", 50)): {
                "events": [older],
                "count": 1,
                "limit": 50,
                "next_before": None,
            },
        }
    )

    app = _run(_FakePageContext(service, scopes=("audit.read",)))
    assert not app.exception
    app.button(key="toolframe-audit-next").click().run()
    assert not app.exception
    assert service.calls[-1] == ("list_events", {"limit": 50, "before": 10})

    app.text_input(key="toolframe-audit-action").set_value("record.updated")
    next(button for button in app.button if button.label == "Apply filters").click().run()
    assert not app.exception
    assert service.calls[-1] == (
        "list_events",
        {"limit": 50, "action": "record.updated"},
    )
    assert app.session_state["toolframe-audit-cursors"] == [None]


def test_jsonl_export_is_bounded_to_one_hundred_events_and_valid_lines() -> None:
    payload = _jsonl(
        [{"event_id": f"event-{index}", "metadata": {"index": index}} for index in range(105)]
    )
    lines = payload.splitlines()

    assert len(lines) == 100
    assert [json.loads(line)["event_id"] for line in lines] == [
        f"event-{index}" for index in range(100)
    ]
    assert payload.endswith("\n")
    assert _jsonl([]) == ""


def test_authorized_retention_requires_preview_and_confirmation_before_apply() -> None:
    service = _FakeService(
        pages={
            (("limit", 50),): {
                "events": [_event("newest", sequence=42)],
                "count": 1,
                "limit": 50,
                "next_before": None,
            }
        }
    )
    app = _run(
        _FakePageContext(service, scopes=("audit.read", "system.plugins.manage"))
    )

    assert not app.exception
    assert [item.label for item in app.expander] == ["Retention"]
    app.text_input(key="toolframe-audit-retention-cutoff").set_value(
        "2025-01-01T00:00:00Z"
    )
    app.button(key="toolframe-audit-retention-preview-button").click().run()
    assert not app.exception
    assert (
        "prune_events",
        {"cutoff": "2025-01-01T00:00:00Z", "limit": 100, "preview": True},
    ) in service.calls
    assert service.mutations == 0
    assert any("Preview: 2 event(s)" in item.value for item in app.info)
    assert app.button(key="toolframe-audit-retention-apply").proto.disabled

    # Inspect the disabled guard without an interaction unavailable to users.
    app.run()
    assert not app.exception
    assert app.button(key="toolframe-audit-retention-apply").proto.disabled
    assert service.mutations == 0
    app.checkbox(key="toolframe-audit-retention-confirm").check().run()
    assert not app.exception
    assert not app.button(key="toolframe-audit-retention-apply").proto.disabled
    assert service.mutations == 0
    app.button(key="toolframe-audit-retention-apply").click().run()
    assert not app.exception
    assert service.mutations == 1
    assert (
        "prune_events",
        {
            "cutoff": "2025-01-01T00:00:00Z",
            "limit": 100,
            "preview": False,
            "through_sequence": 42,
        },
    ) in service.calls
    assert any("Retention applied; 2 event(s) removed." in item.value for item in app.success)


def test_retention_cutoff_change_discards_stale_preview() -> None:
    service = _FakeService(
        pages={
            (("limit", 50),): {
                "events": [_event("newest", sequence=42)],
                "count": 1,
                "limit": 50,
                "next_before": None,
            }
        }
    )
    app = _run(
        _FakePageContext(service, scopes=("audit.read", "system.plugins.manage"))
    )
    app.text_input(key="toolframe-audit-retention-cutoff").set_value(
        "2025-01-01T00:00:00Z"
    )
    app.button(key="toolframe-audit-retention-preview-button").click().run()
    assert any("Preview: 2 event(s)" in item.value for item in app.info)

    app.text_input(key="toolframe-audit-retention-cutoff").set_value(
        "2025-02-01T00:00:00Z"
    ).run()

    assert not app.exception
    assert not any(item.value.startswith("Preview:") for item in app.info)
    assert not any(
        button.key == "toolframe-audit-retention-apply" for button in app.button
    )
    assert service.mutations == 0


def test_filter_helpers_strip_values_and_omit_empty_request_fields() -> None:
    filters = _normalise_filters(
        {
            "limit": 999,
            "source_owner": " producer ",
            "action": None,
            "entity_type": "  record ",
        }
    )

    assert filters["limit"] == 50
    assert filters["source_owner"] == "producer"
    assert filters["action"] == ""
    assert _request(filters, before=7) == {
        "limit": 50,
        "before": 7,
        "source_owner": "producer",
        "entity_type": "record",
    }
