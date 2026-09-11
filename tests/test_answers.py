import json

import httpx
import pytest

from citestack.answers import AnswerService, generate, select_context, validate_grounding
from citestack.providers import OllamaProvider
from citestack.schemas import Claim, Evidence, GeneratedAnswer


def valid_generated(hit):
    return GeneratedAnswer(
        abstained=False,
        claims=[
            Claim(
                text="A Pod groups containers.",
                evidence=[Evidence(source_id=hit.chunk.id, quote=hit.chunk.text[:60])],
            )
        ],
    )


def test_fabricated_citations_rejected(retriever):
    hits = retriever.search("Pod containers")
    generated = valid_generated(hits[0])
    validate_grounding(generated, hits)
    generated.claims[0].evidence[0].source_id = "invented"
    with pytest.raises(ValueError, match="Citation"):
        validate_grounding(generated, hits)
    generated = valid_generated(hits[0])
    generated.claims[0].evidence[0].quote = "This is a fabricated source quote."
    with pytest.raises(ValueError, match="Citation"):
        validate_grounding(generated, hits)


def test_empty_claims_fail_closed(retriever):
    with pytest.raises(ValueError):
        validate_grounding(GeneratedAnswer(abstained=False, claims=[]), [])


def test_quote_reflow_restores_exact_source_span(retriever):
    hits = retriever.search("Pod containers")
    hits[0].chunk.text = "A Pod contains one or more\n  containers with shared networking."
    generated = valid_generated(hits[0])
    evidence = generated.claims[0].evidence[0]
    evidence.quote = "A Pod contains one or more containers with shared networking."
    validate_grounding(generated, hits)
    assert evidence.quote == hits[0].chunk.text
    evidence.quote = "A Pod contains zero containers with shared networking."
    with pytest.raises(ValueError):
        validate_grounding(generated, hits)


def test_context_respects_budget(retriever, settings):
    settings.context_tokens = 256
    selected = select_context(retriever.search("Pod containers"), settings)
    assert sum(hit.chunk.token_count + 80 for hit in selected) <= 256


def test_extractive_quotes_match_sources(retriever, settings):
    answer = AnswerService(retriever, settings).answer("Pod containers")
    assert answer.mode == "extractive" and not answer.abstained
    sources = {hit.chunk.id: hit.chunk.text for hit in answer.hits}
    assert all(citation.quote in sources[citation.source_id] for citation in answer.citations)


def test_low_relevance_abstains(retriever, settings):
    settings.min_rerank_score = 100
    answer = AnswerService(retriever, settings).answer("What is tomorrow's weather?")
    assert answer.abstained and not answer.citations


def test_provider_failure_uses_labeled_fallback(retriever, settings, monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("citestack.answers.generate", fail)
    settings.answer_mode = "ollama"
    answer = AnswerService(retriever, settings).answer("Pod containers")
    assert answer.mode == "extractive" and answer.fallback_reason


def test_generation_retries_malformed_json_then_accepts(retriever, settings, monkeypatch):
    hits = retriever.search("Pod containers")
    responses = ["not JSON", valid_generated(hits[0]).model_dump_json()]
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"response": responses.pop(0), "done": True})

    monkeypatch.setattr(
        "citestack.answers.OllamaProvider",
        lambda settings: OllamaProvider(settings, transport=httpx.MockTransport(respond)),
    )
    result = generate("Pod containers", hits, settings)
    assert len(calls) == 2 and not result.abstained
    assert calls[0]["stream"] is False


def test_model_abstention_is_preserved(retriever, settings, monkeypatch):
    monkeypatch.setattr(
        "citestack.answers.generate", lambda *args: GeneratedAnswer(claims=[], abstained=True)
    )
    settings.answer_mode = "ollama"
    answer = AnswerService(retriever, settings).answer("Pod containers")
    assert answer.abstained and not answer.citations and not answer.fallback_reason


def test_invalid_provider_envelope_is_bounded(retriever, settings, monkeypatch):
    calls = []

    def respond(request):
        calls.append(request.url)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(
        "citestack.answers.OllamaProvider",
        lambda settings: OllamaProvider(settings, transport=httpx.MockTransport(respond)),
    )
    settings.answer_mode = "ollama"
    answer = AnswerService(retriever, settings).answer("Pod containers")
    assert len(calls) == 2
    assert answer.mode == "extractive" and answer.fallback_reason
