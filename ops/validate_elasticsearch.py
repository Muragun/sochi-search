"""
Проверяет новый mapping и анализаторы на настоящем Elasticsearch.

Скрипт создаёт временный индекс, прогоняет через `_analyze` набор
утверждений, индексирует небольшой набор документов и выполняет поисковые
запросы. Рабочий индекс и alias не затрагиваются, временный индекс удаляется
в любом исходе.

Запуск:

    python -m ops.validate_elasticsearch \\
        --mapping elasticsearch/sochi_docs_v3.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import requests


class ValidationError(RuntimeError):
    pass


class Checker:
    def __init__(self, base_url: str, session: requests.Session) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  [OK]   {name}")
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"  [FAIL] {name} — {detail}")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        raw: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"

        headers = {"Accept": "application/json"}
        data = None

        if raw is not None:
            headers["Content-Type"] = "application/x-ndjson"
            data = raw.encode("utf-8")

        response = self.session.request(
            method,
            url,
            json=body if raw is None else None,
            data=data,
            headers=headers,
            timeout=(10, 120),
        )

        if not response.ok:
            raise ValidationError(
                f"{method} {path} → HTTP {response.status_code}: "
                f"{response.text[:800]}"
            )

        return response.json() if response.content else {}


def tokens(checker: Checker, index: str, analyzer: str, text: str) -> list[str]:
    result = checker.request(
        "POST",
        f"{index}/_analyze",
        body={"analyzer": analyzer, "text": text},
    )
    return [item["token"] for item in result.get("tokens", [])]


def field_tokens(checker: Checker, index: str, field: str, text: str) -> list[str]:
    result = checker.request(
        "POST",
        f"{index}/_analyze",
        body={"field": field, "text": text},
    )
    return [item["token"] for item in result.get("tokens", [])]


SAMPLE_DOCUMENTS = [
    {
        "url": "https://sochi.ru/upload/doc-512.pdf",
        "title": "Постановление № 512-р о предоставлении субсидий",
        "text": (
            "Администрация города Сочи постановляет предоставить субсидии "
            "субъектам малого и среднего предпринимательства. Ёлочные "
            "мероприятия проводятся в декабре."
        ),
        "document_number": "512-р",
        "document_kind": "постановление",
        "document_date": "2024-03-14",
        "sort_date": "2024-03-14",
        "content_category": "legal",
        "doc_type": "pdf",
        "is_attachment": True,
        "url_path": "/upload/doc-512.pdf",
        "page_number": 1,
        "chunk_number": 0,
        "hash": "a" * 64,
    },
    {
        "url": "https://sochi.ru/press-sluzhba/novosti/vesna.html",
        "title": "Новости города: весенние мероприятия",
        "text": (
            "В городе прошли весенние мероприятия. Елка была установлена "
            "на центральной площади."
        ),
        "content_category": "news",
        "doc_type": "page",
        "is_attachment": False,
        "url_path": "/press-sluzhba/novosti/vesna.html",
        "sort_date": "2024-04-01",
        "page_number": 1,
        "chunk_number": 0,
        "hash": "b" * 64,
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    # Значения по умолчанию берутся из окружения: внутри контейнера
    # Elasticsearch доступен по имени, а не по 127.0.0.1.
    parser.add_argument(
        "--es-url",
        default=os.getenv("ES_URL", "http://127.0.0.1:9200"),
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        required=True,
    )
    parser.add_argument("--username", default=os.getenv("ES_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("ES_PASSWORD", ""))
    parser.add_argument(
        "--keep-index",
        action="store_true",
        help="Не удалять временный индекс (для разбора после сбоя)",
    )
    arguments = parser.parse_args()

    session = requests.Session()

    if arguments.username:
        session.auth = (arguments.username, arguments.password)

    checker = Checker(arguments.es_url, session)

    mapping = json.loads(
        arguments.mapping.read_text(encoding="utf-8")
    )

    index = f"sochi_validate_{uuid.uuid4().hex[:10]}"

    print(f"Elasticsearch: {arguments.es_url}")
    print(f"Временный индекс: {index}\n")

    try:
        info = checker.request("GET", "/")
        version = info.get("version", {}).get("number", "?")
        print(f"Версия Elasticsearch: {version}\n")

        print("1. Создание индекса с новым mapping")
        checker.request("PUT", index, body=mapping)
        checker.check("индекс создан", True)

        print("\n2. Анализаторы")

        yo_a = tokens(checker, index, "russian_text", "ёлка")
        yo_b = tokens(checker, index, "russian_text", "елка")
        checker.check(
            "ё и е дают одинаковые токены",
            yo_a == yo_b,
            f"{yo_a} != {yo_b}",
        )

        stemmed = tokens(checker, index, "russian_text", "постановления")
        base = tokens(checker, index, "russian_text", "постановление")
        checker.check(
            "словоформы приводятся к одной основе",
            stemmed == base,
            f"{stemmed} != {base}",
        )

        exact_a = tokens(checker, index, "russian_exact", "постановления")
        exact_b = tokens(checker, index, "russian_exact", "постановление")
        checker.check(
            "russian_exact различает словоформы",
            exact_a != exact_b,
            f"{exact_a} == {exact_b}",
        )

        number_tokens = field_tokens(
            checker, index, "document_number.text", "512-р"
        )
        checker.check(
            "номер акта даёт слитный вариант 512р",
            "512р" in number_tokens,
            f"токены: {number_tokens}",
        )

        stop_removed = tokens(checker, index, "russian_text", "и в на о")
        checker.check(
            "стоп-слова удаляются",
            len(stop_removed) == 0,
            f"осталось: {stop_removed}",
        )

        print("\n3. Индексация документов")

        lines = []
        for document in SAMPLE_DOCUMENTS:
            lines.append(json.dumps({"index": {}}, ensure_ascii=False))
            lines.append(json.dumps(document, ensure_ascii=False))

        result = checker.request(
            "POST",
            f"{index}/_bulk?refresh=true",
            raw="\n".join(lines) + "\n",
        )
        checker.check(
            "документы записаны без ошибок mapping",
            not result.get("errors"),
            json.dumps(result, ensure_ascii=False)[:600],
        )

        print("\n4. Отклонение неизвестного поля (dynamic: strict)")
        rejected = checker.request(
            "POST",
            f"{index}/_bulk?refresh=true",
            raw=(
                json.dumps({"index": {}}) + "\n"
                + json.dumps(
                    {"url": "https://sochi.ru/x", "unknown_field": 1},
                    ensure_ascii=False,
                ) + "\n"
            ),
        )
        checker.check(
            "неизвестное поле отклонено",
            bool(rejected.get("errors")),
            "поле принято — mapping не strict",
        )

        print("\n5. Поисковые запросы")

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app_v2.search_query import build_search_body

        def search(body: dict[str, Any]) -> dict[str, Any]:
            return checker.request("POST", f"{index}/_search", body=body)

        response = search(
            build_search_body("ёлка", limit=10, offset=0)
        )
        checker.check(
            "запрос «ёлка» находит документ со словом «елка»",
            response["hits"]["total"]["value"] >= 1,
            json.dumps(response["hits"]["total"], ensure_ascii=False),
        )

        response = search(
            build_search_body("512-р", limit=10, offset=0)
        )
        hits = response["hits"]["hits"]
        checker.check(
            "поиск по номеру акта находит нужный документ",
            bool(hits) and "512" in hits[0]["_source"].get("url", ""),
            f"попаданий: {len(hits)}",
        )

        response = search(
            build_search_body(
                "субсидии", limit=10, offset=0, content_category="legal"
            )
        )
        checker.check(
            "фильтр рубрики работает по content_category",
            response["hits"]["total"]["value"] >= 1,
            json.dumps(response["hits"]["total"], ensure_ascii=False),
        )

        response = search(
            build_search_body(
                "мероприятия", limit=10, offset=0, content_category="news"
            )
        )
        checker.check(
            "рубрика news не смешивается с legal",
            all(
                hit["_source"].get("content_category") == "news"
                for hit in response["hits"]["hits"]
            ),
            "в выдаче есть чужая рубрика",
        )

        response = search(
            build_search_body("субсидии", limit=10, offset=0)
        )
        checker.check(
            "подсветка возвращается по полю text",
            any(
                "highlight" in hit
                for hit in response["hits"]["hits"]
            ),
            "highlight отсутствует",
        )
        checker.check(
            "агрегация рубрик заполнена",
            "category_facets" in response.get("aggregations", {}),
            "нет агрегации",
        )

        response = search(
            build_search_body(
                "", limit=10, offset=0, date_from="2024-03-01",
                date_to="2024-03-31",
            )
        )
        checker.check(
            "фильтр по дате отбирает только март",
            response["hits"]["total"]["value"] == 1,
            json.dumps(response["hits"]["total"], ensure_ascii=False),
        )

        print("\n6. Настройки индекса")
        settings = checker.request("GET", f"{index}/_settings")
        index_settings = list(settings.values())[0]["settings"]["index"]

        checker.check(
            "refresh_interval не отключён",
            index_settings.get("refresh_interval") != "-1",
            f"refresh_interval={index_settings.get('refresh_interval')}",
        )
        checker.check(
            "включено сжатие best_compression",
            index_settings.get("codec") == "best_compression",
            f"codec={index_settings.get('codec')}",
        )

    finally:
        if not arguments.keep_index:
            try:
                checker.request("DELETE", index)
                print(f"\nВременный индекс {index} удалён")
            except Exception as exc:
                print(f"\nНе удалось удалить {index}: {exc}", file=sys.stderr)

    print(
        f"\nИтог: успешно {checker.passed}, "
        f"с ошибкой {len(checker.failed)}"
    )

    if checker.failed:
        print("\nНеуспешные проверки:")
        for item in checker.failed:
            print(f"  - {item}")
        return 1

    print("ELASTICSEARCH_VALIDATION=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
