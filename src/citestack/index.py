import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from filelock import FileLock

from citestack.config import Settings
from citestack.ingestion import chunk_document, corpus_digest, load_documents
from citestack.schemas import Chunk, Hit
from citestack.snapshots import SCHEMA_VERSION, publish, snapshot_id, validate_snapshot


def build_index(corpus: Path, settings: Settings, models) -> dict:
    """Build a complete snapshot and atomically replace it only after success."""
    settings.index_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.index_path) + ".build.lock", timeout=0):
        with FileLock(str(corpus) + ".fetch.lock", timeout=0):
            documents = load_documents(corpus)
            corpus_sha256 = corpus_digest(documents)
            source_path = corpus.with_suffix(".manifest.json")
            source_manifest = None
            if source_path.exists():
                source_manifest = json.loads(source_path.read_text())
                if source_manifest.get("corpus_sha256") != corpus_sha256:
                    raise ValueError("Corpus provenance mismatch; refetch the corpus")
        chunks = [
            chunk
            for document in documents
            for chunk in chunk_document(
                document, models.tokenizer, settings.chunk_tokens, settings.overlap_tokens
            )
        ]
        if not chunks:
            raise ValueError("Corpus has no indexable chunks")
        # Heading prefixes are bounded so model truncation does not eat chunk content.
        texts = [embedding_text(chunk) for chunk in chunks]
        vectors = models.embed(texts)
        if vectors.ndim != 2 or vectors.shape[0] != len(chunks):
            raise ValueError("Embedding shape mismatch")
        if not np.isfinite(vectors).all():
            raise ValueError("Embeddings contain non-finite values")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError("Zero embedding vector")
        vectors = (vectors / norms).astype("<f4")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "corpus_sha256": corpus_sha256,
            "embedding_fingerprint": models.fingerprint,
            "dimensions": int(vectors.shape[1]),
            "documents": len(documents),
            "chunks": len(chunks),
            "chunk_tokens": settings.chunk_tokens,
            "overlap_tokens": settings.overlap_tokens,
            "built_at": datetime.now(UTC).isoformat(),
        }
        if source_manifest is not None:
            manifest["source"] = source_manifest
        handle, name = tempfile.mkstemp(
            prefix="index-", suffix=".sqlite", dir=settings.index_path.parent
        )
        os.close(handle)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(temporary)) as connection, connection:
                connection.executescript(
                    "CREATE TABLE metadata (value TEXT NOT NULL);"
                    "CREATE TABLE chunks (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, "
                    "vector BLOB NOT NULL);"
                    "CREATE VIRTUAL TABLE search USING fts5(title, heading, body);"
                )
                connection.execute("INSERT INTO metadata VALUES (?)", (json.dumps(manifest),))
                connection.executemany(
                    "INSERT INTO chunks VALUES (?, ?, ?)",
                    [
                        (i, chunk.model_dump_json(), vectors[i].tobytes())
                        for i, chunk in enumerate(chunks)
                    ],
                )
                connection.executemany(
                    "INSERT INTO search(rowid, title, heading, body) VALUES (?, ?, ?, ?)",
                    [(i, c.title, c.heading, c.text) for i, c in enumerate(chunks)],
                )
                connection.execute("INSERT INTO search(search, rank) VALUES('integrity-check', 1)")
                connection.commit()  # Flush FTS pending posting lists before hashing.
                manifest["snapshot_id"] = snapshot_id(connection, manifest)
                connection.execute("UPDATE metadata SET value=?", (json.dumps(manifest),))
                connection.commit()
                validate_snapshot(connection)
            publish(temporary, settings.index_path, replace=True)
        finally:
            temporary.unlink(missing_ok=True)
        return manifest


def embedding_text(chunk: Chunk) -> str:
    return f"{chunk.title[:100]} — {chunk.heading[:100]}\n{chunk.text}"


def reciprocal_rank_fusion(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(dict.fromkeys(ranking), 1):
            scores[item] = scores.get(item, 0) + 1 / (k + rank)
    return scores


class Retriever:
    def __init__(self, settings: Settings, models):
        self.settings, self.models = settings, models
        # This connection and vector matrix refer to the same immutable snapshot.
        # A running service keeps its snapshot until restart, even during rebuilds.
        uri = settings.index_path.resolve().as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.lock = threading.Lock()
        try:
            self.manifest, self.chunks, self.ids, self.vectors = validate_snapshot(
                self.connection,
                fingerprint=models.fingerprint,
            )
        except Exception:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def lexical(self, question: str, limit: int) -> list[int]:
        words = list(dict.fromkeys(re.findall(r"\w+", question.lower())))[:64]
        if not words:
            return []
        expression = " OR ".join('"' + word + '"' for word in words)
        with self.lock:
            rows = self.connection.execute(
                "SELECT rowid FROM search WHERE search MATCH ? "
                "ORDER BY bm25(search, 2.0, 1.5, 1.0), rowid LIMIT ?",
                (expression, limit),
            ).fetchall()
        return [row[0] for row in rows]

    def dense(self, question: str, limit: int) -> list[int]:
        query = self.models.embed([question], query=True)[0]
        query = query / max(float(np.linalg.norm(query)), 1e-12)
        scores = self.vectors @ query
        order = np.argsort(-scores, kind="stable")[:limit]
        return [int(self.ids[i]) for i in order]

    def search(self, question: str, top_k: int = 5, mode: str = "reranked") -> list[Hit]:
        if mode not in {"bm25", "dense", "hybrid", "reranked"}:
            raise ValueError("Unknown retrieval mode")
        if top_k < 1:
            raise ValueError("top_k must be positive")
        candidates = max(self.settings.candidate_k, top_k)
        rankings = []
        if mode != "dense":
            rankings.append(self.lexical(question, candidates))
        if mode != "bm25":
            rankings.append(self.dense(question, candidates))
        fused = reciprocal_rank_fusion(rankings)
        ordered = sorted(fused, key=lambda item: (-fused[item], item))[:candidates]
        hits = [Hit(chunk=self.chunks[item], score=fused[item]) for item in ordered]
        if mode == "reranked" and hits:
            scores = self.models.rerank(question, [embedding_text(hit.chunk) for hit in hits])
            for hit, score in zip(hits, scores, strict=True):
                hit.rerank_score = float(score)
            hits.sort(key=lambda hit: -hit.rerank_score)
        # Keep at most two chunks from one document to improve evidence diversity.
        counts: dict[str, int] = {}
        result = []
        for hit in hits:
            doc = hit.chunk.document_id
            if counts.get(doc, 0) >= 2:
                continue
            counts[doc] = counts.get(doc, 0) + 1
            result.append(hit)
            if len(result) == top_k:
                break
        return result
