# Operating controls

The release targets one API process on one host, with distinct client index snapshots.
See the [acceptance evidence](production-plan.md), [TLS deployment profile](deployment.md),
[capacity experiments](capacity.md), monitoring integration below, and
[recovery runbook](recovery.md). Host security, certificates, backup retention, and
notification destinations remain operator responsibilities.

## Production configuration

`CITESTACK_ENVIRONMENT=production` fails startup unless authentication is configured
and `CITESTACK_ALLOWED_HOSTS` explicitly lists the serving hostnames. Development's
`testserver` host and the catch-all `*` are rejected. `/docs`, `/redoc`, and
`/openapi.json` are disabled. Use the generated OpenAPI contract in development.

For a single client, supply a random 32+ character `CITESTACK_API_KEY`, preferably
through a mounted file named by `CITESTACK_API_KEY_FILE`. The optional
`CITESTACK_PREVIOUS_API_KEY` permits a rotation overlap. Restart after changing keys;
then remove the previous key and restart after callers migrate. Secret values are
redacted in settings representations. Never put real keys in Git or shell command
arguments, commit `.env`, or publish request/response payloads.

For several clients, `CITESTACK_TENANT_CONFIG` points to a local JSON registry:

```json
{
  "tenants": [
    {
      "id": "client-a",
      "key_hashes": ["REPLACE_WITH_SHA256_OF_A_RANDOM_32_BYTE_KEY"],
      "index_path": "client-a.sqlite",
      "rate_per_minute": 30,
      "burst": 5,
      "daily_limit": 1000
    }
  ]
}
```

This intentionally invalid placeholder must be replaced with the 64-character
lowercase SHA-256 digest of a cryptographically random key. Clients send the original
key in `X-API-Key`; they never send the hash. A registry supports up to four active
key hashes per client, up to 128 clients, and cannot exceed 256 KiB. Paths are relative
to the registry file unless absolute. Duplicate IDs, key hashes, and resolved index
paths fail startup. Every RAG client needs its own index. In structured-only mode an
index is unnecessary. All entries are loaded at startup; the file is operator-owned,
not writable through the API. Registry authentication takes precedence over the
single-client API key configuration.

`X-Tenant-ID`, URL query fields, and generated output cannot select the data store.
Schemas are defined by trusted application code. There is no endpoint for users to
submit Python schemas or execute tools. Client data is separated at the retrieval
layer; the model process and host remain shared, not separate security sandboxes.

## Admission and timeout behavior

| Control | Default | Scope |
|---|---:|---|
| `MAX_HTTP_REQUESTS` | 64 | Active admitted HTTP requests in one process |
| `MAX_CONCURRENT_REQUESTS` | 2 | Inference workers; no pending submission queue |
| Quota executor | 4 workers | Local SQLite admission operations, no pending queue |
| `MAX_REQUEST_BYTES` | 65,536 | Actual POST bytes, including chunked requests |
| `BODY_TIMEOUT` | 5 seconds | Receive the entire request body |
| `REQUEST_TIMEOUT` | 120 seconds | HTTP handling, including admission and inference wait |
| `GENERATION_TIMEOUT` | 90 seconds | One complete upstream request |
| `GENERATION_BUDGET` | 90 seconds | All generation attempts and backoff |
| `PROVIDER_MAX_BYTES` | 262,144 | Complete uncompressed Ollama response |
| `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_BURST` | 30 / 5 | Default client's token bucket |
| `DAILY_REQUEST_LIMIT` | 1,000 | Default client's admitted POST count per UTC day |
| `SHUTDOWN_TIMEOUT` | 30 seconds | Worker drain wait during application shutdown |

Environment variables in the table have the `CITESTACK_` prefix. Registry entries
supply their own client rate/burst/daily settings. Limits are admission counts, not
per-token billing. Authenticated POST requests consume admission before body/schema
validation, so malformed requests and attempts rejected later by inference capacity
still count. No refunds are issued for timeouts or fallbacks. GET requests have HTTP
concurrency limits but do not consume client POST quotas.

`CITESTACK_QUOTA_PATH` defaults to `data/quotas.sqlite`. Persist this file and its SQLite
WAL state on a local writable volume. Startup creates it if absent. Do not put it on
network storage or remove it during a service restart: removing it resets quotas.
Transactional admission prevents concurrent callers overspending their configured
count. Missing/broken quota tables during service return 503 rather than bypassing quotas.
A backwards clock does not replenish a bucket or reset the daily limit.

HTTP errors distinguish 401 (credentials), 408 (body receive timeout), 413 (body too
large), 415 (unsupported content type/encoding), 422 (invalid schema), 429 (quota),
503 (capacity/dependency), and 504 (request deadline). Retry advice is included for
quota/capacity errors. Successful extraction returns HTTP 200 even for a validated
fallback; inspect the result's `status` and `errors` before using its data.

Timeouts and disconnects cannot forcibly stop native CPU inference. The worker slot
stays occupied until that work ends. Shutdown drains active workers before closing
owned index connections. A container supervisor must enforce a final termination
grace period for a stalled native task. Settings are per process; running multiple
API workers multiplies model memory and concurrency. The intended topology uses one.

## Health, logs, and metrics

`/healthz` is unauthenticated liveness. `/readyz` returns 503 if draining or if Ollama
is unreachable or does not list the configured model in production/structured mode.
Development extractive mode can be ready without Ollama. A successful tags response
proves availability, not generation speed, model quality, or sufficient free memory.
Checks are cached for `CITESTACK_READINESS_CACHE_SECONDS` (default 5 seconds).

`/metrics` uses API-key authentication and exposes aggregate counts/latencies without
client IDs, prompts, query strings, or raw paths in labels. At this stage every valid
client key can read aggregate operational metrics; keep this route internal at ingress.
HTTP responses include a server-generated `X-Request-ID`. Answers and structured
outcome logs carry the same ID. The CLI disables Uvicorn's access log because it would
include raw query strings; the application's JSON request log records only known route
labels, method, status, ID, and elapsed time. Configure any reverse-proxy logging with
the same care. Successful answer payloads themselves contain source excerpts and must
be treated according to the client's data policy.

## Current verification

Run `uv run pytest -q` and `uv run python tests/container_smoke.py`. The latter launches
a temporary API process and a deterministic local Ollama protocol peer, verifies HTTP
behavior, then shuts both down. It does not download a model or test model quality.
CI executes it inside a read-only Docker container with dropped capabilities. Real
model quality measurements are recorded separately; they are not inferred from this
transport test. The production acceptance checklist remains the release authority.

## Monitoring integration

Load `deploy/alerts.yml` into your existing Prometheus and adapt
`deploy/prometheus.example.yml` to its private-network location. Mount an active client
key at `/run/secrets/metrics-key` as a read-only secret; never embed it in the YAML.
The external proxy blocks metrics. A monitor must join the private backend network
and address `api:8000`, or use an equivalently protected internal route. Scrapes use
GET and do not consume POST quotas. Rotate the scrape credential with the registry.

The rules cover unreachable API metrics (two minutes), repeated readiness failures,
over 5% HTTP server errors, and over 10% validated output fallbacks. Error/fallback
ratios require at least 20 observations in five minutes to avoid low-volume noise.
The normal container health check supplies readiness observations every 30 seconds.
CI uses Prometheus's `promtool` to test firing, hold time, recovery, and suppression.
These initial thresholds are operator policies, not established customer SLOs.

Connect your Alertmanager routing and receiver before relying on notifications;
no notification destination is configured or contacted by this repository. Also
monitor host disk, memory/OOM events, certificate expiry, and the monitor itself in
your infrastructure stack. The supplied application rules cannot detect every host
failure. Prometheus configuration and rule semantics follow the
[official documentation](https://prometheus.io/docs/prometheus/latest/configuration/configuration/).

## Incident response

1. **API unavailable:** check container health/exit state and disk availability. Use
   `/healthz` to distinguish a stopped process from `/readyz` dependency failure.
   Inspect correlated JSON logs; do not enable raw request or model-prompt logging.
2. **Readiness/fallback alert:** check `ollama list`, the pinned model digest, server
   memory and logs. Restore the expected model and verify a synthetic ticket succeeds.
   A schema-valid fallback is a degraded result, not evidence that generation works.
3. **503/504 spike:** reduce client concurrency and honor `Retry-After`; check occupied
   inference slots. A timed-out native worker still holds its slot. Restart only in a
   planned disruption window after the configured drain; do not multiply API workers
   without a memory and capacity experiment.
4. **429 spike:** check the client's UTC daily budget and token bucket. Failed POST
   attempts also count. Do not delete the quota database to clear an incident.
5. **Index failure or quality regression:** freeze promotion, run snapshot inspection,
   restore a verified backup, and rerun the appropriate quality gate. Keep the failed
   artifact and previous image for diagnosis. Follow [recovery](recovery.md) and
   [deployment rollback](deployment.md#upgrade-and-rollback).
6. **Credential exposure:** revoke the digest in the registry, deploy a new random key,
   and restart API/monitor consumers. Removing a key from Git does not revoke it.

Back up quota state consistently with SQLite's backup API, not a copy of only the
live main database while WAL writes are active. During disaster recovery, admissions
after the backup are unknown: preserve the newest intact database or pause affected
clients until their UTC quota window resets. Never promise exact spend recovery from
a stale backup. Store quota/credential backups encrypted and test restoration in an
isolated instance before replacing production state.
