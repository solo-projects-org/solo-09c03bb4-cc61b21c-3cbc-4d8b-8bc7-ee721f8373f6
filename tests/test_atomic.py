import json
import threading
from pathlib import Path

from app import repository
from app.schemas import OCRPageInput

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo.json"
CORRECTED = ROOT / "samples" / "page-002-corrected.json"


def test_concurrent_search_sees_old_or_new_complete_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.sqlite3"
    repository.initialize_database(db_path)
    repository.seed_demo_if_empty(db_path, DEMO)

    corrected = OCRPageInput.model_validate(
        json.loads(CORRECTED.read_text(encoding="utf-8"))
    )

    original_rebuild = repository._rebuild_article
    started = threading.Event()
    release = threading.Event()
    call_count = [0]

    def paused_rebuild(conn, article_id):
        call_count[0] += 1
        if call_count[0] == 2:
            started.set()
            # The writer has already rebuilt A-100 inside its transaction but
            # has not rebuilt or committed A-200.  A reader must still see the
            # complete pre-correction snapshot for both articles.
            if not release.wait(timeout=10):
                raise AssertionError("writer was not released")
        return original_rebuild(conn, article_id)

    monkeypatch.setattr(repository, "_rebuild_article", paused_rebuild)

    errors = []

    def writer():
        try:
            repository.upsert_page_ocr(db_path, "page-002", corrected)
        except BaseException as exc:  # pragma: no branch - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert started.wait(timeout=10), "writer never reached the rebuild stage"

    # A separate read transaction must see the old, complete index for both
    # affected articles even though the writer has already replaced page OCR.
    assert repository.search_phrase(
        db_path, "the lighthouse keeper climbed"
    )["count"] == 1
    assert repository.search_phrase(
        db_path, "only in the archive edition"
    )["count"] == 1
    assert repository.search_phrase(
        db_path, "the lighthouse keeper walked"
    )["count"] == 0

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []

    # After one atomic commit, all affected articles and locations are new.
    assert repository.search_phrase(
        db_path, "the lighthouse keeper climbed"
    )["count"] == 0
    assert repository.search_phrase(
        db_path, "the lighthouse keeper walked"
    )["count"] == 1
    assert repository.search_phrase(
        db_path, "only in the archive edition"
    )["count"] == 0
    assert repository.search_phrase(
        db_path, "only in the archive supplement"
    )["count"] == 1
