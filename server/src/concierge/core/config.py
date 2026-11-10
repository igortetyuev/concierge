from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONCIERGE_", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://concierge@127.0.0.1:5432/concierge"
    qdrant_url: str = "http://127.0.0.1:6333"


@lru_cache
def get_settings() -> Settings:
    return Settings()
