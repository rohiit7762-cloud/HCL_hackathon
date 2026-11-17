"""Local file storage for original documents/images and user uploads."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path

from app.schemas.llm import ImageInput
from app.schemas.security import UserContext

_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_UPLOAD_ID = re.compile(r"^up_[0-9a-f]{32}$")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
UPLOAD_TTL_SECONDS = 24 * 3600


def _safe(part: str) -> str:
    cleaned = _SAFE.sub("_", part).strip("._") or "x"
    return cleaned[:120]


class BlobStore:
    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def path_for(self, tenant_id: str, source_id: str, version: int, filename: str) -> str:
        return "/".join([_safe(tenant_id), _safe(source_id), str(version), _safe(filename)])

    async def save(self, relative: str, data: bytes) -> None:
        path = self._resolve(relative)
        await asyncio.to_thread(_write, path, data)

    async def read(self, relative: str) -> bytes | None:
        path = self._resolve(relative)
        if not path.is_file():
            return None
        return await asyncio.to_thread(path.read_bytes)

    def _resolve(self, relative: str) -> Path:
        path = (self._root / relative).resolve()
        if self._root not in path.parents:
            raise ValueError("blob path escapes the storage root")
        return path


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class FileUploadStore:
    """User-uploaded images, readable only by the uploader, expiring after 24 hours."""

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve() / "uploads"

    def _dir(self, user: UserContext) -> Path:
        return self._root / _safe(user.tenant_id) / _safe(user.employee_id)

    async def save(self, user: UserContext, data: bytes, mime_type: str) -> str:
        if mime_type not in _EXT:
            raise ValueError("unsupported image type")
        upload_id = f"up_{uuid.uuid4().hex}"
        directory = self._dir(user)
        meta = {"mime_type": mime_type, "expires_at": time.time() + UPLOAD_TTL_SECONDS}
        await asyncio.to_thread(_write, directory / f"{upload_id}.bin", data)
        await asyncio.to_thread(_write, directory / f"{upload_id}.json", json.dumps(meta).encode())
        return upload_id

    async def load(self, user: UserContext, upload_id: str) -> ImageInput | None:
        if not _UPLOAD_ID.match(upload_id):
            return None
        directory = self._dir(user)  # path is derived from the caller: no cross-user access
        meta_path, data_path = directory / f"{upload_id}.json", directory / f"{upload_id}.bin"
        if not meta_path.is_file() or not data_path.is_file():
            return None
        meta = json.loads(await asyncio.to_thread(meta_path.read_text))
        if float(meta["expires_at"]) < time.time():
            return None
        data = await asyncio.to_thread(data_path.read_bytes)
        return ImageInput(data=data, mime_type=meta["mime_type"], source_id=upload_id)
