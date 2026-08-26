"""
Заголовки и реквизиты вложений.

На рабочем сервере вкладка «Файлы и PDF» выглядела так:

    028e97063df6b807dc50e5901603af2e.docx    Файл · Вложение · стр. 1
    a17bf7cba87e0121c7f89362f77d1ab2.docx    Файл · Вложение · стр. 1
    526d358fe7e29173cf3cfcd0234463d8.docx    Файл · Вложение · стр. 1

Столбец неразличимых строк без даты, номера и вида акта — по такой
выдаче выбрать нужный документ нельзя.

Причин было две, и обе тихие.

Фильтр технических имён заканчивался на `(?:\\.pdf)?`, поэтому хеш с
расширением `.docx` он не узнавал и пропускал как осмысленное название.
А Битрикс раскладывает вложения по `/upload/iblock/` именно так, причём
не только в hex: `tg7dcgl1954sjipys673nrc12y1zd6o0.docx`.

Реквизиты же извлекались только из PDF: `extract_legal_title_metadata`
вызывался ровно в одном месте — внутри разбора PDF. Офисные файлы
получали номер и дату исключительно по наследству от родительской
HTML-карточки, а найденные прямо в `/upload/` не получали ничего, хотя
внутри у них ровно тот же текст «ПОСТАНОВЛЕНИЕ … от … № …».
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_v2.search_service import get_title, is_generic_title
from crawler_v2.content_processing import (
    describe_document,
    document_title_from_url,
    legal_title_from_text,
)
from crawler_v2.text_repair import technical_filename

# Имена, снятые с рабочей выдачи.
MACHINE_NAMES = (
    "028e97063df6b807dc50e5901603af2e.docx",
    "a17bf7cba87e0121c7f89362f77d1ab2.docx",
    "526d358fe7e29173cf3cfcd0234463d8.docx",
    "tg7dcgl1954sjipys673nrc12y1zd6o0.docx",
    "15u3668pupl2w2srntgjoa2je119o12v.pdf",
)

HUMAN_NAMES = (
    "postanovlenie-512-r.pdf",
    "Отчёт за 2024 год.xlsx",
    "prilozhenie1.docx",
    "2024_plan_meropriyatiy.docx",
    "устав.docx",
)

ACT_TEXT = (
    "ПОСТАНОВЛЕНИЕ администрации города Сочи "
    "от 14 марта 2024 года № 512-р "
    "«Об утверждении порядка рассмотрения обращений граждан». "
    "В соответствии с Федеральным законом от 6 октября 2003 года "
    "№ 131-ФЗ постановляю: ..."
)


class TechnicalFilenameTests(unittest.TestCase):
    def test_machine_names_are_recognised(self) -> None:
        for name in MACHINE_NAMES:
            self.assertTrue(
                technical_filename(name),
                msg=f"{name} должно считаться техническим",
            )

    def test_human_names_are_left_alone(self) -> None:
        """Важнее первого: ложное срабатывание съедает настоящее имя."""

        for name in HUMAN_NAMES:
            self.assertFalse(
                technical_filename(name),
                msg=f"{name} — осмысленное имя, трогать нельзя",
            )

    def test_extension_is_not_limited_to_pdf(self) -> None:
        """Та самая причина: прежний фильтр знал только `.pdf`."""

        stem = "028e97063df6b807dc50e5901603af2e"

        for extension in ("pdf", "docx", "xlsx", "rtf", "odt"):
            self.assertTrue(
                technical_filename(f"{stem}.{extension}"),
                msg=extension,
            )

    def test_letters_beyond_hex_are_recognised(self) -> None:
        """Битрикс генерирует не только шестнадцатеричные имена."""

        self.assertTrue(
            technical_filename("tg7dcgl1954sjipys673nrc12y1zd6o0.docx")
        )

    def test_short_names_are_not_technical(self) -> None:
        for name in ("plan.docx", "akt2024.pdf", "1.pdf"):
            self.assertFalse(technical_filename(name), msg=name)

    def test_empty_value_is_safe(self) -> None:
        self.assertFalse(technical_filename(""))
        self.assertFalse(technical_filename(None))


class TitleFromUrlTests(unittest.TestCase):
    def test_machine_name_yields_no_title(self) -> None:
        self.assertEqual(
            document_title_from_url(
                "https://sochi.ru/upload/iblock/491/"
                "028e97063df6b807dc50e5901603af2e.docx"
            ),
            "",
        )

    def test_human_name_is_kept(self) -> None:
        self.assertEqual(
            document_title_from_url(
                "https://sochi.ru/upload/postanovlenie-512-r.pdf"
            ),
            "postanovlenie-512-r.pdf",
        )


class TitleFromTextTests(unittest.TestCase):
    def test_title_starts_at_the_kind_of_act(self) -> None:
        title = legal_title_from_text(
            "Приложение 3 к письму. ПОСТАНОВЛЕНИЕ администрации "
            "города Сочи от 14 марта 2024 года № 512-р «Об "
            "утверждении порядка» . Далее текст."
        )

        self.assertTrue(title.startswith("ПОСТАНОВЛЕНИЕ"))
        self.assertIn("512-р", title)

    def test_title_is_bounded(self) -> None:
        title = legal_title_from_text("Постановление " + "слово " * 200)

        self.assertLessEqual(len(title), 240)

    def test_text_without_act_still_gives_something(self) -> None:
        title = legal_title_from_text(
            "Итоги работы с обращениями граждан за 2017 год. "
            "Таблица показателей."
        )

        self.assertIn("Итоги работы", title)

    def test_empty_text_is_safe(self) -> None:
        self.assertEqual(legal_title_from_text(""), "")


class DescribeDocumentTests(unittest.TestCase):
    """Главное: офисный файл получает реквизиты из своего текста."""

    def describe(self, url: str, text: str = ACT_TEXT):
        return describe_document(text, url)

    def test_office_document_gets_requisites(self) -> None:
        _, metadata = self.describe(
            "https://sochi.ru/upload/iblock/491/"
            "028e97063df6b807dc50e5901603af2e.docx"
        )

        self.assertEqual(metadata.document_number, "512-р")
        self.assertEqual(metadata.document_date, "2024-03-14")
        self.assertEqual(
            (metadata.document_kind or "").casefold(),
            "постановление",
        )

    def test_machine_name_is_replaced_by_text_title(self) -> None:
        title, _ = self.describe(
            "https://sochi.ru/upload/iblock/491/"
            "028e97063df6b807dc50e5901603af2e.docx"
        )

        self.assertNotIn("028e97063", title)
        self.assertIn("ПОСТАНОВЛЕНИЕ", title)

    def test_human_filename_wins_over_text(self) -> None:
        """Осмысленное имя файла информативнее первой строки."""

        title, _ = self.describe(
            "https://sochi.ru/upload/postanovlenie-512-r.pdf"
        )

        self.assertEqual(title, "postanovlenie-512-r.pdf")

    def test_explicit_title_wins_over_everything(self) -> None:
        title, _ = describe_document(
            ACT_TEXT,
            "https://sochi.ru/upload/iblock/491/"
            "028e97063df6b807dc50e5901603af2e.docx",
            fallback_title="Название с карточки страницы",
        )

        self.assertEqual(title, "Название с карточки страницы")

    def test_document_without_requisites_does_not_invent_them(self) -> None:
        _, metadata = describe_document(
            "Итоги работы с обращениями граждан за 2017 год.",
            "https://sochi.ru/upload/itogi.docx",
        )

        self.assertIsNone(metadata.document_number)
        self.assertIsNone(metadata.document_date)


class SearchDisplayTests(unittest.TestCase):
    """
    Уже проиндексированные записи чинятся на стороне выдачи.

    В индексе лежат документы, собранные до этой правки, и переобход
    всего корпуса ради заголовков никто затевать не станет.
    """

    def test_machine_name_is_generic(self) -> None:
        for name in MACHINE_NAMES:
            self.assertTrue(is_generic_title(name), msg=name)

    def test_human_name_is_not_generic(self) -> None:
        for name in HUMAN_NAMES:
            self.assertFalse(is_generic_title(name), msg=name)

    def test_title_falls_back_to_requisites(self) -> None:
        title = get_title(
            {
                "title": "028e97063df6b807dc50e5901603af2e.docx",
                "document_kind": "постановление",
                "document_number": "512-р",
                "document_date": "2024-03-14",
            },
            "",
        )

        self.assertNotIn("028e97063", title)
        self.assertIn("512-р", title)
        self.assertIn("14 марта 2024", title)

    def test_title_falls_back_to_text_when_no_requisites(self) -> None:
        title = get_title(
            {"title": "028e97063df6b807dc50e5901603af2e.docx"},
            ACT_TEXT,
        )

        self.assertNotIn("028e97063", title)
        self.assertIn("постановление", title.casefold())


if __name__ == "__main__":
    unittest.main()
