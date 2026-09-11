import httpx
import pytest

from citestack.providers import OllamaProvider
from citestack.structured import GenerationRequest, ProviderFailure


@pytest.mark.parametrize("status,retryable", [(401, False), (404, False), (429, True), (503, True)])
def test_http_status_retry_policy(settings, monkeypatch, status, retryable):
    def post(self, url, **kwargs):
        return httpx.Response(
            status, text="private provider response", request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.Client, "post", post)
    with pytest.raises(ProviderFailure) as caught:
        OllamaProvider(settings)(GenerationRequest("prompt", "system", {}, 1))
    assert caught.value.retryable is retryable
    assert str(caught.value) == "provider_http_error"


@pytest.mark.parametrize("envelope", [{}, [], {"response": 123}, {"response": "{}", "done": False}])
def test_bad_provider_envelope(settings, monkeypatch, envelope):
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda self, url, **kwargs: httpx.Response(
            200, json=envelope, request=httpx.Request("POST", url)
        ),
    )
    with pytest.raises(ProviderFailure, match="provider_response_invalid"):
        OllamaProvider(settings)(GenerationRequest("prompt", "system", {}, 1))


def test_timeout_is_terminal(settings, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout("private host")

    monkeypatch.setattr(httpx.Client, "post", fail)
    with pytest.raises(ProviderFailure) as caught:
        OllamaProvider(settings)(GenerationRequest("prompt", "system", {}, 1))
    assert caught.value.code == "provider_timeout" and not caught.value.retryable
