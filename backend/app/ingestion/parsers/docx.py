"""DOCX parser: paragraphs (style-based headings and lists) and tables in body order."""

from __future__ import annotations

import io
import re

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.ingestion.parsers.base import BlockType, DocumentBlock, HeadingTracker, ParsedDocument

_HEADING_STYLE = re.compile(r"^(heading|title)\s*(\d*)$", re.I)
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}


class DocxParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        document = Document(io.BytesIO(data))
        headings = HeadingTracker()
        blocks: list[DocumentBlock] = []
        title: str | None = document.core_properties.title or None

        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                paragraph = Paragraph(child, document)
                text = paragraph.text.strip()
                if not text:
                    continue
                style = (paragraph.style.name if paragraph.style is not None else "") or ""
                match = _HEADING_STYLE.match(style.strip())
                if match:
                    level = int(match.group(2) or 1) if match.group(1).lower() == "heading" else 1
                    path = headings.push(level, text)
                    title = title or text
                    blocks.append(
                        DocumentBlock(
                            BlockType.HEADING, text, path, level=level, paragraph_index=len(blocks)
                        )
                    )
                else:
                    kind = BlockType.LIST_ITEM if "list" in style.lower() else BlockType.PARAGRAPH
                    blocks.append(
                        DocumentBlock(kind, text, headings.path, paragraph_index=len(blocks))
                    )
            elif tag == "tbl":
                markdown = _table_markdown(Table(child, document))
                if markdown:
                    blocks.append(
                        DocumentBlock(
                            BlockType.TABLE, markdown, headings.path, paragraph_index=len(blocks)
                        )
                    )

        for part in document.part.related_parts.values():
            content_type = getattr(part, "content_type", "")
            if content_type in _IMAGE_TYPES:
                blocks.append(
                    DocumentBlock(
                        BlockType.IMAGE, "", [], image_bytes=part.blob, image_mime=content_type
                    )
                )
        return ParsedDocument(blocks=blocks, title=title)


def _table_markdown(table: Table) -> str:
    rows = [[cell.text.strip().replace("|", "/") for cell in row.cells] for row in table.rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)
