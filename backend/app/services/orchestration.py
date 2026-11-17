"""QueryOrchestrator: runs the LangGraph workflow for one query (§7.4)."""

from __future__ import annotations

import asyncio
import time

from app.core.errors import ToolTimeoutError
from app.core.logging import get_logger, request_id_var
from app.graph.query_graph import QueryWorkflow
from app.graph.state import QueryState
from app.schemas.api import QueryRequest, QueryResponse
from app.schemas.security import UserContext
from app.services.conversations import ConversationStore

logger = get_logger(__name__)

# Share of the time budget reserved for retrieval; the rest is for answer + validation.
TASK_BUDGET_SHARE = 0.7
RECURSION_LIMIT = 60


class QueryOrchestrator:
    def __init__(
        self,
        workflow: QueryWorkflow,
        conversations: ConversationStore,
        timeout_seconds: float,
        max_concurrency: int = 4,
    ) -> None:
        self._workflow = workflow
        self._conversations = conversations
        self._timeout = timeout_seconds
        self._max_concurrency = max_concurrency

    async def run(self, user: UserContext, request: QueryRequest) -> QueryResponse:
        started = time.monotonic()
        conversation_id = self._conversations.resolve_id(user, request.conversation_id)
        state: QueryState = {
            "user": user,
            "request": request,
            "request_id": request_id_var.get(),
            "conversation_id": conversation_id,
            "history": self._conversations.history(user, conversation_id),
            "deadline": started + self._timeout * TASK_BUDGET_SHARE,
            "outcomes": {},
            "progress": [],
            "warnings": [],
        }
        try:
            final: QueryState = await asyncio.wait_for(
                self._workflow.graph.ainvoke(
                    state,
                    config={
                        "max_concurrency": self._max_concurrency,
                        "recursion_limit": RECURSION_LIMIT,
                    },
                ),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            logger.warning("query_timeout", extra={"elapsed_ms": _ms(started)})
            raise ToolTimeoutError() from exc

        self._conversations.append(user, conversation_id, request.query)
        response = final["response"]
        logger.info(
            "query_done",
            extra={
                "elapsed_ms": _ms(started),
                "tasks": len(response.tasks),
                "statuses": [t.status.value for t in response.tasks],
                "citations": len(response.citations),
            },
        )
        return response


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
