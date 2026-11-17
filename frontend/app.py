from __future__ import annotations

import httpx
import streamlit as st
from frontend.api_client import APIClient

st.set_page_config(page_title="Lumen Copilot", page_icon="✦", layout="wide")
st.markdown(
    """<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
.stApp{background:radial-gradient(ellipse at 55% -18%,#203c50,#0b1220 52%,#080d17);color:#e9edf5;font-family:'DM Sans',sans-serif}
[data-testid="stSidebar"]{background:#0b1320;border-right:1px solid #1d293a}h1,h2,h3{font-family:'Space Grotesk',sans-serif!important;letter-spacing:-.035em}
.brand{font:700 18px 'Space Grotesk';letter-spacing:-.03em}.brand span,.eyebrow{color:#83e6c0}.eyebrow{font-size:11px;font-weight:700;letter-spacing:.16em;text-transform:uppercase}
.hero{padding:24px 0 12px}.hero h1{font-size:44px;line-height:1.04;margin:10px 0}.hero p{color:#9eabc0;font-size:16px;max-width:690px}
.card{background:linear-gradient(140deg,rgba(20,31,48,.95),rgba(13,21,34,.95));border:1px solid #28364b;border-radius:16px;padding:16px 19px;margin:10px 0;box-shadow:0 15px 40px #03071244}
div[data-testid="stChatMessage"]{background:#0f1826bb;border:1px solid #1f2d40;border-radius:16px;padding:8px 14px}button[kind="primary"]{background:#83e6c0!important;color:#09140f!important;border:0!important}
</style>""",
    unsafe_allow_html=True,
)
api = APIClient()
for k, v in {"messages": [], "token": None, "conversation": None}.items():
    st.session_state.setdefault(k, v)
with st.sidebar:
    st.markdown(
        '<div class="brand">✦ LUMEN <span>/ COPILOT</span></div><br><div class="eyebrow">Synthetic demo identity</div>',
        unsafe_allow_html=True,
    )
    try:
        users = api.get("/api/v1/demo/users")
    except httpx.HTTPError:
        users = []
    if users:
        selected = st.selectbox(
            "Choose identity",
            users,
            format_func=lambda u: f"{u['display_name']} · {u['title']}",
            label_visibility="collapsed",
        )
        if (
            st.button("Use identity", type="primary", use_container_width=True)
            or not st.session_state.token
        ):
            try:
                st.session_state.token = api.post(
                    "/api/v1/demo/session", {"employee_id": selected["employee_id"]}
                )["access_token"]
            except httpx.HTTPError as e:
                st.error(f"Backend unavailable: {e}")
        st.markdown(
            f'<div class="card"><div class="eyebrow">Synthetic identity</div><h3>{selected["display_name"]}</h3><p>{selected["title"]}</p><small>{selected["access_summary"]}</small></div>',
            unsafe_allow_html=True,
        )
    st.markdown('<div class="eyebrow">Try a prompt</div>', unsafe_allow_html=True)
    prompts = [
        "What should I set up in my first week?",
        "What caused INC-1042 and what does the failover runbook say?",
        "What is the payments escalation path, and what is Sam Patel’s salary?",
        "What does the payments architecture diagram show?",
    ]
    for i, p in enumerate(prompts):
        if st.button(p, key=f"p{i}", use_container_width=True):
            st.session_state.pending = p
    if st.button("＋ New conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.conversation = None
        st.rerun()
    st.caption("Synthetic data · Add provider credentials to .env for live integrations")
st.markdown(
    '<div class="hero"><div class="eyebrow">Enterprise knowledge, with guardrails</div><h1>Find the signal.<br>Keep the context.</h1><p>Ask across runbooks, architecture, incidents and structured records. Answers cite their sources and respect your access.</p></div>',
    unsafe_allow_html=True,
)
cols = st.columns(3)
for c, (a, b) in zip(
    cols,
    [
        ("01 · RETRIEVE", "Search authorized sources"),
        ("02 · VERIFY", "Check evidence and citations"),
        ("03 · RESPOND", "Answer permitted parts"),
    ],
):
    with c:
        st.markdown(
            f'<div class="card"><div class="eyebrow">{a}</div><p>{b}</p></div>',
            unsafe_allow_html=True,
        )


def render_citations(citations):
    if citations:
        with st.expander(f"Sources · {len(citations)}"):
            for c in citations:
                st.markdown(f"**[{c['citation_id']}] {c['title']}** · {c['location_label']}")
                st.caption(c["excerpt"])
                if st.button(
                    "Open source preview", key=f"open-{c['source_id']}-{c['citation_id']}"
                ):
                    try:
                        st.json(api.get(c["preview_url"]))
                    except httpx.HTTPError:
                        st.info("Source preview unavailable")


for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["text"])
        render_citations(m.get("citations", []))
prompt = st.chat_input(
    "Ask about a process, incident, service, or source…"
) or st.session_state.pop("pending", None)
if prompt:
    st.session_state.messages.append({"role": "user", "text": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.status("Checking identity, access and evidence…") as status:
            try:
                result = api.post(
                    "/api/v1/query",
                    {"query": prompt, "conversation_id": st.session_state.conversation},
                    st.session_state.token,
                )
                status.update(label="Response ready", state="complete")
            except httpx.HTTPError as e:
                result = None
                status.update(label="Backend unavailable", state="error")
                st.error(f"Start the API and retry: {e}")
        if result:
            st.session_state.conversation = result["conversation_id"]
            st.markdown(result["answer_markdown"])
            render_citations(result["citations"])
            for warning in result["warnings"]:
                st.caption("ⓘ " + warning)
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "text": result["answer_markdown"],
                    "citations": result["citations"],
                }
            )
