from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath
from urllib.parse import (
    urljoin,
    urlsplit,
    urlunsplit,
)

from bs4 import BeautifulSoup
from bs4.element import Tag

from .config import settings


RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


RESERVED_LABELS = {
    "номер документа",
    "дата документа",
    "тип документа",
    "опубликовано на сайте",
    "скачать документ",
    "скачать файл",
    "формат файла",
    "размер",
}


LEGAL_KIND_CANONICAL = {
    "постановление": "Постановление",
    "распоряжение": "Распоряжение",
    "решение": "Решение",
    "приказ": "Приказ",
    "положение": "Положение",
    "указ": "Указ",
}


DOCUMENT_NUMBER_MARKER_RE = (
    r"(?:№|N[oо]?\.?)"
)


LEGAL_TITLE_KIND_RE = re.compile(
    (
        r"\b(постановление|распоряжение|решение|"
        r"приказ|положение|указ)\b"
    ),
    flags=re.IGNORECASE,
)


LEGAL_TITLE_NUMBER_RE = re.compile(
    (
        rf"{DOCUMENT_NUMBER_MARKER_RE}\s*"
        r"([0-9А-Яа-яЁё"
        r"\-/–—]+)"
    ),
    flags=re.IGNORECASE,
)


LEGAL_TITLE_DATE_RE = re.compile(
    (
        r"\bот\s+("
        r"(?:\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})"
        r"|"
        r"(?:\d{1,2}\s+[а-яё]+\s+\d{4}(?:\s+года)?)"
        r")"
    ),
    flags=re.IGNORECASE,
)


def normalized_label(value: str) -> str:
    return (
        compact_text(value)
        .rstrip(":")
        .strip()
        .casefold()
    )


def validate_document_number(
    value: str | None,
) -> str | None:
    if not value:
        return None

    normalized = compact_text(
        value
    ).strip(" .,:;")

    if not normalized:
        return None

    if normalized_label(
        normalized
    ) in RESERVED_LABELS:
        return None

    if len(normalized) > 100:
        return None

    if not re.search(
        r"\d",
        normalized,
    ):
        return None

    return normalized


def normalize_document_kind(
    value: str | None,
) -> str | None:
    if not value:
        return None

    normalized = compact_text(
        value
    ).casefold()

    if normalized_label(
        normalized
    ) in RESERVED_LABELS:
        return None

    for keyword, canonical in (
        LEGAL_KIND_CANONICAL.items()
    ):
        if re.search(
            rf"\b{re.escape(keyword)}\b",
            normalized,
        ):
            return canonical

    return None


@dataclass(frozen=True)
class AttachmentLink:
    url: str
    title: str


@dataclass(frozen=True)
class LegalMetadata:
    document_number: str | None
    document_date: str | None
    document_kind: str | None
    attachments: tuple[AttachmentLink, ...]


def compact_text(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def extract_labeled_value(
    text: str,
    label: str,
) -> str | None:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    expected_label = (
        label.strip()
        .rstrip(":")
        .casefold()
    )

    for index, line in enumerate(lines):
        normalized_line = (
            line.rstrip(":")
            .strip()
            .casefold()
        )

        if normalized_line == expected_label:
            if index + 1 >= len(lines):
                return None

            candidate = compact_text(
                lines[index + 1]
            )

            if normalized_label(
                candidate
            ) in RESERVED_LABELS:
                return None

            return candidate

        prefix = expected_label + ":"

        if line.casefold().startswith(prefix):
            value = line[len(prefix):].strip()

            if value:
                return compact_text(value)

    return None


def parse_russian_date(
    value: str | None,
) -> str | None:
    if not value:
        return None

    normalized = compact_text(
        value
    ).casefold()

    numeric_match = re.search(
        r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})\b",
        normalized,
    )

    if numeric_match:
        day, month, year = map(
            int,
            numeric_match.groups(),
        )

        try:
            return date(
                year,
                month,
                day,
            ).isoformat()
        except ValueError:
            return None

    word_match = re.search(
        (
            r"\b(\d{1,2})\s+"
            r"([а-яё]+)\s+"
            r"(\d{4})\b"
        ),
        normalized,
    )

    if not word_match:
        return None

    day = int(
        word_match.group(1)
    )

    month = RUSSIAN_MONTHS.get(
        word_match.group(2)
    )

    year = int(
        word_match.group(3)
    )

    if month is None:
        return None

    try:
        parsed = date(
            year,
            month,
            day,
        )
    except ValueError:
        return None

    return parsed.isoformat()


def extract_legal_title_metadata(
    value: str | None,
) -> LegalMetadata:
    """
    Извлекает реквизиты только из короткой шапки правового акта.

    В отличие от общего поиска по PDF, функция не рассматривает весь текст:
    вид документа должен встретиться в первых 500 символах, а номер и дата —
    рядом с ним. Это не позволяет принять упомянутый внутри документа чужой
    акт за реквизиты самого файла.
    """

    normalized = compact_text(
        value or ""
    )
    kind_match = LEGAL_TITLE_KIND_RE.search(
        normalized[:500]
    )

    if kind_match is None:
        return LegalMetadata(
            document_number=None,
            document_date=None,
            document_kind=None,
            attachments=(),
        )

    header = normalized[
        kind_match.start():
        kind_match.start() + 320
    ]
    number_match = (
        LEGAL_TITLE_NUMBER_RE.search(
            header
        )
    )
    date_match = (
        LEGAL_TITLE_DATE_RE.search(
            header
        )
    )

    return LegalMetadata(
        document_number=(
            validate_document_number(
                number_match.group(1)
            )
            if number_match is not None
            else None
        ),
        document_date=(
            parse_russian_date(
                date_match.group(1)
            )
            if date_match is not None
            else None
        ),
        document_kind=(
            normalize_document_kind(
                kind_match.group(1)
            )
        ),
        attachments=(),
    )


def host_is_allowed(
    hostname: str,
) -> bool:
    lowered = hostname.casefold()

    for allowed_host in settings.allowed_hosts:
        if (
            lowered == allowed_host
            or lowered.endswith(
                "." + allowed_host
            )
        ):
            return True

    return False


def canonicalize_attachment_url(
    raw_url: str,
    *,
    final_url: str,
) -> str | None:
    joined = urljoin(
        final_url,
        raw_url.strip(),
    )

    try:
        parsed = urlsplit(joined)
    except ValueError:
        return None

    if parsed.scheme.casefold() not in {
        "http",
        "https",
    }:
        return None

    hostname = (
        parsed.hostname
        or ""
    ).casefold()

    if not host_is_allowed(hostname):
        return None

    if hostname == "www.sochi.ru":
        hostname = "sochi.ru"

    path = parsed.path or "/"

    return urlunsplit(
        (
            "http",
            hostname,
            path,
            parsed.query,
            "",
        )
    )


def extract_attachments(
    container: Tag,
    *,
    final_url: str,
) -> tuple[AttachmentLink, ...]:
    attachments_by_url: dict[
        str,
        AttachmentLink,
    ] = {}

    for anchor in container.select(
        "a[href]"
    ):
        href = str(
            anchor.get(
                "href",
                "",
            )
        ).strip()

        if not href:
            continue

        link_text = compact_text(
            anchor.get_text(
                " ",
                strip=True,
            )
        )

        lowered_text = (
            link_text.casefold()
        )

        try:
            path = urlsplit(
                urljoin(final_url, href)
            ).path.casefold()
        except ValueError:
            continue

        suffix = PurePosixPath(
            path
        ).suffix

        looks_like_pdf = (
            suffix == ".pdf"
            or ".pdf" in lowered_text
            or "скачать документ"
            in lowered_text
            or "скачать файл"
            in lowered_text
        )

        if not looks_like_pdf:
            continue

        normalized_url = (
            canonicalize_attachment_url(
                href,
                final_url=final_url,
            )
        )

        if normalized_url is None:
            continue

        title = (
            link_text
            or PurePosixPath(
                path
            ).name
            or "PDF-документ"
        )

        attachments_by_url[
            normalized_url
        ] = AttachmentLink(
            url=normalized_url,
            title=title,
        )

    return tuple(
        attachments_by_url.values()
    )


def extract_legal_metadata(
    soup: BeautifulSoup,
    container: Tag,
    *,
    text: str,
    final_url: str,
) -> LegalMetadata:
    document_number = (
        extract_labeled_value(
            text,
            "Номер документа",
        )
    )

    document_date_raw = (
        extract_labeled_value(
            text,
            "Дата документа",
        )
    )

    document_kind = (
        extract_labeled_value(
            text,
            "Тип документа",
        )
    )

    text_casefold = text.casefold()

    has_legal_labels = any(
        label in text_casefold
        for label in (
            "номер документа",
            "дата документа",
            "тип документа",
        )
    )

    is_detail_page = (
        "element_id=" in final_url.casefold()
        or has_legal_labels
    )

    document_number = (
        validate_document_number(
            document_number
        )
    )

    document_kind = (
        normalize_document_kind(
            document_kind
        )
    )

    if (
        not document_number
        and is_detail_page
    ):
        typed_number_match = re.search(
            (
                r"\b(?:"
                r"постановление|"
                r"распоряжение|"
                r"решение|"
                r"приказ|"
                r"положение|"
                r"указ"
                r")\b"
                r".{0,180}?"
                rf"{DOCUMENT_NUMBER_MARKER_RE}\s*"
                r"([0-9А-Яа-яЁё"
                r"\-/–—]+)"
            ),
            text[:2500],
            flags=(
                re.IGNORECASE
                | re.DOTALL
            ),
        )

        if typed_number_match:
            document_number = (
                validate_document_number(
                    typed_number_match.group(1)
                )
            )

    if (
        not document_number
        and has_legal_labels
    ):
        simple_number_match = re.search(
            (
                rf"{DOCUMENT_NUMBER_MARKER_RE}\s*"
                r"([0-9А-Яа-яЁё"
                r"\-/–—]+)"
            ),
            text[:1500],
            flags=re.IGNORECASE,
        )

        if simple_number_match:
            document_number = (
                validate_document_number(
                    simple_number_match.group(1)
                )
            )

    if (
        not document_kind
        and is_detail_page
    ):
        document_kind = (
            normalize_document_kind(
                text[:2500]
            )
        )

    return LegalMetadata(
        document_number=(
            document_number
            or None
        ),
        document_date=parse_russian_date(
            document_date_raw
        ),
        document_kind=(
            document_kind
            or None
        ),
        attachments=extract_attachments(
            container,
            final_url=final_url,
        ),
    )
