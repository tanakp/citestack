import json
import sqlite3
from contextlib import closing

import numpy as np
import pytest
from filelock import FileLock, Timeout

from citestack.index import Retriever, build_index
from citestack.snapshots import copy_snapshot, inspect_snapshot, snapshot_id


def mutate(path, operation, *, reseal=False):
    with closing(sqlite3.connect(path)) as connection, connection:
        operation(connection)
        connection.commit()
        if reseal:
            manifest = json.loads(connection.execute("SELECT value FROM metadata").fetchone()[0])
            manifest["snapshot_id"] = snapshot_id(connection, manifest)
            connection.execute("UPDATE metadata SET value=?", (json.dumps(manifest),))


def test_snapshot_identity_reproducible_and_backup_immutable(corpus, settings, models, retriever):
    original = retriever.manifest["snapshot_id"]
    assert build_index(corpus, settings, models)["snapshot_id"] == original
    destination = settings.index_path.parent / "backup.sqlite"
    assert copy_snapshot(settings.index_path, destination)["snapshot_id"] == original
    assert inspect_snapshot(destination)["snapshot_id"] == original
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        copy_snapshot(settings.index_path, destination)
    assert destination.read_bytes() == before


def test_backup_restore_preserves_reader_snapshots(corpus, settings, models, retriever):
    backup = settings.index_path.parent / "backup.sqlite"
    copy_snapshot(settings.index_path, backup)
    corpus.write_text(corpus.read_text().splitlines()[1])
    build_index(corpus, settings, models)
    updated = Retriever(settings, models)
    try:
        assert updated.manifest["documents"] == 1
        copy_snapshot(backup, settings.index_path, restore=True)
        restored = Retriever(settings, models)
        try:
            assert restored.manifest["snapshot_id"] == retriever.manifest["snapshot_id"]
            assert restored.lexical("Pods", 5)
            assert not updated.lexical("Pods", 5)
        finally:
            restored.close()
    finally:
        updated.close()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM chunks WHERE id=0",
        "UPDATE chunks SET payload='{}' WHERE id=0",
        "UPDATE chunks SET vector=zeroblob(length(vector)) WHERE id=0",
        "DELETE FROM search WHERE rowid=0",
        "UPDATE search_data SET block=zeroblob(length(block)) WHERE id>1",
        "UPDATE metadata SET value='[]'",
        "INSERT INTO metadata SELECT value FROM metadata",
    ],
)
def test_corruption_rejected_without_replacing_live_index(settings, retriever, sql):
    backup = settings.index_path.parent / "bad.sqlite"
    copy_snapshot(settings.index_path, backup)
    mutate(backup, lambda db: db.execute(sql))
    before = settings.index_path.read_bytes()
    with pytest.raises(ValueError):
        copy_snapshot(backup, settings.index_path, restore=True)
    assert settings.index_path.read_bytes() == before
    assert retriever.lexical("Pods", 5)
    assert not list(settings.index_path.parent.glob("snapshot-*.sqlite"))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, 2.0])
def test_invalid_vectors_rejected_even_with_matching_checksum(settings, retriever, bad):
    dimensions = retriever.manifest["dimensions"]
    mutate(
        settings.index_path,
        lambda db: db.execute(
            "UPDATE chunks SET vector=? WHERE id=0",
            (np.full(dimensions, bad, dtype="<f4").tobytes(),),
        ),
        reseal=True,
    )
    with pytest.raises(ValueError, match="vector"):
        inspect_snapshot(settings.index_path)


def test_consistent_content_required_even_with_matching_checksum(settings, retriever):
    mutate(
        settings.index_path,
        lambda db: db.execute("UPDATE search SET body='changed' WHERE rowid=0"),
        reseal=True,
    )
    with pytest.raises(ValueError, match="differs"):
        inspect_snapshot(settings.index_path)


def test_restore_and_build_share_lock(settings, retriever):
    backup = settings.index_path.parent / "backup.sqlite"
    copy_snapshot(settings.index_path, backup)
    with FileLock(str(settings.index_path) + ".build.lock"):
        with pytest.raises(Timeout):
            copy_snapshot(backup, settings.index_path, restore=True)


def test_cli_backup_inspect_restore_need_no_models(settings, retriever, monkeypatch, capsys):
    from citestack.cli import main

    monkeypatch.setenv("CITESTACK_INDEX_PATH", str(settings.index_path))
    backup = settings.index_path.parent / "cli.sqlite"
    for arguments in (["backup", str(backup)], ["inspect"], ["restore", str(backup)]):
        monkeypatch.setattr("sys.argv", ["citestack", *arguments])
        main()
        assert (
            json.loads(capsys.readouterr().out)["snapshot_id"] == retriever.manifest["snapshot_id"]
        )


def test_stale_corpus_provenance_cannot_claim_pinned_source(corpus, settings, models, retriever):
    corpus.with_suffix(".manifest.json").write_text(json.dumps({"corpus_sha256": "stale"}))
    before = settings.index_path.read_bytes()
    with pytest.raises(ValueError, match="provenance"):
        build_index(corpus, settings, models)
    assert settings.index_path.read_bytes() == before
