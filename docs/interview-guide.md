# Understand the project before putting it on your CV

Run the pipeline, inspect the returned passages, and reproduce the evaluation.
Do not describe development smoke scores as production accuracy or claim users,
business impact, or deployment scale that has not been demonstrated.

Be prepared to explain these decisions with examples from actual runs:

1. Why lexical search can help identifiers and exact configuration names, while
   embeddings can help paraphrases; inspect a query where the rankings differ.
2. Why scores from BM25 and cosine similarity should not simply be added, and what
   reciprocal-rank fusion does instead.
3. Why a cross-encoder is more expensive than a single query embedding, and why it
   only scores a candidate shortlist.
4. What overlapping token windows solve and where they cut code or sections poorly.
5. How a rebuild avoids readers combining new lexical data with old vectors.
6. Why a valid quote and URL do not establish that an answer is factually entailed.
7. What happens when the model is offline, produces bad JSON, or cannot find evidence.
8. Why an API process has an inference concurrency limit, and what a real deployment
   would need beyond this repository.

Make one improvement of your own and measure it on an independently labeled set.
Possible experiments: heading-aware splitting, document-level recall, a larger
candidate pool, contextual compression, or a calibrated abstention threshold.
Report the tradeoff in latency and quality, including regressions.

For the structured-output engine, be ready to explain why syntactically valid JSON
can still fail a schema, why strict types matter, which provider failures should be
retried, why fallback data is validated, and why a valid extraction can still be
factually wrong. Run `examples/structured_demo.py` and follow the failure codes across
attempts. The standalone ticket endpoint is a concrete second use of the same engine.
