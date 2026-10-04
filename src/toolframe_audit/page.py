"""Streamlit dashboard for the durable audit event history."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from toolframe_sdk import PageContext, PluginError

SERVICE = "audit.events"
_FILTER_STATE = "toolframe-audit-filters"
_CURSORS_STATE = "toolframe-audit-cursors"
_SIGNATURE_STATE = "toolframe-audit-filter-signature"
_LIMITS = (25, 50, 100)
_MAX_CURSOR = 2**63 - 1
_MAX_TEXT = 256
_MAX_METADATA = 2_000
_FILTER_NAMES = (
    "source_owner",
    "action",
    "entity_type",
    "entity_id",
    "since",
    "until",
)
_RETENTION_INPUT_STATE = "toolframe-audit-retention-input"
_RETENTION_PREVIEW_STATE = "toolframe-audit-retention-preview"
_RETENTION_CONFIRM_STATE = "toolframe-audit-retention-confirm"
_RETENTION_FLASH_STATE = "toolframe-audit-retention-flash"


def _default_filters() -> dict[str, Any]:
    return {
        "limit": 50,
        "source_owner": "",
        "action": "",
        "entity_type": "",
        "entity_id": "",
        "since": "",
        "until": "",
    }


def _normalise_filters(value: Any) -> dict[str, Any]:
    defaults = _default_filters()
    if isinstance(value, Mapping):
        for name in _FILTER_NAMES:
            item = value.get(name, "")
            defaults[name] = item.strip() if isinstance(item, str) else ""
        limit = value.get("limit", defaults["limit"])
        if isinstance(limit, int) and not isinstance(limit, bool) and limit in _LIMITS:
            defaults["limit"] = limit
    return defaults


def _filter_signature(filters: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        [filters.get("limit", 50)] + [filters.get(name, "") for name in _FILTER_NAMES]
    )


def _valid_cursor(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= _MAX_CURSOR
    )


def _valid_highwater(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_CURSOR
    )


def _cursor_stack(state: Mapping[str, Any]) -> list[int | None]:
    raw = state.get(_CURSORS_STATE)
    if (
        not isinstance(raw, list)
        or not raw
        or any(cursor is not None and not _valid_cursor(cursor) for cursor in raw)
    ):
        return [None]
    return list(raw)


def _request(filters: Mapping[str, Any], before: int | None) -> dict[str, Any]:
    request: dict[str, Any] = {"limit": filters["limit"]}
    if before is not None:
        request["before"] = before
    for name in _FILTER_NAMES:
        value = filters.get(name)
        if isinstance(value, str) and value:
            request[name] = value
    return request


def _bounded_text(value: Any, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _actor_label(actor: Any) -> str:
    """Render actor identity fields as plain bounded text."""

    if not isinstance(actor, Mapping):
        return "Unknown"
    kind = _bounded_text(actor.get("kind")) or "unknown"
    identity: list[str] = []
    for field in ("user_id", "client_id"):
        value = actor.get(field)
        if value is not None and str(value):
            identity.append(f"{field}={_bounded_text(value, 96)}")
    if identity:
        return f"{kind} · {', '.join(identity)}"
    return kind


def _metadata_text(metadata: Any) -> str:
    """Return bounded JSON text suitable for a dataframe cell."""

    if not isinstance(metadata, Mapping):
        return "{}"
    try:
        text = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except RecursionError, TypeError, ValueError:
        return "{unavailable}"
    if len(text) <= _MAX_METADATA:
        return text
    return text[: _MAX_METADATA - 1] + "…"


def _event_row(event: Mapping[str, Any]) -> dict[str, Any]:
    """Project an event into a bounded, readable table row."""

    return {
        "Sequence": event.get("sequence"),
        "Event ID": _bounded_text(event.get("event_id")),
        "Application": _bounded_text(event.get("application_id")),
        "Source owner": _bounded_text(event.get("source_owner")),
        "Source generation": _bounded_text(event.get("source_generation")),
        "Occurred (UTC)": _bounded_text(event.get("occurred_at")),
        "Received (UTC)": _bounded_text(event.get("received_at")),
        "Action": _bounded_text(event.get("action")),
        "Entity type": _bounded_text(event.get("entity_type")),
        "Entity ID": _bounded_text(event.get("entity_id")),
        "Source actor (attested)": _actor_label(event.get("source_attested_actor")),
        "Delivery actor (host-admitted)": _actor_label(event.get("delivery_actor")),
        "Metadata": _metadata_text(event.get("metadata")),
    }


def _jsonl(events: list[Mapping[str, Any]]) -> str:
    """Serialize at most one displayed page for a bounded download."""

    rows = [
        json.dumps(dict(event), ensure_ascii=False, sort_keys=True, default=str)
        for event in events[:100]
    ]
    return "\n".join(rows) + ("\n" if rows else "")


def _show_error(st: Any, error: PluginError) -> None:
    st.error(error.message)
    st.caption(f"Audit read failed ({error.code}).")


def _render_filter_form(st: Any, active: Mapping[str, Any]) -> bool:
    with st.form("toolframe-audit-filter-form"):
        st.subheader("Filters")
        first, second, third = st.columns(3)
        source_owner = first.text_input(
            "Source owner",
            value=active["source_owner"],
            max_chars=64,
            key="toolframe-audit-source-owner",
            help="Exact source owner identifier.",
        )
        action = second.text_input(
            "Action",
            value=active["action"],
            max_chars=128,
            key="toolframe-audit-action",
            help="Exact event action.",
        )
        entity_type = third.text_input(
            "Entity type",
            value=active["entity_type"],
            max_chars=128,
            key="toolframe-audit-entity-type",
            help="Exact entity type.",
        )
        entity_id = first.text_input(
            "Entity ID",
            value=active["entity_id"],
            max_chars=256,
            key="toolframe-audit-entity-id",
            help="Exact entity identifier.",
        )
        since = second.text_input(
            "Since (UTC ISO 8601)",
            value=active["since"],
            max_chars=40,
            key="toolframe-audit-since",
            placeholder="2026-01-01T00:00:00Z",
        )
        until = third.text_input(
            "Until (UTC ISO 8601)",
            value=active["until"],
            max_chars=40,
            key="toolframe-audit-until",
            placeholder="2026-01-31T23:59:59Z",
        )
        limit = st.selectbox(
            "Rows per page",
            _LIMITS,
            index=_LIMITS.index(active["limit"]),
            key="toolframe-audit-limit",
            help="The service caps each page at 100 rows.",
        )
        submitted = st.form_submit_button("Apply filters")
    if not submitted:
        return False
    st.session_state[_FILTER_STATE] = {
        "limit": int(limit),
        "source_owner": source_owner.strip(),
        "action": action.strip(),
        "entity_type": entity_type.strip(),
        "entity_id": entity_id.strip(),
        "since": since.strip(),
        "until": until.strip(),
    }
    st.session_state[_CURSORS_STATE] = [None]
    return True


def _render_navigation(st: Any, cursors: list[int | None], next_before: Any) -> None:
    previous, reset, next_page = st.columns(3)
    if previous.button(
        "← Newer events",
        disabled=len(cursors) <= 1,
        key="toolframe-audit-previous",
    ):
        st.session_state[_CURSORS_STATE] = cursors[:-1] or [None]
        st.rerun()
    if reset.button(
        "Reset to newest",
        disabled=len(cursors) <= 1,
        key="toolframe-audit-reset",
    ):
        st.session_state[_CURSORS_STATE] = [None]
        st.rerun()
    can_continue = _valid_cursor(next_before)
    if (
        next_page.button(
            "Older events →",
            disabled=not can_continue,
            key="toolframe-audit-next",
        )
        and can_continue
    ):
        st.session_state[_CURSORS_STATE] = [*cursors, next_before]
        st.rerun()


def _render_retention(st: Any, service: Any, context: PageContext) -> None:
    """Render the explicit, high-water-marked retention workflow for operators."""

    if not context.can("system.plugins.manage"):
        return

    with st.expander("Retention", expanded=False):
        st.caption("Requires the Manage plugins operator permission.")
        flash = st.session_state.pop(_RETENTION_FLASH_STATE, None)
        if isinstance(flash, str) and flash:
            st.success(flash)
        st.caption(
            "Retention is durable. Events arriving later with an older occurred-at "
            "timestamp are rejected; pending producer outbox items need attention."
        )
        cutoff = st.text_input(
            "Delete events before (UTC ISO 8601)",
            max_chars=40,
            key="toolframe-audit-retention-cutoff",
            placeholder="2026-01-01T00:00:00Z",
            help="The cutoff is exclusive and is persisted by the Audit store.",
        ).strip()
        limit = int(
            st.number_input(
                "Retention batch limit",
                min_value=1,
                max_value=1000,
                value=100,
                step=100,
                key="toolframe-audit-retention-limit",
                help="Preview and apply at most this many rows in one operation.",
            )
        )
        input_signature = (cutoff, limit)
        if st.session_state.get(_RETENTION_INPUT_STATE) != input_signature:
            st.session_state[_RETENTION_INPUT_STATE] = input_signature
            st.session_state.pop(_RETENTION_PREVIEW_STATE, None)
            st.session_state.pop(_RETENTION_CONFIRM_STATE, None)

        if st.button(
            "Preview retention", key="toolframe-audit-retention-preview-button"
        ):
            try:
                preview = service.call(
                    "prune_events",
                    {"cutoff": cutoff, "limit": limit, "preview": True},
                )
            except PluginError as error:
                _show_error(st, error)
            else:
                if not isinstance(preview, Mapping):
                    st.error("The retention service returned an invalid preview.")
                elif not _valid_highwater(preview.get("through_sequence")):
                    st.error(
                        "The retention preview did not include a valid high-water sequence."
                    )
                else:
                    st.session_state[_RETENTION_PREVIEW_STATE] = {
                        "input": input_signature,
                        "result": dict(preview),
                    }
                    st.session_state.pop(_RETENTION_CONFIRM_STATE, None)
                    st.rerun()

        stored = st.session_state.get(_RETENTION_PREVIEW_STATE)
        if not isinstance(stored, Mapping) or stored.get("input") != input_signature:
            return
        preview = stored.get("result")
        if not isinstance(preview, Mapping):
            return
        matched = preview.get("matched_count", 0)
        highwater = preview.get("through_sequence")
        has_more = bool(preview.get("has_more"))
        st.info(
            f"Preview: {matched:,} event(s) are eligible through sequence {highwater}."
            if isinstance(matched, int)
            else f"Preview: eligible events run through sequence {highwater}."
        )
        if has_more:
            st.warning("More eligible rows remain; apply again with a new preview.")
        confirmed = st.checkbox(
            "I understand this removes one eligible batch and advances retention.",
            key=_RETENTION_CONFIRM_STATE,
        )
        if st.button(
            "Apply retention",
            disabled=not confirmed,
            key="toolframe-audit-retention-apply",
        ):
            if not _valid_highwater(highwater):
                st.error(
                    "The retention preview is missing a valid high-water sequence."
                )
                return
            try:
                result = service.call(
                    "prune_events",
                    {
                        "cutoff": cutoff,
                        "limit": limit,
                        "preview": False,
                        "through_sequence": highwater,
                    },
                )
            except PluginError as error:
                _show_error(st, error)
            else:
                removed = (
                    result.get("pruned_count", 0) if isinstance(result, Mapping) else 0
                )
                st.session_state.pop(_RETENTION_PREVIEW_STATE, None)
                st.session_state.pop(_RETENTION_CONFIRM_STATE, None)
                st.session_state[_RETENTION_FLASH_STATE] = (
                    f"Retention applied; {removed:,} event(s) removed. "
                    "The cutoff is now durable. Late older events are rejected, "
                    "and pending producer outbox items require attention."
                )
                st.rerun()


def render(context: PageContext) -> None:
    """Render the audit history using only the public SDK page facade."""

    import streamlit as st

    st.title("Audit event history")
    st.caption(
        "Review durable events captured by admitted producers. Results are read-only and bounded."
    )
    st.caption(
        "Source actor is the producer's attestation of the original actor. Delivery actor "
        "comes from the host-admitted delivery context; neither is cryptographic proof "
        "of historical identity."
    )

    active = _normalise_filters(st.session_state.get(_FILTER_STATE))
    st.session_state[_FILTER_STATE] = active
    if _render_filter_form(st, active):
        st.rerun()

    signature = _filter_signature(active)
    if st.session_state.get(_SIGNATURE_STATE) != signature:
        st.session_state[_SIGNATURE_STATE] = signature
        st.session_state[_CURSORS_STATE] = [None]
    cursors = _cursor_stack(st.session_state)
    st.session_state[_CURSORS_STATE] = cursors
    before = cursors[-1]

    if st.button("Refresh events", key="toolframe-audit-refresh"):
        st.rerun()

    try:
        service = context.services.require(SERVICE, version=1)
        result = service.call("list_events", _request(active, before))
    except PluginError as error:
        _show_error(st, error)
        return

    if not isinstance(result, Mapping) or not isinstance(result.get("events"), list):
        st.error("The audit service returned an invalid page.")
        return
    events = [event for event in result["events"] if isinstance(event, Mapping)]
    if len(events) != len(result["events"]):
        st.warning("Some rows were omitted because they did not match the event shape.")
    count = result.get("count", len(events))
    if isinstance(count, int) and not isinstance(count, bool):
        st.caption(f"{count:,} event(s) returned · page {len(cursors)}")
    else:
        st.caption(f"{len(events):,} event(s) returned · page {len(cursors)}")
    st.caption(
        "A page can contain fewer than the requested rows when the service reaches "
        "its event-byte budget."
    )

    displayed_events = events[: active["limit"]]
    if displayed_events:
        st.dataframe(
            [_event_row(event) for event in displayed_events],
            hide_index=True,
            width="stretch",
        )
        st.download_button(
            "Download displayed page (partial JSONL)",
            data=_jsonl(displayed_events),
            file_name="audit-events-page.jsonl",
            mime="application/x-ndjson",
            key="toolframe-audit-download",
            help="Exports only the currently displayed page, up to 100 events.",
        )
        st.caption(
            "This is a partial page export. Use pagination to inspect older events."
        )
    else:
        st.info("No audit events match these filters.")

    _render_navigation(st, cursors, result.get("next_before"))
    _render_retention(st, service, context)


__all__ = ["render"]
