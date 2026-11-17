"""Streamlit UI for the Enterprise Knowledge Assistant.

    streamlit run frontend/streamlit_app.py

Holds no secrets and no business logic: identity, policy, retrieval and LLM calls
all happen in the FastAPI backend.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `frontend` importable

from frontend.api_client import ApiClient, ApiError
from frontend.components.render import render_response

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8765")
EXAMPLES = [
    "What do I need to set up in my first week, and who do I ask about VPN access?",
    "How many P1 incidents hit payments-api last month, what was the root cause of INC-1042, "
    "and what does the runbook say to do for a ledger database failover?",
    "What's the escalation path for payments outages, and what is Sam Patel's salary?",
    "In the payments architecture diagram, which component writes to the ledger database?",
    "Summarize the Project Falcon memo.",
    "What are my direct reports' performance ratings and salaries?",
]

st.set_page_config(page_title="Knowledge Assistant", page_icon="💬", layout="wide")
client = ApiClient(BACKEND_URL)
state = st.session_state
state.setdefault("messages", [])
state.setdefault("token", None)
state.setdefault("persona", None)
state.setdefault("conversation_id", None)


def reset_conversation() -> None:
    state.messages = []
    state.conversation_id = None


def sign_in(employee_id: str) -> None:
    try:
        state.token = client.login(employee_id)
        state.persona = employee_id
        reset_conversation()
    except ApiError as exc:
        st.sidebar.error(exc.message)


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.warning(
        "**DEMO MODE — synthetic identities.** All people and data are fictional.", icon="🧪"
    )
    if not client.health():
        st.error(
            f"Backend not reachable at {BACKEND_URL}. Start it with "
            "`python scripts/run_backend.py`."
        )
        st.stop()
    try:
        personas = client.demo_users()
    except ApiError as exc:
        st.error(exc.message)
        st.stop()

    labels = {p.employee_id: f"{p.display_name} — {p.title}" for p in personas}
    choice = st.selectbox(
        "Sign in as",
        options=list(labels),
        format_func=labels.__getitem__,
        index=None,
        placeholder="Choose a synthetic identity",
    )
    if choice and choice != state.persona:
        sign_in(choice)
    if choice:
        persona = next(p for p in personas if p.employee_id == choice)
        st.caption(persona.access_summary)

    if state.token:
        try:
            me = client.me(state.token)
            st.markdown(
                f"**Roles:** {', '.join(me.roles)}  \n**Groups:** {', '.join(me.groups)}"
                f"  \n**Clearance:** {me.clearance.value}"
            )
        except ApiError as exc:
            if exc.session_expired:
                state.token = None
                st.warning("Session expired. Choose your identity again.")
    if st.button("New conversation", use_container_width=True):
        reset_conversation()
        st.rerun()

# ------------------------------------------------------------------ main
st.title("Enterprise Knowledge Assistant")
st.caption("Answers with citations from the documents and data you're allowed to see.")

if not state.token:
    st.info("Choose a synthetic identity in the sidebar to start.")
    st.stop()

for index, message in enumerate(state.messages):
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
        else:
            render_response(message["content"], client, state.token, key=f"m{index}")

if not state.messages:
    st.markdown("**Try asking:**")
    for example in EXAMPLES:
        st.markdown(f"- {example}")

prompt = st.chat_input("Ask about policies, runbooks, incidents, people or diagrams…")
if prompt:
    state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Planning, checking access and gathering evidence…"):
                response = client.query(state.token, prompt, state.conversation_id, [])
            state.conversation_id = response.conversation_id
            state.messages.append({"role": "assistant", "content": response})
            render_response(response, client, state.token, key=f"m{len(state.messages) - 1}")
        except ApiError as exc:
            state.messages.pop()
            if exc.session_expired:
                state.token = None
                st.warning("Your session expired. Choose your identity again.")
            else:
                suffix = f" (request {exc.request_id})" if exc.request_id else ""
                st.error(f"{exc.message}{suffix}")
