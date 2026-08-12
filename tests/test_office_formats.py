"""
Тесты извлечения текста из RTF и OpenDocument.

Эти форматы прежде отбрасывались на этапе обнаружения: в очереди
1707 записей со статусом `skipped_unsupported`.

Запуск:

    python -m unittest tests.test_office_formats -v
"""

from __future__ import annotations

import io
import sys
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# PDF-библиотека этим форматам не нужна, а в окружении проверки её может
# не быть. Подставляем заглушку до импорта модуля.
if "pymupdf" not in sys.modules:
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        stub = type(sys)("pymupdf")
        stub.TOOLS = None
        sys.modules["pymupdf"] = stub

from crawler_v2.content_processing import (  # noqa: E402
    CorruptDocumentError,
    EmptyContentError,
    extract_opendocument,
    extract_rtf,
)


def cp1251_escapes(value: str) -> str:
    """Кодирует строку так, как это делает Word в RTF."""
    return "".join(
        "\\'%02x" % byte
        for byte in value.encode("cp1251")
    )


HEADER = cp1251_escapes("АДМИНИСТРАЦИЯ ГОРОДА СОЧИ")
KIND = cp1251_escapes("ПОСТАНОВЛЕНИЕ № 512-р")
DATE = cp1251_escapes("от 14 марта 2024 года")
BODY = cp1251_escapes(
    "Об утверждении порядка предоставления субсидий субъектам "
    "малого и среднего предпринимательства на территории города."
)
AUTHOR = cp1251_escapes("Иванов")

SAMPLE_RTF = (
    r"{\rtf1\ansi\ansicpg1251\deff0"
    r"{\fonttbl{\f0\froman\fcharset204 Times New Roman;}}"
    r"{\colortbl ;\red0\green0\blue0;}"
    r"{\info{\author " + AUTHOR + r"}}"
    r"\viewkind4\uc1\pard\f0\fs28 "
    + HEADER + r"\par "
    + KIND + " " + DATE + r"\par "
    + BODY + r"\par"
    r"}"
).encode("cp1251")


CONTENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
 <office:body><office:text>
  <text:h>АДМИНИСТРАЦИЯ ГОРОДА СОЧИ</text:h>
  <text:p>РАСПОРЯЖЕНИЕ № 84 от 27 января 2021 года</text:p>
  <text:p>Об утверждении порядка предоставления субсидий субъектам
  малого и среднего предпринимательства на территории города.</text:p>
 </office:text></office:body></office:document-content>"""


def make_opendocument(content: str = CONTENT_XML) -> bytes:
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
        )
        archive.writestr("content.xml", content)

    return buffer.getvalue()


class RtfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = extract_rtf(
            SAMPLE_RTF,
            content_type="application/rtf",
            final_url="https://sochi.ru/upload/doc.rtf",
        )
        self.text = self.result.pages[0][1]

    def test_cyrillic_escapes_are_decoded(self) -> None:
        self.assertIn("АДМИНИСТРАЦИЯ ГОРОДА СОЧИ", self.text)
        self.assertIn("субсидий", self.text)

    def test_document_requisites_survive(self) -> None:
        self.assertIn("ПОСТАНОВЛЕНИЕ", self.text)
        self.assertIn("512-р", self.text)
        self.assertIn("14 марта 2024", self.text)

    def test_font_table_is_dropped(self) -> None:
        self.assertNotIn("Times New Roman", self.text)

    def test_metadata_group_is_dropped(self) -> None:
        # Иначе автор файла попадал бы в текст документа.
        self.assertNotIn("Иванов", self.text)

    def test_control_words_are_removed(self) -> None:
        for control in ("\\rtf1", "\\par", "\\viewkind", "\\fs28"):
            self.assertNotIn(control, self.text)

    def test_braces_are_removed(self) -> None:
        self.assertNotIn("{", self.text)
        self.assertNotIn("}", self.text)

    def test_extraction_version_is_reported(self) -> None:
        self.assertEqual(self.result.extraction_version, "rtf-v1")
        self.assertEqual(self.result.doc_type, "file")

    def test_empty_document_raises(self) -> None:
        with self.assertRaises(EmptyContentError):
            extract_rtf(
                rb"{\rtf1\ansi }",
                content_type="application/rtf",
                final_url="https://sochi.ru/x.rtf",
            )

    def test_unicode_escape_is_decoded(self) -> None:
        # Символы вне cp1251 Word пишет как \uN с запасным знаком.
        word = "НОМЕР"
        escaped = "".join(
            "\\u%d?" % ord(character)
            for character in word
        )
        payload = (
            r"{\rtf1\ansi " + BODY + " " + escaped + r"\par}"
        ).encode("cp1251")

        text = extract_rtf(
            payload,
            content_type="application/rtf",
            final_url="https://sochi.ru/x.rtf",
        ).pages[0][1]

        self.assertIn(word, text)

    def test_escaped_braces_survive(self) -> None:
        payload = (
            r"{\rtf1\ansi " + BODY
            + r" \{ \} \\ \par}"
        ).encode("cp1251")

        text = extract_rtf(
            payload,
            content_type="application/rtf",
            final_url="https://sochi.ru/x.rtf",
        ).pages[0][1]

        self.assertIn("предпринимательства", text)


class OpenDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = extract_opendocument(
            make_opendocument(),
            content_type="",
            final_url="https://sochi.ru/upload/a.odt",
        )
        self.text = self.result.pages[0][1]

    def test_text_is_extracted(self) -> None:
        self.assertIn("РАСПОРЯЖЕНИЕ", self.text)
        self.assertIn("27 января 2021", self.text)
        self.assertIn("субсидий", self.text)

    def test_paragraphs_do_not_run_together(self) -> None:
        self.assertNotIn("СОЧИРАСПОРЯЖЕНИЕ", self.text)

    def test_xml_markup_is_absent(self) -> None:
        self.assertNotIn("<", self.text)
        self.assertNotIn("office:", self.text)

    def test_spreadsheet_section_label(self) -> None:
        result = extract_opendocument(
            make_opendocument(),
            content_type="",
            final_url="https://sochi.ru/upload/a.ods",
        )

        self.assertEqual(result.section, "Таблица OpenDocument")

    def test_broken_archive_raises(self) -> None:
        with self.assertRaises(CorruptDocumentError):
            extract_opendocument(
                b"not a zip at all",
                content_type="",
                final_url="https://sochi.ru/x.odt",
            )

    def test_archive_without_content_raises(self) -> None:
        buffer = io.BytesIO()

        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mimetype", "application/x")

        with self.assertRaises(CorruptDocumentError):
            extract_opendocument(
                buffer.getvalue(),
                content_type="",
                final_url="https://sochi.ru/x.odt",
            )


class SupportedSuffixTests(unittest.TestCase):
    def test_new_formats_pass_discovery(self) -> None:
        from crawler_v2.url_policy import (
            SUPPORTED_FILE_SUFFIXES,
            UNSUPPORTED_OFFICE_SUFFIXES,
            is_unsupported_office_url,
        )

        for suffix in (".rtf", ".odt", ".ods"):
            self.assertIn(suffix, SUPPORTED_FILE_SUFFIXES)
            self.assertNotIn(suffix, UNSUPPORTED_OFFICE_SUFFIXES)
            self.assertFalse(
                is_unsupported_office_url(
                    f"https://sochi.ru/upload/a{suffix}"
                )
            )

    def test_binary_word_formats_remain_unsupported(self) -> None:
        from crawler_v2.url_policy import is_unsupported_office_url

        for suffix in (".doc", ".xls"):
            self.assertTrue(
                is_unsupported_office_url(
                    f"https://sochi.ru/upload/a{suffix}"
                )
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
