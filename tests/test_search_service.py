from __future__ import annotations

import json
import sys
import types
import unittest


# Эти тесты проверяют чистую сборку поискового запроса и не ходят в сеть.
fake_es_http = types.ModuleType("app_v2.es_http")
fake_es_http.es_client = types.SimpleNamespace(search=lambda body: {})
sys.modules.setdefault("app_v2.es_http", fake_es_http)

from app_v2 import search_query, search_service  # noqa: E402


class SearchBodyTests(unittest.TestCase):
    def test_date_filter_uses_the_precomputed_sort_date(self) -> None:
        """
        Фильтр по датам — один диапазон, а не объединение трёх полей.

        В прежней схеме дата публикации лежала в трёх разных полях, и
        фильтр приходилось собирать как `should` по всем трём: документ
        считался подходящим, если хоть одно из них попадало в интервал.
        Схема v3 сводит их при индексации в `sort_date`, поэтому запрос
        стал одним диапазоном — быстрее и без расхождений между полями.
        """

        body = search_service.build_search_body(
            "сочи",
            limit=10,
            offset=0,
            date_from="2026-08-01",
            date_to="2026-08-05",
        )

        filters = body["query"]["bool"]["filter"]
        ranges = [
            clause["range"]
            for clause in filters
            if "range" in clause
        ]

        self.assertEqual(len(ranges), 1)
        self.assertEqual(list(ranges[0]), ["sort_date"])
        self.assertEqual(ranges[0]["sort_date"]["gte"], "2026-08-01")
        self.assertEqual(ranges[0]["sort_date"]["lte"], "2026-08-05")

    def test_query_uses_every_configured_search_field(self) -> None:
        body = search_service.build_search_body(
            "благоустройство территории",
            limit=10,
            offset=0,
        )

        multi_match = next(
            clause["multi_match"]
            for clause in body["query"]["bool"]["must"]
            if "multi_match" in clause
        )

        # Список полей объявлен константой: если его правят, тест должен
        # это заметить, а не сверять запрос сам с собой.
        self.assertEqual(
            multi_match["fields"],
            list(search_query.QUERY_FIELDS),
        )

        # Поля, которых нет в схеме v3, не должны вернуться. Прежний
        # запрос перечислял content, body и headings — обходчик их
        # никогда не заполнял, и highlighter отрабатывал впустую.
        for absent in ("content", "body", "headings"):
            self.assertNotIn(absent, multi_match["fields"])

        # cross_fields рассматривает поля как один текст: запрос
        # «постановление 512-р» находит документ, у которого вид в
        # заголовке, а номер — в тексте.
        self.assertEqual(multi_match["type"], "cross_fields")

    def test_two_word_query_requires_both_words(self) -> None:
        """
        Порог совпадения задан ступенькой, а не долей.

        При «75%» два слова из двух округлялись вниз до одного, и запрос
        «постановление о благоустройстве» возвращал десять тысяч
        документов, где встречалось только слово «постановление».
        Форма «3<75%» означает: до трёх слов нужны все, дальше 75%.
        """

        body = search_service.build_search_body(
            "постановление благоустройство",
            limit=10,
            offset=0,
        )

        multi_match = next(
            clause["multi_match"]
            for clause in body["query"]["bool"]["must"]
            if "multi_match" in clause
        )

        self.assertEqual(
            multi_match["minimum_should_match"],
            "3<75%",
        )

    def test_response_source_contains_card_and_answer_metadata(self) -> None:
        body = search_service.build_search_body(
            "документ",
            limit=10,
            offset=0,
        )

        required = {
            "url",
            "title",
            "section",
            "site_section",
            "document_number",
            "document_kind",
            "document_date",
            "published_at",
            "sort_date",
            "content_category",
            "doc_type",
            "parent_url",
            "is_attachment",
            "text_extraction",
            "ocr_used",
        }

        missing = required - set(body["_source"])
        self.assertEqual(missing, set(), msg=f"не запрашиваются: {missing}")

    def test_category_is_applied_after_aggregations(self) -> None:
        """
        Рубрика сужает выдачу, но не счётчики вкладок.

        Обычный `filter` сузил бы и агрегации: у всех остальных рубрик
        счётчик стал бы нулём, и по ним нельзя было бы перейти.
        """

        body = search_service.build_search_body(
            "документ",
            limit=10,
            offset=0,
            content_category="legal",
        )

        self.assertIn("post_filter", body)
        self.assertIn("category_facets", body["aggs"])

        filters = body["query"]["bool"].get("filter", [])
        self.assertNotIn(
            "content_category",
            json.dumps(filters, ensure_ascii=False),
        )

    def test_legacy_category_fallback_includes_www_urls(self) -> None:
        category = search_service.content_category_filter("events")
        prefixes = {
            clause["prefix"]["url"]
            for clause in category["bool"]["should"]
            if "prefix" in clause
        }

        self.assertIn(
            "https://www.sochi.ru/afisha-goroda/",
            prefixes,
        )
        self.assertIn(
            "http://sochi.ru/kalendar-meropriyatiy/",
            prefixes,
        )

    def test_fuzzy_fallback_preserves_filters_and_is_bounded(self) -> None:
        """
        Запасной нечёткий запрос сохраняет фильтры и остаётся ограниченным.

        Нечёткое совпадение считается дорого: без предела на число
        вариантов слова запрос по опечатке разворачивается в сотни
        термов. Фильтры при этом должны сохраниться — иначе после
        опечатки выдача внезапно перестаёт учитывать выбранный год или
        вид документа.
        """

        body = search_service.build_fuzzy_search_body(
            "благоустройтво територии",
            limit=10,
            offset=0,
            document_kind="Постановление",
            date_from="2026-01-01",
            content_category="news",
        )

        bool_query = body["query"]["bool"]

        # Фильтры перенесены из обычного запроса, рубрика — по-прежнему
        # post_filter, чтобы счётчики вкладок не обнулились.
        self.assertGreaterEqual(len(bool_query["filter"]), 2)
        self.assertIn("post_filter", body)

        multi_match = next(
            clause["multi_match"]
            for clause in bool_query["must"]
            if "multi_match" in clause
        )

        self.assertEqual(multi_match["fuzziness"], "AUTO")
        self.assertLessEqual(multi_match["max_expansions"], 24)

        # Первая буква фиксирована: без этого «сочи» и «дочи» считаются
        # одинаково близкими, и выдача заполняется случайными словами.
        self.assertGreaterEqual(multi_match["prefix_length"], 1)

        self.assertEqual(
            multi_match["fields"],
            list(search_query.FUZZY_FIELDS),
        )

    def test_zero_result_uses_fuzzy_fallback_once(self) -> None:
        responses = [
            {
                "took": 3,
                "hits": {
                    "total": {"value": 0},
                    "hits": [],
                },
                "aggregations": {
                    "unique_documents": {"value": 0},
                    "category_facets": {"buckets": {}},
                },
            },
            {
                "took": 5,
                "hits": {
                    "total": {"value": 1},
                    "hits": [
                        {
                            "_id": "one",
                            "_score": 7.0,
                            "_source": {
                                "url": "https://sochi.ru/example/",
                                "title": "Благоустройство территории",
                                "text": "План благоустройства территории.",
                                "doc_type": "page",
                            },
                        }
                    ],
                },
                "aggregations": {
                    "unique_documents": {"value": 1},
                    "category_facets": {"buckets": {}},
                },
            },
        ]

        class RecordingClient:
            def __init__(self) -> None:
                self.bodies = []

            def search(self, body):
                self.bodies.append(body)
                return responses[len(self.bodies) - 1]

        original_client = search_service.es_client
        client = RecordingClient()
        search_service.es_client = client

        try:
            result = search_service.search_documents(
                "благоустройтво",
                limit=10,
                offset=0,
            )
        finally:
            search_service.es_client = original_client

        self.assertEqual(len(client.bodies), 2)
        self.assertTrue(result["fallback_attempted"])
        self.assertTrue(result["fallback_applied"])
        self.assertEqual(result["match_mode"], "fuzzy")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["took_ms"], 8)
        self.assertEqual(len(result["items"]), 1)

    def test_pdf_card_prefers_attachment_title_and_hides_garbled_layer(self) -> None:
        self.assertTrue(
            search_service.is_generic_title(
                "Официальное опубликование муниципальных правовых актов за 2024 год"
            )
        )
        self.assertFalse(
            search_service.is_generic_title(
                "Администрация города Сочи — Постановление № 223"
            )
        )

        item = search_service.normalize_hit(
            {
                "_id": "pdf-one",
                "_score": 1.0,
                "_source": {
                    "url": "https://sochi.ru/upload/223.pdf",
                    "title": (
                        "Официальное опубликование "
                        "муниципальных правовых актов"
                    ),
                    "attachment_title": (
                        "Постановление администрации города Сочи № 223"
                    ),
                    "text": (
                        "AAMrHrrCTPAqU TOPOAA COrrU TIO "
                        "CTAIIOBJIEIII,IE sHeceuuu H3MeHeHrIfi "
                    ) * 4,
                    "doc_type": "pdf",
                    "text_extraction": "metadata_only",
                    "ocr_used": False,
                    "unreadable_page_count": 1,
                },
            },
            "постановление",
        )

        self.assertEqual(
            item["title"],
            "Постановление администрации города Сочи № 223",
        )
        self.assertIn(
            "требует повторного распознавания",
            item["snippet"],
        )
        self.assertEqual(
            item["text_extraction"],
            "metadata_only",
        )
        self.assertEqual(
            item["unreadable_page_count"],
            1,
        )


class ExtractiveAnswerTests(unittest.TestCase):
    def test_answer_is_structured_and_preserves_source_metadata(self) -> None:
        response = search_service.build_extractive_answer(
            {
                "query": "благоустройство",
                "normalized_query": "благоустройство",
                "filters": {"content_category": "news"},
                "total": 12,
                "items": [
                    {
                        "title": "Работы по благоустройству",
                        "url": "https://sochi.ru/example/",
                        "snippet": "Работы будут завершены в августе.",
                        "document_kind": "Постановление",
                        "document_number": "30-р",
                        "document_date": "2026-08-01",
                        "section": "Новости",
                        "page_number": 2,
                        "document_type": "page",
                        "score": 42.0,
                    }
                ],
            }
        )

        self.assertEqual(response["mode"], "extractive")
        self.assertEqual(response["source_count"], 1)
        self.assertEqual(response["total"], 12)
        self.assertEqual(
            response["filters"],
            {"content_category": "news"},
        )
        self.assertIn("• Работы будут завершены", response["answer"])
        self.assertTrue(response["notice"])
        self.assertEqual(
            response["sources"][0]["document_number"],
            "30-р",
        )

    def test_empty_answer_has_stable_shape(self) -> None:
        response = search_service.build_extractive_answer(
            {
                "query": "несуществующий запрос",
                "filters": {},
                "total": 0,
                "items": [],
            }
        )

        self.assertEqual(response["mode"], "extractive")
        self.assertEqual(response["source_count"], 0)
        self.assertEqual(response["sources"], [])
        self.assertEqual(response["total"], 0)


if __name__ == "__main__":
    unittest.main()
