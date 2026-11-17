"""Provider-neutral LLM message types."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRole(StrEnum):
    """Each role maps to a configurable model ID (see Settings.model_for)."""

    PLANNER = "planner"
    REFINER = "refiner"
    EVIDENCE_JUDGE = "evidence_judge"
    ANSWER = "answer"
    GROUNDING_CHECK = "grounding_check"
    SQL_SPEC = "sql_spec"
    VISION = "vision"


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class ImageInput(BaseModel):
    """An authorized image passed to a vision model. Caller must check access first."""

    model_config = ConfigDict(frozen=True)

    data: bytes = Field(repr=False)
    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    source_id: str | None = None  # for logging/provenance only
