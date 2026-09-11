from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CITESTACK_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
        validate_assignment=True,
    )

    environment: Literal["development", "production"] = "development"

    index_path: Path = Path("data/index.sqlite")
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_revision: str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_revision: str = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
    device: str = "cpu"
    chunk_tokens: int = Field(default=220, ge=64, le=350)
    overlap_tokens: int = Field(default=35, ge=0, le=60)
    candidate_k: int = Field(default=30, ge=5, le=100)
    context_tokens: int = Field(default=1600, ge=256, le=4096)
    min_rerank_score: float = 0.0
    answer_mode: Literal["extractive", "ollama"] = "extractive"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    generation_timeout: float = Field(default=90, gt=0, le=300)
    structured_max_attempts: int = Field(default=2, ge=1, le=5)
    api_key: SecretStr | None = None
    previous_api_key: SecretStr | None = None
    api_key_file: Path | None = None
    tenant_config: Path | None = None
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "testserver"]
    )
    max_request_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    max_http_requests: int = Field(default=64, ge=4, le=1024)
    body_timeout: float = Field(default=5, gt=0, le=30)
    request_timeout: float = Field(default=120, gt=0, le=600)
    provider_max_bytes: int = Field(default=262_144, ge=1024, le=4_194_304)
    generation_budget: float = Field(default=90, gt=0, le=300)
    rate_limit_per_minute: int = Field(default=30, ge=1, le=10_000)
    rate_limit_burst: int = Field(default=5, ge=1, le=1000)
    daily_request_limit: int = Field(default=1000, ge=1, le=10_000_000)
    quota_path: Path = Path("data/quotas.sqlite")
    readiness_cache_seconds: float = Field(default=5, ge=0, le=30)
    shutdown_timeout: float = Field(default=30, ge=1, le=120)
    max_concurrent_requests: int = Field(default=2, ge=1, le=32)

    @model_validator(mode="after")
    def production_invariants(self):
        if self.api_key_file is not None and self.api_key is None:
            try:
                with self.api_key_file.open("r") as secret:
                    value = secret.read(513).strip()
            except OSError as error:
                raise ValueError("Cannot read configured API-key secret file") from error
            object.__setattr__(self, "api_key", SecretStr(value))
        for key in (self.api_key, self.previous_api_key):
            if key is not None and (
                not key.get_secret_value() or len(key.get_secret_value()) > 512
            ):
                raise ValueError("API keys must contain 1 to 512 characters")
        url = urlsplit(self.ollama_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Ollama URL must be HTTP(S) without credentials, query, or fragment")
        if not self.allowed_hosts:
            raise ValueError("allowed_hosts cannot be empty")
        if self.environment == "production":
            if self.tenant_config is None and (
                self.api_key is None or len(self.api_key.get_secret_value()) < 32
            ):
                raise ValueError(
                    "Production requires a tenant registry or an API key of 32+ characters"
                )
            if self.previous_api_key and len(self.previous_api_key.get_secret_value()) < 32:
                raise ValueError("Previous production API key must have at least 32 characters")
            if "*" in self.allowed_hosts or "testserver" in self.allowed_hosts:
                raise ValueError(
                    "Production requires explicit allowed_hosts without testserver or *"
                )
        return self
