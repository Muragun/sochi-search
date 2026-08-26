"""
Перепостановка документов в очередь.

Когда меняется разбор, а не сайт — добавили извлечение реквизитов из
офисных файлов, научились отличать машинное имя от осмысленного, — уже
загруженные документы остаются в индексе со старыми полями. Перечитывать
их нечему: сайт не менялся.

Сдвинуть `next_check_at` недостаточно. Воркер сравнивает хеш содержимого
с прошлым и при совпадении помечает документ неизменившимся и выходит,
не переразбирая, — а при живом `ETag` сервер и вовсе ответит 304, и
содержимое не доедет. Поэтому сбрасывается и хеш, и условные заголовки.

Опаснее всего здесь широкий отбор: лишние десятки тысяч HTML-страниц в
очереди — это сутки нагрузки на общий сервер. Поэтому тестов на то, что
`не` попало в отбор, больше, чем на то, что попало.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops.requeue import build_conditions, count_matching, requeue
from test_crawler_runtime import CRAWL_SCHEMA


class Arguments:
    """Замена argparse.Namespace с нужными полями."""

    def __init__(self, **values) -> None:
        self.office = values.get("office", False)
        self.pdf = values.get("pdf", False)
        self.page = values.get("page", False)
        self.attachments = values.get("attachments", False)
        self.missing_requisites = values.get(
            "missing_requisites", False
        )
        self.url_like = values.get("url_like", "")


# Что лежит в базе: адрес, тип, статус, есть ли реквизиты.
ROWS = (
    ("https://sochi.ru/upload/a.docx", "file", "indexed", False),
    ("https://sochi.ru/upload/b.docx", "file", "indexed", True),
    ("https://sochi.ru/upload/c.pdf", "pdf", "indexed", False),
    ("https://sochi.ru/upload/d.pdf", "pdf", "indexed", True),
    ("https://sochi.ru/news/1.html", "page", "indexed", False),
    ("https://sochi.ru/upload/e.docx", "file", "discovered", False),
    ("https://sochi.ru/upload/f.docx", "file", "gone", False),
)


class RequeueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"

        connection = sqlite3.connect(self.path)
        connection.executescript(CRAWL_SCHEMA)

        for url, doc_type, status, has_requisites in ROWS:
            connection.execute(
                """
                INSERT INTO crawl_urls (
                    url, status, is_active, doc_type,
                    content_hash, etag, last_modified,
                    document_number, document_date, document_kind
                )
                VALUES (?, ?, 1, ?, 'hash', 'etag', 'modified', ?, ?, ?)
                """,
                (
                    url,
                    status,
                    doc_type,
                    "512-р" if has_requisites else None,
                    "2024-03-14" if has_requisites else None,
                    "Постановление" if has_requisites else None,
                ),
            )

        connection.commit()
        connection.close()

        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.connection.close()
        self.directory.cleanup()

    def selected(self, **values) -> set:
        where, parameters = build_conditions(Arguments(**values))
        rows = self.connection.execute(
            f"SELECT url FROM crawl_urls WHERE {where}",
            parameters,
        ).fetchall()

        return {str(row["url"]) for row in rows}

    def row(self, url: str) -> sqlite3.Row:
        return self.connection.execute(
            "SELECT * FROM crawl_urls WHERE url = ?",
            (url,),
        ).fetchone()

    def apply(self, **values) -> int:
        where, parameters = build_conditions(Arguments(**values))

        return requeue(self.connection, where, parameters, limit=0)

    # --- отбор ---

    def test_office_selects_only_office(self) -> None:
        self.assertEqual(
            self.selected(office=True),
            {
                "https://sochi.ru/upload/a.docx",
                "https://sochi.ru/upload/b.docx",
            },
        )

    def test_missing_requisites_narrows_further(self) -> None:
        self.assertEqual(
            self.selected(office=True, missing_requisites=True),
            {"https://sochi.ru/upload/a.docx"},
        )

    def test_pages_are_not_touched_by_default(self) -> None:
        """
        Главная защита.

        HTML-страниц больше всего, и перечитывать их заодно с вложениями
        значит поставить в очередь десятки тысяч загрузок.
        """

        for values in (
            {"office": True},
            {"pdf": True},
            {"attachments": True},
        ):
            self.assertNotIn(
                "https://sochi.ru/news/1.html",
                self.selected(**values),
                msg=str(values),
            )

    def test_attachments_covers_both_kinds(self) -> None:
        self.assertEqual(
            self.selected(attachments=True, missing_requisites=True),
            {
                "https://sochi.ru/upload/a.docx",
                "https://sochi.ru/upload/c.pdf",
            },
        )

    def test_unprocessed_rows_are_skipped(self) -> None:
        """Документ, который и так ждёт очереди, трогать незачем."""

        self.assertNotIn(
            "https://sochi.ru/upload/e.docx",
            self.selected(office=True),
        )

    def test_terminal_rows_are_skipped(self) -> None:
        self.assertNotIn(
            "https://sochi.ru/upload/f.docx",
            self.selected(office=True),
        )

    def test_url_filter_narrows_selection(self) -> None:
        self.assertEqual(
            self.selected(office=True, url_like="%/a.docx"),
            {"https://sochi.ru/upload/a.docx"},
        )

    # --- применение ---

    def test_hash_is_cleared(self) -> None:
        """
        Без этого воркер увидит совпадение хеша и не переразберёт.

        Реквизиты и заголовок остались бы прежними, а смысл перепостановки
        именно в них.
        """

        self.apply(office=True, missing_requisites=True)
        row = self.row("https://sochi.ru/upload/a.docx")

        self.assertIsNone(row["content_hash"])

    def test_conditional_headers_are_cleared(self) -> None:
        """Иначе сервер ответит 304 и содержимое не доедет."""

        self.apply(office=True, missing_requisites=True)
        row = self.row("https://sochi.ru/upload/a.docx")

        self.assertIsNone(row["etag"])
        self.assertIsNone(row["last_modified"])

    def test_row_becomes_due(self) -> None:
        self.apply(office=True, missing_requisites=True)
        row = self.row("https://sochi.ru/upload/a.docx")

        self.assertIsNotNone(row["next_check_at"])

    def test_other_rows_are_untouched(self) -> None:
        self.apply(office=True, missing_requisites=True)

        for url in (
            "https://sochi.ru/upload/b.docx",
            "https://sochi.ru/upload/c.pdf",
            "https://sochi.ru/news/1.html",
        ):
            self.assertEqual(
                self.row(url)["content_hash"],
                "hash",
                msg=url,
            )

    def test_limit_bounds_the_batch(self) -> None:
        where, parameters = build_conditions(
            Arguments(attachments=True, missing_requisites=True)
        )

        self.assertEqual(
            requeue(self.connection, where, parameters, limit=1),
            1,
        )

    def test_count_groups_by_kind(self) -> None:
        where, parameters = build_conditions(
            Arguments(attachments=True, missing_requisites=True)
        )

        self.assertEqual(
            count_matching(self.connection, where, parameters),
            {"file": 1, "pdf": 1},
        )

    def test_missing_column_does_not_break(self) -> None:
        """
        `source_hash` добавляется миграцией и есть не в каждой базе.

        Инструмент определяет состав столбцов, а не предполагает его:
        на базе без миграции первая же попытка падала с
        «no such column: source_hash».
        """

        self.assertNotIn(
            "source_hash",
            {
                str(row["name"])
                for row in self.connection.execute(
                    "PRAGMA table_info(crawl_urls)"
                ).fetchall()
            },
        )

        self.assertEqual(
            self.apply(office=True, missing_requisites=True),
            1,
        )


if __name__ == "__main__":
    unittest.main()
