"""Helpers for placing untrusted content inside prompts (invariant I9)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.schemas.evidence import Evidence

_TAG_BREAKOUT = re.compile(r"</?\s*(evidence|document|ocr|record|user_query|system)\b", re.I)


def neutralize(text: str) -> str:
    """Stop untrusted text from opening or closing our delimiter tags."""
    return _TAG_BREAKOUT.sub(lambda m: m.group(0).replace("<", "&lt;"), text)


def wrap_user_query(text: str) -> str:
    return f"<user_query>\n{neutralize(text)}\n</user_query>"


def render_evidence(items: Iterable[Evidence]) -> str:
    blocks = [
        f'<evidence id="{e.evidence_id}" source="{neutralize(e.source_title)}" '
        f'kind="{e.kind.value}" location="{neutralize(e.location.label)}" untrusted="true">\n'
        f"{neutralize(e.excerpt)}\n</evidence>"
        for e in items
    ]
    return "\n".join(blocks) if blocks else "(no evidence was found)"
