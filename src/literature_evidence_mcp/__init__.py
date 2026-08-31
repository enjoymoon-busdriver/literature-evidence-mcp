"""Local, auditable literature evidence snapshots."""

__version__ = "0.1.0.dev0"

from .retrieval import search_snapshot
from .registry import LibraryRegistry, default_application_root
from .snapshot import build_snapshot, verify_snapshot

__all__ = [
    "LibraryRegistry",
    "build_snapshot",
    "default_application_root",
    "search_snapshot",
    "verify_snapshot",
]
