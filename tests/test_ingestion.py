import pytest

from citestack.ingestion import chunk_document, clean_markdown, load_documents
from citestack.schemas import Document


def test_cleaning_removes_template_and_preserves_code():
    title, body = clean_markdown(
        "---\ntitle: Pods\n---\n{{< note >}}\n# Pod\n```sh\nkubectl get pods\n```\n<!-- hidden -->"
    )
    assert title == "Pods"
    assert "{{" not in body and "hidden" not in body
    assert "kubectl get pods" in body


def test_chunk_overlap_boundaries_and_stable_ids(models):
    document = Document(
        id="a",
        title="A",
        url="https://example.org/a",
        text=" ".join(f"word{i}" for i in range(155)),
    )
    chunks = chunk_document(document, models.tokenizer, 64, 10)
    assert all(chunk.token_count <= 64 for chunk in chunks)
    assert chunks[0].text.split()[-10:] == chunks[1].text.split()[:10]
    assert chunks[-1].text.endswith("word154")
    assert chunks == chunk_document(document, models.tokenizer, 64, 10)
    with pytest.raises(ValueError):
        chunk_document(document, models.tokenizer, 64, 64)


def test_duplicate_documents_rejected(corpus):
    corpus.write_text(corpus.read_text() + "\n" + corpus.read_text().splitlines()[0])
    with pytest.raises(ValueError, match="Duplicate"):
        load_documents(corpus)


def test_glossary_labels_preserve_sentence_meaning():
    _, body = clean_markdown(
        'A {{< glossary_tooltip text="Pod" term_id="pod" >}} contains '
        '{{< glossary_tooltip term_id="container" >}} processes.'
    )
    assert body == "A Pod contains container processes."


def test_corpus_read_is_bounded(tmp_path, monkeypatch):
    from citestack import ingestion

    path = tmp_path / "corpus.jsonl"
    path.write_bytes(b"x" * 101)
    monkeypatch.setattr(ingestion, "MAX_CORPUS_LINE_BYTES", 100)
    with pytest.raises(ValueError, match="byte limit"):
        load_documents(path)


def test_empty_document_fails_before_model_work(corpus):
    corpus.write_text(
        Document(id="a", title="A", text=" ", url="https://example.org").model_dump_json()
    )
    with pytest.raises(ValueError, match="must not be empty"):
        load_documents(corpus)
