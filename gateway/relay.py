import asyncio
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from gateway.auth import Principal
from gateway.config import Settings
from gateway.models import APIError, Subscribe, SubscriptionFilter


@dataclass(eq=False)
class Connection:
    principal: Principal
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    subscriptions: dict[str, SubscriptionFilter] = field(default_factory=dict)
    queue: asyncio.Queue[tuple[str, int]] = field(default_factory=asyncio.Queue)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    buffered_bytes: int = 0
    buffered_messages: int = 0
    seq: int = 0
    reason: str = "connection_closed"


class Relay:
    """All mutation occurs on the API event loop; no persisted delivery state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.connections: set[Connection] = set()
        self.buffered_bytes = 0
        self.received = 0
        self.unrouted = 0
        self.dropped = 0
        self.slow_consumers = 0

    def connect(self, principal: Principal) -> Connection:
        principal.active()
        if len(self.connections) >= self.settings.max_connections:
            raise APIError("connection_limit", 429)
        connection = Connection(principal)
        self.connections.add(connection)
        return connection

    def subscribe(self, connection: Connection, request: Subscribe) -> None:
        f = request.filter
        connection.principal.require("events.receive", f.app_id)
        if f.chat_ids is not None:
            for chat_id in f.chat_ids:
                connection.principal.require("events.receive", f.app_id, chat_id)
        old = connection.subscriptions.get(request.subscription_id)
        if old is not None:
            if old != f:
                raise APIError("subscription_conflict", 409)
            return
        if len(connection.subscriptions) >= self.settings.max_subscriptions_per_connection:
            raise APIError("subscription_limit", 429)
        connection.subscriptions[request.subscription_id] = f

    def release(self, connection: Connection, size: int) -> None:
        connection.buffered_bytes -= size
        connection.buffered_messages -= 1
        self.buffered_bytes -= size

    def disconnect(self, connection: Connection, reason: str = "connection_closed") -> None:
        if connection.closed.is_set():
            return
        connection.reason = reason
        connection.closed.set()
        self.connections.discard(connection)
        connection.subscriptions.clear()
        while not connection.queue.empty():
            _, size = connection.queue.get_nowait()
            self.release(connection, size)
            self.dropped += 1

    def enqueue(self, connection: Connection, frame: dict[str, Any]) -> bool:
        if connection.closed.is_set():
            return False
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        size = len(data.encode())
        if (
            connection.buffered_messages >= self.settings.connection_buffer_messages
            or connection.buffered_bytes + size > self.settings.connection_buffer_bytes
        ):
            self.slow_consumers += 1
            self.dropped += 1
            self.disconnect(connection, "slow_consumer")
            return False
        if self.buffered_bytes + size > self.settings.total_buffer_bytes:
            self.dropped += 1
            self.disconnect(connection, "buffer_capacity")
            return False
        self.buffered_bytes += size
        connection.buffered_bytes += size
        connection.buffered_messages += 1
        connection.queue.put_nowait((data, size))
        return True

    def publish(self, event: dict[str, Any]) -> None:
        self.received += 1
        matched = False
        for connection in tuple(self.connections):
            try:
                allowed = connection.principal.allows(
                    "events.receive", event["app_id"], event["chat_id"]
                )
            except APIError:
                self.disconnect(connection, "token_expired")
                continue
            if not allowed:
                continue
            ids = [
                sid
                for sid, f in connection.subscriptions.items()
                if f.app_id == event["app_id"]
                and event["type"] in f.event_types
                and (f.chat_ids is None or event["chat_id"] in f.chat_ids)
                and (f.sender_ids is None or event["sender"].get("open_id") in f.sender_ids)
            ]
            if not ids:
                continue
            matched = True
            connection.seq += 1
            self.enqueue(
                connection,
                {
                    "type": "event",
                    "event": "message.received",
                    "seq": connection.seq,
                    "subscription_ids": sorted(ids),
                    "payload": event,
                },
            )
        if not matched:
            self.unrouted += 1

    def stats(self) -> dict[str, int]:
        return {
            "connections": len(self.connections),
            "subscriptions": sum(len(c.subscriptions) for c in self.connections),
            "buffered_bytes": self.buffered_bytes,
            "received": self.received,
            "unrouted": self.unrouted,
            "dropped": self.dropped,
            "slow_consumers": self.slow_consumers,
        }
