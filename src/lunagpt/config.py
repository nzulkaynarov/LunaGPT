"""LunaGPT configuration using pydantic-settings."""

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    # Gateway & Cloud API
    litellm_endpoint: str = "http://localhost:4000/v1/chat/completions"
    litellm_embeddings_endpoint: str = "http://localhost:4000/v1/embeddings"
    litellm_master_key: str = os.getenv("LITELLM_MASTER_KEY", "sk-luna-master-key-2026")
    core_api_key: str = ""
    require_auth: bool = True
    max_upload_size_bytes: int = 50 * 1024 * 1024  # 50MB

    # Langfuse Observability
    langfuse_host: str = "http://localhost:3001"
    langfuse_public_key: str = os.getenv("LANGFUSE_PUBLIC_KEY", "pk-lf-luna-local-public-key")
    langfuse_secret_key: str = os.getenv("LANGFUSE_SECRET_KEY", "sk-lf-luna-local-secret-key")

    # Dedicated PostgreSQL with pgvector for LunaGPT Core & Open WebUI
    lunagpt_database_url: str = os.getenv(
        "LUNAGPT_DATABASE_URL",
        "postgresql+psycopg://postgres:lunagpt_secure_postgres_pass@localhost:5433/lunagpt",
    )
    openwebui_database_url: str = os.getenv(
        "OPENWEBUI_DATABASE_URL",
        "postgresql+psycopg://postgres:lunagpt_secure_postgres_pass@localhost:5433/openwebui",
    )

    # Redis Cache
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6380/0")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
