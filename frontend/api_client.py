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
