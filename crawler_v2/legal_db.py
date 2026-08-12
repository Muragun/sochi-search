from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any


LEGAL_COLUMNS = {
    "document_number": "TEXT",
    "document_date": "TEXT",
    "document_kind": "TEXT",
    "parent_url": "TEXT",
    "attachment_title": "TEXT",
}


class LegalMetadataDatabase:
    def __init__(
        self,
        path: Path,
    ) -> None:
        self.path = path

    def connect(
        self,
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
        )

        connection.row_factory = (
            sqlite3.Row
        )

        connection.execute(
            "PRAGMA journal_mode=WAL"
        )

        connection.execute(
            "PRAGMA busy_timeout=30000"
        )

        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(crawl_urls)"
                ).fetchall()
            }

            if not columns:
                raise RuntimeError(
                    "Таблица crawl_urls "
                    "не найдена"
                )

            for name, definition in (
                LEGAL_COLUMNS.items()
            ):
                if name in columns:
                    continue

                connection.execute(
                    f"""
                    ALTER TABLE crawl_urls
                    ADD COLUMN {name} {definition}
                    """
                )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_crawl_urls_parent_url
                ON crawl_urls(parent_url)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_crawl_urls_document_number
                ON crawl_urls(document_number)
                """
            )

    def save_page_metadata(
        self,
        *,
        url: str,
        document_number: str | None,
        document_date: str | None,
        document_kind: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    document_number = ?,
                    document_date = ?,
                    document_kind = ?
                WHERE url = ?
                """,
                (
                    document_number,
                    document_date,
                    document_kind,
                    url,
                ),
            )

    def save_resolved_attachment_metadata(
        self,
        *,
        parent_url: str,
        attachment_url: str,
        document_number: str | None,
        document_date: str | None,
        document_kind: str | None,
    ) -> None:
        """Сохраняет проверенные реквизиты карточки у родителя и вложения."""

        connection = self.connect()

        try:
            connection.execute(
                "BEGIN IMMEDIATE"
            )
            connection.executemany(
                """
                UPDATE crawl_urls
                SET
                    document_number = COALESCE(
                        ?,
                        document_number
                    ),
                    document_date = COALESCE(
                        ?,
                        document_date
                    ),
                    document_kind = COALESCE(
                        ?,
                        document_kind
                    )
                WHERE url = ?
                """,
                (
                    (
                        document_number,
                        document_date,
                        document_kind,
                        parent_url,
                    ),
                    (
                        document_number,
                        document_date,
                        document_kind,
                        attachment_url,
                    ),
                ),
            )
            connection.commit()

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def replace_attachment_metadata(
        self,
        *,
        attachment_url: str,
        document_number: str | None,
        document_date: str | None,
        document_kind: str | None,
    ) -> None:
        """Записывает проверенные реквизиты вложения, включая явные NULL."""

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    document_number = ?,
                    document_date = ?,
                    document_kind = ?
                WHERE url = ?
                """,
                (
                    document_number,
                    document_date,
                    document_kind,
                    attachment_url,
                ),
            )

    def upsert_attachments(
        self,
        attachments: Iterable[Any],
        *,
        parent_url: str,
        seen_at: str,
    ) -> tuple[int, int]:
        inserted = 0
        existing = 0

        connection = self.connect()

        try:
            connection.execute(
                "BEGIN IMMEDIATE"
            )

            parent_metadata = connection.execute(
                """
                SELECT
                    document_number,
                    document_date,
                    document_kind
                FROM crawl_urls
                WHERE url = ?
                LIMIT 1
                """,
                (
                    parent_url,
                ),
            ).fetchone()

            parent_document_number = (
                parent_metadata["document_number"]
                if parent_metadata is not None
                else None
            )
            parent_document_date = (
                parent_metadata["document_date"]
                if parent_metadata is not None
                else None
            )
            parent_document_kind = (
                parent_metadata["document_kind"]
                if parent_metadata is not None
                else None
            )

            for attachment in attachments:
                attachment_url = str(
                    attachment.url
                )

                attachment_title = str(
                    attachment.title
                )

                already_exists = (
                    connection.execute(
                        """
                        SELECT 1
                        FROM crawl_urls
                        WHERE url = ?
                        """,
                        (
                            attachment_url,
                        ),
                    ).fetchone()
                    is not None
                )

                connection.execute(
                    """
                    INSERT INTO crawl_urls (
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
                        parent_url,
                        attachment_title,
                        document_number,
                        document_date,
                        document_kind
                    )
                    VALUES (
                        ?,
                        ?,
                        ?,
                        'attachment',
                        'pdf',
                        'application/pdf',
                        'discovered',
                        ?,
                        ?,
                        ?,
                        1,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?
                    )
                    ON CONFLICT(url) DO UPDATE SET
                        page_url = excluded.page_url,
                        file_url = excluded.file_url,

                        parent_url = excluded.parent_url,

                        attachment_title =
                            excluded.attachment_title,

                        document_number = COALESCE(
                            excluded.document_number,
                            crawl_urls.document_number
                        ),

                        document_date = COALESCE(
                            excluded.document_date,
                            crawl_urls.document_date
                        ),

                        document_kind = COALESCE(
                            excluded.document_kind,
                            crawl_urls.document_kind
                        ),

                        last_seen_at =
                            excluded.last_seen_at,

                        is_active = 1,

                        status = CASE
                            WHEN crawl_urls.status IN (
                                'gone',
                                'quarantined'
                            )
                            THEN 'discovered'
                            ELSE crawl_urls.status
                        END,

                        next_check_at = CASE
                            WHEN crawl_urls.status IN (
                                'gone',
                                'quarantined'
                            )
                            THEN excluded.next_check_at
                            ELSE crawl_urls.next_check_at
                        END
                    """,
                    (
                        attachment_url,
                        parent_url,
                        attachment_url,
                        seen_at,
                        seen_at,
                        seen_at,
                        parent_url,
                        attachment_title,
                        parent_document_number,
                        parent_document_date,
                        parent_document_kind,
                    ),
                )

                if already_exists:
                    existing += 1
                else:
                    inserted += 1

            connection.commit()

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

        return inserted, existing
