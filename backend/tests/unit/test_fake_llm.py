from __future__ import annotations

import pytest

from app.core.errors import CapabilityLimitError, ValidationFailedError
from app.schemas.llm import ChatMessage, ImageInput, ModelRole
from app.schemas.planning import RefinedQuery
from tests.fakes.llm import FakeLLMProvider

MESSAGES = [ChatMessage(role="user", content="hi")]


async def test_scripted_structured_responses_in_order() -> None:
    llm = FakeLLMProvider().script(
        ModelRole.REFINER,
        {"task_id": "t1", "retrieval_query": "first"},
        '{"task_id": "t1", "retrieval_query": "second"}',
    )
    first = await llm.complete_structured(MESSAGES, RefinedQuery, role=ModelRole.REFINER)
    second = await llm.complete_structured(MESSAGES, RefinedQuery, role=ModelRole.REFINER)
    assert (first.retrieval_query, second.retrieval_query) == ("first", "second")
    assert len(llm.calls_for(ModelRole.REFINER)) == 2


async def test_invalid_scripted_response_raises_validation_failed() -> None:
    llm = FakeLLMProvider().script(ModelRole.REFINER, {"nope": 1})
    with pytest.raises(ValidationFailedError):
        await llm.complete_structured(MESSAGES, RefinedQuery, role=ModelRole.REFINER)


async def test_callable_and_exception_responses() -> None:
    llm = FakeLLMProvider().script(
        ModelRole.ANSWER, lambda messages, schema: f"echo:{messages[-1].content}", TimeoutError()
    )
    assert await llm.complete(MESSAGES, role=ModelRole.ANSWER) == "echo:hi"
    with pytest.raises(TimeoutError):
        await llm.complete(MESSAGES, role=ModelRole.ANSWER)


async def test_vision_capability_limit() -> None:
    image = ImageInput(data=b"x", mime_type="image/png")
    with pytest.raises(CapabilityLimitError):
        await FakeLLMProvider(vision=False).describe_image(image, "describe")


async def test_missing_script_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        await FakeLLMProvider().complete(MESSAGES, role=ModelRole.PLANNER)
