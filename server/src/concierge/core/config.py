from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONCIERGE_", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://concierge@127.0.0.1:5432/concierge"
    qdrant_url: str = "http://127.0.0.1:6333"

    # Fernet key for encrypting secrets stored in `settings` (generate: `uv run python -c
    # "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
    secret_key: str = ""
    # HMAC key for signing access JWTs. MUST be overridden in production env.
    jwt_secret: str = "concierge-dev-insecure-jwt-secret-change-me"

    access_token_ttl_seconds: int = 900
    refresh_token_ttl_days: int = 30
    invite_ttl_hours: int = 72
    cookie_secure: bool = False

    # Content-addressed blob store for attachments (/srv/concierge/blobs on box).
    blob_dir: str = "data/blobs"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"


@lru_cache
def get_settings() -> Settings:
    return Settings()
