"""Update an existing Zeabur PREBUILT service and verify the exact running SHA."""

import os
import re
import sys
import time
from collections.abc import Callable

import httpx

MUTATION = """mutation UpdateGatewayImage($serviceID: ObjectID!,
    $environmentID: ObjectID!, $tag: String!) {
    updateServiceImageTag(serviceID: $serviceID, environmentID: $environmentID, tag: $tag)
}"""


def validate_version(version: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", version):
        raise ValueError("IMAGE_TAG must be a full lowercase Git commit SHA")
    return version


def update_image(
    client: httpx.Client, token: str, service_id: str, environment_id: str, version: str
) -> None:
    validate_version(version)
    response = client.post(
        "https://api.zeabur.com/graphql",
        headers={"Authorization": "Bearer " + token},
        json={
            "query": MUTATION,
            "variables": {"serviceID": service_id, "environmentID": environment_id, "tag": version},
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors") or payload.get("data", {}).get("updateServiceImageTag") is not True:
        raise RuntimeError("Zeabur image update rejected; inspect the service dashboard")


def verify_deployment(
    client: httpx.Client,
    base_url: str,
    version: str,
    attempts: int = 60,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    # Three consecutive matches reduce false success during a rolling transition.
    consecutive = 0
    for _ in range(attempts):
        try:
            healthy = True
            for path in ("/healthz", "/readyz"):
                response = client.get(
                    base_url.rstrip("/") + path, headers={"Cache-Control": "no-cache"}
                )
                if response.status_code != 200 or response.json().get("version") != version:
                    healthy = False
            consecutive = consecutive + 1 if healthy else 0
            if consecutive >= 3:
                return
        except (httpx.HTTPError, ValueError):
            consecutive = 0
        pause(5)
    raise RuntimeError("Deployment verification timed out: health/readiness/version mismatch")


def main() -> None:
    required = [
        "ZEABUR_API_TOKEN",
        "ZEABUR_SERVICE_ID",
        "ZEABUR_ENVIRONMENT_ID",
        "GATEWAY_BASE_URL",
        "IMAGE_TAG",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing deployment configuration: " + ", ".join(missing))
    version = validate_version(os.environ["IMAGE_TAG"])
    if not os.environ["GATEWAY_BASE_URL"].startswith("https://"):
        raise ValueError("GATEWAY_BASE_URL must use HTTPS")
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        update_image(
            client,
            os.environ["ZEABUR_API_TOKEN"],
            os.environ["ZEABUR_SERVICE_ID"],
            os.environ["ZEABUR_ENVIRONMENT_ID"],
            version,
        )
        verify_deployment(client, os.environ["GATEWAY_BASE_URL"], version)
    print("Verified deployed version: " + version)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never print request headers or bodies from HTTP exceptions.
        print(
            "Deployment failed: "
            + (str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__),
            file=sys.stderr,
        )
        sys.exit(1)
