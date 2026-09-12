from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    feishu_app_id: str = Field(min_length=1)
    feishu_app_secret: SecretStr
    webhook_url: str
    webhook_signing_secret: SecretStr
    api_access_token: SecretStr
    user_allowlist: str = ""
    chat_allowlist: str = ""
    data_dir: Path = Path("data")
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    retry_max_attempts: int = Field(default=8, ge=1, le=100)
    retry_base_seconds: float = Field(default=2, ge=0.1, le=3600)
    retry_max_seconds: float = Field(default=300, ge=0.1, le=86400)
    request_timeout_seconds: float = Field(default=15, ge=1, le=60)
    shutdown_timeout_seconds: float = Field(default=25, ge=2, le=120)
    code_version: str = "dev"

    @field_validator("api_access_token", "webhook_signing_secret")
    @classmethod
    def strong_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("must contain at least 32 characters")
        return value

    @field_validator("feishu_app_secret")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("webhook_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.fragment:
            raise ValueError("must be an HTTP(S) URL without credentials or fragment")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("invalid Webhook port")
        return value

    @model_validator(mode="after")
    def retry_bounds(self) -> Self:
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("RETRY_MAX_SECONDS must be >= RETRY_BASE_SECONDS")
        if self.shutdown_timeout_seconds <= self.request_timeout_seconds:
            raise ValueError("SHUTDOWN_TIMEOUT_SECONDS must exceed REQUEST_TIMEOUT_SECONDS")
        return self

    @property
    def users(self) -> set[str]:
        return {x.strip() for x in self.user_allowlist.split(",") if x.strip()}

    @property
    def chats(self) -> set[str]:
        return {x.strip() for x in self.chat_allowlist.split(",") if x.strip()}
