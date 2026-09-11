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
