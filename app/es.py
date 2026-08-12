from __future__ import annotations

from elasticsearch import Elasticsearch

from .settings import ELASTIC_URL


def get_es() -> Elasticsearch:
    return Elasticsearch(
        ELASTIC_URL,
        request_timeout=60,
        retry_on_timeout=True,
        max_retries=3,
    )


def ping_es() -> bool:
    try:
        return bool(get_es().ping())
    except Exception:
        return False
