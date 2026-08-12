from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .url_policy import is_legacy_detail_url


MIGRATION_COLUMNS = {
    "discovered_at": "TEXT",
    "discovered_from": "TEXT",
    "sitemap_lastmod": "TEXT",
}


DISCOVERY_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS discovery_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    started_at TEXT NOT NULL,
    finished_at TEXT,

    status TEXT NOT NULL,

    robots_url TEXT,
    sitemaps_seen INTEGER NOT NULL DEFAULT 0,
    sitemap_errors INTEGER NOT NULL DEFAULT 0,

    candidates_seen INTEGER NOT NULL DEFAULT 0,
    known_urls INTEGER NOT NULL DEFAULT 0,
    new_urls_found INTEGER NOT NULL DEFAULT 0,
    inserted_urls INTEGER NOT NULL DEFAULT 0,

    message TEXT
);

CREATE INDEX IF NOT EXISTS
idx_discovery_runs_started_at
ON discovery_runs(started_at);
"""


class DiscoveryDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
        )

        connection.row_factory = sqlite3.Row
        connection.execute(
            "PRAGMA journal_mode=WAL"
        )
        connection.execute(
            "PRAGMA busy_timeout=30000"
        )

        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            existing_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(crawl_urls)"
                ).fetchall()
            }

            if not existing_columns:
                raise RuntimeError(
                    "Таблица crawl_urls не найдена. "
                    "Сначала выполните импорт SQLite."
                )

            for name, definition in (
                MIGRATION_COLUMNS.items()
            ):
                if name in existing_columns:
                    continue

                connection.execute(
                    f"""
                    ALTER TABLE crawl_urls
                    ADD COLUMN {name} {definition}
                    """
                )

            connection.executescript(
                DISCOVERY_RUNS_SQL
            )

    def known_urls(
        self,
        urls: Iterable[str],
    ) -> set[str]:
        values = list(dict.fromkeys(urls))
        result: set[str] = set()

        with self.connect() as connection:
            for start in range(
                0,
                len(values),
                500,
            ):
                batch = values[
                    start:start + 500
                ]

                if not batch:
                    continue

                placeholders = ",".join(
                    "?"
                    for _ in batch
                )

                rows = connection.execute(
                    f"""
                    SELECT url
                    FROM crawl_urls
                    WHERE url IN ({placeholders})
                    """,
                    batch,
                ).fetchall()

                result.update(
                    str(row["url"])
                    for row in rows
                )

        return result

    def insert_discovered(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        seen_at: str,
    ) -> tuple[int, int]:
        inserted = 0
        existing = 0

        connection = self.connect()

        try:
            connection.execute(
                "BEGIN IMMEDIATE"
            )

            for row in rows:
                if is_legacy_detail_url(
                    str(row["url"])
                ):
                    continue

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO crawl_urls (
                        url,
                        page_url,
                        file_url,
                        source,
                        doc_type,
                        content_type,
                        status,
                        first_seen_at,
                        last_seen_at,
                        next_check_at,
                        is_active,
                        discovered_at,
                        discovered_from,
                        sitemap_lastmod
                    )
                    VALUES (
                        :url,
                        :page_url,
                        :file_url,
                        'sitemap',
                        :doc_type,
                        :content_type,
                        'discovered',
                        :seen_at,
                        :seen_at,
                        :seen_at,
                        1,
                        :seen_at,
                        :discovered_from,
                        :sitemap_lastmod
                    )
                    """,
                    {
                        **row,
                        "seen_at": seen_at,
                    },
                )

                if cursor.rowcount == 1:
                    inserted += 1
                    continue

                existing += 1

                connection.execute(
                    """
                    UPDATE crawl_urls
                    SET
                        last_seen_at = ?,
                        discovered_from = COALESCE(
                            ?,
                            discovered_from
                        ),
                        sitemap_lastmod = COALESCE(
                            ?,
                            sitemap_lastmod
                        ),

                        is_active = CASE
                            WHEN status = 'gone'
                            THEN 1
                            ELSE is_active
                        END,

                        status = CASE
                            WHEN status = 'gone'
                            THEN 'discovered'
                            ELSE status
                        END,

                        next_check_at = CASE
                            WHEN status = 'gone'
                            THEN ?
                            ELSE next_check_at
                        END
                    WHERE url = ?
                    """,
                    (
                        seen_at,
                        row.get(
                            "discovered_from"
                        ),
                        row.get(
                            "sitemap_lastmod"
                        ),
                        seen_at,
                        row["url"],
                    ),
                )

            connection.commit()

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

        return inserted, existing

    def create_run(
        self,
        *,
        started_at: str,
        robots_url: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO discovery_runs (
                    started_at,
                    status,
                    robots_url
                )
                VALUES (?, 'running', ?)
                """,
                (
                    started_at,
                    robots_url,
                ),
            )

            run_id = cursor.lastrowid

        if run_id is None:
            raise RuntimeError(
                "Не удалось создать discovery run"
            )

        return int(run_id)

    def finish_run(
        self,
        run_id: int,
        *,
        finished_at: str,
        status: str,
        sitemaps_seen: int,
        sitemap_errors: int,
        candidates_seen: int,
        known_urls: int,
        new_urls_found: int,
        inserted_urls: int,
        message: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE discovery_runs
                SET
                    finished_at = ?,
                    status = ?,
                    sitemaps_seen = ?,
                    sitemap_errors = ?,
                    candidates_seen = ?,
                    known_urls = ?,
                    new_urls_found = ?,
                    inserted_urls = ?,
                    message = ?
                WHERE id = ?
                """,
                (
                    finished_at,
                    status,
                    sitemaps_seen,
                    sitemap_errors,
                    candidates_seen,
                    known_urls,
                    new_urls_found,
                    inserted_urls,
                    message,
                    run_id,
                ),
            )
