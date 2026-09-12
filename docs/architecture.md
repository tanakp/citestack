# Architecture and operating boundaries

```mermaid
flowchart LR
  A[Pinned Kubernetes source] --> B[Checksum verification + Markdown cleanup]
  B --> C[Token windows + heading metadata]
  C --> D[BGE embeddings]
  C --> E[SQLite FTS5]
  D --> F[Atomic SQLite snapshot]
  E --> F
  Q[Question] --> G[BM25 + dense cosine search]
  F --> G
  G --> H[Reciprocal-rank fusion]
  H --> I[Cross-encoder reranking]
  I --> J[Relevance filter + context budget]
  J --> K[Ollama structured generation]
  K --> L[Schema + quote validation]
  L --> M[Cited answer or abstention]
  L --> N[Labeled extractive fallback]
```

## Ingestion

The downloader reads a pinned GitHub tree and fetches only English documentation
Markdown, with eight concurrent requests, three attempts per file, a 60-second
network timeout, a 2 MB file cap, and Git blob checksum validation. No arbitrary
URLs or filesystem paths are accepted through the HTTP API. A fetch failure keeps
the previous corpus intact. Frontmatter, Hugo shortcodes, comments, image references,
and link targets are removed; code blocks remain. Very short navigation pages are
excluded. Hugo includes are not rendered, so some source text is incomplete.

The BGE tokenizer produces 220-token windows with 35-token overlap. Offsets preserve
the cleaned source text, while each chunk keeps its page URL, title, and most recent
heading. A chunk can cross a section boundary; this is a transparent baseline rather
than a syntax-aware Markdown splitter. Chunk IDs depend on source, offset, and content.

## Indexing and consistency

Normalized float32 vectors, chunk metadata, and FTS5 live in one SQLite database.
All model work happens before replacing the active index. A build lock prevents
concurrent writers; a complete temporary database is atomically renamed on success.
Rebuilds replace the corpus snapshot, so deleted sources do not leave stale chunks.
Embedding identity and revision are checked before opening an index.

A service process keeps an open database connection and an in-memory vector matrix
from the same snapshot. Snapshot validation checks the manifest, counts, normalized
vectors, FTS content and posting-list checksums before serving. [Backup and restore](recovery.md)
validate private copies before atomic publication. Rebuilding does not switch a running process to the new
index: restart it explicitly. The SQLite read connection is protected by a lock.
The model's dense vectors are searched exactly, which is straightforward at this
corpus size; ANN or a separate vector database would become useful at larger scale.

## Retrieval

1. BM25 over title, heading, and body retrieves 30 candidates; titles receive more weight.
2. The BGE model embeds the question with its recommended retrieval prefix; normalized
   vectors are compared with cosine similarity for another 30 candidates.
3. Reciprocal-rank fusion combines both rankings using `1 / (60 + rank)`.
4. The top 30 fused chunks are scored jointly with the question by a cross-encoder.
5. Return five chunks by default, with at most two from a single page.

RRF scores and cross-encoder logits are ranking signals, not confidence probabilities.
The default answer filter discards logits below zero. This threshold is a starting
point and is not calibrated on a representative abstention dataset.

## Answers and citations

The context builder budgets passages in BGE-tokenizer units, including an allowance
for metadata. This bounds retrieved text, not the exact prompt tokens of every
Ollama model. Ollama receives up to 8,192 context tokens and a 1,200-token output cap.
Supported local models must handle that context size and structured output.

Generated output is a Pydantic schema of claims and evidence. Each claim must cite an
available chunk with a verbatim quote. The shared [structured engine](structured-output.md)
enforces strict schemas and owns the retry budget. Whitespace-only reflow is accepted, then replaced
with the exact original source span; changes to words or punctuation are rejected.
URLs are resolved by the server, never accepted
from generated text. Invalid JSON, missing claims, unknown IDs, and nonmatching quotes
trigger a repair attempt (two total attempts by default, configurable up to five).
Network failures and exhausted repair attempts return
explicitly labeled excerpts. A model's deliberate abstention is preserved.

This verifies citation provenance; it does **not** prove that a quote entails a claim.
Prompt instructions are a mitigation, not a complete prompt-injection defense. The
application grants the model no tools, code execution, secrets, or outbound browsing.

## Serving

FastAPI provides strict input validation and a pure ASGI admission boundary. Production
requires credentials and an explicit host allowlist. API keys select a client registry
entry and its dedicated index; request fields and headers cannot choose another tenant.
A shared model instance serves separate in-memory vectors and SQLite read connections.
OpenAPI is available in development and disabled in production.

A persistent SQLite token bucket and daily admission count limit each client. HTTP
requests, quota workers, and inference workers have bounded admission. Saturation
returns 503 with `Retry-After`; exhausted client quotas return 429. Body size, body
receive time, request duration, and complete provider response size are bounded.
Generation retries share one deadline. A native inference thread cannot be forcibly
cancelled; its slot remains occupied until completion even after the HTTP caller exits.

Structured logs correlate HTTP requests, answers, and extraction outcomes without
payloads. The CLI disables Uvicorn's raw-URL access log. Prometheus labels are bounded
by known routes and outcomes. `/healthz` checks process responsiveness; `/readyz` also
checks the configured Ollama model in production or structured-only mode. Readiness
is cached briefly and proves model availability, not the quality of a generated answer.

The supported target is one API process on one host. Quotas share a local SQLite file,
not a distributed counter. No distributed tracing, autoscaling, per-token billing,
or audited security certification is claimed. See [operating controls](operations.md)
and the [production acceptance record](production-plan.md) for tested scope, evidence,
and remaining operational responsibilities.

## Design references

- [Sentence Transformers retrieve and rerank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html)
- [SQLite FTS5 and BM25](https://www.sqlite.org/fts5.html)
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
- [BGE model card and query instructions](https://huggingface.co/BAAI/bge-small-en-v1.5)
