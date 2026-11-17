"""Fixed-template progress events. Messages never contain query or evidence content."""

from __future__ import annotations

from typing import Literal

from app.schemas.api import ProgressEvent, ProgressStep

Status = Literal["started", "done", "skipped", "failed"]


def part(task_id: str) -> str:
    return task_id[1:] if task_id.startswith("t") and task_id[1:].isdigit() else task_id


def event(
    step: ProgressStep, status: Status, message: str, task_id: str | None = None
) -> ProgressEvent:
    return ProgressEvent(step=step, status=status, message=message, task_id=task_id)


def identity_verified() -> ProgressEvent:
    return event("identity", "done", "Verified your identity")


def planning_done(task_count: int) -> ProgressEvent:
    noun = "part" if task_count == 1 else "parts"
    return event("planning", "done", f"Split your question into {task_count} {noun}")


def task_declined(task_id: str) -> ProgressEvent:
    return event("compliance", "skipped", f"Part {part(task_id)} is outside your access", task_id)


def compliance_done() -> ProgressEvent:
    return event("compliance", "done", "Checked access for each part")


def retrieval_started(task_id: str) -> ProgressEvent:
    return event("retrieval", "started", f"Searching documents for part {part(task_id)}", task_id)


def sql_started(task_id: str) -> ProgressEvent:
    return event("sql", "started", f"Querying structured records for part {part(task_id)}", task_id)


def evidence_checked(task_id: str, sufficient: bool) -> ProgressEvent:
    status: Status = "done" if sufficient else "failed"
    verdict = "enough" if sufficient else "not enough"
    return event(
        "evidence_check", status, f"Found {verdict} evidence for part {part(task_id)}", task_id
    )


def retry_started(task_id: str) -> ProgressEvent:
    return event(
        "retry", "started", f"Retrying part {part(task_id)} with a refined search", task_id
    )


def dependency_unavailable(task_id: str) -> ProgressEvent:
    return event(
        "retrieval",
        "skipped",
        f"Part {part(task_id)} depends on a part that couldn't be answered",
        task_id,
    )


def answer_started() -> ProgressEvent:
    return event("answer", "started", "Writing the answer")


def validation_done() -> ProgressEvent:
    return event("validation", "done", "Verified citations and access")
