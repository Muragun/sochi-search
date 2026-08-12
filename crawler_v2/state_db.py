from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crawl_urls (
    url TEXT PRIMARY KEY,

    page_url TEXT,
    file_url TEXT,
    source TEXT,

    doc_type TEXT,
    content_type TEXT,
    content_hash TEXT,
    extraction_version TEXT,

    status TEXT NOT NULL DEFAULT 'discovered',
    http_status INTEGER,

    etag TEXT,
    last_modified TEXT,

    attempt_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,

    last_error TEXT,

    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,

    last_fetched_at TEXT,
    last_indexed_at TEXT,
    source_updated_at TEXT,

    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_crawl_urls_status
    ON crawl_urls(status);

CREATE INDEX IF NOT EXISTS idx_crawl_urls_last_seen
    ON crawl_urls(last_seen_at);

CREATE INDEX IF NOT EXISTS idx_crawl_urls_http_status
    ON crawl_urls(http_status);

CREATE INDEX IF NOT EXISTS idx_crawl_urls_active
    ON crawl_urls(is_active);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    run_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,

    status TEXT NOT NULL,

    documents_seen INTEGER NOT NULL DEFAULT 0,
    urls_seen INTEGER NOT NULL DEFAULT 0,
    inserted_urls INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,

    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_started_at
    ON crawl_runs(started_at);
"""


UPSERT_URL_SQL = """
INSERT INTO crawl_urls (
    url,
    page_url,
    file_url,
    source,
    doc_type,
    content_type,
    content_hash,
    status,
    http_status,
    etag,
    last_modified,
    attempt_count,
    failure_count,
    last_error,
    first_seen_at,
    last_seen_at,
    last_fetched_at,
    last_indexed_at,
    source_updated_at,
    is_active
)
VALUES (
    :url,
    :page_url,
    :file_url,
    :source,
    :doc_type,
    :content_type,
    :content_hash,
    :status,
    :http_status,
    :etag,
    :last_modified,
    :attempt_count,
    :failure_count,
    :last_error,
    :first_seen_at,
    :last_seen_at,
    :last_fetched_at,
    :last_indexed_at,
    :source_updated_at,
    :is_active
)
ON CONFLICT(url) DO UPDATE SET
    page_url = COALESCE(
        excluded.page_url,
        crawl_urls.page_url
    ),

    file_url = COALESCE(
        excluded.file_url,
        crawl_urls.file_url
    ),

    source = COALESCE(
        excluded.source,
        crawl_urls.source
    ),

    doc_type = COALESCE(
        excluded.doc_type,
        crawl_urls.doc_type
    ),

    content_type = COALESCE(
        excluded.content_type,
        crawl_urls.content_type
    ),

    content_hash = COALESCE(
        excluded.content_hash,
        crawl_urls.content_hash
    ),

    status = excluded.status,

    last_seen_at = excluded.last_seen_at,

    last_fetched_at = COALESCE(
        excluded.last_fetched_at,
        crawl_urls.last_fetched_at
    ),

    last_indexed_at = COALESCE(
        excluded.last_indexed_at,
        crawl_urls.last_indexed_at
    ),

    source_updated_at = COALESCE(
        excluded.source_updated_at,
        crawl_urls.source_updated_at
    ),

    is_active = 1
"""


class CrawlStateDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        connection = sqlite3.connect(
            self.path,
            timeout=30,
        )

        connection.row_factory = sqlite3.Row

        connection.execute(
            "PRAGMA journal_mode=WAL"
        )

        connection.execute(
            "PRAGMA foreign_keys=ON"
        )

        connection.execute(
            "PRAGMA busy_timeout=30000"
        )

        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA_SQL)

    def count_urls(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM crawl_urls"
            ).fetchone()

        return int(row["count"])

    def create_run(
        self,
        *,
        run_type: str,
        started_at: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO crawl_runs (
                    run_type,
                    started_at,
                    status
                )
                VALUES (?, ?, 'running')
                """,
                (
                    run_type,
                    started_at,
                ),
            )

            run_id = cursor.lastrowid

        if run_id is None:
            raise RuntimeError(
                "SQLite не вернул идентификатор запуска"
            )

        return int(run_id)

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        documents_seen: int,
        urls_seen: int,
        inserted_urls: int,
        errors: int,
        message: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_runs
                SET
                    finished_at = ?,
                    status = ?,
                    documents_seen = ?,
                    urls_seen = ?,
                    inserted_urls = ?,
                    errors = ?,
                    message = ?
                WHERE id = ?
                """,
                (
                    finished_at,
                    status,
                    documents_seen,
                    urls_seen,
                    inserted_urls,
                    errors,
                    message,
                    run_id,
                ),
            )

    def upsert_urls(
        self,
        rows: Iterable[dict[str, Any]],
    ) -> None:
        prepared_rows = list(rows)

        if not prepared_rows:
            return

        with self.connect() as connection:
            connection.executemany(
                UPSERT_URL_SQL,
                prepared_rows,
            )

    def get_statistics(self) -> dict[str, Any]:
        with self.connect() as connection:
            total_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(
                        CASE WHEN is_active = 1
                        THEN 1 ELSE 0 END
                    ) AS active
                FROM crawl_urls
                """
            ).fetchone()

            status_rows = connection.execute(
                """
                SELECT
                    status,
                    COUNT(*) AS count
                FROM crawl_urls
                GROUP BY status
                ORDER BY count DESC
                """
            ).fetchall()

            type_rows = connection.execute(
                """
                SELECT
                    COALESCE(doc_type, 'unknown') AS doc_type,
                    COUNT(*) AS count
                FROM crawl_urls
                GROUP BY COALESCE(doc_type, 'unknown')
                ORDER BY count DESC
                """
            ).fetchall()

            latest_run = connection.execute(
                """
                SELECT *
                FROM crawl_runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()

        return {
            "total": int(total_row["total"] or 0),
            "active": int(total_row["active"] or 0),

            "statuses": {
                str(row["status"]): int(row["count"])
                for row in status_rows
            },

            "document_types": {
                str(row["doc_type"]): int(row["count"])
                for row in type_rows
            },

            "latest_run": (
                dict(latest_run)
                if latest_run is not None
                else None
            ),
        }
