"""Groq implementation of LLMProvider (§7.1).

Infrastructure retries (rate limits, timeouts, 5xx) live here and are completely
separate from the evidence-retrieval retry loop in the orchestrator.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Sequence
from typing import Any, TypeVar

import groq
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import Settings
from app.core.errors import (
    CapabilityLimitError,
    ProviderUnavailableError,
    RateLimitedError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.schemas.llm import ChatMessage, ImageInput, ModelRole

T = TypeVar("T", bound=BaseModel)

logger = get_logger(__name__)

_RETRYABLE = (
    groq.RateLimitError,
    groq.APITimeoutError,
    groq.APIConnectionError,
    groq.InternalServerError,
)
_MAX_RETRY_AFTER_SECONDS = 20.0
_backoff = wait_exponential_jitter(multiplier=0.5, max=8.0)


def _wait(retry_state: RetryCallState) -> float:
    """Honor Retry-After on 429s; otherwise exponential backoff with jitter."""
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome else None
    if isinstance(exc, groq.RateLimitError):
        header = exc.response.headers.get("retry-after") if exc.response is not None else None
        try:
            if header is not None:
                return min(float(header), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return _backoff(retry_state)


def _structured_system_suffix(schema: type[BaseModel]) -> str:
    return (
        "\n\nRespond with a single JSON object only, no prose and no code fences. "
        "It must validate against this JSON Schema:\n"
        + json.dumps(schema.model_json_schema(), separators=(",", ":"))
    )


class GroqLLMProvider:
    """LLMProvider backed by the Groq chat-completions API."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or groq.AsyncGroq(
            api_key=settings.groq_api_key.get_secret_value() or None,
            timeout=settings.groq_timeout_seconds,
            max_retries=0,  # retries are handled here, with logging and Retry-After
        )
        self._semaphore = asyncio.Semaphore(settings.groq_max_concurrency)

    # --- LLMProvider -----------------------------------------------------
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        role: ModelRole,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        payload = [{"role": m.role, "content": m.content} for m in messages]
        return await self._chat(payload, role=role, temperature=temperature, max_tokens=max_tokens)

    async def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        max_tokens: int | None = None,
    ) -> T:
        payload = self._with_schema_instructions(messages, schema)
        raw = await self._chat(payload, role=role, max_tokens=max_tokens, json_mode=True)
        try:
            return schema.model_validate_json(_strip_fences(raw))
        except ValidationError as first_error:
            logger.info(
                "llm_structured_invalid",
                extra={"role": role.value, "error_count": first_error.error_count()},
            )
            repair = [
                *payload,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "Your JSON did not validate. Errors:\n"
                    + _summarize_errors(first_error)
                    + "\nReturn a corrected JSON object only.",
                },
            ]
            raw = await self._chat(repair, role=role, max_tokens=max_tokens, json_mode=True)
            try:
                return schema.model_validate_json(_strip_fences(raw))
            except ValidationError as exc:
                raise ValidationFailedError("The model returned an invalid response.") from exc

    async def describe_image(self, image: ImageInput, instruction: str) -> str:
        if not self.supports_vision():
            raise CapabilityLimitError("Image interpretation is not available.")
        data_url = f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}"
        payload = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        return await self._chat(payload, role=ModelRole.VISION)

    def supports_vision(self) -> bool:
        return self._settings.groq_vision_enabled and bool(self._settings.groq_model_vision)

    # --- internals -------------------------------------------------------
    @staticmethod
    def _with_schema_instructions(
        messages: Sequence[ChatMessage], schema: type[BaseModel]
    ) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = [{"role": m.role, "content": m.content} for m in messages]
        suffix = _structured_system_suffix(schema)
        if payload and payload[0]["role"] == "system":
            payload[0] = {"role": "system", "content": payload[0]["content"] + suffix}
        else:
            payload.insert(0, {"role": "system", "content": suffix.strip()})
        return payload

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        role: ModelRole,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        model = self._settings.model_for(role)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        attempts = 0
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(_RETRYABLE),
                stop=stop_after_attempt(self._settings.groq_max_infra_retries),
                wait=_wait,
                reraise=True,
            ):
                with attempt:
                    attempts += 1
                    async with self._semaphore:
                        response = await self._client.chat.completions.create(**kwargs)
        except groq.RateLimitError as exc:
            self._log_failure(role, model, attempts, started, exc)
            raise RateLimitedError() from exc
        except (groq.APIError, groq.GroqError) as exc:
            self._log_failure(role, model, attempts, started, exc)
            raise ProviderUnavailableError() from exc

        usage = getattr(response, "usage", None)
        logger.info(
            "llm_call",
            extra={
                "role": role.value,
                "model": model,
                "attempts": attempts,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            },
        )
        content = response.choices[0].message.content
        return content or ""

    @staticmethod
    def _log_failure(
        role: ModelRole, model: str, attempts: int, started: float, exc: Exception
    ) -> None:
        logger.warning(
            "llm_call_failed",
            extra={
                "role": role.value,
                "model": model,
                "attempts": attempts,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "exc_type": type(exc).__name__,
            },
        )


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _summarize_errors(error: ValidationError, limit: int = 8) -> str:
    lines = []
    for item in error.errors()[:limit]:
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        lines.append(f"- {location}: {item['msg']}")
    return "\n".join(lines)
