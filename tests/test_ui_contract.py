from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "app_v2" / "static"


class IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        attributes = dict(attrs)

        if attributes.get("id"):
            self.ids.append(attributes["id"])


class SearchUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        cls.javascript = (STATIC_DIR / "search.js").read_text(encoding="utf-8")
        cls.css = (STATIC_DIR / "search.css").read_text(encoding="utf-8")

    def test_required_interactive_regions_exist_once(self) -> None:
        parser = IdCollector()
        parser.feed(self.html)

        self.assertEqual(len(parser.ids), len(set(parser.ids)))

        required = {
            "search-form",
            "query",
            "answer-button",
            "active-filters",
            "answer-panel",
            "answer-content",
            "recent-searches",
            "facet-tabs",
            "export-button",
            "results",
            "pagination-pages",
            "pipeline-panel",
            "pipeline-refresh",
            "pipeline-progressbar",
            "pipeline-total",
            "pipeline-loaded",
            "pipeline-remaining",
            "pipeline-processing",
            "pipeline-documents",
            "pipeline-problems",
            "pipeline-formats",
            "pipeline-publication",
            "pipeline-cleanup",
            "pipeline-pdf-ocr",
            "pipeline-discovery",
        }
        self.assertTrue(required.issubset(set(parser.ids)))

    def test_static_asset_version_is_consistent(self) -> None:
        self.assertIn("/assets/search.css?v=9", self.html)
        self.assertIn("/assets/search.js?v=9", self.html)

    def test_client_avoids_untrusted_html_injection(self) -> None:
        self.assertNotIn("innerHTML", self.javascript)
        self.assertIn("safeExternalUrl", self.javascript)
        self.assertIn("spreadsheetSafe", self.javascript)
        self.assertIn("noopener noreferrer", self.javascript)

    def test_recent_searches_and_csv_export_are_client_side(self) -> None:
        for marker in (
            "RECENT_SEARCHES_KEY",
            "renderRecentSearches",
            "rememberSearch",
            "exportCurrentPage",
            "URL.createObjectURL",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.javascript)

    def test_pipeline_progress_is_read_only_and_auto_refreshed(self) -> None:
        for marker in (
            'fetch("/pipeline/status"',
            "renderPipelineStatus",
            "renderPipelineFormats",
            "PIPELINE_REFRESH_MS",
            "pipelineProgressFill.style.width",
            "pipelinePdfOcrText",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.javascript)

    def test_states_and_responsive_layout_are_styled(self) -> None:
        for selector in (
            ".answer-panel",
            ".active-filters",
            ".recent-searches",
            ".export-button",
            ".empty-state",
            ".error-state",
            ".page-button--current",
            ".pipeline-panel",
            ".pipeline-metrics",
            ".pipeline-progressbar",
            ".pipeline-table",
            "@media (max-width: 600px)",
            "@media (prefers-reduced-motion: reduce)",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.css)


if __name__ == "__main__":
    unittest.main()
