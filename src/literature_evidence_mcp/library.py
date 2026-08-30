from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Sequence

from .errors import ImportPolicyError, LiteratureEvidenceError, SnapshotError
from .retrieval import search_snapshot
from .snapshot import build_snapshot, verify_snapshot


_SNAPSHOT_ID = re.compile(
    r"\A[0-9]{8}T[0-9]{12}Z-[0-9a-f]{12}-[0-9a-f]{8}\Z"
)
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
            self._root = requested.resolve(strict=False)
        except ImportPolicyError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ImportPolicyError("无法固定资料库根目录。") from exc

    def _snapshots_directory(self, *, allow_missing: bool) -> Path | None:
        try:
            root_status = self._root.lstat()
        except FileNotFoundError:
            if allow_missing:
                return None
            raise SnapshotError("资料库尚未建立。") from None
        except OSError as exc:
            raise SnapshotError("无法读取固定资料库状态。") from exc
        if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
            raise SnapshotError("固定资料库根目录无效。")

        snapshots = self._root / "snapshots"
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
        """Verify safe, direct snapshot children and return newest IDs first."""
        snapshots = self._snapshots_directory(allow_missing=True)
        if snapshots is None:
            return []
        try:
            with os.scandir(snapshots) as entries:
                names = sorted(
                    (
                        entry.name
                        for entry in entries
                        if _SNAPSHOT_ID.fullmatch(entry.name) is not None
                        and entry.is_dir(follow_symlinks=False)
                    ),
                    reverse=True,
                )
        except OSError as exc:
            raise SnapshotError("无法列出固定资料库中的快照。") from exc

        results: list[dict[str, Any]] = []
        for snapshot_id in names:
            try:
                result = verify_snapshot(self._snapshot_directory(snapshot_id))
            except (LiteratureEvidenceError, OSError):
                results.append(
                    {
                        "snapshot_id": snapshot_id,
                        "verified": False,
                        "counts": None,
                        "error": "快照未通过核验。",
                    }
                )
            else:
                results.append(_public_result(result))
        return results

    def verify(self, snapshot_id: str) -> dict[str, Any]:
        result = verify_snapshot(self._snapshot_directory(snapshot_id))
        return _public_result(result)

    def search(
        self,
        snapshot_id: str,
        query: str,
        *,
        top_k: int = 5,
        excerpt_chars: int = 1000,
    ) -> dict[str, Any]:
        result = search_snapshot(
            self._snapshot_directory(snapshot_id),
            query,
            top_k=top_k,
            excerpt_chars=excerpt_chars,
        )
        public = _public_result(result)
        if public.get("found") is False and isinstance(query, str):
            if _CONTIGUOUS_CJK.search(query) is not None:
                public["input_hint"] = "连续中文检索召回有限，可尝试在关键词间加空格。"
        return public

    def build(self, controlled_sources: Sequence[Path]) -> dict[str, Any]:
        """Build from paths already confined and validated by the upload layer."""
        if not all(isinstance(source, Path) for source in controlled_sources):
            raise ImportPolicyError("构建输入必须来自受控临时文件。")
        result = build_snapshot(self._root, tuple(controlled_sources))
        return _public_result(result)


__all__ = ["FixedLibrary"]
