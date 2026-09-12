import logging
import random
import threading
import time
from typing import Any

import httpx

from gateway.config import Settings
from gateway.feishu import UpstreamError
from gateway.signing import sign
from gateway.store import Store


class Worker:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        feishu: Any,
        transport: httpx.BaseTransport | None = None,
    ):
        self.store, self.settings, self.feishu = store, settings, feishu
        self.http = httpx.Client(
            timeout=settings.request_timeout_seconds, follow_redirects=False, transport=transport
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name="delivery-worker", daemon=True)
        self.failed = False

    def step(self) -> bool:
        row = self.store.claim()
        if row is None:
            return False
        error, retryable, message_id = None, True, None
        try:
            if row["kind"] == "webhook":
                timestamp = str(int(time.time()))
                body = row["payload"].encode()
                with self.http.stream(
                    "POST",
                    self.settings.webhook_url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Gateway-Delivery-Id": row["delivery_id"],
                        "X-Gateway-Timestamp": timestamp,
                        "X-Gateway-Signature": sign(
                            self.settings.webhook_signing_secret.get_secret_value(),
                            timestamp,
                            row["delivery_id"],
                            body,
                        ),
                    },
                ) as response:
                    if not 200 <= response.status_code < 300:
                        error = f"webhook_http_{response.status_code}"
                        retryable = (
                            response.status_code in {408, 425, 429} or response.status_code >= 500
                        )
            else:
                message_id = self.feishu.send(row)
        except UpstreamError as exc:
            error, retryable = exc.code, exc.retryable
        except Exception:
            error = "upstream_transport_error"
        dead = bool(
            error and (not retryable or row["attempts"] >= self.settings.retry_max_attempts)
        )
        delay = min(
            self.settings.retry_max_seconds,
            self.settings.retry_base_seconds * 2 ** (row["attempts"] - 1),
        )
        self.store.finish(
            row["delivery_id"],
            error=error,
            dead=dead,
            delay=delay * random.uniform(0.8, 1.0),
            message_id=message_id,
        )
        logging.getLogger(__name__).info(
            "delivery=%s outcome=%s", row["delivery_id"], "dead" if dead else error or "succeeded"
        )
        return True

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                if not self.step():
                    self.stop.wait(0.2)
        except Exception:
            self.failed = True
            logging.getLogger(__name__).error("worker_storage_failure")

    def close(self) -> None:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(self.settings.shutdown_timeout_seconds)
        if not self.thread.is_alive():
            self.http.close()
