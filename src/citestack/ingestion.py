import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import yaml
from filelock import FileLock

from citestack.schemas import Chunk, Document

KUBERNETES_REVISION = "17133089068629ec12ca15c1bdf36a60d2671a74"
MAX_DOCUMENT_BYTES = 2_000_000
MAX_CORPUS_BYTES = 100_000_000
MAX_DOCUMENTS = 10_000
MAX_CORPUS_LINE_BYTES = 8_000_000
MAX_TREE_BYTES = 20_000_000
CLEANER_VERSION = "markdown-v2-glossary"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def clean_markdown(raw: str) -> tuple[str, str]:
    title = ""
    if raw.startswith("---\n"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            metadata = yaml.safe_load(parts[1]) or {}
            if isinstance(metadata, dict):
                title = str(metadata.get("title", ""))
            raw = parts[2]
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)

    def shortcode(match):
        try:
            parts = shlex.split(match[1])
        except ValueError:
            raise ValueError("Malformed documentation shortcode") from None
        if parts and parts[0] == "glossary_tooltip":
            attrs = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
            return attrs.get("text") or attrs.get("term_id", "").replace("-", " ")
        return ""

    raw = re.sub(r"{{[<%](.*?)[>%]}}", shortcode, raw, flags=re.S)
    raw = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    if not title:
        match = re.search(r"^#+\s+(.+)", raw, flags=re.M)
        title = match[1] if match else "Untitled"
    return title, raw


def fetch_kubernetes(destination: Path, *, limit: int | None = None) -> dict:
    """Fetch only pinned English Markdown blobs, checking each Git object hash."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination) + ".fetch.lock", timeout=0):
        return _fetch_kubernetes(destination, limit=limit)


def _fetch_kubernetes(destination: Path, *, limit: int | None = None) -> dict:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.github.com/repos/kubernetes/website/git/trees/{KUBERNETES_REVISION}"

    def bounded_body(response, maximum):
        response.raise_for_status()
        content = bytearray()
        started = time.monotonic()
        for block in response.iter_bytes():
            if len(content) + len(block) > maximum:
                raise ValueError("Download exceeds size limit")
            if time.monotonic() - started > 90:
                raise ValueError("Download exceeds elapsed-time limit")
            content.extend(block)
        return content

    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url, params={"recursive": "1"}) as response:
            tree = json.loads(bounded_body(response, MAX_TREE_BYTES))
        if tree.get("truncated"):
            raise ValueError("Upstream tree is truncated; refusing incomplete ingestion")
        entries = sorted(
            [
                item
                for item in tree["tree"]
                if item["type"] == "blob"
                and item["path"].startswith("content/en/docs/")
                and item["path"].endswith(".md")
                and item.get("size", 0) <= MAX_DOCUMENT_BYTES
            ],
            key=lambda item: item["path"],
        )
        if limit:
            entries = entries[:limit]
        if len(entries) > MAX_DOCUMENTS:
            raise ValueError("Source exceeds document limit")

        def download(item):
            relative = item["path"]
            raw_url = (
                f"https://raw.githubusercontent.com/kubernetes/website/{KUBERNETES_REVISION}/"
                + relative
            )
            for attempt in range(3):
                try:
                    with client.stream("GET", raw_url) as blob:
                        content = bounded_body(blob, MAX_DOCUMENT_BYTES)
                    break
                except httpx.HTTPError:
                    if attempt == 2:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            header = f"blob {len(content)}\0".encode()
            if hashlib.sha1(header + content).hexdigest() != item["sha"]:
                raise ValueError(f"Git blob checksum mismatch: {relative}")
            title, body = clean_markdown(content.decode("utf-8"))
            if len(body.split()) < 40:
                return None
            source = f"https://github.com/kubernetes/website/blob/{KUBERNETES_REVISION}/{relative}"
            return Document(id=digest(source), title=title, url=source, text=body)

        documents = []
        total = 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            # Bound completed results waiting behind a slow file (map is eager on 3.11/3.12).
            for offset in range(0, len(entries), 32):
                for i, document in enumerate(
                    pool.map(download, entries[offset : offset + 32]), offset + 1
                ):
                    if document:
                        total += len(document.model_dump_json().encode()) + 1
                        if total > MAX_CORPUS_BYTES:
                            raise ValueError("Corpus exceeds byte limit")
                        documents.append(document)
                    if i % 100 == 0:
                        print(f"Fetched {i}/{len(entries)} source files", file=sys.stderr)
    if not documents:
        raise ValueError("No documents found in upstream tree")
    manifest = {
        "source": "Kubernetes documentation contributors",
        "revision": KUBERNETES_REVISION,
        "tree_url": url,
        "blob_checksums_verified": True,
        "license": "CC-BY-4.0",
        "license_url": f"https://github.com/kubernetes/website/blob/{KUBERNETES_REVISION}/LICENSE",
        "cleaner_version": CLEANER_VERSION,
        "transforms": (
            "English docs; glossary labels retained; other shortcodes and link targets removed"
        ),
        "documents": len(documents),
        "corpus_sha256": corpus_digest(documents),
    }
    # The corpus is a local build input, not part of the source repository.
    handle, name = tempfile.mkstemp(prefix="corpus-", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w") as output:
            for document in documents:
                output.write(document.model_dump_json() + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
        destination.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def corpus_digest(documents):
    result = hashlib.sha256()
    for document in documents:
        result.update(document.model_dump_json().encode() + b"\n")
    return result.hexdigest()


def load_documents(path: Path) -> list[Document]:
    documents = []
    total = 0
    with path.open("rb") as source:
        while line := source.readline(MAX_CORPUS_LINE_BYTES + 1):
            total += len(line)
            if len(line) > MAX_CORPUS_LINE_BYTES or total > MAX_CORPUS_BYTES:
                raise ValueError("Corpus exceeds byte limit")
            if not line.strip():
                continue
            document = Document.model_validate_json(line, strict=True, extra="forbid")
            if len(document.text.encode()) > MAX_DOCUMENT_BYTES:
                raise ValueError("Document exceeds byte limit")
            if not document.id.strip() or not document.text.strip():
                raise ValueError("Document identity and text must not be empty")
            documents.append(document)
            if len(documents) > MAX_DOCUMENTS:
                raise ValueError("Corpus exceeds document limit")
    ids = [document.id for document in documents]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate document IDs in corpus")
    if not documents:
        raise ValueError("Corpus is empty")
    return documents


def chunk_document(document: Document, tokenizer, size: int, overlap: int) -> list[Chunk]:
    if not 0 <= overlap < size:
        raise ValueError("Overlap must be nonnegative and smaller than chunk size")
    # Token offsets preserve the actual source text for verifiable quotation.
    offsets = tokenizer(
        document.text, add_special_tokens=False, return_offsets_mapping=True, verbose=False
    )["offset_mapping"]
    headings = list(re.finditer(r"^#{1,6}\s+(.+)$", document.text, re.M))
    chunks = []
    for start in range(0, len(offsets), size - overlap):
        end = min(start + size, len(offsets))
        left, right = offsets[start][0], offsets[end - 1][1]
        text = document.text[left:right].strip()
        heading = document.title
        for match in headings:
            if match.start() > left:
                break
            heading = match[1]
        if text:
            chunks.append(
                Chunk(
                    id=digest(f"{document.id}:{start}:{text}"),
                    document_id=document.id,
                    title=document.title,
                    url=document.url,
                    heading=heading,
                    text=text,
                    token_count=end - start,
                )
            )
        if end == len(offsets):
            break
    return chunks
