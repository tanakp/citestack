"""Reproducible, failure-preserving quality gates for retrieval and extraction."""

import hashlib
import json
import os
import platform
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from citestack.answers import AnswerService


class RetrievalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1)
    question: str = Field(min_length=3)
    category: Literal["documentation", "out_of_domain", "private_state"]
    relevant_paths: list[str]

    @model_validator(mode="after")
    def labels(self):
        if bool(self.relevant_paths) != (self.category == "documentation"):
            raise ValueError("Documentation cases need source labels; abstention cases must not")
        return self


class QualityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    retrieval_hit_rate: float = Field(default=0.85, ge=0, le=1)
    answerable_coverage: float = Field(default=0.90, ge=0, le=1)
    out_of_domain_abstention: float = Field(default=0.90, ge=0, le=1)
    private_state_abstention: float = Field(default=0.90, ge=0, le=1)
    citation_provenance: float = Field(default=1.0, ge=0, le=1)
    structured_accuracy: float = Field(default=0.85, ge=0, le=1)
    structured_success: float = Field(default=0.90, ge=0, le=1)
    max_regression: float = Field(default=0.025, ge=0, le=1)


def load_cases(path, schema):
    raw = path.read_bytes()
    cases = [schema.model_validate_json(line) for line in raw.splitlines() if line.strip()]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Quality dataset must be nonempty and contain unique IDs")
    return cases, hashlib.sha256(raw).hexdigest()


def gate(metrics, policy):
    thresholds = policy.model_dump()
    return {
        name: {"actual": value, "minimum": thresholds[name], "passed": value >= thresholds[name]}
        for name, value in metrics.items()
    }


def retrieval_quality(retriever, dataset: Path, policy: QualityPolicy, *, progress=None):
    cases, digest = load_cases(dataset, RetrievalCase)
    categories = {case.category for case in cases}
    if categories != {"documentation", "out_of_domain", "private_state"}:
        raise ValueError("Retrieval quality suite must cover all three categories")
    available = {chunk.url for chunk in retriever.chunks.values()}
    for case in cases:
        for path in case.relevant_paths:
            if not any(url.endswith(path) for url in available):
                raise ValueError(f"Missing golden source for {case.id}: {path}")
    service = AnswerService(retriever, retriever.settings)
    rows = []
    valid_citations = citations = 0
    for case in cases:
        answer = service.answer(case.question)
        rank = next(
            (
                i
                for i, hit in enumerate(answer.hits, 1)
                if any(hit.chunk.url.endswith(path) for path in case.relevant_paths)
            ),
            None,
        )
        sources = {hit.chunk.id: hit.chunk for hit in answer.hits}
        verified = all(
            c.source_id in sources
            and c.quote in sources[c.source_id].text
            and c.url == sources[c.source_id].url
            for c in answer.citations
        )
        citations += len(answer.citations)
        valid_citations += len(answer.citations) if verified else 0
        rows.append(
            {
                **case.model_dump(),
                "rank": rank,
                "abstained": answer.abstained,
                "mode": answer.mode,
                "fallback_reason": answer.fallback_reason,
                "abstention_reason": answer.abstention_reason,
                "answer": answer.answer,
                "citations": [c.model_dump() for c in answer.citations],
                "retrieved": [
                    {"url": hit.chunk.url, "score": hit.rerank_score} for hit in answer.hits
                ],
                "timings_ms": answer.timings_ms,
            }
        )
        if progress:
            progress(rows[-1])
    positive = [r for r in rows if r["category"] == "documentation"]
    metrics = {
        "retrieval_hit_rate": sum(r["rank"] is not None for r in positive) / len(positive),
        "answerable_coverage": sum(not r["abstained"] for r in positive) / len(positive),
        "citation_provenance": valid_citations / citations if citations else 0.0,
    }
    for category in ("out_of_domain", "private_state"):
        group = [r for r in rows if r["category"] == category]
        metrics[category + "_abstention"] = sum(r["abstained"] for r in group) / len(group)
    checks = gate(metrics, policy)
    return {
        "suite": "retrieval",
        "dataset_sha256": digest,
        "cases": len(cases),
        "index": retriever.manifest,
        "min_rerank_score": retriever.settings.min_rerank_score,
        "answer_mode": retriever.settings.answer_mode,
        "policy": policy.model_dump(),
        "metrics": metrics,
        "checks": checks,
        "passed": all(check["passed"] for check in checks.values()),
        "results": rows,
    }


class TicketCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    text: str
    category: Literal["availability", "performance", "configuration", "question", "unknown"]
    priority: Literal["low", "medium", "high", "unknown"]
    affected_services: list[str]
    requires_human_review: bool


def structured_quality(extractor, dataset: Path, policy: QualityPolicy, *, progress=None):
    cases, digest = load_cases(dataset, TicketCase)
    rows = []
    for case in cases:
        result = extractor.extract(case.text)
        expected = case.model_dump(exclude={"id", "text"})
        actual = result.data.model_dump(exclude={"summary"}) if result.data else None
        if actual:
            actual["affected_services"] = sorted(s.casefold() for s in actual["affected_services"])
        expected["affected_services"] = sorted(s.casefold() for s in expected["affected_services"])
        correct = result.status == "success" and actual == expected
        rows.append(
            {
                "id": case.id,
                "text": case.text,
                "expected": expected,
                "actual": actual,
                "correct": correct,
                "result": result.model_dump(),
            }
        )
        if progress:
            progress(rows[-1])
    metrics = {
        "structured_accuracy": sum(r["correct"] for r in rows) / len(rows),
        "structured_success": sum(r["result"]["status"] == "success" for r in rows) / len(rows),
    }
    checks = gate(metrics, policy)
    return {
        "suite": "structured",
        "dataset_sha256": digest,
        "cases": len(cases),
        "policy": policy.model_dump(),
        "metrics": metrics,
        "checks": checks,
        "passed": all(check["passed"] for check in checks.values()),
        "results": rows,
    }


def write_report(report, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return 0 if report["passed"] else 1


def compare_baseline(report, baseline):
    if (
        baseline.get("dataset_sha256") != report["dataset_sha256"]
        or baseline.get("suite") != report["suite"]
        or not baseline.get("passed")
    ):
        raise ValueError("Baseline must be a passing report for the same suite and dataset digest")
    if report["suite"] == "structured" and baseline.get("model") != report.get("model"):
        raise ValueError("Structured baseline requires the same model and server version")
    if set(baseline["metrics"]) != set(report["metrics"]):
        raise ValueError("Baseline metrics do not match the current suite")
    tolerance = report["policy"]["max_regression"]
    report["regression_checks"] = {
        name: {
            "baseline": old,
            "actual": report["metrics"][name],
            "max_drop": tolerance,
            "passed": report["metrics"][name] + 1e-12 >= old - tolerance,
        }
        for name, old in baseline["metrics"].items()
    }
    report["passed"] = report["passed"] and all(
        check["passed"] for check in report["regression_checks"].values()
    )


def journal(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")

    def append(row):
        with path.open("a") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    return append


def provenance():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return {
        "started_at": datetime.now(UTC).isoformat(),
        "source_sha256": digest.hexdigest(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "libraries": {
            name: version(name) for name in ("numpy", "torch", "pydantic", "sentence-transformers")
        },
    }
