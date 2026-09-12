import json

import pytest

from citestack.extraction import TicketExtractor
from citestack.quality import (
    QualityPolicy,
    RetrievalCase,
    compare_baseline,
    load_cases,
    retrieval_quality,
    structured_quality,
    write_report,
)
from citestack.structured import StructuredOutputEngine


def test_quality_failures_preserved_and_gate_fails(retriever, tmp_path):
    path = tmp_path / "suite.jsonl"
    cases = [
        {
            "id": "positive",
            "question": "Secret confidential password token",
            "category": "documentation",
            "relevant_paths": ["/secrets"],
        },
        {
            "id": "negative",
            "question": "Secret confidential password token",
            "category": "out_of_domain",
            "relevant_paths": [],
        },
        {
            "id": "private",
            "question": "Secret confidential password token",
            "category": "private_state",
            "relevant_paths": [],
        },
    ]
    path.write_text("\n".join(json.dumps(case) for case in cases))
    result = retrieval_quality(retriever, path, QualityPolicy())
    assert result["metrics"]["retrieval_hit_rate"] == 1
    assert result["metrics"]["out_of_domain_abstention"] == 0
    assert not result["passed"]
    report = tmp_path / "failed.json"
    assert write_report(result, report) == 1
    assert len(json.loads(report.read_text())["results"]) == 3


def test_duplicate_and_incomplete_quality_suites_fail_closed(tmp_path):
    path = tmp_path / "suite.jsonl"
    case = {"id": "one", "question": "Where?", "category": "private_state", "relevant_paths": []}
    path.write_text(json.dumps(case) + "\n" + json.dumps(case))
    with pytest.raises(ValueError, match="unique"):
        load_cases(path, RetrievalCase)
    path.write_text(json.dumps(case))
    with pytest.raises(ValueError, match="three categories"):
        retrieval_quality(None, path, QualityPolicy())


def test_baseline_regression_gate_rejects_material_drop():
    baseline = {
        "suite": "retrieval",
        "dataset_sha256": "same",
        "passed": True,
        "metrics": {"retrieval_hit_rate": 1.0},
    }
    current = {
        **baseline,
        "policy": QualityPolicy().model_dump(),
        "metrics": {"retrieval_hit_rate": 0.9},
    }
    compare_baseline(current, baseline)
    assert not current["passed"]
    with pytest.raises(ValueError, match="digest"):
        compare_baseline(current, {**baseline, "dataset_sha256": "different"})


def test_structured_fallback_cannot_be_counted_as_correct(settings, tmp_path):
    case = {
        "id": "unknown",
        "text": "Something happened",
        "category": "unknown",
        "priority": "unknown",
        "affected_services": [],
        "requires_human_review": True,
    }
    path = tmp_path / "tickets.jsonl"
    path.write_text(json.dumps(case))
    extractor = TicketExtractor(settings, StructuredOutputEngine(lambda _: "bad", max_attempts=1))
    result = structured_quality(extractor, path, QualityPolicy())
    assert result["metrics"]["structured_accuracy"] == 0
    assert result["results"][0]["actual"] == result["results"][0]["expected"]
    assert not result["passed"]


def test_journal_keeps_completed_cases_if_later_work_fails(tmp_path):
    from citestack.quality import journal

    path = tmp_path / "cases.jsonl"
    record = journal(path)
    record({"id": "complete", "correct": False})
    assert json.loads(path.read_text()) == {"id": "complete", "correct": False}


def test_structured_reference_rejects_changed_model():
    baseline = {
        "suite": "structured",
        "dataset_sha256": "same",
        "passed": True,
        "model": {"digest": "old"},
        "metrics": {"structured_accuracy": 0.9},
    }
    current = {**baseline, "model": {"digest": "new"}, "policy": QualityPolicy().model_dump()}
    with pytest.raises(ValueError, match="same model"):
        compare_baseline(current, baseline)
