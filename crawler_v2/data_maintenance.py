from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .url_policy import (
    is_archive_url,
    is_legacy_detail_url,
    is_unsupported_office_url,
)


MIGRATION_NAME = "package-2.3.0-data-maintenance"

LOADED_STATUSES = {
    "indexed",
    "indexed_existing",
    "unchanged",
}

SAFE_TERMINAL_SOURCE_STATUSES = {
    "gone",
    "quarantined",
    "skipped_corrupt",
    "skipped_unsupported",
}

NEWS_PATH = re.compile(
    r"^/press-sluzhba/novosti/\d+/\d+/?$",
    re.IGNORECASE,
)

ANNOUNCEMENT_PATH = re.compile(
    r"^/press-sluzhba/obyavleniya/\d+/?$",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def row_was_indexed(row: dict[str, Any]) -> bool:
    if str(row.get("status") or "") in LOADED_STATUSES:
        return True

    if int(row.get("indexed_chunks") or 0) > 0:
        return True

    return bool(str(row.get("last_indexed_at") or "").strip())


def select_crawl_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    columns = table_columns(connection, "crawl_urls")
    required = {"url", "status", "is_active"}

    if not required.issubset(columns):
        missing = ", ".join(sorted(required - columns))
        raise RuntimeError(
            "В crawl_urls отсутствуют обязательные поля: "
            + missing
        )

    selected = ["url", "status", "is_active"]

    for optional in (
        "http_status",
        "indexed_chunks",
        "last_indexed_at",
    ):
        if optional in columns:
            selected.append(optional)

    query = "SELECT " + ", ".join(selected) + " FROM crawl_urls"

    return [
        dict(row)
        for row in connection.execute(query).fetchall()
    ]


def update_crawl_terminal(
    connection: sqlite3.Connection,
    urls: list[str],
    *,
    status: str,
    error: str,
    legacy: bool,
) -> int:
    if not urls:
        return 0

    columns = table_columns(connection, "crawl_urls")
    assignments = [
        "status = ?",
        "is_active = 0",
    ]

    if "next_check_at" in columns:
        assignments.append("next_check_at = NULL")
    if "locked_at" in columns:
        assignments.append("locked_at = NULL")
    if "locked_by" in columns:
        assignments.append("locked_by = NULL")
    if "last_error" in columns:
        assignments.append("last_error = ?")
    if legacy and "http_status" in columns:
        assignments.append("http_status = COALESCE(http_status, 404)")
    if legacy and "consecutive_missing" in columns:
        assignments.append(
            "consecutive_missing = "
            "MAX(COALESCE(consecutive_missing, 0), 3)"
        )

    sql = (
        "UPDATE crawl_urls SET "
        + ", ".join(assignments)
        + " WHERE url = ?"
    )

    values = []

    for url in urls:
        parameters: list[Any] = [status]

        if "last_error" in columns:
            parameters.append(error)

        parameters.append(url)
        values.append(tuple(parameters))

    connection.executemany(sql, values)
    return len(urls)


def close_legacy_urls(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    close_urls: list[str] = []
    review_urls: list[str] = []
    by_path: dict[str, int] = {}

    for row in select_crawl_rows(connection):
        url = str(row["url"] or "")

        if not is_legacy_detail_url(url):
            continue

        if (
            int(row.get("is_active") or 0) == 0
            and str(row.get("status") or "") == "gone"
        ):
            continue

        if row_was_indexed(row):
            review_urls.append(url)
            continue

        close_urls.append(url)
        path = urlparse(url).path.rstrip("/").casefold()
        by_path[path] = by_path.get(path, 0) + 1

    updated = update_crawl_terminal(
        connection,
        close_urls,
        status="gone",
        error="Package 2.3: терминальная политика legacy URL",
        legacy=True,
    )

    return {
        "closed": updated,
        "review": len(review_urls),
        "review_samples": review_urls[:10],
        "closed_by_path": by_path,
    }


def finalize_unsupported_formats(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    close_urls: list[str] = []
    review_urls: list[str] = []
    archive_count = 0
    office_count = 0

    for row in select_crawl_rows(connection):
        url = str(row["url"] or "")
        archive = is_archive_url(url)
        old_office = is_unsupported_office_url(url)

        if not archive and not old_office:
            continue

        if int(row.get("is_active") or 0) == 0:
            continue

        if row_was_indexed(row):
            review_urls.append(url)
            continue

        close_urls.append(url)

        if archive:
            archive_count += 1
        else:
            office_count += 1

    updated = update_crawl_terminal(
        connection,
        close_urls,
        status="skipped_unsupported",
        error=(
            "Package 2.3: архивный или устаревший офисный "
            "формат не индексируется"
        ),
        legacy=False,
    )

    return {
        "finalized": updated,
        "archives": archive_count,
        "old_office": office_count,
        "review": len(review_urls),
        "review_samples": review_urls[:10],
    }


def supported_cleanup_url(url: str) -> bool:
    path = urlparse(str(url or "")).path
    return bool(
        NEWS_PATH.fullmatch(path)
        or ANNOUNCEMENT_PATH.fullmatch(path)
    )


def finalize_cleanup_queue(
    connection: sqlite3.Connection,
    now: str,
) -> int:
    if not table_exists(connection, "content_cleanup_queue"):
        return 0

    rows = connection.execute(
        """
        SELECT url
        FROM content_cleanup_queue
        WHERE status IN ('pending', 'processing', 'retry')
        """
    ).fetchall()

    urls = [
        str(row["url"])
        for row in rows
        if not supported_cleanup_url(str(row["url"]))
    ]

    connection.executemany(
        """
        UPDATE content_cleanup_queue
        SET
            status = 'skipped_out_of_scope',
            last_error = (
                'Package 2.3: вне области одноразовой cleanup-задачи'
            ),
            locked_at = NULL,
            locked_by = NULL,
            updated_at = ?
        WHERE url = ?
          AND status IN ('pending', 'processing', 'retry')
        """,
        [(now, url) for url in urls],
    )

    return len(urls)


def reconcile_publication_queue(
    connection: sqlite3.Connection,
    now: str,
) -> dict[str, int]:
    if not table_exists(connection, "publication_date_queue"):
        return {"terminal": 0, "requeued": 0}

    terminal = connection.execute(
        """
        UPDATE publication_date_queue
        SET
            status = 'skipped',
            locked_at = NULL,
            locked_by = NULL,
            last_error = (
                'Package 2.3: основной crawler закрыл URL'
            ),
            updated_at = ?
        WHERE status IN (
            'pending', 'processing', 'retry',
            'not_indexed', 'failed'
        )
          AND EXISTS (
              SELECT 1
              FROM crawl_urls
              WHERE crawl_urls.url = publication_date_queue.url
                AND (
                    COALESCE(crawl_urls.is_active, 1) = 0
                    OR crawl_urls.status IN (
                        'gone', 'quarantined',
                        'skipped_corrupt', 'skipped_unsupported'
                    )
                )
          )
        """,
        (now,),
    ).rowcount

    http_terminal = connection.execute(
        """
        UPDATE publication_date_queue
        SET
            status = 'skipped',
            locked_at = NULL,
            locked_by = NULL,
            last_error = (
                'Package 2.3: терминальный HTTP '
                || COALESCE(last_http_status, 404)
            ),
            updated_at = ?
        WHERE status IN ('retry', 'failed')
          AND last_http_status IN (404, 410)
        """,
        (now,),
    ).rowcount

    requeued = connection.execute(
        """
        UPDATE publication_date_queue
        SET
            status = 'pending',
            attempts = 0,
            locked_at = NULL,
            locked_by = NULL,
            last_error = NULL,
            updated_at = ?
        WHERE status = 'not_indexed'
          AND EXISTS (
              SELECT 1
              FROM crawl_urls
              WHERE crawl_urls.url = publication_date_queue.url
                AND crawl_urls.status IN (
                    'indexed', 'indexed_existing', 'unchanged'
                )
          )
        """,
        (now,),
    ).rowcount

    return {
        "terminal": int(terminal) + int(http_terminal),
        "requeued": int(requeued),
    }


def ensure_migration_log(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS package_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            details_json TEXT NOT NULL
        )
        """
    )


def run_migration(
    database_path: Path,
    *,
    require_no_review: bool = False,
) -> dict[str, Any]:
    if not database_path.is_file():
        raise RuntimeError(
            f"SQLite не найдена: {database_path}"
        )

    connection = connect_database(database_path)

    try:
        connection.execute("BEGIN IMMEDIATE")

        if not table_exists(connection, "crawl_urls"):
            raise RuntimeError("Таблица crawl_urls не найдена")

        now = utc_now()
        ensure_migration_log(connection)
        legacy = close_legacy_urls(connection)
        formats = finalize_unsupported_formats(connection)
        cleanup = finalize_cleanup_queue(connection, now)
        publication = reconcile_publication_queue(connection, now)

        review_total = int(legacy["review"]) + int(formats["review"])

        if require_no_review and review_total:
            raise RuntimeError(
                "Обнаружены ранее индексированные legacy/unsupported URL; "
                "требуется безопасное удаление из Elasticsearch"
            )

        quick_check = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        )

        if quick_check != "ok":
            raise RuntimeError(
                f"PRAGMA quick_check вернул: {quick_check}"
            )

        result = {
            "migration": MIGRATION_NAME,
            "applied_at": now,
            "legacy": legacy,
            "formats": formats,
            "cleanup_finalized": cleanup,
            "publication": publication,
            "review_total": review_total,
            "sqlite_quick_check": quick_check,
        }

        connection.execute(
            """
            INSERT INTO package_migrations (
                name,
                applied_at,
                details_json
            )
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                applied_at = excluded.applied_at,
                details_json = excluded.details_json
            """,
            (
                MIGRATION_NAME,
                now,
                json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )

        connection.commit()
        return result

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Транзакционная миграция данных Пакета 2.3",
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Подтвердить изменение SQLite",
    )
    parser.add_argument(
        "--require-no-review",
        action="store_true",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    if not arguments.apply:
        raise SystemExit("Для изменения данных требуется --apply")

    result = run_migration(
        arguments.database,
        require_no_review=arguments.require_no_review,
    )

    print("DATA_MIGRATION=OK")
    print("LEGACY_CLOSED=" + str(result["legacy"]["closed"]))
    print("LEGACY_REVIEW=" + str(result["legacy"]["review"]))
    print("UNSUPPORTED_FINALIZED=" + str(result["formats"]["finalized"]))
    print("CLEANUP_FINALIZED=" + str(result["cleanup_finalized"]))
    print("PUBLICATION_REQUEUED=" + str(result["publication"]["requeued"]))
    print("PUBLICATION_TERMINAL=" + str(result["publication"]["terminal"]))
    print("SQLITE_QUICK_CHECK=" + str(result["sqlite_quick_check"]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
