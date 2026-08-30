class LiteratureEvidenceError(RuntimeError):
    """Base class for expected, user-facing project errors."""


class ImportPolicyError(LiteratureEvidenceError):
    """A selected source cannot be imported under the v0.1 policy."""


class SnapshotError(LiteratureEvidenceError):
    """A frozen snapshot failed identity, schema, or integrity checks."""


class SearchInputError(LiteratureEvidenceError):
    """A local search request is outside the bounded read-only contract."""
