from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    quorum_mode: Literal["sim", "openai", "bedrock"] = "sim"

    database_url: str = "postgresql://root@localhost:26257/quorum?sslmode=disable"
    embedding_dim: int = 128

    openai_api_key: str | None = None

    aws_region: str = "us-east-1"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None

    audit_s3_bucket: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
