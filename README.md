# CiteStack

[![CI](https://github.com/tanakp/citestack/actions/workflows/ci.yml/badge.svg)](https://github.com/tanakp/citestack/actions/workflows/ci.yml)

**Ask questions over public documentation. Inspect the evidence behind every answer.**

CiteStack is a runnable RAG reference service with document ingestion, token-based
chunking, BM25 + semantic retrieval, neural reranking, and validated citations.
It indexes a pinned snapshot of **1,180 Kubernetes documentation pages** and runs
locally without a paid API key.

Built to make engineering decisions inspectable: the repository includes an API,
CLI, reproducible evaluation, failure-path tests, Docker packaging, and CI.
It is a single-node reference implementation, not a claim of production certification.

```text
Pinned docs → clean & chunk → BM25 + BGE embeddings → rank fusion
           → cross-encoder reranking → budgeted evidence → local LLM
           → schema + quote validation → cited answer / abstention
```

## Quick start

Requirements: Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), internet for the
initial document/model downloads, and enough RAM to load the models and index.
Python 3.12 is used in CI. Allow several minutes for the full index build; timing
depends on your hardware. All retrieval models are pinned to explicit revisions.

```bash
git clone https://github.com/tanakp/citestack.git
cd citestack
uv sync --frozen --python 3.12
cp .env.example .env
export HF_HOME="$PWD/.cache/huggingface"

uv run citestack fetch
uv run citestack ingest
uv run citestack ask "What is the difference between a readiness probe and a liveness probe?"
uv run citestack serve
```

Open [the interactive API documentation](http://127.0.0.1:8000/docs). Use
`POST /v1/answer` for answers and `POST /v1/search` to inspect retrieved passages.

The default `extractive` mode returns labeled source excerpts. It exercises real
neural retrieval and reranking, but does not synthesize an LLM answer.

### Enable generated answers

Install [Ollama](https://ollama.com/) and start its local server, then:

```bash
ollama pull qwen3:4b
uv run citestack ask "How do readiness probes differ from liveness probes?" --mode ollama
```

For the HTTP API, set `CITESTACK_ANSWER_MODE=ollama` in `.env` and restart the service.
Every claim must include a quote from a retrieved passage. Whitespace-only differences
are resolved back to the exact original source span. The server builds
the citation URLs. Invalid output gets one repair attempt, then a clearly marked
extractive fallback. Missing relevant evidence produces an abstention.

**Verified example:** “What does a Kubernetes readiness probe do?”

> A readiness probe is a diagnostic performed periodically by the kubelet to
> determine if a container is ready to receive traffic. [1]

The [full API response](docs/example-answer.json) includes both generated claims,
their exact source quotes, source links, retrieved passages, and timings.
[Run metadata](docs/example-run.json) records the model digest. This local example
took about 23 seconds including generation; model latency depends on hardware.

```bash
curl http://127.0.0.1:8000/v1/answer \
  -H 'Content-Type: application/json' \
  -d '{"question":"How do I roll back a Deployment?","top_k":5}'
```

The response includes `answer`, `citations`, `abstained`, `mode`, `fallback_reason`,
retrieved `hits`, a `request_id`, and retrieval/generation/total `timings_ms`.
Citation numbers in the answer correspond to the one-based `citations` array.

Index format 2 requires a fresh fetch and rebuild when upgrading from the initial
release. See [backup and recovery](docs/recovery.md) for migration and rollback commands.

## What is implemented

| Stage | Implementation |
|---|---|
| Ingestion | Pinned Git revision, filtered English Markdown, bounded parallel downloads, checksum checks and retry limits |
| Chunking | BGE tokenizer offsets, 220-token windows, 35-token overlap, title/heading/source metadata |
| Hybrid retrieval | SQLite FTS5 BM25 + normalized BGE vectors, combined with reciprocal-rank fusion |
| Reranking | Pinned MS MARCO MiniLM cross-encoder scores the top 30 fused candidates |
| Context | Relevance threshold, passage-token budget, at most two chunks per source page |
| Answers | Local Ollama, Pydantic schema validation, source-ID and verbatim-quote checks |
| Failure behavior | Abstention, bounded repair, labeled excerpts on model failure |
| Serving | Credential-bound clients, separate index snapshots, persistent quotas, body/deadline limits, bounded workers, readiness, metrics |
| Index safety | Validated checksums including FTS data, atomic publication, immutable backups, tested restore, consistent readers |
| Evaluation | Full-corpus retrieval/abstention and real-model ticket gates, saved failures, versioned references, automatic CI |

[Production deployment](docs/deployment.md) provides a TLS Compose profile and private
configuration bootstrap.

Production hardening is in progress. See the [operating controls](docs/operations.md)
and [acceptance plan](docs/production-plan.md) for verified behavior and remaining work.

[Quality gates and retained failures](docs/quality.md) document what the current scores
measure and the errors that remain.

## Structured output engine (project #2)

The reusable engine enforces Pydantic schemas, retries malformed or invalid output,
and validates fallbacks. RAG uses it internally; you can also use it independently.

```bash
# No model or API key needed: demonstrate malformed JSON, repair, and fallback.
uv run python examples/structured_demo.py

# Real extraction through the installed local Ollama model.
uv run citestack extract "The checkout service is down for all customers. Requests return HTTP 503."

# API without downloading embedding models or building a RAG index.
uv run citestack serve --structured-only
```

`POST /v1/structured/ticket` returns typed ticket data and explicit success/fallback
status, attempt counts, and safe validation diagnostics. See the
[engine guide and Python API](docs/structured-output.md).

## Evaluation and tests

The expanded suite over 1,180 pages measures **39/40 source hit@5**, **40/40
answerable coverage**, and **8/8 abstentions** in each unrelated/private-state category.
Fresh Linux generation scored **20/20 exact ticket fields and schema-valid successes**.
See [quality methodology and preserved failures](docs/quality.md). These are authored
development cases, not held-out accuracy claims.

The original retrieval-strategy comparison below used the earlier 1,175-page /
24,580-chunk corpus on macOS ARM64,
using CPU retrieval and the same 20 questions in every mode:

| Retrieval | Hit rate@5 | MRR@5 | Median retrieval latency |
|---|---:|---:|---:|
| BM25 | 85% | 0.602 | 15 ms |
| Dense | 90% | 0.560 | 14 ms |
| Hybrid (RRF) | 85% | 0.735 | 29 ms |
| Hybrid + reranking | **95%** | **0.756** | 642 ms |

Reranking found the labeled page for 19/20 questions, compared with 17/20 for
BM25, at a clear latency cost. Hybrid fusion alone did not improve hit rate here.
These are local development measurements, not load-test or hosted-service SLAs.
See [all per-question results](docs/evaluation.json) and
[the verification record](docs/verification.md).

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run citestack eval --output docs/evaluation.json
```

The evaluation uses 20 authored smoke questions, one labeled source page per question,
and a top-five cutoff. These are development cases, **not a held-out accuracy benchmark**.
The default CLI gate requires reranked hit rate@5 of at least 0.8. See
[evaluation methodology](evals/README.md) for definitions and limits.

CI runs lint, formatting, and isolated unit/API tests on pushes and pull requests.
Automatic **Quality gates** build or restore a verified public index keyed by source
and locked dependencies, enforce retrieval/abstention
thresholds, and run pinned real-model extraction against versioned references. The
manual **Neural retrieval evaluation** keeps the four-strategy comparison available.
**Security and operations** audits locked dependencies, scans Git history for secrets,
and tests alert rules. The public repository protects `main` with all seven required
GitHub Actions checks, including the two real-model gates; administrator updates are
subject to the same checks.

## Docker

```bash
docker compose build
docker compose run --rm api fetch
docker compose run --rm api ingest
docker compose up -d
```

The container runs as a non-root user. Named volumes persist documents, index, and
model weights. The published port binds to localhost. For Ollama mode, the container
uses `host.docker.internal`; the host Ollama server must accept that connection.
If it cannot, responses explicitly report an extractive fallback.

## Configuration and operations

All settings use the `CITESTACK_` prefix and can be loaded from `.env`.

| Setting | Default | Purpose |
|---|---|---|
| `INDEX_PATH` | `data/index.sqlite` | Snapshot location |
| `DEVICE` | `cpu` | `cpu`, `mps`, or an available CUDA device |
| `ANSWER_MODE` | `extractive` | `extractive` or `ollama` |
| `OLLAMA_MODEL` | `qwen3:4b` | Installed model for generation |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Operator-configured model server |
| `CONTEXT_TOKENS` | `1600` | Retrieval-tokenizer context allowance |
| `MIN_RERANK_SCORE` | `0` | Initial relevance cutoff; requires domain calibration |
| `API_KEY` | unset | `X-API-Key` authentication; production requires a key or tenant registry |
| `MAX_CONCURRENT_REQUESTS` | `2` | Saturation returns 503 + `Retry-After` |
| `STRUCTURED_MAX_ATTEMPTS` | `2` | Total model attempts for extraction and RAG generation (1–5) |

Use `uv run citestack inspect` to view corpus and model metadata. To refresh a corpus,
fetch and ingest again, then restart the API. The fetcher is intentionally pinned;
updating its source revision is an explicit code change. A running process keeps
its existing snapshot until restart.

To index your own public documents, supply JSONL records with `id`, `title`, `url`,
and `text`, then run `uv run citestack ingest --corpus path/to/corpus.jsonl`.
Use stable IDs and safe, public source URLs. Corpus content and model weights stay
outside Git; `.env` is ignored.

## Boundaries and next steps

- Exact quote matching proves provenance, not that a quote supports every generated claim.
- Source Markdown is cleaned, not rendered through Hugo; includes and some template content are omitted.
- Token windows can split sections and code. The context allowance uses the retrieval
  tokenizer, not the exact tokenizer of every generation model.
- Exact vector search is suitable for this demonstrated corpus; larger deployments
  should benchmark ANN indexes and separate storage.
- The [production profile](docs/deployment.md) supplies TLS, client isolation, persistent
  admission quotas, and resource limits. Operators still supply certificates, firewall
  rules, and alert routing. This is a single-host service with planned restart downtime.
- [Load measurements](docs/capacity.md) and dependency-recovery checks state their hardware
  and workload limits; they do not establish customer SLAs or distributed capacity.
- A zero reranker threshold is a starting value, not calibrated confidence. Add
  independently labeled unanswerable questions before relying on abstention behavior.

See [architecture and tradeoffs](docs/architecture.md),
[the interview walkthrough](docs/interview-guide.md), and [source attribution](NOTICE.md).
Code is MIT licensed; downloaded documents and model weights retain their own licenses.
