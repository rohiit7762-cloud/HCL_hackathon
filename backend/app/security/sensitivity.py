from __future__ import annotations

import re

from app.schemas.common import DataCategory
from app.schemas.security import SensitiveSpan, SensitivityFindings


class DeterministicSensitivityDetector:
    _patterns = {
        "salary": r"\b(salary|compensation|pay|bonus)\b",
        "performance_rating": r"\b(performance rating|rating|performance review)\b",
        "personal_phone": r"\b(personal phone|phone number|mobile number)\b",
        "health": r"\b(health|medical|diagnosis)\b",
        "national_id": r"\b(ssn|national id|social security)\b",
    }

    def detect(self, text: str) -> SensitivityFindings:
        fields = set()
        spans = []
        for field, pattern in self._patterns.items():
            for m in re.finditer(pattern, text, re.I):
                fields.add(field)
                spans.append((m.start(), m.end(), DataCategory.EMPLOYEE_PERSONAL))
        categories = set()
        if re.search(
            r"\b(salary|compensation|pay|bonus|performance rating|personal phone|health|medical|ssn)\b",
            text,
            re.I,
        ):
            categories.add(DataCategory.EMPLOYEE_PERSONAL)
        if re.search(
            r"\b(acquisition|merger|project falcon|unreleased financial|m&a)\b", text, re.I
        ):
            categories.add(DataCategory.CONFIDENTIAL_BUSINESS)
        if re.search(r"\b(incident|ticket|outage|service|on-call)\b", text, re.I):
            categories.add(DataCategory.OPERATIONAL)
        return SensitivityFindings(
            categories=frozenset(categories), fields=frozenset(fields), spans=tuple(spans)
        )

    def scan_output(self, text: str) -> list[SensitiveSpan]:
        patterns = {
            "email": r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
            "phone": r"\b(?:\+?\d[\d ()-]{7,}\d)\b",
            "national_id": r"\b\d{3}-\d{2}-\d{4}\b",
        }
        return [
            SensitiveSpan(
                start=m.start(),
                end=m.end(),
                kind=kind,
                text=m.group(),
                category=DataCategory.EMPLOYEE_PERSONAL,
            )
            for kind, p in patterns.items()
            for m in re.finditer(p, text)
        ]
