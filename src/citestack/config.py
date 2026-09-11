from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CITESTACK_", env_file=".env", extra="ignore")

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
    api_key: str | None = None
    max_concurrent_requests: int = Field(default=2, ge=1, le=32)
