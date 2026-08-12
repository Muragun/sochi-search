from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

import requests

from .config import settings


class ElasticsearchWriteError(
    RuntimeError
):
    pass


class ElasticsearchWriter:
    def __init__(self) -> None:
        self.session = requests.Session()

        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": (
                    "sochi-search-worker/2.0"
                ),
            }
        )

        if settings.es_username:
            self.session.auth = (
                settings.es_username,
                settings.es_password,
            )

    @property
    def encoded_index(self) -> str:
        return quote(
            settings.es_index,
            safe="-_*.,",
        )

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        response = self.session.request(
            method=method,
            url=(
                f"{settings.es_url}/"
                f"{path.lstrip('/')}"
            ),
            json=body,
            timeout=(10, 120),
            verify=settings.es_verify_tls,
        )

        if not response.ok:
            raise ElasticsearchWriteError(
                "Elasticsearch вернул "
                f"HTTP {response.status_code}: "
                f"{response.text[:2000]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ElasticsearchWriteError(
                "Elasticsearch вернул не JSON"
            ) from exc

    @staticmethod
    def document_id(
        url: str,
        page_number: int,
        chunk_number: int,
    ) -> str:
        raw_value = (
            f"{url}\0"
            f"{page_number}\0"
            f"{chunk_number}"
        )

        return hashlib.sha256(
            raw_value.encode(
                "utf-8"
            )
        ).hexdigest()

    def bulk_index(
        self,
        documents: list[dict[str, Any]],
    ) -> None:
        if not documents:
            raise ElasticsearchWriteError(
                "Список документов пуст"
            )

        lines: list[str] = []

        for document in documents:
            document_id = self.document_id(
                str(document["url"]),
                int(document["page_number"]),
                int(document["chunk_number"]),
            )

            lines.append(
                json.dumps(
                    {
                        "index": {
                            "_id": document_id,
                        }
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

            lines.append(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )

        payload = (
            "\n".join(lines)
            + "\n"
        ).encode("utf-8")

        # `refresh=wait_for` вместе с `index.refresh_interval: -1` в прежнем
        # mapping означал, что каждый bulk ждал обновления, которое само по
        # себе никогда не наступало: запрос висел до аварийного предела
        # `index.max_refresh_listeners` либо до таймаута сокета. Индекс
        # `sochi_docs_v3` обновляется раз в 5 секунд, поэтому достаточно
        # `refresh=false`.
        response = self.session.post(
            (
                f"{settings.es_url}/"
                f"{self.encoded_index}/_bulk"
                "?require_alias=true"
                "&refresh=false"
            ),
            data=payload,
            headers={
                "Content-Type": (
                    "application/x-ndjson"
                ),
            },
            timeout=(10, 180),
            verify=settings.es_verify_tls,
        )

        if not response.ok:
            raise ElasticsearchWriteError(
                "Bulk API вернул "
                f"HTTP {response.status_code}: "
                f"{response.text[:2000]}"
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise ElasticsearchWriteError(
                "Bulk API вернул не JSON"
            ) from exc

        if not result.get("errors"):
            return

        failures: list[str] = []

        for item in result.get(
            "items",
            [],
        ):
            operation = item.get(
                "index",
                {},
            )

            if int(
                operation.get(
                    "status",
                    500,
                )
            ) < 300:
                continue

            failures.append(
                json.dumps(
                    operation.get(
                        "error",
                        operation,
                    ),
                    ensure_ascii=False,
                )
            )

            if len(failures) >= 5:
                break

        raise ElasticsearchWriteError(
            "Ошибки Bulk API: "
            + " | ".join(failures)
        )

    def delete_stale_chunks(
        self,
        *,
        url: str,
        current_hash: str,
    ) -> int:
        result = self._json_request(
            "POST",
            (
                f"{self.encoded_index}/"
                "_delete_by_query"
                "?conflicts=proceed"
                "&refresh=false"
            ),
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {
                                "term": {
                                    "url": url,
                                }
                            }
                        ],
                        "must_not": [
                            {
                                "term": {
                                    "hash": (
                                        current_hash
                                    ),
                                }
                            }
                        ],
                    }
                }
            },
        )

        failures = result.get(
            "failures",
            [],
        )

        if failures:
            raise ElasticsearchWriteError(
                "Ошибки удаления старых "
                f"фрагментов: {failures[:3]}"
            )

        return int(
            result.get(
                "deleted",
                0,
            )
        )

    def delete_all_for_url(
        self,
        *,
        url: str,
    ) -> int:
        result = self._json_request(
            "POST",
            (
                f"{self.encoded_index}/"
                "_delete_by_query"
                "?conflicts=proceed"
                "&refresh=false"
            ),
            body={
                "query": {
                    "term": {
                        "url": url,
                    }
                }
            },
        )

        failures = result.get(
            "failures",
            [],
        )

        if failures:
            raise ElasticsearchWriteError(
                "Ошибки удаления документа: "
                f"{failures[:3]}"
            )

        return int(
            result.get(
                "deleted",
                0,
            )
        )

    def count_for_url(
        self,
        *,
        url: str,
    ) -> int:
        result = self._json_request(
            "POST",
            (
                f"{self.encoded_index}/"
                "_count"
            ),
            body={
                "query": {
                    "term": {
                        "url": url,
                    }
                }
            },
        )

        return int(
            result.get(
                "count",
                0,
            )
        )
