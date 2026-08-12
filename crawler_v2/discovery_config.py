from __future__ import annotations

import os
from dataclasses import dataclass

from .config import settings as crawler_settings


def env_int(
    name: str,
    default: int,
    *,
    minimum: int = 1,
) -> int:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        return default

    return max(value, minimum)


def env_list(
    name: str,
    default: str,
) -> tuple[str, ...]:
    raw_value = os.getenv(name, default)

    return tuple(
        item.strip()
        for item in raw_value.split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class DiscoverySettings:
    root_url: str
    default_sitemaps: tuple[str, ...]

    canonical_scheme: str
    canonical_host: str

    max_pagination_page: int
    max_sitemaps: int
    max_candidates: int
    max_new_urls: int


settings = DiscoverySettings(
    root_url=os.getenv(
        "DISCOVERY_ROOT_URL",
        "https://sochi.ru/",
    ),

    default_sitemaps=env_list(
        "DISCOVERY_DEFAULT_SITEMAPS",
        (
            "https://sochi.ru/sitemap.xml,"
            "https://sochi.ru/sitemap_index.xml"
        ),
    ),

    canonical_scheme=os.getenv(
        "DISCOVERY_CANONICAL_SCHEME",
        "http",
    ).strip().lower(),

    canonical_host=os.getenv(
        "DISCOVERY_CANONICAL_HOST",
        "sochi.ru",
    ).strip().lower(),

    # Архив новостей уходит на сотни страниц. Прежний потолок в десять
    # страниц отсекал всё, на что не было ссылки из sitemap.
    max_pagination_page=env_int(
        "DISCOVERY_MAX_PAGINATION_PAGE",
        200,
    ),

    max_sitemaps=env_int(
        "DISCOVERY_MAX_SITEMAPS",
        200,
    ),

    max_candidates=env_int(
        "DISCOVERY_MAX_CANDIDATES",
        100000,
    ),

    max_new_urls=env_int(
        "DISCOVERY_MAX_NEW_URLS",
        5000,
    ),
)


if settings.canonical_scheme not in {
    "http",
    "https",
}:
    raise RuntimeError(
        "DISCOVERY_CANONICAL_SCHEME должен "
        "быть http или https"
    )


USER_AGENT = crawler_settings.user_agent
ALLOWED_HOSTS = crawler_settings.allowed_hosts

CONNECT_TIMEOUT = (
    crawler_settings.connect_timeout
)

READ_TIMEOUT = (
    crawler_settings.read_timeout
)

MAX_DOWNLOAD_BYTES = (
    crawler_settings.max_download_bytes
)
