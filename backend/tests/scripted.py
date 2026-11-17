"""Prompt-aware scripted LLM behaviours shared by workflow and API tests."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from app.schemas.llm import ChatMessage, ModelRole
from tests.fakes.llm import FakeLLMProvider

_EVIDENCE = re.compile(r'<evidence id="([^"]+)"[^>]*>\n?(.*?)\n?</evidence>', re.S)
_TASK = re.compile(r"### Task (t\d+)\nQuestion: (.*?)\n(.*?)(?=### Task t\d+|\Z)", re.S)
_CLAIM = re.compile(r'<claim id="([^"]+)">')


def _system(messages: Sequence[ChatMessage]) -> str:
    return messages[0].content


def judge_sufficient(messages: Sequence[ChatMessage], _schema: Any) -> dict[str, Any]:
    text = _system(messages)
    task_id = re.search(r'"task_id": "(t\d+)"', text) or re.search(r"`task_id`: \"(t\d+)\"", text)
    attempt = re.search(r"`attempt`: (\d)", text)
    ids = [m.group(1) for m in _EVIDENCE.finditer(text)]
    return {
        "task_id": task_id.group(1) if task_id else "t1",
        "attempt": int(attempt.group(1)) if attempt else 1,
        "status": "sufficient" if ids else "no_evidence",
        "relevant_evidence_ids": ids,
    }


def judge_insufficient(messages: Sequence[ChatMessage], schema: Any) -> dict[str, Any]:
    result = judge_sufficient(messages, schema)
    result.update(
        status="insufficient",
        missing_information=["the specific value"],
        refinement_hint="search for the exact term",
    )
    return result


def answer_from_evidence(messages: Sequence[ChatMessage], _schema: Any) -> dict[str, Any]:
    """Write one fact per task: the first evidence excerpt, cited."""
    answers = []
    for match in _TASK.finditer(_system(messages)):
        task_id, block = match.group(1), match.group(3)
        found = _EVIDENCE.findall(block)
        if found:
            eid, excerpt = found[0]
            segment = {
                "text": excerpt.strip().split("\n")[0][:300],
                "kind": "fact",
                "evidence_ids": [eid],
            }
        else:
            segment = {"text": "No information was found.", "kind": "gap", "evidence_ids": []}
        answers.append({"task_id": task_id, "segments": [segment]})
    return {"overview": None, "task_answers": answers}


def all_grounded(messages: Sequence[ChatMessage], _schema: Any) -> dict[str, Any]:
    return {
        "verdicts": [{"claim_id": c, "supported": True} for c in _CLAIM.findall(_system(messages))]
    }


def planned(*tasks: dict[str, Any]) -> dict[str, Any]:
    defaults = {
        "original_span": "",
        "subject_refs": [],
        "requested_fields": [],
        "depends_on": [],
        "retrieval_query": None,
        "use_upload_ids": [],
        "intent": "fact_lookup",
        "source_needs": ["documents"],
    }
    return {"tasks": [{**defaults, **t} for t in tasks]}


def scripted_llm(*plans: dict[str, Any]) -> FakeLLMProvider:
    llm = FakeLLMProvider()
    llm.script(ModelRole.PLANNER, *plans)
    llm.default(ModelRole.EVIDENCE_JUDGE, judge_sufficient)
    llm.default(ModelRole.ANSWER, answer_from_evidence)
    llm.default(ModelRole.GROUNDING_CHECK, all_grounded)
    llm.default(
        ModelRole.REFINER,
        {
            "task_id": "t1",
            "retrieval_query": "refined query",
            "keywords": ["RTO"],
            "rationale": "expand",
        },
    )
    return llm
