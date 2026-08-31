from __future__ import annotations


class LiteratureEvidenceError(RuntimeError):
    """Base class for expected, user-facing project errors."""


class ImportPolicyError(LiteratureEvidenceError):
    """A selected source cannot be imported under the v0.1 policy."""


class SnapshotError(LiteratureEvidenceError):
    """A frozen snapshot failed identity, schema, or integrity checks."""


class SearchInputError(LiteratureEvidenceError):
    """A local search request is outside the bounded read-only contract."""


class LibraryRegistryError(LiteratureEvidenceError):
    """The local multi-library registry is invalid or cannot be updated safely."""


class VectorError(SnapshotError):
    """A derived vector object or complete snapshot mapping is invalid."""


class EnhancedSearchError(LiteratureEvidenceError):
    """An explicit enhanced search failed closed with a path-free call audit."""

    def __init__(self, message: str, audit: dict[str, object] | None = None) -> None:
        super().__init__(message)
        source = (
            {"simulated": None, "call_count": 0, "calls": []}
            if audit is None
            else audit
        )
        simulated = source.get("simulated")
        self.audit = {
            "simulated": simulated if type(simulated) is bool else None,
            "call_count": source.get("call_count", 0),
            "calls": [dict(item) for item in source.get("calls", [])],
        }
