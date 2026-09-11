import pytest

from citestack.index import Retriever, build_index, reciprocal_rank_fusion


def test_reciprocal_rank_fusion_rewards_agreement():
    scores = reciprocal_rank_fusion([[1, 2, 3], [3, 2, 4]])
    assert scores[3] > scores[1]
    assert reciprocal_rank_fusion([[1, 1]])[1] == 1 / 61


@pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid", "reranked"])
def test_search_returns_expected_source(retriever, mode):
    hits = retriever.search("Secret confidential password token", mode=mode)
    assert hits[0].chunk.document_id == "secrets"


def test_fts_special_characters_are_literal(retriever):
    assert isinstance(retriever.search('" OR * NOT ( ) :'), list)
    assert retriever.lexical("***", 5) == []


def test_rebuild_is_idempotent_and_atomic(corpus, settings, models, retriever):
    first_ids = set(retriever.chunks)
    build_index(corpus, settings, models)
    with pytest.raises(ValueError):
        corpus.write_text("")
        build_index(corpus, settings, models)
    fresh = Retriever(settings, models)
    assert set(fresh.chunks) == first_ids
    fresh.close()
    assert retriever.search("Pods")[0].chunk.document_id == "pods"


def test_old_reader_keeps_consistent_snapshot(corpus, settings, models, retriever):
    corpus.write_text(corpus.read_text().splitlines()[1])
    build_index(corpus, settings, models)
    assert retriever.search("Pods", mode="bm25")[0].chunk.document_id == "pods"
    fresh = Retriever(settings, models)
    assert fresh.manifest["documents"] == 1
    assert fresh.search("Pods", mode="bm25") == []
    fresh.close()


def test_model_mismatch_fails_closed(settings, models, retriever):
    models.fingerprint = "different-model"
    with pytest.raises(ValueError, match="rebuild"):
        Retriever(settings, models)
