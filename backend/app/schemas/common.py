"""Enums shared by every layer. This module imports nothing from the app."""

from __future__ import annotations

from enum import StrEnum


class Classification(StrEnum):
    """Source sensitivity. Ordered: public < internal < confidential < restricted."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    @property
    def rank(self) -> int:
        return _CLASSIFICATION_ORDER.index(self)

    def allows(self, other: Classification) -> bool:
        """True when a clearance of `self` may see content classified `other`."""
        return other.rank <= self.rank


_CLASSIFICATION_ORDER: tuple[Classification, ...] = (
    Classification.PUBLIC,
    Classification.INTERNAL,
    Classification.CONFIDENTIAL,
    Classification.RESTRICTED,
)


class DataCategory(StrEnum):
    GENERAL_KNOWLEDGE = "general_knowledge"  # docs, runbooks, policies, architecture
    OPERATIONAL = "operational"  # tickets, incidents, services, on-call
    EMPLOYEE_DIRECTORY = "employee_directory"  # name, title, dept, work email, expertise, manager
    EMPLOYEE_PERSONAL = "employee_personal"  # comp, rating, leave, health, address, national ID
    CONFIDENTIAL_BUSINESS = "confidential_business"  # M&A, unreleased financials, legal, strategy


class SourceKind(StrEnum):
    DOCUMENT = "document"
    IMAGE = "image"
    STRUCTURED = "structured"
    USER_UPLOAD = "user_upload"


class Modality(StrEnum):
    TEXT = "text"
    IMAGE_CAPTION = "image_caption"
    IMAGE_OCR = "image_ocr"


class SourceNeed(StrEnum):
    DOCUMENTS = "documents"
    IMAGES = "images"
    STRUCTURED = "structured"
