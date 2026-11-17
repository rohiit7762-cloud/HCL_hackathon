"""OCR via RapidOCR (ONNX runtime; no system binaries, works on Windows)."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)

_MIN_CONFIDENCE = 0.5


class OcrEngine(Protocol):
    async def extract(self, image: bytes) -> str: ...


class RapidOcrEngine:
    def __init__(self) -> None:
        self._engine: Any = None
        self._lock = asyncio.Lock()

    async def extract(self, image: bytes) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._extract, image)

    def _extract(self, image: bytes) -> str:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        try:
            result, _ = self._engine(image)
        except Exception as exc:  # unreadable image: OCR is best-effort
            logger.warning("ocr_failed", extra={"exc_type": type(exc).__name__})
            return ""
        if not result:
            return ""
        # Each item: [box, text, score]; order top-to-bottom, then left-to-right.
        items = sorted(
            (r for r in result if float(r[2]) >= _MIN_CONFIDENCE),
            key=lambda r: (round(r[0][0][1] / 12), r[0][0][0]),
        )
        return "\n".join(str(r[1]).strip() for r in items if str(r[1]).strip())


class NullOcrEngine:
    async def extract(self, image: bytes) -> str:
        return ""
