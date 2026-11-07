"""LunaGPT configuration using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    # Gateway & Cloud API
    litellm_endpoint: str = "http://localhost:4000/v1/chat/completions"
    litellm_embeddings_endpoint: str = "http://localhost:4000/v1/embeddings"
    litellm_master_key: str = "sk-luna-master-key-2026"

    # Langfuse Observability
    langfuse_host: str = "http://localhost:3001"
    langfuse_public_key: str = "pk-lf-luna-local-public-key"
    langfuse_secret_key: str = "sk-lf-luna-local-secret-key"

    # Database with pgvector
    database_url: str = "postgresql+asyncpg://postgres:lunagpt_secure_postgres_pass@localhost:5433/lunagpt"

    # Redis Cache
    redis_url: str = "redis://localhost:6380/0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
