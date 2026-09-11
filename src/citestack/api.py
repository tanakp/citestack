import hmac
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader

from citestack.answers import AnswerService
from citestack.config import Settings
from citestack.index import Retriever
from citestack.models import NeuralModels
from citestack.schemas import Answer, QueryRequest


def create_app(settings: Settings | None = None, service: AnswerService | None = None) -> FastAPI:
    settings = settings or Settings()
    slots = threading.BoundedSemaphore(settings.max_concurrent_requests)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        if service is None:
            models = NeuralModels(settings)
            retriever = Retriever(settings, models)
            app.state.service = AnswerService(retriever, settings)
        else:
            app.state.service = service
        yield
        if service is None:
            app.state.service.retriever.close()

    app = FastAPI(
        title="CiteStack",
        version="0.1.0",
        description="Hybrid search and cited answers over a pinned documentation corpus.",
        lifespan=lifespan,
    )
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorize(key: str | None = Depends(key_header)):
        if settings.api_key and (not key or not hmac.compare_digest(key, settings.api_key)):
            raise HTTPException(status_code=401, detail="Invalid API key")

    def capacity():
        if not slots.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="Server busy", headers={"Retry-After": "2"})
        try:
            yield
        finally:
            slots.release()

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        manifest = app.state.service.retriever.manifest
        return {"status": "ready", "documents": manifest["documents"], "chunks": manifest["chunks"]}

    @app.get("/v1/index", dependencies=[Depends(authorize)])
    def index():
        return app.state.service.retriever.manifest

    @app.post("/v1/search", dependencies=[Depends(authorize), Depends(capacity)])
    def search(query: QueryRequest):
        return {"hits": app.state.service.retriever.search(query.question, query.top_k)}

    @app.post(
        "/v1/answer", response_model=Answer, dependencies=[Depends(authorize), Depends(capacity)]
    )
    def answer(query: QueryRequest):
        return app.state.service.answer(query.question, query.top_k)

    return app
