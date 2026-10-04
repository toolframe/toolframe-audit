"""Example business plugin using Toolframe Audit's transactional outbox."""

__version__ = "0.1.0"

from .plugin import SERVICE_NAME, plugin

__all__ = ["SERVICE_NAME", "plugin"]
