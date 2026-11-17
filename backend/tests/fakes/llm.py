"""Scripted LLM provider for tests. CI never needs a Groq key.

Usage:
    llm = FakeLLMProvider()
    llm.script(ModelRole.PLANNER, PlannerOutput(...))          # one queued response
    llm.script(ModelRole.ANSWER, lambda messages, schema: ...)  # computed response
    llm.default(ModelRole.EVIDENCE_JUDGE, {...})                # used when the queue is empty

Queued responses are consumed in order per role. A response may be a Pydantic
model, a dict, a JSON string, a plain string, an Exception (raised), or a callable
`(messages, schema) -> any of the above`.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.errors import CapabilityLimitError, ValidationFailedError
from app.schemas.llm import ChatMessage, ImageInput, ModelRole

T = TypeVar("T", bound=BaseModel)

Responder = Callable[[Sequence[ChatMessage], type[BaseModel] | None], Any]


@dataclass
class LLMCall:
    role: ModelRole
    messages: list[ChatMessage]
    schema: type[BaseModel] | None = None
    image: ImageInput | None = None


@dataclass
class FakeLLMProvider:
    vision: bool = True
    calls: list[LLMCall] = field(default_factory=list)
    _queues: dict[ModelRole, deque[Any]] = field(default_factory=lambda: defaultdict(deque))
    _defaults: dict[ModelRole, Any] = field(default_factory=dict)

    # --- scripting -------------------------------------------------------
    def script(self, role: ModelRole, *responses: Any) -> FakeLLMProvider:
        self._queues[role].extend(responses)
        return self

    def default(self, role: ModelRole, response: Any) -> FakeLLMProvider:
        self._defaults[role] = response
        return self

    def calls_for(self, role: ModelRole) -> list[LLMCall]:
        return [call for call in self.calls if call.role == role]

    # --- LLMProvider -----------------------------------------------------
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        role: ModelRole,
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        self.calls.append(LLMCall(role=role, messages=list(messages)))
        raw = self._resolve(role, messages, None)
        if isinstance(raw, BaseModel):
            return raw.model_dump_json()
        if isinstance(raw, dict | list):
            return json.dumps(raw)
        return str(raw)

    async def complete_structured(
        self,
        messages: Sequence[ChatMessage],
        schema: type[T],
        *,
        role: ModelRole,
        max_tokens: int | None = None,
    ) -> T:
        self.calls.append(LLMCall(role=role, messages=list(messages), schema=schema))
        raw = self._resolve(role, messages, schema)
        try:
            if isinstance(raw, schema):
                return raw
            if isinstance(raw, BaseModel):
                return schema.model_validate(raw.model_dump())
            if isinstance(raw, str):
                return schema.model_validate_json(raw)
            return schema.model_validate(raw)
        except ValidationError as exc:
            raise ValidationFailedError("Scripted response did not match schema") from exc

    async def describe_image(self, image: ImageInput, instruction: str) -> str:
        if not self.vision:
            raise CapabilityLimitError()
        message = ChatMessage(role="user", content=instruction)
        self.calls.append(LLMCall(role=ModelRole.VISION, messages=[message], image=image))
        return str(self._resolve(ModelRole.VISION, [message], None))

    def supports_vision(self) -> bool:
        return self.vision

    # --- internals -------------------------------------------------------
    def _resolve(
        self,
        role: ModelRole,
        messages: Sequence[ChatMessage],
        schema: type[BaseModel] | None,
    ) -> Any:
        queue = self._queues[role]
        if queue:
            response = queue.popleft()
        elif role in self._defaults:
            response = self._defaults[role]
        else:
            raise AssertionError(f"FakeLLMProvider: no scripted response for role {role}")
        if callable(response) and not isinstance(response, type | BaseModel):
            response = response(messages, schema)
        if isinstance(response, BaseException):
            raise response
        return response
