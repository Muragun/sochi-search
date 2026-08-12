from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app_v2.pipeline_status import (
    PipelineStatusUnavailable,
    build_pipeline_status,
)


class PipelineStatusTests(unittest.TestCase):
    def make_database(self, directory: str) -> Path:
        path = Path(directory) / "crawl.sqlite3"

        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE crawl_urls (
                    url TEXT PRIMARY KEY,
                    doc_type TEXT,
                    status TEXT,
                    is_active INTEGER,
                    indexed_chunks INTEGER,
                    last_indexed_at TEXT,
                    extraction_version TEXT
                );

                CREATE TABLE publication_date_queue (
                    url TEXT PRIMARY KEY,
                    status TEXT
                );

                CREATE TABLE content_cleanup_queue (
                    url TEXT PRIMARY KEY,
                    status TEXT
                );

                CREATE TABLE discovery_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT,
                    candidates_seen INTEGER,
                    new_urls_found INTEGER,
                    inserted_urls INTEGER,
                    sitemap_errors INTEGER
                );
                """
            )
            connection.executemany(
                """
                INSERT INTO crawl_urls VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "https://sochi.ru/news/1/",
                        "page", "indexed", 1, 4,
                        "2026-08-05T10:00:00+00:00",
                        None,
                    ),
                    (
                        "https://sochi.ru/files/a.pdf",
                        "pdf", "discovered", 1, 0, None, None,
                    ),
                    (
                        "https://sochi.ru/files/b.docx",
                        "file", "processing", 1, 0, None, None,
                    ),
                    (
                        "https://sochi.ru/files/c.zip",
                        "file", "skipped_unsupported", 0, 0, None, None,
                    ),
                    (
                        "https://sochi.ru/page/404/",
                        "page", "missing", 1, 0, None, None,
                    ),
                    (
                        "https://sochi.ru/files/empty.txt",
                        "file", "skipped_empty", 1, 0, None, None,
                    ),
                ),
            )
            connection.executemany(
                "INSERT INTO publication_date_queue VALUES (?, ?)",
                (
                    ("one", "done"),
                    ("two", "pending"),
                    ("three", "retry"),
                ),
            )
            connection.executemany(
                "INSERT INTO content_cleanup_queue VALUES (?, ?)",
                (
                    ("one", "done"),
                    ("two", "skipped_out_of_scope"),
                ),
            )
            connection.execute(
                """
                INSERT INTO discovery_runs VALUES (
                    1, 'start', 'finish', 'success', 100, 5, 3, 0
                )
                """
            )

        return path

    def test_status_has_clear_loaded_remaining_and_queue_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_database(directory)
            result = build_pipeline_status(
                path,
                elasticsearch_documents=123,
            )

            crawler = result["crawler"]
            self.assertEqual(crawler["total_urls"], 6)
            self.assertEqual(crawler["loaded_urls"], 1)
            self.assertEqual(crawler["remaining_urls"], 3)
            self.assertEqual(crawler["processing_urls"], 1)
            self.assertEqual(crawler["terminal_urls"], 1)
            self.assertEqual(crawler["problem_urls"], 1)
            self.assertEqual(crawler["completion_percent"], 33.3)
            self.assertEqual(result["elasticsearch"]["documents"], 123)

            formats = {
                item["key"]: item
                for item in crawler["formats"]
            }
            self.assertEqual(formats["pdf"]["remaining"], 1)
            self.assertEqual(formats["office"]["processing"], 1)
            self.assertEqual(formats["archive"]["terminal"], 1)
            self.assertEqual(formats["text"]["remaining"], 1)
            self.assertEqual(result["publication_date"]["pending"], 2)
            self.assertEqual(result["publication_date"]["problems"], 1)
            self.assertEqual(result["content_cleanup"]["completed"], 2)
            self.assertTrue(result["pdf_ocr"]["available"])
            self.assertEqual(result["pdf_ocr"]["total"], 1)
            self.assertEqual(result["pdf_ocr"]["completed"], 0)
            self.assertEqual(result["pdf_ocr"]["remaining"], 1)
            self.assertEqual(result["pdf_ocr"]["fast_indexed"], 0)
            self.assertEqual(result["latest_discovery"]["inserted_urls"], 3)

            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    UPDATE crawl_urls
                    SET status = 'processing'
                    WHERE doc_type = 'pdf'
                    """
                )

            processing_result = build_pipeline_status(path)
            self.assertEqual(
                processing_result["pdf_ocr"]["remaining"],
                0,
            )
            self.assertEqual(
                processing_result["pdf_ocr"]["processing"],
                1,
            )

            with sqlite3.connect(path) as connection:
                migration_table = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqlite_master
                    WHERE name = 'package_migrations'
                    """
                ).fetchone()[0]

            self.assertEqual(migration_table, 0)

    def test_missing_database_is_reported_without_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.sqlite3"

            with self.assertRaises(PipelineStatusUnavailable):
                build_pipeline_status(path)

            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
