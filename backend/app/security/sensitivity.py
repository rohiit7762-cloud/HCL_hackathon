"""Deterministic sensitivity detection (no LLM).

`detect` classifies user text into data categories and normalized field names; it
backs up the planner LLM so a misclassification can never grant access (I2).
`scan_output` finds PII-like spans in generated answers for the final validator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.common import DataCategory
from app.schemas.security import SensitiveSpan, SensitivityFindings


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern[str]
    category: DataCategory
    field: str


def _rule(pattern: str, category: DataCategory, field: str) -> _Rule:
    return _Rule(re.compile(pattern, re.IGNORECASE), category, field)


_P = DataCategory.EMPLOYEE_PERSONAL
_C = DataCategory.CONFIDENTIAL_BUSINESS

_DETECT_RULES: tuple[_Rule, ...] = (
    _rule(
        r"\b(salar(y|ies)|compensation|pay\s?(check|slip|grade|band)?|wages?|earns?|"
        r"how much (does|do|is) .{1,40} (make|paid|earn))\b",
        _P,
        "salary",
    ),
    _rule(r"\bbonus(es)?\b|\bstock (grant|options?)\b|\bequity\b", _P, "bonus"),
    _rule(r"\b(performance (rating|review|score)s?|appraisal|rating)\b", _P, "performance_rating"),
    _rule(
        r"\b(home|personal) (address|phone|number|email)\b|\bwhere does .{1,30} live\b",
        _P,
        "personal_phone",
    ),
    _rule(r"\b(home city|lives? in)\b", _P, "home_city"),
    _rule(r"\b(medical|health|sick) (leave|condition|record)s?\b|\bdiagnos", _P, "health"),
    _rule(
        r"\b(ssn|social security|national id|passport( number)?|aadhaar|pan number)\b",
        _P,
        "national_id",
    ),
    _rule(
        r"\b(disciplinary|warning letter|pip|performance improvement plan|termination)\b",
        _P,
        "disciplinary",
    ),
    _rule(
        r"\b(acquisition|acquire|merger|m&a|due diligence|term sheet|valuation|"
        r"buyout|takeover|layoffs?|restructuring)\b",
        _C,
        "confidential_business",
    ),
    _rule(
        r"\b(unreleased|unannounced|confidential) (financials?|results|earnings|revenue)\b",
        _C,
        "confidential_business",
    ),
)

_OUTPUT_RULES: tuple[tuple[re.Pattern[str], str, DataCategory], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "email", DataCategory.EMPLOYEE_DIRECTORY),
    (re.compile(r"(?<!\w)(\+?\d[\d\s().-]{7,}\d)(?!\w)"), "phone", _P),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "national_id", _P),
    (
        re.compile(
            r"(?:[$€£₹]\s?\d[\d,]*(?:\.\d+)?\s?(?:k|K|m|M)?|\b\d[\d,]{3,}\s?(?:USD|INR|EUR))"
        ),
        "compensation",
        _P,
    ),
    (
        re.compile(r"\b(exceeds|meets|below) expectations\b|\brating (of )?[1-5](/5)?\b", re.I),
        "performance_rating",
        _P,
    ),
)


class RegexSensitivityDetector:
    def detect(self, text: str) -> SensitivityFindings:
        categories: set[DataCategory] = set()
        fields: set[str] = set()
        spans: list[tuple[int, int, DataCategory]] = []
        for rule in _DETECT_RULES:
            for match in rule.pattern.finditer(text):
                categories.add(rule.category)
                fields.add(rule.field)
                spans.append((match.start(), match.end(), rule.category))
        return SensitivityFindings(
            categories=frozenset(categories), fields=frozenset(fields), spans=tuple(spans)
        )

    def scan_output(self, text: str) -> list[SensitiveSpan]:
        found: list[SensitiveSpan] = []
        for pattern, kind, category in _OUTPUT_RULES:
            for match in pattern.finditer(text):
                if kind == "phone" and _looks_like_date_or_id(match.group(0)):
                    continue
                found.append(
                    SensitiveSpan(
                        start=match.start(),
                        end=match.end(),
                        kind=kind,
                        text=match.group(0),
                        category=category,
                    )
                )
        return _drop_overlaps(found)


def _looks_like_date_or_id(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip())) or len(digits) < 9


def _drop_overlaps(spans: list[SensitiveSpan]) -> list[SensitiveSpan]:
    kept: list[SensitiveSpan] = []
    for span in sorted(spans, key=lambda s: (s.start, -(s.end - s.start))):
        if kept and span.start < kept[-1].end:
            continue
        kept.append(span)
    return kept
