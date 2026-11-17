from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from app.ingestion.parsers.models import DocumentBlock, ParsedDocument
from app.schemas.common import Modality
from app.schemas.ingestion import ChunkRecord, SourceMetadata
from app.schemas.interfaces import EmbeddingProvider


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text) if s.strip()]


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    d = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=False)) / d if d else 0.0


class SemanticChunker:
    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        threshold: float = 0.72,
        min_tokens: int = 80,
        max_tokens: int = 450,
    ) -> None:
        if not 0 <= threshold <= 1 or min_tokens < 1 or max_tokens < min_tokens:
            raise ValueError("Invalid chunking settings")
        self.embedder, self.threshold, self.minimum, self.maximum = (
            embedding_provider,
            threshold,
            min_tokens,
            max_tokens,
        )

    async def chunk(self, doc: ParsedDocument, source: SourceMetadata) -> list[ChunkRecord]:
        segments: list[tuple[DocumentBlock, str]] = []
        for block in doc.blocks:
            if block.block_type in {"heading", "table", "list_item", "code", "image"}:
                if block.block_type != "image" and block.text:
                    segments.extend(self._atomic(block))
                continue
            for sentence in _sentences(block.text):
                segments.append((block, sentence))
        if not segments:
            return []
        heading_prefix = lambda b: " > ".join(b.heading_path)
        embedded = [
            ((heading_prefix(b) + "\n" if heading_prefix(b) else "") + t) for b, t in segments
        ]
        vectors = await self.embedder.embed_documents(embedded)
        chunks = []
        group = []
        group_vec = []
        count = 0
        section = []
        for i, (block, text) in enumerate(segments):
            size = len(_tokens(text))
            boundary = False
            if group:
                if (
                    block.block_type == "heading"
                    or block.heading_path != section
                    or (
                        block.block_type in {"table", "list_item", "code"}
                        and count + size > self.maximum
                    )
                    or count + size > self.maximum
                    or (
                        count >= self.minimum
                        and _cos(
                            vectors[i],
                            [
                                sum(v[j] for v in group_vec) / len(group_vec)
                                for j in range(len(vectors[i]))
                            ],
                        )
                        < self.threshold
                    )
                ):
                    boundary = True
            if boundary:
                chunks.append((group, group_vec))
                group = []
                group_vec = []
                count = 0
            if size > self.maximum:
                # Large tables split only on row boundaries; ordinary blocks split on sentence/token boundaries.
                for frag in self._split_large(block, text):
                    chunks.append(([(block, frag)], []))
                section = block.heading_path
                continue
            group.append((block, text))
            group_vec.append(vectors[i])
            count += size
            section = block.heading_path
        if group:
            chunks.append((group, group_vec))
        merged = []
        for entry in chunks:
            if (
                merged
                and sum(len(_tokens(t)) for _, t in merged[-1][0]) < self.minimum
                and entry[0][0].heading_path == merged[-1][0][0].heading_path
                and sum(len(_tokens(t)) for _, t in merged[-1][0] + entry[0]) <= self.maximum
            ):
                merged[-1] = (merged[-1][0] + entry[0], merged[-1][1] + entry[1])
            else:
                merged.append(entry)
        records = []
        for ordinal, (items, _) in enumerate(merged):
            block = items[0][0]
            clean = "\n".join(t for _, t in items)
            heading = block.heading_path
            loc = block.location.model_copy(
                update={"section_path": heading, "label": block.location.label}
            )
            modality = Modality.IMAGE_OCR if block.block_type == "image" else Modality.TEXT
            records.append(
                ChunkRecord(
                    chunk_id=hashlib.sha256(
                        f"{source.source_id}:{source.version}:{ordinal}:{modality}".encode()
                    ).hexdigest(),
                    source_id=source.source_id,
                    source_version=source.version,
                    tenant_id=source.tenant_id,
                    ordinal=ordinal,
                    modality=modality,
                    text=clean,
                    heading_path=heading,
                    location=loc,
                    token_count=len(_tokens(clean)),
                    classification=source.classification,
                    embedding_model=self.embedder.model_id,
                )
            )
        return records

    def _atomic(self, block: DocumentBlock):
        return [(block, block.text)]

    def _split_large(self, block: DocumentBlock, text: str) -> list[str]:
        parts = text.splitlines() if block.block_type == "table" else _sentences(text)
        out = []
        current = []
        count = 0
        for part in parts:
            n = len(_tokens(part))
            if current and count + n > self.maximum:
                out.append("\n".join(current))
                current = []
                count = 0
            if n > self.maximum:
                toks = _tokens(part)
                out.extend(
                    " ".join(toks[i : i + self.maximum]) for i in range(0, len(toks), self.maximum)
                )
            else:
                current.append(part)
                count += n
        if current:
            out.append("\n".join(current))
        return out
