import json
from pathlib import Path
from tempfile import TemporaryDirectory

from gateway.app import create_app
from gateway.config import Settings

with TemporaryDirectory() as directory:
    settings = Settings(
        feishu_app_id="documentation",
        feishu_app_secret="placeholder",
        webhook_url="https://example.invalid/events",
        webhook_signing_secret="x" * 32,
        api_access_token="x" * 32,
        data_dir=Path(directory),
        _env_file=None,
    )
    # No lifespan/network is started to generate the document.
    app = create_app(settings, feishu=object(), receiver=object(), run_worker=False)
    Path("docs/openapi.json").write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"
    )
    app.state.worker.close()
