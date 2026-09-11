import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from citestack.api import create_app
from citestack.extraction import TicketExtractor
from citestack.structured import ProviderFailure, StructuredOutputEngine

TICKET = {
    "summary": "Checkout unavailable",
    "category": "availability",
    "priority": "high",
    "affected_services": ["checkout"],
    "requires_human_review": False,
}


def test_ticket_success_and_conservative_fallback(settings):
    extractor = TicketExtractor(settings, StructuredOutputEngine(lambda _: json.dumps(TICKET)))
    assert extractor.extract("Checkout is down").data.affected_services == ["checkout"]

    extractor = TicketExtractor(settings, StructuredOutputEngine(lambda _: "broken", retry_delay=0))
    result = extractor.extract("Checkout is down")
    assert result.status == "fallback" and result.data.requires_human_review
    assert result.data.category == result.data.priority == "unknown"
    assert result.data.affected_services == []


def test_unknown_classification_requires_human_review(settings):
    invalid = {**TICKET, "category": "unknown", "requires_human_review": False}
    extractor = TicketExtractor(
        settings, StructuredOutputEngine(lambda _: json.dumps(invalid), max_attempts=1)
    )
    assert extractor.extract("Need help").status == "fallback"


@pytest.mark.parametrize(
    "change",
    [
        {"summary": "   "},
        {"affected_services": ["   "]},
        {"affected_services": ["invented-service"]},
    ],
)
def test_blank_and_unmentioned_fields_cannot_be_returned(settings, change):
    extractor = TicketExtractor(
        settings, StructuredOutputEngine(lambda _: json.dumps({**TICKET, **change}), max_attempts=1)
    )
    assert extractor.extract("Checkout is down").status == "fallback"


def test_structured_only_api_has_auth_validation_and_no_rag_startup(settings, monkeypatch):
    def fail_if_loaded(*args):
        raise AssertionError("Extraction must not load neural retrieval models")

    monkeypatch.setattr("citestack.api.NeuralModels", fail_if_loaded)
    settings.api_key = "test-secret"
    extractor = TicketExtractor(settings, StructuredOutputEngine(lambda _: json.dumps(TICKET)))
    with TestClient(create_app(settings, extractor=extractor, structured_only=True)) as client:
        headers = {"X-API-Key": "test-secret"}
        assert client.get("/readyz").json()["mode"] == "structured-only"
        assert (
            client.post("/v1/structured/ticket", json={"text": "Checkout down"}).status_code == 401
        )
        assert (
            client.post("/v1/structured/ticket", json={"text": " "}, headers=headers).status_code
            == 422
        )
        response = client.post(
            "/v1/structured/ticket", json={"text": "Checkout down"}, headers=headers
        )
        assert response.status_code == 200 and response.json()["status"] == "success"
        assert client.get("/v1/index", headers=headers).status_code == 503
        assert "/v1/structured/ticket" in client.get("/openapi.json").json()["paths"]


def test_api_fallback_is_marked_in_response(settings):
    def unavailable(_):
        raise ProviderFailure("provider_unavailable")

    extractor = TicketExtractor(settings, StructuredOutputEngine(unavailable))
    with TestClient(create_app(settings, extractor=extractor, structured_only=True)) as client:
        result = client.post("/v1/structured/ticket", json={"text": "Checkout down"})
        assert result.status_code == 200
        assert result.json()["status"] == "fallback"
        assert result.json()["data"]["requires_human_review"] is True


def test_cli_extraction_without_models_or_index(tmp_path):
    import os

    env = {
        **os.environ,
        "CITESTACK_OLLAMA_URL": "http://127.0.0.1:1",
        "CITESTACK_GENERATION_TIMEOUT": "0.1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "citestack.cli", "extract", "Checkout down"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "fallback" and payload["attempts"] == 1
