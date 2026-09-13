import logging

from gateway.config import Settings


class Redactor(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [value for value in secrets if value]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        record.msg, record.args = message, ()
        # Exception strings may carry URLs, request bodies or tokens.
        record.exc_info = None
        record.exc_text = None
        return True


def configure(settings: Settings) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(
        Redactor(
            [
                settings.feishu_app_secret.get_secret_value(),
                settings.gateway_admin_token.get_secret_value(),
                settings.token_signing_key.get_secret_value(),
            ]
        )
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    # Uvicorn passes its own logger into the WS protocol; DEBUG there prints raw frames.
    for name in ("uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        logging.getLogger(name).setLevel(max(logging.INFO, getattr(logging, settings.log_level)))
    # SDK debug logs include complete message content and authenticated WS URLs.
    for name in ("Lark", "lark_oapi", "httpx", "httpcore", "websockets", "urllib3"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True
