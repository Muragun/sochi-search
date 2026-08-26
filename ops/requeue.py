#!/usr/bin/env python3
"""
Поставить уже обработанные документы в очередь заново.

Нужен, когда изменился не сайт, а разбор: добавили извлечение реквизитов
из офисных файлов, научились отличать машинное имя файла от осмысленного,
подняли качество распознавания. Документы в индексе остались с прежними
полями, а перечитывать их нечему — сайт-то не менялся.

Просто сдвинуть `next_check_at` недостаточно. Воркер сравнивает хеш
содержимого с прошлым, и при совпадении помечает документ неизменившимся
и выходит, не переразбирая. Поэтому здесь сбрасывается и хеш: для
воркера документ становится новым.

По умолчанию ничего не меняет — показывает, сколько адресов подпадает.
Изменения вносятся только с `--apply`.

    # что подпадает под отбор
    python -m ops.requeue --database data/crawl_state.sqlite3 --office

    # поставить в очередь офисные документы без реквизитов
    python -m ops.requeue --database data/crawl_state.sqlite3 \\
        --office --missing-requisites --apply

    # то же для PDF, порциями
    python -m ops.requeue --database data/crawl_state.sqlite3 \\
        --pdf --missing-requisites --limit 500 --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from crawler_v2.incremental_db import iso_now  # noqa: E402

# Типы документов по группам. `page` намеренно отдельно: HTML-страниц
# больше всего, и перечитывать их заодно с вложениями — это лишние
# десятки тысяч загрузок.
DOC_TYPE_GROUPS = {
    "office": ("file",),
    "pdf": ("pdf",),
    "page": ("page",),
    "attachments": ("pdf", "file"),
}


def connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise SystemExit(f"SQLite не найдена: {path}")

    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")

    return connection


def table_columns(
    connection: sqlite3.Connection,
    table: str,
) -> set:
    return {
        str(row["name"])
        for row in connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


def build_conditions(
    arguments: argparse.Namespace,
) -> tuple[str, list[Any]]:
    conditions = ["COALESCE(is_active, 1) = 1"]
    parameters: list[Any] = []

    doc_types: list[str] = []

    for name, values in DOC_TYPE_GROUPS.items():
        if getattr(arguments, name, False):
            doc_types.extend(values)

    if doc_types:
        placeholders = ", ".join("?" for _ in doc_types)
        conditions.append(
            f"LOWER(COALESCE(doc_type, '')) IN ({placeholders})"
        )
        parameters.extend(doc_types)

    # Уже обработанные: перечитывать то, что и так стоит в очереди,
    # незачем — оно и так дойдёт.
    conditions.append(
        "status IN ('indexed', 'unchanged', 'skipped_empty')"
    )

    if arguments.missing_requisites:
        conditions.append(
            """
            (
                COALESCE(document_number, '') = ''
                OR COALESCE(document_date, '') = ''
                OR COALESCE(document_kind, '') = ''
            )
            """
        )

    if arguments.url_like:
        conditions.append("url LIKE ?")
        parameters.append(arguments.url_like)

    return " AND ".join(conditions), parameters


def count_matching(
    connection: sqlite3.Connection,
    where: str,
    parameters: list[Any],
) -> dict[str, int]:
    rows = connection.execute(
        f"""
        SELECT
            LOWER(COALESCE(doc_type, '')) AS kind,
            COUNT(*) AS total
        FROM crawl_urls
        WHERE {where}
        GROUP BY kind
        ORDER BY total DESC
        """,
        parameters,
    ).fetchall()

    return {str(row["kind"] or "?"): int(row["total"]) for row in rows}


def requeue(
    connection: sqlite3.Connection,
    where: str,
    parameters: list[Any],
    *,
    limit: int,
) -> int:
    selection = f"""
        SELECT url
        FROM crawl_urls
        WHERE {where}
        ORDER BY last_indexed_at IS NULL DESC, last_indexed_at ASC
    """

    if limit > 0:
        selection += " LIMIT ?"
        parameters = parameters + [limit]

    urls = [
        str(row["url"])
        for row in connection.execute(selection, parameters).fetchall()
    ]

    if not urls:
        return 0

    now = iso_now()

    # Хеш обнуляется намеренно: без этого воркер сравнит его с прежним,
    # увидит совпадение и выйдет, не переразбирая документ. Реквизиты и
    # заголовок так и остались бы старыми.
    #
    # ETag и Last-Modified тоже: иначе сервер ответит 304, и содержимое
    # даже не доедет.
    #
    # Состав столбцов проверяется, а не предполагается: `source_hash`
    # добавляется миграцией и есть не в каждой базе.
    wanted = (
        "content_hash",
        "source_hash",
        "etag",
        "last_modified",
        "locked_at",
        "locked_by",
    )
    present = table_columns(connection, "crawl_urls")
    cleared = [name for name in wanted if name in present]

    assignments = ", ".join(f"{name} = NULL" for name in cleared)

    if "next_check_at" in present:
        assignments = f"next_check_at = ?, {assignments}"
        leading: list[Any] = [now]
    else:
        leading = []

    connection.execute("BEGIN IMMEDIATE")

    try:
        for chunk_start in range(0, len(urls), 500):
            chunk = urls[chunk_start:chunk_start + 500]
            placeholders = ", ".join("?" for _ in chunk)

            connection.execute(
                f"""
                UPDATE crawl_urls
                SET {assignments}
                WHERE url IN ({placeholders})
                """,
                [*leading, *chunk],
            )

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    return len(urls)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Поставить обработанные документы в очередь заново"
        ),
    )
    parser.add_argument("--database", type=Path, required=True)

    parser.add_argument(
        "--office",
        action="store_true",
        help="DOCX, XLSX, RTF, ODT, TXT — всё, что doc_type=file",
    )
    parser.add_argument("--pdf", action="store_true")
    parser.add_argument("--page", action="store_true")
    parser.add_argument(
        "--attachments",
        action="store_true",
        help="PDF и офисные файлы вместе",
    )

    parser.add_argument(
        "--missing-requisites",
        action="store_true",
        help="Только те, у кого нет номера, даты или вида акта",
    )
    parser.add_argument(
        "--url-like",
        default="",
        help="Дополнительный отбор по адресу, например %%/upload/%%",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Сколько адресов взять за раз; 0 — все подходящие",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Внести изменения; без него только показывает отбор",
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    connection = connect(arguments.database)

    try:
        where, parameters = build_conditions(arguments)
        counts = count_matching(connection, where, list(parameters))
        total = sum(counts.values())

        print("Подходит под отбор:")

        for kind, count in counts.items():
            print(f"  {kind or '(без типа)'}: {count}")

        print(f"  всего: {total}")

        if not total:
            print("\nНечего ставить в очередь.")
            return 0

        if not arguments.apply:
            print(
                "\nЭто предварительный просмотр. Для изменения добавьте "
                "--apply."
            )
            return 0

        requeued = requeue(
            connection,
            where,
            list(parameters),
            limit=arguments.limit,
        )

        print(f"\nREQUEUED={requeued}")
        print(
            "Документы будут перечитаны воркером по мере разбора "
            "очереди. Прогресс виден в панели управления."
        )

        return 0

    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
