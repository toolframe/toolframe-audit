"""Audit's public collector identifier and transactional producer helpers."""

from .outbox import drain, enqueue, outbox_migration

__version__ = "0.1.0"
SERVICE_NAME = "audit.events"

__all__ = ["SERVICE_NAME", "drain", "enqueue", "outbox_migration"]
