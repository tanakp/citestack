from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Document(BaseModel):
    id: str
    title: str
    url: str
    text: str


class Chunk(BaseModel):
    id: str
    document_id: str
    title: str
    url: str
    heading: str
    text: str
    token_count: int


class Hit(BaseModel):
    chunk: Chunk
    score: float
    rerank_score: float | None = None


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000, pattern=r"\S")
    top_k: int = Field(default=5, ge=1, le=10)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    quote: str = Field(min_length=20, max_length=2000)


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1200)
    evidence: list[Evidence] = Field(min_length=1, max_length=3)


class GeneratedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[Claim] = Field(default_factory=list, max_length=6)
    abstained: bool


class Citation(BaseModel):
    source_id: str
    title: str
    url: str
    quote: str


class Answer(BaseModel):
    request_id: str
    answer: str
    citations: list[Citation]
    abstained: bool
    mode: Literal["extractive", "ollama", "abstained"]
    fallback_reason: str | None = None
    hits: list[Hit]
    timings_ms: dict[str, float]
