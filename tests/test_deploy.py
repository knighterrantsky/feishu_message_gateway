import httpx
import pytest

from scripts.deploy import update_image, validate_version, verify_deployment

SHA = "a" * 40


def test_update_uses_official_graphql_contract():
    def respond(request):
        import json

        body = json.loads(request.content)
        assert body["variables"] == {"serviceID": "svc", "environmentID": "env", "tag": SHA}
        assert "updateServiceImageTag" in body["query"]
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json={"data": {"updateServiceImageTag": True}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        update_image(client, "secret", "svc", "env", SHA)


def test_update_rejects_graphql_errors():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"errors": [{"message": "not allowed"}]})
        )
    ) as client:
        with pytest.raises(RuntimeError):
            update_image(client, "secret", "svc", "env", SHA)


@pytest.mark.parametrize("tag", ["main", "latest", "a" * 7, "$(touch /tmp/x)"])
def test_invalid_rollback_tag(tag):
    with pytest.raises(ValueError):
        validate_version(tag)


def test_verification_requires_ready_and_exact_version():
    responses = iter([{"version": "old"}] * 2 + [{"version": SHA}] * 6)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(responses)))
    ) as client:
        verify_deployment(client, "https://gateway.example", SHA, attempts=4, pause=lambda _: None)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"version": SHA}))
    ) as client:
        with pytest.raises(RuntimeError, match="timed out"):
            verify_deployment(
                client, "https://gateway.example", SHA, attempts=1, pause=lambda _: None
            )
