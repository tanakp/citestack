# Production hardening acceptance plan

Status: in progress. This is an acceptance checklist, not a certification or a claim
that the current release already satisfies these requirements.

The starting release (0.2) has optional API-key authentication, in-process concurrency
control, atomic index replacement, strict structured outputs, and development smoke
tests. Its own architecture document identifies missing operational controls.

The production target is a deployable API with both documentation RAG and structured
extraction, with credential-bound clients and distinct index snapshots on one host.
The owner delegated the deployment choice; this topology makes isolation and resource
limits demonstrable without claiming distributed SaaS operations. Existing functionality must
remain usable. No production credentials, raw private inputs, or local model caches
may be published.

## Required evidence before completion

- [ ] Fail-closed production settings, secret-safe configuration and error reporting,
  mandatory authentication, key rotation, and explicit network exposure controls.
- [ ] Request-body and receive-time limits, request rate limits, bounded inference,
  total generation/request deadlines, capped provider responses, and tests of overload,
  cancellation, slow upstreams, and malformed requests.
- [ ] Liveness distinct from dependency readiness, graceful shutdown/draining,
  correlated request logs without sensitive payloads, bounded-cardinality metrics,
  and actionable alert definitions.
- [ ] Corpus/index integrity validation, safe and bounded ingestion, retained source
  meaning, reproducible versioned snapshots, and a tested backup/restore/rollback path.
- [ ] Expanded retrieval/abstention and structured-output evaluation with explicit
  quality gates, preserved failures, and measured operating limits on real models/data.
- [ ] A hardened, reproducible deployment (including secrets, TLS ingress, resource
  limits, health checks, persistence, and upgrade/rollback instructions) exercised in CI.
- [ ] CI tests security/reliability invariants, real container behavior, dependency
  vulnerabilities, and meaningful quality checks rather than only imports or CLI help.
- [ ] Load/failure experiments and an operator runbook describe supported capacity,
  model limitations, recovery procedures, and residual risks backed by actual results.
- [ ] All changes are reviewed against this checklist, pushed to GitHub, and final
  checks pass at the published commit. No broad guarantee rests on a narrow smoke test.

## Current findings

1. Missing API keys silently disable authentication, even when serving on all interfaces.
2. Request validation can echo input values; there is no ingress body cap or rate limit.
3. HTTPX's current per-operation timeout is not a total generation deadline. Provider
   response bodies are buffered without a byte cap, and retries multiply the timeout.
4. Readiness reports success without checking the configured model dependency.
5. Concurrency is process-local and no operating topology/capacity is enforced or measured.
6. Prometheus metrics, alerts, and a production deployment profile are absent.
7. Index loading verifies dimensions/model ID but not comprehensive snapshot integrity.
8. Markdown cleanup drops glossary shortcode display text, changing source meaning.
9. Retrieval evaluation has 20 development cases and no representative abstention gate.
10. Container CI only builds the image and runs `--help`; it does not exercise a service.

Progress and measured evidence will be recorded here as the requirements are implemented.

## Progress: admission and transport controls (2026-09-12)

Implemented on `production-hardening`:

- Production startup requires credentials and an explicit host allowlist. Secrets use
  redacted settings; a mounted key file and primary/previous key rotation are supported.
- Registry keys bind clients to distinct snapshots. Caller-supplied tenant headers do
  not select data. Startup, search, and cited-answer isolation are tested with two
  actual SQLite snapshots and deterministic retrieval model doubles.
- Transactional SQLite token buckets and daily admission limits persist across API
  restarts. Concurrent admission and backwards-clock behavior are tested.
- HTTP body caps, receive/total deadlines, bounded inference and quota executors,
  strict query validation, safe errors, correlated logs, and Prometheus metrics exist.
  Worker slots remain occupied after caller cancellation until the work really ends.
- The Ollama transport streams into a bounded buffer, rejects compressed responses,
  disables ambient proxies/redirects, checks completion, and has an async total deadline.
  Generation retries share one total budget. Readiness verifies the configured model
  is listed by Ollama; liveness stays independent.
- Local verification: 121 tests passed; Ruff lint/format and diff checks passed.
  A separate TCP process integration check passed 12 checks using a deterministic
  Ollama protocol peer. The CI container job now runs that script under a read-only
  filesystem, dropped capabilities, and `no-new-privileges`.

These results do not establish the remaining checklist items. Still required: expanded
quality and abstention evaluation, index/ingestion integrity and recovery, a complete
TLS deployment profile, security audit automation, monitoring alerts, measured load
limits with real models, and verification of the final published release.

## Progress: snapshot integrity and recovery (2026-09-12)

Index format 2 now validates a content identifier covering metadata, vectors, payloads,
SQL schema, and FTS shadow tables; it also checks counts, identifiers, normalization,
and matching search text. Build and backup/restore run the native FTS integrity check.
`inspect`, `backup`, and `restore` work without loading models. Backups cannot overwrite
an existing destination; restore validates a private copy before atomic publication.
Tests cover corruption, resealed invalid vectors, stale corpus provenance, failed
restore preserving the destination, shared publication locks, and reader consistency.

The glossary fix was exercised on a fresh pinned download: 1,180 usable pages (all
1,175 previous pages plus five newly qualifying pages). Corpus/manifest digests now
bind provenance to the documents actually ingested. Ingestion also bounds tree bytes,
corpus bytes/count, line sizes, and pending download results behind slow files.

Local verification: 144 tests and Ruff checks passed. The full 132,685,824-byte snapshot
was inspected, backed up, restored, and queried with the real BGE/cross-encoder models.
A deliberately corrupted backup was rejected without changing the restoration target.
The existing 20-case development evaluation remains 95% hit@5 for reranking (19/20).
See [recovery verification](recovery-verification.json), [per-question results](evaluation-v2.json),
and the [recovery runbook](recovery.md). This evidence does not replace the outstanding
expanded quality/abstention evaluation or sustained load experiments. The local default
now uses format 2; the old index and corpus were preserved under ignored `data/backups/`.

## Progress: real-model quality gates (2026-09-12)

Added 56 retrieval/abstention cases and 20 synthetic ticket cases with strict dataset
validation, policy floors, versioned reference digests, and a 2.5-point regression
limit. Per-case journals preserve completed observations on interrupted runs. Reports
include failed examples and reproducibility metadata. The automatic workflow now
rebuilds the full corpus and uses actual retrieval models plus a pinned Ollama image
and exact model digest; it does not substitute mock generation for model quality.

The initial retrieval baseline found private-state abstention at 1/8. An explicit
scope boundary improves this regression set to 8/8 without reducing 40/40 answerable
coverage; source hit@5 is 39/40 and unrelated abstention is 8/8. The ticket suite scores
18/20 exact field matches with 20/20 valid successful outputs. A prompt candidate
regressed to 17/20 and was reverted; both reports remain published. All are development
cases, not held-out accuracy claims. See [quality methodology](quality.md).

Local unit/API verification: 165 tests pass. Automated model jobs and eventual required
branch checks still need confirmation at the published revision. Complete deployment,
security audit automation, monitoring alerts, measured load limits, and final release
verification remain outstanding.

The first automatic Linux retrieval gate passed at 39/40 with all abstention and
provenance checks passing. The structured gate correctly failed at 17/20 versus the
18/20 reference because three service identifiers included an extra generic noun.
A source-grounded canonicalization rule now addresses that output contract mismatch;
the original Linux report remains in `docs/quality-linux-baseline.json`. A separate
unit test timing race was removed by giving the overload assertion its own request
budget after exercising the short timeout. Local checks now pass 169 tests. The
reference thresholds remain unchanged; fresh Linux verification is required.
