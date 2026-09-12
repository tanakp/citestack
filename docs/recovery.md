# Index integrity, backup, and recovery

Index format **2** verifies an immutable snapshot before opening it for requests:
SQLite structural checks; exactly one manifest; expected model revision and dimension;
finite, normalized vectors; unique chunk IDs; document/chunk counts; matching FTS
content; and a SHA-256 identifier covering metadata, chunk payloads, vector bytes, SQL
schema, and the FTS shadow tables that hold search posting lists. Timestamps are excluded
from the identifier. Builds and backup/restore also run SQLite's FTS5 integrity command.
Serving uses a read-only connection, so it verifies the stored FTS bytes by checksum.

The checksum detects accidental corruption. It is not a signature: an attacker with
write access to both data and metadata could replace the checksum. Store deployment
snapshots on a read-only mount and restrict build/backup access to trusted operators.
Changing SQLite versions can change internal FTS representation and hence snapshot
IDs; record the ID of the artifact you actually deploy.

## Upgrade from format 1

Keep a copy of the old index and its matching application release before upgrading.
Format 1 has no integrity identifier and is deliberately rejected by the new reader.
Re-fetch and rebuild instead of relabeling old data as verified:

```bash
uv run citestack fetch --output data/corpus-v2.jsonl
CITESTACK_INDEX_PATH=data/index-v2.sqlite uv run citestack ingest --corpus data/corpus-v2.jsonl
CITESTACK_INDEX_PATH=data/index-v2.sqlite uv run citestack inspect
```

This also applies the glossary-cleaning fix. The fresh corpus contains **1,180** usable
pages at the pinned revision; the previous cleaner produced 1,175 because it removed
glossary words and five small pages fell below the minimum-text threshold.
Do not overwrite an existing corpus manifest after editing its data: ingestion checks
that the source manifest's canonical corpus digest matches the documents being built.
For your own data, supply a JSONL file without the Kubernetes provenance sidecar.

## Verified backups

```bash
CITESTACK_INDEX_PATH=data/index-v2.sqlite uv run citestack backup data/backups/release-v2.sqlite
CITESTACK_INDEX_PATH=data/backups/release-v2.sqlite uv run citestack inspect
```

`backup` uses SQLite's backup API, validates the private copy, runs the full FTS check,
and publishes it without overwriting an existing destination. It requires no model
loading or model download. Use unique names and retain copies on independent storage;
a second file on the same disk is only a local rollback point. The returned
`snapshot_id` must match the source's ID. Copying a running snapshot remains consistent
because live readers and the backup connection retain their original file handles.

## Restore or roll back

```bash
CITESTACK_INDEX_PATH=data/index.sqlite uv run citestack restore data/backups/release-v2.sqlite
CITESTACK_INDEX_PATH=data/index.sqlite uv run citestack inspect
```

Restore validates a complete private copy before replacing the destination atomically.
A corrupt input cannot replace the active index. Restore and build take the same
per-destination lock, so concurrent publications fail rather than race. Publication
syncs the file before replacement and its parent directory afterwards. The commands
assume a local filesystem with SQLite, atomic rename, hard links, and directory sync
support; network filesystems are outside the deployment target.

**Restart the API after restoring.** Already-open readers intentionally retain their
previous consistent snapshot. Verify readiness, authenticate as each client, and check
`GET /v1/index` against the expected `snapshot_id`. The application release and embedding
model fingerprint must match the restored format and metadata. In a tenant registry,
restore the affected client's exact index path. Never point two clients at a shared
writable file to save disk space.

If a failed build leaves an `index-*.sqlite` or `snapshot-*.sqlite` temporary file after
an abrupt process kill, do not serve it. Normal exceptions remove temporary files.
Confirm no build/restore is running, inspect the live index, then remove abandoned
files during maintenance. Do not delete lock files while another process holds a lock.
Do not modify a served database in place; publish a replacement and restart.

## Boundaries and remaining work

Downloaded files are pinned and checked against their Git blob hashes. Tree responses,
individual documents, corpus bytes, document count, concurrent downloads, and queued
results are bounded. Each download has a 30-second network-operation timeout and an
elapsed-time check between received blocks. The latter can overshoot by one read
operation; it is not an async hard deadline. Failed fetches before publication retain
the old corpus. Fetch/build share a corpus lock, and corpus digest verification rejects
a mismatched sidecar after an interrupted two-file publication.

The cleaner preserves glossary labels and code blocks but does not render the entire
Hugo website, execute templates, or expand externally included examples. This is a
Markdown corpus with documented transformations, not a byte-for-byte rendered site.

These commands back up index snapshots only. Client registry secrets, quotas, model
artifacts, application images, and deployment configuration need their own retention
and recovery policies. See [quota incident response](operations.md#incident-response) and
[deployment rollback](deployment.md#upgrade-and-rollback). An index restore must never
silently reset client quotas.

## Measured recovery exercise

The [recorded full-dataset exercise](recovery-verification.json) validated a
132,685,824-byte index built from 1,180 public pages, copied and restored it with the same
snapshot ID, rejected a deliberately corrupted backup without altering the destination,
and retrieved expected evidence with the pinned BGE and cross-encoder models after
restore. Full snapshot inspection took about 1.28 seconds on the recorded local host.
These are single-run timings, not a recovery-time SLA. The original 20 development
queries still score 19/20 for reranked hit@5; [all results](evaluation-v2.json) retain
the miss and retrieved paths. Expanded [quality](quality.md) and [load experiments](capacity.md) are reported separately.
