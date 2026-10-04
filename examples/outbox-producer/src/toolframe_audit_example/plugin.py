"""Serializable definition for the Audit outbox producer example."""

from importlib.resources import files

from toolframe_audit import outbox_migration
from toolframe_sdk import (
    AuthorizationSpec,
    MethodSpec,
    Migration,
    PluginDefinition,
    Requirement,
    ScopeSpec,
    ServiceSpec,
    StoreSchema,
)

PACKAGE = "toolframe_audit_example"
SERVICE_NAME = "audit-example.records"

RECORD_ID = {"type": "string", "minLength": 1, "maxLength": 128}
LABEL = {"type": "string", "minLength": 1, "maxLength": 80}
RECORD = {
    "type": "object",
    "properties": {"record_id": RECORD_ID, "label": LABEL},
    "required": ["record_id", "label"],
    "additionalProperties": False,
}
EMPTY_INPUT = {"type": "object", "additionalProperties": False}
LIST_INPUT = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100}
    },
    "additionalProperties": False,
}
LIST_OUTPUT = {
    "type": "object",
    "properties": {
        "records": {"type": "array", "items": RECORD, "maxItems": 100},
        "count": {"type": "integer", "minimum": 0, "maximum": 100},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "required": ["records", "count", "limit"],
    "additionalProperties": False,
}
DRAIN_OUTPUT = {
    "type": "object",
    "properties": {
        "delivered": {"type": "integer", "minimum": 0},
        "acknowledged": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "required": ["delivered", "acknowledged", "limit"],
    "additionalProperties": False,
}
QUEUED_OUTPUT = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["queued"]}},
    "required": ["status"],
    "additionalProperties": False,
}

_V1_SQL = files(PACKAGE).joinpath("migrations", "0001_init.sql").read_text(
    encoding="utf-8"
)

plugin = PluginDefinition(
    id="audit-example",
    factory="toolframe_audit_example.service:Records",
    authorization=AuthorizationSpec(
        scopes={
            "audit-example.read": ScopeSpec(title="Read example records"),
            "audit-example.write": ScopeSpec(title="Create records and drain audit events"),
        }
    ),
    requires={
        "storage": Requirement(service="toolframe.storage.sql"),
        "audit": Requirement(service="audit.events", version=1),
    },
    services={
        SERVICE_NAME: ServiceSpec(
            methods={
                "create_record": MethodSpec(
                    audience="business",
                    required_scopes=("audit-example.write",),
                    input_schema={
                        "type": "object",
                        "properties": {"record_id": RECORD_ID, "label": LABEL},
                        "required": ["record_id", "label"],
                        "additionalProperties": False,
                    },
                    output_schema=RECORD,
                    description="Create a record and enqueue its audit event atomically.",
                ),
                "list_records": MethodSpec(
                    audience="business",
                    required_scopes=("audit-example.read",),
                    read_only=True,
                    input_schema=LIST_INPUT,
                    output_schema=LIST_OUTPUT,
                    description="List the newest example records.",
                ),
                "drain": MethodSpec(
                    audience="internal",
                    input_schema=EMPTY_INPUT,
                    output_schema=DRAIN_OUTPUT,
                    description="Deliver one finite batch of pending audit events.",
                ),
                "drain_background": MethodSpec(
                    audience="business",
                    required_scopes=("audit-example.write",),
                    input_schema=EMPTY_INPUT,
                    output_schema=QUEUED_OUTPUT,
                    description="Schedule one finite background audit drain.",
                ),
            }
        )
    },
    store=StoreSchema(
        revision="v2",
        migrations=(
            Migration(
                revision="v1",
                sql=_V1_SQL,
                dialects={"postgresql": _V1_SQL},
            ),
            outbox_migration(revision="v2", parent="v1"),
        ),
    ),
)

__all__ = ["PACKAGE", "SERVICE_NAME", "plugin"]
