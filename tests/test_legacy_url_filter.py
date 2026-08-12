from __future__ import annotations

import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path

try:
    import bs4  # noqa: F401
except ImportError:
    bs4_module = types.ModuleType("bs4")
    bs4_module.BeautifulSoup = object
    sys.modules["bs4"] = bs4_module

try:
    import requests  # noqa: F401
except ImportError:
    requests_module = types.ModuleType("requests")
    adapters_module = types.ModuleType("requests.adapters")
    requests_module.Session = object
    adapters_module.HTTPAdapter = object
    sys.modules["requests"] = requests_module
    sys.modules["requests.adapters"] = adapters_module

try:
    from urllib3.util.retry import Retry  # noqa: F401
except ImportError:
    urllib3_module = types.ModuleType("urllib3")
    util_module = types.ModuleType("urllib3.util")
    retry_module = types.ModuleType("urllib3.util.retry")
    retry_module.Retry = object
    sys.modules["urllib3"] = urllib3_module
    sys.modules["urllib3.util"] = util_module
    sys.modules["urllib3.util.retry"] = retry_module


from crawler_v2.discovery_db import DiscoveryDatabase
from crawler_v2.discovery_worker import canonicalize_url
from crawler_v2.discovery_worker_v2 import canonicalize_html_url
from crawler_v2.url_policy import (
    is_legacy_content_detail_url,
    is_legacy_detail_url,
)


LEGACY_URL = (
    "http://sochi.ru/content/detail.php?ID=85340"
)

MODERN_DETAIL_URL = (
    "https://sochi.ru/gorodskaya-vlast/"
    "administratsiya-goroda/glava-goroda/"
    "novosti/detail.php"
)


def candidate(url: str) -> dict[str, object]:
    return {
        "url": url,
        "page_url": url,
        "file_url": None,
        "doc_type": "page",
        "content_type": None,
        "discovered_from": "test",
        "sitemap_lastmod": None,
    }


class LegacyUrlPolicyTests(unittest.TestCase):
    def test_exact_legacy_variants_are_excluded(self) -> None:
        variants = (
            LEGACY_URL,
            "http://sochi.ru/contents/detail.php?ID=107677",
            (
                "https://sochi.ru/content/detail.php"
                "?section_id=1&id=85340"
            ),
            (
                "https://sochi.ru/CONTENT/DETAIL.PHP/"
                "?Id=85340"
            ),
        )

        for url in variants:
            with self.subTest(url=url):
                self.assertTrue(
                    is_legacy_content_detail_url(url)
                )
                self.assertTrue(
                    is_legacy_detail_url(url)
                )
                self.assertIsNone(
                    canonicalize_url(url)
                )
                self.assertIsNone(
                    canonicalize_html_url(url)
                )

    def test_modern_detail_and_regular_urls_remain_allowed(
        self,
    ) -> None:
        allowed = (
            MODERN_DETAIL_URL,
            "https://sochi.ru/press-sluzhba/",
            "https://sochi.ru/upload/example.pdf",
            (
                "https://sochi.ru/gorod/detail.php"
                "?section_id=10"
            ),
        )

        for url in allowed:
            with self.subTest(url=url):
                self.assertFalse(
                    is_legacy_content_detail_url(url)
                )
                self.assertIsNotNone(
                    canonicalize_url(url)
                )
                self.assertIsNotNone(
                    canonicalize_html_url(url)
                )

    def test_database_does_not_insert_or_reactivate_legacy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.sqlite3"

            with sqlite3.connect(database_path) as connection:
                connection.execute("""
                    CREATE TABLE crawl_urls (
                        url TEXT PRIMARY KEY,
                        page_url TEXT,
                        file_url TEXT,
                        source TEXT,
                        doc_type TEXT,
                        content_type TEXT,
                        status TEXT,
                        first_seen_at TEXT,
                        last_seen_at TEXT,
                        next_check_at TEXT,
                        is_active INTEGER,
                        discovered_at TEXT,
                        discovered_from TEXT,
                        sitemap_lastmod TEXT
                    )
                """)

                connection.executemany(
                    """
                    INSERT INTO crawl_urls (
                        url,
                        status,
                        is_active,
                        next_check_at
                    )
                    VALUES (?, 'gone', 0, ?)
                    """,
                    (
                        (LEGACY_URL, "old"),
                        (MODERN_DETAIL_URL, "old"),
                    ),
                )

            database = DiscoveryDatabase(database_path)

            inserted, existing = database.insert_discovered(
                (
                    candidate(LEGACY_URL),
                    candidate(MODERN_DETAIL_URL),
                ),
                seen_at="2026-08-05T10:00:00+00:00",
            )

            self.assertEqual((inserted, existing), (0, 1))

            with sqlite3.connect(database_path) as connection:
                legacy_state = connection.execute(
                    """
                    SELECT status, is_active
                    FROM crawl_urls
                    WHERE url = ?
                    """,
                    (LEGACY_URL,),
                ).fetchone()

                modern_state = connection.execute(
                    """
                    SELECT status, is_active
                    FROM crawl_urls
                    WHERE url = ?
                    """,
                    (MODERN_DETAIL_URL,),
                ).fetchone()

            self.assertEqual(legacy_state, ("gone", 0))
            self.assertEqual(modern_state, ("discovered", 1))

            new_legacy = (
                "https://sochi.ru/content/detail.php?ID=999999"
            )

            result = database.insert_discovered(
                (candidate(new_legacy),),
                seen_at="2026-08-05T10:01:00+00:00",
            )

            self.assertEqual(result, (0, 0))

            with sqlite3.connect(database_path) as connection:
                count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM crawl_urls
                    WHERE url = ?
                    """,
                    (new_legacy,),
                ).fetchone()[0]

            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
