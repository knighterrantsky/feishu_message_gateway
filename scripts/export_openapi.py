import json
from pathlib import Path

from pydantic import SecretStr

from gateway.app import create_app
from gateway.config import Settings


def main() -> None:
    settings = Settings(  # type: ignore[call-arg]  # BaseSettings runtime-only _env_file option.
        feishu_app_id="documentation",
        feishu_app_secret=SecretStr("placeholder"),
        gateway_admin_token=SecretStr("a" * 32),
        token_signing_key=SecretStr("s" * 32),
        code_version="dev",
        _env_file=None,
    )
    # Do not start lifespan, SDK processes or any network requests.
    app = create_app(settings, feishu=object(), receiver=object())
    Path("docs/openapi.json").write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
