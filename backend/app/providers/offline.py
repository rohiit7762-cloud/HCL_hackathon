"""LLM stand-in for offline tooling (e.g. seeding without a Groq key).

Every call fails with ProviderUnavailableError, so callers take their documented
fallbacks (derived image captions instead of vision captions).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel

from app.core.errors import CapabilityLimitError, ProviderUnavailableError
from app.schemas.llm import ChatMessage, ImageInput, ModelRole

T = TypeVar("T", bound=BaseModel)


class OfflineLLMProvider:
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        role: ModelRole,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        raise ProviderUnavailableError()

    async def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        max_tokens: int | None = None,
    ) -> T:
        raise ProviderUnavailableError()

    async def describe_image(self, image: ImageInput, instruction: str) -> str:
        raise CapabilityLimitError()

    def supports_vision(self) -> bool:
        return False
