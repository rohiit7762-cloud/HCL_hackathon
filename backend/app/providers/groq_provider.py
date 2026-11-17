from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from groq import AsyncGroq, APIConnectionError, APITimeoutError, RateLimitError, InternalServerError

from app.core.config import Settings
from app.core.errors import (
    CapabilityLimitError,
    ProviderUnavailableError,
    RateLimitedError,
    ValidationFailedError,
)
from app.schemas.llm import ChatMessage, ImageInput, ModelRole

T = TypeVar("T", bound=BaseModel)


class GroqProvider:
    """Groq adapter with bounded infrastructure retries and Pydantic output validation."""

    def __init__(self, settings: Settings):
        key = settings.groq_api_key.get_secret_value()
        if not key:
            raise RuntimeError("GROQ_API_KEY is required for live mode")
        self.settings = settings
        self.client = AsyncGroq(api_key=key, timeout=settings.groq_timeout_seconds, max_retries=0)
        self._semaphore = asyncio.Semaphore(settings.groq_max_concurrency)

    async def _call(
        self,
        role: ModelRole,
        messages: list[dict],
        temperature: float,
        max_tokens: int | None,
        response_format=None,
    ):
        for attempt in range(self.settings.groq_max_infra_retries):
            try:
                async with self._semaphore:
                    return await self.client.chat.completions.create(
                        model=self.settings.model_for(role),
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                    )
            except RateLimitError as exc:
                if attempt + 1 == self.settings.groq_max_infra_retries:
                    raise RateLimitedError() from exc
                await asyncio.sleep(min(2**attempt, 8))
            except (APIConnectionError, APITimeoutError, InternalServerError) as exc:
                if attempt + 1 == self.settings.groq_max_infra_retries:
                    raise ProviderUnavailableError() from exc
                await asyncio.sleep(min(2**attempt, 8))

    @staticmethod
    def _messages(messages: Sequence[ChatMessage]) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in messages]

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        role: ModelRole,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        response = await self._call(role, self._messages(messages), temperature, max_tokens)
        return response.choices[0].message.content or ""

    async def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        max_tokens: int | None = None,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema())
        prompt = [
            {
                "role": "system",
                "content": "Return exactly one JSON object matching this schema. Do not add prose. Schema: "
                + schema_json,
            },
            *self._messages(messages),
        ]
        response = await self._call(role, prompt, 0, max_tokens, {"type": "json_object"})
        raw = response.choices[0].message.content or "{}"
        try:
            return schema.model_validate_json(raw)
        except ValidationError as exc:
            raise ValidationFailedError() from exc

    async def describe_image(self, image: ImageInput, instruction: str) -> str:
        if not self.supports_vision():
            raise CapabilityLimitError()
        payload = base64.b64encode(image.data).decode("ascii")
        content = [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{payload}"}},
        ]
        response = await self._call(
            ModelRole.VISION, [{"role": "user", "content": content}], 0, 500
        )
        return response.choices[0].message.content or ""

    def supports_vision(self) -> bool:
        return self.settings.groq_vision_enabled and bool(self.settings.groq_model_vision)
