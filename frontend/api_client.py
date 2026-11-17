from __future__ import annotations

import os
import httpx


class APIClient:
    def __init__(self):
        self.base = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")

    def get(self, path):
        return httpx.get(self.base + path, timeout=10).raise_for_status().json()

    def post(self, path, data, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return (
            httpx.post(self.base + path, json=data, headers=headers, timeout=60)
            .raise_for_status()
            .json()
        )

    def upload(self, file, classification, groups, token):
        return (
            httpx.post(
                self.base + "/api/v1/ingest",
                headers={"Authorization": f"Bearer {token}"},
                data={"classification": classification, "allowed_groups": groups},
                files={"file": (file.name, file.getvalue(), file.type)},
                timeout=180,
            )
            .raise_for_status()
            .json()
        )
