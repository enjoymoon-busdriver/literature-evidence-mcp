"""Local, auditable literature evidence snapshots."""

__version__ = "0.1.0.dev0"

from .retrieval import search_snapshot
from .snapshot import build_snapshot, verify_snapshot

__all__ = ["build_snapshot", "search_snapshot", "verify_snapshot"]
