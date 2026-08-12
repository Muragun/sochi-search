from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import settings


class ElasticsearchRequestError(RuntimeError):
    """Ошибка обращения API к Elasticsearch."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ElasticsearchHttpClient:
    def __init__(self) -> None:
        self.base_url = settings.es_url
        self.index_name = settings.es_index

        self.session = requests.Session()

        # Одиночный сетевой сбой или короткая недоступность узла не должны
        # превращаться в ошибку пользователю. Повторяем только безопасные
        # коды и только дважды, чтобы не растягивать ответ.
        retry = Retry(
            total=2,
            connect=2,
            read=1,
            backoff_factor=0.2,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=8,
            pool_maxsize=16,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "sochi-search-api-v2/1.0",
            }
        )

        if settings.es_username:
            self.session.auth = (
                settings.es_username,
                settings.es_password,
            )

    @property
    def encoded_index_name(self) -> str:
        return quote(
            self.index_name,
            safe="-_*.,",
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"

        try:
            response = self.session.request(
                method=method,
                url=url,
                json=json_body,
                timeout=(5, 30),
                verify=settings.es_verify_tls,
            )
        except requests.RequestException as exc:
            raise ElasticsearchRequestError(
                f"Не удалось подключиться к Elasticsearch: {exc}"
            ) from exc

        if not response.ok:
            body = response.text[:2000]

            raise ElasticsearchRequestError(
                (
                    "Elasticsearch вернул ошибку "
                    f"HTTP {response.status_code}"
                ),
                status_code=response.status_code,
                response_body=body,
            )

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise ElasticsearchRequestError(
                "Elasticsearch вернул ответ не в формате JSON",
                status_code=response.status_code,
                response_body=response.text[:2000],
            ) from exc

    def info(self) -> dict[str, Any]:
        return self.request(
            "GET",
            "/",
        )

    def cluster_health(self) -> dict[str, Any]:
        return self.request(
            "GET",
            "/_cluster/health",
        )

    def index_mapping(self) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/{self.encoded_index_name}/_mapping",
        )

    def count(self) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/{self.encoded_index_name}/_count",
        )

    def search(
        self,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/{self.encoded_index_name}/_search",
            json_body=body,
        )


es_client = ElasticsearchHttpClient()
