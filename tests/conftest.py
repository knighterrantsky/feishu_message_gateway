import json
from queue import Empty, SimpleQueue

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.models import TokenRequest


@pytest.fixture
def settings():
    return Settings(
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret",
        gateway_admin_token="a" * 32,
        token_signing_key="s" * 32,
        _env_file=None,
    )


class FakeFeishu:
    def __init__(self):
        self.sent = []
        self.targets = {"om_original": "oc_test", "om_other": "oc_other"}
        self.error = None

    def send(self, kind, chat_id, target, text, stable_uuid):
        self.sent.append((kind, chat_id, target, text, stable_uuid))
        if self.error:
            raise self.error
        return "om_sent"

    def chat_for(self, message_id):
        return self.targets[message_id]


class FakeReceiver:
    connected = True
    restarts = 0

    def __init__(self):
        self.events = SimpleQueue()

    def start(self):
        pass

    def drain(self):
        events = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except Empty:
                return events

    def ensure_alive(self):
        pass

    def close(self):
        self.connected = False


@pytest.fixture
def service(settings):
    feishu, receiver = FakeFeishu(), FakeReceiver()
    app = create_app(settings, feishu=feishu, receiver=receiver)
    with TestClient(app) as client:
        yield client, app, feishu, receiver


def token(app, chats=None, ops=None, principal="client-a", ttl=3600):
    return app.state.auth.issue(
        TokenRequest(
            principal_id=principal,
            ttl_seconds=ttl,
            grants=[
                {
                    "app_id": "cli_test",
                    "chat_ids": ["oc_test"] if chats is None else chats,
                    "operations": ops
                    or ["events.receive", "messages.send", "messages.reply", "status.read"],
                }
            ],
        )
    ).access_token


def headers(access_token, key="key-1"):
    return {"Authorization": "Bearer " + access_token, "Idempotency-Key": key}


def rpc(ws, method, params=None, request_id="r1"):
    ws.send_json({"type": "req", "id": request_id, "method": method, "params": params or {}})
    return ws.receive_json()


def connect(ws, access_token):
    response = rpc(ws, "connect", {"token": access_token})
    assert response["ok"]
    return response["payload"]


def event(chat="oc_test", mid="om_original", kind="p2p", user="ou_user"):
    return {
        "schema": "2.0",
        "header": {
            "event_type": "im.message.receive_v1",
            "event_id": "ev_1",
            "app_id": "cli_test",
            "create_time": "1700000000000",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": user}},
            "message": {
                "message_id": mid,
                "chat_id": chat,
                "chat_type": kind,
                "message_type": "text",
                "content": json.dumps({"text": "你好"}),
                "create_time": "1700000000000",
                "mentions": [],
            },
        },
    }
