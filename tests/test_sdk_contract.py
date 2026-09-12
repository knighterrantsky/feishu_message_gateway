"""Exercise the installed official SDK's parsing, ACK and request builders offline."""

import asyncio
import json
from types import SimpleNamespace

import lark_oapi as lark
from lark_oapi.ws.const import (
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)
from lark_oapi.ws.pb.pbbp2_pb2 import Frame
from test_gateway import event

from gateway.feishu import Feishu
from gateway.ingress import receive
from gateway.store import Store


def test_sdk_ack_after_commit_and_failure_ack(settings, monkeypatch):
    store = Store(settings.data_dir)
    raw = event()
    raw["schema"] = "2.0"
    raw["header"]["event_type"] = "im.message.receive_v1"
    raw["header"]["create_time"] = "1700000000000"

    def callback(data):
        receive(json.loads(lark.JSON.marshal(data)), store, settings, "ou_bot")

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(callback)
        .build()
    )
    client = lark.ws.Client("cli_test", "secret", event_handler=dispatcher)
    responses = []

    async def capture(data):
        frame = Frame()
        frame.ParseFromString(data)
        response = json.loads(frame.payload)
        if response["code"] == 200:
            assert store.stats()["queue_count"] == 1
        responses.append(response["code"])

    monkeypatch.setattr(client, "_write_message", capture)

    def frame():
        f = Frame()
        f.SeqID = 0
        f.LogID = 0
        f.service = 1
        f.method = 1
        for key, value in [
            (HEADER_MESSAGE_ID, "transport"),
            (HEADER_TRACE_ID, "trace"),
            (HEADER_SUM, "1"),
            (HEADER_SEQ, "0"),
            (HEADER_TYPE, "event"),
        ]:
            header = f.headers.add()
            header.key, header.value = key, value
        f.payload = json.dumps(raw).encode()
        return f

    asyncio.run(client._handle_data_frame(frame()))
    assert responses == [200]

    def fail(*args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(store, "enqueue", fail)
    asyncio.run(client._handle_data_frame(frame()))
    assert responses == [200, 500]


def test_sdk_send_reply_builders_and_uuid(settings):
    feishu = Feishu(settings)
    requests = []

    def capture(request):
        requests.append(request)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_sent"))

    feishu.client.im.v1.message.create = capture
    feishu.client.im.v1.message.reply = capture
    row = {
        "delivery_id": "stable-uuid",
        "chat_id": "oc_test",
        "kind": "send",
        "payload": json.dumps({"text": "你好"}),
    }
    assert feishu.send(row) == "om_sent"
    assert requests[-1].body.uuid == "stable-uuid"
    assert requests[-1].body.receive_id == "oc_test"
    assert json.loads(requests[-1].body.content)["text"] == "你好"
    row.update(kind="reply", payload=json.dumps({"message_id": "om_original", "text": "reply"}))
    assert feishu.send(row) == "om_sent"
    assert requests[-1].body.uuid == "stable-uuid"
    assert requests[-1].paths["message_id"] == "om_original"
