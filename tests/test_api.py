from fastapi.testclient import TestClient

from citestack.answers import AnswerService
from citestack.api import create_app


def test_api_auth_validation_and_answer(retriever, settings):
    settings.api_key = "test-secret"
    with TestClient(create_app(settings, AnswerService(retriever, settings))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").json()["documents"] == 3
        assert client.get("/v1/index").status_code == 401
        headers = {"X-API-Key": "test-secret"}
        assert client.post("/v1/answer", json={"question": " "}, headers=headers).status_code == 422
        assert (
            client.post(
                "/v1/answer", json={"question": "Pods", "top_k": 100}, headers=headers
            ).status_code
            == 422
        )
        result = client.post("/v1/answer", json={"question": "Pod containers"}, headers=headers)
        assert result.status_code == 200 and result.json()["citations"]


def test_busy_api_rejects_extra_work_and_recovers(retriever, settings):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    started, release = threading.Event(), threading.Event()
    base = AnswerService(retriever, settings)

    class SlowService:
        def __init__(self):
            self.retriever = retriever

        def answer(self, *args):
            started.set()
            release.wait(timeout=5)
            return base.answer(*args)

    settings.max_concurrent_requests = 1
    with TestClient(create_app(settings, SlowService())) as client:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, "/v1/answer", json={"question": "Pod containers"})
            try:
                assert started.wait(timeout=3)
                overloaded = client.post("/v1/answer", json={"question": "Pod containers"})
                assert overloaded.status_code == 503
                assert overloaded.headers["Retry-After"] == "2"
            finally:
                release.set()
            assert future.result(timeout=5).status_code == 200
        assert client.post("/v1/answer", json={"question": "Pod containers"}).status_code == 200
