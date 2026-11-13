from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schemas import CatalogArticleInput, OCRPageInput, OCRWordInput


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    page_id     TEXT PRIMARY KEY,
    image_url   TEXT,
    width       REAL,
    height      REAL,
    revision    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_blocks (
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    ordinal     INTEGER NOT NULL,
    PRIMARY KEY (page_id, block_no),
    UNIQUE (page_id, ordinal),
    FOREIGN KEY (page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS page_words (
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    word_no     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    token       TEXT NOT NULL,
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    x1          REAL NOT NULL,
    y1          REAL NOT NULL,
    PRIMARY KEY (page_id, block_no, word_no),
    FOREIGN KEY (page_id, block_no)
        REFERENCES page_blocks(page_id, block_no) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_page_words_token ON page_words(token);

CREATE TABLE IF NOT EXISTS articles (
    article_id     TEXT PRIMARY KEY,
    title          TEXT,
    revision       INTEGER NOT NULL,
    updated_at     TEXT NOT NULL,
    indexed_words  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS catalog_blocks (
    article_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    PRIMARY KEY (article_id, seq),
    UNIQUE (article_id, page_id, block_no),
    FOREIGN KEY (article_id) REFERENCES articles(article_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_catalog_blocks_page
    ON catalog_blocks(page_id, block_no);

CREATE TABLE IF NOT EXISTS article_index (
    article_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    word_no     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    token       TEXT NOT NULL,
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    x1          REAL NOT NULL,
    y1          REAL NOT NULL,
    PRIMARY KEY (article_id, seq),
    FOREIGN KEY (article_id) REFERENCES articles(article_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_article_index_token ON article_index(token);
CREATE INDEX IF NOT EXISTS idx_article_index_article_seq
    ON article_index(article_id, seq);
"""

MAX_PHRASE_TOKENS = 40


class DomainError(Exception):
    """An expected error that should be returned as a structured HTTP error."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_token(value: str) -> str:
    """Normalize case and Unicode width while keeping OCR token boundaries."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def _connection(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # BEGIN IMMEDIATE serializes writers.  All source changes and every
    # affected article's index rows commit together, never partially.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def _read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # A deferred read transaction gives a single SQLite snapshot for the whole
    # search (candidate positions, boxes, and article titles).
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def initialize_database(db_path: str | os.PathLike[str]) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connection(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def _get_page_refs(
    conn: sqlite3.Connection, page_id: str
) -> tuple[set[int], list[str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT article_id, block_no
        FROM catalog_blocks
        WHERE page_id = ?
        ORDER BY article_id, block_no
        """,
        (page_id,),
    ).fetchall()
    required = {int(row["block_no"]) for row in rows}
    affected = sorted({str(row["article_id"]) for row in rows})
    return required, affected


def _replace_page_source(
    conn: sqlite3.Connection,
    page_id: str,
    payload: OCRPageInput,
    blocks: list[tuple[int, list[OCRWordInput]]],
    updated_at: str,
) -> tuple[int, int]:
    old = conn.execute(
        "SELECT revision FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    revision = (int(old["revision"]) + 1) if old else 1

    if old:
        conn.execute(
            """
            UPDATE pages
            SET image_url = ?, width = ?, height = ?,
                revision = ?, updated_at = ?
            WHERE page_id = ?
            """,
            (
                payload.image_url,
                payload.width,
                payload.height,
                revision,
                updated_at,
                page_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO pages
                (page_id, image_url, width, height, revision, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                payload.image_url,
                payload.width,
                payload.height,
                revision,
                updated_at,
            ),
        )

    # Children first; ordering is explicit because page block numbers need not
    # be contiguous or ascending in the source export.
    conn.execute("DELETE FROM page_words WHERE page_id = ?", (page_id,))
    conn.execute("DELETE FROM page_blocks WHERE page_id = ?", (page_id,))

    word_count = 0
    for ordinal, (block_no, words) in enumerate(blocks):
        conn.execute(
            "INSERT INTO page_blocks (page_id, block_no, ordinal) VALUES (?, ?, ?)",
            (page_id, block_no, ordinal),
        )
        rows = []
        for word_no, word in enumerate(words):
            token = normalize_token(word.text)
            rows.append(
                (
                    page_id,
                    block_no,
                    word_no,
                    word.text,
                    token,
                    word.bbox.x0,
                    word.bbox.y0,
                    word.bbox.x1,
                    word.bbox.y1,
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO page_words
                    (page_id, block_no, word_no, text, token, x0, y0, x1, y1)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        word_count += len(words)

    return revision, word_count


def _rebuild_article(conn: sqlite3.Connection, article_id: str) -> int:
    """Rebuild one article's flattened OCR sequence from current page OCR.

    An article is indexed only when every catalog-referenced block is present
    in OCR.  This makes catalog-before-OCR ingestion possible without exposing
    phrases that would silently jump over a missing block.
    """
    conn.execute("DELETE FROM article_index WHERE article_id = ?", (article_id,))

    incomplete = conn.execute(
        """
        SELECT 1
        FROM catalog_blocks AS cb
        LEFT JOIN page_blocks AS pb
          ON pb.page_id = cb.page_id AND pb.block_no = cb.block_no
        WHERE cb.article_id = ? AND pb.page_id IS NULL
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    if incomplete:
        conn.execute(
            "UPDATE articles SET indexed_words = 0 WHERE article_id = ?",
            (article_id,),
        )
        return 0

    rows = conn.execute(
        """
        SELECT pw.page_id, pw.block_no, pw.word_no, pw.text, pw.token,
               pw.x0, pw.y0, pw.x1, pw.y1
        FROM catalog_blocks AS cb
        JOIN page_words AS pw
          ON pw.page_id = cb.page_id
         AND pw.block_no = cb.block_no
        WHERE cb.article_id = ?
        ORDER BY cb.seq ASC, pw.word_no ASC
        """,
        (article_id,),
    ).fetchall()

    params = [
        (
            article_id,
            seq,
            row["page_id"],
            row["block_no"],
            row["word_no"],
            row["text"],
            row["token"],
            row["x0"],
            row["y0"],
            row["x1"],
            row["y1"],
        )
        for seq, row in enumerate(rows)
    ]
    if params:
        conn.executemany(
            """
            INSERT INTO article_index
                (article_id, seq, page_id, block_no, word_no,
                 text, token, x0, y0, x1, y1)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
    conn.execute(
        "UPDATE articles SET indexed_words = ? WHERE article_id = ?",
        (len(rows), article_id),
    )
    return len(rows)


def upsert_page_ocr(
    db_path: str | os.PathLike[str],
    page_id: str,
    payload: OCRPageInput,
) -> dict[str, Any]:
    page_id = page_id.strip()
    if not page_id:
        raise DomainError(422, "invalid_page_id", "page_id must not be blank")

    blocks = payload.grouped_blocks()
    provided = {block_no for block_no, _ in blocks}
    updated_at = utc_now()

    with _connection(db_path) as conn:
        with _write_transaction(conn):
            required, affected = _get_page_refs(conn, page_id)
            missing = sorted(required - provided)
            if missing:
                raise DomainError(
                    409,
                    "missing_catalog_blocks",
                    (
                        "OCR retransmission rejected: the page omits blocks that "
                        "catalog articles reference. No OCR or index change was "
                        "committed; the previous results remain queryable."
                    ),
                    page_id=page_id,
                    missing_blocks=missing,
                    affected_articles=affected,
                    unchanged=True,
                )

            revision, word_count = _replace_page_source(
                conn, page_id, payload, blocks, updated_at
            )

            for article_id in affected:
                _rebuild_article(conn, article_id)

            return {
                "page_id": page_id,
                "revision": revision,
                "updated_at": updated_at,
                "blocks_count": len(blocks),
                "words_count": word_count,
                "affected_articles": affected,
                "message": (
                    "Page OCR and all affected article indexes committed "
                    "atomically."
                ),
            }


def upsert_catalog_article(
    db_path: str | os.PathLike[str],
    article_id: str,
    payload: CatalogArticleInput,
) -> dict[str, Any]:
    article_id = article_id.strip()
    if not article_id:
        raise DomainError(422, "invalid_article_id", "article_id must not be blank")

    updated_at = utc_now()
    with _connection(db_path) as conn:
        with _write_transaction(conn):
            old = conn.execute(
                "SELECT revision FROM articles WHERE article_id = ?",
                (article_id,),
            ).fetchone()

            missing = []
            for ref in payload.blocks:
                exists = conn.execute(
                    """
                    SELECT 1 FROM page_blocks
                    WHERE page_id = ? AND block_no = ?
                    """,
                    (ref.page_id, ref.block_no),
                ).fetchone()
                if not exists:
                    missing.append(f"{ref.page_id}/{ref.block_no}")

            # A brand-new article may be cataloged before its OCR pages arrive;
            # it stays unindexed until complete.  Replacing an existing article
            # with missing OCR blocks is rejected so its old results are not
            # silently discarded.
            if missing and old:
                raise DomainError(
                    409,
                    "missing_ocr_blocks",
                    (
                        "Catalog replacement rejected because referenced OCR "
                        "blocks are not present. The previous catalog and index "
                        "remain unchanged."
                    ),
                    article_id=article_id,
                    missing_blocks=missing,
                    unchanged=True,
                )

            revision = (int(old["revision"]) + 1) if old else 1

            if old:
                conn.execute(
                    """
                    UPDATE articles
                    SET title = ?, revision = ?, updated_at = ?
                    WHERE article_id = ?
                    """,
                    (payload.title, revision, updated_at, article_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO articles
                        (article_id, title, revision, updated_at, indexed_words)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (article_id, payload.title, revision, updated_at),
                )

            conn.execute(
                "DELETE FROM catalog_blocks WHERE article_id = ?", (article_id,)
            )
            conn.executemany(
                """
                INSERT INTO catalog_blocks
                    (article_id, seq, page_id, block_no)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (article_id, seq, ref.page_id, ref.block_no)
                    for seq, ref in enumerate(payload.blocks)
                ],
            )

            _rebuild_article(conn, article_id)
            return _article_info_from_conn(conn, article_id)


def _page_info_from_conn(conn: sqlite3.Connection, page_id: str) -> dict[str, Any]:
    page = conn.execute(
        "SELECT * FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if not page:
        raise DomainError(404, "page_not_found", f"page {page_id!r} was not found")

    blocks = conn.execute(
        """
        SELECT pb.block_no, pb.ordinal, COUNT(pw.word_no) AS word_count
        FROM page_blocks AS pb
        LEFT JOIN page_words AS pw
          ON pw.page_id = pb.page_id AND pw.block_no = pb.block_no
        WHERE pb.page_id = ?
        GROUP BY pb.page_id, pb.block_no, pb.ordinal
        ORDER BY pb.ordinal
        """,
        (page_id,),
    ).fetchall()

    return {
        "page_id": page["page_id"],
        "image_url": page["image_url"],
        "width": page["width"],
        "height": page["height"],
        "revision": int(page["revision"]),
        "updated_at": page["updated_at"],
        "blocks": [
            {
                "block_no": int(row["block_no"]),
                "ordinal": int(row["ordinal"]),
                "word_count": int(row["word_count"]),
            }
            for row in blocks
        ],
    }


def get_page_info(
    db_path: str | os.PathLike[str], page_id: str
) -> dict[str, Any]:
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            return _page_info_from_conn(conn, page_id)


def _article_info_from_conn(
    conn: sqlite3.Connection, article_id: str
) -> dict[str, Any]:
    article = conn.execute(
        "SELECT * FROM articles WHERE article_id = ?", (article_id,)
    ).fetchone()
    if not article:
        raise DomainError(
            404, "article_not_found", f"article {article_id!r} was not found"
        )

    blocks = conn.execute(
        """
        SELECT page_id, block_no
        FROM catalog_blocks
        WHERE article_id = ?
        ORDER BY seq
        """,
        (article_id,),
    ).fetchall()
    incomplete = conn.execute(
        """
        SELECT 1
        FROM catalog_blocks AS cb
        LEFT JOIN page_blocks AS pb
          ON pb.page_id = cb.page_id AND pb.block_no = cb.block_no
        WHERE cb.article_id = ? AND pb.page_id IS NULL
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    return {
        "article_id": article["article_id"],
        "title": article["title"],
        "revision": int(article["revision"]),
        "updated_at": article["updated_at"],
        "status": "pending_ocr" if incomplete else "indexed",
        "indexed_words": int(article["indexed_words"]),
        "blocks": [
            {"page_id": row["page_id"], "block_no": int(row["block_no"])}
            for row in blocks
        ],
    }


def get_article_info(
    db_path: str | os.PathLike[str], article_id: str
) -> dict[str, Any]:
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            return _article_info_from_conn(conn, article_id)


def _candidate_starts(
    conn: sqlite3.Connection,
    tokens: list[str],
    article_id: str | None,
    limit: int,
) -> tuple[list[sqlite3.Row], bool]:
    """Find consecutive token runs with a bounded number of SQL self joins."""
    token_count = len(tokens)
    params: list[Any] = []

    if token_count == 1:
        sql = "SELECT article_id, seq FROM article_index WHERE token = ?"
        params.append(tokens[0])
    else:
        parts = ["SELECT i0.article_id, i0.seq FROM article_index AS i0"]
        for offset in range(1, token_count):
            parts.append(
                "JOIN article_index AS i{off} "
                "ON i{off}.article_id = i0.article_id "
                "AND i{off}.seq = i0.seq + {off} "
                "AND i{off}.token = ?".format(off=offset)
            )
            params.append(tokens[offset])
        parts.append("WHERE i0.token = ?")
        params.append(tokens[0])
        sql = "\n".join(parts)

    if article_id:
        sql += " AND article_id = ?" if token_count == 1 else " AND i0.article_id = ?"
        params.append(article_id)

    sql += (
        " ORDER BY article_id, seq" if token_count == 1 else " ORDER BY i0.article_id, i0.seq"
    )
    sql += " LIMIT ?"
    params.append(limit + 1)

    rows = conn.execute(sql, params).fetchall()
    return rows[:limit], len(rows) > limit


def search_phrase(
    db_path: str | os.PathLike[str],
    phrase: str,
    article_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    tokens = [normalize_token(token) for token in phrase.split()]
    if not tokens:
        raise DomainError(422, "empty_phrase", "phrase must contain at least one token")
    if len(tokens) > MAX_PHRASE_TOKENS:
        raise DomainError(
            422,
            "phrase_too_long",
            f"phrase may contain at most {MAX_PHRASE_TOKENS} tokens",
        )
    if limit < 1 or limit > 200:
        raise DomainError(422, "invalid_limit", "limit must be between 1 and 200")

    article_id = article_id.strip() if article_id else None
    if article_id == "":
        article_id = None

    with _connection(db_path) as conn:
        with _read_transaction(conn):
            candidates, truncated = _candidate_starts(
                conn, tokens, article_id, limit
            )
            if not candidates:
                return {
                    "query": phrase,
                    "normalized_tokens": tokens,
                    "limit": limit,
                    "count": 0,
                    "truncated": False,
                    "hits": [],
                }

            article_ids = sorted({str(row["article_id"]) for row in candidates})
            title_rows = conn.execute(
                """
                SELECT article_id, title
                FROM articles
                WHERE article_id IN ({})
                """.format(", ".join("?" for _ in article_ids)),
                article_ids,
            ).fetchall()
            titles = {
                str(row["article_id"]): row["title"] for row in title_rows
            }

            needed: dict[str, set[int]] = defaultdict(set)
            for row in candidates:
                start = int(row["seq"])
                aid = str(row["article_id"])
                needed[aid].update(range(start, start + len(tokens)))

            word_rows = []
            for aid, seqs in needed.items():
                ordered = sorted(seqs)
                # Stay comfortably below SQLite's host-parameter limit.
                for offset in range(0, len(ordered), 500):
                    chunk = ordered[offset : offset + 500]
                    word_rows.extend(
                        conn.execute(
                            """
                            SELECT article_id, seq, page_id, block_no, word_no,
                                   text, token, x0, y0, x1, y1
                            FROM article_index
                            WHERE article_id = ?
                              AND seq IN ({})
                            ORDER BY seq
                            """.format(", ".join("?" for _ in chunk)),
                            [aid, *chunk],
                        ).fetchall()
                    )

            words_by_seq = {
                (str(row["article_id"]), int(row["seq"])): row
                for row in word_rows
            }

            hits = []
            for candidate in candidates:
                aid = str(candidate["article_id"])
                start = int(candidate["seq"])
                sequence = [
                    words_by_seq[(aid, seq)]
                    for seq in range(start, start + len(tokens))
                ]
                pages: list[str] = []
                for row in sequence:
                    page_id = str(row["page_id"])
                    if page_id not in pages:
                        pages.append(page_id)

                hits.append(
                    {
                        "article_id": aid,
                        "title": titles.get(aid),
                        "start_seq": start,
                        "pages": pages,
                        "words": [
                            {
                                "seq": int(row["seq"]),
                                "page_id": row["page_id"],
                                "block_no": int(row["block_no"]),
                                "word_no": int(row["word_no"]),
                                "text": row["text"],
                                "bbox": {
                                    "x0": row["x0"],
                                    "y0": row["y0"],
                                    "x1": row["x1"],
                                    "y1": row["y1"],
                                },
                            }
                            for row in sequence
                        ],
                    }
                )

            return {
                "query": phrase,
                "normalized_tokens": tokens,
                "limit": limit,
                "count": len(hits),
                "truncated": truncated,
                "hits": hits,
            }


def health_counts(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    with _connection(db_path) as conn:
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        ocr_words = conn.execute("SELECT COUNT(*) FROM page_words").fetchone()[0]
        indexed_words = conn.execute("SELECT COUNT(*) FROM article_index").fetchone()[0]
    return {
        "status": "ok",
        "database": str(db_path),
        "pages": int(pages),
        "articles": int(articles),
        "ocr_words": int(ocr_words),
        "indexed_words": int(indexed_words),
    }


def seed_demo_if_empty(
    db_path: str | os.PathLike[str], sample_path: str | os.PathLike[str]
) -> bool:
    """Load the bundled local sample once into an empty database."""
    counts = health_counts(db_path)
    if counts["pages"] or counts["articles"]:
        return False

    with open(sample_path, "r", encoding="utf-8") as handle:
        sample = json.load(handle)

    for page_data in sample.get("pages", []):
        page_id = page_data["page_id"]
        page_payload = OCRPageInput.model_validate(page_data)
        upsert_page_ocr(db_path, page_id, page_payload)

    for article_data in sample.get("articles", []):
        article_id = article_data["article_id"]
        article_payload = CatalogArticleInput.model_validate(article_data)
        upsert_catalog_article(db_path, article_id, article_payload)

    return True
