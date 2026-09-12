import json
from typing import Any

from gateway.config import Settings
from gateway.store import Store


def receive(raw: dict[str, Any], store: Store, settings: Settings, bot_id: str) -> str | None:
    header, event = raw["header"], raw["event"]
    if header.get("app_id") != settings.feishu_app_id:
        return None
    message, sender = event["message"], event["sender"]
    if message["message_type"] != "text" or sender.get("sender_type") != "user":
        return None
    user_id = sender.get("sender_id", {}).get("open_id", "")
    chat_id = message["chat_id"]
    if settings.users and user_id not in settings.users:
        return None
    if settings.chats and chat_id not in settings.chats:
        return None
    mentions = message.get("mentions") or []
    if message["chat_type"] == "group":
        if not any(m.get("id", {}).get("open_id") == bot_id for m in mentions):
            return None
    elif message["chat_type"] != "p2p":
        return None
    text = json.loads(message["content"])["text"]
    if not isinstance(text, str):
        raise ValueError("invalid_text_content")
    event_body = {
        "schema_version": "1.0",
        "type": "message.received",
        "app_id": header["app_id"],
        "event_id": header["event_id"],
        "message_id": message["message_id"],
        "chat_id": chat_id,
        "chat_type": message["chat_type"],
        "sender": sender["sender_id"],
        "text": text,
        "mentions": mentions,
        "create_time": message["create_time"],
    }
    # message_id is stable even if upstream resends with a different event envelope.
    row = store.enqueue(
        "webhook", chat_id, f"event:{header['app_id']}:{message['message_id']}", event_body
    )
    return str(row["delivery_id"])
