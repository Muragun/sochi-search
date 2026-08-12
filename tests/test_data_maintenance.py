from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from crawler_v2.data_maintenance import run_migration


CRAWL_SCHEMA = """
CREATE TABLE crawl_urls (
    url TEXT PRIMARY KEY,
    doc_type TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    is_active INTEGER NOT NULL,
    indexed_chunks INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TEXT,
    consecutive_missing INTEGER NOT NULL DEFAULT 0,
    next_check_at TEXT,
    locked_at TEXT,
    locked_by TEXT,
    last_error TEXT
);

CREATE TABLE content_cleanup_queue (
    url TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_error TEXT,
    updated_at TEXT,
    locked_at TEXT,
    locked_by TEXT
);

CREATE TABLE publication_date_queue (
    url TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_http_status INTEGER,
    last_error TEXT,
    updated_at TEXT,
    locked_at TEXT,
    locked_by TEXT
);
"""


class DataMaintenanceTests(unittest.TestCase):
    def make_database(self, directory: str) -> Path:
        path = Path(directory) / "crawl.sqlite3"

        with sqlite3.connect(path) as connection:
            connection.executescript(CRAWL_SCHEMA)
            connection.executemany(
                """
                INSERT INTO crawl_urls (
                    url, doc_type, status, http_status, is_active,
                    indexed_chunks, last_indexed_at, next_check_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'soon')
                """,
                (
                    (
                        "http://sochi.ru/content/detail.php?ID=1",
                        "page", "missing", 404, 1, 0, None,
                    ),
                    (
                        "http://sochi.ru/contents/detail.php?ID=2",
                        "page", "discovered", None, 1, 0, None,
                    ),
                    (
                        "https://sochi.ru/files/archive.zip",
                        "file", "discovered", None, 1, 0, None,
                    ),
                    (
                        "https://sochi.ru/files/old.doc",
                        "file", "retry", 500, 1, 0, None,
                    ),
                    (
                        "https://sochi.ru/files/current.docx",
                        "file", "discovered", None, 1, 0, None,
                    ),
                    (
                        "https://sochi.ru/press-sluzhba/novosti/66/100/",
                        "page", "indexed", 200, 1, 3,
                        "2026-08-05T10:00:00+00:00",
                    ),
                    (
                        "https://sochi.ru/press-sluzhba/obyavleniya/200/",
                        "page", "gone", 404, 0, 0, None,
                    ),
                ),
            )
            connection.executemany(
                """
                INSERT INTO content_cleanup_queue (
                    url, status, updated_at
                )
                VALUES (?, ?, 'old')
                """,
                (
                    (
                        "https://sochi.ru/press-sluzhba/novosti/11812/",
                        "pending",
                    ),
                    (
                        "https://sochi.ru/random/stale-processing/",
                        "processing",
                    ),
                    (
                        "https://sochi.ru/press-sluzhba/novosti/66/100/",
                        "pending",
                    ),
                ),
            )
            connection.executemany(
                """
                INSERT INTO publication_date_queue (
                    url, status, attempts, updated_at
                )
                VALUES (?, ?, 3, 'old')
                """,
                (
                    (
                        "https://sochi.ru/press-sluzhba/novosti/66/100/",
                        "not_indexed",
                    ),
                    (
                        "https://sochi.ru/press-sluzhba/obyavleniya/200/",
                        "retry",
                    ),
                ),
            )

        return path

    def test_migration_is_transactional_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_database(directory)
            result = run_migration(path, require_no_review=True)

            self.assertEqual(result["legacy"]["closed"], 2)
            self.assertEqual(result["formats"]["finalized"], 2)
            self.assertEqual(result["cleanup_finalized"], 2)
            self.assertEqual(result["publication"]["terminal"], 1)
            self.assertEqual(result["publication"]["requeued"], 1)
            self.assertEqual(result["sqlite_quick_check"], "ok")

            with sqlite3.connect(path) as connection:
                states = dict(
                    connection.execute(
                        "SELECT url, status FROM crawl_urls"
                    ).fetchall()
                )
                active = dict(
                    connection.execute(
                        "SELECT url, is_active FROM crawl_urls"
                    ).fetchall()
                )
                cleanup = dict(
                    connection.execute(
                        "SELECT url, status FROM content_cleanup_queue"
                    ).fetchall()
                )
                publication = dict(
                    connection.execute(
                        "SELECT url, status FROM publication_date_queue"
                    ).fetchall()
                )

            self.assertEqual(
                states["http://sochi.ru/content/detail.php?ID=1"],
                "gone",
            )
            self.assertEqual(
                active["http://sochi.ru/content/detail.php?ID=1"],
                0,
            )
            self.assertEqual(
                states["https://sochi.ru/files/archive.zip"],
                "skipped_unsupported",
            )
            self.assertEqual(
                states["https://sochi.ru/files/current.docx"],
                "discovered",
            )
            self.assertEqual(
                cleanup[
                    "https://sochi.ru/press-sluzhba/novosti/11812/"
                ],
                "skipped_out_of_scope",
            )
            self.assertEqual(
                publication[
                    "https://sochi.ru/press-sluzhba/novosti/66/100/"
                ],
                "pending",
            )

            second = run_migration(path, require_no_review=True)
            self.assertEqual(second["legacy"]["closed"], 0)
            self.assertEqual(second["formats"]["finalized"], 0)
            self.assertEqual(second["cleanup_finalized"], 0)
            self.assertEqual(second["publication"]["terminal"], 0)
            self.assertEqual(second["publication"]["requeued"], 0)

    def test_review_guard_rolls_back_every_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_database(directory)

            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    INSERT INTO crawl_urls (
                        url, doc_type, status, http_status, is_active,
                        indexed_chunks, last_indexed_at, next_check_at
                    )
                    VALUES (?, 'page', 'indexed', 200, 1, 2, ?, 'later')
                    """,
                    (
                        "http://sochi.ru/content/detail.php?ID=999",
                        "2026-08-05T10:00:00+00:00",
                    ),
                )

            with self.assertRaises(RuntimeError):
                run_migration(path, require_no_review=True)

            with sqlite3.connect(path) as connection:
                state = connection.execute(
                    """
                    SELECT status, is_active
                    FROM crawl_urls
                    WHERE url = ?
                    """,
                    ("http://sochi.ru/content/detail.php?ID=1",),
                ).fetchone()

            self.assertEqual(state, ("missing", 1))


if __name__ == "__main__":
    unittest.main()
