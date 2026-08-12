from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from .incremental_db import (
    IncrementalStateDatabase,
)
from .legal_db import LegalMetadataDatabase
from .pdf_ocr import PDF_EXTRACTION_VERSION


def migrate_pdf_ocr_state(
    database_path: Path,
) -> dict[str, Any]:
    if not database_path.is_file():
        raise RuntimeError(
            f"SQLite не найдена: {database_path}"
        )

    IncrementalStateDatabase(
        database_path
    ).migrate()
    LegalMetadataDatabase(
        database_path
    ).migrate()

    connection = sqlite3.connect(
        database_path,
        timeout=60,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(
        "PRAGMA busy_timeout=60000"
    )

    try:
        connection.execute(
            "BEGIN IMMEDIATE"
        )

        inherited = connection.execute(
            """
            UPDATE crawl_urls AS child
            SET
                document_number = COALESCE(
                    (
                        SELECT parent.document_number
                        FROM crawl_urls AS parent
                        WHERE parent.url = child.parent_url
                    ),
                    child.document_number
                ),
                document_date = COALESCE(
                    (
                        SELECT parent.document_date
                        FROM crawl_urls AS parent
                        WHERE parent.url = child.parent_url
                    ),
                    child.document_date
                ),
                document_kind = COALESCE(
                    (
                        SELECT parent.document_kind
                        FROM crawl_urls AS parent
                        WHERE parent.url = child.parent_url
                    ),
                    child.document_kind
                )
            WHERE child.parent_url IS NOT NULL
              AND LOWER(COALESCE(child.doc_type, '')) IN (
                  'pdf',
                  'file'
              )
              AND (
                  (
                      EXISTS (
                          SELECT parent.document_number
                          FROM crawl_urls AS parent
                          WHERE parent.url = child.parent_url
                            AND parent.document_number IS NOT NULL
                            AND child.document_number
                                IS NOT parent.document_number
                      )
                  )
                  OR (
                      EXISTS (
                          SELECT parent.document_date
                          FROM crawl_urls AS parent
                          WHERE parent.url = child.parent_url
                            AND parent.document_date IS NOT NULL
                            AND child.document_date
                                IS NOT parent.document_date
                      )
                  )
                  OR (
                      EXISTS (
                          SELECT parent.document_kind
                          FROM crawl_urls AS parent
                          WHERE parent.url = child.parent_url
                            AND parent.document_kind IS NOT NULL
                            AND child.document_kind
                                IS NOT parent.document_kind
                      )
                  )
              )
            """
        ).rowcount

        upgraded = connection.execute(
            """
            UPDATE crawl_urls
            SET extraction_version = ?
            WHERE LOWER(COALESCE(doc_type, '')) = 'pdf'
              AND extraction_version = 'pdf-ocr-v4'
            """,
            (
                PDF_EXTRACTION_VERSION,
            ),
        ).rowcount

        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN extraction_version = ?
                        THEN 1
                        ELSE 0
                    END
                ) AS completed
            FROM crawl_urls
            WHERE COALESCE(is_active, 1) = 1
              AND LOWER(COALESCE(doc_type, '')) = 'pdf'
            """,
            (
                PDF_EXTRACTION_VERSION,
            ),
        ).fetchone()

        quick_check = str(
            connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
        )

        if quick_check != "ok":
            raise RuntimeError(
                "PDF OCR SQLite quick_check: "
                f"{quick_check}"
            )

        connection.commit()

        total = int(row["total"] or 0)
        completed = int(
            row["completed"] or 0
        )

        return {
            "version": PDF_EXTRACTION_VERSION,
            "total": total,
            "completed": completed,
            "remaining": max(
                0,
                total - completed,
            ),
            "metadata_inherited": int(
                inherited
            ),
            "version_upgraded": int(
                upgraded
            ),
            "sqlite_quick_check": quick_check,
        }

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Подготовка SQLite для PDF OCR"
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Подтвердить изменение SQLite",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    if not arguments.apply:
        print(
            "Требуется --apply; без него "
            "SQLite не изменялась"
        )
        return 2

    result = migrate_pdf_ocr_state(
        arguments.database
    )

    print("PDF_OCR_STATE_MIGRATION=OK")
    print(
        "PDF_EXTRACTION_VERSION="
        f"{result['version']}"
    )
    print(
        "PDF_OCR_TOTAL="
        f"{result['total']}"
    )
    print(
        "PDF_OCR_COMPLETED="
        f"{result['completed']}"
    )
    print(
        "PDF_OCR_REMAINING="
        f"{result['remaining']}"
    )
    print(
        "PDF_METADATA_INHERITED="
        f"{result['metadata_inherited']}"
    )
    print(
        "PDF_OCR_VERSION_UPGRADED="
        f"{result['version_upgraded']}"
    )
    print(
        "PDF_OCR_SQLITE_QUICK_CHECK="
        f"{result['sqlite_quick_check']}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
