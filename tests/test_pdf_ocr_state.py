from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from crawler_v2.pdf_ocr import (
    PDF_EXTRACTION_VERSION,
)
from crawler_v2.pdf_ocr_state import (
    migrate_pdf_ocr_state,
)
from crawler_v2.incremental_db import (
    IncrementalStateDatabase,
)
from crawler_v2.legal_db import (
    LegalMetadataDatabase,
)
from crawler_v2.state_db import (
    CrawlStateDatabase,
)


class PdfOcrStateTests(unittest.TestCase):
    def test_verified_attachment_metadata_can_clear_false_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crawl.sqlite3"
            pdf_url = "https://sochi.ru/upload/list.pdf"

            CrawlStateDatabase(path).initialize()
            IncrementalStateDatabase(path).migrate()
            database = LegalMetadataDatabase(path)
            database.migrate()

            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    INSERT INTO crawl_urls (
                        url, source, doc_type, status,
                        first_seen_at, last_seen_at, is_active,
                        document_number, document_date, document_kind
                    )
                    VALUES (?, 'test', 'pdf', 'indexed', ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        pdf_url,
                        "2026-08-10T08:00:00+00:00",
                        "2026-08-10T08:00:00+00:00",
                        "870",
                        "2024-10-10",
                        "Указ",
                    ),
                )

            database.replace_attachment_metadata(
                attachment_url=pdf_url,
                document_number=None,
                document_date=None,
                document_kind=None,
            )

            with sqlite3.connect(path) as connection:
                metadata = connection.execute(
                    """
                    SELECT document_number, document_date, document_kind
                    FROM crawl_urls
                    WHERE url = ?
                    """,
                    (pdf_url,),
                ).fetchone()

            self.assertEqual(metadata, (None, None, None))

    def test_resolved_parent_metadata_is_saved_for_parent_and_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crawl.sqlite3"
            parent_url = "https://sochi.ru/legal/?ELEMENT_ID=4638"
            pdf_url = "https://sochi.ru/upload/document.pdf"

            CrawlStateDatabase(path).initialize()
            IncrementalStateDatabase(path).migrate()
            database = LegalMetadataDatabase(path)
            database.migrate()

            with sqlite3.connect(path) as connection:
                connection.executemany(
                    """
                    INSERT INTO crawl_urls (
                        url, page_url, source, doc_type, status,
                        first_seen_at, last_seen_at, is_active,
                        parent_url, document_date, document_kind
                    )
                    VALUES (?, ?, 'test', ?, 'indexed', ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        (
                            parent_url,
                            parent_url,
                            "page",
                            "2026-08-10T08:00:00+00:00",
                            "2026-08-10T08:00:00+00:00",
                            None,
                            "2012-11-30",
                            "Распоряжение",
                        ),
                        (
                            pdf_url,
                            parent_url,
                            "pdf",
                            "2026-08-10T08:00:00+00:00",
                            "2026-08-10T08:00:00+00:00",
                            parent_url,
                            "2012-11-30",
                            "Распоряжение",
                        ),
                    ),
                )

            database.save_resolved_attachment_metadata(
                parent_url=parent_url,
                attachment_url=pdf_url,
                document_number="620-р",
                document_date="2012-11-30",
                document_kind="Распоряжение",
            )

            with sqlite3.connect(path) as connection:
                rows = connection.execute(
                    """
                    SELECT url, document_number, document_date, document_kind
                    FROM crawl_urls
                    ORDER BY url
                    """
                ).fetchall()

            self.assertEqual(len(rows), 2)
            self.assertTrue(
                all(row[1] == "620-р" for row in rows)
            )
            self.assertTrue(
                all(row[2] == "2012-11-30" for row in rows)
            )
            self.assertTrue(
                all(row[3] == "Распоряжение" for row in rows)
            )

    def test_new_pdf_attachment_inherits_parent_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crawl.sqlite3"
            parent_url = "https://sochi.ru/legal/223/"
            pdf_url = "https://sochi.ru/upload/223.pdf"

            CrawlStateDatabase(path).initialize()
            IncrementalStateDatabase(path).migrate()
            database = LegalMetadataDatabase(path)
            database.migrate()

            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    INSERT INTO crawl_urls (
                        url, page_url, source, doc_type, status,
                        first_seen_at, last_seen_at, is_active,
                        document_number, document_date, document_kind
                    )
                    VALUES (?, ?, 'html', 'page', 'indexed', ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        parent_url,
                        parent_url,
                        "2026-08-10T08:00:00+00:00",
                        "2026-08-10T08:00:00+00:00",
                        "223",
                        "2019-02-12",
                        "Постановление",
                    ),
                )

            attachment = SimpleNamespace(
                url=pdf_url,
                title="Постановление № 223",
            )
            first = database.upsert_attachments(
                (attachment,),
                parent_url=parent_url,
                seen_at="2026-08-10T09:00:00+00:00",
            )
            second = database.upsert_attachments(
                (attachment,),
                parent_url=parent_url,
                seen_at="2026-08-10T10:00:00+00:00",
            )

            with sqlite3.connect(path) as connection:
                child = connection.execute(
                    """
                    SELECT
                        parent_url,
                        attachment_title,
                        document_number,
                        document_date,
                        document_kind
                    FROM crawl_urls
                    WHERE url = ?
                    """,
                    (pdf_url,),
                ).fetchone()

            self.assertEqual(first, (1, 0))
            self.assertEqual(second, (0, 1))
            self.assertEqual(
                child,
                (
                    parent_url,
                    "Постановление № 223",
                    "223",
                    "2019-02-12",
                    "Постановление",
                ),
            )

    def test_migration_adds_version_and_inherits_parent_legal_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crawl.sqlite3"

            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE crawl_urls (
                        url TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        is_active INTEGER NOT NULL,
                        doc_type TEXT,
                        parent_url TEXT,
                        document_number TEXT,
                        document_date TEXT,
                        document_kind TEXT
                    );
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO crawl_urls (
                        url, status, is_active, doc_type, parent_url,
                        document_number, document_date, document_kind
                    )
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            "https://sochi.ru/legal/223/",
                            "indexed",
                            "page",
                            None,
                            "223",
                            "2019-02-12",
                            "Постановление",
                        ),
                        (
                            "https://sochi.ru/upload/223.pdf",
                            "indexed",
                            "pdf",
                            "https://sochi.ru/legal/223/",
                            None,
                            None,
                            None,
                        ),
                    ),
                )

            first = migrate_pdf_ocr_state(path)
            second = migrate_pdf_ocr_state(path)

            self.assertEqual(
                first["version"],
                PDF_EXTRACTION_VERSION,
            )
            self.assertEqual(first["total"], 1)
            self.assertEqual(first["completed"], 0)
            self.assertEqual(first["remaining"], 1)
            self.assertEqual(first["metadata_inherited"], 1)
            self.assertEqual(second["metadata_inherited"], 0)

            with sqlite3.connect(path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(crawl_urls)"
                    ).fetchall()
                }
                metadata = connection.execute(
                    """
                    SELECT
                        document_number,
                        document_date,
                        document_kind,
                        extraction_version
                    FROM crawl_urls
                    WHERE doc_type = 'pdf'
                    """
                ).fetchone()

            self.assertIn(
                "extraction_version",
                columns,
            )
            self.assertEqual(
                metadata,
                (
                    "223",
                    "2019-02-12",
                    "Постановление",
                    None,
                ),
            )

    def test_migration_replaces_false_child_metadata_from_parent_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crawl.sqlite3"

            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE crawl_urls (
                        url TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        is_active INTEGER NOT NULL,
                        doc_type TEXT,
                        parent_url TEXT,
                        document_number TEXT,
                        document_date TEXT,
                        document_kind TEXT,
                        extraction_version TEXT
                    );
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO crawl_urls (
                        url, status, is_active, doc_type, parent_url,
                        document_number, document_date, document_kind
                    )
                    VALUES (?, 'indexed', 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            "https://sochi.ru/legal/?ELEMENT_ID=4638",
                            "page",
                            None,
                            "620-р",
                            "2012-11-30",
                            "Распоряжение",
                        ),
                        (
                            "https://sochi.ru/upload/iblock/3de/document",
                            "pdf",
                            "https://sochi.ru/legal/?ELEMENT_ID=4638",
                            "122",
                            "2012-12-02",
                            "Распоряжение",
                        ),
                    ),
                )

            first = migrate_pdf_ocr_state(path)
            second = migrate_pdf_ocr_state(path)

            with sqlite3.connect(path) as connection:
                metadata = connection.execute(
                    """
                    SELECT
                        document_number,
                        document_date,
                        document_kind
                    FROM crawl_urls
                    WHERE doc_type = 'pdf'
                    """
                ).fetchone()

            self.assertEqual(first["metadata_inherited"], 1)
            self.assertEqual(second["metadata_inherited"], 0)
            self.assertEqual(
                metadata,
                (
                    "620-р",
                    "2012-11-30",
                    "Распоряжение",
                ),
            )

    def test_v4_completion_is_preserved_as_v5(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crawl.sqlite3"

            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE crawl_urls (
                        url TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        is_active INTEGER NOT NULL,
                        doc_type TEXT,
                        extraction_version TEXT
                    );

                    INSERT INTO crawl_urls VALUES (
                        'https://sochi.ru/complete.pdf',
                        'indexed',
                        1,
                        'pdf',
                        'pdf-ocr-v4'
                    );
                    """
                )

            first = migrate_pdf_ocr_state(path)
            second = migrate_pdf_ocr_state(path)

            self.assertEqual(first["version_upgraded"], 1)
            self.assertEqual(first["completed"], 1)
            self.assertEqual(first["remaining"], 0)
            self.assertEqual(second["version_upgraded"], 0)

            with sqlite3.connect(path) as connection:
                version = connection.execute(
                    """
                    SELECT extraction_version
                    FROM crawl_urls
                    """
                ).fetchone()[0]

            self.assertEqual(
                version,
                PDF_EXTRACTION_VERSION,
            )


if __name__ == "__main__":
    unittest.main()
