from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    feishu_app_id: str = Field(min_length=1, max_length=128)
    feishu_app_secret: SecretStr
    gateway_admin_token: SecretStr
    token_signing_key: SecretStr
    token_issuer: str = "feishu-message-gateway"
    token_audience: str = "feishu-message-gateway-clients"
    token_max_ttl_seconds: int = Field(default=86400, ge=1, le=604800)
    user_allowlist: str = ""
    chat_allowlist: str = ""
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    request_timeout_seconds: float = Field(default=15, ge=0.1, le=60)
    shutdown_timeout_seconds: float = Field(default=25, ge=2, le=120)
    max_connections: int = Field(default=100, ge=1, le=10000)
    max_subscriptions_per_connection: int = Field(default=16, ge=1, le=100)
    max_message_bytes: int = Field(default=65536, ge=1024, le=1048576)
    connection_buffer_messages: int = Field(default=100, ge=1, le=10000)
    connection_buffer_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    total_buffer_bytes: int = Field(default=33554432, ge=1024, le=1073741824)
    ws_write_timeout_seconds: float = Field(default=5, gt=0, le=60)
    ws_auth_timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_outbound_requests: int = Field(default=16, ge=1, le=100)
    idempotency_cache_entries: int = Field(default=10000, ge=1, le=1000000)
    code_version: str = "dev"

    @field_validator("gateway_admin_token", "token_signing_key")
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

    @model_validator(mode="after")
    def bounds(self) -> "Settings":
        if self.shutdown_timeout_seconds <= self.request_timeout_seconds:
            raise ValueError("shutdown timeout must exceed request timeout")
        if self.gateway_admin_token == self.token_signing_key:
            raise ValueError("administration and signing keys must differ")
        return self

    @property
    def users(self) -> set[str]:
        return {x.strip() for x in self.user_allowlist.split(",") if x.strip()}

    @property
    def chats(self) -> set[str]:
        return {x.strip() for x in self.chat_allowlist.split(",") if x.strip()}
