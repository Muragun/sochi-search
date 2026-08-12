from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


BACKFILL_COLUMNS = {
    "legal_backfill_status": "TEXT",
    "legal_backfill_attempts": (
        "INTEGER NOT NULL DEFAULT 0"
    ),
    "legal_backfill_last_error": "TEXT",
    "legal_backfill_checked_at": "TEXT",
    "legal_backfill_next_at": "TEXT",
    "legal_backfill_locked_at": "TEXT",
    "legal_backfill_locked_by": "TEXT",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def iso_after_hours(hours: int) -> str:
    return (
        utc_now()
        + timedelta(hours=hours)
    ).isoformat()


class LegalBackfillDatabase:
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
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(crawl_urls)"
                ).fetchall()
            }

            if not columns:
                raise RuntimeError(
                    "Таблица crawl_urls не найдена"
                )

            for name, definition in (
                BACKFILL_COLUMNS.items()
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
                idx_crawl_urls_legal_backfill_status
                ON crawl_urls(
                    legal_backfill_status
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_crawl_urls_legal_backfill_next
                ON crawl_urls(
                    legal_backfill_next_at
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_crawl_urls_legal_backfill_lock
                ON crawl_urls(
                    legal_backfill_locked_at
                )
                """
            )

    @staticmethod
    def legal_page_condition() -> str:
        return """
            is_active = 1
            AND doc_type = 'page'

            AND (
                LOWER(
                    COALESCE(url, '')
                ) LIKE
                    '%normativno-pravovyye-akty%'
                OR LOWER(
                    COALESCE(page_url, '')
                ) LIKE
                    '%normativno-pravovyye-akty%'
            )

            AND (
                LOWER(
                    COALESCE(url, '')
                ) LIKE
                    '%element_id=%'
                OR LOWER(
                    COALESCE(page_url, '')
                ) LIKE
                    '%element_id=%'
            )
        """

    def seed_queue(self) -> int:
        condition = (
            self.legal_page_condition()
        )

        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE crawl_urls
                SET
                    legal_backfill_status =
                        'pending',

                    legal_backfill_next_at =
                        COALESCE(
                            legal_backfill_next_at,
                            ?
                        ),

                    legal_backfill_last_error =
                        NULL,

                    legal_backfill_locked_at =
                        NULL,

                    legal_backfill_locked_by =
                        NULL
                WHERE {condition}
                  AND legal_backfill_status
                      IS NULL
                """,
                (
                    iso_now(),
                ),
            )

            return int(cursor.rowcount)

    def reset_failed(
        self,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE crawl_urls
                SET
                    legal_backfill_status =
                        'pending',

                    legal_backfill_next_at = ?,

                    legal_backfill_last_error =
                        NULL,

                    legal_backfill_locked_at =
                        NULL,

                    legal_backfill_locked_by =
                        NULL
                WHERE legal_backfill_status
                    IN (
                        'failed',
                        'retry'
                    )
                """,
                (
                    iso_now(),
                ),
            )

            return int(cursor.rowcount)

    def release_stale_locks(
        self,
        *,
        older_than_minutes: int,
    ) -> int:
        threshold = (
            utc_now()
            - timedelta(
                minutes=older_than_minutes
            )
        ).isoformat()

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE crawl_urls
                SET
                    legal_backfill_status =
                        'retry',

                    legal_backfill_next_at = ?,

                    legal_backfill_last_error =
                        'Released stale backfill lock',

                    legal_backfill_locked_at =
                        NULL,

                    legal_backfill_locked_by =
                        NULL
                WHERE legal_backfill_status =
                        'processing'

                  AND legal_backfill_locked_at
                        IS NOT NULL

                  AND legal_backfill_locked_at < ?
                """,
                (
                    iso_now(),
                    threshold,
                ),
            )

            return int(cursor.rowcount)

    def _candidate_query(
        self,
        *,
        specific_url: str | None,
        force: bool,
    ) -> tuple[str, list[Any]]:
        condition = (
            self.legal_page_condition()
        )

        filters = [
            condition,
            """
            legal_backfill_status
            IN ('pending', 'retry')
            """,
        ]

        parameters: list[Any] = []

        if specific_url:
            filters.append(
                """
                (
                    url = ?
                    OR page_url = ?
                )
                """
            )

            parameters.extend(
                [
                    specific_url,
                    specific_url,
                ]
            )

        elif not force:
            filters.append(
                """
                (
                    legal_backfill_next_at
                        IS NULL
                    OR legal_backfill_next_at
                        <= ?
                )
                """
            )

            parameters.append(
                iso_now()
            )

        sql = f"""
            SELECT *
            FROM crawl_urls
            WHERE {' AND '.join(filters)}
            ORDER BY
                CASE legal_backfill_status
                    WHEN 'retry' THEN 0
                    WHEN 'pending' THEN 1
                    ELSE 2
                END,

                CASE
                    WHEN document_number
                        IS NULL
                    THEN 0
                    ELSE 1
                END,

                COALESCE(
                    legal_backfill_checked_at,
                    ''
                ) ASC,

                url ASC
        """

        return sql, parameters

    def peek_candidates(
        self,
        *,
        limit: int,
        specific_url: str | None,
        force: bool,
    ) -> list[dict[str, Any]]:
        sql, parameters = (
            self._candidate_query(
                specific_url=specific_url,
                force=force,
            )
        )

        parameters.append(limit)

        with self.connect() as connection:
            rows = connection.execute(
                sql + "\nLIMIT ?",
                parameters,
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def claim_candidates(
        self,
        *,
        limit: int,
        worker_id: str,
        specific_url: str | None,
        force: bool,
    ) -> list[dict[str, Any]]:
        sql, parameters = (
            self._candidate_query(
                specific_url=specific_url,
                force=force,
            )
        )

        parameters.append(limit)

        locked_at = iso_now()

        connection = self.connect()

        try:
            connection.execute(
                "BEGIN IMMEDIATE"
            )

            rows = connection.execute(
                sql + "\nLIMIT ?",
                parameters,
            ).fetchall()

            urls = [
                str(row["url"])
                for row in rows
            ]

            if urls:
                connection.executemany(
                    """
                    UPDATE crawl_urls
                    SET
                        legal_backfill_status =
                            'processing',

                        legal_backfill_locked_at =
                            ?,

                        legal_backfill_locked_by =
                            ?
                    WHERE url = ?
                    """,
                    [
                        (
                            locked_at,
                            worker_id,
                            url,
                        )
                        for url in urls
                    ],
                )

            connection.commit()

            return [
                dict(row)
                for row in rows
            ]

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def mark_finished(
        self,
        url: str,
        *,
        result_status: str,
    ) -> None:
        if result_status in {
            "indexed",
            "unchanged",
        }:
            backfill_status = "done"
            error_message = None

        elif result_status in {
            "skipped_empty",
            "skipped_unsupported",
        }:
            backfill_status = (
                result_status
            )

            error_message = (
                f"Backfill result: "
                f"{result_status}"
            )

        elif result_status in {
            "missing",
            "gone",
        }:
            backfill_status = (
                "skipped_missing"
            )

            error_message = (
                f"Backfill result: "
                f"{result_status}"
            )

        else:
            backfill_status = "done"
            error_message = None

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    legal_backfill_status = ?,

                    legal_backfill_attempts =
                        legal_backfill_attempts
                        + 1,

                    legal_backfill_last_error =
                        ?,

                    legal_backfill_checked_at =
                        ?,

                    legal_backfill_next_at =
                        NULL,

                    legal_backfill_locked_at =
                        NULL,

                    legal_backfill_locked_by =
                        NULL
                WHERE url = ?
                """,
                (
                    backfill_status,
                    error_message,
                    iso_now(),
                    url,
                ),
            )

    def mark_failed(
        self,
        url: str,
        *,
        error_message: str,
        retry_hours: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    legal_backfill_status =
                        'retry',

                    legal_backfill_attempts =
                        legal_backfill_attempts
                        + 1,

                    legal_backfill_last_error =
                        ?,

                    legal_backfill_checked_at =
                        ?,

                    legal_backfill_next_at =
                        ?,

                    legal_backfill_locked_at =
                        NULL,

                    legal_backfill_locked_by =
                        NULL
                WHERE url = ?
                """,
                (
                    error_message[:2000],
                    iso_now(),
                    iso_after_hours(
                        retry_hours
                    ),
                    url,
                ),
            )

    def statistics(
        self,
    ) -> dict[str, Any]:
        condition = (
            self.legal_page_condition()
        )

        with self.connect() as connection:
            total = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM crawl_urls
                WHERE {condition}
                """
            ).fetchone()["count"]

            rows = connection.execute(
                f"""
                SELECT
                    COALESCE(
                        legal_backfill_status,
                        'not_queued'
                    ) AS status,

                    COUNT(*) AS count
                FROM crawl_urls
                WHERE {condition}
                GROUP BY COALESCE(
                    legal_backfill_status,
                    'not_queued'
                )
                ORDER BY count DESC
                """
            ).fetchall()

            metadata_row = connection.execute(
                f"""
                SELECT
                    SUM(
                        CASE
                            WHEN document_number
                                IS NOT NULL
                            THEN 1
                            ELSE 0
                        END
                    ) AS numbers,

                    SUM(
                        CASE
                            WHEN document_date
                                IS NOT NULL
                            THEN 1
                            ELSE 0
                        END
                    ) AS dates,

                    SUM(
                        CASE
                            WHEN document_kind
                                IS NOT NULL
                            THEN 1
                            ELSE 0
                        END
                    ) AS kinds
                FROM crawl_urls
                WHERE {condition}
                """
            ).fetchone()

        return {
            "total_legal_pages": int(
                total or 0
            ),

            "statuses": {
                str(row["status"]): int(
                    row["count"]
                )
                for row in rows
            },

            "metadata": {
                "document_number": int(
                    metadata_row["numbers"]
                    or 0
                ),

                "document_date": int(
                    metadata_row["dates"]
                    or 0
                ),

                "document_kind": int(
                    metadata_row["kinds"]
                    or 0
                ),
            },
        }
