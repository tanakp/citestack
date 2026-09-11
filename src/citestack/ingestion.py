import hashlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import yaml

from citestack.schemas import Chunk, Document

KUBERNETES_REVISION = "17133089068629ec12ca15c1bdf36a60d2671a74"
MAX_DOCUMENT_BYTES = 2_000_000


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
    raw = re.sub(r"{{[<%].*?[>%]}}", "", raw, flags=re.S)
    raw = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()
    if not title:
        match = re.search(r"^#+\s+(.+)", raw, flags=re.M)
        title = match[1] if match else "Untitled"
    return title, raw


def fetch_kubernetes(destination: Path, *, limit: int | None = None) -> dict:
    """Fetch only pinned English Markdown blobs, checking each Git object hash."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://api.github.com/repos/kubernetes/website/git/trees/{KUBERNETES_REVISION}"
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(url, params={"recursive": "1"})
        response.raise_for_status()
        tree = response.json()
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

        def download(item):
            relative = item["path"]
            raw_url = (
                f"https://raw.githubusercontent.com/kubernetes/website/{KUBERNETES_REVISION}/"
                + relative
            )
            for attempt in range(3):
                try:
                    with client.stream("GET", raw_url) as blob:
                        blob.raise_for_status()
                        content = bytearray()
                        for block in blob.iter_bytes():
                            content.extend(block)
                            if len(content) > MAX_DOCUMENT_BYTES:
                                raise ValueError("Document exceeds size limit")
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
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, document in enumerate(pool.map(download, entries), 1):
                if document:
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
        "transforms": "English docs only; frontmatter, Hugo shortcodes and link targets removed",
        "documents": len(documents),
    }
    # The corpus is a local build input, not part of the source repository.
    temporary = destination.with_suffix(".jsonl.tmp")
    with temporary.open("w") as output:
        for document in documents:
            output.write(document.model_dump_json() + "\n")
    temporary.replace(destination)
    destination.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def load_documents(path: Path) -> list[Document]:
    documents = [
        Document.model_validate_json(line) for line in path.read_text().splitlines() if line
    ]
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
