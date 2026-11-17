"""Start the FastAPI backend.

    python scripts/run_backend.py [--host 127.0.0.1] [--port 8000]

On Windows the selector event loop is required by psycopg's async driver, so the
server is started inside our own event loop instead of uvicorn's default one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import enter_root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    enter_root()

    import uvicorn

    from app.repositories.postgres_repository import use_selector_event_loop_on_windows

    use_selector_event_loop_on_windows()
    config = uvicorn.Config(
        "app.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        loop="none",
        log_config=None,
    )
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
