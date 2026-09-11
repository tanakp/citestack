"""Local Ollama adapter shared by RAG and standalone structured extraction."""

import httpx

from citestack.config import Settings
from citestack.structured import GenerationRequest, ProviderFailure


class OllamaProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def __call__(self, request: GenerationRequest) -> str:
        try:
            with httpx.Client(timeout=self.settings.generation_timeout) as client:
                response = client.post(
                    self.settings.ollama_url.rstrip("/") + "/api/generate",
                    json={
                        "model": self.settings.ollama_model,
                        "system": request.system,
                        "prompt": request.prompt,
                        "format": request.json_schema,
                        "stream": False,
                        "think": False,
                        "options": {"temperature": 0, "num_predict": 1200, "num_ctx": 8192},
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as error:
            raise ProviderFailure("provider_timeout") from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            raise ProviderFailure(
                "provider_http_error", retryable=status == 429 or status >= 500
            ) from error
        except httpx.HTTPError as error:
            raise ProviderFailure("provider_unavailable") from error
        try:
            payload = response.json()
            text = payload["response"]
            if not isinstance(text, str) or payload.get("done") is False:
                raise ValueError("Incomplete or invalid provider envelope")
            return text
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderFailure("provider_response_invalid", retryable=True) from error
