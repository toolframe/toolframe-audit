"""Audit's public collector identifier and transactional producer helpers."""

from .outbox import drain, enqueue, outbox_migration

__version__ = "0.2.1"
SERVICE_NAME = "audit.events"

__all__ = ["SERVICE_NAME", "drain", "enqueue", "outbox_migration"]
