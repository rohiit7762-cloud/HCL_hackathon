"""Dependency wiring. Builds Claude's components on top of Codex's DataLayer.

Codex's services are created by ONE factory, `app.services.data_layer.build_data_layer`,
imported lazily so the app (and tests) can run with an injected DataLayer before
Codex's implementation exists.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.agents.answer import AnswerAgent
from app.agents.compliance import ResponseValidator
from app.agents.evidence import EvidenceChecker
from app.agents.planner import Planner
from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.graph.query_graph import QueryWorkflow
from app.graph.task_graph import TaskWorkflow
from app.prompts import PromptLibrary
from app.providers.groq_provider import GroqLLMProvider
from app.schemas.interfaces import DataLayer, LLMProvider
from app.schemas.security import UserContext
from app.security.identity import (
    DemoTokenIdentityResolver,
    EntraIdIdentityResolver,
    IdentityResolver,
)
from app.services.conversations import ConversationStore
from app.services.orchestration import QueryOrchestrator

DATA_LAYER_FACTORY = "app.services.data_layer:build_data_layer"


@dataclass
class ServiceContainer:
    settings: Settings
    llm: LLMProvider
    data: DataLayer
    identity: IdentityResolver
    demo_identity: DemoTokenIdentityResolver | None
    orchestrator: QueryOrchestrator

    async def close(self) -> None:
        if self.data.close is not None:
            await self.data.close()


async def load_data_layer(settings: Settings, llm: LLMProvider) -> DataLayer:
    module_name, _, attr = DATA_LAYER_FACTORY.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "Data layer not available: Codex's build_data_layer() has not been merged yet. "
            "Inject a DataLayer explicitly (see backend/tests/fakes) to run without it."
        ) from exc
    data: DataLayer = await factory(settings, llm)
    return data


async def build_container(
    settings: Settings,
    *,
    data: DataLayer | None = None,
    llm: LLMProvider | None = None,
) -> ServiceContainer:
    llm = llm or GroqLLMProvider(settings)
    data = data or await load_data_layer(settings, llm)
    prompts = PromptLibrary()

    planner = Planner(llm, data.policy, data.principals, data.sensitivity, prompts)
    checker = EvidenceChecker(llm, prompts, settings.evidence_min_score)
    task_workflow = TaskWorkflow(planner, checker, data.retrieval, data.sql, data.policy)
    workflow = QueryWorkflow(
        planner,
        task_workflow,
        AnswerAgent(llm, prompts),
        ResponseValidator(llm, data.policy, data.sensitivity, prompts),
        data.policy,
    )
    orchestrator = QueryOrchestrator(
        workflow,
        ConversationStore(),
        timeout_seconds=settings.query_timeout_seconds,
        max_concurrency=settings.groq_max_concurrency,
    )

    demo_identity: DemoTokenIdentityResolver | None = None
    identity: IdentityResolver
    if settings.demo_mode:
        demo_identity = DemoTokenIdentityResolver(
            data.principals,
            settings.demo_jwt_secret.get_secret_value(),
            settings.demo_token_ttl_minutes,
        )
        identity = demo_identity
    else:
        identity = EntraIdIdentityResolver()

    return ServiceContainer(
        settings=settings,
        llm=llm,
        data=data,
        identity=identity,
        demo_identity=demo_identity,
        orchestrator=orchestrator,
    )


def get_container(request: Request) -> ServiceContainer:
    container: ServiceContainer = request.app.state.container
    return container


_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    container: Annotated[ServiceContainer, Depends(get_container)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UserContext:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError()
    return await container.identity.resolve(credentials.credentials)


Container = Annotated[ServiceContainer, Depends(get_container)]
CurrentUser = Annotated[UserContext, Depends(get_current_user)]
