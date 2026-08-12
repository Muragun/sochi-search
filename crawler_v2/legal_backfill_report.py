from __future__ import annotations

import json
import sqlite3

from .config import settings
from .legal_backfill_db import LegalBackfillDatabase


def main() -> None:
    database = LegalBackfillDatabase(
        settings.state_db_path
    )

    database.migrate()

    statistics = database.statistics()

    connection = sqlite3.connect(
        settings.state_db_path
    )
    connection.row_factory = sqlite3.Row

    recent = connection.execute(
        """
        SELECT
            url,
            legal_backfill_status,
            legal_backfill_attempts,
            legal_backfill_last_error,
            document_number,
            document_date,
            document_kind,
            legal_backfill_checked_at
        FROM crawl_urls
        WHERE legal_backfill_checked_at
            IS NOT NULL
        ORDER BY
            legal_backfill_checked_at DESC
        LIMIT 15
        """
    ).fetchall()

    retries = connection.execute(
        """
        SELECT
            url,
            legal_backfill_attempts,
            legal_backfill_last_error,
            legal_backfill_next_at
        FROM crawl_urls
        WHERE legal_backfill_status = 'retry'
        ORDER BY
            legal_backfill_checked_at DESC
        LIMIT 15
        """
    ).fetchall()

    connection.close()

    report = {
        **statistics,
        "recent": [
            dict(row)
            for row in recent
        ],
        "retries": [
            dict(row)
            for row in retries
        ],
    }

    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
