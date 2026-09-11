import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from citestack.access import QuotaStore, TenantRegistry
from citestack.answers import AnswerService
from citestack.boundary import HTTPBoundary
from citestack.config import Settings
from citestack.extraction import ExtractionRequest, SupportTicket, TicketExtractor
from citestack.index import Retriever
from citestack.models import NeuralModels
from citestack.providers import OllamaProvider
from citestack.runtime import InferenceExecutor, Overloaded, request_id_var
from citestack.schemas import Answer, QueryRequest
from citestack.structured import StructuredResult
from citestack.telemetry import Metrics


def create_app(
    settings: Settings | None = None,
    service: AnswerService | None = None,
    extractor: TicketExtractor | None = None,
    *,
    structured_only: bool = False,
    services: dict[str, AnswerService] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    extractor = extractor or TicketExtractor(settings)
    registry = TenantRegistry(settings)
    quota = QuotaStore(settings.quota_path)
    metrics = Metrics()
    quota_executor = InferenceExecutor(4)
    executor = InferenceExecutor(settings.max_concurrent_requests)
    metrics.inference.set_function(lambda: executor.active)
    owned_retrievers = []
    available = dict(services or {})
    if service is not None:
        available["default"] = service
    dependency_status = {"checked": 0.0, "ready": False}
    readiness_lock = asyncio.Lock()
    provider = OllamaProvider(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        # HTTPX's default INFO logs contain complete URLs, which may contain sensitive data.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        try:
            if not structured_only:
                models = None
                for tenant in registry.tenants.values():
                    if tenant.id in available:
                        continue
                    if tenant.index_path is None:
                        raise ValueError("Every RAG tenant needs an index snapshot")
                    models = models or NeuralModels(settings)
                    tenant_settings = settings.model_copy(update={"index_path": tenant.index_path})
                    retriever = Retriever(tenant_settings, models)
                    owned_retrievers.append(retriever)
                    available[tenant.id] = AnswerService(retriever, tenant_settings)
            app.state.services = available
            app.state.executor = executor
            app.state.metrics = metrics
            yield
        finally:
            executor.close()
            quota_executor.close()
            deadline = time.monotonic() + settings.shutdown_timeout
            while (executor.active or quota_executor.active) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            if executor.active == 0:
                for retriever in owned_retrievers:
                    retriever.close()
            else:
                logging.getLogger("citestack").error(
                    json.dumps(
                        {
                            "event": "shutdown_workers_remaining",
                            "active": executor.active,
                        }
                    )
                )
                # Do not close SQLite underneath an active worker. The container's
                # termination grace period ultimately bounds a stalled native task.

    production = settings.environment == "production"
    app = FastAPI(
        title="CiteStack",
        version="0.3.0",
        description="Tenant-isolated RAG and validated structured extraction.",
        lifespan=lifespan,
        redirect_slashes=False,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(
        HTTPBoundary,
        settings=settings,
        registry=registry,
        quota=quota,
        metrics=metrics,
        quota_executor=quota_executor,
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # FastAPI's default response includes rejected input values. Never echo them.
        return JSONResponse(
            {"error": "invalid_request", "request_id": request_id_var.get()}, status_code=422
        )

    def rag_service(request: Request):
        tenant = request.state.tenant
        if structured_only or tenant.id not in available:
            raise HTTPException(status_code=503, detail="RAG unavailable for this client")
        return available[tenant.id]

    async def infer(function, *args):
        try:
            return await executor.run(function, *args)
        except Overloaded:
            raise HTTPException(
                status_code=503, detail="Server busy", headers={"Retry-After": "2"}
            ) from None
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Request deadline exceeded") from None

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        if executor.draining:
            return JSONResponse({"status": "draining"}, status_code=503)
        # Development extractive mode remains useful without an Ollama installation.
        # Production and structured-only deployments check their real model dependency.
        if production or structured_only or settings.answer_mode == "ollama":
            async with readiness_lock:
                if (
                    time.monotonic() - dependency_status["checked"]
                    >= settings.readiness_cache_seconds
                ):
                    dependency_status["ready"] = await provider.ready()
                    dependency_status["checked"] = time.monotonic()
            if not dependency_status["ready"]:
                return JSONResponse({"status": "not_ready", "dependency": "model"}, status_code=503)
        if structured_only:
            return {"status": "ready", "mode": "structured-only"}
        if not production and set(available) == {"default"}:
            manifest = available["default"].retriever.manifest
            return {
                "status": "ready",
                "documents": manifest["documents"],
                "chunks": manifest["chunks"],
            }
        return {"status": "ready"}

    @app.get("/metrics")
    async def prometheus_metrics():
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/v1/index")
    async def index(request: Request):
        return rag_service(request).retriever.manifest

    @app.post("/v1/search")
    async def search(query: QueryRequest, request: Request):
        return {
            "hits": await infer(rag_service(request).retriever.search, query.question, query.top_k)
        }

    @app.post("/v1/answer", response_model=Answer)
    async def answer(query: QueryRequest, request: Request):
        result = await infer(rag_service(request).answer, query.question, query.top_k)
        metrics.outcomes.labels("rag", "fallback" if result.fallback_reason else result.mode).inc()
        return result

    @app.post("/v1/structured/ticket", response_model=StructuredResult[SupportTicket])
    async def extract_ticket(request: ExtractionRequest):
        result = await infer(extractor.extract, request.text)
        metrics.outcomes.labels("ticket", result.status).inc()
        return result

    return app


def create_structured_app() -> FastAPI:
    return create_app(structured_only=True)
