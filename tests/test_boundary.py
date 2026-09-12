import asyncio
import logging

import pytest
from fastapi.testclient import TestClient
from test_access import definition, write_registry

from citestack.access import QuotaStore, TenantRegistry
from citestack.answers import AnswerService
from citestack.api import create_app
from citestack.boundary import HTTPBoundary
from citestack.runtime import InferenceExecutor
from citestack.telemetry import Metrics


def raw_request(settings, *, headers=(), blocks=(), delay=0):
    called = []
    sent = []
    metrics = Metrics()

    async def app(scope, receive, send):
        called.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    quota_executor = InferenceExecutor(4)
    boundary = HTTPBoundary(
        app,
        settings,
        TenantRegistry(settings),
        QuotaStore(settings.quota_path),
        metrics,
        quota_executor,
    )

    async def run():
        pending = iter(blocks)

        async def receive():
            await asyncio.sleep(delay)
            return next(pending, {"type": "http.disconnect"})

        async def send(message):
            sent.append(message)

        await boundary(
            {"type": "http", "method": "POST", "path": "/v1/answer", "headers": list(headers)},
            receive,
            send,
        )

    try:
        asyncio.run(run())
    finally:
        quota_executor.close()
    return sent, called, metrics


def block(body, more=False):
    return {"type": "http.request", "body": body, "more_body": more}


@pytest.mark.parametrize(
    "headers,blocks,status",
    [
        ([(b"content-length", b"999999")], [], 413),
        ([(b"content-length", b"2"), (b"content-length", b"2")], [], 400),
        ([(b"content-length", b"2"), (b"transfer-encoding", b"chunked")], [], 400),
        ([(b"content-length", b"-1")], [], 400),
        ([(b"content-length", b"+2")], [], 400),
        ([(b"content-length", b"3")], [block(b"{}")], 400),
        ([(b"content-encoding", b"gzip")], [], 415),
        ([], [block(b"x" * 600, True), block(b"x" * 600)], 413),
        ([], [block(b"{", True), block(b"}")], 200),
    ],
)
def test_bounded_ingress(settings, headers, blocks, status):
    settings.max_request_bytes = 1024
    sent, called, _ = raw_request(
        settings, headers=[(b"content-type", b"application/json"), *headers], blocks=blocks
    )
    assert sent[0]["status"] == status
    assert bool(called) is (status == 200)
    if called:
        assert called[0]["body"] == b"{}"
    assert dict(sent[0]["headers"])[b"x-request-id"]


def test_slow_body_and_disconnect_never_dispatch(settings):
    settings.body_timeout = 0.02
    sent, called, _ = raw_request(
        settings, headers=[(b"content-type", b"application/json")], blocks=[block(b"{}")], delay=0.1
    )
    assert sent[0]["status"] == 408 and not called
    sent, called, metrics = raw_request(settings, headers=[(b"content-type", b"application/json")])
    assert not sent and not called
    assert b'status="499"' in metrics.render()


def test_http_total_deadline_bounds_body_even_if_receive_limit_is_longer(settings):
    settings.request_timeout = 0.02
    sent, called, _ = raw_request(
        settings, headers=[(b"content-type", b"application/json")], blocks=[block(b"{}")], delay=0.1
    )
    assert sent[0]["status"] == 504 and not called


def test_validation_logs_and_metric_labels_do_not_echo_inputs(settings, retriever, caplog):
    caplog.set_level(logging.INFO)
    with TestClient(create_app(settings, AnswerService(retriever, settings))) as client:
        response = client.post(
            "/v1/answer", json={"question": "secret-person@example.org", "top_k": "private-value"}
        )
        assert response.status_code == 422
        assert response.json()["request_id"] == response.headers["x-request-id"]
        assert "private-value" not in response.text
        client.get("/unknown-secret-path?token=private-value")
        metrics = client.get("/metrics").text
        assert 'route="unmatched"' in metrics
        assert "private-value" not in metrics and "unknown-secret-path" not in metrics
        answer = client.post("/v1/answer", json={"question": "Pod containers"})
        assert answer.json()["request_id"] == answer.headers["x-request-id"]
    assert "private-value" not in caplog.text and "secret-person@example.org" not in caplog.text


def test_tenant_identity_cannot_be_overridden_and_quotas_are_independent(settings, retriever):
    write_registry(
        settings,
        [definition("alpha", "alpha-key", burst=1), definition("beta", "beta-key", burst=1)],
    )

    # Distinct manifests make leakage visible through actual authenticated endpoints.
    class OtherRetriever:
        manifest = {"documents": 777, "chunks": 888}

    class OtherService:
        retriever = OtherRetriever()

    with TestClient(
        create_app(
            settings, services={"alpha": AnswerService(retriever, settings), "beta": OtherService()}
        )
    ) as client:
        alpha = {"X-API-Key": "alpha-key", "X-Tenant-ID": "beta"}
        beta = {"X-API-Key": "beta-key"}
        assert client.get("/v1/index", headers=alpha).json()["documents"] == 3
        assert client.get("/v1/index", headers=beta).json()["documents"] == 777
        assert client.get("/v1/index?tenant=beta", headers=alpha).json()["documents"] == 3
        assert client.get("/v1/index").status_code == 401
        assert client.get("/metrics").status_code == 401
        assert (
            client.get(
                "/v1/index", headers=[("X-API-Key", "alpha-key"), ("X-API-Key", "beta-key")]
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/v1/answer", headers=alpha, json={"question": "Pod containers"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/answer", headers=alpha, json={"question": "Pod containers"}
            ).status_code
            == 429
        )
        # Invalid request still consumes admission, but beta gets its own bucket.
        assert client.post("/v1/answer", headers=beta, json={}).status_code == 422


def test_readiness_tracks_dependency_and_production_disables_public_schema(settings, monkeypatch):
    status = [False]

    async def ready(self):
        return status[0]

    monkeypatch.setattr("citestack.api.OllamaProvider.ready", ready)
    production = settings.model_copy(update={"environment": "production", "api_key": None})
    # Build through validation, never rely on unchecked model_copy for security settings.
    from citestack.config import Settings

    production = Settings(
        **{
            **production.model_dump(),
            "api_key": "a" * 32,
            "allowed_hosts": ["localhost"],
            "readiness_cache_seconds": 0,
        },
        _env_file=None,
    )
    with TestClient(
        create_app(production, structured_only=True), base_url="http://localhost"
    ) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        status[0] = True
        assert client.get("/readyz").status_code == 200
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/healthz", headers={"Host": "untrusted.example"}).status_code == 400
        client.app.state.executor.close()
        assert client.get("/readyz").status_code == 503


def test_real_snapshot_selection_isolates_search_and_answers(settings, models, monkeypatch):
    from citestack.index import build_index
    from citestack.schemas import Document

    definitions = []
    for name in ("alpha", "beta"):
        index = settings.index_path.parent / f"{name}.sqlite"
        corpus = settings.index_path.parent / f"{name}.jsonl"
        corpus.write_text(
            Document(
                id=name,
                title=f"{name} private operations",
                url=f"https://example.org/{name}",
                text=f"The {name} service stores its confidential configuration in {name} storage.",
            ).model_dump_json()
        )
        build_index(corpus, settings.model_copy(update={"index_path": index}), models)
        definitions.append(definition(name, f"{name}-key", index_path=str(index)))
    write_registry(settings, definitions)
    monkeypatch.setattr("citestack.api.NeuralModels", lambda _: models)
    with TestClient(create_app(settings)) as client:
        for name, other in (("alpha", "beta"), ("beta", "alpha")):
            headers = {"X-API-Key": f"{name}-key", "X-Tenant-ID": other}
            for route in ("search", "answer"):
                result = client.post(
                    f"/v1/{route}",
                    headers=headers,
                    json={"question": "service confidential configuration"},
                )
                assert result.status_code == 200
                hits = result.json()["hits"]
                assert hits and {hit["chunk"]["document_id"] for hit in hits} == {name}
                if route == "answer":
                    assert result.json()["citations"]
                    assert all(c["url"].endswith(name) for c in result.json()["citations"])


def test_request_deadline_returns_504_without_releasing_inference_slot(settings, retriever):
    import threading

    release = threading.Event()

    class SlowService:
        def __init__(self):
            self.retriever = retriever

        def answer(self, *args):
            release.wait(10)
            return AnswerService(retriever, settings).answer(*args)

    settings.request_timeout = 0.25
    settings.max_concurrent_requests = 1
    with TestClient(create_app(settings, SlowService())) as client:
        try:
            first = client.post("/v1/answer", json={"question": "Pod containers"})
            assert first.status_code == 504
            assert client.app.state.executor.active == 1
            # Only the first request tests the short deadline. Admission for the
            # overload assertion must not race slow CI scheduling or SQLite I/O.
            settings.request_timeout = 2
            assert client.get("/healthz").status_code == 200
            assert client.post("/v1/answer", json={"question": "Pod containers"}).status_code == 503
        finally:
            release.set()
