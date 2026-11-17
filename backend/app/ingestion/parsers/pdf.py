"""PDF parser (PyMuPDF): text blocks with page numbers, inferred headings, embedded images."""

from __future__ import annotations

import statistics

import pymupdf

from app.ingestion.parsers.base import BlockType, DocumentBlock, HeadingTracker, ParsedDocument

_BOLD_FLAG = 16
_MIN_IMAGE_BYTES = 2_000
_MIN_IMAGE_SIDE = 80
_EXT_MIME = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}


class PdfParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        with pymupdf.open(stream=data, filetype="pdf") as doc:  # type: ignore[no-untyped-call]
            raw_blocks = []  # (page, text, max_size, all_bold)
            sizes: list[float] = []
            for page_number, page in enumerate(doc, start=1):
                for block in page.get_text("dict")["blocks"]:
                    if block.get("type") != 0:
                        continue
                    spans = [
                        s for line in block["lines"] for s in line["spans"] if s["text"].strip()
                    ]
                    if not spans:
                        continue
                    text = "\n".join(
                        "".join(s["text"] for s in line["spans"]).strip() for line in block["lines"]
                    ).strip()
                    max_size = max(s["size"] for s in spans)
                    all_bold = all(s["flags"] & _BOLD_FLAG for s in spans)
                    sizes.extend(s["size"] for s in spans)
                    raw_blocks.append((page_number, text, max_size, all_bold))

            body_size = statistics.median(sizes) if sizes else 11.0
            heading_sizes = sorted(
                {round(s) for _, t, s, _ in raw_blocks if s >= body_size * 1.15}, reverse=True
            )
            headings = HeadingTracker()
            blocks: list[DocumentBlock] = []
            title = (doc.metadata or {}).get("title") or None

            for page_number, text, size, bold in raw_blocks:
                short = len(text) <= 120 and "\n" not in text
                is_heading = short and (size >= body_size * 1.15 or bold)
                if is_heading:
                    level = (
                        heading_sizes.index(round(size)) + 1
                        if round(size) in heading_sizes
                        else len(heading_sizes) + 1
                    )
                    path = headings.push(level, text)
                    title = title or text
                    blocks.append(
                        DocumentBlock(
                            BlockType.HEADING,
                            text,
                            path,
                            level=level,
                            page=page_number,
                            paragraph_index=len(blocks),
                        )
                    )
                else:
                    kind = (
                        BlockType.LIST_ITEM
                        if text[:2] in {"• ", "- ", "* "}
                        else BlockType.PARAGRAPH
                    )
                    blocks.append(
                        DocumentBlock(
                            kind, text, headings.path, page=page_number, paragraph_index=len(blocks)
                        )
                    )

            for page_number, page in enumerate(doc, start=1):
                for image in page.get_images(full=True):
                    extracted = doc.extract_image(image[0])
                    mime = _EXT_MIME.get(str(extracted.get("ext", "")).lower())
                    payload: bytes = extracted.get("image", b"")
                    if (
                        mime is None
                        or len(payload) < _MIN_IMAGE_BYTES
                        or min(extracted.get("width", 0), extracted.get("height", 0))
                        < _MIN_IMAGE_SIDE
                    ):
                        continue
                    blocks.append(
                        DocumentBlock(
                            BlockType.IMAGE,
                            "",
                            [],
                            page=page_number,
                            image_bytes=payload,
                            image_mime=mime,
                        )
                    )
        return ParsedDocument(blocks=blocks, title=title)
