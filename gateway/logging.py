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
                settings.api_access_token.get_secret_value(),
                settings.webhook_signing_secret.get_secret_value(),
                settings.webhook_url,
            ]
        )
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    # SDK debug logs include complete message content and authenticated WS URLs.
    for name in ("Lark", "lark_oapi", "httpx", "httpcore", "websockets"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        logger.disabled = True
