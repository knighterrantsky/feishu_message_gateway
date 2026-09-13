import hmac
import time
import uuid
from dataclasses import dataclass

import jwt
from pydantic import ValidationError

from gateway.config import Settings
from gateway.models import APIError, Grant, Operation, TokenRequest, TokenResponse


@dataclass(frozen=True)
class Principal:
    id: str
    grants: tuple[Grant, ...]
    expires_at: int

    def active(self) -> None:
        if time.time() >= self.expires_at:
            raise APIError("token_expired", 401)

    def allows(self, op: Operation, app_id: str, chat_id: str | None = None) -> bool:
        self.active()
        return any(
            g.app_id == app_id
            and op in g.operations
            and (chat_id is None or "*" in g.chat_ids or chat_id in g.chat_ids)
            for g in self.grants
        )

    def require(self, op: Operation, app_id: str, chat_id: str | None = None) -> None:
        if not self.allows(op, app_id, chat_id):
            raise APIError("forbidden", 403)


class Authorization:
    def __init__(self, settings: Settings):
        self.settings = settings

    def admin(self, token: str) -> None:
        if not hmac.compare_digest(
            token.encode(), self.settings.gateway_admin_token.get_secret_value().encode()
        ):
            raise APIError("unauthorized", 401)

    def issue(self, request: TokenRequest) -> TokenResponse:
        if request.ttl_seconds > self.settings.token_max_ttl_seconds:
            raise APIError("token_ttl_exceeded", 422)
        if any(g.app_id != self.settings.feishu_app_id for g in request.grants):
            raise APIError("unknown_app_id", 422)
        now = int(time.time())
        expires = now + request.ttl_seconds
        token = jwt.encode(
            {
                "sub": request.principal_id,
                "iss": self.settings.token_issuer,
                "aud": self.settings.token_audience,
                "iat": now,
                "exp": expires,
                "jti": str(uuid.uuid4()),
                "grants": [g.model_dump() for g in request.grants],
            },
            self.settings.token_signing_key.get_secret_value(),
            algorithm="HS256",
        )
        if len(token) > 16384:
            raise APIError("token_too_large", 422)
        return TokenResponse(access_token=token, expires_at=expires)

    def authenticate(self, token: str) -> Principal:
        if not token or len(token) > 16384:
            raise APIError("unauthorized", 401)
        try:
            claims = jwt.decode(
                token,
                self.settings.token_signing_key.get_secret_value(),
                algorithms=["HS256"],
                issuer=self.settings.token_issuer,
                audience=self.settings.token_audience,
                options={"require": ["exp", "iat", "sub", "jti", "grants"]},
            )
            grants = tuple(Grant.model_validate(g) for g in claims["grants"])
            if not grants or len(grants) > 16 or not claims["sub"]:
                raise ValueError("invalid claims")
            if not isinstance(claims["exp"], int) or not isinstance(claims["iat"], int):
                raise ValueError("invalid time")
            if claims["exp"] - claims["iat"] > self.settings.token_max_ttl_seconds:
                raise ValueError("invalid lifetime")
            principal = Principal(claims["sub"], grants, claims["exp"])
            principal.active()
            return principal
        except jwt.ExpiredSignatureError:
            raise APIError("token_expired", 401) from None
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError, ValidationError):
            raise APIError("unauthorized", 401) from None
