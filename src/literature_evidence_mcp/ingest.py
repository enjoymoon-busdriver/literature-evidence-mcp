from __future__ import annotations

import hashlib
import importlib.metadata
import io
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .errors import ImportPolicyError


MAX_CHUNK_CHARS = 1200
MARKDOWN_PARSER_VERSION = 1
PDF_PARSER_VERSION = 1
CHUNKER_VERSION = 1
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SUPPORTED_SUFFIXES = {".md", ".markdown", ".pdf"}


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    text: str
    heading_path: tuple[str, ...]
    pdf_page_start: int | None
    pdf_page_end: int | None
    source_line_start: int | None
    source_line_end: int | None
    anchor_label: str


@dataclass(frozen=True)
class ParsedDocument:
    media_type: str
    extraction_method: str
    extraction_status: str
    title_hint: str
    units: tuple[str, ...]
    page_count: int | None
    parser: dict[str, object]


@dataclass(frozen=True)
class PreparedDocument:
    source_name: str
    suffix: str
    media_type: str
    payload: bytes
    source_sha256: str
    byte_size: int
    document_id: str
    asset_id: str
    title: str
    extraction_method: str
    extraction_status: str
    page_count: int | None
    extracted_char_count: int
    chunks: tuple[ChunkDraft, ...]
    parsed: ParsedDocument
    chunker: dict[str, object]
    chunk_title_hint: str


def _clean_title(value: object, fallback: str) -> str:
    raw = value if isinstance(value, str) else fallback
    visible = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in raw
    )
    cleaned = re.sub(r"\s+", " ", visible).strip()
    return (cleaned or fallback)[:500]


def _dependency_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _parser_identity(suffix: str) -> dict[str, object]:
    if suffix in {".md", ".markdown"}:
        return {
            "implementation": "native_utf8_markdown",
            "version": MARKDOWN_PARSER_VERSION,
            "config": {
                "encoding": "utf-8",
                "decode_errors": "strict",
                "newline_normalization": "lf",
                "reject_replacement_character": True,
            },
        }
    if suffix == ".pdf":
        return {
            "implementation": "pypdf_text_layer",
            "version": PDF_PARSER_VERSION,
            "config": {
                "pypdf_version": _dependency_version("pypdf"),
                "strict": False,
                "encrypted_pdf": "reject",
                "extract_text_arguments": [],
                "nul_replacement": "space",
                "outer_whitespace": "strip",
                "newline_normalization": "lf",
                "minimum_visible_characters": 10,
            },
        }
    raise ImportPolicyError("暂不支持该文件类型。")


def _chunker_identity(media_type: str) -> dict[str, object]:
    common: dict[str, object] = {
        "max_chunk_chars": MAX_CHUNK_CHARS,
        "horizontal_whitespace": "collapse_spaces_and_tabs_then_strip",
        "blank_lines": "drop",
        "oversize_line": "fixed_width",
        "ordinal_start": 1,
    }
    if media_type == "text/markdown":
        config = {
            **common,
            "heading_pattern": _HEADING.pattern,
            "heading_flags": _HEADING.flags,
            "heading_levels": 6,
            "anchor_format": "markdown-lines-heading-path-v1",
        }
        implementation = "markdown_evidence_chunks"
    elif media_type == "application/pdf":
        config = {
            **common,
            "page_scope": "single_page",
            "anchor_format": "pdf-page-v1",
        }
        implementation = "pdf_evidence_chunks"
    else:
        raise ImportPolicyError("解析结果的媒体类型不受支持。")
    return {
        "implementation": implementation,
        "version": CHUNKER_VERSION,
        "config": config,
    }


def _read_regular_file_once(path: Path) -> tuple[bytes, str]:
    candidate = Path(path).expanduser()
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise ImportPolicyError(f"找不到所选文件：{candidate.name or '所选文件'}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ImportPolicyError(f"为避免导入目标变化，暂不接受符号链接：{candidate.name}")
    if not stat.S_ISREG(before.st_mode):
        raise ImportPolicyError(f"所选路径不是普通文件：{candidate.name}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        try:
            blocks: list[bytes] = []
            digest = hashlib.sha256()
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                blocks.append(block)
                digest.update(block)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ImportPolicyError(f"无法安全读取所选文件：{candidate.name}") from exc

    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after:
        raise ImportPolicyError(f"读取期间文件发生变化，请重新选择：{candidate.name}")
    return b"".join(blocks), digest.hexdigest()


def _visible_character_count(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _split_numbered_lines(
    numbered_lines: Sequence[tuple[int, str]],
    heading_path: tuple[str, ...],
) -> list[tuple[str, int, int, str]]:
    output: list[tuple[str, int, int, str]] = []
    pending: list[tuple[int, str]] = []
    pending_chars = 0

    def anchor(start: int, end: int) -> str:
        base = f"Markdown lines {start}-{end}"
        if heading_path:
            return base + " · " + " / ".join(heading_path)
        return base

    def emit() -> None:
        nonlocal pending, pending_chars
        if not pending:
            return
        text = "\n".join(line for _, line in pending).strip()
        if text:
            start, end = pending[0][0], pending[-1][0]
            output.append((text, start, end, anchor(start, end)))
        pending = []
        pending_chars = 0

    for line_number, raw_line in numbered_lines:
        line = re.sub(r"[\t ]+", " ", raw_line).strip()
        if not line:
            continue
        if len(line) > MAX_CHUNK_CHARS:
            emit()
            for start in range(0, len(line), MAX_CHUNK_CHARS):
                piece = line[start : start + MAX_CHUNK_CHARS].strip()
                if piece:
                    output.append(
                        (piece, line_number, line_number, anchor(line_number, line_number))
                    )
            continue
        projected = pending_chars + (1 if pending else 0) + len(line)
        if pending and projected > MAX_CHUNK_CHARS:
            emit()
        pending.append((line_number, line))
        pending_chars += (1 if len(pending) > 1 else 0) + len(line)
    emit()
    return output


def _markdown_chunks(text: str) -> tuple[str, tuple[ChunkDraft, ...], int]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    hierarchy: list[str] = []
    first_h1: str | None = None
    section_heading: tuple[str, ...] = ()
    section_lines: list[tuple[int, str]] = []
    staged: list[tuple[str, tuple[str, ...], int, int, str]] = []

    def flush() -> None:
        nonlocal section_lines
        for chunk_text, start, end, anchor in _split_numbered_lines(
            section_lines, section_heading
        ):
            staged.append((chunk_text, section_heading, start, end, anchor))
        section_lines = []

    for line_number, raw_line in enumerate(lines, 1):
        match = _HEADING.match(raw_line)
        if match:
            flush()
            level = len(match.group(1))
            heading = _clean_title(match.group(2), "Untitled section")
            hierarchy[level - 1 :] = [heading]
            section_heading = tuple(hierarchy)
            section_lines.append((line_number, heading))
            if level == 1 and first_h1 is None:
                first_h1 = heading
        else:
            section_lines.append((line_number, raw_line))
    flush()

    if _visible_character_count(text) < 1 or not staged:
        raise ImportPolicyError("Markdown 文件没有可建立证据索引的文本。")
    chunks = tuple(
        ChunkDraft(
            ordinal=index,
            text=chunk_text,
            heading_path=heading_path,
            pdf_page_start=None,
            pdf_page_end=None,
            source_line_start=line_start,
            source_line_end=line_end,
            anchor_label=anchor,
        )
        for index, (chunk_text, heading_path, line_start, line_end, anchor) in enumerate(
            staged, 1
        )
    )
    return first_h1 or "", chunks, sum(len(item.text) for item in chunks)


def _split_plain_text(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [(index, line) for index, line in enumerate(normalized.splitlines(), 1)]
    return [item[0] for item in _split_numbered_lines(lines, ())]


def _normalized_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_payload(payload: bytes, suffix: str) -> ParsedDocument:
    parser = _parser_identity(suffix)
    if suffix in {".md", ".markdown"}:
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ImportPolicyError("Markdown 必须是 UTF-8 编码。") from exc
        if "\ufffd" in text:
            raise ImportPolicyError("Markdown 含 U+FFFD 替换字符，已拒绝导入。")
        return ParsedDocument(
            media_type="text/markdown",
            extraction_method="native_utf8_markdown",
            extraction_status="native_text",
            title_hint="",
            units=(_normalized_newlines(text),),
            page_count=None,
            parser=parser,
        )

    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - installation error, not data behavior
        raise ImportPolicyError("缺少 pypdf，无法导入 PDF。") from exc

    try:
        reader = PdfReader(io.BytesIO(payload), strict=False)
        if reader.is_encrypted:
            raise ImportPolicyError("v0.1 暂不导入加密 PDF。")
        metadata = reader.metadata
        metadata_title = metadata.title if metadata else None
        page_texts: list[str] = []
        for page in reader.pages:
            extracted = page.extract_text() or ""
            page_texts.append(
                _normalized_newlines(extracted.replace("\x00", " ")).strip()
            )
    except ImportPolicyError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError, KeyError) as exc:
        raise ImportPolicyError("PDF 无法解析或文件结构无效。") from exc
    except Exception as exc:
        raise ImportPolicyError("PDF 文本层提取失败。") from exc

    if sum(_visible_character_count(text) for text in page_texts) < 10:
        raise ImportPolicyError(
            "PDF 没有可用文本层；v0.1 不会静默执行 OCR。"
        )

    return ParsedDocument(
        media_type="application/pdf",
        extraction_method="pypdf_text_layer",
        extraction_status="text_layer",
        title_hint=_clean_title(metadata_title, ""),
        units=tuple(page_texts),
        page_count=len(page_texts),
        parser=parser,
    )


def _pdf_chunks(page_texts: Sequence[str]) -> tuple[tuple[ChunkDraft, ...], int]:
    staged: list[ChunkDraft] = []
    for page_number, text in enumerate(page_texts, 1):
        for piece in _split_plain_text(text):
            staged.append(
                ChunkDraft(
                    ordinal=len(staged) + 1,
                    text=piece,
                    heading_path=(),
                    pdf_page_start=page_number,
                    pdf_page_end=page_number,
                    source_line_start=None,
                    source_line_end=None,
                    anchor_label=f"PDF page {page_number}",
                )
            )
    if not staged:
        raise ImportPolicyError("PDF 文本层没有生成任何证据片段。")
    chunks = tuple(staged)
    return chunks, sum(len(item.text) for item in chunks)


def _chunk_parsed(
    parsed: ParsedDocument,
) -> tuple[str, tuple[ChunkDraft, ...], int, dict[str, object]]:
    chunker = _chunker_identity(parsed.media_type)
    if parsed.media_type == "text/markdown":
        if len(parsed.units) != 1 or parsed.page_count is not None:
            raise ImportPolicyError("Markdown 解析对象结构无效。")
        title_hint, chunks, extracted_chars = _markdown_chunks(parsed.units[0])
    elif parsed.media_type == "application/pdf":
        if (
            type(parsed.page_count) is not int
            or parsed.page_count < 1
            or parsed.page_count != len(parsed.units)
        ):
            raise ImportPolicyError("PDF 解析对象页数无效。")
        chunks, extracted_chars = _pdf_chunks(parsed.units)
        title_hint = ""
    else:
        raise ImportPolicyError("解析对象的媒体类型不受支持。")
    return title_hint, chunks, extracted_chars, chunker


def _prepared_document(
    *,
    source_name: str,
    suffix: str,
    payload: bytes,
    source_sha256: str,
    parsed: ParsedDocument,
    chunk_title_hint: str,
    chunks: tuple[ChunkDraft, ...],
    extracted_char_count: int,
    chunker: dict[str, object],
) -> PreparedDocument:
    expected_media_type = (
        "application/pdf" if suffix == ".pdf" else "text/markdown"
    )
    if parsed.media_type != expected_media_type:
        raise ImportPolicyError("解析对象与源文件类型不一致。")
    if not chunks or extracted_char_count != sum(len(item.text) for item in chunks):
        raise ImportPolicyError("切块对象的文本数量或字符数无效。")
    if [item.ordinal for item in chunks] != list(range(1, len(chunks) + 1)):
        raise ImportPolicyError("切块对象的序号必须从 1 连续排列。")
    identity_seed = f"{source_name}\0{source_sha256}".encode("utf-8")
    identity = hashlib.sha256(identity_seed).hexdigest()[:24]
    fallback_title = _clean_title(Path(source_name).stem, "Untitled document")
    title = _clean_title(
        chunk_title_hint or parsed.title_hint,
        fallback_title,
    )
    return PreparedDocument(
        source_name=source_name,
        suffix=suffix,
        media_type=parsed.media_type,
        payload=payload,
        source_sha256=source_sha256,
        byte_size=len(payload),
        document_id="doc_" + identity,
        asset_id="asset_" + identity,
        title=title,
        extraction_method=parsed.extraction_method,
        extraction_status=parsed.extraction_status,
        page_count=parsed.page_count,
        extracted_char_count=extracted_char_count,
        chunks=chunks,
        parsed=parsed,
        chunker=chunker,
        chunk_title_hint=chunk_title_hint,
    )


def prepare_document(source: Path, *, source_name: str | None = None) -> PreparedDocument:
    path = Path(source).expanduser()
    if source_name is None:
        source_name = path.name
    if (
        not isinstance(source_name, str)
        or not source_name
        or Path(source_name).name != source_name
        or "/" in source_name
        or "\\" in source_name
        or "\x00" in source_name
    ):
        raise ImportPolicyError("冻结源文件名无效。")
    suffix = Path(source_name).suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ImportPolicyError(
            f"暂不支持 {path.suffix or '无扩展名'}；v0.1 仅接受 Markdown 和 PDF。"
        )
    payload, source_sha256 = _read_regular_file_once(path)
    parsed = _parse_payload(payload, suffix)
    chunk_title_hint, chunks, extracted_chars, chunker = _chunk_parsed(parsed)
    return _prepared_document(
        source_name=source_name,
        suffix=suffix,
        payload=payload,
        source_sha256=source_sha256,
        parsed=parsed,
        chunk_title_hint=chunk_title_hint,
        chunks=chunks,
        extracted_char_count=extracted_chars,
        chunker=chunker,
    )


def prepare_documents(sources: Sequence[Path]) -> tuple[PreparedDocument, ...]:
    if not sources:
        raise ImportPolicyError("请至少选择一个 Markdown 或 PDF 文件。")
    documents = tuple(prepare_document(path) for path in sources)
    ids = [document.document_id for document in documents]
    if len(ids) != len(set(ids)):
        raise ImportPolicyError("同一个源文件不能在一次快照中重复导入。")
    return tuple(sorted(documents, key=lambda item: item.document_id))
