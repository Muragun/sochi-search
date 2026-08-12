from __future__ import annotations

import tempfile
import subprocess
import signal
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Пропуск объявляется до импорта crawler_v2: content_processing тоже
# тянет PyMuPDF, и без этого весь модуль падал бы с ImportError, а
# `unittest discover` показывал бы ошибку вместо пропуска.
#
# Проверяется не сам факт импорта, а что найдена настоящая библиотека.
# Соседний tests/test_crawler_runtime кладёт в sys.modules собственную
# заглушку с именем pymupdf, и при общем прогоне она доставалась этому
# модулю: импорт проходил, а тесты падали на отсутствующих методах.
try:
    import pymupdf
except ImportError:  # pragma: no cover — среда без PyMuPDF
    pymupdf = None

if pymupdf is None or not hasattr(pymupdf, "csGRAY"):
    raise unittest.SkipTest(
        "нужен настоящий PyMuPDF: тесты строят настоящие PDF"
    )

import bs4

from crawler_v2 import content_processing as content_processing_module
from crawler_v2 import fetcher as fetcher_module
from crawler_v2.content_processing import (
    PDF_OCR_MODE_FAST,
    PdfExtractionTimeoutError,
    extract_pdf,
)
from crawler_v2.pdf_ocr import PDF_FAST_EXTRACTION_VERSION
from crawler_v2.fetcher import (
    DownloadTooLargeError,
    HttpFetcher,
)


class FakeResponse:
    def __init__(
        self,
        *,
        body: bytes,
        content_type: str,
        declared_size: int | None = None,
        url: str = "https://sochi.ru/document.pdf",
        status_code: int = 200,
    ) -> None:
        self.body = body
        self.url = url
        self.status_code = status_code
        self.closed = False
        self.headers = {
            "Content-Type": content_type,
        }

        if declared_size is not None:
            self.headers["Content-Length"] = str(
                declared_size
            )

    def iter_content(
        self,
        *,
        chunk_size: int,
    ):
        for start in range(
            0,
            len(self.body),
            max(1, chunk_size),
        ):
            yield self.body[
                start:start + chunk_size
            ]

    def close(self) -> None:
        self.closed = True


class LargePdfFetchTests(unittest.TestCase):
    @staticmethod
    def fetcher_for(response: FakeResponse) -> HttpFetcher:
        fetcher = object.__new__(HttpFetcher)
        fetcher.session = SimpleNamespace(
            get=lambda *_args, **_kwargs: response
        )
        return fetcher

    def local_settings(
        self,
        directory: str,
        *,
        normal_limit: int = 8,
        pdf_limit: int = 64,
    ):
        return replace(
            fetcher_module.settings,
            max_download_bytes=normal_limit,
            pdf_max_download_bytes=pdf_limit,
            download_temp_dir=Path(directory),
            download_min_free_bytes=0,
        )

    def test_pdf_larger_than_memory_limit_is_streamed_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response = FakeResponse(
                body=b"%PDF-" + b"x" * 27,
                content_type="application/pdf",
                declared_size=32,
            )

            with patch.object(
                fetcher_module,
                "settings",
                self.local_settings(directory),
            ):
                fetcher = self.fetcher_for(
                    response
                )
                result = fetcher.fetch(
                    response.url,
                    etag=None,
                    last_modified=None,
                )

            self.assertEqual(result.body, b"")
            self.assertEqual(result.size_bytes, 32)
            self.assertIsInstance(result.payload, Path)
            self.assertEqual(
                result.payload.read_bytes(),
                response.body,
            )
            self.assertTrue(response.closed)

            temporary_path = result.body_path
            self.assertIsNotNone(temporary_path)
            result.cleanup()
            self.assertFalse(temporary_path.exists())

    def test_non_pdf_keeps_original_memory_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response = FakeResponse(
                body=b"<html>too large</html>",
                content_type="text/html",
                declared_size=22,
                url="https://sochi.ru/page/",
            )

            with patch.object(
                fetcher_module,
                "settings",
                self.local_settings(directory),
            ):
                fetcher = self.fetcher_for(
                    response
                )

                with self.assertRaises(
                    DownloadTooLargeError
                ):
                    fetcher.fetch(
                        response.url,
                        etag=None,
                        last_modified=None,
                    )

            self.assertTrue(response.closed)
            self.assertEqual(
                list(Path(directory).iterdir()),
                [],
            )

    def test_pdf_limit_is_checked_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response = FakeResponse(
                body=b"%PDF-large",
                content_type="application/pdf",
                declared_size=65,
            )

            with patch.object(
                fetcher_module,
                "settings",
                self.local_settings(directory),
            ):
                fetcher = self.fetcher_for(
                    response
                )

                with self.assertRaises(
                    DownloadTooLargeError
                ):
                    fetcher.fetch(
                        response.url,
                        etag=None,
                        last_modified=None,
                    )

            self.assertTrue(response.closed)
            self.assertEqual(
                list(Path(directory).iterdir()),
                [],
            )

    @unittest.skipUnless(
        getattr(bs4, "__file__", None),
        "requires installed BeautifulSoup in child process",
    )
    def test_pdf_path_is_extracted_in_isolated_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.pdf"
            document = pymupdf.open()

            try:
                page = document.new_page()
                page.insert_text(
                    (72, 72),
                    (
                        "Municipal document with searchable text. "
                        "This page verifies isolated PDF extraction. "
                    ) * 4,
                )
                document.save(path)
            finally:
                document.close()

            with redirect_stdout(StringIO()):
                content = extract_pdf(
                    path,
                    content_type="application/pdf",
                    final_url="https://sochi.ru/document.pdf",
                )

            self.assertEqual(content.source_page_count, 1)
            self.assertEqual(content.extraction_version, "pdf-ocr-v5")
            self.assertIn(
                "isolated PDF extraction",
                content.pages[0][1],
            )
            self.assertEqual(
                sorted(item.name for item in Path(directory).iterdir()),
                ["document.pdf"],
            )

    def test_isolated_pdf_timeout_removes_result_file(self) -> None:
        class TimeoutProcess:
            pid = 999_999_999

            @staticmethod
            def poll():
                return None

            @staticmethod
            def wait(*, timeout: int):
                raise subprocess.TimeoutExpired(
                    cmd=("python",),
                    timeout=timeout,
                )

            @staticmethod
            def terminate() -> None:
                return None

            @staticmethod
            def kill() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slow.pdf"
            path.write_bytes(b"%PDF-slow")
            local_settings = replace(
                content_processing_module.settings,
                pdf_processing_timeout_seconds=60,
            )

            with patch.object(
                content_processing_module,
                "settings",
                local_settings,
            ), patch(
                "crawler_v2.content_processing.subprocess.Popen",
                return_value=TimeoutProcess(),
            ), redirect_stdout(StringIO()):
                with self.assertRaises(
                    PdfExtractionTimeoutError
                ):
                    extract_pdf(
                        path,
                        content_type="application/pdf",
                        final_url="https://sochi.ru/slow.pdf",
                    )

            self.assertEqual(
                sorted(item.name for item in Path(directory).iterdir()),
                ["slow.pdf"],
            )

    def test_keyboard_interrupt_terminates_pdf_process_group(self) -> None:
        class InterruptedProcess:
            pid = 424242

            def __init__(self) -> None:
                self.wait_calls = 0

            def poll(self):
                return (
                    -signal.SIGTERM
                    if self.wait_calls >= 2
                    else None
                )

            def wait(self, *, timeout: int):
                self.wait_calls += 1

                if self.wait_calls == 1:
                    raise KeyboardInterrupt()

                return -signal.SIGTERM

            @staticmethod
            def terminate() -> None:
                return None

            @staticmethod
            def kill() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupt.pdf"
            path.write_bytes(b"%PDF-interrupt")
            process = InterruptedProcess()

            with patch(
                "crawler_v2.content_processing.subprocess.Popen",
                return_value=process,
            ), patch(
                "crawler_v2.content_processing.os.killpg"
            ) as kill_group, redirect_stdout(StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    extract_pdf(
                        path,
                        content_type="application/pdf",
                        final_url="https://sochi.ru/interrupt.pdf",
                    )

            kill_group.assert_called_once_with(
                process.pid,
                signal.SIGTERM,
            )
            self.assertEqual(
                sorted(item.name for item in Path(directory).iterdir()),
                ["interrupt.pdf"],
            )

    def test_fast_pdf_timeout_returns_metadata_for_background_ocr(self) -> None:
        class TimeoutProcess:
            pid = 999_999_998

            @staticmethod
            def poll():
                return None

            @staticmethod
            def wait(*, timeout: int):
                raise subprocess.TimeoutExpired(
                    cmd=("python",),
                    timeout=timeout,
                )

            @staticmethod
            def terminate() -> None:
                return None

            @staticmethod
            def kill() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fast-timeout.pdf"
            path.write_bytes(b"%PDF-fast-timeout")
            local_settings = replace(
                content_processing_module.settings,
                pdf_fast_processing_timeout_seconds=30,
            )

            with patch.object(
                content_processing_module,
                "settings",
                local_settings,
            ), patch(
                "crawler_v2.content_processing.subprocess.Popen",
                return_value=TimeoutProcess(),
            ), redirect_stdout(StringIO()):
                content = extract_pdf(
                    path,
                    content_type="application/pdf",
                    final_url=(
                        "https://sochi.ru/fast-timeout.pdf"
                    ),
                    ocr_mode=PDF_OCR_MODE_FAST,
                )

            self.assertEqual(
                content.extraction_version,
                PDF_FAST_EXTRACTION_VERSION,
            )
            self.assertEqual(content.ocr_deferred_pages, (1,))
            self.assertEqual(content.unreadable_pages, (1,))
            self.assertEqual(
                sorted(item.name for item in Path(directory).iterdir()),
                ["fast-timeout.pdf"],
            )


if __name__ == "__main__":
    unittest.main()
