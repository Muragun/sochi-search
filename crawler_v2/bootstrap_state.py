from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from .config import settings
from .state_db import CrawlStateDatabase


SOURCE_FIELDS = [
    "url",
    "page_url",
    "file_url",
    "source",
    "doc_type",
    "content_type",
    "hash",
    "fetched_at",
    "updated_at",
]


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def normalize_optional_string(
    value: Any,
) -> str | None:
    if not isinstance(value, str):
        return None

    normalized = value.strip()

    return normalized or None


def choose_primary_url(
    source: dict[str, Any],
) -> str | None:
    """
    Выбирает главный URL записи.

    Сначала используем url, затем file_url,
    затем page_url.
    """
    for field_name in (
        "url",
        "file_url",
        "page_url",
    ):
        value = normalize_optional_string(
            source.get(field_name)
        )

        if value:
            return value

    return None


class ElasticsearchBootstrapClient:
    def __init__(self) -> None:
        self.session = requests.Session()

        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": (
                    "sochi-search-state-bootstrap/1.0"
                ),
            }
        )

        if settings.es_username:
            self.session.auth = (
                settings.es_username,
                settings.es_password,
            )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: tuple[int, int] = (10, 120),
    ) -> dict[str, Any]:
        url = (
            f"{settings.es_url}/"
            f"{path.lstrip('/')}"
        )

        response = self.session.request(
            method=method,
            url=url,
            json=json_body,
            timeout=timeout,
            verify=settings.es_verify_tls,
        )

        if not response.ok:
            raise RuntimeError(
                "Elasticsearch вернул "
                f"HTTP {response.status_code}: "
                f"{response.text[:2000]}"
            )

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                "Elasticsearch вернул не JSON: "
                f"{response.text[:1000]}"
            ) from exc

    def start_scroll(self) -> dict[str, Any]:
        encoded_index = quote(
            settings.es_index,
            safe="-_*.,",
        )

        return self.request(
            "POST",
            (
                f"{encoded_index}/_search"
                f"?scroll={settings.scroll_ttl}"
            ),
            json_body={
                "size": settings.batch_size,
                "sort": [
                    "_doc"
                ],
                "_source": SOURCE_FIELDS,
                "query": {
                    "match_all": {}
                },
            },
        )

    def continue_scroll(
        self,
        scroll_id: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "_search/scroll",
            json_body={
                "scroll": settings.scroll_ttl,
                "scroll_id": scroll_id,
            },
        )

    def clear_scroll(
        self,
        scroll_id: str,
    ) -> None:
        try:
            self.request(
                "DELETE",
                "_search/scroll",
                json_body={
                    "scroll_id": [
                        scroll_id
                    ]
                },
                timeout=(5, 30),
            )
        except Exception as exc:
            print(
                "[WARN] Не удалось удалить "
                f"scroll-контекст: {exc}",
                file=sys.stderr,
            )


def convert_hit_to_state(
    hit: dict[str, Any],
    *,
    observed_at: str,
) -> dict[str, Any] | None:
    source = hit.get("_source")

    if not isinstance(source, dict):
        return None

    primary_url = choose_primary_url(source)

    if not primary_url:
        return None

    fetched_at = normalize_optional_string(
        source.get("fetched_at")
    )

    updated_at = normalize_optional_string(
        source.get("updated_at")
    )

    return {
        "url": primary_url,

        "page_url": normalize_optional_string(
            source.get("page_url")
        ),

        "file_url": normalize_optional_string(
            source.get("file_url")
        ),

        "source": normalize_optional_string(
            source.get("source")
        ),

        "doc_type": normalize_optional_string(
            source.get("doc_type")
        ),

        "content_type": normalize_optional_string(
            source.get("content_type")
        ),

        "content_hash": normalize_optional_string(
            source.get("hash")
        ),

        "status": "indexed_existing",
        "http_status": None,

        "etag": None,
        "last_modified": None,

        "attempt_count": 0,
        "failure_count": 0,
        "last_error": None,

        "first_seen_at": (
            fetched_at
            or updated_at
            or observed_at
        ),

        "last_seen_at": observed_at,

        "last_fetched_at": fetched_at,

        "last_indexed_at": (
            updated_at
            or fetched_at
        ),

        "source_updated_at": updated_at,

        "is_active": 1,
    }


def get_hits(
    response: dict[str, Any],
) -> list[dict[str, Any]]:
    hits_section = response.get("hits")

    if not isinstance(hits_section, dict):
        return []

    hits = hits_section.get("hits")

    if not isinstance(hits, list):
        return []

    return [
        hit
        for hit in hits
        if isinstance(hit, dict)
    ]


def main() -> int:
    database = CrawlStateDatabase(
        settings.state_db_path
    )

    database.initialize()

    started_at = utc_now()

    run_id = database.create_run(
        run_type="bootstrap_from_elasticsearch",
        started_at=started_at,
    )

    count_before = database.count_urls()

    client = ElasticsearchBootstrapClient()

    documents_seen = 0
    urls_seen = 0
    errors = 0
    scroll_id: str | None = None

    try:
        response = client.start_scroll()

        while True:
            response_scroll_id = response.get(
                "_scroll_id"
            )

            if isinstance(
                response_scroll_id,
                str,
            ):
                scroll_id = response_scroll_id

            hits = get_hits(response)

            if not hits:
                break

            observed_at = utc_now()

            rows_by_url: dict[
                str,
                dict[str, Any],
            ] = {}

            for hit in hits:
                documents_seen += 1

                try:
                    row = convert_hit_to_state(
                        hit,
                        observed_at=observed_at,
                    )
                except Exception as exc:
                    errors += 1

                    print(
                        "[WARN] Не удалось "
                        f"обработать документ: {exc}",
                        file=sys.stderr,
                    )

                    continue

                if row is None:
                    errors += 1
                    continue

                rows_by_url[row["url"]] = row

            database.upsert_urls(
                rows_by_url.values()
            )

            urls_seen += len(rows_by_url)

            print(
                "[PROGRESS] "
                f"documents={documents_seen} "
                f"batch_unique_urls={len(rows_by_url)} "
                f"errors={errors}",
                flush=True,
            )

            if not scroll_id:
                raise RuntimeError(
                    "Elasticsearch не вернул _scroll_id"
                )

            response = client.continue_scroll(
                scroll_id
            )

        count_after = database.count_urls()
        inserted_urls = max(
            count_after - count_before,
            0,
        )

        finished_at = utc_now()

        database.finish_run(
            run_id,
            finished_at=finished_at,
            status="success",
            documents_seen=documents_seen,
            urls_seen=count_after,
            inserted_urls=inserted_urls,
            errors=errors,
            message=(
                "Состояние импортировано "
                "из Elasticsearch"
            ),
        )

        print()
        print("[OK] Импорт состояния завершён")
        print(
            f"Документов Elasticsearch: "
            f"{documents_seen}"
        )
        print(
            f"Уникальных URL в SQLite: "
            f"{count_after}"
        )
        print(
            f"Новых URL добавлено: "
            f"{inserted_urls}"
        )
        print(
            f"Пропущено/ошибок: "
            f"{errors}"
        )
        print(
            f"Файл базы: "
            f"{settings.state_db_path}"
        )

        return 0

    except Exception as exc:
        finished_at = utc_now()

        database.finish_run(
            run_id,
            finished_at=finished_at,
            status="failed",
            documents_seen=documents_seen,
            urls_seen=database.count_urls(),
            inserted_urls=max(
                database.count_urls()
                - count_before,
                0,
            ),
            errors=errors + 1,
            message=str(exc),
        )

        print(
            f"[ERROR] Импорт не завершён: {exc}",
            file=sys.stderr,
        )

        return 1

    finally:
        if scroll_id:
            client.clear_scroll(scroll_id)


if __name__ == "__main__":
    raise SystemExit(main())
