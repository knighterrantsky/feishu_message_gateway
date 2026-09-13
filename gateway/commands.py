import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gateway.auth import Principal
from gateway.config import Settings
from gateway.feishu import UpstreamError
from gateway.models import APIError, Operation, SendResult

# Keep this namespace stable across releases; callers persist their own key and principal.
NAMESPACE = uuid.UUID("0eea20e1-a54f-49c7-bce6-00f715885fee")


@dataclass(frozen=True)
class Outcome:
    value: str = ""
    code: str = ""
    status: int = 502

    def unwrap(self) -> str:
        if self.code:
            raise APIError(self.code, self.status)
        return self.value


@dataclass
class CachedCommand:
    fingerprint: str
    expires: float
    task: asyncio.Task[Outcome]


class Commands:
    def __init__(self, settings: Settings, feishu: Any):
        self.settings, self.feishu = settings, feishu
        self.cache: dict[tuple[str, str, str], CachedCommand] = {}
        self.workers: set[asyncio.Task[Outcome]] = set()
        self.active = 0
        self.stopping = False
        self.completed = 0
        self.failed = 0
        self.last_result: dict[str, Any] | None = None

    def prune(self) -> None:
        now = time.monotonic()
        self.cache = {k: v for k, v in self.cache.items() if v.expires > now or not v.task.done()}

    def start_call(
        self, function: Callable[..., str], *args: Any, sending: bool = False
    ) -> asyncio.Task[Outcome]:
        if self.stopping or self.active >= self.settings.max_outbound_requests:
            raise APIError("outbound_capacity", 503)
        # Reserve before scheduling. A cancelled caller never releases a still-running SDK call.
        self.active += 1

        async def run() -> Outcome:
            try:
                outcome = Outcome(value=await asyncio.to_thread(function, *args))
            except UpstreamError as exc:
                outcome = Outcome(
                    code=exc.code if sending else "reply_target_unavailable",
                    status=502 if sending else 404,
                )
            except Exception:
                outcome = Outcome(
                    code="outcome_unknown" if sending else "upstream_unavailable",
                    status=504 if sending else 503,
                )
            finally:
                self.active -= 1
            if sending:
                self.completed += 1
                self.failed += bool(outcome.code)
                self.last_result = {
                    "status": "error" if outcome.code else "sent",
                    "code": outcome.code or None,
                    "completed_at": int(time.time()),
                }
            return outcome

        task = asyncio.create_task(run())
        self.workers.add(task)
        task.add_done_callback(self.workers.discard)
        return task

    async def result(self, task: asyncio.Task[Outcome], *, sending: bool) -> str:
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(task), self.settings.request_timeout_seconds + 1
            )
        except TimeoutError:
            raise APIError(
                "outcome_unknown" if sending else "upstream_unavailable", 504 if sending else 503
            ) from None
        # Cache plain data, never exceptions/tracebacks retaining message bodies.
        return outcome.unwrap()

    async def execute(
        self,
        principal: Principal,
        kind: str,
        target: str,
        text: str,
        key: str,
        session_id: str | None = None,
    ) -> SendResult:
        operation: Operation = "messages.send" if kind == "send" else "messages.reply"
        principal.require(operation, self.settings.feishu_app_id)
        self.prune()
        cache_key = (principal.id, kind, key)
        fingerprint = hashlib.sha256(json.dumps([target, text]).encode()).hexdigest()
        # Authorize the real target on every request, even cached results with narrowed grants.
        if kind == "send":
            chat_id = target
        else:
            chat_id = await self.result(
                self.start_call(self.feishu.chat_for, target), sending=False
            )
        principal.require(operation, self.settings.feishu_app_id, chat_id)
        cached = self.cache.get(cache_key)
        if cached is not None and cached.fingerprint != fingerprint:
            raise APIError("idempotency_conflict", 409)
        if cached is not None and (not cached.task.done() or not cached.task.result().code):
            task = cached.task
        else:
            if cached is None and len(self.cache) >= self.settings.idempotency_cache_entries:
                raise APIError("idempotency_capacity", 503)
            stable_uuid = str(
                uuid.uuid5(
                    NAMESPACE, json.dumps([self.settings.feishu_app_id, principal.id, kind, key])
                )
            )
            # No automatic retry. A new explicit request may retry an error with the same UUID.
            task = self.start_call(
                self.feishu.send, kind, chat_id, target, text, stable_uuid, sending=True
            )
            expires = cached.expires if cached else time.monotonic() + 3600
            self.cache[cache_key] = CachedCommand(fingerprint, expires, task)
        message_id = await self.result(task, sending=True)
        return SendResult(message_id=message_id, session_id=session_id)

    async def close(self) -> None:
        self.stopping = True
        if self.workers:
            await asyncio.wait(self.workers, timeout=self.settings.shutdown_timeout_seconds)
        self.prune()
