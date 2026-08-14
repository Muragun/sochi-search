"""
Починка текста после распознавания.

Тесты держат две границы. Первая — что починка действительно чинит то,
ради чего написана. Вторая, важнее, — что она не трогает правильный текст:
любое правило вида «заменить одно на другое» опаснее той ошибки, которую
исправляет, потому что срабатывает на всём корпусе, а не только на браке.
"""

from __future__ import annotations

import sys
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler_v2.text_repair import (
    count_mixed_script_tokens,
    join_hyphenated_line_breaks,
    repair_document_number_suffix,
    repair_mixed_script,
    repair_mixed_script_word,
    repair_ocr_text,
)


class HyphenatedLineBreakTests(unittest.TestCase):
    def test_joins_word_split_across_lines(self) -> None:
        self.assertEqual(
            join_hyphenated_line_breaks(
                "мероприятия по благоустрой-\nству территории"
            ),
            "мероприятия по благоустройству территории",
        )

    def test_joins_when_indentation_follows_break(self) -> None:
        self.assertEqual(
            join_hyphenated_line_breaks(
                "муниципаль-\n    ного образования"
            ),
            "муниципального образования",
        )

    def test_joins_repeated_breaks(self) -> None:
        self.assertEqual(
            join_hyphenated_line_breaks(
                "инфор-\nмационно-телекоммуни-\nкационной сети"
            ),
            "информационно-телекоммуникационной сети",
        )

    def test_keeps_real_hyphen_inside_line(self) -> None:
        """«город-курорт» — не перенос, дефис в нём настоящий."""

        self.assertEqual(
            join_hyphenated_line_breaks(
                "городской округ город-курорт Сочи"
            ),
            "городской округ город-курорт Сочи",
        )

    def test_keeps_hyphen_at_line_end_before_uppercase(self) -> None:
        """Заглавная после переноса — это новое предложение, не хвост слова."""

        self.assertEqual(
            join_hyphenated_line_breaks(
                "утверждена программа-\nПостановление вступает в силу"
            ),
            "утверждена программа-\nПостановление вступает в силу",
        )

    def test_keeps_dash_between_numbers(self) -> None:
        self.assertEqual(
            join_hyphenated_line_breaks("пункты 3-\n14 изложить"),
            "пункты 3-\n14 изложить",
        )


class MixedScriptTests(unittest.TestCase):
    def test_repairs_latin_letters_inside_russian_word(self) -> None:
        # «Сочи» с латинской C и o.
        self.assertEqual(
            repair_mixed_script_word("Cочи"),
            "Сочи",
        )

    def test_repairs_single_latin_letter_in_long_word(self) -> None:
        self.assertEqual(
            repair_mixed_script_word("постановлениe"),
            "постановление",
        )

    def test_repairs_russian_letters_inside_latin_word(self) -> None:
        """Перевес в другую сторону — чиним в латиницу."""

        self.assertEqual(
            repair_mixed_script_word("dосumеnt"),
            "document",
        )

    def test_keeps_pure_russian_word(self) -> None:
        self.assertEqual(
            repair_mixed_script_word("благоустройство"),
            "благоустройство",
        )

    def test_keeps_pure_latin_word(self) -> None:
        self.assertEqual(
            repair_mixed_script_word("resolution"),
            "resolution",
        )

    def test_keeps_word_without_homoglyph_for_every_letter(self) -> None:
        """«ф» латиницей не пишется — токен остаётся нетронутым."""

        original = "фdж"
        self.assertEqual(
            repair_mixed_script_word(original),
            original,
        )

    def test_repairs_whole_text_and_keeps_punctuation(self) -> None:
        repaired = repair_mixed_script(
            'Администрация города Cочи, «Об утверждении» — 2013 год.'
        )

        self.assertEqual(
            repaired,
            'Администрация города Сочи, «Об утверждении» — 2013 год.',
        )

    def test_counts_mixed_tokens(self) -> None:
        self.assertEqual(
            count_mixed_script_tokens("Cочи Сочи pешение решение"),
            2,
        )


class DocumentNumberSuffixTests(unittest.TestCase):
    def test_converts_latin_suffix_to_cyrillic(self) -> None:
        self.assertEqual(
            repair_document_number_suffix("от 14 марта 2024 года № 512-p"),
            "от 14 марта 2024 года № 512-р",
        )

    def test_normalizes_spaces_around_dash(self) -> None:
        self.assertEqual(
            repair_document_number_suffix("№ 512 - p"),
            "№ 512-р",
        )

    def test_keeps_cyrillic_suffix(self) -> None:
        self.assertEqual(
            repair_document_number_suffix("№ 512-р"),
            "№ 512-р",
        )

    def test_keeps_long_latin_suffix(self) -> None:
        """Правило узкое: длинный хвост может быть настоящим кодом."""

        self.assertEqual(
            repair_document_number_suffix("протокол 512-abcd"),
            "протокол 512-abcd",
        )

    def test_keeps_suffix_without_cyrillic_twin(self) -> None:
        self.assertEqual(
            repair_document_number_suffix("№ 131-fz"),
            "№ 131-fz",
        )

    def test_keeps_federal_law_number(self) -> None:
        self.assertEqual(
            repair_document_number_suffix("№ 131-ФЗ"),
            "№ 131-ФЗ",
        )


class RepairOcrTextTests(unittest.TestCase):
    def test_applies_every_stage_in_order(self) -> None:
        raw = (
            "АДМИНИСТРАЦИЯ ГОРОДА CОЧИ\n"
            "ПОСТАНОВЛЕНИЕ от 14 марта 2024 года № 512-p\n"
            "О благоустрой-\nстве территории"
        )

        repaired = repair_ocr_text(raw)

        self.assertIn("ГОРОДА СОЧИ", repaired)
        self.assertIn("№ 512-р", repaired)
        self.assertIn("благоустройстве", repaired)

    def test_keeps_numero_sign_intact(self) -> None:
        """
        NFKC переписал бы «№» в «No».

        Знак номера стоит в шапке каждого акта и виден пользователю в
        карточке результата, поэтому подменять его нельзя.
        """

        self.assertIn("№", repair_ocr_text("Постановление № 512-р"))
        self.assertNotIn("No", repair_ocr_text("Постановление № 512-р"))

    def test_keeps_ellipsis_and_roman_numerals(self) -> None:
        repaired = repair_ocr_text("раздел Ⅳ …")

        self.assertIn("Ⅳ", repaired)
        self.assertIn("…", repaired)

    def test_composes_decomposed_letters(self) -> None:
        decomposed = unicodedata.normalize(
            "NFD",
            "новый район",
        )

        self.assertNotEqual(decomposed, "новый район")
        self.assertEqual(
            repair_ocr_text(decomposed),
            "новый район",
        )

    def test_replaces_non_breaking_space(self) -> None:
        self.assertEqual(
            repair_ocr_text("№ 512"),
            "№ 512",
        )

    def test_removes_soft_hyphen(self) -> None:
        self.assertEqual(
            repair_ocr_text("благо­устройство"),
            "благоустройство",
        )

    def test_joins_word_split_by_soft_hyphen_at_line_end(self) -> None:
        self.assertEqual(
            repair_ocr_text("благоустрой­\nство"),
            "благоустройство",
        )

    def test_empty_input_is_safe(self) -> None:
        self.assertEqual(repair_ocr_text(""), "")
        self.assertEqual(repair_ocr_text(None), "")

    def test_stages_can_be_disabled(self) -> None:
        raw = "благоустрой-\nство Cочи № 512-p"

        self.assertEqual(
            repair_ocr_text(
                raw,
                join_hyphens=False,
                fix_mixed_script=False,
                fix_document_numbers=False,
            ),
            raw,
        )

    def test_leaves_clean_russian_text_unchanged(self) -> None:
        """Главная гарантия: правильный текст проходит насквозь."""

        clean = (
            "В соответствии с Федеральным законом от 6 октября 2003 года "
            "№ 131-ФЗ «Об общих принципах организации местного "
            "самоуправления в Российской Федерации», Уставом города Сочи, "
            "городской округ город-курорт Сочи Краснодарского края "
            "постановляю: 1.1. Паспорт программы изложить в новой редакции."
        )

        self.assertEqual(repair_ocr_text(clean), clean)


if __name__ == "__main__":
    unittest.main()
