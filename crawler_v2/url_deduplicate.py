from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import settings
from .es_writer import ElasticsearchWriter
from .fetcher import HttpFetcher
from .incremental_db import IncrementalStateDatabase
from .incremental_worker import process_row
from .legal_db import LegalMetadataDatabase


SAFE_PROCESS_RESULTS = {
    "indexed",
    "unchanged",
}


def iso_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        settings.state_db_path,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    connection.execute(
        "PRAGMA journal_mode=WAL"
    )

    connection.execute(
        "PRAGMA busy_timeout=30000"
    )

    connection.execute(
        "PRAGMA foreign_keys=ON"
    )

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


def available_columns(
    connection: sqlite3.Connection,
) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(crawl_urls)"
        ).fetchall()
    }


def load_verified_groups(
    report_path: Path,
) -> list[dict[str, Any]]:
    report = json.loads(
        report_path.read_text(
            encoding="utf-8"
        )
    )

    groups = report.get("groups", [])

    if not groups:
        raise RuntimeError(
            "В отчёте нет групп дублей"
        )

    verified_groups = []

    for group in groups:
        canonical_url = str(
            group.get("canonical_url")
            or ""
        ).strip()

        files = group.get("files", [])

        if not canonical_url:
            raise RuntimeError(
                "В группе отсутствует canonical_url"
            )

        if not group.get(
            "all_current_files_equal"
        ):
            raise RuntimeError(
                "Файлы группы не подтверждены "
                f"как одинаковые: {canonical_url}"
            )

        urls = list(
            dict.fromkeys(
                str(item.get("url") or "").strip()
                for item in files
                if str(
                    item.get("url") or ""
                ).strip()
            )
        )

        if canonical_url not in urls:
            urls.append(canonical_url)

        duplicate_urls = [
            url
            for url in urls
            if url != canonical_url
        ]

        if not duplicate_urls:
            continue

        verified_groups.append(
            {
                "canonical_url": canonical_url,
                "duplicate_urls": duplicate_urls,
                "files": files,
            }
        )

    if not verified_groups:
        raise RuntimeError(
            "Нет подтверждённых дублей "
            "для обработки"
        )

    return verified_groups


def inspect_group(
    connection: sqlite3.Connection,
    group: dict[str, Any],
) -> dict[str, Any]:
    urls = [
        group["canonical_url"],
        *group["duplicate_urls"],
    ]

    placeholders = ",".join(
        "?"
        for _ in urls
    )

    rows = connection.execute(
        f"""
        SELECT
            rowid AS sqlite_rowid,
            *
        FROM crawl_urls
        WHERE url IN ({placeholders})
        ORDER BY url
        """,
        urls,
    ).fetchall()

    attachment_links = []

    if table_exists(
        connection,
        "attachment_links",
    ):
        attachment_links = (
            connection.execute(
                f"""
                SELECT *
                FROM attachment_links
                WHERE attachment_url
                        IN ({placeholders})
                   OR parent_url
                        IN ({placeholders})
                ORDER BY
                    attachment_url,
                    parent_url
                """,
                [
                    *urls,
                    *urls,
                ],
            ).fetchall()
        )

    return {
        "canonical_url": (
            group["canonical_url"]
        ),
        "duplicate_urls": (
            group["duplicate_urls"]
        ),
        "crawl_rows": [
            dict(row)
            for row in rows
        ],
        "attachment_links": [
            dict(row)
            for row in attachment_links
        ],
    }


def print_plan(
    plans: list[dict[str, Any]],
) -> None:
    print("[PLAN]")

    for number, plan in enumerate(
        plans,
        start=1,
    ):
        print()
        print("=" * 100)
        print(
            f"Группа #{number}:",
            plan["canonical_url"],
        )

        print(
            "Удаляемые варианты:",
            len(plan["duplicate_urls"]),
        )

        for url in plan["duplicate_urls"]:
            print("  -", url)

        print(
            "Строк crawl_urls:",
            len(plan["crawl_rows"]),
        )

        for row in plan["crawl_rows"]:
            print(
                "  ",
                row.get("sqlite_rowid"),
                row.get("status"),
                row.get("url"),
            )

        print(
            "Связей attachment_links:",
            len(plan["attachment_links"]),
        )


def copy_missing_metadata(
    connection: sqlite3.Connection,
    *,
    canonical_url: str,
    duplicate_url: str,
    columns: set[str],
) -> None:
    candidate_fields = [
        "source",
        "doc_type",
        "content_type",
        "page_url",
        "parent_url",
        "attachment_title",
        "document_number",
        "document_date",
        "document_kind",
        "etag",
        "last_modified",
        "first_seen_at",
        "last_seen_at",
    ]

    fields = [
        field
        for field in candidate_fields
        if field in columns
    ]

    for field in fields:
        connection.execute(
            f"""
            UPDATE crawl_urls
            SET {field} = COALESCE(
                NULLIF({field}, ''),
                (
                    SELECT NULLIF({field}, '')
                    FROM crawl_urls
                    WHERE url = ?
                )
            )
            WHERE url = ?
            """,
            (
                duplicate_url,
                canonical_url,
            ),
        )


def merge_attachment_links(
    connection: sqlite3.Connection,
    *,
    canonical_url: str,
    duplicate_url: str,
) -> None:
    if not table_exists(
        connection,
        "attachment_links",
    ):
        return

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
        SELECT
            parent_url,
            ?,
            attachment_title,
            source,
            first_seen_at,
            last_seen_at,
            is_active
        FROM attachment_links
        WHERE attachment_url = ?

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

            first_seen_at = CASE
                WHEN excluded.first_seen_at
                     < attachment_links.first_seen_at
                THEN excluded.first_seen_at
                ELSE attachment_links.first_seen_at
            END,

            last_seen_at = CASE
                WHEN excluded.last_seen_at
                     > attachment_links.last_seen_at
                THEN excluded.last_seen_at
                ELSE attachment_links.last_seen_at
            END,

            is_active = CASE
                WHEN excluded.is_active = 1
                THEN 1
                ELSE attachment_links.is_active
            END
        """,
        (
            canonical_url,
            duplicate_url,
        ),
    )

    connection.execute(
        """
        DELETE FROM attachment_links
        WHERE attachment_url = ?
        """,
        (duplicate_url,),
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
        SELECT
            ?,
            attachment_url,
            attachment_title,
            source,
            first_seen_at,
            last_seen_at,
            is_active
        FROM attachment_links
        WHERE parent_url = ?

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
                WHEN excluded.last_seen_at
                     > attachment_links.last_seen_at
                THEN excluded.last_seen_at
                ELSE attachment_links.last_seen_at
            END,

            is_active = CASE
                WHEN excluded.is_active = 1
                THEN 1
                ELSE attachment_links.is_active
            END
        """,
        (
            canonical_url,
            duplicate_url,
        ),
    )

    connection.execute(
        """
        DELETE FROM attachment_links
        WHERE parent_url = ?
        """,
        (duplicate_url,),
    )


def merge_sqlite_group(
    group: dict[str, Any],
) -> dict[str, Any]:
    canonical_url = group["canonical_url"]
    duplicate_urls = group["duplicate_urls"]

    connection = connect_database()

    try:
        columns = available_columns(
            connection
        )

        canonical = connection.execute(
            """
            SELECT *
            FROM crawl_urls
            WHERE url = ?
            """,
            (canonical_url,),
        ).fetchone()

        if canonical is None:
            raise RuntimeError(
                "Каноническая строка отсутствует: "
                f"{canonical_url}"
            )

        connection.execute(
            "BEGIN IMMEDIATE"
        )

        for duplicate_url in duplicate_urls:
            duplicate = connection.execute(
                """
                SELECT *
                FROM crawl_urls
                WHERE url = ?
                """,
                (duplicate_url,),
            ).fetchone()

            if duplicate is None:
                print(
                    "[WARN] Строка уже отсутствует:",
                    duplicate_url,
                )
                continue

            copy_missing_metadata(
                connection,
                canonical_url=canonical_url,
                duplicate_url=duplicate_url,
                columns=columns,
            )

            merge_attachment_links(
                connection,
                canonical_url=canonical_url,
                duplicate_url=duplicate_url,
            )

            for field in (
                "file_url",
                "page_url",
                "parent_url",
            ):
                if field not in columns:
                    continue

                connection.execute(
                    f"""
                    UPDATE crawl_urls
                    SET {field} = ?
                    WHERE {field} = ?
                    """,
                    (
                        canonical_url,
                        duplicate_url,
                    ),
                )

            connection.execute(
                """
                DELETE FROM crawl_urls
                WHERE url = ?
                """,
                (duplicate_url,),
            )

        assignments = [
            "status = 'discovered'",
            "content_hash = NULL",
            "indexed_chunks = 0",
            "next_check_at = ?",
            "locked_at = NULL",
            "locked_by = NULL",
            "last_error = NULL",
        ]

        if "file_url" in columns:
            assignments.append(
                "file_url = ?"
            )

        parameters: list[Any] = [
            iso_now(),
        ]

        if "file_url" in columns:
            parameters.append(
                canonical_url
            )

        parameters.append(
            canonical_url
        )

        connection.execute(
            f"""
            UPDATE crawl_urls
            SET {", ".join(assignments)}
            WHERE url = ?
            """,
            parameters,
        )

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM crawl_urls
            WHERE url = ?
            """,
            (canonical_url,),
        ).fetchone()

        if row is None:
            raise RuntimeError(
                "Каноническая строка исчезла: "
                f"{canonical_url}"
            )

        return dict(row)

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def reindex_canonical(
    row: dict[str, Any],
) -> str:
    crawl_database = (
        IncrementalStateDatabase(
            settings.state_db_path
        )
    )

    crawl_database.migrate()

    legal_database = (
        LegalMetadataDatabase(
            settings.state_db_path
        )
    )

    legal_database.migrate()

    result = process_row(
        row,
        database=crawl_database,
        legal_database=legal_database,
        fetcher=HttpFetcher(),
        writer=ElasticsearchWriter(),
        dry_run=False,
    )

    return str(result)


def delete_old_es_documents(
    duplicate_urls: list[str],
) -> dict[str, Any]:
    es_url = os.getenv(
        "ELASTICSEARCH_URL",
        os.getenv(
            "ES_URL",
            "http://127.0.0.1:9200",
        ),
    ).rstrip("/")

    index_name = os.getenv(
        "ES_INDEX",
        "sochi_search",
    )

    response = requests.post(
        (
            f"{es_url}/{index_name}/"
            "_delete_by_query"
        ),
        params={
            "conflicts": "proceed",
            "refresh": "true",
        },
        json={
            "query": {
                "bool": {
                    "should": [
                        {
                            "terms": {
                                "url": duplicate_urls,
                            }
                        },
                        {
                            "terms": {
                                "file_url": (
                                    duplicate_urls
                                ),
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            }
        },
        timeout=120,
    )

    response.raise_for_status()

    return response.json()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Точечное объединение "
            "подтверждённых URL-дублей"
        )
    )

    parser.add_argument(
        "verification_report",
        help=(
            "JSON отчёт "
            "url_duplicate_verify"
        ),
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Применить изменения. "
            "Без флага работает только план."
        ),
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    report_path = Path(
        arguments.verification_report
    )

    if not report_path.is_file():
        print(
            "[ERROR] Отчёт не найден:",
            report_path,
            file=sys.stderr,
        )
        return 2

    try:
        groups = load_verified_groups(
            report_path
        )
    except Exception as exc:
        print(
            "[STOP]",
            exc,
            file=sys.stderr,
        )
        return 1

    connection = connect_database()

    try:
        plans = [
            inspect_group(
                connection,
                group,
            )
            for group in groups
        ]
    finally:
        connection.close()

    print_plan(plans)

    if not arguments.apply:
        print()
        print(
            "[DRY RUN] SQLite и "
            "Elasticsearch не изменены"
        )
        print(
            "[READY] Подтверждённых групп:",
            len(groups),
        )
        return 0

    results = []

    for group in groups:
        canonical_url = (
            group["canonical_url"]
        )

        print()
        print("=" * 100)
        print("[APPLY]", canonical_url)

        canonical_row = merge_sqlite_group(
            group
        )

        process_result = reindex_canonical(
            canonical_row
        )

        if process_result not in (
            SAFE_PROCESS_RESULTS
        ):
            raise RuntimeError(
                "Канонический документ "
                "не переиндексирован: "
                f"{canonical_url}; "
                f"result={process_result}"
            )

        delete_result = (
            delete_old_es_documents(
                group["duplicate_urls"]
            )
        )

        result = {
            "canonical_url": canonical_url,
            "removed_urls": (
                group["duplicate_urls"]
            ),
            "process_result": (
                process_result
            ),
            "es_deleted": int(
                delete_result.get(
                    "deleted",
                    0,
                )
            ),
        }

        results.append(result)

        print(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2,
            )
        )

    print()
    print("[FINISH]")

    print(
        json.dumps(
            results,
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
