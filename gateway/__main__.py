import logging

import uvicorn
from pydantic import ValidationError

from gateway.app import create_app
from gateway.config import Settings


def main() -> None:
    try:
        settings = Settings()
    except ValidationError as exc:
        # Log field names only: a model validation exception can contain the entire env.
        fields = [".".join(map(str, item["loc"])) or "configuration" for item in exc.errors()]
        logging.error("Invalid configuration fields: %s", ", ".join(fields))
        raise SystemExit(2) from None
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=int(settings.shutdown_timeout_seconds),
    )


if __name__ == "__main__":
    main()
