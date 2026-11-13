import json
import multiprocessing
from pathlib import Path

import pytest

from app import repository
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo.json"


def load_demo() -> dict:
    return json.loads(DEMO.read_text(encoding="utf-8"))


def write_sample(path: Path, sample: dict) -> Path:
    path.write_text(json.dumps(sample), encoding="utf-8")
    return path


def _seed_worker(db_path, sample_path, barrier, queue):
    """Run one startup's initialize + seed against a shared empty database."""
    try:
        repository.initialize_database(db_path)
        barrier.wait(timeout=10)
        loaded = repository.seed_demo_if_empty(db_path, sample_path)
        queue.put(("ok", loaded))
    except BaseException as exc:  # surface worker failures in the test
        queue.put(("error", repr(exc)))


def counts(db_path) -> dict:
    return repository.health_counts(db_path)


def test_seed_rolls_back_whole_batch_when_a_late_article_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.sqlite3"
    sample_path = DEMO
    repository.initialize_database(db_path)

    original = repository._apply_catalog_article
    calls: list[str] = []

    def fail_on_last_article(conn, article_id, payload, updated_at):
        calls.append(article_id)
        if article_id == "A-300":
            raise RuntimeError("simulated corrupt catalog entry")
        return original(conn, article_id, payload, updated_at)

    monkeypatch.setattr(repository, "_apply_catalog_article", fail_on_last_article)

    with pytest.raises(RuntimeError, match="simulated corrupt catalog entry"):
        repository.seed_demo_if_empty(db_path, sample_path)

    # Pages and the first two articles were already written before the
    # failure, but the batch transaction must leave nothing behind.
    assert calls[:2] == ["A-100", "A-200"]
    assert counts(db_path) == {
        "status": "ok",
        "database": str(db_path),
        "pages": 0,
        "articles": 0,
        "ocr_words": 0,
        "indexed_words": 0,
    }

    # Fixing the input and restarting with the same volume loads everything.
    monkeypatch.undo()
    assert repository.seed_demo_if_empty(db_path, sample_path) is True
    assert counts(db_path)["pages"] == 2
    assert counts(db_path)["articles"] == 3
    assert repository.search_phrase(
        db_path, "the lighthouse keeper climbed"
    )["count"] == 1
    assert repository.search_phrase(
        db_path, "only in the archive edition"
    )["count"] == 1
    assert repository.search_phrase(db_path, "weather noted a calm sea")["count"] == 1


def test_app_startup_failure_leaves_empty_db_then_fixed_file_loads_on_restart(
    tmp_path,
):
    db_path = tmp_path / "archive.sqlite3"
    broken = load_demo()
    # A duplicate block reference makes the last catalog entry invalid.
    broken["articles"][-1]["blocks"].append(
        dict(broken["articles"][-1]["blocks"][0])
    )
    broken_path = write_sample(tmp_path / "broken.json", broken)

    # Validation happens before any write; even if it happened mid-batch the
    # transaction would roll back the whole load.
    with pytest.raises(Exception):
        app = create_app(
            database_path=db_path,
            sample_path=broken_path,
            seed_demo=True,
        )
        from fastapi.testclient import TestClient

        with TestClient(app):
            pass

    assert counts(db_path)["pages"] == 0
    assert counts(db_path)["articles"] == 0

    fixed_path = write_sample(tmp_path / "fixed.json", load_demo())
    app = create_app(
        database_path=db_path,
        sample_path=fixed_path,
        seed_demo=True,
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/health").json()["articles"] == 3
        search = client.get(
            "/api/search/phrase", params={"q": "weather noted a calm sea"}
        )
        assert search.status_code == 200
        assert search.json()["count"] == 1
        for article_id in ("A-100", "A-200", "A-300"):
            info = client.get(f"/api/articles/{article_id}").json()
            assert info["revision"] == 1
            assert info["status"] == "indexed"
        for page_id in ("page-001", "page-002"):
            assert client.get(f"/api/pages/{page_id}").json()["revision"] == 1


def test_concurrent_first_start_seeds_exactly_once(tmp_path):
    db_path = tmp_path / "archive.sqlite3"
    workers = 6

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(workers)
    queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_seed_worker,
            args=(str(db_path), str(DEMO), barrier, queue),
        )
        for _ in range(workers)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert not process.is_alive()

    results = [queue.get(timeout=5) for _ in range(workers)]
    assert [status for status, _ in results] == ["ok"] * workers
    assert [loaded for _, loaded in results].count(True) == 1
    assert [loaded for _, loaded in results].count(False) == workers - 1

    assert counts(db_path)["pages"] == 2
    assert counts(db_path)["articles"] == 3
    assert counts(db_path)["ocr_words"] == 22
    assert counts(db_path)["indexed_words"] == 22
    assert repository.search_phrase(
        db_path, "the lighthouse keeper climbed"
    )["count"] == 1


def test_existing_database_keeps_skipping_seed(tmp_path):
    db_path = tmp_path / "archive.sqlite3"
    repository.initialize_database(db_path)
    assert repository.seed_demo_if_empty(db_path, DEMO) is True

    extended = load_demo()
    extended["articles"].append(
        {
            "article_id": "A-900",
            "title": "Never loaded",
            "blocks": [{"page_id": "page-001", "block_no": 1}],
        }
    )
    extended_path = write_sample(tmp_path / "extended.json", extended)

    assert repository.seed_demo_if_empty(db_path, extended_path) is False
    assert counts(db_path)["articles"] == 3
    with repository._connection(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM article_index WHERE article_id = 'A-900'"
        ).fetchone()[0] == 0
