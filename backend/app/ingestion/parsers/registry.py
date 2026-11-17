from mimetypes import guess_type
from pathlib import Path

from app.ingestion.parsers.base import DocumentParser
from app.ingestion.parsers.models import ParsedDocument


class ParserRegistry:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes

    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        if not data or len(data) > self.max_bytes:
            raise ValueError("File is empty or exceeds the configured ingestion limit")
        ext = Path(filename).suffix.lower()
        mime = guess_type(filename)[0] or "application/octet-stream"
        if ext == ".pdf":
            from app.ingestion.parsers.pdf import PDFParser

            parser: DocumentParser = PDFParser()
        elif ext == ".docx":
            from app.ingestion.parsers.docx import DOCXParser

            parser = DOCXParser()
        elif ext in {".md", ".markdown"}:
            from app.ingestion.parsers.markdown import MarkdownParser

            parser = MarkdownParser()
        elif ext == ".txt":
            from app.ingestion.parsers.text import TextParser

            parser = TextParser()
        else:
            raise ValueError("Unsupported document type")
        parsed = parser.parse(data, filename)
        return parsed.model_copy(update={"mime_type": mime})
