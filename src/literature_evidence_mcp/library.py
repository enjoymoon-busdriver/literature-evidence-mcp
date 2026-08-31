from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import (
    SNAPSHOT_ID_PATTERN,
    _root_guard,
    _validated_root,
    load_snapshot_catalog,
    snapshot_catalog_lock,
    snapshot_record,
    write_snapshot_catalog,
)
from .errors import ImportPolicyError, LiteratureEvidenceError, SnapshotError
from .retrieval import search_snapshot
from .snapshot import build_snapshot, verify_snapshot


_SNAPSHOT_ID = SNAPSHOT_ID_PATTERN
_CONTIGUOUS_CJK = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]{2,}"
)
_PRIVATE_RESULT_KEYS = {"snapshot_path"}


def _public_result(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _public_result(item)
            for key, item in value.items()
            if key not in _PRIVATE_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_public_result(item) for item in value]
    if isinstance(value, tuple):
        return [_public_result(item) for item in value]
    return value


class FixedLibrary:
    """Thin application boundary around one fixed local snapshot library."""

    def __init__(self, library: Path) -> None:
        try:
            requested = Path(library).expanduser()
            if requested.is_symlink():
                raise ImportPolicyError("固定资料库根目录不能是符号链接。")
            self._root_guard = _root_guard(requested)
            self._root = self._root_guard.application_root
        except ImportPolicyError:
            raise
        except (SnapshotError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ImportPolicyError("无法固定资料库根目录。") from exc

    def _snapshots_directory(self, *, allow_missing: bool) -> Path | None:
        root = _validated_root(
            self._root_guard,
            allow_missing=allow_missing,
        )
        if root is None:
            return None

        snapshots = root / "snapshots"
        try:
            snapshots_status = snapshots.lstat()
        except FileNotFoundError:
            if allow_missing:
                return None
            raise SnapshotError("资料库中尚无 snapshots 目录。") from None
        except OSError as exc:
            raise SnapshotError("无法读取 snapshots 目录状态。") from exc
        if stat.S_ISLNK(snapshots_status.st_mode) or not stat.S_ISDIR(
            snapshots_status.st_mode
        ):
            raise SnapshotError("snapshots 必须是普通目录且不能是符号链接。")
        return snapshots

    @staticmethod
    def _validated_snapshot_id(snapshot_id: str) -> str:
        if not isinstance(snapshot_id, str) or _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
            raise SnapshotError("snapshot_id 格式无效。")
        return snapshot_id

    def _snapshot_directory(self, snapshot_id: str) -> Path:
        snapshot_id = self._validated_snapshot_id(snapshot_id)
        snapshots = self._snapshots_directory(allow_missing=False)
        assert snapshots is not None
        candidate = snapshots / snapshot_id
        try:
            candidate_status = candidate.lstat()
        except FileNotFoundError:
            raise SnapshotError("找不到所选快照。") from None
        except OSError as exc:
            raise SnapshotError("无法读取所选快照状态。") from exc
        if stat.S_ISLNK(candidate_status.st_mode) or not stat.S_ISDIR(
            candidate_status.st_mode
        ):
            raise SnapshotError("所选快照必须是普通目录且不能是符号链接。")
        return candidate

    def _published_snapshot(
        self, snapshot_id: str
    ) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        catalog = load_snapshot_catalog(self._root_guard)
        record = snapshot_record(catalog, snapshot_id)
        return self._snapshot_directory(snapshot_id), record, catalog

    @staticmethod
    def _catalog_bound_status(
        status: dict[str, Any], record: dict[str, Any]
    ) -> dict[str, Any]:
        if status["manifest_sha256"] != record["manifest_sha256"]:
            raise SnapshotError("快照内容与目录册绑定不一致。")
        return status

    def catalog_status(self) -> dict[str, Any]:
        catalog = load_snapshot_catalog(self._root_guard)
        return {
            "snapshot_count": len(catalog["snapshots"]),
            "current_snapshot_id": catalog["current_snapshot_id"],
            "last_successful_snapshot_id": catalog[
                "last_successful_snapshot_id"
            ],
        }

    def status(self) -> dict[str, Any]:
        """Return a cheap path-free status without verifying snapshot contents."""
        try:
            snapshots = self._snapshots_directory(allow_missing=True)
            if snapshots is None:
                return {
                    "ready": False,
                    "readonly": True,
                    "snapshot_count": 0,
                }
            with os.scandir(snapshots) as entries:
                snapshot_count = sum(
                    1
                    for entry in entries
                    if _SNAPSHOT_ID.fullmatch(entry.name) is not None
                    and entry.is_dir(follow_symlinks=False)
                )
        except (LiteratureEvidenceError, OSError):
            return {
                "ready": False,
                "readonly": True,
                "snapshot_count": 0,
                "error": "资料库结构无效或不可安全读取。",
            }
        return {
            "ready": True,
            "readonly": True,
            "snapshot_count": snapshot_count,
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        """Return only cataloged successful snapshots, newest first."""
        if _validated_root(self._root_guard, allow_missing=True) is None:
            return []
        catalog = load_snapshot_catalog(self._root_guard)
        results: list[dict[str, Any]] = []
        for record in reversed(catalog["snapshots"]):
            snapshot_id = record["snapshot_id"]
            try:
                result = self._catalog_bound_status(
                    verify_snapshot(self._snapshot_directory(snapshot_id)), record
                )
            except (LiteratureEvidenceError, OSError):
                results.append(
                    {
                        "snapshot_id": snapshot_id,
                        "verified": False,
                        "counts": None,
                        "base_snapshot_id": record["base_snapshot_id"],
                        "current": snapshot_id
                        == catalog["current_snapshot_id"],
                        "last_successful": snapshot_id
                        == catalog["last_successful_snapshot_id"],
                        "error": "快照未通过核验。",
                    }
                )
            else:
                public = _public_result(result)
                public.update(
                    {
                        "base_snapshot_id": record["base_snapshot_id"],
                        "current": snapshot_id
                        == catalog["current_snapshot_id"],
                        "last_successful": snapshot_id
                        == catalog["last_successful_snapshot_id"],
                    }
                )
                results.append(public)
        return results

    def verify(self, snapshot_id: str) -> dict[str, Any]:
        snapshot, record, catalog = self._published_snapshot(snapshot_id)
        result = self._catalog_bound_status(verify_snapshot(snapshot), record)
        public = _public_result(result)
        public.update(
            {
                "base_snapshot_id": record["base_snapshot_id"],
                "current": snapshot_id == catalog["current_snapshot_id"],
                "last_successful": snapshot_id
                == catalog["last_successful_snapshot_id"],
            }
        )
        return public

    def search(
        self,
        snapshot_id: str,
        query: str,
        *,
        top_k: int = 5,
        excerpt_chars: int = 1000,
    ) -> dict[str, Any]:
        snapshot, record, _catalog = self._published_snapshot(snapshot_id)
        result = search_snapshot(
            snapshot,
            query,
            top_k=top_k,
            excerpt_chars=excerpt_chars,
            expected_manifest_sha256=record["manifest_sha256"],
        )
        public = _public_result(result)
        if public.get("found") is False and isinstance(query, str):
            if _CONTIGUOUS_CJK.search(query) is not None:
                public["input_hint"] = "连续中文检索召回有限，可尝试在关键词间加空格。"
        return public

    def build(
        self,
        controlled_sources: Sequence[Path],
        *,
        base_snapshot_id: str | None = None,
        replacements: Mapping[str, Path] | None = None,
        remove_document_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Build from paths already confined and validated by the upload layer."""
        _validated_root(self._root_guard, allow_missing=True)
        if not all(isinstance(source, Path) for source in controlled_sources):
            raise ImportPolicyError("构建输入必须来自受控临时文件。")
        result = build_snapshot(
            self._root_guard,
            tuple(controlled_sources),
            base_snapshot_id=base_snapshot_id,
            replacements=replacements,
            remove_document_ids=remove_document_ids,
        )
        return _public_result(result)

    def activate(self, snapshot_id: str) -> dict[str, Any]:
        """Explicitly move current to one verified successful snapshot."""
        with snapshot_catalog_lock(self._root_guard):
            snapshot, record, catalog = self._published_snapshot(snapshot_id)
            self._catalog_bound_status(verify_snapshot(snapshot), record)
            if catalog["current_snapshot_id"] != snapshot_id:
                next_catalog = {
                    **catalog,
                    "current_snapshot_id": snapshot_id,
                    "snapshots": [dict(item) for item in catalog["snapshots"]],
                }
                write_snapshot_catalog(self._root_guard, next_catalog)
                catalog = next_catalog
        return {
            "snapshot_id": snapshot_id,
            "current_snapshot_id": catalog["current_snapshot_id"],
            "last_successful_snapshot_id": catalog[
                "last_successful_snapshot_id"
            ],
            "verified": True,
        }


__all__ = ["FixedLibrary"]
