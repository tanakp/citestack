# Verification record

## Version 0.2 — structured output engine

77 unit/API tests pass, including the original RAG regression tests. The reusable
engine was exercised with malformed JSON, strict type failures, nested extra fields,
bounded retries, provider errors, invalid fallbacks, and diagnostics that omit raw
input. The deterministic demo, real Ollama ticket extraction, standalone API route,
and RAG generation were checked. See [the engine guide](structured-output.md) and
[recorded verification metadata](structured-verification.json).

## Original RAG build

Date: 2026-09-11. Host: macOS 26.6.2, ARM64. Python: 3.12.13.

- Public source: Kubernetes website commit `17133089068629ec12ca15c1bdf36a60d2671a74`.
- Fetched 1,718 English Markdown source files; 1,175 remained after filtering out
  short navigation/template pages. Downloaded bytes were checked against Git blob hashes.
- Built 24,580 overlapping chunks with 384-dimensional BGE vectors. The embedding
  build used Apple MPS; retrieval measurements used CPU.
- 29 unit/API tests passed. Tests isolate external services with explicit test doubles.
- Ruff lint and formatting checks passed.
- The real neural retrieval CLI evaluation passed its 0.8 hit-rate threshold:
  BM25 0.85, dense 0.90, hybrid 0.85, reranked 0.95.
- A real Uvicorn server passed HTTP health/readiness/OpenAPI and answer checks.
  The answer's citation quotes matched the retrieved source text. An unrelated
  football prediction question returned an abstention.
- When the Ollama server was unavailable, the pipeline returned a labeled extractive
  fallback with `generation_unavailable_or_invalid`, rather than a synthetic answer.
- A generated readiness-probe explanation passed schema and source-quote checks with
  `mode=ollama` and no fallback. The full response and local model digest are saved in
  `example-answer.json` and `example-run.json`. Both claims were inspected against
  their supporting source spans.
- GitHub Actions passed the tests and built the Linux Docker image, then ran the
  container CLI successfully: [initial CI run](https://github.com/tanakp/citestack/actions/runs/34584317033).
  This container check does not run the full downloaded corpus inside Docker.

## Reading the benchmark

The evaluation is sequential within one process, excludes model loading, and uses
20 manually authored development cases. Other local processes were present during
the run, so timings are illustrative development measurements. This is not a
controlled performance benchmark, production traffic replay, or answer-quality audit.

The reranked miss was “Are Kubernetes Secrets encrypted in etcd by default?” The
single expected label was the general Secret concept page. The report preserves the
actual retrieved pages for inspection. Alternative pages can be relevant, and a
single-source label is not exhaustive. No labels or thresholds were adjusted to hide
this miss.

A comparison question about readiness and liveness produced an overbroad statement
about their differences based on a valid quote about configuration syntax. Citation
matching alone cannot detect that contextual error. The project deliberately reports
this limitation; the saved example is not evidence that every generated answer is correct.

## Reproduce

Follow the README quick start, then run:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run citestack eval --output data/evaluation.json
uv run citestack ask "What is the difference between a readiness probe and a liveness probe?" --mode ollama
```

Model revisions and the corpus revision are recorded in source and index metadata.
Local Ollama model tags are mutable; the example's model digest is recorded separately
when available. Ingesting changes to the corpus or chunk settings requires rebuilding.
