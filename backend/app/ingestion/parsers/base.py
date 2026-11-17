"""Common document model produced by every parser (§8.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class BlockType(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    CODE = "code"
    IMAGE = "image"


@dataclass
class DocumentBlock:
    block_type: BlockType
    text: str
    heading_path: list[str] = field(default_factory=list)
    level: int = 0  # heading level (1 = top) for HEADING blocks
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    paragraph_index: int | None = None
    image_bytes: bytes | None = None
    image_mime: str | None = None

    @property
    def atomic(self) -> bool:
        """Blocks never split by sentence (lists, tables, code)."""
        return self.block_type in {BlockType.LIST_ITEM, BlockType.TABLE, BlockType.CODE}


@dataclass
class ParsedDocument:
    blocks: list[DocumentBlock]
    title: str | None = None

    @property
    def text_blocks(self) -> list[DocumentBlock]:
        return [b for b in self.blocks if b.block_type != BlockType.IMAGE and b.text.strip()]

    @property
    def image_blocks(self) -> list[DocumentBlock]:
        return [b for b in self.blocks if b.block_type == BlockType.IMAGE and b.image_bytes]


class DocumentParser(Protocol):
    def parse(self, data: bytes, filename: str) -> ParsedDocument: ...


class HeadingTracker:
    """Maintains the current heading trail while walking blocks in order."""

    def __init__(self) -> None:
        self._stack: list[tuple[int, str]] = []

    def push(self, level: int, text: str) -> list[str]:
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, text.strip()))
        return self.path

    @property
    def path(self) -> list[str]:
        return [text for _, text in self._stack]
