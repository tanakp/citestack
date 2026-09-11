import json
from pathlib import Path
from time import perf_counter

import numpy as np


def evaluate(retriever, dataset: Path, *, k: int = 5) -> dict:
    cases = [json.loads(line) for line in dataset.read_text().splitlines() if line.strip()]
    if not cases:
        raise ValueError("Evaluation dataset is empty")
    available = {chunk.url for chunk in retriever.chunks.values()}
    for case in cases:
        if not any(url.endswith(case["relevant_path"]) for url in available):
            raise ValueError(f"Missing golden source: {case['relevant_path']}")
    report = {
        "dataset": dataset.name,
        "cases": len(cases),
        "k": k,
        "index": retriever.manifest,
        "modes": {},
    }
    for mode in ["bm25", "dense", "hybrid", "reranked"]:
        results = []
        for case in cases:
            start = perf_counter()
            hits = retriever.search(case["question"], top_k=k, mode=mode)
            rank = next(
                (
                    i
                    for i, hit in enumerate(hits, 1)
                    if hit.chunk.url.endswith(case["relevant_path"])
                ),
                None,
            )
            results.append(
                {
                    "question": case["question"],
                    "expected_path": case["relevant_path"],
                    "rank": rank,
                    "latency_ms": round((perf_counter() - start) * 1000, 2),
                    "retrieved": [hit.chunk.url for hit in hits],
                }
            )
        report["modes"][mode] = {
            "hit_rate_at_k": sum(row["rank"] is not None for row in results) / len(results),
            "mrr_at_k": sum(1 / row["rank"] if row["rank"] else 0 for row in results)
            / len(results),
            "latency_p50_ms": float(np.median([row["latency_ms"] for row in results])),
            "latency_p95_ms": float(np.percentile([row["latency_ms"] for row in results], 95)),
            "results": results,
        }
    return report
