"""Standalone CLIENT: owns its SQLite history; the gateway never imports this module."""

import argparse
import asyncio
import json
import random
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException


class ClientStore:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages (
            app_id TEXT NOT NULL, message_id TEXT NOT NULL, event_json TEXT NOT NULL,
            PRIMARY KEY(app_id, message_id))""")

    def save(self, event: dict[str, Any]) -> bool:
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, ?, ?)",
                (event["app_id"], event["message_id"], json.dumps(event, ensure_ascii=False)),
            )
        return result.rowcount == 1

    def close(self) -> None:
        self.db.close()


async def consume(args: argparse.Namespace) -> None:
    parsed = urlsplit(args.url)
    if parsed.scheme not in ("ws", "wss") or parsed.query or parsed.username or parsed.password:
        raise ValueError("Use a WebSocket URL without credentials or query parameters")
    if parsed.scheme != "wss" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote connections require wss://")
    store = ClientStore(args.db)
    delay = 1.0
    try:
        while True:
            # A trusted issuer may atomically replace this file before token expiry.
            access_token = args.token_file.read_text().strip()
            try:
                async with connect(
                    args.url,
                    max_size=1048576,
                    max_queue=4,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "req",
                                "id": "connect",
                                "method": "connect",
                                "params": {"token": access_token},
                            }
                        )
                    )
                    async with asyncio.timeout(15):
                        hello = json.loads(await ws.recv())
                    if not hello.get("ok"):
                        raise RuntimeError("Gateway rejected authentication")
                    f: dict[str, Any] = {"app_id": args.app_id}
                    if args.chat_id:
                        f["chat_ids"] = args.chat_id
                    await ws.send(
                        json.dumps(
                            {
                                "type": "req",
                                "id": "subscribe",
                                "method": "subscribe",
                                "params": {"subscription_id": "messages", "filter": f},
                            }
                        )
                    )
                    async for raw in ws:
                        frame = json.loads(raw)
                        if frame["type"] == "res":
                            if not frame["ok"]:
                                raise RuntimeError(
                                    "Gateway rejected subscription: " + frame["error"]["code"]
                                )
                            delay = 1.0
                        elif frame["type"] == "event":
                            event = frame["payload"]
                            # Business processing must start AFTER this local transaction commits.
                            if store.save(event):
                                print("saved", event["app_id"], event["message_id"], flush=True)
            except ConnectionClosed as exc:
                if exc.rcvd and exc.rcvd.reason in ("token_expired", "authentication_failed"):
                    if args.token_file.read_text().strip() == access_token:
                        raise RuntimeError(
                            "Replace the client token file, then restart this client"
                        ) from None
                print("connection closed; reconnecting without offline replay", flush=True)
            except (OSError, WebSocketException, TimeoutError) as exc:
                print("connection unavailable:", type(exc).__name__, flush=True)
            await asyncio.sleep(random.uniform(delay / 2, delay))
            delay = min(30, delay * 2)
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8080/v1/ws")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--chat-id", action="append")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=Path("client-messages.sqlite3"))
    args = parser.parse_args()
    try:
        asyncio.run(consume(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Configuration/runtime errors only; never echo frames, tokens or message bodies.
        print(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
