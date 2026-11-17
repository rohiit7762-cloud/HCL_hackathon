"""Centralized application settings, loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas.llm import ModelRole


class Settings(BaseSettings):
    """All configuration in one place. Secrets use SecretStr so they never print."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_env: Literal["dev", "test", "prod"] = "dev"
    app_version: str = "0.1.0"
    demo_mode: bool = True
    demo_jwt_secret: SecretStr = SecretStr("")
    demo_token_ttl_minutes: int = Field(120, ge=1, le=24 * 60)
    demo_tenant_id: str = "lumen"
    query_timeout_seconds: float = Field(60.0, gt=0)
    log_level: str = "INFO"
    max_request_bytes: int = 32 * 1024 * 1024

    # --- Groq ---
    groq_api_key: SecretStr = SecretStr("")
    groq_model_planner: str = "qwen/qwen3.8-27b"
    groq_model_refiner: str = "qwen/qwen3.8-27b"
    groq_model_evidence_judge: str = "qwen/qwen3.8-27b"
    groq_model_answer: str = "qwen/qwen3.8-27b"
    groq_model_grounding_check: str = "qwen/qwen3.8-27b"
    groq_model_sql_spec: str = "qwen/qwen3.8-27b"
    groq_model_vision: str = "qwen/qwen3.8-27b"
    groq_vision_enabled: bool = True
    groq_timeout_seconds: float = Field(30.0, gt=0)
    groq_max_concurrency: int = Field(4, ge=1)
    groq_max_infra_retries: int = Field(3, ge=1, le=6)
    evidence_min_score: float = Field(0.35, ge=0.0, le=1.0)

    # --- Embeddings & Chroma (Codex) ---
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    chroma_path: str = "./.chroma"
    chunk_similarity_threshold: float = Field(0.72, ge=0.0, le=1.0)
    chunk_min_tokens: int = Field(80, ge=1)
    chunk_max_tokens: int = Field(450, ge=1)
    blob_path: str = "./.blobs"
    max_ingest_bytes: int = 25 * 1024 * 1024

    # --- Neon Postgres (Codex) ---
    neon_migration_url: SecretStr = SecretStr("")
    neon_ingest_url: SecretStr = SecretStr("")
    neon_query_url: SecretStr = SecretStr("")
    sql_timeout_ms: int = Field(5000, ge=100)
    sql_max_rows: int = Field(200, ge=1, le=1000)
    min_aggregation_group: int = Field(5, ge=2)

    def model_for(self, role: ModelRole) -> str:
        """Return the configured Groq model ID for an LLM role."""
        return {
            ModelRole.PLANNER: self.groq_model_planner,
            ModelRole.REFINER: self.groq_model_refiner,
            ModelRole.EVIDENCE_JUDGE: self.groq_model_evidence_judge,
            ModelRole.ANSWER: self.groq_model_answer,
            ModelRole.GROUNDING_CHECK: self.groq_model_grounding_check,
            ModelRole.SQL_SPEC: self.groq_model_sql_spec,
            ModelRole.VISION: self.groq_model_vision,
        }[role]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
