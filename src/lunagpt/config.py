"""LunaGPT configuration using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    # Gateway & Cloud API
    litellm_endpoint: str = "http://localhost:4000/v1/chat/completions"
    litellm_embeddings_endpoint: str = "http://localhost:4000/v1/embeddings"
    litellm_master_key: str = ""
    core_api_key: str = ""
    require_auth: bool = True
    max_upload_size_bytes: int = 50 * 1024 * 1024  # 50MB

    # Langfuse Observability
    langfuse_host: str = "http://localhost:3001"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # Dedicated PostgreSQL with pgvector for LunaGPT Core & Open WebUI
    lunagpt_database_url: str = "postgresql+psycopg://postgres@localhost:5433/lunagpt"
    openwebui_database_url: str = "postgresql+psycopg://postgres@localhost:5433/openwebui"
    openwebui_feedback_sync_interval_seconds: int = 300  # 0 disables the background sync

    # Luna AI Assistant (Telegram bot for the family)
    luna_telegram_bot_token: str = ""
    litellm_assistant_key: str = ""
    searxng_url: str = "http://localhost:8082"
    luna_pi_api_url: str = "http://192.168.88.2:8000/api/luna/v1"
    luna_pi_api_token: str = ""
    luna_utc_offset_hours: int = 5

    # Redis Cache
    redis_url: str = "redis://localhost:6380/0"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
