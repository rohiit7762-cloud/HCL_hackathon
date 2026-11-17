"""Upload documents and data into the knowledge base (ingestor/admin roles only).

The backend enforces the role check and sniffs file types; this page only collects
the file and its access settings.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from app.schemas.common import Classification
from app.schemas.ingestion import IngestOptions
from frontend.api_client import ApiClient, ApiError

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8765")
INGEST_ROLES = {"ingestor", "admin"}
KNOWN_GROUPS = [
    "all-staff",
    "eng-platform",
    "eng-payments",
    "finance",
    "hr-team",
    "leadership",
    "it",
]
FILE_TYPES = ["pdf", "docx", "md", "txt", "csv", "json", "png", "jpg", "jpeg"]
DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
CLASSIFICATION_HELP = {
    Classification.PUBLIC: "Anyone in the company",
    Classification.INTERNAL: "All employees (internal clearance)",
    Classification.CONFIDENTIAL: "Only people with confidential clearance",
    Classification.RESTRICTED: "Only people with restricted clearance (e.g. HR)",
}

st.set_page_config(page_title="Upload documents", page_icon="📤", layout="wide")
client = ApiClient(BACKEND_URL)
st.title("Upload documents")
st.caption("Add documents or data to the knowledge base and choose who can see them.")

token = st.session_state.get("token")
if not token:
    st.info("Sign in first: go to the main page and choose **Admin Bot** in the sidebar.")
    st.stop()
try:
    me = client.me(token)
except ApiError as exc:
    st.warning(exc.message if not exc.session_expired else "Session expired. Sign in again.")
    st.stop()
if not INGEST_ROLES & set(me.roles):
    st.warning(
        f"You're signed in as **{me.display_name}**, who can't add documents. "
        "Go to the main page and choose **Admin Bot**."
    )
    st.stop()

with st.form("upload", clear_on_submit=False):
    files = st.file_uploader(
        "Files",
        type=FILE_TYPES,
        accept_multiple_files=True,
        help="PDF, Word, Markdown, text, CSV/JSON tables, or images (searched via OCR text).",
    )
    left, right = st.columns(2)
    with left:
        classification = st.selectbox(
            "Classification",
            options=list(Classification),
            index=1,
            format_func=lambda c: f"{c.value} — {CLASSIFICATION_HELP[c]}",
        )
        groups = st.multiselect(
            "Restrict to groups (optional)",
            options=KNOWN_GROUPS,
            help="Leave empty so everyone with the clearance can see it. "
            "If you pick groups, only their members can.",
        )
        extra_groups = st.text_input("Other groups (comma-separated, optional)")
    with right:
        title = st.text_input(
            "Title (optional)",
            help="Used as the document title in citations. "
            "Only applied when uploading a single file; otherwise the file name is used.",
        )
        dataset_name = st.text_input(
            "Table name for CSV/JSON (optional)",
            help="Lowercase letters, digits and underscores, e.g. sales_2026. "
            "Defaults to the file name.",
        )
        personal = st.checkbox(
            "Contains personal data about employees",
            help="Only HR can see it unless it's only about the viewer.",
        )
        confidential = st.checkbox("Contains confidential business information (M&A, financials)")
    submitted = st.form_submit_button("Upload", type="primary", use_container_width=True)

if submitted:
    all_groups = sorted(set(groups) | {g.strip() for g in extra_groups.split(",") if g.strip()})
    if not files:
        st.error("Choose at least one file.")
        st.stop()
    if dataset_name and not DATASET_NAME.match(dataset_name):
        st.error("Table name must start with a lowercase letter and use only a-z, 0-9 and _.")
        st.stop()

    results = []
    progress = st.progress(0.0, text="Uploading…")
    for index, upload in enumerate(files, start=1):
        options = IngestOptions(
            title=title.strip() or None if len(files) == 1 else None,
            classification=classification,
            allowed_groups=all_groups,
            contains_personal_data=personal,
            contains_confidential_business=confidential,
            dataset_name=dataset_name.strip() or None,
        )
        try:
            result = client.ingest(
                token,
                upload.name,
                upload.getvalue(),
                upload.type or "application/octet-stream",
                options,
            )
            detail = (
                f"{result.chunk_count} chunks"
                if result.chunk_count
                else f"{result.row_count} rows"
                if result.row_count
                else ""
            )
            status = {
                "indexed": "✅ Indexed",
                "loaded": "✅ Loaded",
                "skipped_duplicate": "⏭️ Already uploaded",
                "failed": "❌ Failed",
            }[result.status]
            results.append(
                {
                    "File": upload.name,
                    "Result": status,
                    "Details": detail,
                    "Notes": "; ".join(result.warnings),
                }
            )
        except ApiError as exc:
            results.append(
                {"File": upload.name, "Result": "❌ Failed", "Details": "", "Notes": exc.message}
            )
        progress.progress(index / len(files), text=f"Processed {index} of {len(files)}")
    progress.empty()
    st.dataframe(results, use_container_width=True, hide_index=True)
    if any(r["Result"].startswith("✅") for r in results):
        st.success(
            "Done. New documents are searchable right away — ask about them on the "
            "main page (sign in as someone who is allowed to see them)."
        )
