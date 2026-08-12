from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from .config import settings
from .content_processing import (
    EmptyContentError,
)
from .es_writer import ElasticsearchWriter
from .fetcher import HttpFetcher
from .incremental_db import (
    IncrementalStateDatabase,
)
from .incremental_worker import (
    process_row,
)
from .legal_backfill_db import (
    LegalBackfillDatabase,
)
from .legal_db import (
    LegalMetadataDatabase,
)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill метаданных "
            "нормативных документов"
        )
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help=(
            "Количество карточек "
            "за один запуск"
        ),
    )

    parser.add_argument(
        "--url",
        help=(
            "Обработать одну карточку"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Не изменять SQLite "
            "и Elasticsearch"
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Игнорировать "
            "legal_backfill_next_at"
        ),
    )

    parser.add_argument(
        "--reset-failed",
        action="store_true",
        help=(
            "Вернуть retry/failed "
            "в очередь"
        ),
    )

    parser.add_argument(
        "--stats",
        action="store_true",
        help=(
            "Только показать статистику"
        ),
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    if arguments.limit < 1:
        print(
            "--limit должен быть "
            "больше нуля",
            file=sys.stderr,
        )
        return 2

    if arguments.url is not None:
        arguments.url = (
            arguments.url.strip()
        )

        if not arguments.url:
            print(
                "--url передан, но пуст",
                file=sys.stderr,
            )
            return 2

    backfill_database = (
        LegalBackfillDatabase(
            settings.state_db_path
        )
    )

    backfill_database.migrate()

    inserted_into_queue = (
        backfill_database.seed_queue()
    )

    if inserted_into_queue:
        print(
            "[QUEUE] Добавлено:",
            inserted_into_queue,
        )

    if arguments.reset_failed:
        reset_count = (
            backfill_database.reset_failed()
        )

        print(
            "[QUEUE] Возвращено "
            "retry/failed:",
            reset_count,
        )

    if arguments.stats:
        print(
            json.dumps(
                backfill_database.statistics(),
                ensure_ascii=False,
                indent=2,
            )
        )

        return 0

    released = (
        backfill_database.release_stale_locks(
            older_than_minutes=(
                settings.lock_timeout_minutes
            )
        )
    )

    if released:
        print(
            "[LOCKS] Освобождено:",
            released,
        )

    worker_id = (
        f"{socket.gethostname()}:"
        f"{os.getpid()}"
    )

    if arguments.dry_run:
        rows = (
            backfill_database.peek_candidates(
                limit=arguments.limit,
                specific_url=arguments.url,
                force=arguments.force,
            )
        )
    else:
        rows = (
            backfill_database.claim_candidates(
                limit=arguments.limit,
                worker_id=worker_id,
                specific_url=arguments.url,
                force=arguments.force,
            )
        )

    if not rows:
        print(
            "[OK] Нет карточек "
            "для backfill"
        )

        print(
            json.dumps(
                backfill_database.statistics(),
                ensure_ascii=False,
                indent=2,
            )
        )

        return 0

    print(
        f"[START] worker={worker_id} "
        f"urls={len(rows)} "
        f"dry_run={arguments.dry_run}"
    )

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

    fetcher = HttpFetcher()
    writer = ElasticsearchWriter()

    counters = {
        "indexed": 0,
        "unchanged": 0,
        "skipped_empty": 0,
        "skipped_unsupported": 0,
        "missing": 0,
        "gone": 0,
        "failed": 0,
    }

    for row in rows:
        identity_url = str(
            row["url"]
        )

        try:
            result_status = process_row(
                row,
                database=crawl_database,
                legal_database=legal_database,
                fetcher=fetcher,
                writer=writer,
                dry_run=arguments.dry_run,
            )

            counters[result_status] = (
                counters.get(
                    result_status,
                    0,
                )
                + 1
            )

            if not arguments.dry_run:
                backfill_database.mark_finished(
                    identity_url,
                    result_status=result_status,
                )

        except EmptyContentError as exc:
            result_status = "skipped_empty"
            counters[result_status] += 1

            print(
                f"[SKIPPED EMPTY] "
                f"{identity_url}: {exc}",
                file=sys.stderr,
            )

            if not arguments.dry_run:
                backfill_database.mark_finished(
                    identity_url,
                    result_status=result_status,
                )

        except Exception as exc:
            counters["failed"] += 1

            print(
                f"[ERROR] {identity_url}: "
                f"{exc}",
                file=sys.stderr,
            )

            if not arguments.dry_run:
                backfill_database.mark_failed(
                    identity_url,
                    error_message=str(exc),
                    retry_hours=(
                        settings.retry_hours
                    ),
                )

    print()
    print("=" * 90)
    print("[FINISH]")

    for name, value in (
        counters.items()
    ):
        print(
            f"{name}: {value}"
        )

    print()
    print("[BACKFILL STATS]")

    print(
        json.dumps(
            backfill_database.statistics(),
            ensure_ascii=False,
            indent=2,
        )
    )

    return (
        1
        if counters["failed"]
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
