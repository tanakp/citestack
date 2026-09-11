import json

import pytest

from citestack.evaluation import evaluate


def test_evaluation_compares_all_modes(retriever, tmp_path):
    dataset = tmp_path / "eval.jsonl"
    dataset.write_text(
        json.dumps({"question": "Secret confidential password token", "relevant_path": "/secrets"})
    )
    result = evaluate(retriever, dataset)
    assert set(result["modes"]) == {"bm25", "dense", "hybrid", "reranked"}
    assert all(mode["hit_rate_at_k"] == 1 for mode in result["modes"].values())
    assert all(mode["mrr_at_k"] == 1 for mode in result["modes"].values())


def test_missing_golden_source_cannot_silently_pass(retriever, tmp_path):
    dataset = tmp_path / "eval.jsonl"
    dataset.write_text(json.dumps({"question": "Unrelated", "relevant_path": "/missing"}))
    with pytest.raises(ValueError, match="Missing golden source"):
        evaluate(retriever, dataset)
