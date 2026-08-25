from __future__ import annotations

import argparse
import hashlib
import os
import socket
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import settings
from .content_processing import (
    CorruptDocumentError,
    EmptyContentError,
    PDF_OCR_MODE_BOUNDED,
    PDF_OCR_MODE_FAST,
    PdfExtractionRuntimeError,
    UnsupportedContentTypeError,
    calculate_content_hash,
    classify_content_category,
    extract_content,
    split_into_chunks,
)
from .es_writer import ElasticsearchWriter
from .fetcher import DocumentTooLargeError, HttpFetcher
from .legal_db import LegalMetadataDatabase
from .legal_metadata import (
    extract_legal_title_metadata,
)
from .pdf_ocr import PDF_EXTRACTION_VERSION
from .text_repair import normalize_document_number, technical_filename
from .incremental_db import (
    IncrementalStateDatabase,
    iso_after_hours,
    iso_now,
)


class HttpStatusError(RuntimeError):
    def __init__(
        self,
        status_code: int,
    ) -> None:
        self.status_code = status_code

        super().__init__(
            f"HTTP {status_code}"
        )


def local_worker_is_alive(
    worker_id: str,
    *,
    hostname: str,
    proc_root: Path = Path("/proc"),
) -> bool | None:
    owner_host, separator, raw_pid = (
        worker_id.rpartition(":")
    )

    if (
        not separator
        or owner_host != hostname
    ):
        return None

    try:
        pid = int(raw_pid)
    except ValueError:
        return None

    if pid < 1:
        return False

    process_path = proc_root / str(pid)

    if not process_path.exists():
        return False

    try:
        command_line = (
            process_path / "cmdline"
        ).read_bytes()
    except OSError:
        return None

    return (
        b"crawler_v2.incremental_worker"
        in command_line
    )


def release_orphaned_local_locks(
    database: IncrementalStateDatabase,
    *,
    hostname: str,
    proc_root: Path = Path("/proc"),
) -> int:
    orphaned = tuple(
        worker_id
        for worker_id in (
            database.processing_lock_owners()
        )
        if local_worker_is_alive(
            worker_id,
            hostname=hostname,
            proc_root=proc_root,
        ) is False
    )

    return database.release_worker_locks(
        orphaned,
        reason=(
            "Released orphaned local worker lock"
        ),
    )


GENERIC_PDF_TITLE_PARTS = (
    "официальное опубликование муниципальных правовых актов",
    "администрация города сочи",
    "pdf-документ",
    "скачать документ",
    "скачать файл",
)

LEGAL_TITLE_MARKERS = (
    "Постановление",
    "Распоряжение",
    "Решение",
    "Приказ",
    "Положение",
    "Указ",
)

AUTHORITATIVE_METADATA_FIELDS = (
    "document_number",
    "document_date",
    "document_kind",
)


def useful_pdf_title(value: Any) -> bool:
    normalized = " ".join(
        str(value or "").split()
    )

    if len(normalized) < 4:
        return False

    lowered = normalized.casefold()

    if lowered in {
        "pdf",
        "файл",
        "документ",
    }:
        return False

    if technical_filename(normalized):
        return False

    has_legal_marker = any(
        marker.casefold() in lowered
        for marker in LEGAL_TITLE_MARKERS
    )
    has_generic_part = any(
        part in lowered
        for part in GENERIC_PDF_TITLE_PARTS
    )

    if has_generic_part and not has_legal_marker:
        return False

    if has_legal_marker or any(
        character.isdigit()
        for character in normalized
    ):
        return True

    return True


def apply_authoritative_attachment_metadata(
    row: dict[str, Any],
    content: Any,
) -> Any:
    """
    Для вложения официальная HTML-карточка является источником реквизитов.

    В тексте старого PDF номер и дата могут быть повреждены, хотя основной
    русский текст остаётся читаемым. В таком случае универсальный парсер PDF
    способен принять номер или дату из содержания документа за реквизиты.
    Метаданные родительской карточки структурированы и потому приоритетны.
    """

    if (
        content.doc_type not in {"pdf", "file"}
        or not row.get("parent_url")
    ):
        return content

    title_metadata = (
        extract_legal_title_metadata(
            str(
                row.get("attachment_title")
                or ""
            )
        )
    )
    replacements: dict[str, Any] = {}

    for field in AUTHORITATIVE_METADATA_FIELDS:
        parent_field = f"_parent_{field}"
        parent_value = str(
            (
                row.get(parent_field)
                if parent_field in row
                else row.get(field)
            )
            or ""
        ).strip()
        title_value = str(
            getattr(
                title_metadata,
                field,
                None,
            )
            or ""
        ).strip()

        replacements[field] = (
            parent_value
            or title_value
            or None
        )

    return replace(
        content,
        **replacements,
    )


def fetch_result_payload(
    result: Any,
) -> Any:
    return getattr(
        result,
        "payload",
        result.body,
    )


def fetch_result_size(
    result: Any,
) -> int:
    value = getattr(
        result,
        "size_bytes",
        None,
    )

    if value is not None:
        return int(value)

    return len(result.body)


def calculate_source_hash(
    payload: bytes | Path,
) -> str:
    digest = hashlib.sha256()

    if isinstance(payload, Path):
        with payload.open("rb") as stream:
            while True:
                chunk = stream.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                digest.update(chunk)

    else:
        digest.update(payload)

    return digest.hexdigest()


def cleanup_fetch_result(
    result: Any,
) -> None:
    cleanup = getattr(
        result,
        "cleanup",
        None,
    )

    if callable(cleanup):
        cleanup()


def resolve_attachment_parent_metadata(
    row: dict[str, Any],
    content: Any,
    *,
    fetcher: HttpFetcher,
) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Дополняет неполные реквизиты вложения непосредственно с HTML-карточки.

    Обычно значения берутся одним SQL-запросом из родительской строки. Запрос к
    карточке нужен только для старых записей, где родитель уже был помечен как
    обработанный, но его номер остался пустым из-за маркера вида ``No.620-р``.
    """

    resolved = dict(row)
    parent_url = str(
        resolved.get("parent_url")
        or ""
    ).strip()

    if (
        content.doc_type not in {"pdf", "file"}
        or not parent_url
    ):
        return resolved, {}

    current: dict[str, str] = {}

    for field in AUTHORITATIVE_METADATA_FIELDS:
        parent_field = f"_parent_{field}"
        value = (
            resolved.get(parent_field)
            if parent_field in resolved
            else resolved.get(field)
        )
        normalized = str(
            value or ""
        ).strip()

        if normalized:
            current[field] = normalized

    if all(
        current.get(field)
        for field in AUTHORITATIVE_METADATA_FIELDS
    ):
        return resolved, {}

    parent_result: Any = None

    try:
        parent_result = fetcher.fetch(
            parent_url,
            etag=None,
            last_modified=None,
        )

        if parent_result.status_code != 200:
            print(
                "[PARENT METADATA] Карточка вернула HTTP "
                f"{parent_result.status_code}; сохранены доступные "
                "реквизиты"
            )
            return resolved, {}

        parent_content = extract_content(
            fetch_result_payload(
                parent_result
            ),
            content_type=(
                parent_result.content_type
            ),
            final_url=parent_result.final_url,
        )

    except Exception as exc:
        print(
            "[PARENT METADATA] Не удалось прочитать карточку: "
            f"{type(exc).__name__}: {exc}"
        )
        return resolved, {}

    finally:
        if parent_result is not None:
            cleanup_fetch_result(
                parent_result
            )

    fetched: dict[str, str] = {}

    for field in AUTHORITATIVE_METADATA_FIELDS:
        value = str(
            getattr(
                parent_content,
                field,
                None,
            )
            or ""
        ).strip()

        if not value:
            continue

        fetched[field] = value
        resolved[f"_parent_{field}"] = value

    if fetched:
        print(
            "[PARENT METADATA] Получены реквизиты официальной "
            "карточки: "
            f"номер={fetched.get('document_number', 'не найден')}; "
            f"дата={fetched.get('document_date', 'не найдена')}; "
            f"тип={fetched.get('document_kind', 'не найден')}"
        )
    else:
        print(
            "[PARENT METADATA] В официальной карточке реквизиты "
            "не найдены"
        )

    return resolved, fetched


def title_from_pdf_text(content: Any) -> str:
    text = " ".join(
        page_text
        for _page_number, page_text
        in content.pages[:3]
    )
    normalized = " ".join(
        text.split()
    )

    if not normalized:
        return ""

    lowered = normalized.casefold()
    positions = [
        lowered.find(marker.casefold())
        for marker in LEGAL_TITLE_MARKERS
    ]
    positions = [
        position
        for position in positions
        if position >= 0
    ]

    if not positions:
        return ""

    candidate = normalized[
        min(positions):
        min(positions) + 300
    ]

    for separator in (
        "»",
        ". ",
        "; ",
    ):
        position = candidate.find(
            separator,
            70,
        )

        if position >= 0:
            candidate = candidate[
                :position + len(separator.strip())
            ]
            break

    return candidate[:240].strip()


def best_content_title(
    row: dict[str, Any],
    content: Any,
) -> str:
    if content.doc_type not in {
        "pdf",
        "file",
    }:
        return str(content.title)

    attachment_title = str(
        row.get("attachment_title")
        or ""
    ).strip()

    if useful_pdf_title(
        attachment_title
    ):
        return attachment_title

    document_kind = content.document_kind
    document_number = content.document_number
    document_date = content.document_date

    if document_kind and (
        document_number
        or document_date
    ):
        parts = [
            str(document_kind)
        ]

        if document_number:
            parts.append(
                f"№ {document_number}"
            )

        if document_date:
            parts.append(
                f"от {document_date}"
            )

        return " ".join(parts)

    content_title = str(
        content.title
        or ""
    ).strip()

    if useful_pdf_title(
        content_title
    ):
        return content_title

    derived = title_from_pdf_text(
        content
    )

    if derived:
        return derived

    return (
        attachment_title
        or content_title
        or "PDF-документ"
    )


def choose_fetch_url(
    row: dict[str, Any],
) -> str:
    doc_type = str(
        row.get("doc_type")
        or ""
    ).lower()

    file_url = str(
        row.get("file_url")
        or ""
    ).strip()

    page_url = str(
        row.get("page_url")
        or ""
    ).strip()

    identity_url = str(
        row.get("url")
        or ""
    ).strip()

    if doc_type in {"pdf", "file"} and file_url:
        return file_url

    return (
        identity_url
        or file_url
        or page_url
    )


def source_name(
    row: dict[str, Any],
    final_url: str,
) -> str:
    existing = str(
        row.get("source")
        or ""
    ).strip()

    if existing:
        return existing

    return (
        urlparse(final_url).hostname
        or "sochi.ru"
    )


def build_documents(
    row: dict[str, Any],
    *,
    final_url: str,
    content: Any,
    content_hash: str,
    fetched_at: str,
) -> list[dict[str, Any]]:
    chunks = split_into_chunks(
        content
    )

    identity_url = str(
        row["url"]
    )

    title = best_content_title(
        row,
        content,
    )
    if content.doc_type in {"pdf", "file"}:
        document_number = content.document_number
        document_date = content.document_date
        document_kind = content.document_kind
    else:
        document_number = (
            content.document_number
            or row.get("document_number")
        )
        document_date = (
            content.document_date
            or row.get("document_date")
        )
        document_kind = (
            content.document_kind
            or row.get("document_kind")
        )

    content_category = (
        content.content_category
    )
    site_section = (
        content.site_section
    )

    parent_for_category = str(
        row.get("parent_url")
        or row.get("page_url")
        or ""
    )

    if (
        content.doc_type in {"pdf", "file"}
        and parent_for_category
    ):
        (
            parent_category,
            parent_section,
        ) = classify_content_category(
            parent_for_category
        )

        if not content_category:
            content_category = (
                parent_category
            )

        if not site_section:
            site_section = (
                parent_section
            )

    ocr_pages = set(
        content.ocr_pages
    )
    unreadable_pages = set(
        content.unreadable_pages
    )

    # Канонический вид номера акта — той же функцией, что нормализует
    # запрос. Без него точное совпадение зависело от того, как номер
    # записан в источнике: «№ 512-р» попадал в индекс как «№  512-р», а
    # искался как «512-р», и точное совпадение с весом 40 не срабатывало.
    normalized_document_number = (
        normalize_document_number(document_number) or None
        if document_number
        else None
    )

    # Средняя уверенность Tesseract по распознанным страницам документа.
    #
    # None, а не ноль, когда OCR не запускался: ноль в поле `float`
    # означал бы «распознано отвратительно» и попал бы в отбор страниц
    # на переделку, хотя распознавать было нечего.
    ocr_confidence = (
        round(
            sum(content.ocr_confidences)
            / len(content.ocr_confidences),
            2,
        )
        if content.ocr_confidences
        else None
    )

    documents: list[dict[str, Any]] = []

    for chunk in chunks:
        if content.doc_type in {"pdf", "file"}:
            file_url = final_url
            page_url = (
                row.get("parent_url")
                or row.get("page_url")
                or row.get("url")
            )
        else:
            file_url = (
                row.get("file_url")
                or None
            )
            page_url = final_url

        documents.append(
            {
                "url": identity_url,
                "page_url": page_url,
                "file_url": file_url,
                "source": source_name(
                    row,
                    final_url,
                ),
                "doc_type": content.doc_type,
                "content_type": (
                    content.content_type
                ),
                "hash": content_hash,
                "fetched_at": fetched_at,
                "updated_at": fetched_at,

                "published_at": (
                    content.published_at
                ),

                "sort_date": (
                    document_date
                    or content.published_at
                ),

                "document_number": document_number,
                "document_number_normalized": (
                    normalized_document_number
                ),
                "document_date": document_date,
                "document_kind": document_kind,

                "parent_url": (
                    row.get("parent_url")
                    or None
                ),
                "attachment_title": (
                    row.get("attachment_title")
                    or None
                ),
                "is_attachment": (
                    content.doc_type in {"pdf", "file"}
                ),

                "title": title,
                "section": content.section,

                "content_category": content_category,
                "site_section": site_section,
                "text": chunk.text,
                "page_number": (
                    chunk.page_number
                ),
                "chunk_number": (
                    chunk.chunk_number
                ),
                "extraction_version": (
                    content.extraction_version
                ),
                "text_extraction": (
                    "ocr"
                    if chunk.page_number in ocr_pages
                    else (
                        "metadata_only"
                        if chunk.page_number
                        in unreadable_pages
                        else "native"
                    )
                ),
                "ocr_used": (
                    chunk.page_number in ocr_pages
                ),
                "source_page_count": (
                    content.source_page_count
                ),
                "ocr_page_count": len(
                    content.ocr_pages
                ),
                "ocr_confidence": ocr_confidence,
                "ocr_attempted_page_count": len(
                    content.ocr_attempted_pages
                ),
                "ocr_rejected_page_count": len(
                    content.ocr_rejected_pages
                ),
                "unreadable_page_count": len(
                    content.unreadable_pages
                ),
            }
        )

    return documents


def process_row(
    row: dict[str, Any],
    *,
    database: IncrementalStateDatabase,
    legal_database: LegalMetadataDatabase,
    fetcher: HttpFetcher,
    writer: ElasticsearchWriter,
    dry_run: bool,
    ignore_http_cache: bool = False,
    pdf_ocr_mode: str = PDF_OCR_MODE_BOUNDED,
) -> str:
    identity_url = str(
        row["url"]
    )

    fetch_url = choose_fetch_url(
        row
    )

    if not fetch_url:
        raise RuntimeError(
            "У записи отсутствует URL"
        )

    print()
    print("=" * 90)
    print(f"URL: {identity_url}")
    print(f"GET: {fetch_url}")

    result = fetcher.fetch(
        fetch_url,
        etag=(
            None
            if ignore_http_cache
            else row.get("etag")
            or None
        ),
        last_modified=(
            None
            if ignore_http_cache
            else row.get("last_modified")
            or None
        ),
    )

    checked_at = iso_now()

    print(
        f"HTTP: {result.status_code}"
    )
    print(
        f"Final URL: {result.final_url}"
    )
    print(
        f"Content-Type: {result.content_type}"
    )
    print(
        f"Bytes: {fetch_result_size(result)}"
    )

    if result.status_code == 304:
        print(
            "[UNCHANGED] Сервер вернул 304"
        )

        if not dry_run:
            database.mark_not_modified(
                identity_url,
                checked_at=checked_at,
                next_check_at=iso_after_hours(
                    settings.recheck_hours
                ),
                etag=result.etag,
                last_modified=(
                    result.last_modified
                ),
            )

        return "unchanged"

    if result.status_code in {
        404,
        410,
    }:
        current_missing = int(
            row.get(
                "consecutive_missing"
            )
            or 0
        )

        new_missing = (
            current_missing + 1
        )

        should_delete = (
            new_missing
            >= settings.delete_missing_after
        )

        print(
            "[MISSING] "
            f"Последовательных ответов: "
            f"{new_missing}/"
            f"{settings.delete_missing_after}"
        )

        if dry_run:
            return "missing"

        deleted = False

        if should_delete:
            deleted_count = (
                writer.delete_all_for_url(
                    url=identity_url
                )
            )

            deleted = True

            print(
                "[DELETE] Удалено фрагментов "
                f"Elasticsearch: {deleted_count}"
            )

        database.mark_missing(
            identity_url,
            http_status=result.status_code,
            checked_at=checked_at,
            next_check_at=iso_after_hours(
                settings.missing_retry_hours
            ),
            deleted_from_index=deleted,
        )

        return (
            "gone"
            if deleted
            else "missing"
        )

    if result.status_code >= 400:
        raise HttpStatusError(
            result.status_code
        )

    payload = fetch_result_payload(
        result
    )
    source_hash: str | None = None

    try:
        if isinstance(payload, Path):
            source_hash = (
                calculate_source_hash(
                    payload
                )
            )
            print(
                "PDF source SHA-256:",
                source_hash,
            )

        if (
            not dry_run
            and pdf_ocr_mode
            == PDF_OCR_MODE_FAST
            and source_hash
            and str(
                row.get(
                    "extraction_version"
                )
                or ""
            )
            == PDF_EXTRACTION_VERSION
            and (
                not str(
                    row.get("source_hash")
                    or ""
                )
                or source_hash
                == str(
                    row.get("source_hash")
                    or ""
                )
            )
        ):
            if row.get("source_hash"):
                print(
                    "[PDF SOURCE UNCHANGED] "
                    "Файл не изменился; готовые OCR-фрагменты сохранены"
                )
            else:
                print(
                    "[PDF SOURCE BASELINE] "
                    "Зафиксирован исходный SHA-256; прежние "
                    "OCR-фрагменты сохранены"
                )
            database.mark_source_unchanged(
                identity_url,
                checked_at=checked_at,
                next_check_at=iso_after_hours(
                    settings.recheck_hours
                ),
                fetched_at=checked_at,
                source_hash=source_hash,
                etag=result.etag,
                last_modified=(
                    result.last_modified
                ),
            )
            return "unchanged"

        content = extract_content(
            payload,
            content_type=result.content_type,
            final_url=result.final_url,
            pdf_ocr_mode=pdf_ocr_mode,
        )
    finally:
        cleanup_fetch_result(result)
    row, refreshed_parent_metadata = (
        resolve_attachment_parent_metadata(
            row,
            content,
            fetcher=fetcher,
        )
    )
    content = (
        apply_authoritative_attachment_metadata(
            row,
            content,
        )
    )

    if (
        content.doc_type in {"pdf", "file"}
        and row.get("parent_url")
        and not dry_run
    ):
        legal_database.replace_attachment_metadata(
            attachment_url=identity_url,
            document_number=(
                content.document_number
            ),
            document_date=(
                content.document_date
            ),
            document_kind=(
                content.document_kind
            ),
        )

    if refreshed_parent_metadata and not dry_run:
        legal_database.save_resolved_attachment_metadata(
            parent_url=str(row["parent_url"]),
            attachment_url=identity_url,
            document_number=(
                refreshed_parent_metadata.get(
                    "document_number"
                )
            ),
            document_date=(
                refreshed_parent_metadata.get(
                    "document_date"
                )
            ),
            document_kind=(
                refreshed_parent_metadata.get(
                    "document_kind"
                )
            ),
        )

    print(
        "Номер документа:",
        content.document_number
        or "не найден",
    )

    print(
        "Дата документа:",
        content.document_date
        or "не найдена",
    )

    print(
        "Тип документа:",
        content.document_kind
        or "не найден",
    )

    print(
        "Найдено вложений:",
        len(content.attachments),
    )

    if content.doc_type == "pdf":
        print(
            "PDF extraction version:",
            content.extraction_version,
        )
        print(
            "PDF страниц всего:",
            content.source_page_count,
        )
        print(
            "PDF OCR страниц:",
            len(content.ocr_pages),
        )
        print(
            "PDF OCR попыток:",
            len(content.ocr_attempted_pages),
        )
        print(
            "PDF OCR отклонено:",
            len(content.ocr_rejected_pages),
        )
        print(
            "PDF OCR таймаутов страниц:",
            len(content.ocr_timed_out_pages),
        )
        print(
            "PDF OCR пропущено по бюджету:",
            len(
                content.ocr_budget_skipped_pages
            ),
        )
        print(
            "PDF OCR отложено в фон:",
            len(content.ocr_deferred_pages),
        )

        for reason in (
            content.ocr_rejection_reasons[:5]
        ):
            print(
                "PDF OCR причина:",
                reason,
            )

        print(
            "PDF нечитаемых страниц:",
            len(content.unreadable_pages),
        )

    for attachment in (
        content.attachments
    ):
        print(
            "[ATTACHMENT]",
            attachment.title,
            "->",
            attachment.url,
        )

    if (
        content.doc_type == "page"
        and not dry_run
    ):
        legal_database.save_page_metadata(
            url=identity_url,
            document_number=(
                content.document_number
            ),
            document_date=(
                content.document_date
            ),
            document_kind=(
                content.document_kind
            ),
        )

        inserted_attachments, (
            existing_attachments
        ) = legal_database.upsert_attachments(
            content.attachments,
            parent_url=identity_url,
            seen_at=checked_at,
        )

        print(
            "[ATTACHMENTS] Добавлено:",
            inserted_attachments,
        )

        print(
            "[ATTACHMENTS] Уже известно:",
            existing_attachments,
        )

    content_hash = (
        calculate_content_hash(
            content
        )
    )

    old_hash = str(
        row.get("content_hash")
        or ""
    )

    print(f"Тип: {content.doc_type}")
    print(
        "Заголовок:",
        best_content_title(
            row,
            content,
        ),
    )
    print(
        f"Страниц с текстом: "
        f"{len(content.pages)}"
    )
    print(f"Новый SHA-256: {content_hash}")
    print(
        f"Старый SHA-256: "
        f"{old_hash or 'отсутствует'}"
    )

    fetched_at = checked_at

    if old_hash == content_hash:
        print(
            "[UNCHANGED] Хеш содержимого "
            "не изменился"
        )

        if not dry_run:
            database.mark_unchanged(
                identity_url,
                checked_at=checked_at,
                next_check_at=iso_after_hours(
                    settings.recheck_hours
                ),
                fetched_at=fetched_at,
                content_hash=content_hash,
                content_type=(
                    content.content_type
                ),
                doc_type=content.doc_type,
                etag=result.etag,
                last_modified=(
                    result.last_modified
                ),
                extraction_version=(
                    content.extraction_version
                ),
                source_hash=source_hash,
            )

        return "unchanged"

    documents = build_documents(
        row,
        final_url=result.final_url,
        content=content,
        content_hash=content_hash,
        fetched_at=fetched_at,
    )

    print(
        f"[CHANGED] Новых фрагментов: "
        f"{len(documents)}"
    )

    if dry_run:
        for number, document in enumerate(
            documents[:3],
            start=1,
        ):
            print()
            print(
                f"Фрагмент {number}: "
                f"page={document['page_number']} "
                f"chunk={document['chunk_number']}"
            )

            print(
                str(document["text"])[:500]
            )

        print(
            "[DRY RUN] Elasticsearch и "
            "SQLite не изменены"
        )

        return "would_index"

    writer.bulk_index(
        documents
    )

    deleted_count = (
        writer.delete_stale_chunks(
            url=identity_url,
            current_hash=content_hash,
        )
    )

    # Идентификаторы фрагментов детерминированы (url + страница + номер
    # фрагмента), а `delete_stale_chunks` только что убрал записи с прежним
    # hash. Значит, после операции у URL ровно столько фрагментов, сколько
    # было записано. Отдельный `_count` здесь давал бы устаревшее число:
    # индекс обновляется раз в 5 секунд, а не синхронно с записью.
    actual_count = len(documents)

    database.mark_indexed(
        identity_url,
        checked_at=checked_at,
        next_check_at=iso_after_hours(
            settings.recheck_hours
        ),
        fetched_at=fetched_at,
        indexed_at=iso_now(),
        content_hash=content_hash,
        content_type=content.content_type,
        doc_type=content.doc_type,
        indexed_chunks=actual_count,
        etag=result.etag,
        last_modified=result.last_modified,
        final_url=result.final_url,
        extraction_version=(
            content.extraction_version
        ),
        source_hash=source_hash,
    )

    print(
        "[INDEXED] Записано новых "
        f"фрагментов: {len(documents)}"
    )

    print(
        "[INDEXED] Удалено старых "
        f"фрагментов: {deleted_count}"
    )

    print(
        "[INDEXED] Текущее число "
        f"фрагментов URL: {actual_count}"
    )

    return "indexed"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Инкрементальное обновление "
            "известных URL sochi.ru"
        )
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=settings.worker_batch_limit,
        help="Максимальное число URL",
    )

    parser.add_argument(
        "--url",
        help=(
            "Обработать один конкретный URL "
            "из SQLite"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Скачать и обработать, но не "
            "изменять Elasticsearch и SQLite"
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Игнорировать next_check_at"
        ),
    )

    parser.add_argument(
        "--no-http-cache",
        action="store_true",
        help=(
            "Не передавать ETag/Last-Modified "
            "при контрольной переобработке"
        ),
    )

    parser.add_argument(
        "--pdf-ocr-backfill",
        action="store_true",
        help=(
            "Обрабатывать только PDF, которые ещё "
            f"не прошли {PDF_EXTRACTION_VERSION}"
        ),
    )

    parser.add_argument(
        "--batch",
        action="store_true",
        help=(
            "Обрабатывать документы подряд до внутреннего дедлайна, "
            "а не по одному за запуск"
        ),
    )

    parser.add_argument(
        "--worker-slot",
        default="",
        help=(
            "Номер слота. Входит в идентификатор воркера, чтобы аренда "
            "URL в SQLite различала параллельные слоты"
        ),
    )

    parser.add_argument(
        "--batch-deadline-seconds",
        type=int,
        default=(
            settings.pdf_ocr_batch_deadline_seconds
        ),
        help=(
            "Мягкий предел одного запуска. Должен быть заметно меньше "
            "TimeoutStartSec службы, иначе systemd снимет процесс "
            "на середине документа"
        ),
    )

    parser.add_argument(
        "--batch-documents",
        type=int,
        default=settings.pdf_ocr_batch_documents,
        help="Максимум документов за один запуск",
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    if arguments.url is not None:
        arguments.url = arguments.url.strip()

        if not arguments.url:
            print(
                "--url передан, но его значение пустое",
                file=sys.stderr,
            )
            return 2

    if arguments.limit < 1:
        print(
            "--limit должен быть больше нуля",
            file=sys.stderr,
        )
        return 2

    database = IncrementalStateDatabase(
        settings.state_db_path
    )

    database.migrate()

    legal_database = LegalMetadataDatabase(
        settings.state_db_path
    )

    legal_database.migrate()

    hostname = socket.gethostname()

    orphaned = release_orphaned_local_locks(
        database,
        hostname=hostname,
    )

    if orphaned:
        print(
            f"[LOCKS] Освобождено локальных "
            f"блокировок завершённых worker: {orphaned}"
        )

    released = database.release_stale_locks(
        older_than_minutes=(
            settings.lock_timeout_minutes
        )
    )

    if released:
        print(
            f"[LOCKS] Освобождено "
            f"зависших блокировок: {released}"
        )

    slot = str(
        getattr(arguments, "worker_slot", "") or ""
    ).strip()

    worker_id = (
        f"{hostname}:slot{slot}:{os.getpid()}"
        if slot
        else f"{hostname}:{os.getpid()}"
    )

    required_extraction_version = (
        PDF_EXTRACTION_VERSION
        if arguments.pdf_ocr_backfill
        else None
    )
    pdf_ocr_mode = (
        PDF_OCR_MODE_BOUNDED
        if arguments.pdf_ocr_backfill
        else PDF_OCR_MODE_FAST
    )
    force_selection = (
        arguments.force
        or arguments.pdf_ocr_backfill
    )
    ignore_http_cache = (
        arguments.no_http_cache
        or arguments.pdf_ocr_backfill
    )

    fetcher = HttpFetcher()
    writer = ElasticsearchWriter()

    counters = {
        "indexed": 0,
        "unchanged": 0,
        "would_index": 0,
        "missing": 0,
        "gone": 0,
        "skipped_corrupt": 0,
        "skipped_empty": 0,
        "skipped_unsupported": 0,
        "retry": 0,
        "quarantined": 0,
        "skipped_too_large": 0,
        "failed": 0,
    }
    interrupted = False
    deadline_reached = False
    batch_started = time.monotonic()
    batch_rounds = 0
    processed_documents = 0

    # В пакетном режиме запуск обрабатывает документы подряд, пока не
    # выйдет время или не кончится очередь. Прежняя схема брала один
    # документ за срабатывание таймера, и на очереди в десять тысяч
    # файлов накладные расходы на старт превышали полезную работу.
    while True:
        rows = database.peek_candidates(
            limit=(
                1
                if arguments.url
                else arguments.limit
            ),
            specific_url=arguments.url,
            force=force_selection,
            required_extraction_version=(
                required_extraction_version
            ),
            require_existing_index=(
                arguments.pdf_ocr_backfill
            ),
        )

        if not rows:
            if batch_rounds == 0:
                if arguments.pdf_ocr_backfill:
                    print(
                        "[OK] Все уже индексированные PDF прошли "
                        f"{PDF_EXTRACTION_VERSION}"
                    )
                else:
                    print(
                        "[OK] Нет URL, готовых "
                        "к обработке"
                    )
            break

        batch_rounds += 1

        print(
            f"[START] worker={worker_id} "
            f"urls={len(rows)} "
            f"dry_run={arguments.dry_run} "
            f"pdf_ocr_backfill={arguments.pdf_ocr_backfill} "
            f"pdf_mode={pdf_ocr_mode} "
            "claim_mode=one-at-a-time"
        )

        for scheduled_row in rows:
            # Дедлайн проверяется перед каждым документом, а не только между
            # пачками: иначе один тяжёлый PDF выносит запуск за
            # TimeoutStartSec и systemd снимает службу на середине работы.
            if (
                arguments.batch
                and time.monotonic() - batch_started
                >= arguments.batch_deadline_seconds
            ):
                deadline_reached = True
                break

            processed_documents += 1
            row = scheduled_row
            identity_url = str(
                row["url"]
            )

            if not arguments.dry_run:
                claimed = database.claim_candidates(
                    limit=1,
                    worker_id=worker_id,
                    specific_url=identity_url,
                    exact_url=True,
                    force=True,
                    required_extraction_version=(
                        required_extraction_version
                    ),
                    require_existing_index=(
                        arguments.pdf_ocr_backfill
                    ),
                )

                if not claimed:
                    print(
                        "[SKIP CLAIM] Состояние URL изменилось: "
                        f"{identity_url}"
                    )
                    continue

                row = claimed[0]

            try:
                result_status = process_row(
                    row,
                    database=database,
                    legal_database=legal_database,
                    fetcher=fetcher,
                    writer=writer,
                    dry_run=arguments.dry_run,
                    ignore_http_cache=(
                        ignore_http_cache
                    ),
                    pdf_ocr_mode=(
                        pdf_ocr_mode
                    ),
                )

                counters[result_status] = (
                    counters.get(
                        result_status,
                        0,
                    )
                    + 1
                )

            except KeyboardInterrupt:
                interrupted = True
                counters["retry"] += 1

                print(
                    f"[INTERRUPTED] {identity_url}: "
                    "дочерние PDF-процессы завершены",
                    file=sys.stderr,
                )

                if not arguments.dry_run:
                    released_current = (
                        database.release_worker_locks(
                            (worker_id,),
                            reason=(
                                "Manual interrupt: worker returned URL to retry"
                            ),
                        )
                    )
                    print(
                        "[INTERRUPTED] Возвращено в retry:",
                        released_current,
                        file=sys.stderr,
                    )

                break

            except DocumentTooLargeError as exc:
                # Файл существует, но слишком велик, чтобы разбирать его
                # целиком на этом сервере. Ставим отдельный терминальный
                # статус: документ виден в отчёте, не уходит в
                # бесконечные повторы и остаётся находимым по карточке
                # родительской страницы, где есть его название и
                # реквизиты.
                counters["skipped_too_large"] += 1

                print(
                    f"[TOO LARGE] {identity_url}: {exc}",
                    file=sys.stderr,
                )

                if not arguments.dry_run:
                    database.mark_skipped(
                        identity_url,
                        status="skipped_too_large",
                        checked_at=iso_now(),
                        next_check_at=iso_after_hours(
                            settings.recheck_hours * 4
                        ),
                        error_message=str(exc),
                    )

            except HttpStatusError as exc:
                next_failure_count = (
                    int(
                        row.get("failure_count")
                        or 0
                    )
                    + 1
                )

                quarantined = (
                    next_failure_count
                    >= settings.max_retry_attempts
                )

                result_status = (
                    "quarantined"
                    if quarantined
                    else "retry"
                )

                counters[result_status] += 1

                print(
                    f"[{result_status.upper()}] "
                    f"{identity_url}: {exc} "
                    f"({next_failure_count}/"
                    f"{settings.max_retry_attempts})",
                    file=sys.stderr,
                )

                if not arguments.dry_run:
                    database.mark_failed(
                        identity_url,
                        checked_at=iso_now(),
                        next_check_at=iso_after_hours(
                            settings.retry_hours
                        ),
                        error_message=str(exc),
                        http_status=exc.status_code,
                        quarantine=quarantined,
                    )

            except PdfExtractionRuntimeError as exc:
                counters["retry"] += 1

                print(
                    f"[RETRY PDF] {identity_url}: {exc}",
                    file=sys.stderr,
                )

                if not arguments.dry_run:
                    database.mark_failed(
                        identity_url,
                        checked_at=iso_now(),
                        next_check_at=iso_after_hours(
                            settings.retry_hours
                        ),
                        error_message=str(exc),
                    )

            except CorruptDocumentError as exc:
                counters["skipped_corrupt"] += 1

                print(
                    f"[SKIPPED CORRUPT] {identity_url}: {exc}",
                    file=sys.stderr,
                )

                if not arguments.dry_run:
                    database.mark_skipped(
                        identity_url,
                        status="skipped_corrupt",
                        checked_at=iso_now(),
                        next_check_at=iso_after_hours(
                            settings.recheck_hours
                        ),
                        error_message=str(exc),
                    )

            except EmptyContentError as exc:
                counters["skipped_empty"] += 1

                print(
                    f"[SKIPPED EMPTY] {identity_url}: {exc}",
                    file=sys.stderr,
                )

                if not arguments.dry_run:
                    database.mark_skipped(
                        identity_url,
                        status="skipped_empty",
                        checked_at=iso_now(),
                        next_check_at=iso_after_hours(
                            settings.recheck_hours
                        ),
                        error_message=str(exc),
                    )

            except UnsupportedContentTypeError as exc:
                counters["skipped_unsupported"] += 1

                print(
                    f"[SKIPPED TYPE] {identity_url}: {exc}",
                    file=sys.stderr,
                )

                if not arguments.dry_run:
                    database.mark_skipped(
                        identity_url,
                        status="skipped_unsupported",
                        checked_at=iso_now(),
                        next_check_at=iso_after_hours(
                            settings.recheck_hours
                        ),
                        error_message=str(exc),
                    )

            except Exception as exc:
                counters["failed"] += 1

                print(
                    f"[ERROR] {identity_url}: {exc}",
                    file=sys.stderr,
                )

                if not arguments.dry_run:
                    database.mark_failed(
                        identity_url,
                        checked_at=iso_now(),
                        next_check_at=iso_after_hours(
                            settings.retry_hours
                        ),
                        error_message=str(exc),
                    )

        if not arguments.batch:
            break

        if interrupted or deadline_reached:
            break

        if processed_documents >= arguments.batch_documents:
            print(
                "[BATCH] Достигнут предел документов за запуск: "
                f"{arguments.batch_documents}"
            )
            break

    if deadline_reached:
        print(
            "[BATCH] Достигнут предел времени запуска: "
            f"{arguments.batch_deadline_seconds} секунд"
        )

    if arguments.batch:
        print(
            f"[BATCH] Документов за запуск: {processed_documents}, "
            f"пачек: {batch_rounds}, "
            f"время: {int(time.monotonic() - batch_started)} секунд"
        )

    print()
    print("=" * 90)
    print("[FINISH]")

    for name, value in counters.items():
        print(f"{name}: {value}")

    return (
        130
        if interrupted
        else
        1
        if counters["failed"]
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
