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
