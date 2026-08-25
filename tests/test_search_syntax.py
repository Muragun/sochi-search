"""
Кавычки и минус-слова в строке поиска.

До этого и то, и другое попадало в `multi_match` как обычные символы:
`standard`-токенизатор их выбрасывал, и «состав семьи» в кавычках искалось
ровно так же, как без кавычек. Сотрудник, который взял фразу в кавычки,
хотел сузить выдачу, а получал ту же самую — и не имел способа об этом
узнать.

Отдельная забота — не сломать то, что работало. Минус стоит внутри «512-р»
и «город-курорт», и принять его за исключение нельзя.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_v2.search_query import (
    build_query,
    build_search_body,
    parse_query,
)


def clauses_of(body: dict, section: str) -> list:
    return body.get("query", {}).get("bool", {}).get(section, []) or []


def as_text(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class ParseQueryTests(unittest.TestCase):
    def test_plain_query_has_no_phrases_or_exclusions(self) -> None:
        parsed = parse_query("благоустройство территории")

        self.assertEqual(parsed.text, "благоустройство территории")
        self.assertEqual(parsed.phrases, ())
        self.assertEqual(parsed.excluded, ())

    def test_double_quotes_become_a_phrase(self) -> None:
        parsed = parse_query('"состав семьи"')

        self.assertEqual(parsed.phrases, ("состав семьи",))
        self.assertEqual(parsed.text, "")

    def test_russian_quotes_become_a_phrase(self) -> None:
        """Ёлочки: их и ставят в русской раскладке, а не прямые кавычки."""

        parsed = parse_query("«справка о составе семьи»")

        self.assertEqual(
            parsed.phrases,
            ("справка о составе семьи",),
        )
        self.assertEqual(parsed.text, "")

    def test_phrase_and_free_words_coexist(self) -> None:
        parsed = parse_query('«состав семьи» справка 2024')

        self.assertEqual(parsed.phrases, ("состав семьи",))
        self.assertEqual(parsed.text, "справка 2024")

    def test_minus_word_becomes_exclusion(self) -> None:
        parsed = parse_query("благоустройство -конкурс")

        self.assertEqual(parsed.text, "благоустройство")
        self.assertEqual(parsed.excluded, ("конкурс",))

    def test_minus_at_the_start_is_an_exclusion(self) -> None:
        parsed = parse_query("-конкурс благоустройство")

        self.assertEqual(parsed.excluded, ("конкурс",))
        self.assertEqual(parsed.text, "благоустройство")

    def test_document_number_keeps_its_hyphen(self) -> None:
        """«512-р» — номер акта, а не исключение слова «р»."""

        parsed = parse_query("постановление 512-р")

        self.assertEqual(parsed.excluded, ())
        self.assertEqual(parsed.text, "постановление 512-р")

    def test_compound_word_keeps_its_hyphen(self) -> None:
        parsed = parse_query("город-курорт Сочи")

        self.assertEqual(parsed.excluded, ())
        self.assertEqual(parsed.text, "город-курорт Сочи")

    def test_repeated_phrases_are_collapsed(self) -> None:
        parsed = parse_query('«состав семьи» «состав семьи»')

        self.assertEqual(parsed.phrases, ("состав семьи",))

    def test_unbalanced_quote_is_not_a_phrase(self) -> None:
        parsed = parse_query('состав семьи"')

        self.assertEqual(parsed.phrases, ())
        self.assertIn("состав", parsed.text)


class PhraseQueryTests(unittest.TestCase):
    def body(self, query: str) -> dict:
        return build_search_body(query, limit=10, offset=0)

    def test_phrase_is_required_not_merely_boosted(self) -> None:
        body = self.body('«состав семьи»')
        must = clauses_of(body, "must")

        self.assertEqual(len(must), 1)
        self.assertIn("match_phrase", as_text(must[0]))

    def test_phrase_checked_against_exact_and_stemmed_fields(self) -> None:
        """
        Обе формы нужны.

        Только `.exact` отсекала бы «о благоустройстве территории» при
        запросе «благоустройство территории». Только лемма стирала бы
        разницу, ради которой кавычки и ставят.
        """

        rendered = as_text(clauses_of(self.body('«состав семьи»'), "must"))

        self.assertIn('"text.exact"', rendered)
        self.assertIn('"text"', rendered)
        self.assertIn('"title.exact"', rendered)
        self.assertIn('"attachment_title.exact"', rendered)

    def test_free_words_only_rank_when_a_phrase_is_present(self) -> None:
        body = self.body('«состав семьи» справка')

        self.assertEqual(len(clauses_of(body, "must")), 1)
        self.assertIn("справка", as_text(clauses_of(body, "should")))

    def test_quotes_do_not_reach_elasticsearch(self) -> None:
        rendered = as_text(self.body('«состав семьи»'))

        self.assertNotIn("«", rendered)
        self.assertNotIn("»", rendered)


class ExclusionQueryTests(unittest.TestCase):
    def body(self, query: str) -> dict:
        return build_search_body(query, limit=10, offset=0)

    def test_excluded_term_becomes_must_not(self) -> None:
        body = self.body("благоустройство -конкурс")
        must_not = clauses_of(body, "must_not")

        self.assertEqual(len(must_not), 1)
        self.assertIn("конкурс", as_text(must_not))

    def test_remaining_words_stay_required(self) -> None:
        body = self.body("благоустройство -конкурс")

        self.assertIn("благоустройство", as_text(clauses_of(body, "must")))

    def test_query_of_only_exclusions_still_builds(self) -> None:
        """Приходит из адресной строки; падать на этом нельзя."""

        body = self.body("-конкурс")

        self.assertEqual(
            clauses_of(body, "must"),
            [{"match_all": {}}],
        )
        self.assertEqual(len(clauses_of(body, "must_not")), 1)

    def test_filters_survive_alongside_exclusions(self) -> None:
        body = build_search_body(
            "благоустройство -конкурс",
            limit=10,
            offset=0,
            doc_type="pdf",
        )

        self.assertIn(
            {"term": {"doc_type": "pdf"}},
            clauses_of(body, "filter"),
        )


class PlainQueryUnchangedTests(unittest.TestCase):
    """Запрос без кавычек и минусов должен строиться как раньше."""

    def test_plain_query_keeps_cross_fields_shape(self) -> None:
        body = build_search_body("благоустройство", limit=10, offset=0)
        must = clauses_of(body, "must")

        self.assertEqual(len(must), 1)
        self.assertEqual(
            must[0]["multi_match"]["type"],
            "cross_fields",
        )
        self.assertEqual(
            must[0]["multi_match"]["minimum_should_match"],
            "3<75%",
        )

    def test_plain_query_has_no_must_not(self) -> None:
        body = build_search_body("благоустройство", limit=10, offset=0)

        self.assertEqual(clauses_of(body, "must_not"), [])

    def test_document_number_query_keeps_exact_clause(self) -> None:
        rendered = as_text(
            build_search_body("512-р", limit=10, offset=0)
        )

        self.assertIn("document_number_normalized", rendered)

    def test_empty_query_with_filters_is_a_filter_only_query(self) -> None:
        body = build_query("", [{"term": {"doc_type": "pdf"}}])

        self.assertEqual(
            body,
            {"bool": {"filter": [{"term": {"doc_type": "pdf"}}]}},
        )


if __name__ == "__main__":
    unittest.main()
