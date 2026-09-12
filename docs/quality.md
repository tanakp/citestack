# Quality gates and known failures

The automatic `Quality gates` workflow evaluates real pinned retrieval models against
all 1,180 indexed pages and runs the ticket suite against `qwen3:4b` in a pinned Ollama
container. It runs on pull requests and pushes to the integration/main branches.
Results are uploaded even when a gate fails. Repository branch protection must require
these checks before a passing workflow becomes an enforced merge requirement; release
hardening includes that repository setting.

## Suites and contract

`evals/retrieval-quality.jsonl` has 40 answerable documentation questions, eight unrelated
questions, and eight requests for private cluster state. Source paths are checked before
execution, duplicate IDs are rejected, and all three categories must be represented.
These are authored **development/regression cases**, not independent held-out evidence.
The first 20 documentation questions remain the original suite; the second 20 broaden
coverage to disruption budgets, finalizers, service accounts, volumes, and other topics.

The gate distinguishes source retrieval, answerable coverage, abstention by category,
and exact citation provenance. Coverage means the service returned an answer/excerpt,
not that a human judged it correct. Source-page relevance labels are not exhaustive.
Quote matching does not prove that a generated claim is entailed by a source. The
mandatory retrieval job runs the default extractive mode; generated-answer entailment
is not inferred from its scores.

`evals/structured-quality.jsonl` has 20 synthetic support requests across availability,
performance, configuration, informational questions, and insufficient information.
One request contains an instruction to ignore the output schema. Correctness requires
`success` plus an exact match for category, priority, named services, and review flag.
Service order/case are normalized; summaries are schema-validated but not graded for
semantic accuracy. A safe fallback is **never** counted as a correct extraction, even
when its unknown fields happen to match the golden values.

## Thresholds and regressions

The versioned policy sets floors of 85% retrieval hit@5, 90% answerable coverage,
90% abstention in each negative category, 100% citation provenance, 85% structured
field accuracy, and 90% structured success. The reference gate additionally rejects
metric drops larger than **2.5 percentage points** from the accepted reference. With
20 ticket cases, one new failure is a five-point drop and fails that regression check.
This tolerance is explicit; it is not a promise that every numerical change is blocked.

A reference must be a passing report for the exact suite and dataset SHA-256. Structured
references also require the exact model digest and Ollama version. Intentional model,
data, or policy changes require review of the new complete report before updating a
reference. Do not lower a threshold or remove a difficult case merely to make CI green.

```bash
uv run citestack quality --output data/quality-retrieval.json \
  --baseline evals/retrieval-reference.json

# Requires the matching local Ollama model/version.
uv run citestack quality --suite structured --output data/quality-structured.json \
  --baseline evals/structured-reference.json
```

The CLI exits 1 when a floor or regression check fails. It writes the full report before
exiting, including failed rows, retrieval paths/scores, answers, and expected/actual
fields. A sibling `.cases.jsonl` journal flushes each completed case so a later timeout
or process termination does not erase earlier outcomes. Reports record dataset identity,
source checksum, runtime/library versions, model/index identity, and policy. Run quality
suites only with data approved for storage in reports/artifacts; these bundled inputs
contain public documentation questions and synthetic tickets, never customer secrets.

## Measured local results

| Check | Before | Accepted implementation |
|---|---:|---:|
| Documentation source hit@5 | 39/40 | 39/40 |
| Answerable coverage | 40/40 | 40/40 |
| Unrelated-query abstention | 8/8 | 8/8 |
| Private-state abstention | 1/8 | 8/8 |
| Citation provenance | 100% | 100% |
| Ticket exact field accuracy | 18/20 | 18/20 |
| Ticket schema-valid success | 20/20 | 20/20 |

The [retrieval baseline](quality-baseline.json) preserves a concrete problem: public
documentation excerpts were returned for questions about a caller's actual cluster.
The service now declines explicit requests for private state before retrieval and
reports `abstention_reason=requires_private_context`. Procedural questions such as
“How do I list my Pods?” still retrieve documentation. These are conservative syntax
rules, not a universal intent classifier: ambiguous paraphrases and mixed questions
can escape them. Extend the suite when a new miss or false rejection is found.

The [accepted ticket report](quality-structured.json) retains two errors: one service
name includes the extra word “service,” and a vague request is classified as a question.
A candidate prompt attempted to fix these, but its exact accuracy fell to 17/20. That
change was reverted; the [candidate report](quality-structured-candidate.json) remains
available beside the [original ticket baseline](quality-structured-baseline.json).
This demonstrates why plausible prompt edits need measured regression checks.

The [accepted retrieval report](quality-retrieval.json) retains the one source-label
miss. These measured results are from a local run and are not a traffic accuracy SLA,
a sustained load test, or a security certification. CI verifies the packaged Linux
configuration separately; model generation can vary across hardware and versions.

## Linux validation and service-name normalization

The first Linux run passed all retrieval gates but produced 17/20 exact ticket matches:
three outputs appended the generic noun “service” to an otherwise correct identifier.
The unchanged 18/20 reference correctly rejected that regression. The full
[Linux failure report](quality-linux-baseline.json) is retained.

The extraction validator now canonicalizes an unquoted, explicitly present lowercase
identifier phrase such as “catalog service” to “catalog,” verifies complete name
boundaries (so “cat” cannot match “catalog”), and preserves quoted multiword names.
This is an application normalization rule, not a relaxed evaluator or lower threshold.
Revalidation of the 20 recorded Linux outputs produced 20 correct field sets; that
replay is not evidence of a new model run. Fresh Linux runs independently verify generation with the corrected validator.

Fresh Linux generation at commit `a01b125` passed at **20/20 exact field matches and
20/20 schema-valid successes** using the same pinned model and unchanged reference.
See the [full fresh Linux report](quality-linux-structured.json) and
[CI run](https://github.com/tanakp/citestack/actions/runs/34698606141). This remains
a small development regression set, not a held-out general accuracy estimate.

CI caches only public corpus/index artifacts and pinned retrieval weights, keyed by
all application source and the dependency lock. A hit still runs full snapshot
integrity inspection and every retrieval/abstention case; model quality is never
replaced by cached scores. This avoids rebuilding embeddings for documentation-only
changes. Source, model configuration, or dependency changes create a new build key.

Release confirmation at `99910ba` passed the full retrieval/deployment job twice;
structured extraction again scored 20/20. The published Linux ticket report now
records that release candidate. See [release verification](release-verification.json)
for exact run/attempt links and [Linux capacity](capacity.md#linux-deployment-measurement)
for the measured operating limits and retained earlier failure.
