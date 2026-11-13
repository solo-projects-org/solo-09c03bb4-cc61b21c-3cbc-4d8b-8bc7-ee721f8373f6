import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo.json"
CORRECTED = ROOT / "samples" / "page-002-corrected.json"
MISSING_BLOCK = ROOT / "samples" / "page-002-missing-block.json"


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        database_path=tmp_path / "archive.sqlite3",
        sample_path=DEMO,
        seed_demo=True,
    )
    with TestClient(app) as test_client:
        yield test_client


def search(client: TestClient, q: str, **params):
    response = client.get("/api/search/phrase", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_cross_page_and_cross_block_phrase(client):
    data = search(client, "BULLETIN the lighthouse keeper climbed")
    assert data["count"] == 1
    hit = data["hits"][0]
    assert hit["article_id"] == "A-100"
    assert hit["pages"] == ["page-001", "page-002"]
    assert [word["page_id"] for word in hit["words"]] == [
        "page-001",
        "page-001",
        "page-001",
        "page-001",
        "page-002",
    ]
    assert [word["block_no"] for word in hit["words"]] == [1, 2, 2, 2, 1]
    assert hit["words"][-1]["bbox"] == {
        "x0": 100.0,
        "y0": 140.0,
        "x1": 240.0,
        "y1": 175.0,
    }


def test_case_insensitive_and_article_filter(client):
    upper = search(client, "THE LIGHTHOUSE KEEPER CLIMBED")
    lowered = search(client, "the lighthouse keeper climbed")
    assert upper["count"] == lowered["count"] == 1

    filtered = search(
        client, "only in the archive edition", article_id="A-100"
    )
    assert filtered["count"] == 0
    other = search(client, "only in the archive edition", article_id="A-200")
    assert other["count"] == 1


def test_phrase_cannot_cross_article_boundary(client):
    # A-100 ends with "dawn"; A-200 starts with "only" in the same page OCR.
    data = search(client, "dawn only")
    assert data["count"] == 0


def test_missing_catalog_block_rejects_whole_page_and_keeps_old_results(client):
    before = search(client, "the lighthouse keeper climbed")
    assert before["count"] == 1

    response = client.put(
        "/api/pages/page-002/ocr",
        json=load_json(MISSING_BLOCK),
    )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "missing_catalog_blocks"
    assert detail["missing_blocks"] == [2]
    assert detail["affected_articles"] == ["A-100", "A-200"]
    assert detail["unchanged"] is True

    # The body contained the changed block 1, but the whole page update was
    # rolled back: neither the old cross-page phrase nor the would-be new
    # phrase can observe a half-applied OCR replacement.
    assert search(client, "the lighthouse keeper walked")["count"] == 0
    assert search(client, "the lighthouse keeper climbed")["count"] == 1
    page = client.get("/api/pages/page-002").json()
    assert page["revision"] == 1


def test_single_page_correction_rebuilds_affected_articles(client):
    payload = load_json(CORRECTED)
    response = client.put("/api/pages/page-002/ocr", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["affected_articles"] == ["A-100", "A-200"]
    assert body["revision"] == 2

    old_cross_page = search(client, "the lighthouse keeper climbed")
    assert old_cross_page["count"] == 0
    new_cross_page = search(client, "the lighthouse keeper walked")
    assert new_cross_page["count"] == 1
    assert new_cross_page["hits"][0]["pages"] == ["page-001", "page-002"]

    assert search(client, "only in the archive edition")["count"] == 0
    assert search(client, "only in the archive supplement")["count"] == 1

    # The unaffected article is still searchable.
    assert search(client, "weather noted a calm sea")["count"] == 1


def test_catalog_rejects_missing_ocr_blocks_without_replacing_old_article(client):
    response = client.put(
        "/api/catalog/articles/A-100",
        json={
            "title": "Broken update",
            "blocks": [{"page_id": "page-999", "block_no": 1}],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "missing_ocr_blocks"

    article = client.get("/api/articles/A-100").json()
    assert article["title"] == "The Lighthouse Keeper"
    assert article["blocks"] == [
        {"page_id": "page-001", "block_no": 1},
        {"page_id": "page-001", "block_no": 2},
        {"page_id": "page-002", "block_no": 1},
    ]
    assert search(client, "the lighthouse keeper climbed")["count"] == 1


def test_flat_ocr_words_are_accepted(client):
    payload = {
        "image_url": "/scans/page-004.svg",
        "width": 1600,
        "height": 2200,
        "words": [
            {"block_no": 4, "text": "Flat", "bbox": [10, 10, 50, 40]},
            {"block_no": 4, "text": "input", "bbox": [55, 10, 110, 40]},
            {"block_no": 5, "text": "works", "bbox": [10, 80, 75, 110]},
        ],
    }
    response = client.put("/api/pages/page-004/ocr", json=payload)
    assert response.status_code == 200, response.text

    response = client.put(
        "/api/catalog/articles/A-400",
        json={
            "title": "Flat Input",
            "blocks": [
                {"page_id": "page-004", "block_no": 4},
                {"page_id": "page-004", "block_no": 5},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert search(client, "flat input works")["count"] == 1


def test_catalog_before_ocr_becomes_searchable_when_blocks_arrive(client):
    catalog = client.put(
        "/api/catalog/articles/A-500",
        json={
            "title": "Late OCR",
            "blocks": [{"page_id": "page-005", "block_no": 1}],
        },
    )
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["status"] == "pending_ocr"
    assert catalog.json()["indexed_words"] == 0

    ocr = client.put(
        "/api/pages/page-005/ocr",
        json={
            "width": 800,
            "height": 1000,
            "blocks": [
                {
                    "block_no": 1,
                    "words": [
                        {"text": "Catalog", "bbox": [10, 20, 90, 50]},
                        {"text": "first", "bbox": [100, 20, 155, 50]},
                    ],
                }
            ],
        },
    )
    assert ocr.status_code == 200, ocr.text
    assert ocr.json()["affected_articles"] == ["A-500"]

    article = client.get("/api/articles/A-500").json()
    assert article["status"] == "indexed"
    assert article["indexed_words"] == 2
    assert search(client, "catalog first")["count"] == 1


def test_demo_scan_is_served(client):
    response = client.get("/scans/page-001.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg")


def test_health_reports_seeded_counts(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["pages"] == 2
    assert body["articles"] == 3
    assert body["ocr_words"] == 22
    assert body["indexed_words"] == 22
