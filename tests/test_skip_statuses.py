"""
Статусы пропуска: обходчик и база должны договориться.

Обходчик ставит `skipped_too_large`, панель управления его показывает, а
`mark_skipped` в базе такого статуса не принимала — падала с ValueError.
Очередь разбирается по порядку, поэтому воркер падал на одном и том же
документе каждый запуск. На рабочем сервере это остановило индексацию
целиком: в очереди попался PDF на 237 МБ.

Функция была написана в трёх файлах, и ровно в одном её забыли. Ни один
тест этого не видел: `tests/test_large_pdf_fetch.py` проверяет, что
загрузчик бросает исключение, но не то, что происходит дальше.

Поэтому здесь проверяется не список, а согласие списков: все статусы,
которые обходчик действительно передаёт, обязаны приниматься базой.
Список берётся из исходника обходчика, чтобы не устареть отдельно от него.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from crawler_v2.incremental_db import (
    IncrementalStateDatabase,
    iso_after_hours,
    iso_now,
)

SOURCE = Path(__file__).resolve().parents[1]


def statuses_worker_passes() -> set:
    """Значения `status=` во всех вызовах mark_skipped у обходчика."""

    source = (
        SOURCE / "crawler_v2" / "incremental_worker.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: set = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        name = getattr(node.func, "attr", None)

        if name != "mark_skipped":
            continue

        for keyword in node.keywords:
            if keyword.arg != "status":
                continue

            if isinstance(keyword.value, ast.Constant):
                found.add(keyword.value.value)

    return found


def statuses_database_accepts() -> set:
    """Множество allowed_statuses из mark_skipped."""

    source = (
        SOURCE / "crawler_v2" / "incremental_db.py"
    ).read_text(encoding="utf-8")

    block = re.search(
        r"allowed_statuses = \{(.*?)\}",
        source,
        flags=re.S,
    )

    assert block is not None, "не найден allowed_statuses"

    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


class SkipStatusAgreementTests(unittest.TestCase):
    def test_worker_passes_at_least_one_status(self) -> None:
        """Если разбор сломается, остальные проверки станут пустыми."""

        self.assertTrue(statuses_worker_passes())

    def test_database_accepts_every_status_worker_passes(self) -> None:
        worker = statuses_worker_passes()
        database = statuses_database_accepts()

        self.assertEqual(
            sorted(worker - database),
            [],
            msg=(
                "Обходчик ставит статусы, которых база не принимает. "
                "mark_skipped бросит ValueError, и воркер упадёт на "
                "первом же таком документе — и будет падать на нём "
                "каждый запуск"
            ),
        )

    def test_too_large_is_among_them(self) -> None:
        """Тот самый случай, ради которого написан файл."""

        self.assertIn("skipped_too_large", statuses_worker_passes())
        self.assertIn("skipped_too_large", statuses_database_accepts())


class MarkSkippedTests(unittest.TestCase):
    """Проверка на настоящей базе, а не только по исходникам."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"

        # Базовую таблицу создаёт этап установки, миграция её только
        # дополняет. Берём то же определение, что и остальные тесты.
        from test_crawler_runtime import CRAWL_SCHEMA

        connection = sqlite3.connect(self.path)

        try:
            connection.executescript(CRAWL_SCHEMA)
            connection.commit()
        finally:
            connection.close()

        self.database = IncrementalStateDatabase(self.path)
        self.database.migrate()

        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO crawl_urls (url, status, is_active)
                VALUES (?, 'discovered', 1)
                """,
                ("https://sochi.ru/upload/huge.pdf",),
            )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def row(self) -> sqlite3.Row:
        with self.database.connect() as connection:
            connection.row_factory = sqlite3.Row

            return connection.execute(
                "SELECT * FROM crawl_urls WHERE url = ?",
                ("https://sochi.ru/upload/huge.pdf",),
            ).fetchone()

    def mark(self, status: str) -> None:
        self.database.mark_skipped(
            "https://sochi.ru/upload/huge.pdf",
            status=status,
            checked_at=iso_now(),
            next_check_at=iso_after_hours(672),
            error_message="Размер PDF 237596584 байт превышает предел",
        )

    def test_too_large_is_accepted(self) -> None:
        self.mark("skipped_too_large")

        self.assertEqual(self.row()["status"], "skipped_too_large")

    def test_too_large_is_terminal(self) -> None:
        """
        Файл меньше не станет — повторять его загрузку незачем.

        237 МБ на каждый повтор ради того же отказа: это и трафик, и
        занятый воркер на десятки минут.
        """

        self.mark("skipped_too_large")
        row = self.row()

        self.assertEqual(row["is_active"], 0)
        self.assertIsNone(row["next_check_at"])

    def test_unknown_status_is_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.mark("skipped_whatever")


if __name__ == "__main__":
    unittest.main()
