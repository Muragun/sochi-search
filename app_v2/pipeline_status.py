from __future__ import annotations

import copy
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from crawler_v2.pdf_ocr import (
    PDF_EXTRACTION_VERSION,
    PDF_FAST_EXTRACTION_VERSION,
)


LOADED_STATUSES = {
    "indexed",
    "indexed_existing",
    "unchanged",
}

PROBLEM_STATUSES = {
    "failed",
    "missing",
    "quarantined",
    "retry",
    "skipped_corrupt",
}

TERMINAL_STATUSES = {
    "gone",
    "quarantined",
    "skipped_corrupt",
    "skipped_unsupported",
    "skipped_too_large",
}

FORMAT_ORDER = (
    ("html", "HTML-страницы"),
    ("pdf", "PDF"),
    ("office", "Офисные документы"),
    ("text", "TXT и CSV"),
    ("archive", "Архивы"),
    ("other", "Прочее"),
)

OFFICE_SUFFIXES = {
    ".doc",
    ".docx",
    ".odt",
    ".ods",
    ".rtf",
    ".xls",
    ".xlsx",
}

ARCHIVE_SUFFIXES = {
    ".7z",
    ".gz",
    ".rar",
    ".tar",
    ".tgz",
    ".zip",
}


class PipelineStatusUnavailable(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def table_exists(
    connection: sqlite3.Connection,
    table_name: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def status_counts(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, int]:
    if not table_exists(connection, table_name):
        return {}

    return {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            f"""
            SELECT status, COUNT(*) AS count
            FROM {table_name}
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
    }


def suffix_from_url(url: str) -> str:
    path = str(url or "").split("?", 1)[0].casefold().rstrip("/")
    name = path.rsplit("/", 1)[-1]

    if "." not in name:
        return ""

    return "." + name.rsplit(".", 1)[-1]


def format_key(url: str, doc_type: str) -> str:
    suffix = suffix_from_url(url)
    normalized_doc_type = str(doc_type or "").casefold()

    if normalized_doc_type == "pdf" or suffix == ".pdf":
        return "pdf"
    if suffix in OFFICE_SUFFIXES:
        return "office"
    if suffix in {".csv", ".txt"}:
        return "text"
    if suffix in ARCHIVE_SUFFIXES:
        return "archive"
    if normalized_doc_type == "page" or suffix in {
        "",
        ".asp",
        ".aspx",
        ".cgi",
        ".htm",
        ".html",
        ".php",
    }:
        return "html"
    return "other"


def make_format_bucket(label: str) -> dict[str, Any]:
    return {
        "label": label,
        "total": 0,
        "loaded": 0,
        "remaining": 0,
        "processing": 0,
        "terminal": 0,
    }


def queue_summary(
    counts: dict[str, int],
    *,
    completed_statuses: set[str],
    pending_statuses: set[str],
    problem_statuses: set[str],
) -> dict[str, Any]:
    total = sum(counts.values())
    completed = sum(counts.get(name, 0) for name in completed_statuses)
    pending = sum(counts.get(name, 0) for name in pending_statuses)
    processing = counts.get("processing", 0)
    problems = sum(counts.get(name, 0) for name in problem_statuses)

    return {
        "available": bool(counts),
        "total": total,
        "completed": completed,
        "pending": pending,
        "processing": processing,
        "problems": problems,
        "statuses": counts,
    }


def latest_discovery(
    connection: sqlite3.Connection,
) -> Optional[dict[str, Any]]:
    if not table_exists(connection, "discovery_runs"):
        return None

    row = connection.execute(
        """
        SELECT
            started_at,
            finished_at,
            status,
            candidates_seen,
            new_urls_found,
            inserted_urls,
            sitemap_errors
        FROM discovery_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    return dict(row) if row is not None else None


# Сводка читает таблицу очереди целиком — на рабочем корпусе это около
# восьмидесяти тысяч строк. Страница поиска опрашивает /pipeline/status
# раз в тридцать секунд, панель управления — раз в пятнадцать, и каждая
# открытая вкладка добавляет свой опрос. Без кэша десяток вкладок
# устраивает серверу постоянный полный скан.
#
# Кэш общий на процесс, ключ — путь к базе. Срок берётся с запасом,
# чтобы страница поиска попадала в него между своими обновлениями.
CACHE_TTL_SECONDS = float(os.getenv("PIPELINE_STATUS_CACHE_SECONDS", "20"))

_cache: Dict[str, Tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def reset_pipeline_status_cache() -> None:
    """Сбрасывает кэш сводки. Нужен тестам и после запуска задачи."""

    with _cache_lock:
        _cache.clear()


def build_pipeline_status(
    database_path: Path,
    *,
    elasticsearch_documents: Optional[int] = None,
    elasticsearch_available: bool = True,
) -> dict[str, Any]:
    if not database_path.is_file():
        raise PipelineStatusUnavailable("SQLite crawler не найдена")

    key = str(database_path.resolve())
    now = time.monotonic()

    if CACHE_TTL_SECONDS > 0:
        with _cache_lock:
            cached = _cache.get(key)

            if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
                # Копия: вызывающий дополняет сводку своими полями, и
                # общий кэш не должен от этого меняться.
                return copy.deepcopy(cached[1])

    payload = collect_pipeline_status(
        database_path,
        elasticsearch_documents=elasticsearch_documents,
        elasticsearch_available=elasticsearch_available,
    )

    if CACHE_TTL_SECONDS > 0:
        with _cache_lock:
            _cache[key] = (now, copy.deepcopy(payload))

    return payload


def collect_pipeline_status(
    database_path: Path,
    *,
    elasticsearch_documents: Optional[int] = None,
    elasticsearch_available: bool = True,
) -> dict[str, Any]:
    """Читает очередь и собирает сводку. Без кэша, всегда заново."""

    if not database_path.is_file():
        raise PipelineStatusUnavailable("SQLite crawler не найдена")

    uri = database_path.resolve().as_uri() + "?mode=ro"

    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")

    except sqlite3.Error as exc:
        raise PipelineStatusUnavailable(
            f"SQLite crawler недоступна: {exc}"
        ) from exc

    try:
        if not table_exists(connection, "crawl_urls"):
            raise PipelineStatusUnavailable(
                "Таблица crawl_urls не найдена"
            )

        crawl_columns = table_columns(
            connection,
            "crawl_urls",
        )
        extraction_expression = (
            "extraction_version"
            if "extraction_version" in crawl_columns
            else "NULL"
        )

        rows = connection.execute(
            f"""
            SELECT
                url,
                COALESCE(doc_type, '') AS doc_type,
                status,
                COALESCE(is_active, 1) AS is_active,
                COALESCE(indexed_chunks, 0) AS indexed_chunks,
                last_indexed_at,
                {extraction_expression} AS extraction_version
            FROM crawl_urls
            """
        ).fetchall()

        formats = {
            key: make_format_bucket(label)
            for key, label in FORMAT_ORDER
        }
        counts: dict[str, int] = {}
        loaded_urls = 0
        remaining = 0
        processing = 0
        terminal = 0
        problems = 0
        indexed_chunks = 0
        latest_indexed_at: Optional[str] = None
        pdf_ocr_total = 0
        pdf_ocr_completed = 0
        pdf_ocr_processing = 0
        pdf_fast_indexed = 0

        for row in rows:
            status = str(row["status"] or "unknown")
            active = int(row["is_active"] or 0) == 1
            key = format_key(str(row["url"]), str(row["doc_type"]))
            bucket = formats[key]
            bucket["total"] += 1
            counts[status] = counts.get(status, 0) + 1

            loaded = status in LOADED_STATUSES
            row_processing = status == "processing"
            row_remaining = (
                active
                and not loaded
                and not row_processing
                and status not in TERMINAL_STATUSES
            )
            row_terminal = (
                status in TERMINAL_STATUSES
                or not active and not loaded
            )

            if loaded:
                loaded_urls += 1
                bucket["loaded"] += 1

            if row_remaining:
                remaining += 1
                bucket["remaining"] += 1

            if row_processing:
                processing += 1
                bucket["processing"] += 1

            if row_terminal:
                terminal += 1
                bucket["terminal"] += 1

            if status in PROBLEM_STATUSES:
                problems += 1

            if (
                key == "pdf"
                and active
            ):
                pdf_ocr_total += 1

                if (
                    row["extraction_version"]
                    == PDF_EXTRACTION_VERSION
                ):
                    pdf_ocr_completed += 1
                elif (
                    row["extraction_version"]
                    == PDF_FAST_EXTRACTION_VERSION
                ):
                    pdf_fast_indexed += 1

                    if row_processing:
                        pdf_ocr_processing += 1
                elif row_processing:
                    pdf_ocr_processing += 1

            indexed_chunks += int(row["indexed_chunks"] or 0)

            row_indexed_at = row["last_indexed_at"]

            if row_indexed_at and (
                latest_indexed_at is None
                or str(row_indexed_at) > latest_indexed_at
            ):
                latest_indexed_at = str(row_indexed_at)

        total = len(rows)
        handled = max(0, total - remaining - processing)
        completion_percent = round(
            (handled * 100.0 / total) if total else 100.0,
            1,
        )

        publication_counts = status_counts(
            connection,
            "publication_date_queue",
        )
        cleanup_counts = status_counts(
            connection,
            "content_cleanup_queue",
        )

        publication = queue_summary(
            publication_counts,
            completed_statuses={"done", "no_date", "skipped"},
            pending_statuses={"not_indexed", "pending", "retry"},
            problem_statuses={"failed", "no_date", "retry"},
        )
        cleanup = queue_summary(
            cleanup_counts,
            completed_statuses={"done", "skipped_out_of_scope"},
            pending_statuses={"pending", "retry"},
            problem_statuses={"failed", "retry"},
        )
        pdf_ocr_remaining = max(
            0,
            pdf_ocr_total
            - pdf_ocr_completed
            - pdf_ocr_processing,
        )
        pdf_ocr_percent = round(
            (
                pdf_ocr_completed
                * 100.0
                / pdf_ocr_total
            )
            if pdf_ocr_total
            else 100.0,
            1,
        )

        return {
            "status": "ok",
            "generated_at": utc_now(),
            "database_updated_at": datetime.fromtimestamp(
                database_path.stat().st_mtime,
                timezone.utc,
            ).isoformat(),
            "crawler": {
                "total_urls": total,
                "loaded_urls": loaded_urls,
                "remaining_urls": remaining,
                "processing_urls": processing,
                "handled_urls": handled,
                "terminal_urls": terminal,
                "problem_urls": problems,
                "completion_percent": completion_percent,
                "indexed_chunks_recorded": indexed_chunks,
                "latest_indexed_at": latest_indexed_at,
                "statuses": counts,
                "formats": [
                    {"key": key, **formats[key]}
                    for key, _label in FORMAT_ORDER
                ],
            },
            "elasticsearch": {
                "available": elasticsearch_available,
                "documents": elasticsearch_documents,
            },
            "publication_date": publication,
            "content_cleanup": cleanup,
            "pdf_ocr": {
                "available": (
                    "extraction_version"
                    in crawl_columns
                ),
                "version": (
                    PDF_EXTRACTION_VERSION
                ),
                "fast_version": (
                    PDF_FAST_EXTRACTION_VERSION
                ),
                "total": pdf_ocr_total,
                "completed": (
                    pdf_ocr_completed
                ),
                "remaining": (
                    pdf_ocr_remaining
                ),
                "processing": (
                    pdf_ocr_processing
                ),
                "fast_indexed": (
                    pdf_fast_indexed
                ),
                "completion_percent": (
                    pdf_ocr_percent
                ),
            },
            "latest_discovery": latest_discovery(connection),
        }

    except sqlite3.Error as exc:
        raise PipelineStatusUnavailable(
            f"Ошибка чтения SQLite crawler: {exc}"
        ) from exc

    finally:
        connection.close()
