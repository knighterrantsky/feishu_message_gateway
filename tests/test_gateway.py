import asyncio
import json
import threading
import time

import jwt
import pytest
from conftest import FakeFeishu, connect, event, headers, rpc, token
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from gateway.auth import Authorization
from gateway.commands import Commands
from gateway.config import Settings
from gateway.feishu import UpstreamError
from gateway.ingress import normalize
from gateway.models import APIError, Subscribe, TokenRequest
from gateway.relay import Relay


def principal(settings, **kwargs):
    auth = Authorization(settings)
    request = TokenRequest(
        principal_id=kwargs.pop("principal", "client-a"),
        grants=[
            {
                "app_id": "cli_test",
                "operations": kwargs.pop(
                    "ops", ["events.receive", "messages.send", "messages.reply"]
                ),
                "chat_ids": kwargs.pop("chats", ["oc_test"]),
            }
        ],
        **kwargs,
    )
    return auth.authenticate(auth.issue(request).access_token)


def subscription(sid="s1", **kwargs):
    return Subscribe(subscription_id=sid, filter={"app_id": "cli_test", **kwargs})


def test_admin_and_client_credentials_are_separate(service):
    client, app, _, _ = service
    body = {
        "principal_id": "client-a",
        "grants": [
            {"app_id": "cli_test", "operations": ["events.receive"], "chat_ids": ["oc_test"]}
        ],
    }
    assert client.post("/v1/tokens", json=body).status_code == 401
    assert client.post("/v1/tokens", json=body, headers=headers(token(app))).status_code == 401
    response = client.post("/v1/tokens", json=body, headers=headers("a" * 32))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert app.state.auth.authenticate(response.json()["access_token"]).id == "client-a"
    assert client.get("/v1/status", headers=headers("a" * 32)).status_code == 401


@pytest.mark.parametrize(
    "mutation", ["signature", "expiry", "audience", "issuer", "future", "algorithm"]
)
def test_invalid_tokens_are_rejected(service, mutation):
    client, app, _, _ = service
    original = token(app)
    claims = jwt.decode(original, options={"verify_signature": False})
    key, algorithm = "s" * 32, "HS256"
    if mutation == "signature":
        key = "x" * 32
    elif mutation == "expiry":
        claims["exp"] = int(time.time()) - 10
    elif mutation == "audience":
        claims["aud"] = "other"
    elif mutation == "issuer":
        claims["iss"] = "other"
    elif mutation == "future":
        claims["iat"] = int(time.time()) + 60
    else:
        algorithm, key = "HS384", "s" * 48
    access = jwt.encode(claims, key, algorithm=algorithm)
    response = client.get("/v1/status", headers=headers(access))
    assert response.status_code == 401
    assert access not in response.text
    assert response.headers["www-authenticate"] == "Bearer"


def test_issue_requires_explicit_scopes_and_bounds_ttl(service):
    client, _, _, _ = service
    body = {
        "principal_id": "a",
        "grants": [{"app_id": "cli_test", "operations": ["events.receive"]}],
    }
    assert client.post("/v1/tokens", headers=headers("a" * 32), json=body).status_code == 422
    body["grants"][0]["chat_ids"] = ["oc_test"]
    body["ttl_seconds"] = 86401
    assert client.post("/v1/tokens", headers=headers("a" * 32), json=body).status_code == 422
    body["ttl_seconds"] = 3600
    body["grants"][0]["app_id"] = "cli_other"
    assert client.post("/v1/tokens", headers=headers("a" * 32), json=body).status_code == 422


def test_send_reply_auth_idempotency_and_real_target(service):
    client, app, feishu, _ = service
    access = token(app)
    body = {"chat_id": "oc_test", "text": "你好", "session_id": "client-session"}
    assert client.post("/v1/messages", json=body).status_code == 401
    denied = token(app, ops=["events.receive"])
    assert client.post("/v1/messages", json=body, headers=headers(denied)).status_code == 403
    first = client.post("/v1/messages", json=body, headers=headers(access))
    second = client.post("/v1/messages", json=body, headers=headers(access))
    assert first.status_code == second.status_code == 200
    assert first.json() == {
        "status": "sent",
        "message_id": "om_sent",
        "session_id": "client-session",
    }
    assert len(feishu.sent) == 1
    assert len(feishu.sent[0][-1]) == 36
    assert (
        client.post(
            "/v1/messages", json={**body, "text": "changed"}, headers=headers(access)
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/v1/messages",
            json={**body, "chat_id": "oc_other"},
            headers=headers(access, "other-key"),
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/messages/om_other/replies", json={"text": "reply"}, headers=headers(access)
        ).status_code
        == 403
    )
    assert len(feishu.sent) == 1
    response = client.post(
        "/v1/messages/om_original/replies", json={"text": "reply"}, headers=headers(access)
    )
    assert response.status_code == 200
    assert feishu.sent[-1][0:3] == ("reply", "oc_test", "om_original")
    assert feishu.sent[-1][-1] != feishu.sent[0][-1]
    status = client.get("/v1/status", headers=headers(access)).json()
    assert status["outbound_completed"] == 2 and status["outbound_failed"] == 0
    assert status["last_outbound_result"]["status"] == "sent"
    assert "message_id" not in status["last_outbound_result"]


def test_cached_replies_recheck_new_narrower_grants(service):
    client, app, feishu, _ = service
    broad, narrow = token(app, chats=["*"]), token(app, chats=["oc_other"])
    url = "/v1/messages/om_original/replies"
    assert client.post(url, json={"text": "reply"}, headers=headers(broad)).status_code == 200
    assert client.post(url, json={"text": "reply"}, headers=headers(narrow)).status_code == 403
    assert len(feishu.sent) == 1


def test_validation_and_errors_do_not_echo_body_or_token(service):
    client, app, _, _ = service
    secret = "sensitive-content"
    response = client.post(
        "/v1/messages", json={"chat_id": "invalid", "text": secret}, headers=headers(token(app))
    )
    assert response.status_code == 422
    assert set(response.json()["error"]) == {"code", "message", "request_id"}
    assert secret not in response.text
    assert client.post("/v1/messages", content=b"x" * 65537).status_code == 413
    assert (
        client.post(
            "/v1/messages",
            json={"chat_id": "oc_test", "text": "hello"},
            headers={"Authorization": "Bearer " + token(app)},
        ).status_code
        == 422
    )
    assert client.get("/v1/deliveries/removed").status_code == 404


def test_health_readiness_and_protected_status(service):
    client, app, _, receiver = service
    assert client.get("/healthz").status_code == client.get("/readyz").status_code == 200
    assert (
        client.get("/v1/status", headers=headers(token(app, ops=["events.receive"]))).status_code
        == 403
    )
    status = client.get("/v1/status", headers=headers(token(app))).json()
    assert status["connections"] == 0 and status["buffered_bytes"] == 0
    assert "boot_id" in status and "version" in status
    receiver.connected = False
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200


def test_websocket_subscriptions_broadcast_and_connection_ownership(service, settings):
    client, app, _, receiver = service
    access = token(app)
    with client.websocket_connect("/v1/ws") as a, client.websocket_connect("/v1/ws") as b:
        ca, cb = connect(a, access), connect(b, token(app, principal="client-b"))
        assert ca["connection_id"] != cb["connection_id"]
        for ws in (a, b):
            result = rpc(
                ws, "subscribe", {"subscription_id": "s1", "filter": {"app_id": "cli_test"}}
            )
            assert result["ok"]
        assert rpc(
            a,
            "subscribe",
            {"subscription_id": "s2", "filter": {"app_id": "cli_test", "chat_ids": ["oc_test"]}},
        )["ok"]
        receiver.events.put(normalize(event(), settings, "ou_bot"))
        ea, eb = a.receive_json(), b.receive_json()
        assert ea["subscription_ids"] == ["s1", "s2"]
        assert eb["subscription_ids"] == ["s1"]
        assert ea["payload"]["message_id"] == eb["payload"]["message_id"] == "om_original"
        # No second event on A: the next response must be status, despite overlapping filters.
        assert rpc(a, "status.get")["payload"]["subscriptions"] == 3
        assert (
            rpc(b, "unsubscribe", {"subscription_id": "s2"})["error"]["code"]
            == "subscription_not_found"
        )
        assert rpc(a, "unsubscribe", {"subscription_id": "s2"})["ok"]
    with client.websocket_connect("/v1/ws") as fresh:
        new = connect(fresh, access)
        assert new["connection_id"] != ca["connection_id"]
        assert rpc(fresh, "status.get")["payload"]["subscriptions"] == 0


def test_websocket_unauthorized_scope_session_and_subscription_conflict(service):
    client, app, _, _ = service
    with client.websocket_connect("/v1/ws") as ws:
        connect(ws, token(app))
        denied = rpc(
            ws,
            "subscribe",
            {"subscription_id": "s", "filter": {"app_id": "cli_test", "chat_ids": ["oc_other"]}},
        )
        assert denied["error"]["code"] == "forbidden"
        params = {"subscription_id": "s", "filter": {"app_id": "cli_test"}}
        assert rpc(ws, "subscribe", params)["ok"]
        assert rpc(ws, "subscribe", params)["ok"]
        assert (
            rpc(
                ws,
                "subscribe",
                {**params, "filter": {"app_id": "cli_test", "chat_ids": ["oc_test"]}},
            )["error"]["code"]
            == "subscription_conflict"
        )
        assert (
            rpc(
                ws,
                "messages.send",
                {
                    "chat_id": "oc_other",
                    "text": "x",
                    "idempotency_key": "k",
                    "session_id": "oc_test",
                },
            )["error"]["code"]
            == "forbidden"
        )
        assert (
            rpc(
                ws,
                "messages.reply",
                {
                    "message_id": "om_other",
                    "text": "x",
                    "idempotency_key": "k",
                    "chat_id": "oc_test",
                },
            )["error"]["code"]
            == "invalid_request"
        )


def test_http_and_websocket_share_idempotency(service):
    client, app, feishu, _ = service
    access = token(app)
    with client.websocket_connect("/v1/ws") as ws:
        connect(ws, access)
        sent = rpc(
            ws, "messages.send", {"chat_id": "oc_test", "text": "hello", "idempotency_key": "same"}
        )
        assert sent["ok"]
        response = client.post(
            "/v1/messages",
            json={"chat_id": "oc_test", "text": "hello"},
            headers=headers(access, "same"),
        )
        assert response.status_code == 200
        assert len(feishu.sent) == 1


def test_websocket_requires_connect_first_and_expires_when_idle(service):
    client, app, _, _ = service
    with client.websocket_connect("/v1/ws") as ws:
        with pytest.raises(WebSocketDisconnect):
            rpc(ws, "status.get")
    with client.websocket_connect("/v1/ws") as ws:
        connect(ws, token(app, ttl=2))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.reason == "token_expired"


def test_omitted_filter_never_expands_grants_and_sender_filter_narrows(settings):
    relay = Relay(settings)
    a = relay.connect(principal(settings))
    relay.subscribe(a, subscription(sender_ids=["ou_user"]))
    for raw in (event(chat="oc_other"), event(user="ou_other")):
        relay.publish(normalize(raw, settings, "ou_bot"))
    assert a.queue.empty() and relay.unrouted == 2
    relay.publish(normalize(event(), settings, "ou_bot"))
    assert a.queue.qsize() == 1


def test_bounded_buffers_include_inflight_and_isolate_slow_connection(settings):
    settings.connection_buffer_messages = 1
    relay = Relay(settings)
    slow, fast = relay.connect(principal(settings)), relay.connect(principal(settings))
    relay.subscribe(slow, subscription())
    relay.subscribe(fast, subscription())
    payload = normalize(event(), settings, "ou_bot")
    relay.publish(payload)
    _, in_flight = slow.queue.get_nowait()
    _, fast_size = fast.queue.get_nowait()
    relay.release(fast, fast_size)
    relay.publish(payload)
    assert slow.closed.is_set() and slow.reason == "slow_consumer"
    assert not fast.closed.is_set() and fast.queue.qsize() == 1
    assert slow.buffered_bytes == in_flight
    relay.release(slow, in_flight)
    relay.disconnect(fast)
    assert relay.buffered_bytes == slow.buffered_messages == fast.buffered_messages == 0


def test_global_budget_and_disconnected_subscriptions_are_released(settings):
    settings.total_buffer_bytes = 1024
    relay = Relay(settings)
    connection = relay.connect(principal(settings))
    relay.subscribe(connection, subscription())
    assert not relay.enqueue(connection, {"payload": "x" * 1024})
    assert connection.reason == "buffer_capacity"
    assert not relay.connections and not connection.subscriptions and relay.buffered_bytes == 0


def test_subscription_and_connection_capacity(settings):
    settings.max_connections = settings.max_subscriptions_per_connection = 1
    relay = Relay(settings)
    c = relay.connect(principal(settings))
    relay.subscribe(c, subscription())
    with pytest.raises(APIError, match="subscription_limit"):
        relay.subscribe(c, subscription("s2"))
    with pytest.raises(APIError, match="connection_limit"):
        relay.connect(principal(settings))


def test_no_replay_no_gateway_files_and_duplicate_events_pass_through(
    settings, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    relay = Relay(settings)
    payload = normalize(event(), settings, "ou_bot")
    relay.publish(payload)
    a = relay.connect(principal(settings))
    relay.subscribe(a, subscription())
    assert a.queue.empty()
    relay.publish(payload)
    relay.publish(payload)
    frames = [json.loads(a.queue.get_nowait()[0]) for _ in range(2)]
    assert [f["seq"] for f in frames] == [1, 2]
    assert frames[0]["payload"]["event_id"] == frames[1]["payload"]["event_id"]
    assert list(tmp_path.iterdir()) == []


def test_idempotency_survives_restart_via_stable_provider_uuid(settings):
    async def scenario():
        feishu = FakeFeishu()
        p = principal(settings)
        for _ in range(2):
            cmd = Commands(settings, feishu)
            await cmd.execute(p, "send", "oc_test", "hello", "durable-client-key")
            await cmd.close()
        assert feishu.sent[0][-1] == feishu.sent[1][-1]
        other = Commands(settings, feishu)
        await other.execute(
            principal(settings, principal="different"),
            "send",
            "oc_test",
            "hello",
            "durable-client-key",
        )
        assert feishu.sent[2][-1] != feishu.sent[0][-1]
        await other.close()

    asyncio.run(scenario())


def test_cancellation_keeps_sdk_worker_reserved(settings):
    async def scenario():
        settings.max_outbound_requests = 1
        feishu = FakeFeishu()
        release = threading.Event()
        original = feishu.send

        def blocked(*args):
            release.wait(3)
            return original(*args)

        feishu.send = blocked
        cmd, p = Commands(settings, feishu), principal(settings)
        caller = asyncio.create_task(cmd.execute(p, "send", "oc_test", "hello", "key"))
        try:
            await asyncio.sleep(0.05)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert cmd.active == 1
            with pytest.raises(APIError, match="outbound_capacity"):
                await cmd.execute(p, "send", "oc_test", "other", "key2")
        finally:
            release.set()
            await cmd.close()
        assert cmd.active == 0 and len(feishu.sent) == 1

    asyncio.run(scenario())


def test_failed_sends_require_explicit_retry_and_reuse_uuid(settings):
    async def scenario():
        feishu = FakeFeishu()
        feishu.error = TimeoutError("sensitive provider response")
        cmd, p = Commands(settings, feishu), principal(settings)
        with pytest.raises(APIError, match="outcome_unknown"):
            await cmd.execute(p, "send", "oc_test", "private body", "key")
        assert len(feishu.sent) == 1
        outcome = next(iter(cmd.cache.values())).task.result()
        assert outcome.code == "outcome_unknown" and not isinstance(outcome, Exception)
        feishu.error = None
        assert (
            await cmd.execute(p, "send", "oc_test", "private body", "key")
        ).message_id == "om_sent"
        assert len(feishu.sent) == 2 and feishu.sent[0][-1] == feishu.sent[1][-1]
        await cmd.close()

    asyncio.run(scenario())


def test_expired_cache_pruned_and_capacity_is_bounded(settings):
    async def scenario():
        settings.idempotency_cache_entries = 1
        cmd, p = Commands(settings, FakeFeishu()), principal(settings)
        await cmd.execute(p, "send", "oc_test", "body", "k1")
        with pytest.raises(APIError, match="idempotency_capacity"):
            await cmd.execute(p, "send", "oc_test", "body", "k2")
        next(iter(cmd.cache.values())).expires = 0
        cmd.prune()
        assert not cmd.cache
        await cmd.execute(p, "send", "oc_test", "body", "k2")
        await cmd.close()

    asyncio.run(scenario())


def test_upstream_rejection_is_sanitized(service):
    client, app, feishu, _ = service
    feishu.error = UpstreamError("feishu_230001", False)
    response = client.post(
        "/v1/messages", headers=headers(token(app)), json={"chat_id": "oc_test", "text": "hello"}
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "feishu_230001"


def test_ingress_group_mentions_allowlists_and_original_source(settings):
    raw = event(kind="group")
    assert normalize(raw, settings, "ou_bot") is None
    raw["event"]["message"]["mentions"] = [{"id": {"open_id": "ou_other"}}]
    assert normalize(raw, settings, "ou_bot") is None
    raw["event"]["message"]["mentions"] = [{"id": {"open_id": "ou_bot"}}]
    result = normalize(raw, settings, "ou_bot")
    assert result["conversation_id"] == "feishu:cli_test:oc_test" and result["text"] == "你好"
    settings.user_allowlist = "ou_other"
    assert normalize(raw, settings, "ou_bot") is None
    settings.user_allowlist = "ou_user"
    settings.chat_allowlist = "oc_other"
    assert normalize(raw, settings, "ou_bot") is None
    settings.chat_allowlist = "oc_test"
    assert normalize(raw, settings, "ou_bot") is not None
    raw["event"]["message"]["message_type"] = "image"
    assert normalize(raw, settings, "ou_bot") is None


def test_configuration_rejects_weak_keys_without_exposing_secrets(settings):
    values = settings.model_dump()
    values["gateway_admin_token"] = "too-short"
    with pytest.raises(ValidationError) as exc:
        Settings(**values, _env_file=None)
    assert "too-short" not in str(exc.value)


def test_log_redactor_removes_secrets_and_exception_bodies(settings):
    import logging

    from gateway.logging import Redactor

    record = logging.LogRecord(
        "gateway", logging.ERROR, __file__, 0, "failure %s", ("private-token",), None
    )
    record.exc_info = (ValueError, ValueError("private-message-body"), None)
    record.exc_text = "private-message-body"
    assert Redactor(["private-token"]).filter(record)
    assert record.getMessage() == "failure [REDACTED]"
    assert record.exc_info is None and record.exc_text is None
