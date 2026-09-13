"""Use real Uvicorn/WebSocket sockets so ASGI test doubles do not hide framing bugs."""

import asyncio
import json
import socket
import threading
import time

import httpx
import uvicorn
from conftest import FakeFeishu, FakeReceiver, event
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from gateway.app import create_app
from gateway.ingress import normalize


def test_real_network_auth_event_reply_and_server_shutdown(settings, capsys):
    settings.log_level = "DEBUG"
    feishu, receiver = FakeFeishu(), FakeReceiver()
    app = create_app(settings, feishu=feishu, receiver=receiver)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
            ws="websockets",
            ws_max_size=settings.max_message_bytes,
            ws_max_queue=4,
            ws_per_message_deflate=False,
            timeout_graceful_shutdown=2,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            response = client.post(
                "/v1/tokens",
                headers={"Authorization": "Bearer " + "a" * 32},
                json={
                    "principal_id": "network-client",
                    "grants": [
                        {
                            "app_id": "cli_test",
                            "operations": ["events.receive", "messages.reply"],
                            "chat_ids": ["oc_test"],
                        }
                    ],
                },
            )
            assert response.status_code == 200
            access_token = response.json()["access_token"]

        async def scenario():
            async with (
                asyncio.timeout(8),
                connect(f"ws://127.0.0.1:{port}/v1/ws", proxy=None) as ws,
            ):
                await ws.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "auth",
                            "method": "connect",
                            "params": {"token": access_token},
                        }
                    )
                )
                assert json.loads(await ws.recv())["ok"]
                await ws.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "sub",
                            "method": "subscribe",
                            "params": {"subscription_id": "in", "filter": {"app_id": "cli_test"}},
                        }
                    )
                )
                assert json.loads(await ws.recv())["ok"]
                receiver.events.put(normalize(event(), settings, "ou_bot"))
                payload = json.loads(await ws.recv())["payload"]
                await ws.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": "reply",
                            "method": "messages.reply",
                            "params": {
                                "message_id": payload["message_id"],
                                "text": "收到",
                                "idempotency_key": "reply-1",
                            },
                        }
                    )
                )
                result = json.loads(await ws.recv())
                assert result["payload"]["message_id"] == "om_sent"
                server.should_exit = True
                try:
                    await ws.recv()
                    raise AssertionError("Server shutdown should close WebSocket")
                except ConnectionClosed:
                    pass

        asyncio.run(scenario())
        assert len(feishu.sent) == 1 and feishu.sent[0][2] == "om_original"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
    assert not thread.is_alive()
    assert not app.state.relay.connections and app.state.relay.buffered_bytes == 0
    output = capsys.readouterr().err
    assert access_token not in output and "收到" not in output
