import json

from conftest import event

from examples.client import ClientStore
from gateway.ingress import normalize


def test_client_commits_and_deduplicates_after_restart(tmp_path, settings):
    path = tmp_path / "client.sqlite3"
    message = normalize(event(), settings, "ou_bot")
    first = ClientStore(path)
    assert first.save(message)
    first.close()
    second = ClientStore(path)
    duplicate_envelope = {**message, "event_id": "another-envelope"}
    assert not second.save(duplicate_envelope)
    saved = json.loads(second.db.execute("SELECT event_json FROM messages").fetchone()[0])
    assert saved == message
    assert second.save({**message, "app_id": "cli_other"})
    second.close()
