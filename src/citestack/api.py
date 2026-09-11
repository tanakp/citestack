import hmac
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader

from citestack.answers import AnswerService
from citestack.config import Settings
from citestack.extraction import ExtractionRequest, SupportTicket, TicketExtractor
from citestack.index import Retriever
from citestack.models import NeuralModels
from citestack.schemas import Answer, QueryRequest
from citestack.structured import StructuredResult


def create_app(
    settings: Settings | None = None,
    service: AnswerService | None = None,
    extractor: TicketExtractor | None = None,
    *,
    structured_only: bool = False,
) -> FastAPI:
    settings = settings or Settings()
    extractor = extractor or TicketExtractor(settings)
    slots = threading.BoundedSemaphore(settings.max_concurrent_requests)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        if service is None and not structured_only:
            models = NeuralModels(settings)
            retriever = Retriever(settings, models)
            app.state.service = AnswerService(retriever, settings)
        else:
            app.state.service = service
        yield
        if service is None and not structured_only:
            app.state.service.retriever.close()

    app = FastAPI(
        title="CiteStack",
        version="0.2.0",
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

    def rag_service():
        if app.state.service is None:
            raise HTTPException(status_code=503, detail="RAG is disabled in structured-only mode")
        return app.state.service

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/readyz")
    def ready():
        if structured_only:
            return {"status": "ready", "mode": "structured-only"}
        manifest = rag_service().retriever.manifest
        return {"status": "ready", "documents": manifest["documents"], "chunks": manifest["chunks"]}

    @app.get("/v1/index", dependencies=[Depends(authorize)])
    def index():
        return rag_service().retriever.manifest

    @app.post("/v1/search", dependencies=[Depends(authorize), Depends(capacity)])
    def search(query: QueryRequest):
        return {"hits": rag_service().retriever.search(query.question, query.top_k)}

    @app.post(
        "/v1/answer", response_model=Answer, dependencies=[Depends(authorize), Depends(capacity)]
    )
    def answer(query: QueryRequest):
        return rag_service().answer(query.question, query.top_k)

    @app.post(
        "/v1/structured/ticket",
        response_model=StructuredResult[SupportTicket],
        dependencies=[Depends(authorize), Depends(capacity)],
    )
    def extract_ticket(request: ExtractionRequest):
        return extractor.extract(request.text)

    return app


def create_structured_app() -> FastAPI:
    """Start extraction without downloading retrieval models or building an index."""
    return create_app(structured_only=True)
