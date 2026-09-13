import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

from anyio import CancelScope
from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway.auth import Authorization, Principal
from gateway.commands import Commands
from gateway.config import Settings
from gateway.feishu import Feishu, Receiver
from gateway.logging import configure
from gateway.models import (
    APIError,
    Connect,
    ErrorResponse,
    ReplyRPC,
    RPCFrame,
    SendBody,
    SendResult,
    SendRPC,
    Subscribe,
    TextBody,
    TokenRequest,
    TokenResponse,
    Unsubscribe,
)
from gateway.relay import Connection, Relay

Bearer = HTTPBearer(auto_error=False)
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
]


def error_body(code: str, request_id: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": code, "request_id": request_id}}


class BodyLimit:
    def __init__(self, app: ASGIApp, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})["request_id"] = str(uuid.uuid4())
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.limit:
                response = JSONResponse(
                    error_body("request_too_large", scope["state"]["request_id"]),
                    status_code=413,
                )
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        consumed = False

        async def replay() -> Any:
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings, *, feishu: Any = None, receiver: Any = None) -> FastAPI:
    configure(settings)
    auth = Authorization(settings)
    feishu = feishu if feishu is not None else Feishu(settings)
    receiver = receiver if receiver is not None else Receiver(settings)
    relay = Relay(settings)
    commands = Commands(settings, feishu)
    boot_id = str(uuid.uuid4())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        receiver.start()
        app.state.stopping = False

        async def pump() -> None:
            ticks = 0
            while True:
                for event in receiver.drain():
                    relay.publish(event)
                ticks += 1
                if ticks >= 100:
                    receiver.ensure_alive()
                    commands.prune()
                    ticks = 0
                await asyncio.sleep(0.05)

        task = asyncio.create_task(pump())
        app.state.pump = task
        try:
            yield
        finally:
            app.state.stopping = True
            commands.stopping = True
            for connection in tuple(relay.connections):
                relay.disconnect(connection, "server_shutdown")
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.to_thread(receiver.close)
            await commands.close()

    app = FastAPI(
        title="Feishu Realtime Relay",
        version=settings.code_version,
        lifespan=lifespan,
        description="Best-effort live events over /v1/ws; no durable messages or offline replay.",
        responses={
            code: {"model": ErrorResponse}
            for code in (401, 403, 404, 409, 413, 422, 429, 500, 502, 503, 504)
        },
    )
    app.add_middleware(BodyLimit, limit=settings.max_message_bytes)
    app.state.relay, app.state.commands, app.state.auth = relay, commands, auth
    app.state.receiver, app.state.stopping = receiver, False

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(Bearer)],
    ) -> Principal:
        return auth.authenticate(credentials.credentials if credentials else "")

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            error_body(exc.code, request.state.request_id), status_code=exc.status, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            error_body("invalid_request", request.state.request_id), status_code=422
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            error_body("http_" + str(exc.status_code), request.state.request_id),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger(__name__).error("request_failed type=%s", type(exc).__name__)
        return JSONResponse(error_body("internal_error", request.state.request_id), status_code=500)

    @app.post("/v1/tokens", response_model=TokenResponse)
    def issue(
        body: TokenRequest,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(Bearer)],
    ) -> JSONResponse:
        auth.admin(credentials.credentials if credentials else "")
        token = auth.issue(body)
        return JSONResponse(token.model_dump(), headers={"Cache-Control": "no-store"})

    @app.post("/v1/messages", response_model=SendResult)
    async def send(
        body: SendBody,
        idempotency_key: IdempotencyKey,
        principal: Annotated[Principal, Depends(authenticate)],
    ) -> SendResult:
        return await commands.execute(
            principal, "send", body.chat_id, body.text, idempotency_key, body.session_id
        )

    @app.post("/v1/messages/{message_id}/replies", response_model=SendResult)
    async def reply(
        message_id: str,
        body: TextBody,
        idempotency_key: IdempotencyKey,
        principal: Annotated[Principal, Depends(authenticate)],
    ) -> SendResult:
        if len(message_id) > 128 or not message_id.startswith("om_"):
            raise APIError("invalid_message_id", 422)
        return await commands.execute(
            principal, "reply", message_id, body.text, idempotency_key, body.session_id
        )

    def status_payload() -> dict[str, Any]:
        return {
            "version": settings.code_version,
            "boot_id": boot_id,
            "long_connection": receiver.connected,
            "receiver_restarts": receiver.restarts,
            "outbound_active": commands.active,
            "outbound_completed": commands.completed,
            "outbound_failed": commands.failed,
            "last_outbound_result": commands.last_result,
            **relay.stats(),
        }

    @app.get("/v1/status")
    async def status(principal: Annotated[Principal, Depends(authenticate)]) -> dict[str, Any]:
        principal.require("status.read", settings.feishu_app_id)
        return status_payload()

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "alive", "version": settings.code_version}

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        if app.state.stopping or app.state.pump.done() or not receiver.connected:
            raise APIError("not_ready", 503)
        return {"status": "ready", "version": settings.code_version}

    async def rpc(connection: Connection, frame: RPCFrame) -> Any:
        connection.principal.active()
        if frame.method == "subscribe":
            body = Subscribe.model_validate(frame.params)
            relay.subscribe(connection, body)
            return {"subscription_id": body.subscription_id, "filter": body.filter.model_dump()}
        if frame.method == "unsubscribe":
            sid = Unsubscribe.model_validate(frame.params).subscription_id
            if sid not in connection.subscriptions:
                raise APIError("subscription_not_found", 404)
            del connection.subscriptions[sid]
            return {"subscription_id": sid}
        if frame.method == "messages.send":
            sb = SendRPC.model_validate(frame.params)
            return (
                await commands.execute(
                    connection.principal,
                    "send",
                    sb.chat_id,
                    sb.text,
                    sb.idempotency_key,
                    sb.session_id,
                )
            ).model_dump()
        if frame.method == "messages.reply":
            rb = ReplyRPC.model_validate(frame.params)
            return (
                await commands.execute(
                    connection.principal,
                    "reply",
                    rb.message_id,
                    rb.text,
                    rb.idempotency_key,
                    rb.session_id,
                )
            ).model_dump()
        if frame.method == "status.get":
            if frame.params:
                raise APIError("invalid_request", 422)
            connection.principal.require("status.read", settings.feishu_app_id)
            return status_payload()
        raise APIError("unknown_method", 404)

    @app.websocket("/v1/ws")
    async def websocket(ws: WebSocket) -> None:
        # Limits unauthenticated sockets as well as established connections.
        pending = getattr(app.state, "ws_count", 0)
        if app.state.stopping or pending >= settings.max_connections:
            await ws.close(code=1013)
            return
        app.state.ws_count = pending + 1
        connection: Connection | None = None
        tasks: list[asyncio.Task[Any]] = []

        async def receive_frame() -> RPCFrame:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            raw = message.get("text")
            if raw is None or len(raw.encode()) > settings.max_message_bytes:
                raise APIError("invalid_frame", 422)
            return RPCFrame.model_validate_json(raw)

        try:
            await ws.accept()
            async with asyncio.timeout(settings.ws_auth_timeout_seconds):
                initial = await receive_frame()
                if initial.method != "connect":
                    raise APIError("connect_required", 401)
                token = Connect.model_validate(initial.params).token
                principal = auth.authenticate(token)
                connection = relay.connect(principal)
                await ws.send_json(
                    {
                        "type": "res",
                        "id": initial.id,
                        "ok": True,
                        "payload": {
                            "connection_id": connection.id,
                            "boot_id": boot_id,
                            "protocol_version": 1,
                            "expires_at": principal.expires_at,
                        },
                    }
                )

            async def writer(c: Connection) -> None:
                while True:
                    data, size = await c.queue.get()
                    try:
                        c.principal.active()
                        await asyncio.wait_for(
                            ws.send_text(data), settings.ws_write_timeout_seconds
                        )
                    except TimeoutError:
                        relay.slow_consumers += 1
                        relay.dropped += 1
                        relay.disconnect(c, "slow_consumer")
                        return
                    finally:
                        relay.release(c, size)

            async def reader(c: Connection) -> None:
                while True:
                    frame = await receive_frame()
                    try:
                        result = await rpc(c, frame)
                        response = {"type": "res", "id": frame.id, "ok": True, "payload": result}
                    except (ValidationError, APIError) as exc:
                        code = exc.code if isinstance(exc, APIError) else "invalid_request"
                        response = {
                            "type": "res",
                            "id": frame.id,
                            "ok": False,
                            "error": error_body(code, frame.id)["error"],
                        }
                    if not relay.enqueue(c, response):
                        return

            async def expiry(c: Connection) -> None:
                await asyncio.sleep(max(0, c.principal.expires_at - time.time()))
                relay.disconnect(c, "token_expired")

            tasks = [
                asyncio.create_task(writer(connection)),
                asyncio.create_task(reader(connection)),
                asyncio.create_task(expiry(connection)),
                asyncio.create_task(connection.closed.wait()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, ValidationError, APIError, TimeoutError):
            pass
        except Exception as exc:
            logging.getLogger(__name__).error("websocket_failed type=%s", type(exc).__name__)
        finally:
            with CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                if connection is not None:
                    relay.disconnect(connection)
                app.state.ws_count -= 1
                with suppress(Exception):
                    await asyncio.wait_for(
                        ws.close(
                            code=1008,
                            reason=connection.reason if connection else "authentication_failed",
                        ),
                        1,
                    )

    return app
