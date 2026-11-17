from app.ingestion.parsers.models import DocumentBlock, ParsedDocument
from app.schemas.evidence import EvidenceLocation


class MarkdownParser:
    def parse(self, data: bytes, filename: str) -> ParsedDocument:
        try:
            from markdown_it import MarkdownIt
        except ImportError as exc:
            raise RuntimeError("markdown-it-py is required for Markdown ingestion") from exc
        text = data.decode("utf-8-sig")
        tokens = MarkdownIt("commonmark").enable("table").parse(text)
        blocks, headings, i = [], [], 0
        while i < len(tokens):
            token = tokens[i]
            if token.type == "heading_open":
                inline = tokens[i + 1]
                level = int(token.tag[1:])
                headings[:] = headings[: level - 1] + [inline.content]
                line = token.map[0] + 1 if token.map else 1
                blocks.append(
                    DocumentBlock(
                        block_type="heading",
                        text=inline.content,
                        heading_path=list(headings),
                        level=level,
                        location=EvidenceLocation(line_start=line, label=" > ".join(headings)),
                    )
                )
                i += 3
                continue
            if token.type == "fence":
                line = token.map[0] + 1 if token.map else 1
                blocks.append(
                    DocumentBlock(
                        block_type="code",
                        text=token.content,
                        heading_path=list(headings),
                        location=EvidenceLocation(
                            line_start=line,
                            line_end=token.map[1] if token.map else None,
                            label=f"code block, line {line}",
                        ),
                    )
                )
            elif token.type == "inline" and token.content.strip():
                line = token.map[0] + 1 if token.map else 1
                blocks.append(
                    DocumentBlock(
                        block_type="paragraph",
                        text=token.content,
                        heading_path=list(headings),
                        location=EvidenceLocation(line_start=line, label=f"line {line}"),
                    )
                )
            i += 1
        return ParsedDocument(blocks=blocks, mime_type="text/markdown")
