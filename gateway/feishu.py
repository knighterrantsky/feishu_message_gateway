import asyncio
import json
import logging
import multiprocessing
import signal
import time
from typing import Any

from gateway.config import Settings
from gateway.ingress import receive
from gateway.logging import configure
from gateway.store import Store


class UpstreamError(Exception):
    def __init__(self, code: str, retryable: bool = True):
        self.code, self.retryable = code, retryable
        super().__init__(code)


class Feishu:
    def __init__(self, settings: Settings):
        import lark_oapi as lark

        self.client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret.get_secret_value())
            .timeout(settings.request_timeout_seconds)
            .log_level(lark.LogLevel.ERROR)
            .build()
        )

    @staticmethod
    def check(response: Any) -> None:
        if not response.success():
            code = str(response.code)
            # Unknown failures remain retryable but bounded. No raw upstream text in logs/API.
            permanent = {"230001", "230002", "230006", "230013", "230015", "230017"}
            raise UpstreamError("feishu_" + code, code not in permanent)

    def bot_id(self) -> str:
        import lark_oapi as lark

        req = (
            lark.BaseRequest.builder()
            .http_method(lark.HttpMethod.GET)
            .uri("/open-apis/bot/v3/info")
            .token_types({lark.AccessTokenType.TENANT})
            .build()
        )
        response = self.client.request(req)
        self.check(response)
        return str(json.loads(response.raw.content)["bot"]["open_id"])

    def chat_for(self, message_id: str) -> str:
        from lark_oapi.api.im.v1 import GetMessageRequest

        response = self.client.im.v1.message.get(
            GetMessageRequest.builder().message_id(message_id).build()
        )
        self.check(response)
        if not response.data or not response.data.items:
            raise UpstreamError("message_not_found", False)
        return str(response.data.items[0].chat_id)

    def send(self, row: dict[str, Any]) -> str:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        payload = json.loads(row["payload"])
        content = json.dumps({"text": payload["text"]}, ensure_ascii=False)
        if row["kind"] == "send":
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(row["chat_id"])
                .msg_type("text")
                .content(content)
                .uuid(row["delivery_id"])
                .build()
            )
            response = self.client.im.v1.message.create(
                CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
            )
        else:
            reply_body = (
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(content)
                .uuid(row["delivery_id"])
                .build()
            )
            response = self.client.im.v1.message.reply(
                ReplyMessageRequest.builder()
                .message_id(payload["message_id"])
                .request_body(reply_body)
                .build()
            )
        self.check(response)
        return str(response.data.message_id)


def receiver_main(settings: Settings, connected: Any, activity: Any, stop: Any) -> None:
    """SDK owns a dedicated process/event loop; isolate its synchronous discovery calls."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    import lark_oapi as lark
    import lark_oapi.ws.client as ws_module

    ws_module.loop = loop
    configure(settings)
    store = Store(settings.data_dir)
    ws: Any = None

    def terminate(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    try:
        while not stop.is_set():
            try:
                bot_id = Feishu(settings).bot_id()
                break
            except Exception:
                logging.getLogger(__name__).warning("bot_identity_unavailable")
                stop.wait(5)
        else:
            return

        def callback(data: Any) -> None:
            # Synchronous transaction completes BEFORE official SDK emits success ACK.
            receive(json.loads(lark.JSON.marshal(data)), store, settings, bot_id)

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(callback)
            .build()
        )
        ws = lark.ws.Client(
            settings.feishu_app_id,
            settings.feishu_app_secret.get_secret_value(),
            event_handler=dispatcher,
            log_level=lark.LogLevel.ERROR,
        )

        async def monitor() -> None:
            while not stop.is_set():
                activity.value = time.monotonic()
                connected.value = time.monotonic() if ws._conn is not None else 0
                await asyncio.sleep(0.5)
            ws._auto_reconnect = False
            await ws._disconnect()
            loop.stop()

        loop.create_task(monitor())
        ws.start()
    except (Exception, SystemExit):
        logging.getLogger(__name__).info("receiver_stopped")
    finally:
        connected.value = 0
        if ws is not None:
            ws._auto_reconnect = False
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        if tasks:
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        if ws is not None:
            loop.run_until_complete(ws._disconnect())
        loop.close()


class Receiver:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.context = multiprocessing.get_context("spawn")
        self.heartbeat = self.context.Value("d", 0.0)
        self.activity = self.context.Value("d", 0.0)
        self.stop_event = self.context.Event()
        self.process: Any = None

    @property
    def connected(self) -> bool:
        return bool(
            self.process
            and self.process.is_alive()
            and 0 < time.monotonic() - self.heartbeat.value < 10
        )

    def start(self) -> None:
        self.stop_event.clear()
        self.activity.value = time.monotonic()
        self.process = self.context.Process(
            target=receiver_main,
            args=(self.settings, self.heartbeat, self.activity, self.stop_event),
            daemon=True,
        )
        self.process.start()

    def ensure_alive(self) -> None:
        if (
            self.process
            and self.process.is_alive()
            and not self.stop_event.is_set()
            and time.monotonic() - self.activity.value > 90
        ):
            # Official SDK endpoint discovery has no HTTP timeout; recover a stuck loop.
            self.process.terminate()
            self.process.join(3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()
        if self.process and not self.process.is_alive() and not self.stop_event.is_set():
            self.process.join()
            self.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.process:
            self.process.join(3)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join()
