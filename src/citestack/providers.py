"""Bounded Ollama transport shared by RAG and standalone structured extraction."""

import asyncio
import json

import httpx

from citestack.config import Settings
from citestack.runtime import remaining_seconds
from citestack.structured import GenerationRequest, ProviderFailure


class OllamaProvider:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def _request(self, method: str, path: str, *, budget: float, payload=None):
        try:
            # A total deadline also terminates a peer that sends one byte before every
            # HTTPX read timeout. Disable ambient proxies and reject compressed bodies.
            async with asyncio.timeout(budget):
                async with httpx.AsyncClient(
                    timeout=budget,
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                ) as client:
                    async with client.stream(
                        method,
                        self.settings.ollama_url.rstrip("/") + path,
                        json=payload,
                    ) as response:
                        response.raise_for_status()
                        if response.headers.get("content-encoding", "identity") != "identity":
                            raise ProviderFailure("provider_response_invalid")
                        body = bytearray()
                        async for block in response.aiter_bytes():
                            if len(body) + len(block) > self.settings.provider_max_bytes:
                                raise ProviderFailure("output_too_large")
                            body.extend(block)
                        return json.loads(body)
        except (TimeoutError, httpx.TimeoutException):
            raise ProviderFailure("provider_timeout") from None
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            raise ProviderFailure(
                "provider_http_error",
                retryable=status == 429 or status >= 500,
            ) from None
        except httpx.HTTPError:
            raise ProviderFailure("provider_unavailable") from None
        except (ValueError, RecursionError):
            raise ProviderFailure("provider_response_invalid", retryable=True) from None

    def __call__(self, request: GenerationRequest) -> str:
        budget = remaining_seconds(min(self.settings.generation_timeout, request.timeout_seconds))
        payload = asyncio.run(
            self._request(
                "POST",
                "/api/generate",
                budget=budget,
                payload={
                    "model": self.settings.ollama_model,
                    "system": request.system,
                    "prompt": request.prompt,
                    "format": request.json_schema,
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0, "num_predict": 1200, "num_ctx": 8192},
                },
            )
        )
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("response"), str)
            or payload.get("done") is not True
            or payload.get("done_reason") == "length"
        ):
            raise ProviderFailure("provider_response_invalid", retryable=True)
        return payload["response"]

    async def ready(self) -> bool:
        try:
            payload = await self._request("GET", "/api/tags", budget=remaining_seconds(2))
            configured = self.settings.ollama_model
            expected = configured if ":" in configured else configured + ":latest"
            return (
                isinstance(payload, dict)
                and isinstance(payload.get("models"), list)
                and any(
                    isinstance(model, dict) and model.get("name") == expected
                    for model in payload["models"]
                )
            )
        except ProviderFailure:
            return False

    async def model_metadata(self):
        """Return only the configured model's reproducibility identifiers."""
        payload = await self._request("GET", "/api/tags", budget=remaining_seconds(2))
        expected = self.settings.ollama_model
        expected = expected if ":" in expected else expected + ":latest"
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ProviderFailure("provider_response_invalid")
        for model in payload["models"]:
            if isinstance(model, dict) and model.get("name") == expected:
                digest = model.get("digest")
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)
                ):
                    raise ProviderFailure("provider_response_invalid")
                version = await self._request("GET", "/api/version", budget=remaining_seconds(2))
                if not isinstance(version, dict) or not isinstance(version.get("version"), str):
                    raise ProviderFailure("provider_response_invalid")
                return {
                    "name": expected,
                    "digest": digest,
                    "server_version": version.get("version"),
                }
        raise ProviderFailure("provider_unavailable")
