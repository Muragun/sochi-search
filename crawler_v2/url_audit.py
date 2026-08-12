from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .url_normalization import (
    normalize_project_url,
)


OPTIONAL_FIELDS = (
    "page_url",
    "file_url",
    "parent_url",
    "doc_type",
    "status",
    "content_hash",
    "http_status",
    "is_active",
    "first_seen_at",
    "last_seen_at",
    "last_checked_at",
    "document_number",
    "document_date",
    "document_kind",
    "legal_backfill_status",
)


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def open_read_only() -> sqlite3.Connection:
    database_uri = (
        f"file:{settings.state_db_path}"
        "?mode=ro"
    )

    connection = sqlite3.connect(
        database_uri,
        uri=True,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    connection.execute(
        "PRAGMA query_only=ON"
    )

    connection.execute(
        "PRAGMA busy_timeout=30000"
    )

    return connection


def available_columns(
    connection: sqlite3.Connection,
) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(crawl_urls)"
        ).fetchall()
    }


def select_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    columns = available_columns(
        connection
    )

    selected = [
        "rowid AS sqlite_rowid",
        "url",
    ]

    selected.extend(
        field
        for field in OPTIONAL_FIELDS
        if field in columns
    )

    query = f"""
        SELECT
            {", ".join(selected)}
        FROM crawl_urls
        WHERE url IS NOT NULL
          AND TRIM(url) != ''
        ORDER BY rowid
    """

    return [
        dict(row)
        for row in connection.execute(
            query
        ).fetchall()
    ]


def compact_row(
    row: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "sqlite_rowid": row.get(
            "sqlite_rowid"
        ),
        "url": row.get("url"),
    }

    for field in (
        "doc_type",
        "status",
        "content_hash",
        "parent_url",
        "page_url",
        "file_url",
        "document_number",
        "legal_backfill_status",
    ):
        value = row.get(field)

        if value is not None:
            result[field] = value

    return result


def build_report(
    rows: list[dict[str, Any]],
    *,
    max_duplicate_groups: int,
    max_examples: int,
) -> dict[str, Any]:
    change_counts: Counter[str] = (
        Counter()
    )

    invalid_examples: list[
        dict[str, Any]
    ] = []

    changed_examples: list[
        dict[str, Any]
    ] = []

    groups: dict[
        str,
        dict[str, Any],
    ] = {}

    valid_rows = 0
    invalid_rows = 0
    canonical_rows = 0
    changed_rows = 0

    for row in rows:
        raw_url = str(
            row.get("url")
            or ""
        )

        normalized = (
            normalize_project_url(
                raw_url
            )
        )

        if normalized is None:
            invalid_rows += 1

            if (
                len(invalid_examples)
                < max_examples
            ):
                invalid_examples.append(
                    compact_row(row)
                )

            continue

        valid_rows += 1

        if normalized.normalized == raw_url:
            canonical_rows += 1
        else:
            changed_rows += 1

            for change in normalized.changes:
                change_counts[change] += 1

            if (
                len(changed_examples)
                < max_examples
            ):
                changed_examples.append(
                    {
                        **compact_row(row),
                        "normalized_url": (
                            normalized.normalized
                        ),
                        "changes": list(
                            normalized.changes
                        ),
                    }
                )

        group = groups.setdefault(
            normalized.normalized,
            {
                "canonical_url": (
                    normalized.normalized
                ),
                "count": 0,
                "rows": [],
            },
        )

        group["count"] += 1

        if len(group["rows"]) < 20:
            group["rows"].append(
                compact_row(row)
            )

    duplicate_groups_all = [
        group
        for group in groups.values()
        if group["count"] > 1
    ]

    duplicate_groups_all.sort(
        key=lambda group: (
            -int(group["count"]),
            str(group["canonical_url"]),
        )
    )

    rows_in_duplicate_groups = sum(
        int(group["count"])
        for group in duplicate_groups_all
    )

    duplicate_excess_rows = sum(
        int(group["count"]) - 1
        for group in duplicate_groups_all
    )

    duplicate_statuses: Counter[str] = (
        Counter()
    )

    duplicate_doc_types: Counter[str] = (
        Counter()
    )

    for group in duplicate_groups_all:
        for row in group["rows"]:
            status = row.get("status")

            if status is not None:
                duplicate_statuses[
                    str(status)
                ] += 1

            doc_type = row.get("doc_type")

            if doc_type is not None:
                duplicate_doc_types[
                    str(doc_type)
                ] += 1

    return {
        "generated_at": utc_now(),
        "database": str(
            settings.state_db_path
        ),
        "mode": "read_only",
        "summary": {
            "total_rows": len(rows),
            "valid_project_urls": (
                valid_rows
            ),
            "invalid_or_external_urls": (
                invalid_rows
            ),
            "already_canonical": (
                canonical_rows
            ),
            "would_change": changed_rows,
            "canonical_unique_urls": len(
                groups
            ),
            "duplicate_groups": len(
                duplicate_groups_all
            ),
            "rows_in_duplicate_groups": (
                rows_in_duplicate_groups
            ),
            "duplicate_excess_rows": (
                duplicate_excess_rows
            ),
        },
        "change_types": dict(
            change_counts.most_common()
        ),
        "duplicate_statuses": dict(
            duplicate_statuses.most_common()
        ),
        "duplicate_doc_types": dict(
            duplicate_doc_types.most_common()
        ),
        "duplicate_groups": (
            duplicate_groups_all[
                :max_duplicate_groups
            ]
        ),
        "changed_examples": (
            changed_examples
        ),
        "invalid_examples": (
            invalid_examples
        ),
    }


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only аудит URL "
            "и потенциальных дублей"
        )
    )

    parser.add_argument(
        "--output",
        help="Путь к JSON-отчёту",
    )

    parser.add_argument(
        "--max-groups",
        type=int,
        default=200,
        help=(
            "Максимум групп дублей "
            "в JSON-отчёте"
        ),
    )

    parser.add_argument(
        "--max-examples",
        type=int,
        default=100,
        help=(
            "Максимум примеров "
            "изменённых и ошибочных URL"
        ),
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    connection = open_read_only()

    try:
        rows = select_rows(
            connection
        )
    finally:
        connection.close()

    report = build_report(
        rows,
        max_duplicate_groups=max(
            arguments.max_groups,
            1,
        ),
        max_examples=max(
            arguments.max_examples,
            1,
        ),
    )

    if arguments.output:
        output_path = Path(
            arguments.output
        )
    else:
        timestamp = datetime.now().strftime(
            "%Y%m%d-%H%M%S"
        )

        output_path = Path(
            "logs"
        ) / (
            f"url-audit-{timestamp}.json"
        )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("[SUMMARY]")

    print(
        json.dumps(
            report["summary"],
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("[CHANGE TYPES]")

    print(
        json.dumps(
            report["change_types"],
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print(
        "report:",
        output_path,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
