"""Parser selection by sniffed content and extension."""

from __future__ import annotations

from pathlib import PurePath

from app.ingestion.parsers.base import (
    BlockType,
    DocumentBlock,
    DocumentParser,
    ParsedDocument,
)

DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
STRUCTURED_TYPES = {".csv": "text/csv", ".json": "application/json"}


def sniff_mime(filename: str, data: bytes) -> str | None:
    """Return a supported MIME type, checking magic bytes where the format has them."""
    ext = PurePath(filename).suffix.lower()
    if data.startswith(b"%PDF"):
        return DOCUMENT_TYPES[".pdf"] if ext == ".pdf" else None
    if data.startswith(b"PK\x03\x04"):
        return DOCUMENT_TYPES[".docx"] if ext == ".docx" else None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if b"\x00" in data[:4096]:
        return None  # binary content with a text extension
    if ext in {".md", ".markdown", ".txt"}:
        return DOCUMENT_TYPES[ext]
    return STRUCTURED_TYPES.get(ext)


def parser_for(mime_type: str) -> DocumentParser | None:
    if mime_type == DOCUMENT_TYPES[".pdf"]:
        from app.ingestion.parsers.pdf import PdfParser

        return PdfParser()
    if mime_type == DOCUMENT_TYPES[".docx"]:
        from app.ingestion.parsers.docx import DocxParser

        return DocxParser()
    if mime_type == "text/markdown":
        from app.ingestion.parsers.markdown import MarkdownParser

        return MarkdownParser()
    if mime_type == "text/plain":
        from app.ingestion.parsers.text import TextParser

        return TextParser()
    return None


__all__ = [
    "DOCUMENT_TYPES",
    "IMAGE_TYPES",
    "STRUCTURED_TYPES",
    "BlockType",
    "DocumentBlock",
    "DocumentParser",
    "ParsedDocument",
    "parser_for",
    "sniff_mime",
]
