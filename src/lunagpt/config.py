"""LunaGPT configuration using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings."""

    # Gateway & Cloud API
    litellm_endpoint: str = "http://localhost:4000/v1/chat/completions"
    litellm_embeddings_endpoint: str = "http://localhost:4000/v1/embeddings"
    litellm_master_key: str = ""
    require_auth: bool = True
    max_upload_size_bytes: int = 50 * 1024 * 1024  # 50MB

    # PostgreSQL with pgvector: Luna's data
    lunagpt_database_url: str = "postgresql+psycopg://postgres@localhost:5433/lunagpt"

    # Luna AI Assistant (Telegram bot for the family)
    luna_telegram_bot_token: str = ""
    litellm_assistant_key: str = ""
    searxng_url: str = "http://localhost:8082"
    luna_pi_api_url: str = "http://192.168.88.2:8000/api/luna/v1"
    luna_pi_api_token: str = ""
    luna_utc_offset_hours: int = 5
    luna_phone_region: str = "UZ"
    # The service account's JSON key, on one line; empty keeps Google Calendar off.
    luna_google_service_account: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
