from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import parse_qsl, unquote, urlsplit


LEGACY_DETAIL_PATHS = frozenset(
    {
        "/content/detail.php",
        "/contents/detail.php",
    }
)

ARCHIVE_SUFFIXES = frozenset(
    {
        ".7z",
        ".gz",
        ".rar",
        ".tar",
        ".tgz",
        ".zip",
    }
)

SUPPORTED_FILE_SUFFIXES = frozenset(
    {
        ".csv",
        ".docx",
        ".odt",
        ".ods",
        ".pdf",
        ".rtf",
        ".txt",
        ".xlsx",
    }
)

# .doc и .xls — двоичные форматы Word 97-2003, для них нужен внешний
# конвертер (antiword, catdoc, LibreOffice). На общем сервере это
# отдельное решение, поэтому они пока остаются неподдерживаемыми.
UNSUPPORTED_OFFICE_SUFFIXES = frozenset(
    {
        ".doc",
        ".xls",
    }
)


def normalized_url_suffix(url: str) -> str:
    """Return a lower-case path suffix without looking at query values."""
    try:
        path = unquote(urlsplit(str(url or "")).path)
    except (TypeError, ValueError):
        return ""

    return PurePosixPath(path.casefold()).suffix


def is_legacy_detail_url(url: str) -> bool:
    """Match only the two audited dead detail.php routes with an ID."""
    try:
        parsed = urlsplit(url)
        query_items = parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    except (TypeError, ValueError):
        return False

    normalized_path = (
        parsed.path.rstrip("/").casefold()
        or "/"
    )

    if normalized_path not in LEGACY_DETAIL_PATHS:
        return False

    return any(
        key.casefold() == "id"
        for key, _value in query_items
    )


def is_legacy_content_detail_url(url: str) -> bool:
    """Backward-compatible name retained for older crawler imports."""
    return is_legacy_detail_url(url)


def is_archive_url(url: str) -> bool:
    return normalized_url_suffix(url) in ARCHIVE_SUFFIXES


def is_unsupported_office_url(url: str) -> bool:
    return normalized_url_suffix(url) in UNSUPPORTED_OFFICE_SUFFIXES
