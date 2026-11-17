from typing import Protocol

from app.ingestion.parsers.models import ParsedDocument


class DocumentParser(Protocol):
    def parse(self, data: bytes, filename: str) -> ParsedDocument: ...
