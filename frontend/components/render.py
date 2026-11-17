"""Rendering helpers for assistant responses."""

from __future__ import annotations

import streamlit as st

from app.schemas.api import Citation, QueryResponse, TaskStatus
from app.schemas.evidence import EvidenceKind
from frontend.api_client import ApiClient, ApiError

STATUS_BADGES: dict[TaskStatus, tuple[str, str]] = {
    TaskStatus.ANSWERED: ("Answered", "green"),
    TaskStatus.PARTIAL: ("Partly answered", "orange"),
    TaskStatus.INSUFFICIENT_EVIDENCE: ("Not enough evidence", "orange"),
    TaskStatus.DECLINED: ("Outside your access", "gray"),
    TaskStatus.FAILED: ("Couldn't complete", "red"),
}
KIND_LABELS = {
    EvidenceKind.TEXT_CHUNK: "Document",
    EvidenceKind.IMAGE: "Image",
    EvidenceKind.STRUCTURED: "Structured data",
    EvidenceKind.VISION_OBSERVATION: "Image analysis",
}


def render_response(response: QueryResponse, client: ApiClient, token: str, key: str) -> None:
    """Render one validated answer: per-part status, answer text, citations, warnings."""
    if len(response.tasks) > 1:
        cols = st.columns(len(response.tasks))
        for col, task in zip(cols, response.tasks, strict=True):
            label, color = STATUS_BADGES[task.status]
            col.badge(f"Part {task.task_id.lstrip('t')}: {label}", color=color)
    elif response.tasks:
        label, color = STATUS_BADGES[response.tasks[0].status]
        st.badge(label, color=color)

    st.markdown(response.answer_markdown)

    for warning in response.warnings:
        st.info(warning)

    if response.citations:
        with st.expander(f"Sources ({len(response.citations)})", expanded=False):
            for citation in response.citations:
                _render_citation(citation, client, token, f"{key}-{citation.citation_id}")

    with st.expander("How this answer was produced", expanded=False):
        for event in response.progress:
            icon = {"done": "✅", "started": "▶️", "skipped": "⏭️", "failed": "⚠️"}[event.status]
            st.markdown(f"{icon} {event.message}")
        st.caption(f"Request ID: {response.request_id}")


def _render_citation(citation: Citation, client: ApiClient, token: str, key: str) -> None:
    st.markdown(
        f"**[{citation.citation_id}] {citation.title}**  \n"
        f"<small>{KIND_LABELS[citation.kind]} · {citation.location_label}</small>",
        unsafe_allow_html=False,
    )
    st.caption(citation.excerpt)
    if citation.preview_url is None:
        return
    if st.button("Open source preview", key=f"open-{key}"):
        try:
            preview = client.source_preview(token, citation.source_id, citation.location_label)
        except ApiError as exc:
            st.warning(exc.message)
            return
        if preview.image_url:
            try:
                st.image(client.source_image(token, citation.source_id), caption=preview.title)
            except ApiError as exc:
                st.warning(exc.message)
        if preview.excerpt:
            st.text_area("Preview", preview.excerpt, height=180, disabled=True, key=f"pv-{key}")
