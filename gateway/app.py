import asyncio
import fcntl
import hmac
import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException

from gateway.config import Settings
from gateway.feishu import Feishu, Receiver, UpstreamError
from gateway.logging import configure
from gateway.store import Conflict, Store
from gateway.worker import Worker


class APIError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class TextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=10000)


class SendBody(TextBody):
    chat_id: str = Field(min_length=1, max_length=128, pattern=r"^oc_[A-Za-z0-9_-]+$")


class Delivery(BaseModel):
    delivery_id: str
    kind: str
    status: str
    attempts: int
    total_attempts: int
    created_at: float
    updated_at: float
    next_attempt_at: float
    last_error: str | None
    result_message_id: str | None


def public(row: dict[str, Any]) -> Delivery:
    return Delivery.model_validate(row)


Bearer = HTTPBearer(auto_error=False)
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
]


def create_app(
    settings: Settings, *, feishu: Any = None, receiver: Any = None, run_worker: bool = True
) -> FastAPI:
    configure(settings)
    store = Store(settings.data_dir)
    feishu = feishu if feishu is not None else Feishu(settings)
    receiver = receiver if receiver is not None else Receiver(settings)
    worker = Worker(store, settings, feishu)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock = (settings.data_dir / "gateway.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("Only one gateway process may use DATA_DIR") from None
        app.state.instance_lock = lock
        store.recover()
        receiver.start()
        if run_worker:
            worker.thread.start()

        async def supervise() -> None:
            while True:
                await asyncio.sleep(5)
                receiver.ensure_alive()

        supervisor = asyncio.create_task(supervise())
        app.state.stopping = False
        try:
            yield
        finally:
            app.state.stopping = True
            supervisor.cancel()
            with suppress(asyncio.CancelledError):
                await supervisor
            await asyncio.to_thread(receiver.close)
            await asyncio.to_thread(worker.close)
            # If a bounded shutdown expires, keep the process lock until process exit.
            if not worker.thread.is_alive():
                lock.close()

    app = FastAPI(
        title="Feishu Message Gateway",
        version=settings.code_version,
        lifespan=lifespan,
        responses={code: {"model": ErrorResponse} for code in (401, 404, 409, 422, 500, 503)},
    )
    app.state.store, app.state.worker, app.state.receiver = store, worker, receiver
    app.state.stopping = False

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(Bearer)],
    ) -> None:
        supplied = credentials.credentials if credentials else ""
        if not hmac.compare_digest(
            supplied.encode(), settings.api_access_token.get_secret_value().encode()
        ):
            raise APIError("unauthorized", 401)

    def error_response(code: str, status: int) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(
            status_code=status, content={"error": {"code": code, "message": code}}, headers=headers
        )

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        return error_response(exc.code, exc.status)

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict) -> JSONResponse:
        return error_response("idempotency_conflict", 409)

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response("invalid_request", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return error_response("http_" + str(exc.status_code), exc.status_code)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
        return error_response("storage_unavailable", 503)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger(__name__).error("request_failed type=%s", type(exc).__name__)
        return error_response("internal_error", 500)

    protected = [Depends(authenticate)]

    @app.post("/v1/messages", status_code=202, response_model=Delivery, dependencies=protected)
    def send(body: SendBody, idempotency_key: IdempotencyKey) -> Delivery:
        return public(
            store.enqueue("send", body.chat_id, "api:" + idempotency_key, body.model_dump())
        )

    @app.post(
        "/v1/messages/{message_id}/replies",
        status_code=202,
        response_model=Delivery,
        dependencies=protected,
    )
    def reply(message_id: str, body: TextBody, idempotency_key: IdempotencyKey) -> Delivery:
        if len(message_id) > 128 or not message_id.startswith("om_"):
            raise APIError("invalid_message_id", 422)
        existing = store.by_key("api:" + idempotency_key)
        if existing:
            return public(
                store.enqueue(
                    "reply",
                    existing["chat_id"],
                    "api:" + idempotency_key,
                    {"message_id": message_id, "text": body.text},
                )
            )
        chat_id = store.chat_for(message_id)
        if not chat_id:
            try:
                chat_id = feishu.chat_for(message_id)
            except UpstreamError as exc:
                raise APIError(exc.code, 503 if exc.retryable else 404) from None
            except Exception:
                raise APIError("upstream_unavailable", 503) from None
        return public(
            store.enqueue(
                "reply",
                chat_id,
                "api:" + idempotency_key,
                {"message_id": message_id, "text": body.text},
            )
        )

    @app.get("/v1/deliveries/{delivery_id}", response_model=Delivery, dependencies=protected)
    def delivery(delivery_id: str) -> Delivery:
        row = store.get(delivery_id)
        if row is None:
            raise APIError("delivery_not_found", 404)
        return public(row)

    @app.post(
        "/v1/deliveries/{delivery_id}/replay", response_model=Delivery, dependencies=protected
    )
    def replay(delivery_id: str) -> Delivery:
        if store.get(delivery_id) is None:
            raise APIError("delivery_not_found", 404)
        if not store.replay(delivery_id):
            raise APIError("delivery_not_dead", 409)
        row = store.get(delivery_id)
        assert row is not None
        return public(row)

    @app.get("/v1/status", dependencies=protected)
    def status() -> dict[str, Any]:
        return {
            "version": settings.code_version,
            "long_connection": receiver.connected,
            "worker_alive": worker.thread.is_alive(),
            **store.stats(),
        }

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "alive", "version": settings.code_version}

    @app.get("/readyz")
    def ready() -> dict[str, str]:
        with store.db() as db:
            db.execute("SELECT 1")
        if app.state.stopping or not receiver.connected or not worker.thread.is_alive():
            raise APIError("not_ready", 503)
        return {"status": "ready", "version": settings.code_version}

    return app
