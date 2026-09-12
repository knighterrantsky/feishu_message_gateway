from pathlib import Path

import pytest

from gateway.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret",
        webhook_url="https://receiver.example/events",
        webhook_signing_secret="s" * 32,
        api_access_token="t" * 32,
        data_dir=tmp_path,
        retry_base_seconds=0.1,
        retry_max_seconds=1,
        retry_max_attempts=2,
        _env_file=None,
    )


class FakeFeishu:
    def __init__(self):
        self.sent = []

    def send(self, row):
        self.sent.append(row)
        return "om_sent"

    def chat_for(self, message_id):
        return "oc_test"


class FakeReceiver:
    connected = True

    def start(self):
        pass

    def ensure_alive(self):
        pass

    def close(self):
        self.connected = False
