"""
Показания прогресса переноса индекса.

Перенос запускается нарезанным на срезы (`slices`). У такой задачи
Elasticsearch держит счётчики в подзадачах, а верхнеуровневые оставляет
нулями. Монитор читал только верхние и печатал «0/0 (0.0%)» до самого
конца — на рабочем кластере это выглядело как зависший перенос, и его
прервали руками, хотя он шёл нормально.

Ошибка безобидная на вид и дорогая по последствиям: миграцию боевого
индекса останавливают, потому что индикатор врёт.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.reindex import task_progress


class SlicedTaskProgressTests(unittest.TestCase):
    def test_counts_are_summed_across_slices(self) -> None:
        done, total = task_progress(
            {
                "total": 0,
                "created": 0,
                "updated": 0,
                "slices": [
                    {"total": 30000, "created": 12000, "updated": 0},
                    {"total": 27636, "created": 9000, "updated": 500},
                ],
            }
        )

        self.assertEqual(total, 57636)
        self.assertEqual(done, 21500)

    def test_slice_that_has_not_started_is_skipped(self) -> None:
        """Не начавшийся срез приходит как null, а не как словарь нулей."""

        done, total = task_progress(
            {
                "slices": [
                    {"total": 30000, "created": 15000, "updated": 0},
                    None,
                ]
            }
        )

        self.assertEqual(total, 30000)
        self.assertEqual(done, 15000)

    def test_unsliced_task_uses_top_level_counters(self) -> None:
        done, total = task_progress(
            {
                "total": 57636,
                "created": 40000,
                "updated": 1000,
            }
        )

        self.assertEqual(total, 57636)
        self.assertEqual(done, 41000)

    def test_empty_status_does_not_raise(self) -> None:
        self.assertEqual(task_progress({}), (0, 0))

    def test_empty_slice_list_falls_back_to_top_level(self) -> None:
        done, total = task_progress(
            {"slices": [], "total": 10, "created": 4, "updated": 0}
        )

        self.assertEqual((done, total), (4, 10))

    def test_progress_is_not_zero_while_slices_work(self) -> None:
        """
        Тот самый случай.

        Верхние счётчики нулевые, работа идёт. Прежний монитор печатал
        «0/0» и создавал впечатление зависшей задачи.
        """

        status = {
            "total": 0,
            "created": 0,
            "updated": 0,
            "slices": [
                {"total": 28818, "created": 5000, "updated": 0},
                {"total": 28818, "created": 4800, "updated": 0},
            ],
        }

        done, total = task_progress(status)

        self.assertGreater(done, 0)
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main()
