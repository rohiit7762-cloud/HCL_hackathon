"""Shared helpers for the operational scripts (run from any directory)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"


def enter_root() -> None:
    """Run from the repo root so Settings finds .env and relative paths resolve."""
    os.chdir(ROOT)


def url_with_credentials(base_url: str, user: str, password: str) -> str:
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    netloc = f"{quote(user)}:{quote(password)}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def update_env(values: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}=")
        replaced = False
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}"
                replaced = True
        if not replaced:
            lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
