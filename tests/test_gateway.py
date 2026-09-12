import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from conftest import FakeFeishu, FakeReceiver
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gateway.app import create_app
from gateway.ingress import receive
from gateway.logging import Redactor
from gateway.signing import sign, verify
from gateway.store import Conflict, Store
from gateway.worker import Worker


def event(message_id="om_1", chat="oc_test", kind="p2p", mentions=None):
    return {
        "header": {"app_id": "cli_test", "event_id": "ev_" + message_id},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": message_id,
                "chat_id": chat,
                "chat_type": kind,
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
                "create_time": "1700000000000",
                "mentions": mentions or [],
            },
        },
    }


def due(store):
    with store.db() as db:
        db.execute("UPDATE deliveries SET next_attempt_at=0")


def test_ingress_filter_and_dedup(settings):
    store = Store(settings.data_dir)
    first = receive(event(), store, settings, "ou_bot")
    assert first == receive(event(), store, settings, "ou_bot")
    other_envelope = event()
    other_envelope["header"]["event_id"] = "different_event"
    assert receive(other_envelope, store, settings, "ou_bot") == first
    assert receive(event("om_g", kind="group"), store, settings, "ou_bot") is None
    mention = [{"id": {"open_id": "ou_other"}}]
    assert receive(event("om_g", kind="group", mentions=mention), store, settings, "ou_bot") is None
    mention = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}}]
    assert receive(event("om_g", kind="group", mentions=mention), store, settings, "ou_bot")
    settings.user_allowlist = "ou_different"
    assert receive(event("om_2"), store, settings, "ou_bot") is None
    settings.user_allowlist = "ou_user"
    settings.chat_allowlist = "oc_other"
    assert receive(event("om_2"), store, settings, "ou_bot") is None
    settings.chat_allowlist = "oc_test"
    assert receive(event("om_2"), store, settings, "ou_bot")
    wrong_app = event("om_3")
    wrong_app["header"]["app_id"] = "cli_wrong"
    assert receive(wrong_app, store, settings, "ou_bot") is None


def test_concurrent_enqueue_and_conflicting_key(settings):
    store = Store(settings.data_dir)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(
            pool.map(lambda _: store.enqueue("send", "oc_test", "same", {"text": "x"}), range(20))
        )
    assert len({row["delivery_id"] for row in rows}) == 1
    with pytest.raises(Conflict):
        store.enqueue("send", "oc_test", "same", {"text": "changed"})


def test_retry_order_dead_replay_and_signature(settings):
    store = Store(settings.data_dir)
    ids = [
        receive(event("om_1"), store, settings, "ou_bot"),
        receive(event("om_2"), store, settings, "ou_bot"),
        receive(event("om_3", chat="oc_other"), store, settings, "ou_bot"),
    ]
    calls = []
    fail = True

    def respond(request):
        nonlocal fail
        headers = request.headers
        assert verify(
            "s" * 32,
            headers["X-Gateway-Timestamp"],
            headers["X-Gateway-Delivery-Id"],
            request.content,
            headers["X-Gateway-Signature"],
        )
        message = json.loads(request.content)["message_id"]
        calls.append(message)
        return httpx.Response(503 if fail and message == "om_1" else 204)

    worker = Worker(store, settings, FakeFeishu(), httpx.MockTransport(respond))
    assert worker.step()
    first = store.get(ids[0])
    assert first["status"] == "pending" and first["next_attempt_at"] > time.time()
    assert worker.step()  # different chat is not blocked
    assert calls == ["om_1", "om_3"]
    due(store)
    assert worker.step()
    assert store.get(ids[0])["status"] == "dead"
    assert not worker.step()  # dead head blocks later messages
    assert store.replay(ids[0])
    fail = False
    assert worker.step() and worker.step()
    assert calls == ["om_1", "om_3", "om_1", "om_1", "om_2"]
    assert store.get(ids[0])["total_attempts"] == 3
    worker.close()


def test_restart_recovers_claim_and_retains_uuid(settings):
    store = Store(settings.data_dir)
    row = store.enqueue("send", "oc_test", "key", {"text": "hello"})
    assert store.claim()["delivery_id"] == row["delivery_id"]
    restarted = Store(settings.data_dir)
    restarted.recover()
    fake = FakeFeishu()
    worker = Worker(restarted, settings, fake)
    worker.step()
    assert fake.sent[0]["delivery_id"] == row["delivery_id"]
    assert restarted.get(row["delivery_id"])["status"] == "succeeded"
    worker.close()


def test_send_and_reply_share_order(settings):
    store = Store(settings.data_dir)
    first = store.enqueue("send", "oc_test", "a", {"text": "a"})
    store.enqueue("reply", "oc_test", "b", {"text": "b", "message_id": "om_original"})
    assert store.claim()["kind"] == "send"
    assert store.claim() is None
    store.finish(first["delivery_id"])
    assert store.claim()["kind"] == "reply"


def test_api_auth_idempotency_errors_status(settings):
    fake, receiver = FakeFeishu(), FakeReceiver()
    app = create_app(settings, feishu=fake, receiver=receiver, run_worker=False)
    auth = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": "key1"}
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        assert (
            client.post("/v1/messages", json={"chat_id": "oc_test", "text": "hi"}).status_code
            == 401
        )
        first = client.post("/v1/messages", headers=auth, json={"chat_id": "oc_test", "text": "hi"})
        assert first.status_code == 202
        repeat = client.post(
            "/v1/messages", headers=auth, json={"chat_id": "oc_test", "text": "hi"}
        )
        assert repeat.json()["delivery_id"] == first.json()["delivery_id"]
        conflict = client.post(
            "/v1/messages", headers=auth, json={"chat_id": "oc_test", "text": "changed"}
        )
        assert conflict.status_code == 409
        invalid = client.post("/v1/messages", headers=auth, json={"chat_id": "oc_test", "text": ""})
        assert invalid.json() == {
            "error": {"code": "invalid_request", "message": "invalid_request"}
        }
        app.state.worker.step()
        result = client.get("/v1/deliveries/" + first.json()["delivery_id"], headers=auth)
        assert result.json()["status"] == "succeeded"
        auth["Idempotency-Key"] = "reply1"
        reply = client.post("/v1/messages/om_sent/replies", headers=auth, json={"text": "reply"})
        assert reply.status_code == 202
        app.state.worker.step()
        assert fake.sent[-1]["kind"] == "reply"
        assert client.get("/v1/status", headers=auth).json()["queue_count"] == 0
        assert client.get("/v1/deliveries/missing", headers=auth).status_code == 404
        assert client.get("/missing").json()["error"]["code"] == "http_404"
        schema = client.get("/openapi.json").json()
        assert schema["paths"]["/v1/messages"]["post"]["security"]


def test_readiness_and_graceful_shutdown(settings):
    receiver = FakeReceiver()
    app = create_app(settings, feishu=FakeFeishu(), receiver=receiver)
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 200
        receiver.connected = False
        assert client.get("/readyz").status_code == 503
    assert not app.state.worker.thread.is_alive()


def test_single_instance_lock(settings):
    one = create_app(settings, feishu=FakeFeishu(), receiver=FakeReceiver(), run_worker=False)
    two = create_app(settings, feishu=FakeFeishu(), receiver=FakeReceiver(), run_worker=False)
    with TestClient(one), pytest.raises(RuntimeError, match="Only one"):
        with TestClient(two):
            pass


def test_signature_tamper_and_expiry():
    now = str(int(time.time()))
    signature = sign("secret", now, "id", b"body")
    assert verify("secret", now, "id", b"body", signature)
    assert not verify("secret", now, "id2", b"body", signature)
    assert not verify("secret", now, "id", b"changed", signature)
    assert not verify("secret", "1", "id", b"body", sign("secret", "1", "id", b"body"))
    assert not verify("secret", "invalid", "id", b"body", signature)
    assert not verify("secret", now, "id", b"body", "非ASCII签名")


def test_secret_redaction():
    record = logging.LogRecord("gateway", logging.INFO, "", 0, "token=%s", ("secret",), None)
    Redactor(["secret"]).filter(record)
    assert record.getMessage() == "token=[REDACTED]"


def test_configuration_rejects_invalid(settings):
    config = settings.model_dump()
    for change in (
        {"port": 0},
        {"api_access_token": "short"},
        {"webhook_url": "file:///etc/passwd"},
        {"retry_base_seconds": 2, "retry_max_seconds": 1},
    ):
        with pytest.raises(ValidationError):
            type(settings)(**(config | change), _env_file=None)


@pytest.mark.parametrize(
    "status,expected",
    [(400, "dead"), (401, "dead"), (302, "dead"), (429, "pending"), (500, "pending")],
)
def test_http_retry_classification(settings, status, expected):
    store = Store(settings.data_dir)
    delivery_id = receive(event(), store, settings, "ou_bot")
    worker = Worker(
        store, settings, FakeFeishu(), httpx.MockTransport(lambda _: httpx.Response(status))
    )
    worker.step()
    assert store.get(delivery_id)["status"] == expected
    worker.close()


def test_transport_timeout_is_durable(settings):
    store = Store(settings.data_dir)
    delivery_id = receive(event(), store, settings, "ou_bot")

    def timeout(request):
        raise httpx.ReadTimeout("sensitive downstream url")

    worker = Worker(store, settings, FakeFeishu(), httpx.MockTransport(timeout))
    worker.step()
    row = store.get(delivery_id)
    assert row["status"] == "pending"
    assert row["last_error"] == "upstream_transport_error"
    worker.close()


def test_replay_api(settings):
    app = create_app(settings, feishu=FakeFeishu(), receiver=FakeReceiver(), run_worker=False)
    headers = {"Authorization": "Bearer " + "t" * 32}
    with TestClient(app) as client:
        row = app.state.store.enqueue("send", "oc_test", "dead", {"text": "x"})
        delivery_id = row["delivery_id"]
        url = f"/v1/deliveries/{delivery_id}/replay"
        assert client.post(url, headers=headers).status_code == 409
        app.state.store.finish(delivery_id, error="test_failure", dead=True)
        assert client.post(url).status_code == 401
        result = client.post(url, headers=headers)
        assert result.status_code == 200 and result.json()["status"] == "pending"


def test_reply_idempotency_survives_lookup_failure(settings):
    fake = FakeFeishu()
    app = create_app(settings, feishu=fake, receiver=FakeReceiver(), run_worker=False)
    headers = {"Authorization": "Bearer " + "t" * 32, "Idempotency-Key": "stable-reply"}
    with TestClient(app) as client:
        first = client.post("/v1/messages/om_unseen/replies", headers=headers, json={"text": "hi"})
        assert first.status_code == 202

        def unavailable(message_id):
            raise RuntimeError("upstream unavailable")

        fake.chat_for = unavailable
        repeat = client.post("/v1/messages/om_unseen/replies", headers=headers, json={"text": "hi"})
        assert repeat.status_code == 202
        assert repeat.json()["delivery_id"] == first.json()["delivery_id"]
