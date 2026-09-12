import asyncio
import time

import httpx
import pytest

from citestack.providers import OllamaProvider
from citestack.structured import GenerationRequest, ProviderFailure

REQUEST = GenerationRequest("prompt", "system", {}, 1)


def provider(settings, handler):
    return OllamaProvider(settings, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status,retryable", [(401, False), (404, False), (429, True), (503, True)])
def test_http_status_retry_policy(settings, status, retryable):
    upstream = provider(
        settings, lambda _: httpx.Response(status, text="private provider response")
    )
    with pytest.raises(ProviderFailure) as caught:
        upstream(REQUEST)
    assert caught.value.retryable is retryable
    assert str(caught.value) == "provider_http_error"


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        [],
        {"response": 123},
        {"response": "{}"},
        {"response": "{}", "done": False},
        {"response": "{}", "done": True, "done_reason": "length"},
    ],
)
def test_bad_provider_envelope(settings, envelope):
    with pytest.raises(ProviderFailure, match="provider_response_invalid"):
        provider(settings, lambda _: httpx.Response(200, json=envelope))(REQUEST)


def test_timeout_is_terminal(settings):
    def fail(_):
        raise httpx.ReadTimeout("private host")

    with pytest.raises(ProviderFailure) as caught:
        provider(settings, fail)(REQUEST)
    assert caught.value.code == "provider_timeout" and not caught.value.retryable


class DripStream(httpx.AsyncByteStream):
    def __init__(self, *, delay=0, block=b"x", count=1000):
        self.delay, self.block, self.count = delay, block, count
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(self.count):
            await asyncio.sleep(self.delay)
            self.consumed += 1
            yield self.block

    async def aclose(self):
        self.closed = True


def test_slow_drip_obeys_total_deadline_and_closes_connection(settings):
    settings.generation_timeout = 0.05
    stream = DripStream(delay=0.01)
    start = time.monotonic()
    with pytest.raises(ProviderFailure, match="provider_timeout"):
        provider(settings, lambda _: httpx.Response(200, stream=stream))(REQUEST)
    assert time.monotonic() - start < 1
    assert 0 < stream.consumed < 1000 and stream.closed


def test_chunked_upstream_body_capped_before_reading_entire_response(settings):
    settings.provider_max_bytes = 1024
    stream = DripStream(block=b"x" * 512)
    with pytest.raises(ProviderFailure, match="output_too_large"):
        provider(settings, lambda _: httpx.Response(200, stream=stream))(REQUEST)
    assert stream.consumed == 3 and stream.closed


def test_compressed_response_rejected_before_decompression(settings):
    stream = DripStream()
    with pytest.raises(ProviderFailure, match="provider_response_invalid"):
        provider(
            settings,
            lambda _: httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=stream,
            ),
        )(REQUEST)
    assert stream.consumed == 0 and stream.closed


def test_schema_and_model_sent_without_ambient_proxy(settings):
    import json

    def respond(request):
        payload = json.loads(request.content)
        assert payload["model"] == settings.ollama_model
        assert payload["format"] == {"type": "object"}
        assert request.headers["accept-encoding"] == "identity"
        assert payload["stream"] is False
        return httpx.Response(200, json={"response": "{}", "done": True})

    assert provider(settings, respond)(GenerationRequest("p", "s", {"type": "object"}, 1)) == "{}"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"models": [{"name": "qwen3:4b"}]}, True),
        ({"models": [{"name": "another:latest"}]}, False),
        ({}, False),
        ([], False),
        ({"models": "bad"}, False),
    ],
)
def test_readiness_checks_configured_model(settings, payload, expected):
    upstream = provider(settings, lambda _: httpx.Response(200, json=payload))
    assert asyncio.run(upstream.ready()) is expected


def test_model_metadata_records_only_selected_model(settings):
    def respond(request):
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.18.2"})
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "private-unrelated-model", "digest": "b" * 64},
                    {"name": "qwen3:4b", "digest": "a" * 64},
                ]
            },
        )

    result = asyncio.run(provider(settings, respond).model_metadata())
    assert result == {"name": "qwen3:4b", "digest": "a" * 64, "server_version": "0.18.2"}


def test_model_metadata_rejects_missing_digest(settings):
    upstream = provider(
        settings, lambda _: httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]})
    )
    with pytest.raises(ProviderFailure, match="provider_response_invalid"):
        asyncio.run(upstream.model_metadata())
