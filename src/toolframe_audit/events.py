"""Bounded event normalization and non-secret actor snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from toolframe_sdk import PluginError, Principal

MAX_METADATA_BYTES = 16_384
MAX_EVENT_BYTES = 20_480
MAX_PAGE = 100
MAX_SEQUENCE = 2**63 - 1
ACTOR_KINDS = (
    "local",
    "operator",
    "password",
    "mcp_key",
    "user",
    "delegated",
    "anonymous",
    "public",
    "background",
)
EVENT_FIELDS = {
    "event_id",
    "occurred_at",
    "action",
    "entity_type",
    "entity_id",
    "metadata",
    "source_attested_actor",
}
_SECRET_KEYS = {
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credentials",
    "credential",
    "apikey",
    "accesskey",
    "privatekey",
    "clientsecret",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "bearer",
    "sessiontoken",
}


def invalid(message: str) -> PluginError:
    return PluginError("audit_validation", message)


def text_field(value: Any, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
    ):
        raise invalid(f"{field} must be nonempty text of at most {maximum} characters.")
    return value


def timestamp(value: Any, field: str = "occurred_at") -> str:
    text_field(value, field, 40)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return (
            parsed.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except ValueError, OverflowError:
        raise invalid(
            f"{field} must be an ISO 8601 timestamp with a timezone."
        ) from None


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    try:
        result = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        )
        result.encode("utf-8")
        return result
    except TypeError, ValueError, RecursionError, UnicodeEncodeError:
        raise invalid("Event values must be finite JSON data.") from None


def actor(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) - {"kind", "user_id", "client_id"}:
        raise invalid("Actor snapshots allow only kind, user_id and client_id.")
    if value.get("kind") not in ACTOR_KINDS:
        raise invalid("Actor kind is not supported.")
    result = {"kind": value["kind"]}
    for key in ("user_id", "client_id"):
        if key in value:
            result[key] = text_field(value[key], key, 256)
    return result


def admitted(context, *, producer: bool = False) -> tuple[dict, dict]:
    """Trust only the SDK's host-delivered call context, never event parameters."""
    info = context.call_info
    owner = info.get("owner")
    if (
        not isinstance(owner, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", owner)
        or not all(
            isinstance(info.get(key), str) and 0 < len(info[key]) <= 256
            for key in ("owner_generation", "application_id", "root_id")
        )
        or "principal" not in info
        or (producer and owner == context.plugin_id)
    ):
        raise PluginError(
            "audit_untrusted_context", "A host-admitted producer context is required."
        )
    principal = info["principal"]
    if principal is None:
        return info, {"kind": "background"}
    try:
        principal = Principal.model_validate(principal)
        snapshot = {"kind": principal.kind}
        for key in ("user_id", "client_id"):
            if getattr(principal, key) is not None:
                snapshot[key] = getattr(principal, key)
        return info, actor(snapshot)
    except Exception as error:
        raise PluginError(
            "audit_untrusted_context", "The admitted principal is invalid."
        ) from error


def _metadata(value: Any, depth: int = 0, budget: list[int] | None = None) -> None:
    budget = [1024] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or depth > 8:
        raise invalid(
            "Metadata must have at most 1024 values including containers and the root, and 8 nesting levels."
        )
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise invalid("Metadata keys must be text of at most 128 characters.")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _SECRET_KEYS:
                raise invalid(
                    "Metadata contains a credential or secret field; redact it at the source."
                )
            _metadata(item, depth + 1, budget)
    elif isinstance(value, list):
        for item in value:
            _metadata(item, depth + 1, budget)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise invalid("Metadata must contain JSON values.")


def normalize_event(value: Any) -> tuple[dict, str, str]:
    if not isinstance(value, dict) or set(value) != EVENT_FIELDS:
        raise invalid(
            "Event requires exactly the documented event fields; source overrides are forbidden."
        )
    event = {
        "event_id": text_field(value["event_id"], "event_id", 128),
        "occurred_at": timestamp(value["occurred_at"]),
        "action": text_field(value["action"], "action", 128),
        "entity_type": text_field(value["entity_type"], "entity_type", 128),
        "entity_id": text_field(value["entity_id"], "entity_id", 256),
        "source_attested_actor": actor(value["source_attested_actor"]),
    }
    metadata = value["metadata"]
    if not isinstance(metadata, dict):
        raise invalid("metadata must be a JSON object.")
    _metadata(metadata)
    if len(canonical(metadata).encode()) > MAX_METADATA_BYTES:
        raise invalid("Metadata exceeds 16384 UTF-8 bytes.")
    event["metadata"] = metadata
    payload = canonical(event)
    if len(payload.encode()) > MAX_EVENT_BYTES:
        raise invalid("Event exceeds 20480 UTF-8 bytes.")
    return event, payload, hashlib.sha256(payload.encode()).hexdigest()


def bounded_integer(value: Any, field: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise invalid(f"{field} must be an integer between 1 and {maximum}.")
    return value
