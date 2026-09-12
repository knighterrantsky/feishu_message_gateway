import hashlib
import hmac
import time


def sign(secret: str, timestamp: str, delivery_id: str, body: bytes) -> str:
    data = timestamp.encode() + b"." + delivery_id.encode() + b"." + body
    return "v1=" + hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def verify(
    secret: str, timestamp: str, delivery_id: str, body: bytes, signature: str, tolerance: int = 300
) -> bool:
    try:
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
    except ValueError:
        return False
    try:
        supplied = signature.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(sign(secret, timestamp, delivery_id, body).encode("ascii"), supplied)
