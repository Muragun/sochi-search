from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import settings


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()

    connection = sqlite3.connect(
        settings.state_db_path
    )
    connection.row_factory = sqlite3.Row

    total = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM crawl_urls
        """
    ).fetchone()["count"]

    active = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM crawl_urls
        WHERE is_active = 1
        """
    ).fetchone()["count"]

    due = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM crawl_urls
        WHERE is_active = 1
          AND status != 'processing'
          AND (
              next_check_at IS NULL
              OR next_check_at <= ?
          )
        """,
        (now,),
    ).fetchone()["count"]

    processing = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM crawl_urls
        WHERE status = 'processing'
        """
    ).fetchone()["count"]

    statuses = connection.execute(
        """
        SELECT
            status,
            COUNT(*) AS count
        FROM crawl_urls
        GROUP BY status
        ORDER BY count DESC
        """
    ).fetchall()

    latest = connection.execute(
        """
        SELECT
            url,
            status,
            http_status,
            doc_type,
            indexed_chunks,
            failure_count,
            last_error,
            last_checked_at,
            next_check_at
        FROM crawl_urls
        WHERE last_checked_at IS NOT NULL
        ORDER BY last_checked_at DESC
        LIMIT 10
        """
    ).fetchall()

    connection.close()

    result = {
        "database": str(settings.state_db_path),
        "generated_at": now,
        "total_urls": int(total),
        "active_urls": int(active),
        "due_urls": int(due),
        "processing_urls": int(processing),
        "statuses": {
            row["status"]: int(row["count"])
            for row in statuses
        },
        "latest": [
            dict(row)
            for row in latest
        ],
    }

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
