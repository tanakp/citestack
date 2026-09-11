"""Pure ASGI ingress enforcement before body parsing and inference dispatch."""

import asyncio
import json
import logging
import threading
import time
from uuid import uuid4

from starlette.responses import JSONResponse

from citestack.access import QuotaExceeded, QuotaUnavailable
from citestack.runtime import Overloaded, deadline_var, request_id_var, tenant_id_var

logger = logging.getLogger("citestack.http")
ROUTES = {
    "/healthz",
    "/readyz",
    "/metrics",
    "/v1/index",
    "/v1/search",
    "/v1/answer",
    "/v1/structured/ticket",
    "/docs",
    "/openapi.json",
    "/redoc",
}


class HTTPBoundary:
    def __init__(self, app, settings, registry, quota, metrics, quota_executor):
        self.app, self.settings = app, settings
        self.registry, self.quota, self.metrics = registry, quota, metrics
        self.quota_executor = quota_executor
        self.http_slots = threading.BoundedSemaphore(settings.max_http_requests)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        request_id = uuid4().hex
        request_token = request_id_var.set(request_id)
        deadline_token = deadline_var.set(started + self.settings.request_timeout)
        tenant_token = tenant_id_var.set("unauthenticated")
        route = scope["path"] if scope["path"] in ROUTES else "unmatched"
        method = (
            scope["method"] if scope["method"] in {"GET", "POST", "HEAD", "OPTIONS"} else "OTHER"
        )
        status = 500
        response_started = False
        self.metrics.inflight.inc()

        async def outgoing(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status, response_started = message["status"], True
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"cache-control", b"no-store"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        async def reject(code, message, headers=None):
            response = JSONResponse(
                {"error": message, "request_id": request_id}, status_code=code, headers=headers
            )
            await response(scope, receive, outgoing)

        acquired = self.http_slots.acquire(blocking=False)
        try:
            async with asyncio.timeout(self.settings.request_timeout):
                if not acquired:
                    return await reject(503, "http_capacity_exceeded", {"Retry-After": "1"})
                headers = {}
                for name, value in scope.get("headers", []):
                    headers.setdefault(name.lower(), []).append(value)
                protected = scope["path"].startswith("/v1/") or scope["path"] == "/metrics"
                if protected:
                    credentials = headers.get(b"x-api-key", [])
                    if len(credentials) > 1:
                        return await reject(400, "ambiguous_credentials")
                    tenant = self.registry.authenticate(credentials[0] if credentials else None)
                    if tenant is None:
                        return await reject(401, "unauthorized")
                    scope.setdefault("state", {})["tenant"] = tenant
                    tenant_id_var.set(tenant.id)
                    if scope["method"] == "POST":
                        try:
                            await self.quota_executor.run(self.quota.admit, tenant)
                        except QuotaExceeded as error:
                            return await reject(
                                429, "quota_exceeded", {"Retry-After": str(error.retry_after)}
                            )
                        except (QuotaUnavailable, Overloaded):
                            return await reject(503, "quota_unavailable", {"Retry-After": "1"})
                if scope["method"] == "POST":
                    lengths = headers.get(b"content-length", [])
                    if len(lengths) > 1 or (lengths and b"transfer-encoding" in headers):
                        return await reject(400, "ambiguous_body_length")
                    if lengths:
                        try:
                            if not lengths[0].isdigit():
                                raise ValueError
                            length = int(lengths[0])
                            if length < 0:
                                raise ValueError
                        except ValueError:
                            return await reject(400, "invalid_body_length")
                        if length > self.settings.max_request_bytes:
                            return await reject(413, "body_too_large")
                    if headers.get(b"content-encoding", [b"identity"]) != [b"identity"]:
                        return await reject(415, "content_encoding_not_supported")
                    content_types = headers.get(b"content-type", [])
                    if (
                        len(content_types) != 1
                        or content_types[0].split(b";", 1)[0].strip() != b"application/json"
                    ):
                        return await reject(415, "application_json_required")
                    body = bytearray()
                    try:
                        async with asyncio.timeout(self.settings.body_timeout):
                            while True:
                                message = await receive()
                                if message["type"] == "http.disconnect":
                                    status = 499
                                    return
                                block = message.get("body", b"")
                                if len(body) + len(block) > self.settings.max_request_bytes:
                                    return await reject(413, "body_too_large")
                                body.extend(block)
                                if not message.get("more_body", False):
                                    break
                    except TimeoutError:
                        return await reject(408, "body_timeout")
                    if lengths and len(body) != length:
                        return await reject(400, "body_length_mismatch")
                    delivered = False

                    async def replay():
                        nonlocal delivered
                        if not delivered:
                            delivered = True
                            return {"type": "http.request", "body": bytes(body), "more_body": False}
                        return await receive()

                    await self.app(scope, replay, outgoing)
                else:
                    await self.app(scope, receive, outgoing)
        except TimeoutError:
            if not response_started:
                await reject(504, "request_deadline_exceeded")
        except asyncio.CancelledError:
            status = 499
            raise
        except Exception as error:
            logger.error(
                json.dumps(
                    {
                        "event": "request_failed",
                        "request_id": request_id,
                        "error_type": type(error).__name__,
                    }
                )
            )
            if not response_started:
                await reject(500, "internal_error")
        finally:
            if acquired:
                self.http_slots.release()
            elapsed = time.monotonic() - started
            self.metrics.requests.labels(route, method, str(status)).inc()
            self.metrics.latency.labels(route).observe(elapsed)
            self.metrics.inflight.dec()
            logger.info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "route": route,
                        "method": method,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 2),
                    }
                )
            )
            tenant_id_var.reset(tenant_token)
            deadline_var.reset(deadline_token)
            request_id_var.reset(request_token)
