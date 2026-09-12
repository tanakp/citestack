"""Validate and atomically publish immutable, content-addressed index snapshots."""

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import numpy as np
from filelock import FileLock

from citestack.schemas import Chunk

SCHEMA_VERSION = 2
# Include FTS shadow data: hashing only visible text misses damaged posting lists.
HASH_TABLES = (
    "chunks",
    "search_config",
    "search_content",
    "search_data",
    "search_docsize",
    "search_idx",
)


def snapshot_id(connection, manifest):
    digest = hashlib.sha256()

    def add(value):
        if isinstance(value, bytes):
            encoded = b"b" + value
        else:
            encoded = (
                b"j"
                + json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            )
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)

    add({k: v for k, v in manifest.items() if k not in {"snapshot_id", "built_at"}})
    for row in connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
    ):
        add(row)
    for table in HASH_TABLES:
        add(table)
        # Each table's leading columns form its primary key (search_idx uses two).
        order = "1, 2" if table == "search_idx" else "1"
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            for value in row:
                add(value)
    return digest.hexdigest()


def validate_snapshot(connection, *, fingerprint=None):
    """Check metadata, FTS bytes/content, vectors and identifiers before serving."""
    try:
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        metadata = connection.execute("SELECT value FROM metadata").fetchall()
        if len(metadata) != 1:
            raise ValueError("Snapshot must have exactly one manifest")
        manifest = json.loads(metadata[0][0])
        if not isinstance(manifest, dict):
            raise ValueError("Invalid snapshot manifest")
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Index schema changed; rebuild the index")
        if fingerprint is not None and manifest.get("embedding_fingerprint") != fingerprint:
            raise ValueError("Embedding model changed; rebuild the index")
        if manifest.get("snapshot_id") != snapshot_id(connection, manifest):
            raise ValueError("Snapshot checksum mismatch")
        dimensions = manifest["dimensions"]
        if type(dimensions) is not int or not 1 <= dimensions <= 16384:
            raise ValueError("Invalid snapshot dimensions")
        rows = connection.execute("SELECT id, payload, vector FROM chunks ORDER BY id").fetchall()
        if not rows or len(rows) != manifest["chunks"]:
            raise ValueError("Snapshot chunk count mismatch")
        chunks = {}
        chunk_ids = set()
        documents = set()
        vectors = []
        for row_id, payload, blob in rows:
            chunk = Chunk.model_validate_json(payload, strict=True, extra="forbid")
            if (
                chunk.id in chunk_ids
                or not chunk.id
                or not chunk.document_id
                or not chunk.text.strip()
            ):
                raise ValueError("Invalid or duplicate chunk identity")
            if not 1 <= chunk.token_count <= manifest["chunk_tokens"]:
                raise ValueError("Invalid chunk token count")
            vector = np.frombuffer(blob, dtype="<f4")
            if vector.shape != (dimensions,) or not np.isfinite(vector).all():
                raise ValueError("Invalid embedding vector")
            if not np.isclose(np.linalg.norm(vector), 1, atol=1e-4):
                raise ValueError("Embedding vector is not normalized")
            chunks[row_id] = chunk
            chunk_ids.add(chunk.id)
            documents.add(chunk.document_id)
            vectors.append(vector)
        if len(documents) != manifest["documents"]:
            raise ValueError("Snapshot document count mismatch")
        count = 0
        for row_id, title, heading, body in connection.execute(
            "SELECT rowid, title, heading, body FROM search"
        ):
            chunk = chunks.get(row_id)
            if chunk is None or (title, heading, body) != (chunk.title, chunk.heading, chunk.text):
                raise ValueError("Search content differs from chunk content")
            count += 1
        if count != len(chunks):
            raise ValueError("Search row count mismatch")
        return manifest, chunks, np.array(list(chunks)), np.vstack(vectors)
    except (sqlite3.Error, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid index snapshot; restore a verified backup or rebuild") from error


def inspect_snapshot(path: Path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return validate_snapshot(connection)[0]


def publish(temporary: Path, destination: Path, *, replace: bool):
    """Durable file + directory sync; no-overwrite publication for named backups."""
    with temporary.open("rb") as ready:
        os.fsync(ready.fileno())
    if replace:
        os.replace(temporary, destination)
    else:
        # An atomic link fails if the destination already exists; no exists()/rename race.
        os.link(temporary, destination)
        temporary.unlink()
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def copy_snapshot(source: Path, destination: Path, *, restore=False):
    """Copy/validate before publication. Restoring keeps active readers on their old inode."""
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must differ")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination) + ".build.lock", timeout=0):
        handle, name = tempfile.mkstemp(
            prefix="snapshot-", suffix=".sqlite", dir=destination.parent
        )
        os.close(handle)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(temporary)) as dst:
                    src.backup(dst)
                    manifest = validate_snapshot(dst)[0]
                    # Unlike serving's read-only connection, the private copy can invoke
                    # FTS5's full internal posting-list check before it is published.
                    dst.execute("INSERT INTO search(search, rank) VALUES('integrity-check', 1)")
                    dst.rollback()
            publish(temporary, destination, replace=restore)
            return manifest
        finally:
            temporary.unlink(missing_ok=True)
