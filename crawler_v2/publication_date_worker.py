from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import socket
import sqlite3
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Optional
from typing import TextIO
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .content_processing import (
    extract_published_at,
)


PROJECT_ROOT = Path(
    "/opt/sochi-search"
)

DATABASE_PATH = Path(
    os.getenv(
        "CRAWL_STATE_DB",
        str(
            PROJECT_ROOT
            / "data"
            / "crawl_state.sqlite3"
        ),
    )
)

ES_URL = (
    os.getenv("ES_URL")
    or os.getenv("ELASTICSEARCH_URL")
    or "http://127.0.0.1:9200"
).rstrip("/")

ES_INDEX = (
    os.getenv("ES_INDEX")
    or os.getenv("ELASTICSEARCH_INDEX")
    or "sochi_search"
)

OWN_LOCK_PATH = (
    PROJECT_ROOT
    / "run"
    / "publication-date-worker.lock"
)

SHARED_LOCK_PATH = (
    PROJECT_ROOT
    / "run"
    / "incremental-worker.lock"
)

MAX_ATTEMPTS = 5

STALE_LOCK_MINUTES = 30

REQUEST_TIMEOUT = (
    10,
    45,
)

USER_AGENT = (
    "Mozilla/5.0 "
    "SochiSearchPublicationDateWorker/1.0"
)

NEWS_DETAIL_PATH = re.compile(
    r"^/press-sluzhba/novosti/"
    r"\d+/\d+/?$",
    re.IGNORECASE,
)

ANNOUNCEMENT_DETAIL_PATH = re.compile(
    r"^/press-sluzhba/obyavleniya/"
    r"\d+/?$",
    re.IGNORECASE,
)


def classify_publication_url(
    url: str,
) -> Optional[str]:
    path = urlparse(
        str(url or "")
    ).path

    if NEWS_DETAIL_PATH.fullmatch(path):
        return "news"

    if ANNOUNCEMENT_DETAIL_PATH.fullmatch(
        path
    ):
        return "announcement"

    return None




QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS
publication_date_queue (
    url TEXT PRIMARY KEY,

    category TEXT NOT NULL,

    status TEXT NOT NULL
        DEFAULT 'pending',

    attempts INTEGER NOT NULL
        DEFAULT 0,

    published_at TEXT,

    es_updated_chunks INTEGER,

    last_http_status INTEGER,
    last_error TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    locked_at TEXT,
    locked_by TEXT
);

CREATE INDEX IF NOT EXISTS
idx_publication_date_queue_status
ON publication_date_queue(
    status,
    attempts,
    updated_at
);

CREATE INDEX IF NOT EXISTS
idx_publication_date_queue_category
ON publication_date_queue(
    category,
    status
);
"""


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def utc_before_minutes(
    minutes: int,
) -> str:
    return (
        datetime.now(timezone.utc)
        - timedelta(minutes=minutes)
    ).isoformat()


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=60,
    )

    connection.row_factory = sqlite3.Row

    connection.execute(
        "PRAGMA journal_mode=WAL"
    )

    connection.execute(
        "PRAGMA busy_timeout=60000"
    )

    return connection


def ensure_schema() -> None:
    with connect_database() as connection:
        connection.executescript(
            QUEUE_SCHEMA
        )


def seed_queue() -> int:
    now = utc_now()

    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT url
            FROM crawl_urls
            WHERE url IS NOT NULL
              AND TRIM(url) != ''
              AND is_active = 1
              AND (
                    LOWER(url) LIKE
                        '%/press-sluzhba/novosti/%'

                    OR LOWER(url) LIKE
                        '%/press-sluzhba/obyavleniya/%'
              )
            """
        ).fetchall()

        values: list[
            tuple[str, str, str, str]
        ] = []

        for row in rows:
            url = str(row["url"])

            category = (
                classify_publication_url(url)
            )

            if category is None:
                continue

            values.append(
                (
                    url,
                    category,
                    now,
                    now,
                )
            )

        before_changes = (
            connection.total_changes
        )

        connection.executemany(
            """
            INSERT OR IGNORE INTO
            publication_date_queue (
                url,
                category,
                status,
                attempts,
                created_at,
                updated_at
            )
            VALUES (
                ?,
                ?,
                'pending',
                0,
                ?,
                ?
            )
            """,
            values,
        )

        inserted = (
            connection.total_changes
            - before_changes
        )

        connection.commit()

    return int(inserted)


def prune_invalid_queue() -> int:
    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT url
            FROM publication_date_queue
            """
        ).fetchall()

        invalid_urls = [
            str(row["url"])
            for row in rows
            if classify_publication_url(
                str(row["url"])
            ) is None
        ]

        if not invalid_urls:
            return 0

        connection.executemany(
            """
            DELETE FROM publication_date_queue
            WHERE url = ?
            """,
            [
                (url,)
                for url in invalid_urls
            ],
        )

        connection.commit()

        return len(invalid_urls)


def reconcile_queue() -> dict[str, int]:
    """Synchronize auxiliary work with the authoritative crawl queue."""
    now = utc_now()

    with connect_database() as connection:
        terminal_cursor = connection.execute(
            """
            UPDATE publication_date_queue
            SET
                status = 'skipped',
                locked_at = NULL,
                locked_by = NULL,
                last_error = (
                    'Основной crawler пометил URL как терминальный'
                ),
                updated_at = ?
            WHERE status IN (
                'pending',
                'processing',
                'retry',
                'not_indexed',
                'failed'
            )
              AND EXISTS (
                  SELECT 1
                  FROM crawl_urls
                  WHERE crawl_urls.url = publication_date_queue.url
                    AND (
                        COALESCE(crawl_urls.is_active, 1) = 0
                        OR crawl_urls.status IN (
                            'gone',
                            'quarantined',
                            'skipped_corrupt',
                            'skipped_unsupported'
                        )
                    )
              )
            """,
            (now,),
        )

        http_terminal_cursor = connection.execute(
            """
            UPDATE publication_date_queue
            SET
                status = 'skipped',
                locked_at = NULL,
                locked_by = NULL,
                last_error = (
                    'Терминальный HTTP '
                    || COALESCE(last_http_status, 404)
                ),
                updated_at = ?
            WHERE status IN ('retry', 'failed')
              AND last_http_status IN (404, 410)
            """,
            (now,),
        )

        requeued_cursor = connection.execute(
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
                        'indexed',
                        'indexed_existing',
                        'unchanged'
                    )
              )
            """,
            (now,),
        )

        return {
            "terminal": (
                int(terminal_cursor.rowcount)
                + int(http_terminal_cursor.rowcount)
            ),
            "requeued": int(requeued_cursor.rowcount),
        }


def reset_status(
    old_status: str,
) -> int:
    now = utc_now()

    with connect_database() as connection:
        cursor = connection.execute(
            """
            UPDATE publication_date_queue
            SET
                status = 'retry',
                attempts = 0,
                last_error = NULL,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = ?
            WHERE status = ?
            """,
            (
                now,
                old_status,
            ),
        )

        connection.commit()

        return int(cursor.rowcount)


def release_stale_locks(
    connection: sqlite3.Connection,
) -> int:
    threshold = utc_before_minutes(
        STALE_LOCK_MINUTES
    )

    now = utc_now()

    cursor = connection.execute(
        """
        UPDATE publication_date_queue
        SET
            status = 'retry',
            locked_at = NULL,
            locked_by = NULL,
            last_error = (
                'Освобождён устаревший lock'
            ),
            updated_at = ?
        WHERE status = 'processing'
          AND locked_at IS NOT NULL
          AND locked_at < ?
        """,
        (
            now,
            threshold,
        ),
    )

    return int(cursor.rowcount)


def claim_rows(
    *,
    limit: int,
    worker_name: str,
) -> list[dict[str, Any]]:
    connection = connect_database()

    try:
        connection.execute(
            "BEGIN IMMEDIATE"
        )

        release_stale_locks(
            connection
        )

        rows = connection.execute(
            """
            SELECT
                url,
                category,
                attempts

            FROM publication_date_queue

            WHERE status IN (
                'pending',
                'retry'
            )
              AND attempts < ?

            ORDER BY
                attempts ASC,
                updated_at ASC,
                url ASC

            LIMIT ?
            """,
            (
                MAX_ATTEMPTS,
                limit,
            ),
        ).fetchall()

        now = utc_now()

        claimed: list[
            dict[str, Any]
        ] = []

        for row in rows:
            next_attempt = (
                int(row["attempts"])
                + 1
            )

            connection.execute(
                """
                UPDATE publication_date_queue
                SET
                    status = 'processing',
                    attempts = ?,
                    locked_at = ?,
                    locked_by = ?,
                    updated_at = ?,
                    last_error = NULL
                WHERE url = ?
                """,
                (
                    next_attempt,
                    now,
                    worker_name,
                    now,
                    row["url"],
                ),
            )

            claimed.append(
                {
                    "url": row["url"],
                    "category": (
                        row["category"]
                    ),
                    "attempts": (
                        next_attempt
                    ),
                }
            )

        connection.commit()

        return claimed

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def mark_result(
    *,
    url: str,
    status: str,
    published_at: Optional[str] = None,
    updated_chunks: Optional[int] = None,
    http_status: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    with connect_database() as connection:
        connection.execute(
            """
            UPDATE publication_date_queue
            SET
                status = ?,
                published_at = COALESCE(
                    ?,
                    published_at
                ),
                es_updated_chunks = ?,
                last_http_status = ?,
                last_error = ?,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = ?
            WHERE url = ?
            """,
            (
                status,
                published_at,
                updated_chunks,
                http_status,
                error,
                utc_now(),
                url,
            ),
        )

        connection.commit()


def create_session() -> requests.Session:
    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/json"
            ),
        }
    )

    return session


def fetch_publication_date(
    session: requests.Session,
    url: str,
) -> tuple[
    Optional[str],
    int,
    str,
]:
    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    status_code = int(
        response.status_code
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.content,
        "html.parser",
    )

    published_at = (
        extract_published_at(soup)
    )

    return (
        published_at,
        status_code,
        response.url,
    )


def update_elasticsearch(
    session: requests.Session,
    *,
    url: str,
    published_at: str,
) -> int:
    response = session.post(
        (
            f"{ES_URL}/{ES_INDEX}/"
            "_update_by_query"
        ),
        params={
            "conflicts": "proceed",
            "refresh": "false",
        },
        json={
            "query": {
                "term": {
                    "url": url,
                }
            },
            "script": {
                "lang": "painless",
                "source": (
                    "ctx._source.published_at = "
                    "params.published_at; "
                    "if ("
                    "ctx._source.containsKey("
                    "'document_date'"
                    ") && "
                    "ctx._source.document_date != null "
                    "&& ctx._source.document_date != ''"
                    ") { "
                    "ctx._source.sort_date = "
                    "ctx._source.document_date; "
                    "} else { "
                    "ctx._source.sort_date = "
                    "params.published_at; "
                    "}"
                ),
                "params": {
                    "published_at": (
                        published_at
                    ),
                },
            },
        },
        timeout=120,
    )

    response.raise_for_status()

    data = response.json()

    failures = data.get(
        "failures"
    ) or []

    if failures:
        raise RuntimeError(
            "Elasticsearch failures: "
            + json.dumps(
                failures[:3],
                ensure_ascii=False,
            )
        )

    return int(
        data.get("updated") or 0
    )


def refresh_elasticsearch(
    session: requests.Session,
) -> None:
    response = session.post(
        (
            f"{ES_URL}/"
            f"{ES_INDEX}/_refresh"
        ),
        timeout=60,
    )

    response.raise_for_status()


def acquire_lock(
    path: Path,
) -> Optional[TextIO]:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    handle = path.open(
        "a+",
        encoding="utf-8",
    )

    try:
        fcntl.flock(
            handle.fileno(),
            (
                fcntl.LOCK_EX
                | fcntl.LOCK_NB
            ),
        )

    except BlockingIOError:
        handle.close()
        return None

    handle.seek(0)
    handle.truncate()

    handle.write(
        (
            f"pid={os.getpid()} "
            f"host={socket.gethostname()} "
            f"started={utc_now()}\n"
        )
    )

    handle.flush()

    return handle


def close_lock(
    handle: Optional[TextIO],
) -> None:
    if handle is None:
        return

    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_UN,
        )

    finally:
        handle.close()


def print_report() -> None:
    with connect_database() as connection:
        rows = connection.execute(
            """
            SELECT
                status,
                COUNT(*) AS count
            FROM publication_date_queue
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()

        category_rows = connection.execute(
            """
            SELECT
                category,
                COUNT(*) AS total,

                SUM(
                    CASE
                        WHEN status = 'done'
                        THEN 1
                        ELSE 0
                    END
                ) AS done

            FROM publication_date_queue

            GROUP BY category
            ORDER BY category
            """
        ).fetchall()

        recent_rows = connection.execute(
            """
            SELECT
                url,
                category,
                status,
                attempts,
                published_at,
                es_updated_chunks,
                last_error,
                updated_at

            FROM publication_date_queue

            ORDER BY updated_at DESC
            LIMIT 10
            """
        ).fetchall()

    print()
    print("СТАТУСЫ:")

    for row in rows:
        print(
            f"{row['status']}: "
            f"{row['count']}"
        )

    print()
    print("КАТЕГОРИИ:")

    for row in category_rows:
        print(
            f"{row['category']}: "
            f"done={row['done']} "
            f"total={row['total']}"
        )

    print()
    print("ПОСЛЕДНИЕ ЗАПИСИ:")

    for row in recent_rows:
        print()
        print(
            row["status"],
            row["published_at"],
            row["url"],
        )

        if row["last_error"]:
            print(
                "  ERROR:",
                row["last_error"],
            )


def process_batch(
    *,
    limit: int,
) -> int:
    own_lock = acquire_lock(
        OWN_LOCK_PATH
    )

    if own_lock is None:
        print(
            "[LOCK] Уже запущен другой "
            "publication-date worker"
        )

        return 75

    shared_lock = acquire_lock(
        SHARED_LOCK_PATH
    )

    if shared_lock is None:
        print(
            "[LOCK] Общий crawler lock занят. "
            "Запуск пропущен."
        )

        close_lock(own_lock)

        return 75

    try:
        worker_name = (
            f"{socket.gethostname()}:"
            f"{os.getpid()}"
        )

        rows = claim_rows(
            limit=limit,
            worker_name=worker_name,
        )

        if not rows:
            print(
                "[OK] Нет записей для обработки"
            )

            return 0

        session = create_session()

        counters = {
            "done": 0,
            "skipped": 0,
            "no_date": 0,
            "not_indexed": 0,
            "retry": 0,
            "failed": 0,
        }

        updated_any = False

        for number, row in enumerate(
            rows,
            start=1,
        ):
            url = str(row["url"])

            detected_category = (
                classify_publication_url(url)
            )

            if detected_category is None:
                print()
                print(
                    f"[{number}/{len(rows)}] "
                    f"{url}"
                )
                print(
                    "  [SKIP] URL не является "
                    "карточкой публикации"
                )

                mark_result(
                    url=url,
                    status="skipped",
                    error=(
                        "URL не соответствует "
                        "формату карточки публикации"
                    ),
                )

                counters["skipped"] += 1
                continue

            print()
            print(
                f"[{number}/{len(rows)}] "
                f"{url}"
            )

            http_status: Optional[int] = None

            try:
                try:
                    (
                        published_at,
                        http_status,
                        final_url,
                    ) = fetch_publication_date(
                        session,
                        url,
                    )

                except requests.HTTPError as error:
                    response = error.response

                    http_status = (
                        int(response.status_code)
                        if response is not None
                        else None
                    )

                    if http_status not in {
                        404,
                        410,
                    }:
                        raise

                    final_url = (
                        str(response.url or url)
                        if response is not None
                        else url
                    )

                    error_text = (
                        f"HTTP {http_status}: "
                        "страница публикации "
                        "отсутствует: "
                        f"{final_url}"
                    )

                    print(
                        "  [SKIP]",
                        error_text,
                    )

                    mark_result(
                        url=url,
                        status="skipped",
                        http_status=http_status,
                        error=error_text,
                    )

                    counters["skipped"] += 1
                    continue

                print(
                    "  FINAL URL:",
                    final_url,
                )

                print(
                    "  HTTP:",
                    http_status,
                )

                if not published_at:
                    print(
                        "  [NO DATE] "
                        "Дата публикации не найдена"
                    )

                    mark_result(
                        url=url,
                        status="no_date",
                        http_status=http_status,
                        error=(
                            "time.detail-content-date "
                            "не найден"
                        ),
                    )

                    counters["no_date"] += 1
                    continue

                print(
                    "  DATE:",
                    published_at,
                )

                updated_chunks = (
                    update_elasticsearch(
                        session,
                        url=url,
                        published_at=(
                            published_at
                        ),
                    )
                )

                print(
                    "  ES CHUNKS:",
                    updated_chunks,
                )

                if updated_chunks <= 0:
                    mark_result(
                        url=url,
                        status="not_indexed",
                        published_at=(
                            published_at
                        ),
                        updated_chunks=0,
                        http_status=http_status,
                        error=(
                            "Документы по URL "
                            "не найдены в Elasticsearch"
                        ),
                    )

                    counters[
                        "not_indexed"
                    ] += 1

                    continue

                mark_result(
                    url=url,
                    status="done",
                    published_at=(
                        published_at
                    ),
                    updated_chunks=(
                        updated_chunks
                    ),
                    http_status=http_status,
                )

                counters["done"] += 1
                updated_any = True

            except Exception as error:
                attempts = int(
                    row["attempts"]
                )

                status = (
                    "failed"
                    if attempts >= MAX_ATTEMPTS
                    else "retry"
                )

                error_text = (
                    f"{type(error).__name__}: "
                    f"{error}"
                )[:2000]

                print(
                    "  [ERROR]",
                    error_text,
                )

                mark_result(
                    url=url,
                    status=status,
                    http_status=http_status,
                    error=error_text,
                )

                counters[status] += 1

        if updated_any:
            refresh_elasticsearch(
                session
            )

        print()
        print("ИТОГ ПАЧКИ:")

        for status, count in (
            counters.items()
        ):
            print(
                f"{status}: {count}"
            )

        return 0

    finally:
        close_lock(shared_lock)
        close_lock(own_lock)


def build_argument_parser(
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Заполнение published_at "
            "для новостей и объявлений"
        )
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help=(
            "Количество URL за один запуск"
        ),
    )

    parser.add_argument(
        "--report",
        action="store_true",
        help="Показать отчёт",
    )

    parser.add_argument(
        "--seed-only",
        action="store_true",
        help=(
            "Только заполнить очередь"
        ),
    )

    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Повторить записи failed"
        ),
    )

    parser.add_argument(
        "--retry-missing",
        action="store_true",
        help=(
            "Повторить записи not_indexed"
        ),
    )

    return parser


def main() -> int:
    parser = build_argument_parser()

    arguments = parser.parse_args()

    if arguments.limit < 1:
        parser.error(
            "--limit должен быть больше нуля"
        )

    ensure_schema()

    removed_invalid = (
        prune_invalid_queue()
    )

    print(
        "[QUEUE] Удалено неподходящих URL:",
        removed_invalid,
    )

    inserted = seed_queue()

    print(
        "[QUEUE] Добавлено новых URL:",
        inserted,
    )

    reconciled = reconcile_queue()

    print(
        "[QUEUE] Терминально закрыто:",
        reconciled["terminal"],
    )

    print(
        "[QUEUE] Повтор после индексации:",
        reconciled["requeued"],
    )

    if arguments.retry_failed:
        reset_count = reset_status(
            "failed"
        )

        print(
            "[QUEUE] Повтор failed:",
            reset_count,
        )

    if arguments.retry_missing:
        reset_count = reset_status(
            "not_indexed"
        )

        print(
            "[QUEUE] Повтор not_indexed:",
            reset_count,
        )

    if arguments.seed_only:
        print_report()
        return 0

    if arguments.report:
        print_report()
        return 0

    return process_batch(
        limit=arguments.limit,
    )


if __name__ == "__main__":
    sys.exit(main())
