from app.ingestion.parsers.models import DocumentBlock, ParsedDocument
from app.schemas.evidence import EvidenceLocation


class PDFParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        blocks = []
        for page_index, page in enumerate(doc):
            raw = page.get_text("dict")
            spans = [
                (b, line, span)
                for b in raw["blocks"]
                if "lines" in b
                for line in b["lines"]
                for span in line["spans"]
            ]
            sizes = [s[2]["size"] for s in spans if s[2]["text"].strip()]
            median = sorted(sizes)[len(sizes) // 2] if sizes else 999
            for bi, block in enumerate(raw["blocks"]):
                if "lines" in block:
                    words = []
                    for line in block["lines"]:
                        words.extend(
                            (span["text"], span["size"], span["font"]) for span in line["spans"]
                        )
                    text = " ".join(part[0] for part in words).strip()
                    if not text:
                        continue
                    is_heading = max((part[1] for part in words), default=0) >= median * 1.25
                    label = f"page {page_index + 1}, block {bi + 1}"
                    blocks.append(
                        DocumentBlock(
                            block_type="heading" if is_heading else "paragraph",
                            text=text,
                            location=EvidenceLocation(page=page_index + 1, label=label),
                        )
                    )
                elif "image" in block:
                    blocks.append(
                        DocumentBlock(
                            block_type="image",
                            text="",
                            image_bytes=block["image"],
                            location=EvidenceLocation(
                                page=page_index + 1, label=f"page {page_index + 1}, image {bi + 1}"
                            ),
                        )
                    )
        return ParsedDocument(blocks=blocks, mime_type="application/pdf")
