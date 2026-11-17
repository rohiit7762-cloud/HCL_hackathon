"""Similarity-based semantic chunking (§8.2).

1. Split text blocks into sentences; lists, tables and code stay atomic.
2. Embed every segment.
3. Start a new chunk when the heading section changes, when a segment's cosine
   similarity to the running chunk centroid drops below the threshold (and the
   chunk already meets the minimum size), or when the maximum size would be exceeded.
4. Merge undersized chunks into their neighbour within the same section.

Output is deterministic for a given input, embedding model and configuration.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.ingestion.parsers.base import BlockType, DocumentBlock, ParsedDocument
from app.schemas.common import Modality
from app.schemas.evidence import EvidenceLocation
from app.schemas.ingestion import ChunkRecord, SourceMetadata
from app.schemas.interfaces import EmbeddingProvider

_ABBREVIATIONS = re.compile(r"\b(e\.g|i\.e|etc|vs|mr|mrs|ms|dr|inc|ltd|no|approx|fig)\.", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“'(\[])")
_PLACEHOLDER = "\u0000"


@dataclass(frozen=True)
class ChunkerConfig:
    similarity_threshold: float = 0.72
    min_tokens: int = 80
    max_tokens: int = 450


@dataclass
class _Segment:
    text: str
    block: DocumentBlock
    tokens: int
    vector: list[float] = field(default_factory=list)


@dataclass
class _Chunk:
    segments: list[_Segment] = field(default_factory=list)
    centroid: list[float] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(s.tokens for s in self.segments)

    @property
    def heading_path(self) -> list[str]:
        return self.segments[0].block.heading_path if self.segments else []

    def add(self, segment: _Segment) -> None:
        n = len(self.segments)
        if not self.centroid:
            self.centroid = list(segment.vector)
        else:
            self.centroid = [
                (c * n + v) / (n + 1) for c, v in zip(self.centroid, segment.vector, strict=True)
            ]
        self.segments.append(segment)


def count_tokens(text: str) -> int:
    """Approximate token count (~1.3 tokens per word); stable and dependency-free."""
    return max(1, math.ceil(len(text.split()) * 1.3))


def split_sentences(text: str) -> list[str]:
    protected = _ABBREVIATIONS.sub(lambda m: m.group(0).replace(".", _PLACEHOLDER), text)
    parts = _SENTENCE_END.split(protected.replace("\n", " "))
    return [p.replace(_PLACEHOLDER, ".").strip() for p in parts if p.strip()]


def embedding_text(chunk: ChunkRecord) -> str:
    """Text that is embedded for a chunk: heading trail + body (body stored separately)."""
    prefix = " > ".join(chunk.heading_path)
    return f"{prefix}\n{chunk.text}" if prefix else chunk.text


def chunk_id(source_id: str, version: int, ordinal: int, modality: Modality) -> str:
    raw = f"{source_id}|{version}|{ordinal}|{modality.value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class SemanticChunker:
    def __init__(self, embeddings: EmbeddingProvider, config: ChunkerConfig) -> None:
        self._embeddings = embeddings
        self._config = config

    async def chunk(self, document: ParsedDocument, source: SourceMetadata) -> list[ChunkRecord]:
        segments = self._segments(document)
        if not segments:
            return []
        vectors = await self._embeddings.embed_documents([s.text for s in segments])
        for segment, vector in zip(segments, vectors, strict=True):
            segment.vector = _normalize(vector)
        chunks = self._merge_small(self._group(segments))
        return [self._record(i, c, source) for i, c in enumerate(chunks)]

    # --- segmentation ----------------------------------------------------
    def _segments(self, document: ParsedDocument) -> list[_Segment]:
        segments: list[_Segment] = []
        for block in document.text_blocks:
            if block.block_type == BlockType.HEADING:
                continue  # headings live in heading_path
            if block.atomic:
                for piece in self._split_atomic(block):
                    segments.append(_Segment(piece, block, count_tokens(piece)))
            else:
                for sentence in split_sentences(block.text):
                    segments.append(_Segment(sentence, block, count_tokens(sentence)))
        return segments

    def _split_atomic(self, block: DocumentBlock) -> list[str]:
        if count_tokens(block.text) <= self._config.max_tokens:
            return [block.text]
        lines = block.text.splitlines()
        header = lines[:2] if block.block_type == BlockType.TABLE and len(lines) > 2 else []
        body = lines[len(header) :]
        pieces: list[str] = []
        current: list[str] = []
        for line in body:
            candidate = [*header, *current, line]
            if current and count_tokens("\n".join(candidate)) > self._config.max_tokens:
                pieces.append("\n".join([*header, *current]))
                current = []
            current.append(line)
        if current:
            pieces.append("\n".join([*header, *current]))
        return pieces

    # --- grouping --------------------------------------------------------
    def _group(self, segments: Sequence[_Segment]) -> list[_Chunk]:
        cfg = self._config
        chunks: list[_Chunk] = []
        current = _Chunk()
        for segment in segments:
            if current.segments:
                section_changed = segment.block.heading_path != current.heading_path
                too_big = current.tokens + segment.tokens > cfg.max_tokens
                topic_shift = (
                    current.tokens >= cfg.min_tokens
                    and _cosine(segment.vector, current.centroid) < cfg.similarity_threshold
                )
                if section_changed or too_big or topic_shift:
                    chunks.append(current)
                    current = _Chunk()
            current.add(segment)
        if current.segments:
            chunks.append(current)
        return chunks

    def _merge_small(self, chunks: list[_Chunk]) -> list[_Chunk]:
        merged: list[_Chunk] = []
        for chunk in chunks:
            previous = merged[-1] if merged else None
            if (
                previous is not None
                and chunk.tokens < self._config.min_tokens
                and previous.heading_path == chunk.heading_path
                and previous.tokens + chunk.tokens <= self._config.max_tokens
            ):
                for segment in chunk.segments:
                    previous.add(segment)
            else:
                merged.append(chunk)
        return merged

    # --- records ---------------------------------------------------------
    def _record(self, ordinal: int, chunk: _Chunk, source: SourceMetadata) -> ChunkRecord:
        text = _join(chunk.segments)
        blocks = [s.block for s in chunk.segments]
        pages = [b.page for b in blocks if b.page is not None]
        line_starts = [b.line_start for b in blocks if b.line_start is not None]
        line_ends = [b.line_end for b in blocks if b.line_end is not None]
        location = EvidenceLocation(
            page=pages[0] if pages else None,
            section_path=chunk.heading_path,
            paragraph_index=blocks[0].paragraph_index,
            line_start=min(line_starts) if line_starts else None,
            line_end=max(line_ends) if line_ends else None,
            label=_label(
                pages[0] if pages else None,
                min(line_starts) if line_starts else None,
                max(line_ends) if line_ends else None,
                chunk.heading_path,
                ordinal,
            ),
        )
        return ChunkRecord(
            chunk_id=chunk_id(source.source_id, source.version, ordinal, Modality.TEXT),
            source_id=source.source_id,
            source_version=source.version,
            tenant_id=source.tenant_id,
            ordinal=ordinal,
            modality=Modality.TEXT,
            text=text,
            heading_path=chunk.heading_path,
            location=location,
            token_count=chunk.tokens,
            classification=source.classification,
            embedding_model=self._embeddings.model_id,
        )


def _join(segments: Sequence[_Segment]) -> str:
    parts: list[str] = []
    previous: DocumentBlock | None = None
    for segment in segments:
        if previous is segment.block and not segment.block.atomic:
            parts[-1] = f"{parts[-1]} {segment.text}"
        else:
            parts.append(segment.text)
        previous = segment.block
    return "\n\n".join(parts)


def _label(
    page: int | None, start: int | None, end: int | None, path: list[str], ordinal: int
) -> str:
    where = (
        f"p. {page}"
        if page is not None
        else (f"lines {start}-{end}" if start is not None else f"part {ordinal + 1}")
    )
    trail = " > ".join(path[-2:])
    return f"{where} · {trail}" if trail else where


def _normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    norm_b = math.sqrt(sum(v * v for v in b)) or 1.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm_b  # `a` is unit length
