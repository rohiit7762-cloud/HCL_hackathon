from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.evidence import EvidenceLocation


class DocumentBlock(BaseModel):
    block_type: Literal["heading", "paragraph", "list_item", "table", "code", "image"]
    text: str
    heading_path: list[str] = Field(default_factory=list)
    level: int | None = None
    location: EvidenceLocation
    image_bytes: bytes | None = None
    alt_text: str | None = None


class ParsedDocument(BaseModel):
    blocks: list[DocumentBlock]
    mime_type: str
    warnings: list[str] = Field(default_factory=list)
