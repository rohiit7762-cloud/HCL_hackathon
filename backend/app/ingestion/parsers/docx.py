from io import BytesIO

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.ingestion.parsers.models import DocumentBlock, ParsedDocument
from app.schemas.evidence import EvidenceLocation


class DOCXParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        doc = Document(BytesIO(data))
        blocks = []
        headings = []
        pi = ti = 0
        for element in doc.element.body.iterchildren():
            if element.tag.endswith("}p"):
                pi += 1
                p = Paragraph(element, doc)
                text = p.text.strip()
                if not text:
                    continue
                style = p.style.name if p.style else ""
                if style.lower().startswith("heading"):
                    try:
                        level = int(style.split()[-1])
                    except ValueError:
                        level = 1
                    headings[:] = headings[: level - 1] + [text]
                    kind = "heading"
                elif "list" in style.lower():
                    kind = "list_item"
                else:
                    kind = "paragraph"
                blocks.append(
                    DocumentBlock(
                        block_type=kind,
                        text=text,
                        heading_path=list(headings),
                        level=level if kind == "heading" else None,
                        location=EvidenceLocation(
                            paragraph_index=pi, section_path=list(headings), label=f"paragraph {pi}"
                        ),
                    )
                )
            elif element.tag.endswith("}tbl"):
                ti += 1
                table = Table(element, doc)
                text = "\n".join(
                    "| " + " | ".join(c.text.strip().replace("\n", " ") for c in r.cells) + " |"
                    for r in table.rows
                )
                blocks.append(
                    DocumentBlock(
                        block_type="table",
                        text=text,
                        heading_path=list(headings),
                        location=EvidenceLocation(section_path=list(headings), label=f"table {ti}"),
                    )
                )
        return ParsedDocument(
            blocks=blocks,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
