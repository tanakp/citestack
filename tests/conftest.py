import hashlib
import re

import numpy as np
import pytest

from citestack.config import Settings
from citestack.index import Retriever, build_index
from citestack.schemas import Document


class TestTokenizer:
    def __call__(self, text, **kwargs):
        return {"offset_mapping": [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]}


class FakeModels:
    """Deterministic test double, never used as a semantic-search implementation."""

    fingerprint = "test-only-v1"
    tokenizer = TestTokenizer()

    def embed(self, texts, *, query=False):
        result = np.zeros((len(texts), 128), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"\w+", text.lower()):
                bucket = int(hashlib.sha256(word.encode()).hexdigest()[:8], 16) % 128
                result[i, bucket] += 1
        return result / np.maximum(np.linalg.norm(result, axis=1, keepdims=True), 1e-12)

    def rerank(self, question, texts):
        terms = set(question.lower().split())
        return [float(len(terms & set(text.lower().split()))) for text in texts]


@pytest.fixture
def models():
    return FakeModels()


@pytest.fixture
def corpus(tmp_path):
    docs = [
        Document(
            id="pods",
            title="Pods",
            url="https://example.org/pods",
            text="A Pod is the smallest deployable unit in Kubernetes. "
            "A Pod contains one or more containers that share storage and networking.",
        ),
        Document(
            id="secrets",
            title="Secrets",
            url="https://example.org/secrets",
            text="A Secret stores confidential data such as a password or a token. "
            "Secrets are stored unencrypted in etcd by default. Enable encryption at rest.",
        ),
        Document(
            id="deployments",
            title="Deployments",
            url="https://example.org/deployments",
            text="A Deployment provides declarative updates for Pods and ReplicaSets. "
            "You can roll back a Deployment to an earlier revision after an update.",
        ),
    ]
    path = tmp_path / "corpus.jsonl"
    path.write_text("\n".join(doc.model_dump_json() for doc in docs))
    return path


@pytest.fixture
def settings(tmp_path):
    return Settings(
        index_path=tmp_path / "index.sqlite",
        quota_path=tmp_path / "quotas.sqlite",
        rate_limit_burst=100,
        _env_file=None,
    )


@pytest.fixture
def retriever(corpus, settings, models):
    build_index(corpus, settings, models)
    result = Retriever(settings, models)
    yield result
    result.close()
