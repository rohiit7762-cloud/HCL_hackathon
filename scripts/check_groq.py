import asyncio
import sys

sys.path.insert(0, "backend")

from app.core.config import get_settings
from app.providers.groq_provider import GroqProvider
from app.schemas.llm import ChatMessage, ModelRole


async def main() -> None:
    provider = GroqProvider(get_settings())
    response = await provider.complete(
        [ChatMessage(role="user", content="Reply with OK")],
        role=ModelRole.PLANNER,
        max_tokens=8,
    )
    print("Groq request successful." if response.strip() else "Groq returned an empty response.")


asyncio.run(main())
