# Measured capacity and failure behavior

The [recorded experiment](load-experiment.json) ran four consecutive 60-second phases
against an actual production-mode API, the 1,180-document index, pinned BGE/reranker
models, and local Ollama 0.18.2 with qwen3:4b. It used one API process, two inference
slots, two Torch threads, and one parallel generation. Admission quotas were raised
for the experiment; normal client quotas are intentionally lower.

| Workload | Concurrent clients | Successful requests | Busy (503) | Throughput | Success p95 |
|---|---:|---:|---:|---:|---:|
| Cited extractive answers | 1 | 119 | 0 | 1.97/s | 0.56 s |
| Cited extractive answers | 2 | 126 | 0 | 2.09/s | 1.67 s |
| Cited extractive answers | 4 | 138 | 1,128 | 2.27/s | 1.17 s |
| Structured tickets | 1 | 20 | 0 | 0.32/s | 3.40 s |

Every successful retrieval response had citations; all 20 generated tickets returned
`status=success`. These assertions check response contracts, not field accuracy or
entailment. Model quality is measured by the separate [quality suite](quality.md).
The overload phase offers a new request after each completed request or a 100 ms
pause after rejection. Its high rejection count is intentional offered overload,
not a normal-traffic failure rate. The worker pool admits at most two native tasks.

The experiment then stopped its own real Ollama process. Liveness stayed 200,
readiness returned 503, and extraction returned a validated fallback. Restarting
Ollama restored readiness and successful generation without restarting the API.
Temporary servers and quota state were cleaned up by the harness.

## Operating recommendation and limits

Start with one in-flight request per client and at most two retrieval requests across
this API process. Keep generated extraction at one concurrent request per model
server; honor 429/503 retry guidance and inspect fallback status. The second retrieval
slot buys little additional throughput in this experiment, while higher concurrency
mostly produces admission rejection. Defaults (30 admitted POSTs/minute/client,
burst five, 1,000/day) are admission policy, not a guaranteed throughput entitlement.

These numbers came from a 10-logical-CPU ARM Mac with no container memory/CPU cap.
Ollama can use Metal on this platform. They are **not Linux CPU capacity estimates**.
Requests were repeated, caches warm, and the load generator was closed-loop; it omits
time spent waiting to offer another request. Four minutes is a bounded load experiment,
not an endurance or leak-free claim. This does not establish an SLA, multi-host scaling,
large-tenant-count capacity, or behavior on arbitrary private documents.

The full-corpus CI deployment check also exercises two simultaneous extractive clients
through verified TLS under the production API's two-CPU/2-GiB container limits. Its
`deployment-load.json` artifact records independent Linux measurements and fails on
non-200 responses or missing citations. That result must pass before release; do not
substitute the Mac figures for it. Re-run on your intended host, corpus and traffic mix
before increasing limits or making a customer latency commitment.

## Reproduce

After the README setup and `ollama pull qwen3:4b`:

```bash
HF_HOME="$PWD/.cache/huggingface" uv run python tests/load_experiment.py --seconds 60
```

The harness starts its own servers on temporary loopback ports and writes reports
under ignored `data/`. Publish only reviewed aggregate reports; the sibling private
log is for local diagnosis. Do not point a load test at a live customer service.
