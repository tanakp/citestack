# Structured Output Engine

CiteStack 0.2 extracts the schema/retry logic previously embedded in RAG into a
reusable, provider-independent engine. RAG uses the same engine while retaining its
source-quote validation and domain-specific excerpt fallback.

## Try it without a model

```bash
uv sync --frozen --python 3.12
uv run python examples/structured_demo.py
```

This deterministic demonstration supplies deliberately malformed provider responses.
The first attempt is truncated JSON, the second uses a string where an integer is
required, and the third succeeds. A separate run exhausts its attempts and returns a
validated fallback. The demo executes no deployments and makes no network requests.

## Use your Pydantic models in Python

```python
from pydantic import BaseModel, Field
from citestack.config import Settings
from citestack.providers import OllamaProvider
from citestack.structured import StructuredOutputEngine

class Capacity(BaseModel):
    service: str
    replicas: int = Field(ge=1, le=20)

engine = StructuredOutputEngine(OllamaProvider(Settings()), max_attempts=3)
result = engine.run(
    Capacity,
    "Extract the requested capacity: checkout needs 3 replicas.",
)
if result.status == "success":
    print(result.data.replicas)  # a validated integer
else:
    print(result.status, result.errors)  # data is None when no fallback is supplied
```

Providers are callables accepting a `GenerationRequest` and returning the model's raw
string. The request contains the prompt, system instruction, JSON schema, and attempt
number. Implement another adapter without changing parsing, validation, or fallback
logic. The supplied adapter uses local Ollama; no hosted API key is required.

`engine.run(..., validate=callback)` adds business validation. A callback receives
the typed model, may canonicalize values, and raises `ValueError` to reject output.
The engine revalidates the model after callback mutation. Unexpected programming
errors such as `RuntimeError` propagate instead of being reported as model failures.

`engine.run(..., fallback={...})` supplies an explicit fallback. Fallback values go
through the same schema and business validation. Invalid fallbacks return `failed`
with `data=null`; they never masquerade as a successful model response.

## Validation and recovery contract

| Condition | Behavior |
|---|---|
| Valid JSON and schema | Return a typed model with `status=success` |
| A single outer Markdown JSON fence | Remove only that fence and validate the entire contents |
| Truncated JSON, prose around JSON, trailing content | Ask the provider to regenerate; never guess missing data |
| Duplicate keys, NaN, Infinity, overflowing floating-point literals | Reject before Pydantic validation |
| Wrong types, unknown fields, missing required fields, invalid constraints | Reject; strict JSON validation and `extra=forbid` also apply to nested models |
| Business rule failure | Retry with a safe diagnostic code |
| HTTP 429 or 5xx | Retry within the same attempt budget |
| Timeout, connection failure, non-retryable HTTP error | Stop model attempts and use fallback if supplied |
| Exhausted attempts | Return validated fallback, or `failed` with no data |

Strict validation follows **Pydantic's JSON semantics**: JSON strings are appropriate
for types such as dates and UUIDs, but a string `"3"` is rejected for an integer field.
Schemas and custom validators are trusted application code; the API does not accept
arbitrary Python or user-defined JSON schemas.

The default is two total attempts, configurable with `CITESTACK_STRUCTURED_MAX_ATTEMPTS`
(1–5). Retries wait 0.25 seconds, doubling up to a five-second cap. Provider calls use
`CITESTACK_GENERATION_TIMEOUT` (90 seconds by default). HTTPX timeouts apply to network
operations, not a guaranteed end-to-end deadline; multiple attempts can extend a
request. Raw model text is limited to 32,000 characters before parsing. This is not
a cap on the complete HTTP response download; use transport/gateway limits for that.

Results report `status`, typed `data`, provider-call `attempts`, safe `errors`,
`schema_name`, and `elapsed_ms`. No raw failed output is returned. Repair feedback
uses error categories and trusted top-level field names, not raw failed responses or
validation messages containing input values. Logs contain only schema, outcome,
attempt count, error categories, and elapsed time. Successful extraction naturally
contains data from the caller's input, so callers still control its downstream storage.

## Standalone support-ticket extraction

Start Ollama and install the model once:

```bash
ollama pull qwen3:4b
uv run citestack extract "The checkout service is down for all customers. Requests return HTTP 503. Please investigate."
```

The output schema contains a summary, category, priority, affected services, and a
human-review flag. Unknown classifications require human review. Named affected
services must occur in the input. On failure, the fallback has unknown category and
priority, an empty service list, and `requires_human_review=true`.

The CLI exits **0** for successful model output and **2** for fallback or failure,
while printing the result as JSON in both cases. This lets scripts detect degraded
behavior without parsing free-form error text. See the
[recorded real-model response](structured-example.json).

Serve extraction without an index or embedding-model downloads:

```bash
uv run citestack serve --structured-only
curl http://127.0.0.1:8000/v1/structured/ticket \
  -H 'Content-Type: application/json' \
  -d '{"text":"The checkout service is down for all customers. Requests return HTTP 503. Please investigate."}'
```

The normal RAG server also exposes this endpoint. Both modes share API-key
authentication and inference concurrency limits. HTTP 200 means an extraction result
was delivered; inspect `status` to distinguish success and fallback. Health/readiness
in structured-only mode means the application is ready, not that Ollama is reachable.
RAG endpoints return 503 when that mode disables the retrieval service.

## Verified example and regression checks

Version 0.2 passes 77 unit/API tests, including the original RAG suite. The recorded
ticket example was generated by the installed `qwen3:4b` model on the first attempt,
with no fallback. A standalone API request against the real model was also checked,
and RAG generation still returned citations whose quotes match the retrieved passages.
See [verification metadata](structured-verification.json) for input and model digest.

Ollama supports a subset of JSON Schema constraints. The built-in ticket model keeps
nonblank-string checks in Pydantic field validators because the local Ollama server
rejected an unanchored regex in `format`. Those checks still run after generation;
they are never skipped. Custom schemas may also require provider-specific adaptation.

## What this does not prove

Valid JSON is not the same as accurate extraction. The engine cannot prove that a
summary or priority assignment is factually correct, and an LLM can follow injected
instructions despite prompt mitigations. Review ambiguous or high-impact decisions.
No action is executed by the extractor. There is no general-purpose JSON guessing,
remote tool execution, or automatic fulfillment of extracted tickets.

## References

- [Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/)
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs)
