from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def env_bool(name: str, default: bool) -> bool:
    """Читает логическую переменную окружения."""
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    return raw_value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_int(name: str, default: int) -> int:
    """Читает целое число из переменной окружения."""
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except ValueError:
        return default


def env_list(name: str, default: str) -> tuple[str, ...]:
    """Преобразует строку через запятую в кортеж."""
    raw_value = os.getenv(name, default)

    return tuple(
        value.strip()
        for value in raw_value.split(",")
        if value.strip()
    )


@dataclass(frozen=True)
class Settings:
    es_url: str
    es_index: str
    es_username: str
    es_password: str
    es_verify_tls: bool

    search_fields: tuple[str, ...]
    search_source_fields: tuple[str, ...]

    search_default_limit: int
    search_max_limit: int

    state_db_path: Path


settings = Settings(
    es_url=os.getenv(
        "ES_URL",
        "http://127.0.0.1:9200",
    ).rstrip("/"),

    es_index=os.getenv(
        "ES_INDEX",
        "sochi_search",
    ),

    es_username=os.getenv(
        "ES_USERNAME",
        "",
    ),

    es_password=os.getenv(
        "ES_PASSWORD",
        "",
    ),

    es_verify_tls=env_bool(
        "ES_VERIFY_TLS",
        True,
    ),

    search_fields=env_list(
        "SEARCH_FIELDS",
        "title^5,headings^3,text,content,body",
    ),

    search_source_fields=env_list(
        "SEARCH_SOURCE_FIELDS",
        (
            "url,title,text,content,body,headings,"
            "document_type,page_number,published_at"
        ),
    ),

    search_default_limit=env_int(
        "SEARCH_DEFAULT_LIMIT",
        10,
    ),

    search_max_limit=env_int(
        "SEARCH_MAX_LIMIT",
        50,
    ),

    state_db_path=Path(
        os.getenv(
            "CRAWL_STATE_DB",
            "/opt/sochi-search/data/crawl_state.sqlite3",
        )
    ),
)
