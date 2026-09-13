"""Exercise the pinned official SDK's parsing, ACK and request builders offline."""

import asyncio
import json
import socket
import struct
import time
from types import SimpleNamespace
from unittest.mock import Mock

import lark_oapi as lark
import pytest
from conftest import event
from lark_oapi.ws.const import (
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)
from lark_oapi.ws.pb.pbbp2_pb2 import Frame

from gateway.feishu import Feishu, Receiver, forward_event


def test_sdk_ack_does_not_wait_for_client_receipt(settings, monkeypatch):
    receiver = Receiver(settings)
    receiver.input, output = socket.socketpair()
    receiver.input.setblocking(False)
    output.setblocking(False)

    def callback(data):
        forward_event(json.loads(lark.JSON.marshal(data)), settings, "ou_bot", output)

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(callback)
        .build()
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = lark.ws.Client("cli_test", "secret", event_handler=dispatcher)
    responses = []

    async def capture(data):
        frame = Frame()
        frame.ParseFromString(data)
        responses.append(json.loads(frame.payload)["code"])

    monkeypatch.setattr(client, "_write_message", capture)
    f = Frame()
    f.SeqID = f.LogID = 0
    f.service = f.method = 1
    for key, value in [
        (HEADER_MESSAGE_ID, "transport"),
        (HEADER_TRACE_ID, "trace"),
        (HEADER_SUM, "1"),
        (HEADER_SEQ, "0"),
        (HEADER_TYPE, "event"),
    ]:
        header = f.headers.add()
        header.key, header.value = key, value
    f.payload = json.dumps(event()).encode()
    try:
        loop.run_until_complete(client._handle_data_frame(f))
        assert responses == [200]  # No main-process drain or downstream connection yet.
        normalized = receiver.drain()
        assert normalized[0]["message_id"] == "om_original"
        assert normalized[0]["text"] == "你好"
    finally:
        output.close()
        receiver.close()
        client._cache._cron.cancel()
        loop.run_until_complete(asyncio.gather(client._cache._cron, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


def test_sdk_send_reply_builders_and_uuid(settings):
    feishu = Feishu(settings)
    requests = []

    def capture(request):
        requests.append(request)
        return SimpleNamespace(success=lambda: True, data=SimpleNamespace(message_id="om_sent"))

    feishu.client.im.v1.message.create = capture
    feishu.client.im.v1.message.reply = capture
    assert feishu.send("send", "oc_test", "oc_test", "你好", "stable-uuid") == "om_sent"
    assert requests[-1].body.uuid == "stable-uuid"
    assert requests[-1].body.receive_id == "oc_test"
    assert json.loads(requests[-1].body.content)["text"] == "你好"
    assert feishu.send("reply", "oc_test", "om_original", "reply", "stable-reply-uuid") == "om_sent"
    assert requests[-1].body.uuid == "stable-reply-uuid"
    assert requests[-1].paths["message_id"] == "om_original"


def test_sdk_get_message_checks_actual_chat_and_missing_success_id(settings):
    feishu = Feishu(settings)
    requests = []

    def capture(request):
        requests.append(request)
        return SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(items=[SimpleNamespace(chat_id="oc_actual")])
        )

    feishu.client.im.v1.message.get = capture
    assert feishu.chat_for("om_original") == "oc_actual"
    assert requests[-1].paths["message_id"] == "om_original"
    feishu.client.im.v1.message.create = lambda request: SimpleNamespace(
        success=lambda: True, data=None
    )
    with pytest.raises(ValueError, match="missing_message_id"):
        feishu.send("send", "oc_test", "oc_test", "hi", "uuid")


def test_fragmented_ipc_order_and_disconnect_discards_partial_tail(settings):
    receiver = Receiver(settings)
    receiver.input, output = socket.socketpair()
    receiver.input.setblocking(False)
    packet = json.dumps({"message_id": "om_one"}).encode()
    framed = struct.pack("!I", len(packet)) + packet
    try:
        output.sendall(framed[:6])
        assert receiver.drain() == []
        output.sendall(framed[6:] + framed + framed[:7])
        assert receiver.drain() == [{"message_id": "om_one"}, {"message_id": "om_one"}]
        output.close()
        assert receiver.drain() == [] and receiver.buffer == b"" and receiver.input is None
    finally:
        output.close()
        receiver.close()


def test_sdk_ipc_overflow_closes_stream_instead_of_queuing(settings):
    partial = Mock()
    partial.send.return_value = 1
    with pytest.raises(SystemExit):
        forward_event(event(), settings, "ou_bot", partial)
    partial.send.side_effect = BlockingIOError()
    with pytest.raises(SystemExit):
        forward_event(event(), settings, "ou_bot", partial)
    assert partial.send.call_count == 2


def test_receiver_supervision_never_blocks_event_loop_on_join(settings, monkeypatch):
    receiver = Receiver(settings)
    receiver.process = Mock()
    receiver.process.is_alive.return_value = True
    receiver.activity.value = time.monotonic() - 100
    receiver.ensure_alive()
    receiver.process.terminate.assert_called_once()
    receiver.process.join.assert_not_called()
    receiver.terminating_at = time.monotonic() - 4
    receiver.ensure_alive()
    receiver.process.kill.assert_called_once()
    receiver.process.join.assert_not_called()
    receiver.process.is_alive.return_value = False
    start = Mock()
    monkeypatch.setattr(receiver, "start", start)
    receiver.ensure_alive()
    assert receiver.restarts == 1
    start.assert_called_once()
