"""Serializable Audit contracts and packaged SQL migrations."""

from importlib.resources import files

from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    Migration,
    PluginDefinition,
    Requirement,
    Resource,
    ScopeSpec,
    ServiceSpec,
    StoreSchema,
)

from .events import ACTOR_KINDS, MAX_PAGE, MAX_SEQUENCE

SERVICE_NAME = "audit.events"


def obj(properties, *, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def text(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


ACTOR_SCHEMA = obj(
    {
        "kind": {"type": "string", "enum": list(ACTOR_KINDS)},
        "user_id": text(256),
        "client_id": text(256),
    },
    required=("kind",),
)
_EVENT_PROPERTIES = {
    "event_id": text(128),
    "occurred_at": text(40),
    "action": text(128),
    "entity_type": text(128),
    "entity_id": text(256),
    "metadata": {"type": "object"},
    "source_attested_actor": ACTOR_SCHEMA,
}
EVENT_SCHEMA = obj(_EVENT_PROPERTIES, required=_EVENT_PROPERTIES)
INGEST_INPUT = obj({"event": EVENT_SCHEMA}, required=("event",))
INGEST_OUTPUT = obj(
    {
        "status": {"enum": ["accepted", "duplicate"]},
        "event_id": text(128),
        "source_owner": text(64),
        "sequence": {"type": "integer", "minimum": 1},
    },
    required=("status", "event_id", "source_owner", "sequence"),
)
LIST_INPUT = obj(
    {
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE, "default": 50},
        "before": {"type": "integer", "minimum": 1, "maximum": MAX_SEQUENCE},
        "source_owner": text(64),
        "action": text(128),
        "entity_type": text(128),
        "entity_id": text(256),
        "since": text(40),
        "until": text(40),
    }
)
_ROW_PROPERTIES = {
    **_EVENT_PROPERTIES,
    "application_id": text(256),
    "source_owner": text(64),
    "source_generation": text(256),
    "sequence": {"type": "integer", "minimum": 1},
    "received_at": text(40),
    "delivery_actor": ACTOR_SCHEMA,
}
ROW_SCHEMA = obj(_ROW_PROPERTIES, required=_ROW_PROPERTIES)
LIST_OUTPUT = obj(
    {
        "events": {"type": "array", "maxItems": MAX_PAGE, "items": ROW_SCHEMA},
        "count": {"type": "integer", "minimum": 0, "maximum": MAX_PAGE},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        "next_before": {"type": ["integer", "null"], "minimum": 1},
    },
    required=("events", "count", "limit", "next_before"),
)
PRUNE_INPUT = obj(
    {
        "cutoff": text(40),
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
        "preview": {"type": "boolean", "default": True},
        "through_sequence": {"type": "integer", "minimum": 0, "maximum": MAX_SEQUENCE},
    },
    required=("cutoff",),
)
_PRUNE_PROPERTIES = {
    "cutoff": text(40),
    "through_sequence": {"type": "integer", "minimum": 0},
    "matched_count": {"type": "integer", "minimum": 0, "maximum": 1000},
    "pruned_count": {"type": "integer", "minimum": 0, "maximum": 1000},
    "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
    "has_more": {"type": "boolean"},
    "preview": {"type": "boolean"},
}
PRUNE_OUTPUT = obj(_PRUNE_PROPERTIES, required=_PRUNE_PROPERTIES)
_PACKAGE = "toolframe_audit"

plugin = PluginDefinition(
    id="audit",
    factory="toolframe_audit.service:Audit",
    authorization=AuthorizationSpec(
        scopes={
            "audit.read": ScopeSpec(title="Read audit events"),
        }
    ),
    config_schema=obj(
        {
            "max_events": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1_000_000,
                "default": 100_000,
            }
        }
    ),
    requires={"sql": Requirement(service="toolframe.sql")},
    services={
        SERVICE_NAME: ServiceSpec(
            methods={
                "ingest": MethodSpec(
                    audience="internal",
                    input_schema=INGEST_INPUT,
                    output_schema=INGEST_OUTPUT,
                    description="Collect a source-fenced event; admitted producer dependencies only.",
                ),
                "list_events": MethodSpec(
                    audience="business",
                    required_scopes=("audit.read",),
                    public_api=False,
                    read_only=True,
                    input_schema=LIST_INPUT,
                    output_schema=LIST_OUTPUT,
                    description="Read newest audit events with exact filters and bounded pagination.",
                ),
                "prune_events": MethodSpec(
                    audience="system",
                    required_scopes=("system.plugins.manage",),
                    input_schema=PRUNE_INPUT,
                    output_schema=PRUNE_OUTPUT,
                    description="Preview or explicitly prune one bounded batch before a retention cutoff.",
                ),
            }
        )
    },
    resources=(
        Resource(
            id="main",
            kind="dashboard.page",
            title="Audit",
            required_scopes=("audit.read",),
            data={"entrypoint": "toolframe_audit.page:render", "order": 50},
        ),
        Resource(
            id="list_events",
            kind="mcp.tool",
            title="Read audit events",
            required_scopes=("audit.read",),
            data={
                "service": SERVICE_NAME,
                "method": "list_events",
                "description": "Read audit events. Original actors are source attestations; delivery actors are admitted context.",
                "input_schema": LIST_INPUT,
                "output_schema": LIST_OUTPUT,
            },
        ),
        Resource(
            id="usage",
            kind="llms.document",
            title="Audit usage",
            required_scopes=("audit.read",),
            data={"package": _PACKAGE, "path": "docs/usage.md"},
        ),
    ),
    store=StoreSchema(
        revision="v1",
        migrations=(
            Migration(
                revision="v1",
                sql=files(_PACKAGE).joinpath("migrations/0001_init.sql").read_text(),
                dialects={
                    "postgresql": files(_PACKAGE)
                    .joinpath("migrations/0001_init.postgresql.sql")
                    .read_text()
                },
            ),
        ),
    ),
)
