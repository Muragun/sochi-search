from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .content_processing import extract_content
from .fetcher import HttpFetcher


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def select_candidates(
    *,
    limit: int,
    specific_url: str | None,
) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        settings.state_db_path
    )

    connection.row_factory = sqlite3.Row

    try:
        if specific_url:
            rows = connection.execute(
                """
                SELECT *
                FROM crawl_urls
                WHERE
                    url = ?
                    OR page_url = ?
                LIMIT 1
                """,
                (
                    specific_url,
                    specific_url,
                ),
            ).fetchall()

        else:
            rows = connection.execute(
                """
                SELECT *
                FROM crawl_urls
                WHERE is_active = 1
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
                ORDER BY
                    CASE
                        WHEN document_number IS NULL
                        THEN 0
                        ELSE 1
                    END,
                    COALESCE(
                        last_checked_at,
                        ''
                    ) ASC,
                    url ASC
                LIMIT ?
                """,
                (
                    limit,
                ),
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        connection.close()


def audit_row(
    row: dict[str, Any],
    *,
    fetcher: HttpFetcher,
) -> dict[str, Any]:
    identity_url = str(
        row.get("url")
        or ""
    ).strip()

    fetch_url = str(
        row.get("page_url")
        or row.get("url")
        or ""
    ).strip()

    result: dict[str, Any] = {
        "url": identity_url,
        "fetch_url": fetch_url,
        "success": False,
        "http_status": None,
        "final_url": None,
        "content_type": None,
        "document_number": None,
        "document_date": None,
        "document_kind": None,
        "attachments": [],
        "error": None,
    }

    if not fetch_url:
        result["error"] = "URL отсутствует"
        return result

    response = None

    try:
        response = fetcher.fetch(
            fetch_url,
            etag=None,
            last_modified=None,
        )

        result["http_status"] = (
            response.status_code
        )

        result["final_url"] = (
            response.final_url
        )

        result["content_type"] = (
            response.content_type
        )

        if response.status_code != 200:
            result["error"] = (
                f"HTTP {response.status_code}"
            )

            return result

        content = extract_content(
            response.payload,
            content_type=(
                response.content_type
            ),
            final_url=response.final_url,
        )

        result["document_number"] = (
            content.document_number
        )

        result["document_date"] = (
            content.document_date
        )

        result["document_kind"] = (
            content.document_kind
        )

        result["attachments"] = [
            {
                "url": attachment.url,
                "title": attachment.title,
            }
            for attachment in (
                content.attachments
            )
        ]

        result["title"] = content.title
        result["text_length"] = sum(
            len(text)
            for _, text in content.pages
        )

        result["success"] = True

        return result

    except Exception as exc:
        result["error"] = str(exc)
        return result

    finally:
        if response is not None:
            response.cleanup()


def build_summary(
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    successful = [
        result
        for result in results
        if result["success"]
    ]

    failed = [
        result
        for result in results
        if not result["success"]
    ]

    number_found = sum(
        1
        for result in successful
        if result.get(
            "document_number"
        )
    )

    date_found = sum(
        1
        for result in successful
        if result.get(
            "document_date"
        )
    )

    kind_found = sum(
        1
        for result in successful
        if result.get(
            "document_kind"
        )
    )

    pages_with_attachments = sum(
        1
        for result in successful
        if result.get(
            "attachments"
        )
    )

    total_attachments = sum(
        len(
            result.get(
                "attachments",
                [],
            )
        )
        for result in successful
    )

    return {
        "tested": len(results),
        "successful": len(successful),
        "failed": len(failed),
        "document_number_found": (
            number_found
        ),
        "document_date_found": (
            date_found
        ),
        "document_kind_found": (
            kind_found
        ),
        "pages_with_attachments": (
            pages_with_attachments
        ),
        "total_attachments": (
            total_attachments
        ),
    }


def print_result(
    number: int,
    result: dict[str, Any],
) -> None:
    print()
    print("=" * 90)
    print(f"#{number}")
    print("URL:", result["url"])
    print(
        "HTTP:",
        result.get("http_status"),
    )

    if not result["success"]:
        print(
            "[ERROR]",
            result.get("error"),
        )
        return

    print(
        "Заголовок:",
        result.get("title"),
    )

    print(
        "Номер:",
        result.get("document_number")
        or "не найден",
    )

    print(
        "Дата:",
        result.get("document_date")
        or "не найдена",
    )

    print(
        "Тип:",
        result.get("document_kind")
        or "не найден",
    )

    attachments = result.get(
        "attachments",
        [],
    )

    print(
        "PDF-вложений:",
        len(attachments),
    )

    for attachment in attachments:
        print(
            "  -",
            attachment["title"],
            "->",
            attachment["url"],
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Проверка извлечения "
            "метаданных нормативных актов"
        )
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help=(
            "Количество страниц "
            "для проверки"
        ),
    )

    parser.add_argument(
        "--url",
        help=(
            "Проверить один конкретный URL"
        ),
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help=(
            "Пауза между запросами "
            "в секундах"
        ),
    )

    parser.add_argument(
        "--output",
        help=(
            "Путь к JSON-отчёту"
        ),
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    if arguments.limit < 1:
        print(
            "--limit должен быть больше нуля",
            file=sys.stderr,
        )
        return 2

    specific_url = (
        arguments.url.strip()
        if arguments.url
        else None
    )

    candidates = select_candidates(
        limit=arguments.limit,
        specific_url=specific_url,
    )

    if not candidates:
        print(
            "[ERROR] Подходящие страницы "
            "в SQLite не найдены",
            file=sys.stderr,
        )
        return 1

    print(
        "[START] Страниц для проверки:",
        len(candidates),
    )

    fetcher = HttpFetcher()
    results: list[dict[str, Any]] = []

    for number, row in enumerate(
        candidates,
        start=1,
    ):
        result = audit_row(
            row,
            fetcher=fetcher,
        )

        results.append(result)

        print_result(
            number,
            result,
        )

        if (
            arguments.delay > 0
            and number < len(candidates)
        ):
            time.sleep(
                arguments.delay
            )

    summary = build_summary(
        results
    )

    generated_at = utc_now()

    report = {
        "generated_at": generated_at,
        "database": str(
            settings.state_db_path
        ),
        "summary": summary,
        "results": results,
    }

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
            f"legal-audit-{timestamp}.json"
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

    print()
    print("=" * 90)
    print("[SUMMARY]")

    for key, value in summary.items():
        print(f"{key}: {value}")

    print(
        "report:",
        output_path,
    )

    return (
        1
        if summary["successful"] == 0
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
