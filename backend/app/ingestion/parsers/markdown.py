"""Markdown parser using markdown-it token line maps (headings, lists, tables, code)."""

from __future__ import annotations

from markdown_it import MarkdownIt

from app.ingestion.parsers.base import BlockType, DocumentBlock, HeadingTracker, ParsedDocument
from app.ingestion.parsers.text import decode_text

_BLOCK_TYPES = {
    "paragraph_open": BlockType.PARAGRAPH,
    "table_open": BlockType.TABLE,
    "fence": BlockType.CODE,
    "code_block": BlockType.CODE,
    "blockquote_open": BlockType.PARAGRAPH,
}


class MarkdownParser:
    def __init__(self) -> None:
        self._md = MarkdownIt("commonmark").enable("table")

    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        source = decode_text(data)
        lines = source.splitlines()
        tokens = self._md.parse(source)
        headings = HeadingTracker()
        blocks: list[DocumentBlock] = []
        title: str | None = None

        def add(kind: BlockType, start: int, end: int, text: str | None = None) -> None:
            body = text if text is not None else "\n".join(lines[start:end]).strip()
            if body:
                blocks.append(
                    DocumentBlock(
                        block_type=kind,
                        text=body,
                        heading_path=headings.path,
                        line_start=start + 1,
                        line_end=end,
                        paragraph_index=len(blocks),
                    )
                )

        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.level != 0 or token.map is None:
                index += 1
                continue
            start, end = token.map
            if token.type == "heading_open":
                level = int(token.tag[1])
                text = tokens[index + 1].content.strip()
                path = headings.push(level, text)
                title = title or text
                blocks.append(
                    DocumentBlock(
                        block_type=BlockType.HEADING,
                        text=text,
                        heading_path=path,
                        level=level,
                        line_start=start + 1,
                        line_end=end,
                        paragraph_index=len(blocks),
                    )
                )
            elif token.type in {"bullet_list_open", "ordered_list_open"}:
                index = self._add_list_items(tokens, index, lines, add)
                continue
            elif token.type in _BLOCK_TYPES:
                add(_BLOCK_TYPES[token.type], start, end)
            index += 1
        return ParsedDocument(blocks=blocks, title=title)

    @staticmethod
    def _add_list_items(  # type: ignore[no-untyped-def]
        tokens, index: int, lines, add
    ) -> int:
        """Emit one LIST_ITEM per top-level item; return index after the list."""
        depth = 0
        while index < len(tokens):
            token = tokens[index]
            if token.type.endswith("_list_open"):
                depth += 1
            elif token.type.endswith("_list_close"):
                depth -= 1
                if depth == 0:
                    return index + 1
            elif token.type == "list_item_open" and depth == 1 and token.map:
                add(BlockType.LIST_ITEM, token.map[0], token.map[1])
            index += 1
        return index
