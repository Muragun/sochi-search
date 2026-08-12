from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)

from .config import settings


TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "yclid",
    "_openstat",
    "from",
    "ref",
    "referrer",
}

TRACKING_PREFIXES = (
    "utm_",
)

UNRESERVED_CHARACTERS = frozenset(
    string.ascii_letters
    + string.digits
    + "-._~"
)


@dataclass(frozen=True)
class UrlNormalizationResult:
    original: str
    normalized: str
    changes: tuple[str, ...]


def canonical_scheme() -> str:
    value = os.getenv(
        "DISCOVERY_CANONICAL_SCHEME",
        "http",
    ).strip().lower()

    if value not in {
        "http",
        "https",
    }:
        return "http"

    return value


def canonical_host() -> str:
    return os.getenv(
        "DISCOVERY_CANONICAL_HOST",
        "sochi.ru",
    ).strip().lower()


def allowed_hosts() -> tuple[str, ...]:
    values = []

    for host in settings.allowed_hosts:
        normalized = str(host).strip().lower()

        if normalized:
            values.append(normalized)

    return tuple(values)


def host_is_allowed(
    hostname: str,
) -> bool:
    lowered = hostname.lower().rstrip(".")

    for allowed_host in allowed_hosts():
        if (
            lowered == allowed_host
            or lowered.endswith(
                "." + allowed_host
            )
        ):
            return True

    return False


def normalize_percent_escapes(
    value: str,
) -> str:
    def replace(
        match: re.Match[str],
    ) -> str:
        hexadecimal = match.group(1)

        try:
            character = bytes.fromhex(
                hexadecimal
            ).decode("ascii")
        except (
            ValueError,
            UnicodeDecodeError,
        ):
            return "%" + hexadecimal.upper()

        if character in UNRESERVED_CHARACTERS:
            return character

        return "%" + hexadecimal.upper()

    return re.sub(
        r"%([0-9a-fA-F]{2})",
        replace,
        value,
    )


def is_tracking_parameter(
    name: str,
) -> bool:
    lowered = name.casefold()

    if lowered in TRACKING_PARAMETERS:
        return True

    return any(
        lowered.startswith(prefix)
        for prefix in TRACKING_PREFIXES
    )


def normalize_project_url(
    raw_url: str,
) -> UrlNormalizationResult | None:
    original = str(
        raw_url
        or ""
    ).strip()

    if not original:
        return None

    try:
        parsed = urlsplit(original)
    except ValueError:
        return None

    original_scheme = (
        parsed.scheme
        or ""
    ).lower()

    if original_scheme not in {
        "http",
        "https",
    }:
        return None

    hostname = (
        parsed.hostname
        or ""
    ).lower().rstrip(".")

    if not hostname:
        return None

    if not host_is_allowed(hostname):
        return None

    try:
        port = parsed.port
    except ValueError:
        return None

    changes: list[str] = []

    target_scheme = canonical_scheme()

    if original_scheme != target_scheme:
        changes.append("scheme")

    target_host = hostname
    main_host = canonical_host()

    if hostname in {
        main_host,
        "www." + main_host,
    }:
        target_host = main_host

    if hostname != target_host:
        changes.append("host_alias")

    include_port = (
        port is not None
        and port not in {
            80,
            443,
        }
    )

    if (
        port is not None
        and not include_port
    ):
        changes.append("default_port")

    netloc = target_host

    if include_port:
        netloc = f"{target_host}:{port}"

    original_path = (
        parsed.path
        or "/"
    )

    normalized_path = re.sub(
        r"/{2,}",
        "/",
        original_path,
    )

    normalized_path = (
        normalize_percent_escapes(
            normalized_path
        )
    )

    if normalized_path != original_path:
        changes.append("path")

    query_items = parse_qsl(
        parsed.query,
        keep_blank_values=True,
    )

    filtered_items = [
        (
            key,
            value,
        )
        for key, value in query_items
        if not is_tracking_parameter(key)
    ]

    if len(filtered_items) != len(
        query_items
    ):
        changes.append(
            "tracking_parameters"
        )

    sorted_items = sorted(
        filtered_items,
        key=lambda item: (
            item[0].casefold(),
            item[0],
            item[1],
        ),
    )

    normalized_query = urlencode(
        sorted_items,
        doseq=True,
    )

    if (
        normalized_query
        != parsed.query
        and "tracking_parameters"
        not in changes
    ):
        changes.append(
            "query_normalization"
        )

    if parsed.fragment:
        changes.append("fragment")

    normalized_url = urlunsplit(
        (
            target_scheme,
            netloc,
            normalized_path,
            normalized_query,
            "",
        )
    )

    return UrlNormalizationResult(
        original=original,
        normalized=normalized_url,
        changes=tuple(
            dict.fromkeys(changes)
        ),
    )
