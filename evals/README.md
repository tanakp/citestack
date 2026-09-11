# Retrieval evaluation

`kubernetes.jsonl` contains 20 authored smoke questions and one expected documentation
page per question. Labels are paths into the pinned source tree. Evaluation refuses
to run when an expected source is missing.

Compare BM25, dense cosine similarity, reciprocal-rank fusion, and fusion plus neural
reranking on the same corpus, questions, candidate budget, and top-5 cutoff.
The same document-diversity policy (at most two chunks per page) applies to all modes.

- Hit rate@5: fraction of questions with the labeled page in the top five chunks.
- MRR@5: average reciprocal rank of the first chunk from that page; misses score zero.
- Latency: end-to-end retrieval including query embedding and reranking where used;
  model loading is excluded. These are sequential, single-process measurements.

These are development smoke cases, not a held-out benchmark and not a measure of
answer correctness. Only one page is labeled per query; other valid pages can be
counted as misses. Never present these scores as a production accuracy guarantee.
Quote matching validates provenance, not semantic entailment.

For a stronger evaluation, add independently labeled paraphrases, missing-answer
questions, version-sensitive cases, and adversarial content. Split development and
held-out datasets before tuning thresholds. Have a human review generated claims
against supporting passages. Report abstention precision and recall separately.

Run `uv run citestack eval --output docs/evaluation.json`. The CLI exits nonzero if
reranked hit rate is below `--min-hit-rate` (default 0.8). A manual GitHub Actions
workflow runs this check and uploads the full report. Mandatory merge protection
must be configured separately in repository settings.
