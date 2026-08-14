"""
Номер акта: индекс и запрос обязаны сходиться.

Один и тот же акт записан на сайте десятком способов: «512-р», «№ 512-р»,
«512 – Р», иногда с латинской «p» из распознавания. Пользователь набирает
любой из них и должен получить документ.

Совпадение обеспечивают две стороны, и раньше они расходились. Запрос
нормализовал номер в Python, индекс — нормализатором в схеме, и делали они
разное: «№ 512-р» попадал в индекс как «№  512-р», а искался как «512-р».
Точное совпадение с весом 40 срабатывало на двух написаниях из семи.

Здесь проверяется и Python-нормализация, и то, что цепочка char_filter из
схемы приводит к тому же результату. Живого кластера нет, поэтому
`mapping`-фильтр воспроизводится по его правилам: один проход слева
направо, выигрывает самое длинное совпадение, результат замены заново не
просматривается.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler_v2.text_repair import normalize_document_number

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "elasticsearch"
    / "sochi_docs_v4.json"
)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def decode_rule_side(value: str) -> str:
    """Разворачивает \\uXXXX, которыми в схеме записаны пробелы."""

    result = value
    index = 0

    while True:
        index = result.find("\\u", index)

        if index < 0:
            return result

        code = result[index + 2: index + 6]

        if len(code) == 4:
            try:
                result = (
                    result[:index]
                    + chr(int(code, 16))
                    + result[index + 6:]
                )
                index += 1
                continue
            except ValueError:
                pass

        index += 2


def parse_mapping_rules(mappings: list[str]) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []

    for line in mappings:
        source, _, target = line.partition("=>")
        rules.append(
            (
                decode_rule_side(source.strip()),
                decode_rule_side(target.strip()),
            )
        )

    # Самое длинное совпадение вперёд: так работает mapping char_filter.
    rules.sort(key=lambda item: len(item[0]), reverse=True)

    return rules


def apply_mapping_char_filter(
    value: str,
    rules: list[tuple[str, str]],
) -> str:
    """Один проход слева направо; замена заново не просматривается."""

    result: list[str] = []
    position = 0

    while position < len(value):
        for source, target in rules:
            if source and value.startswith(source, position):
                result.append(target)
                position += len(source)
                break
        else:
            result.append(value[position])
            position += 1

    return "".join(result)


def elasticsearch_normalize(value: str) -> str:
    """Повторяет document_number_normalizer: char_filter, lowercase, trim."""

    schema = load_schema()
    analysis = schema["settings"]["index"]["analysis"]
    normalizer = analysis["normalizer"]["document_number_normalizer"]

    result = value

    for name in normalizer["char_filter"]:
        rules = parse_mapping_rules(
            analysis["char_filter"][name]["mappings"]
        )
        result = apply_mapping_char_filter(result, rules)

    for token_filter in normalizer["filter"]:
        if token_filter == "lowercase":
            result = result.lower()
        elif token_filter == "trim":
            result = result.strip()
        else:
            raise AssertionError(
                f"Неизвестный фильтр нормализатора: {token_filter}"
            )

    return result


# Как номер встречается в источниках: заголовок акта, строка «Номер
# документа», распознанный скан.
REALISTIC_SPELLINGS = (
    "512-р",
    "512-Р",
    "№ 512-р",
    "№512-р",
    "512 - р",
    "512 – р",
    "512–Р",
    "512-p",
    "№ 512 – Р",
    "  512-р  ",
)


class PythonNormalizationTests(unittest.TestCase):
    def test_every_spelling_collapses_to_one_form(self) -> None:
        for spelling in REALISTIC_SPELLINGS:
            self.assertEqual(
                normalize_document_number(spelling),
                "512-р",
                msg=spelling,
            )

    def test_latin_lookalikes_become_cyrillic(self) -> None:
        self.assertEqual(normalize_document_number("84-PC"), "84-рс")

    def test_federal_law_number_survives(self) -> None:
        self.assertEqual(
            normalize_document_number("131-ФЗ"),
            "131-фз",
        )

    def test_empty_value_is_empty(self) -> None:
        self.assertEqual(normalize_document_number(""), "")
        self.assertEqual(normalize_document_number(None), "")


class SchemaNormalizationTests(unittest.TestCase):
    """
    Нормализатор схемы обязан давать то же, что Python.

    Это не проверка Elasticsearch, а проверка того, что цепочка
    `mapping`-правил в схеме описывает ту же нормализацию. Расхождение
    здесь означает, что документы, перенесённые со старой схемы, снова
    перестанут находиться по точному совпадению.
    """

    def test_schema_matches_python_on_every_spelling(self) -> None:
        for spelling in REALISTIC_SPELLINGS:
            self.assertEqual(
                elasticsearch_normalize(spelling),
                normalize_document_number(spelling),
                msg=(
                    f"«{spelling}»: нормализатор схемы и функция запроса "
                    "разошлись"
                ),
            )

    def test_schema_collapses_spellings_to_one_term(self) -> None:
        terms = {
            elasticsearch_normalize(spelling)
            for spelling in REALISTIC_SPELLINGS
        }

        self.assertEqual(terms, {"512-р"})

    def test_schema_keeps_cyrillic_suffix_untouched(self) -> None:
        self.assertEqual(
            elasticsearch_normalize("131-ФЗ"),
            "131-фз",
        )


class SchemaContractTests(unittest.TestCase):
    def test_normalized_field_is_declared(self) -> None:
        properties = load_schema()["mappings"]["properties"]

        self.assertIn("document_number_normalized", properties)
        self.assertEqual(
            properties["document_number_normalized"]["type"],
            "keyword",
        )

    def test_graph_filter_is_flattened_on_index_side(self) -> None:
        """
        `word_delimiter_graph` на индексной стороне требует `flatten_graph`.

        Без него позиции многословных вариантов («512р» рядом с «512» и
        «р») записываются в индекс как граф, а обратный индекс графов не
        хранит: часть вариантов теряется молча.
        """

        analysis = load_schema()["settings"]["index"]["analysis"]
        index_analyzer = analysis["analyzer"]["document_number_index"]

        self.assertIn("legal_word_delimiter", index_analyzer["filter"])
        self.assertIn("flatten_number_graph", index_analyzer["filter"])
        self.assertLess(
            index_analyzer["filter"].index("legal_word_delimiter"),
            index_analyzer["filter"].index("flatten_number_graph"),
            "flatten_graph обязан идти после word_delimiter_graph",
        )

    def test_search_analyzer_keeps_the_graph(self) -> None:
        """На стороне запроса граф нужен: там он разворачивается в клаузы."""

        properties = load_schema()["mappings"]["properties"]
        subfield = properties["document_number"]["fields"]["text"]

        self.assertEqual(subfield["analyzer"], "document_number_index")
        self.assertEqual(
            subfield["search_analyzer"],
            "document_number_text",
        )


if __name__ == "__main__":
    unittest.main()
