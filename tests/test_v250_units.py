"""
Тесты новых модулей 2.5.0 на стандартном unittest.

Запуск без внешних зависимостей:

    python -m unittest discover -s tests -p 'test_v250_units.py' -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_v2.search_query import (  # noqa: E402
    build_search_body,
    category_filter,
    date_filter,
    looks_like_document_number,
    normalize_document_number,
)
from crawler_v2.pdf_ocr_engine import (  # noqa: E402
    choose_render_dpi,
    page_order,
)
from crawler_v2.pdf_ocr_image import (  # noqa: E402
    estimate_skew,
    prepare_page,
    sauvola_binarize,
)

MAPPING_PATH = (
    Path(__file__).resolve().parents[1]
    / "elasticsearch"
    / "sochi_docs_v3.json"
)

DEAD_FIELDS = {"content", "body", "headings", "description", "document_type"}


class FakeRect:
    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


class FakePage:
    """Минимальная замена страницы PyMuPDF для проверки выбора dpi."""

    def __init__(
        self,
        *,
        width_points: float,
        height_points: float,
        image_width: int | None = None,
        image_height: int | None = None,
    ) -> None:
        self.rect = FakeRect(width_points, height_points)
        self._image_width = image_width
        self._image_height = image_height

    def get_text(self, kind: str):
        if self._image_width is None:
            return {"blocks": []}

        return {
            "blocks": [
                {
                    "type": 1,
                    "width": self._image_width,
                    "height": self._image_height,
                }
            ]
        }


class DocumentNumberTests(unittest.TestCase):
    def test_normalization_collapses_variants(self) -> None:
        for value in ("№ 512 – Р", "N512-P", "512-р", "№512 - р"):
            self.assertEqual(
                normalize_document_number(value),
                "512-р",
                msg=value,
            )

    def test_latin_lookalikes_become_cyrillic(self) -> None:
        self.assertEqual(normalize_document_number("84-PC"), "84-рс")

    def test_detects_number_shaped_queries(self) -> None:
        self.assertTrue(looks_like_document_number("постановление 512-р"))
        self.assertTrue(looks_like_document_number("№ 84"))
        self.assertFalse(looks_like_document_number("субсидии малому бизнесу"))


class SearchBodyTests(unittest.TestCase):
    def body(self, **kwargs) -> dict:
        parameters = {"limit": 10, "offset": 0}
        parameters.update(kwargs)
        return build_search_body(
            parameters.pop("query", "субсидии"),
            **parameters,
        )

    def test_no_dead_fields_anywhere(self) -> None:
        serialized = json.dumps(
            self.body(query="постановление", content_category="legal"),
            ensure_ascii=False,
        )

        for field in DEAD_FIELDS:
            self.assertNotIn(
                f'"{field}"',
                serialized,
                msg=f"поле {field} не заполняется crawler, но попало в запрос",
            )

    def test_category_filter_has_no_url_prefix_clauses(self) -> None:
        serialized = json.dumps(category_filter("legal"), ensure_ascii=False)

        self.assertNotIn("prefix", serialized)
        self.assertIn("content_category", serialized)

    def test_date_filter_uses_single_field(self) -> None:
        clause = date_filter("2024-01-01", "2024-12-31")

        self.assertEqual(list(clause), ["range"])
        self.assertEqual(list(clause["range"]), ["sort_date"])

    def test_number_query_adds_boost_clauses(self) -> None:
        body = self.body(query="512-р")
        should = body["query"]["bool"]["should"]
        serialized = json.dumps(should, ensure_ascii=False)

        self.assertIn("document_number", serialized)

    def test_plain_query_has_no_number_clauses(self) -> None:
        body = self.body(query="весенние мероприятия")
        serialized = json.dumps(
            body["query"]["bool"]["should"], ensure_ascii=False
        )

        self.assertNotIn("document_number", serialized)

    def test_empty_query_has_no_highlight(self) -> None:
        body = self.body(query="")

        self.assertNotIn("highlight", body)
        self.assertEqual(body["query"], {"match_all": {}})

    def test_highlight_only_on_populated_fields(self) -> None:
        body = self.body(query="субсидии")

        self.assertEqual(
            set(body["highlight"]["fields"]),
            {"text", "title"},
        )

    def test_collapse_and_rescore_are_not_combined(self) -> None:
        body = self.body(query="субсидии")

        self.assertIn("collapse", body)
        self.assertNotIn("rescore", body)

    def test_track_total_hits_is_bounded(self) -> None:
        body = self.body(query="субсидии")

        self.assertNotEqual(body["track_total_hits"], True)

    def test_cardinality_precision_is_moderate(self) -> None:
        body = self.body(query="субсидии")
        precision = (
            body["aggs"]["unique_documents"]["cardinality"][
                "precision_threshold"
            ]
        )

        self.assertLessEqual(precision, 5000)

    def test_filters_are_applied(self) -> None:
        body = self.body(
            query="субсидии",
            doc_type="pdf",
            is_attachment=True,
            date_from="2024-01-01",
        )
        serialized = json.dumps(
            body["query"]["bool"]["filter"], ensure_ascii=False
        )

        self.assertIn("doc_type", serialized)
        self.assertIn("is_attachment", serialized)
        self.assertIn("sort_date", serialized)


class MappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mapping = json.loads(
            MAPPING_PATH.read_text(encoding="utf-8")
        )
        self.properties = self.mapping["mappings"]["properties"]
        self.settings = self.mapping["settings"]["index"]

    def test_refresh_interval_is_not_disabled(self) -> None:
        self.assertNotEqual(self.settings["refresh_interval"], "-1")

    def test_dead_fields_removed(self) -> None:
        for field in DEAD_FIELDS:
            self.assertNotIn(field, self.properties)

    def test_text_field_has_offsets_for_highlighting(self) -> None:
        self.assertEqual(
            self.properties["text"]["index_options"],
            "offsets",
        )

    def test_yo_folding_char_filter_present(self) -> None:
        analyzer = self.settings["analysis"]["analyzer"]["russian_text"]

        self.assertIn("ru_yo", analyzer["char_filter"])

    def test_exact_analyzer_has_no_stemmer(self) -> None:
        analyzer = self.settings["analysis"]["analyzer"]["russian_exact"]

        self.assertNotIn("russian_stemmer", analyzer["filter"])

    def test_document_number_is_keyword_with_normalizer(self) -> None:
        field = self.properties["document_number"]

        self.assertEqual(field["type"], "keyword")
        self.assertIn("normalizer", field)

    def test_word_delimiter_graph_avoids_invalid_token_graph(self) -> None:
        # preserve_original вместе с catenate_all даёт некорректный граф
        # токенов и ломает фразовые запросы.
        delimiter = (
            self.settings["analysis"]["filter"]["legal_word_delimiter"]
        )

        self.assertFalse(
            delimiter["catenate_all"] and delimiter["preserve_original"]
        )

    def test_document_number_analyzer_uses_whitespace_tokenizer(self) -> None:
        # standard уже разбил бы «512-р» на два токена, и склейка
        # word_delimiter_graph не сработала бы.
        analyzer = (
            self.settings["analysis"]["analyzer"]["document_number_text"]
        )

        self.assertEqual(analyzer["tokenizer"], "whitespace")

    def test_mapping_is_strict(self) -> None:
        self.assertEqual(self.mapping["mappings"]["dynamic"], "strict")


class RenderDpiTests(unittest.TestCase):
    def test_uses_native_resolution_of_embedded_scan(self) -> None:
        # A4 (595x842 pt), вложенный растр 2480x3508 → примерно 300 dpi.
        page = FakePage(
            width_points=595,
            height_points=842,
            image_width=2480,
            image_height=3508,
        )

        self.assertEqual(
            choose_render_dpi(
                page,
                minimum_dpi=250,
                maximum_dpi=400,
                pixel_budget=12_000_000,
            ),
            300,
        )

    def test_low_resolution_scan_is_raised_to_minimum(self) -> None:
        page = FakePage(
            width_points=595,
            height_points=842,
            image_width=1000,
            image_height=1400,
        )

        self.assertEqual(
            choose_render_dpi(
                page,
                minimum_dpi=250,
                maximum_dpi=400,
                pixel_budget=12_000_000,
            ),
            250,
        )

    def test_large_format_page_is_capped_by_pixel_budget(self) -> None:
        # Лист A0: при 300 dpi это больше 130 мегапикселей.
        page = FakePage(
            width_points=2384,
            height_points=3370,
            image_width=9933,
            image_height=14043,
        )

        dpi = choose_render_dpi(
            page,
            minimum_dpi=250,
            maximum_dpi=400,
            pixel_budget=12_000_000,
        )

        width_inches = 2384 / 72.0
        height_inches = 3370 / 72.0
        pixels = width_inches * dpi * height_inches * dpi

        self.assertLessEqual(pixels, 12_000_000 * 1.05)
        self.assertGreater(dpi, 72)

    def test_missing_image_falls_back_to_minimum(self) -> None:
        page = FakePage(width_points=595, height_points=842)

        self.assertEqual(
            choose_render_dpi(
                page,
                minimum_dpi=250,
                maximum_dpi=400,
                pixel_budget=12_000_000,
            ),
            250,
        )


class PageOrderTests(unittest.TestCase):
    def test_priority_pages_come_first(self) -> None:
        self.assertEqual(
            page_order(6, priority_pages=3),
            [0, 1, 2, 3, 4, 5],
        )

    def test_short_document_is_unchanged(self) -> None:
        self.assertEqual(page_order(2, priority_pages=3), [0, 1])

    def test_no_page_is_lost_or_duplicated(self) -> None:
        order = page_order(40, priority_pages=3)

        self.assertEqual(sorted(order), list(range(40)))


class PreprocessingTests(unittest.TestCase):
    @staticmethod
    def make_page(
        *,
        skew: float = 0.0,
        faded: bool = False,
        blank: bool = False,
    ) -> Image.Image:
        from PIL import ImageDraw

        image = Image.new("L", (1200, 1600), 255)

        if not blank:
            draw = ImageDraw.Draw(image)

            # Строки набираются из отдельных «слов» с просветами. Сплошная
            # полоса во всю ширину не годится: её проекция почти не зависит
            # от угла, и такой лист не проверяет оценку перекоса.
            rng = np.random.default_rng(11)

            for index in range(22):
                y = 110 + index * 62
                x = 140

                while x < 1040:
                    word = int(rng.integers(38, 130))
                    draw.rectangle(
                        [x, y, min(x + word, 1040), y + 14],
                        fill=35,
                    )
                    x += word + int(rng.integers(14, 26))

        if skew:
            image = image.rotate(
                skew,
                resample=Image.BICUBIC,
                fillcolor=255,
            )

        if faded:
            array = np.asarray(image).astype(np.float32)
            columns = np.linspace(1.0, 0.72, array.shape[1])
            array = 150.0 + (array - 150.0) * 0.30
            array = array * columns[None, :]
            image = Image.fromarray(
                np.clip(array, 0, 255).astype(np.uint8)
            )

        return image

    def test_binarization_separates_ink_from_faded_background(self) -> None:
        image = self.make_page(faded=True)
        binary = sauvola_binarize(np.asarray(image))
        ink_ratio = float((binary == 0).mean())

        self.assertGreater(ink_ratio, 0.02)
        self.assertLess(ink_ratio, 0.55)

    def test_skew_is_detected_and_corrected(self) -> None:
        prepared = prepare_page(self.make_page(skew=1.5, faded=True))

        self.assertAlmostEqual(prepared.skew_degrees, -1.5, delta=0.35)

    def test_straight_page_is_not_rotated(self) -> None:
        prepared = prepare_page(self.make_page(skew=0.0))

        self.assertAlmostEqual(prepared.skew_degrees, 0.0, delta=0.25)

    def test_blank_page_is_flagged(self) -> None:
        prepared = prepare_page(self.make_page(blank=True))

        self.assertTrue(prepared.blank)

    def test_text_page_is_not_flagged_blank(self) -> None:
        prepared = prepare_page(self.make_page(faded=True))

        self.assertFalse(prepared.blank)

    def test_output_is_pure_black_and_white(self) -> None:
        prepared = prepare_page(self.make_page(faded=True))
        values = set(np.unique(np.asarray(prepared.image)).tolist())

        self.assertTrue(values <= {0, 255}, msg=f"значения: {values}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


FAKE_RESPONSE = {
    "hits": {
        "total": {"value": 42, "relation": "eq"},
        "hits": [
            {
                "_id": "1",
                "_score": 9.1,
                "_source": {
                    "url": "https://sochi.ru/upload/a.pdf",
                    "title": "Постановление о субсидиях",
                    "text": (
                        "Администрация города Сочи постановляет "
                        "предоставить субсидии субъектам малого "
                        "предпринимательства согласно приложению. "
                    ) * 4,
                    "doc_type": "pdf",
                    "content_category": "legal",
                    "document_number": "512-р",
                    "document_date": "2024-03-14",
                    "is_attachment": True,
                    "page_number": 1,
                    "chunk_number": 0,
                },
                "highlight": {
                    "text": [
                        "предоставить <mark>субсидии</mark> субъектам "
                        "малого предпринимательства согласно приложению"
                    ]
                },
            }
        ],
    },
    "aggregations": {
        "unique_documents": {"value": 30},
        "category_facets": {
            "buckets": [
                {
                    "key": "legal",
                    "doc_count": 20,
                    "unique_documents": {"value": 12},
                },
                {
                    "key": "news",
                    "doc_count": 15,
                    "unique_documents": {"value": 9},
                },
            ]
        },
    },
}


class FakeElasticsearch:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def search(self, body: dict) -> dict:
        self.bodies.append(body)
        return FAKE_RESPONSE


class SearchWiringTests(unittest.TestCase):
    """
    Проверяет, что сервис действительно использует новый построитель.

    Модуль `search_query` можно написать правильно и забыть подключить —
    тогда API продолжит работать по старой схеме, и ни один тест самого
    модуля этого не заметит.
    """

    def setUp(self) -> None:
        import app_v2.search_service as service

        self.service = service
        self.fake = FakeElasticsearch()
        self.original = service.es_client
        service.es_client = self.fake

    def tearDown(self) -> None:
        self.service.es_client = self.original

    def run_search(self, **kwargs):
        parameters = {"limit": 10, "offset": 0}
        parameters.update(kwargs)
        return self.service.search_documents(
            parameters.pop("query", "субсидии"),
            **parameters,
        )

    def test_service_uses_new_query_builder(self) -> None:
        self.run_search()
        body = self.fake.bodies[0]

        self.assertIn("must", body["query"]["bool"])
        self.assertEqual(
            body["query"]["bool"]["must"][0]["multi_match"]["type"],
            "cross_fields",
        )

    def test_no_dead_fields_reach_elasticsearch(self) -> None:
        self.run_search(content_category="legal")
        serialized = json.dumps(self.fake.bodies[0], ensure_ascii=False)

        for field in DEAD_FIELDS:
            self.assertNotIn(f'"{field}"', serialized)

    def test_category_uses_post_filter_not_filter(self) -> None:
        self.run_search(content_category="legal")
        body = self.fake.bodies[0]

        self.assertIn("post_filter", body)
        self.assertIsNone(body["query"]["bool"].get("filter"))

    def test_facet_counts_parsed_from_terms_buckets(self) -> None:
        # `terms` отдаёт список корзин, прежняя `filters` — словарь.
        result = self.run_search(content_category="legal")
        counts = {
            facet["code"]: facet["count"]
            for facet in result["facets"]
        }

        self.assertEqual(counts["all"], 30)
        self.assertEqual(counts["legal"], 12)
        self.assertEqual(counts["news"], 9)

    def test_absent_category_shows_zero_not_error(self) -> None:
        result = self.run_search()
        counts = {
            facet["code"]: facet["count"]
            for facet in result["facets"]
        }

        self.assertEqual(counts["media"], 0)

    def test_selected_facet_is_marked(self) -> None:
        result = self.run_search(content_category="legal")
        selected = [
            facet["code"]
            for facet in result["facets"]
            if facet["selected"]
        ]

        self.assertEqual(selected, ["legal"])

    def test_result_has_title_and_snippet(self) -> None:
        item = self.run_search()["items"][0]

        self.assertTrue(item["title"])
        self.assertGreater(len(item["snippet"]), 20)
        self.assertNotIn("<mark>", item["snippet"])


class DocumentDateFormatTests(unittest.TestCase):
    def test_iso_date_becomes_russian(self) -> None:
        from app_v2.search_service import format_document_date

        self.assertEqual(
            format_document_date("2024-03-14"), "14 марта 2024"
        )
        self.assertEqual(
            format_document_date("2021-01-27"), "27 января 2021"
        )

    def test_invalid_input_is_returned_unchanged(self) -> None:
        from app_v2.search_service import format_document_date

        for value in ("", "мусор", "2024-13-01"):
            self.assertEqual(format_document_date(value), value)


class SearchApiImportTests(unittest.TestCase):
    def test_search_service_imports_without_pymupdf(self) -> None:
        # API поиска не должен зависеть от PDF-библиотеки: её сбой
        # ронял бы поиск на старте.
        import subprocess

        source_root = Path(__file__).resolve().parents[1]
        code = (
            "import sys;"
            "sys.modules['pymupdf']=None;"
            "sys.path.insert(0, %r);"
            "import app_v2.search_service;"
            "print('ok')" % str(source_root)
        )
        done = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            done.returncode, 0, msg=done.stderr[-800:]
        )


class MinimumShouldMatchTests(unittest.TestCase):
    """
    Короткий запрос не должен вырождаться в поиск по одному слову.

    Elasticsearch округляет процент вниз. При «75%» запрос из двух слов
    требовал одно: на рабочем индексе «постановление о благоустройстве»
    давал 10277 документов без благоустройства в выдаче, а «512-р» —
    2460, потому что хватало «512» или «р» по отдельности.
    """

    def clause(self, query: str) -> dict:
        body = build_search_body(query, limit=10, offset=0)
        return body["query"]["bool"]["must"][0]["multi_match"]

    def test_threshold_form_is_used(self) -> None:
        value = self.clause("постановление о благоустройстве")[
            "minimum_should_match"
        ]

        self.assertIn("<", value, msg=f"голый процент: {value}")

    def test_short_queries_require_every_word(self) -> None:
        value = self.clause("512-р")["minimum_should_match"]
        threshold = int(value.split("<", 1)[0])

        # До порога включительно требуются все слова.
        self.assertGreaterEqual(threshold, 2)

    def test_long_queries_stay_tolerant(self) -> None:
        value = self.clause(
            "порядок предоставления субсидий субъектам малого "
            "предпринимательства"
        )["minimum_should_match"]
        percent = value.split("<", 1)[1]

        self.assertTrue(percent.endswith("%"))
        self.assertLessEqual(int(percent.rstrip("%")), 85)
