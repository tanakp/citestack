import hashlib
import json

import httpx
import pytest

from citestack.ingestion import fetch_kubernetes


def fake_upstream(monkeypatch, *, corrupt=False, truncated=False):
    raw = ("---\ntitle: Pods\n---\n" + "Containers share networking and storage. " * 12).encode()
    sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    calls = []

    def handle(request):
        calls.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "truncated": truncated,
                    "tree": [
                        {
                            "path": "content/en/docs/concepts/pods.md",
                            "type": "blob",
                            "size": len(raw),
                            "sha": "bad" if corrupt else sha,
                        },
                        {"path": "content/fr/docs/concepts/pods.md", "type": "blob", "size": 50},
                        {"path": "content/en/docs/image.png", "type": "blob", "size": 50},
                    ],
                },
            )
        return httpx.Response(200, content=raw)

    original = httpx.Client
    monkeypatch.setattr(
        "citestack.ingestion.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    return calls


def test_fetch_filters_sources_and_checks_content(tmp_path, monkeypatch):
    calls = fake_upstream(monkeypatch)
    destination = tmp_path / "corpus.jsonl"
    manifest = fetch_kubernetes(destination)
    assert manifest["documents"] == 1 and manifest["blob_checksums_verified"]
    assert len(calls) == 2
    document = json.loads(destination.read_text())
    assert document["title"] == "Pods" and "---" not in document["text"]


@pytest.mark.parametrize("option", ["corrupt", "truncated"])
def test_bad_download_keeps_old_corpus(tmp_path, monkeypatch, option):
    fake_upstream(monkeypatch, **{option: True})
    destination = tmp_path / "corpus.jsonl"
    destination.write_text("old corpus")
    with pytest.raises(ValueError):
        fetch_kubernetes(destination)
    assert destination.read_text() == "old corpus"


def test_fetched_corpus_is_bound_to_manifest_and_buildable(tmp_path, monkeypatch, settings, models):
    from citestack.index import build_index
    from citestack.ingestion import corpus_digest, load_documents

    fake_upstream(monkeypatch)
    destination = tmp_path / "corpus.jsonl"
    manifest = fetch_kubernetes(destination)
    assert manifest["corpus_sha256"] == corpus_digest(load_documents(destination))
    built = build_index(destination, settings, models)
    assert built["source"]["corpus_sha256"] == built["corpus_sha256"]
    destination.write_text(destination.read_text().replace("Containers", "Altered"))
    with pytest.raises(ValueError, match="provenance"):
        build_index(destination, settings, models)


def test_tree_download_cap_preserves_previous_corpus(tmp_path, monkeypatch):
    fake_upstream(monkeypatch)
    monkeypatch.setattr("citestack.ingestion.MAX_TREE_BYTES", 10)
    destination = tmp_path / "corpus.jsonl"
    destination.write_text("previous")
    with pytest.raises(ValueError, match="size limit"):
        fetch_kubernetes(destination)
    assert destination.read_text() == "previous"


def test_download_queue_stays_bounded_behind_slow_first_file(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    raw = ("A documentation paragraph with useful technical information. " * 10).encode()
    digest = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    entered, release, window_filled = threading.Event(), threading.Event(), threading.Event()
    started = []
    lock = threading.Lock()

    def handle(request):
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {
                            "type": "blob",
                            "path": f"content/en/docs/{i:04}.md",
                            "size": len(raw),
                            "sha": digest,
                        }
                        for i in range(100)
                    ]
                },
            )
        with lock:
            started.append(request.url.path)
            if len(started) == 32:
                window_filled.set()
        if request.url.path.endswith("/0000.md"):
            entered.set()
            assert release.wait(3)
        return httpx.Response(200, content=raw)

    original = httpx.Client
    monkeypatch.setattr(
        "citestack.ingestion.httpx.Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fetch_kubernetes, tmp_path / "corpus.jsonl")
        try:
            assert entered.wait(2) and window_filled.wait(2)
            assert len(started) == 32
        finally:
            release.set()
        assert future.result(5)["documents"] == 100
