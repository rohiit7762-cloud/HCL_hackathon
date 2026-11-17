"""Plain-text parser: paragraphs separated by blank lines, with line ranges."""

from __future__ import annotations

from app.ingestion.parsers.base import BlockType, DocumentBlock, ParsedDocument


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class TextParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        lines = decode_text(data).splitlines()
        blocks: list[DocumentBlock] = []
        buffer: list[str] = []
        start = 0
        for number, line in enumerate([*lines, ""], start=1):
            if line.strip():
                if not buffer:
                    start = number
                buffer.append(line.rstrip())
                continue
            if buffer:
                blocks.append(
                    DocumentBlock(
                        block_type=BlockType.PARAGRAPH,
                        text="\n".join(buffer),
                        line_start=start,
                        line_end=number - 1,
                        paragraph_index=len(blocks),
                    )
                )
                buffer = []
        title = blocks[0].text.splitlines()[0][:200] if blocks else None
        return ParsedDocument(blocks=blocks, title=title)
