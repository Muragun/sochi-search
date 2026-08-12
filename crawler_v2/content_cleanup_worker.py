from __future__ import annotations

import argparse
import os
import re
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .config import settings


PROJECT_ROOT = Path(
    "/opt/sochi-search"
)

MAX_ATTEMPTS = 5
PROCESS_TIMEOUT_SECONDS = 600

NEWS_PATH = re.compile(
    r"^/press-sluzhba/novosti/\d+/\d+/?$",
    re.IGNORECASE,
)

ANNOUNCEMENT_PATH = re.compile(
    r"^/press-sluzhba/obyavleniya/\d+/?$",
    re.IGNORECASE,
)


QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS
content_cleanup_queue (
    url TEXT PRIMARY KEY,

    scope TEXT NOT NULL,
    status TEXT NOT NULL
        DEFAULT 'pending',

    attempts INTEGER NOT NULL
        DEFAULT 0,

    last_error TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    locked_at TEXT,
    locked_by TEXT
);

CREATE INDEX IF NOT EXISTS
idx_content_cleanup_status
ON content_cleanup_queue(
    status,
    attempts,
    updated_at
);

CREATE INDEX IF NOT EXISTS
idx_content_cleanup_scope
ON content_cleanup_queue(
    scope,
    status
);
"""


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def worker_name() -> str:
    return (
        f"{socket.gethostname()}:"
        f"{os.getpid()}"
    )


def connect() -> sqlite3.Connection:
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

    return connection


def migrate() -> None:
    with connect() as connection:
        connection.executescript(
            QUEUE_SCHEMA
        )


def crawl_columns(
    connection: sqlite3.Connection,
) -> set:
    return {
        str(row["name"])
        for row in connection.execute(
            "PRAGMA table_info(crawl_urls)"
        ).fetchall()
    }


def scope_condition(
    scope: str,
) -> str:
    if scope == "press":
        return """
            AND (
                url GLOB
                    '*sochi.ru/press-sluzhba/novosti/[0-9]*/[0-9]*/'

                OR url GLOB
                    '*sochi.ru/press-sluzhba/obyavleniya/[0-9]*/'
            )
        """

    if scope == "legal":
        return """
            AND url LIKE
                '%/normativno-pravovyye-akty/%'
            AND url LIKE
                '%ELEMENT_ID=%'
        """

    if scope == "all":
        return ""

    raise ValueError(
        f"Неизвестный scope: {scope}"
    )


def is_supported_cleanup_url(url: str) -> bool:
    path = urlparse(str(url or "")).path

    return bool(
        NEWS_PATH.fullmatch(path)
        or ANNOUNCEMENT_PATH.fullmatch(path)
    )


def finalize_out_of_scope() -> int:
    now = utc_now()

    with connect() as connection:
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
            if not is_supported_cleanup_url(
                str(row["url"])
            )
        ]

        if not urls:
            return 0

        connection.executemany(
            """
            UPDATE content_cleanup_queue
            SET
                status = 'skipped_out_of_scope',
                last_error = (
                    'Запись не относится к карточкам новостей '
                    'или объявлений'
                ),
                locked_at = NULL,
                locked_by = NULL,
                updated_at = ?
            WHERE url = ?
              AND status IN ('pending', 'processing', 'retry')
            """,
            [
                (now, url)
                for url in urls
            ],
        )

        return len(urls)


def seed_queue(
    scope: str,
) -> Dict[str, int]:
    now = utc_now()

    with connect() as connection:
        columns = crawl_columns(
            connection
        )

        conditions = [
            "doc_type = 'page'",
            "url IS NOT NULL",
            "TRIM(url) != ''",
        ]

        if "status" in columns:
            conditions.append(
                """
                status IN (
                    'indexed',
                    'indexed_existing'
                )
                """
            )

        if "is_active" in columns:
            conditions.append(
                "COALESCE(is_active, 1) = 1"
            )

        query = f"""
            SELECT url
            FROM crawl_urls
            WHERE {' AND '.join(conditions)}
            {scope_condition(scope)}
        """

        rows = connection.execute(
            query
        ).fetchall()

        before = connection.total_changes

        for row in rows:
            connection.execute(
                """
                INSERT OR IGNORE INTO
                content_cleanup_queue (
                    url,
                    scope,
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
                (
                    row["url"],
                    scope,
                    now,
                    now,
                ),
            )

        inserted = (
            connection.total_changes
            - before
        )

        return {
            "candidates": len(rows),
            "inserted": inserted,
        }


def release_stale_locks() -> int:
    threshold = (
        datetime.now(timezone.utc)
        - timedelta(hours=2)
    ).isoformat()

    now = utc_now()

    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE content_cleanup_queue
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
    limit: int,
) -> List[sqlite3.Row]:
    now = utc_now()
    worker = worker_name()

    connection = connect()

    try:
        connection.execute(
            "BEGIN IMMEDIATE"
        )

        rows = connection.execute(
            """
            SELECT
                url,
                scope,
                attempts
            FROM content_cleanup_queue
            WHERE status IN (
                'pending',
                'retry'
            )
              AND attempts < ?

              AND (
                  url GLOB
                      '*sochi.ru/press-sluzhba/novosti/[0-9]*/[0-9]*/'

                  OR url GLOB
                      '*sochi.ru/press-sluzhba/obyavleniya/[0-9]*/'
              )

            ORDER BY
                CASE
                    WHEN url GLOB
                        '*sochi.ru/press-sluzhba/novosti/[0-9]*/[0-9]*/'
                        THEN 0

                    WHEN url GLOB
                        '*sochi.ru/press-sluzhba/obyavleniya/[0-9]*/'
                        THEN 1

                    ELSE 2
                END ASC,

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

        for row in rows:
            connection.execute(
                """
                UPDATE content_cleanup_queue
                SET
                    status = 'processing',
                    attempts = attempts + 1,
                    locked_at = ?,
                    locked_by = ?,
                    updated_at = ?,
                    last_error = NULL
                WHERE url = ?
                """,
                (
                    now,
                    worker,
                    now,
                    row["url"],
                ),
            )

        connection.commit()

        return rows

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def finish_row(
    url: str,
    *,
    success: bool,
    message: Optional[str],
) -> None:
    now = utc_now()

    with connect() as connection:
        current = connection.execute(
            """
            SELECT attempts
            FROM content_cleanup_queue
            WHERE url = ?
            """,
            (url,),
        ).fetchone()

        attempts = (
            int(current["attempts"])
            if current
            else MAX_ATTEMPTS
        )

        if success:
            status = "done"
            error = None

        elif attempts >= MAX_ATTEMPTS:
            status = "failed"
            error = message

        else:
            status = "retry"
            error = message

        connection.execute(
            """
            UPDATE content_cleanup_queue
            SET
                status = ?,
                last_error = ?,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = ?
            WHERE url = ?
            """,
            (
                status,
                error,
                now,
                url,
            ),
        )


def process_url(
    url: str,
) -> bool:
    command = [
        sys.executable,
        "-m",
        "crawler_v2.incremental_worker",
        "--url",
        url,
        "--limit",
        "1",
        "--force",
    ]

    print()
    print("=" * 100)
    print("[CLEANUP]", url)

    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
            check=False,
        )

    except subprocess.TimeoutExpired as exc:
        output = (
            exc.stdout
            or ""
        )

        if not isinstance(output, str):
            output = output.decode(
                "utf-8",
                errors="replace",
            )

        print(output)
        print(
            "[ERROR] Превышен timeout"
        )

        finish_row(
            url,
            success=False,
            message=(
                "Timeout при переиндексации"
            ),
        )

        return False

    output = completed.stdout or ""

    print(output.rstrip())

    success = (
        completed.returncode == 0
        and "[FINISH]" in output
        and "failed: 0" in output
    )

    if success:
        finish_row(
            url,
            success=True,
            message=None,
        )

        print("[DONE]", url)

        return True

    error_tail = output[-3000:]

    finish_row(
        url,
        success=False,
        message=(
            f"exit={completed.returncode}; "
            f"{error_tail}"
        ),
    )

    print(
        "[RETRY OR FAILED]",
        url,
    )

    return False


def statistics() -> Dict[str, int]:
    result = {
        "pending": 0,
        "processing": 0,
        "retry": 0,
        "done": 0,
        "failed": 0,
        "skipped_out_of_scope": 0,
        "total": 0,
    }

    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
                status,
                COUNT(*) AS count
            FROM content_cleanup_queue
            GROUP BY status
            """
        ).fetchall()

        for row in rows:
            status = str(
                row["status"]
            )

            count = int(
                row["count"]
            )

            result[status] = count
            result["total"] += count

    return result


def print_statistics() -> None:
    values = statistics()

    print("[STATS]")

    for key in (
        "pending",
        "processing",
        "retry",
        "done",
        "failed",
        "skipped_out_of_scope",
        "total",
    ):
        print(
            f"{key}: {values.get(key, 0)}"
        )


def reset_failed() -> int:
    now = utc_now()

    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE content_cleanup_queue
            SET
                status = 'retry',
                attempts = 0,
                last_error = NULL,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = ?
            WHERE status = 'failed'
            """,
            (now,),
        )

        return int(cursor.rowcount)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Фоновая очистка и "
            "переиндексация HTML"
        )
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
    )

    parser.add_argument(
        "--seed-scope",
        choices=(
            "none",
            "press",
            "legal",
            "all",
        ),
        default="none",
    )

    parser.add_argument(
        "--stats",
        action="store_true",
    )

    parser.add_argument(
        "--reset-failed",
        action="store_true",
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    migrate()

    finalized = finalize_out_of_scope()

    if finalized:
        print(
            "[OUT OF SCOPE FINALIZED]",
            finalized,
        )

    if arguments.reset_failed:
        count = reset_failed()

        print(
            "[RESET FAILED]",
            count,
        )

    if arguments.seed_scope != "none":
        seeded = seed_queue(
            arguments.seed_scope
        )

        print(
            "[SEED]",
            seeded,
        )

    if arguments.stats:
        print_statistics()
        return 0

    released = release_stale_locks()

    if released:
        print(
            "[STALE LOCKS RELEASED]",
            released,
        )

    rows = claim_rows(
        max(arguments.limit, 1)
    )

    print(
        "[START]",
        f"worker={worker_name()}",
        f"urls={len(rows)}",
    )

    successful = 0
    unsuccessful = 0

    for row in rows:
        if process_url(
            str(row["url"])
        ):
            successful += 1
        else:
            unsuccessful += 1

    print()
    print("[FINISH]")
    print("successful:", successful)
    print("unsuccessful:", unsuccessful)

    print_statistics()

    return (
        0
        if unsuccessful == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
