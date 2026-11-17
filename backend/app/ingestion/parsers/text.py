import re

from app.ingestion.parsers.models import DocumentBlock, ParsedDocument
from app.schemas.evidence import EvidenceLocation


class TextParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        text = data.decode("utf-8-sig")
        blocks = []
        for match in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text):
            value = match.group().strip()
            if value:
                start = text.count("\n", 0, match.start()) + 1
                end = start + value.count("\n")
                blocks.append(
                    DocumentBlock(
                        block_type="paragraph",
                        text=value,
                        location=EvidenceLocation(
                            line_start=start, line_end=end, label=f"lines {start}-{end}"
                        ),
                    )
                )
        return ParsedDocument(blocks=blocks, mime_type="text/plain")
