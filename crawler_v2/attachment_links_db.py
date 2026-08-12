from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .url_normalization import normalize_project_url


ATTACHMENT_LINKS_SQL = """
CREATE TABLE IF NOT EXISTS attachment_links (
    parent_url TEXT NOT NULL,
    attachment_url TEXT NOT NULL,

    attachment_title TEXT,
    source TEXT NOT NULL DEFAULT 'html',

    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,

    is_active INTEGER NOT NULL DEFAULT 1
        CHECK (is_active IN (0, 1)),

    PRIMARY KEY (
        parent_url,
        attachment_url
    )
);

CREATE INDEX IF NOT EXISTS
idx_attachment_links_attachment_url
ON attachment_links(attachment_url);

CREATE INDEX IF NOT EXISTS
idx_attachment_links_parent_url
ON attachment_links(parent_url);

CREATE INDEX IF NOT EXISTS
idx_attachment_links_active
ON attachment_links(is_active);
"""


def iso_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


class AttachmentLinksDatabase:
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
            connection.executescript(
                ATTACHMENT_LINKS_SQL
            )

    def upsert(
        self,
        *,
        parent_url: str,
        attachment_url: str,
        attachment_title: str | None,
        seen_at: str,
        source: str = "html",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO attachment_links (
                    parent_url,
                    attachment_url,
                    attachment_title,
                    source,
                    first_seen_at,
                    last_seen_at,
                    is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, 1)

                ON CONFLICT(
                    parent_url,
                    attachment_url
                )
                DO UPDATE SET
                    attachment_title = COALESCE(
                        NULLIF(
                            excluded.attachment_title,
                            ''
                        ),
                        attachment_links.attachment_title
                    ),

                    source = excluded.source,
                    last_seen_at = excluded.last_seen_at,
                    is_active = 1
                """,
                (
                    parent_url,
                    attachment_url,
                    attachment_title,
                    source,
                    seen_at,
                    seen_at,
                ),
            )

    def seed_from_crawl_urls(
        self,
    ) -> dict[str, int]:
        self.migrate()

        connection = self.connect()

        try:
            rows = connection.execute(
                """
                SELECT
                    url,
                    page_url,
                    parent_url,
                    attachment_title,
                    first_seen_at,
                    last_seen_at
                FROM crawl_urls
                WHERE doc_type = 'pdf'
                  AND url IS NOT NULL
                  AND TRIM(url) != ''
                """
            ).fetchall()

            inserted_or_updated = 0
            skipped = 0

            connection.execute(
                "BEGIN IMMEDIATE"
            )

            for row in rows:
                attachment_result = (
                    normalize_project_url(
                        str(row["url"])
                    )
                )

                raw_parent = (
                    row["parent_url"]
                    or row["page_url"]
                )

                if (
                    attachment_result is None
                    or not raw_parent
                ):
                    skipped += 1
                    continue

                parent_result = (
                    normalize_project_url(
                        str(raw_parent)
                    )
                )

                if parent_result is None:
                    skipped += 1
                    continue

                attachment_url = (
                    attachment_result.normalized
                )

                parent_url = (
                    parent_result.normalized
                )

                if attachment_url == parent_url:
                    skipped += 1
                    continue

                first_seen_at = (
                    row["first_seen_at"]
                    or row["last_seen_at"]
                    or iso_now()
                )

                last_seen_at = (
                    row["last_seen_at"]
                    or first_seen_at
                )

                connection.execute(
                    """
                    INSERT INTO attachment_links (
                        parent_url,
                        attachment_url,
                        attachment_title,
                        source,
                        first_seen_at,
                        last_seen_at,
                        is_active
                    )
                    VALUES (
                        ?, ?, ?, 'migration',
                        ?, ?, 1
                    )

                    ON CONFLICT(
                        parent_url,
                        attachment_url
                    )
                    DO UPDATE SET
                        attachment_title = COALESCE(
                            NULLIF(
                                excluded.attachment_title,
                                ''
                            ),
                            attachment_links.attachment_title
                        ),

                        last_seen_at = CASE
                            WHEN excluded.last_seen_at >
                                 attachment_links.last_seen_at
                            THEN excluded.last_seen_at
                            ELSE attachment_links.last_seen_at
                        END,

                        is_active = 1
                    """,
                    (
                        parent_url,
                        attachment_url,
                        row["attachment_title"],
                        first_seen_at,
                        last_seen_at,
                    ),
                )

                inserted_or_updated += 1

            connection.commit()

            return {
                "pdf_rows_seen": len(rows),
                "processed_links": (
                    inserted_or_updated
                ),
                "skipped": skipped,
            }

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def statistics(self) -> dict[str, Any]:
        with self.connect() as connection:
            total_links = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM attachment_links
                WHERE is_active = 1
                """
            ).fetchone()["count"]

            unique_attachments = (
                connection.execute(
                    """
                    SELECT COUNT(
                        DISTINCT attachment_url
                    ) AS count
                    FROM attachment_links
                    WHERE is_active = 1
                    """
                ).fetchone()["count"]
            )

            unique_parents = (
                connection.execute(
                    """
                    SELECT COUNT(
                        DISTINCT parent_url
                    ) AS count
                    FROM attachment_links
                    WHERE is_active = 1
                    """
                ).fetchone()["count"]
            )

            multiple_parents = (
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM (
                        SELECT attachment_url
                        FROM attachment_links
                        WHERE is_active = 1
                        GROUP BY attachment_url
                        HAVING COUNT(
                            DISTINCT parent_url
                        ) > 1
                    )
                    """
                ).fetchone()["count"]
            )

        return {
            "total_links": int(
                total_links or 0
            ),
            "unique_attachments": int(
                unique_attachments or 0
            ),
            "unique_parents": int(
                unique_parents or 0
            ),
            "attachments_with_multiple_parents": int(
                multiple_parents or 0
            ),
        }
