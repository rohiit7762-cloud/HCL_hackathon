"""In-memory conversation history, scoped per user.

Only the user's own previous questions are kept (for resolving "it"/"that"); they
are namespaced by tenant and employee so one user can never read another's.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict, deque

from app.schemas.security import UserContext

MAX_TURNS = 5
MAX_CONVERSATIONS = 1000


class ConversationStore:
    def __init__(self, max_conversations: int = MAX_CONVERSATIONS) -> None:
        self._max = max_conversations
        self._items: OrderedDict[tuple[str, str, str], deque[str]] = OrderedDict()

    def resolve_id(self, user: UserContext, conversation_id: str | None) -> str:
        if conversation_id and self._key(user, conversation_id) in self._items:
            return conversation_id
        return conversation_id or uuid.uuid4().hex

    def history(self, user: UserContext, conversation_id: str) -> list[str]:
        return list(self._items.get(self._key(user, conversation_id), ()))

    def append(self, user: UserContext, conversation_id: str, question: str) -> None:
        key = self._key(user, conversation_id)
        turns = self._items.setdefault(key, deque(maxlen=MAX_TURNS))
        turns.append(question)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    @staticmethod
    def _key(user: UserContext, conversation_id: str) -> tuple[str, str, str]:
        return (user.tenant_id, user.employee_id, conversation_id)
