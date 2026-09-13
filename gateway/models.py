from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Operation = Literal["events.receive", "messages.send", "messages.reply", "status.read"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Grant(StrictModel):
    app_id: str = Field(min_length=1, max_length=128)
    operations: list[Operation] = Field(min_length=1, max_length=4)
    chat_ids: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("chat_ids")
    @classmethod
    def chats_valid(cls, values: list[str]) -> list[str]:
        if any(v != "*" and (not v.startswith("oc_") or len(v) > 128) for v in values):
            raise ValueError("invalid chat scope")
        return sorted(set(values))

    @model_validator(mode="after")
    def message_scope_required(self) -> "Grant":
        if any(op != "status.read" for op in self.operations) and not self.chat_ids:
            raise ValueError("message operations require explicit chat scope")
        return self


class TokenRequest(StrictModel):
    principal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    grants: list[Grant] = Field(min_length=1, max_length=16)
    ttl_seconds: int = Field(default=3600, ge=1, le=604800)


class TokenResponse(StrictModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_at: int


class SubscriptionFilter(StrictModel):
    app_id: str = Field(min_length=1, max_length=128)
    event_types: list[Literal["message.received"]] = Field(
        default=["message.received"], min_length=1, max_length=1
    )
    chat_ids: list[str] | None = Field(default=None, min_length=1, max_length=128)
    sender_ids: list[str] | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("chat_ids", "sender_ids")
    @classmethod
    def identifiers_valid(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and any(not v or len(v) > 128 or v == "*" for v in values):
            raise ValueError("use omitted filter for all authorized resources")
        return sorted(set(values)) if values is not None else None


class Subscribe(StrictModel):
    subscription_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    filter: SubscriptionFilter


class Unsubscribe(StrictModel):
    subscription_id: str = Field(min_length=1, max_length=64)


class TextBody(StrictModel):
    text: str = Field(min_length=1, max_length=10000)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class SendBody(TextBody):
    chat_id: str = Field(min_length=1, max_length=128, pattern=r"^oc_[A-Za-z0-9_-]+$")


class SendRPC(SendBody):
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")


class ReplyRPC(TextBody):
    message_id: str = Field(min_length=1, max_length=128, pattern=r"^om_[A-Za-z0-9_-]+$")
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")


class Connect(StrictModel):
    token: str = Field(min_length=1, max_length=16384)


class RPCFrame(StrictModel):
    type: Literal["req"]
    id: str = Field(min_length=1, max_length=128)
    method: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class SendResult(StrictModel):
    status: Literal["sent"] = "sent"
    message_id: str
    session_id: str | None = None


class ErrorDetail(StrictModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(StrictModel):
    error: ErrorDetail


class APIError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)
