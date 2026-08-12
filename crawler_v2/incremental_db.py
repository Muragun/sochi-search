from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


MIGRATION_COLUMNS = {
    "last_checked_at": "TEXT",
    "next_check_at": "TEXT",
    "locked_at": "TEXT",
    "locked_by": "TEXT",
    "indexed_chunks": "INTEGER NOT NULL DEFAULT 0",
    "consecutive_missing": "INTEGER NOT NULL DEFAULT 0",
    "extraction_version": "TEXT",
    "source_hash": "TEXT",
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


def candidate_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)

    for key in (
        "_queue_status_rank",
        "_queue_type_rank",
        "_queue_format_rank",
    ):
        result.pop(key, None)

    return result


class IncrementalStateDatabase:
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
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")

        return connection

    def migrate(self) -> None:
        with self.connect() as connection:
            table = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'crawl_urls'
                """
            ).fetchone()

            if table is None:
                raise RuntimeError(
                    "Таблица crawl_urls не найдена. "
                    "Сначала выполните этап 9."
                )

            existing_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(crawl_urls)"
                ).fetchall()
            }

            for column_name, definition in (
                MIGRATION_COLUMNS.items()
            ):
                if column_name in existing_columns:
                    continue

                connection.execute(
                    f"""
                    ALTER TABLE crawl_urls
                    ADD COLUMN {column_name} {definition}
                    """
                )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_crawl_urls_next_check
                ON crawl_urls(next_check_at)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_crawl_urls_locked_at
                ON crawl_urls(locked_at)
                """
            )

    def release_stale_locks(
        self,
        *,
        older_than_minutes: int,
    ) -> int:
        threshold = (
            utc_now()
            - timedelta(minutes=older_than_minutes)
        ).isoformat()

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = 'retry',
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error = 'Released stale worker lock',
                    next_check_at = ?
                WHERE status = 'processing'
                  AND locked_at IS NOT NULL
                  AND locked_at < ?
                """,
                (
                    iso_now(),
                    threshold,
                ),
            )

            return int(cursor.rowcount)

    def processing_lock_owners(
        self,
    ) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT locked_by
                FROM crawl_urls
                WHERE status = 'processing'
                  AND locked_by IS NOT NULL
                  AND TRIM(locked_by) != ''
                ORDER BY locked_by
                """
            ).fetchall()

        return tuple(
            str(row["locked_by"])
            for row in rows
        )

    def release_worker_locks(
        self,
        worker_ids: tuple[str, ...],
        *,
        reason: str,
    ) -> int:
        normalized = tuple(
            dict.fromkeys(
                value.strip()
                for value in worker_ids
                if value.strip()
            )
        )

        if not normalized:
            return 0

        placeholders = ", ".join(
            "?"
            for _value in normalized
        )

        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE crawl_urls
                SET
                    status = 'retry',
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error = ?,
                    next_check_at = ?
                WHERE status = 'processing'
                  AND locked_by IN ({placeholders})
                """,
                (
                    reason[:2000],
                    iso_now(),
                    *normalized,
                ),
            )

            return int(cursor.rowcount)

    def release_all_processing(
        self,
        *,
        reason: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = 'retry',
                    locked_at = NULL,
                    locked_by = NULL,
                    last_error = ?,
                    next_check_at = ?
                WHERE status = 'processing'
                """,
                (
                    reason[:2000],
                    iso_now(),
                ),
            )

            return int(cursor.rowcount)

    def _candidate_query(
        self,
        *,
        specific_url: str | None,
        exact_url: bool = False,
        force: bool,
        required_extraction_version: str | None = None,
        require_existing_index: bool = False,
    ) -> tuple[str, list[Any]]:
        conditions = [
            "is_active = 1",
            "status != 'processing'",
        ]

        parameters: list[Any] = []

        if required_extraction_version:
            conditions.extend(
                [
                    "LOWER(COALESCE(doc_type, '')) = 'pdf'",
                    (
                        "COALESCE(extraction_version, '') != ?"
                    ),
                ]
            )
            parameters.append(
                required_extraction_version
            )

        if require_existing_index:
            conditions.append(
                """
                (
                    COALESCE(indexed_chunks, 0) > 0
                    OR status IN (
                        'indexed',
                        'indexed_existing',
                        'unchanged'
                    )
                )
                """
            )

        if specific_url:
            if exact_url:
                conditions.append(
                    "url = ?"
                )
                parameters.append(
                    specific_url
                )
            else:
                conditions.append(
                    """
                    (
                        url = ?
                        OR page_url = ?
                        OR file_url = ?
                    )
                    """
                )

                parameters.extend(
                    [
                        specific_url,
                        specific_url,
                        specific_url,
                    ]
                )

        elif not force:
            conditions.append(
                """
                (
                    next_check_at IS NULL
                    OR next_check_at <= ?
                )
                """
            )

            parameters.append(iso_now())

        type_rank = """
            CASE LOWER(COALESCE(doc_type, ''))
                WHEN 'page' THEN 0
                WHEN 'pdf' THEN 1
                WHEN 'file' THEN 2
                ELSE 3
            END
        """

        sql = f"""
            SELECT *
            FROM (
                SELECT
                    crawl_urls.*,
                    (
                        SELECT parent.document_number
                        FROM crawl_urls AS parent
                        WHERE parent.url = crawl_urls.parent_url
                        LIMIT 1
                    ) AS _parent_document_number,
                    (
                        SELECT parent.document_date
                        FROM crawl_urls AS parent
                        WHERE parent.url = crawl_urls.parent_url
                        LIMIT 1
                    ) AS _parent_document_date,
                    (
                        SELECT parent.document_kind
                        FROM crawl_urls AS parent
                        WHERE parent.url = crawl_urls.parent_url
                        LIMIT 1
                    ) AS _parent_document_kind,
                    CASE status
                        WHEN 'discovered' THEN 0
                        WHEN 'indexed_existing' THEN 1
                        WHEN 'retry' THEN 2
                        WHEN 'missing' THEN 3
                        WHEN 'indexed' THEN 4
                        WHEN 'unchanged' THEN 5
                        WHEN 'skipped_empty' THEN 6
                        ELSE 7
                    END AS _queue_status_rank,
                    {type_rank} AS _queue_type_rank,
                    ROW_NUMBER() OVER (
                        PARTITION BY status, {type_rank}
                        ORDER BY
                            COALESCE(next_check_at, '') ASC,
                            COALESCE(last_checked_at, '') ASC,
                            url ASC
                    ) AS _queue_format_rank
                FROM crawl_urls
                WHERE {' AND '.join(conditions)}
            ) AS ranked_candidates
            ORDER BY
                _queue_status_rank ASC,
                _queue_format_rank ASC,
                _queue_type_rank ASC,
                COALESCE(next_check_at, '') ASC,
                COALESCE(last_checked_at, '') ASC,
                url ASC
        """

        return sql, parameters

    def peek_candidates(
        self,
        *,
        limit: int,
        specific_url: str | None,
        force: bool,
        required_extraction_version: str | None = None,
        require_existing_index: bool = False,
    ) -> list[dict[str, Any]]:
        sql, parameters = self._candidate_query(
            specific_url=specific_url,
            force=force,
            required_extraction_version=(
                required_extraction_version
            ),
            require_existing_index=(
                require_existing_index
            ),
        )

        parameters.append(limit)

        with self.connect() as connection:
            rows = connection.execute(
                sql + "\nLIMIT ?",
                parameters,
            ).fetchall()

        return [candidate_dict(row) for row in rows]

    def claim_candidates(
        self,
        *,
        limit: int,
        worker_id: str,
        specific_url: str | None,
        exact_url: bool = False,
        force: bool,
        required_extraction_version: str | None = None,
        require_existing_index: bool = False,
    ) -> list[dict[str, Any]]:
        sql, parameters = self._candidate_query(
            specific_url=specific_url,
            exact_url=exact_url,
            force=force,
            required_extraction_version=(
                required_extraction_version
            ),
            require_existing_index=(
                require_existing_index
            ),
        )

        parameters.append(limit)
        locked_at = iso_now()

        connection = self.connect()

        try:
            connection.execute("BEGIN IMMEDIATE")

            rows = connection.execute(
                sql + "\nLIMIT ?",
                parameters,
            ).fetchall()

            urls = [
                row["url"]
                for row in rows
            ]

            if urls:
                connection.executemany(
                    """
                    UPDATE crawl_urls
                    SET
                        status = 'processing',
                        locked_at = ?,
                        locked_by = ?
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

            return [candidate_dict(row) for row in rows]

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    def mark_not_modified(
        self,
        url: str,
        *,
        checked_at: str,
        next_check_at: str,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = 'unchanged',
                    http_status = 304,
                    etag = COALESCE(?, etag),
                    last_modified = COALESCE(
                        ?,
                        last_modified
                    ),
                    attempt_count = attempt_count + 1,
                    failure_count = 0,
                    consecutive_missing = 0,
                    last_error = NULL,
                    last_checked_at = ?,
                    next_check_at = ?,
                    locked_at = NULL,
                    locked_by = NULL
                WHERE url = ?
                """,
                (
                    etag,
                    last_modified,
                    checked_at,
                    next_check_at,
                    url,
                ),
            )

    def mark_source_unchanged(
        self,
        url: str,
        *,
        checked_at: str,
        next_check_at: str,
        fetched_at: str,
        source_hash: str,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        """Обновляет проверку PDF, не заменяя уже готовые OCR-фрагменты."""

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = 'unchanged',
                    http_status = 200,
                    source_hash = ?,
                    etag = COALESCE(?, etag),
                    last_modified = COALESCE(
                        ?,
                        last_modified
                    ),
                    attempt_count = attempt_count + 1,
                    failure_count = 0,
                    consecutive_missing = 0,
                    last_error = NULL,
                    last_checked_at = ?,
                    next_check_at = ?,
                    last_fetched_at = ?,
                    locked_at = NULL,
                    locked_by = NULL
                WHERE url = ?
                """,
                (
                    source_hash,
                    etag,
                    last_modified,
                    checked_at,
                    next_check_at,
                    fetched_at,
                    url,
                ),
            )

    def mark_unchanged(
        self,
        url: str,
        *,
        checked_at: str,
        next_check_at: str,
        fetched_at: str,
        content_hash: str,
        content_type: str,
        doc_type: str,
        etag: str | None,
        last_modified: str | None,
        extraction_version: str | None = None,
        source_hash: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = 'unchanged',
                    http_status = 200,
                    content_hash = ?,
                    content_type = ?,
                    doc_type = ?,
                    etag = ?,
                    last_modified = ?,
                    extraction_version = COALESCE(
                        ?,
                        extraction_version
                    ),
                    source_hash = COALESCE(
                        ?,
                        source_hash
                    ),
                    attempt_count = attempt_count + 1,
                    failure_count = 0,
                    consecutive_missing = 0,
                    last_error = NULL,
                    last_checked_at = ?,
                    next_check_at = ?,
                    last_fetched_at = ?,
                    locked_at = NULL,
                    locked_by = NULL
                WHERE url = ?
                """,
                (
                    content_hash,
                    content_type,
                    doc_type,
                    etag,
                    last_modified,
                    extraction_version,
                    source_hash,
                    checked_at,
                    next_check_at,
                    fetched_at,
                    url,
                ),
            )

    def mark_indexed(
        self,
        url: str,
        *,
        checked_at: str,
        next_check_at: str,
        fetched_at: str,
        indexed_at: str,
        content_hash: str,
        content_type: str,
        doc_type: str,
        indexed_chunks: int,
        etag: str | None,
        last_modified: str | None,
        final_url: str,
        extraction_version: str | None = None,
        source_hash: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = 'indexed',
                    http_status = 200,
                    content_hash = ?,
                    content_type = ?,
                    doc_type = ?,
                    etag = ?,
                    last_modified = ?,
                    extraction_version = COALESCE(
                        ?,
                        extraction_version
                    ),
                    source_hash = COALESCE(
                        ?,
                        source_hash
                    ),
                    attempt_count = attempt_count + 1,
                    failure_count = 0,
                    consecutive_missing = 0,
                    last_error = NULL,
                    last_checked_at = ?,
                    next_check_at = ?,
                    last_fetched_at = ?,
                    last_indexed_at = ?,
                    indexed_chunks = ?,
                    last_seen_at = ?,
                    locked_at = NULL,
                    locked_by = NULL
                WHERE url = ?
                """,
                (
                    content_hash,
                    content_type,
                    doc_type,
                    etag,
                    last_modified,
                    extraction_version,
                    source_hash,
                    checked_at,
                    next_check_at,
                    fetched_at,
                    indexed_at,
                    indexed_chunks,
                    checked_at,
                    url,
                ),
            )

    def mark_missing(
        self,
        url: str,
        *,
        http_status: int,
        checked_at: str,
        next_check_at: str,
        deleted_from_index: bool,
    ) -> None:
        status = (
            "gone"
            if deleted_from_index
            else "missing"
        )

        is_active = (
            0
            if deleted_from_index
            else 1
        )

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = ?,
                    http_status = ?,
                    attempt_count = attempt_count + 1,
                    failure_count = failure_count + 1,
                    consecutive_missing =
                        consecutive_missing + 1,
                    last_error = ?,
                    last_checked_at = ?,
                    next_check_at = ?,
                    is_active = ?,
                    locked_at = NULL,
                    locked_by = NULL
                WHERE url = ?
                """,
                (
                    status,
                    http_status,
                    f"HTTP {http_status}",
                    checked_at,
                    next_check_at,
                    is_active,
                    url,
                ),
            )

    def mark_skipped(
        self,
        url: str,
        *,
        status: str,
        checked_at: str,
        next_check_at: str,
        error_message: str,
        http_status: int = 200,
    ) -> None:
        allowed_statuses = {
            "skipped_corrupt",
            "skipped_empty",
            "skipped_unsupported",
        }

        if status not in allowed_statuses:
            raise ValueError(
                f"Недопустимый статус пропуска: {status}"
            )

        terminal = status in {
            "skipped_corrupt",
            "skipped_unsupported",
        }

        stored_next_check_at = (
            None
            if terminal
            else next_check_at
        )

        is_active = 0 if terminal else 1

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = ?,
                    http_status = ?,
                    attempt_count = attempt_count + 1,
                    failure_count = 0,
                    last_error = ?,
                    last_checked_at = ?,
                    next_check_at = ?,
                    is_active = ?,
                    locked_at = NULL,
                    locked_by = NULL
                WHERE url = ?
                """,
                (
                    status,
                    http_status,
                    error_message[:2000],
                    checked_at,
                    stored_next_check_at,
                    is_active,
                    url,
                ),
            )

    def mark_failed(
        self,
        url: str,
        *,
        checked_at: str,
        next_check_at: str,
        error_message: str,
        http_status: int | None = None,
        quarantine: bool = False,
    ) -> None:
        status = (
            "quarantined"
            if quarantine
            else "retry"
        )

        is_active = (
            0
            if quarantine
            else 1
        )

        stored_next_check_at = (
            None
            if quarantine
            else next_check_at
        )

        with self.connect() as connection:
            connection.execute(
                """
                UPDATE crawl_urls
                SET
                    status = ?,
                    http_status = ?,
                    attempt_count = attempt_count + 1,
                    failure_count = failure_count + 1,
                    last_error = ?,
                    last_checked_at = ?,
                    next_check_at = ?,
                    is_active = ?,
                    locked_at = NULL,
                    locked_by = NULL
                WHERE url = ?
                """,
                (
                    status,
                    http_status,
                    error_message[:2000],
                    checked_at,
                    stored_next_check_at,
                    is_active,
                    url,
                ),
            )

    def get_url(
        self,
        url: str,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM crawl_urls
                WHERE
                    url = ?
                    OR page_url = ?
                    OR file_url = ?
                LIMIT 1
                """,
                (
                    url,
                    url,
                    url,
                ),
            ).fetchone()

        return (
            dict(row)
            if row is not None
            else None
        )
