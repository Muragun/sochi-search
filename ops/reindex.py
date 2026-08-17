"""
Перенос индекса на новую схему без повторного обхода сайта.

Текст, уже извлечённый из HTML и PDF, лежит в Elasticsearch. Менять нужно
разбор этого текста — анализаторы, поля, подсветку, — а не сам текст, поэтому
`_reindex` внутри кластера дешевле повторного скачивания десятков тысяч
страниц. Качество OCR у старых PDF доводится отдельно, фоновым backfill.

Схема задаётся ключом `--mapping`, поэтому модуль не привязан к её номеру.
Раньше он назывался `reindex_v3` и при этом создавал уже v4: имя с номером
устаревает молча, ровно как устарел тест, сверявшийся со строкой
«pdf-ocr-v5».

Порядок:

    1. --create   создать новый индекс
    2. --reindex  перенести документы (асинхронно, с отслеживанием задачи)
    3. --verify   сравнить число документов и прогнать контрольные запросы
    4. --switch   перевести alias
    5. --rollback вернуть alias на прежний индекс

Каждый шаг отдельный и обратимый. Alias переключается одной атомарной
операцией, поэтому поиск не видит промежуточного состояния.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests

# Вычисление рубрики и пути повторяет логику crawler, но выполняется внутри
# кластера: так старые документы получают `content_category` и `url_path`,
# из-за отсутствия которых прежний поиск был вынужден перебирать `prefix`
# по полю `url`.
REINDEX_SCRIPT = """
if (ctx._source.containsKey('document_type')) {
    ctx._source.remove('document_type');
}
for (String dead : ['content', 'body', 'headings', 'description']) {
    if (ctx._source.containsKey(dead)) {
        ctx._source.remove(dead);
    }
}

String url = ctx._source.url;
if (url == null) {
    ctx.op = 'noop';
} else {
    int schemeEnd = url.indexOf('://');
    String rest = schemeEnd < 0 ? url : url.substring(schemeEnd + 3);
    int slash = rest.indexOf('/');
    String path = slash < 0 ? '/' : rest.substring(slash);
    ctx._source.url_path = path;

    if (ctx._source.content_category == null) {
        String category = 'other';
        if (path.startsWith('/press-sluzhba/novosti/')) {
            category = 'news';
        } else if (path.startsWith('/gorodskaya-vlast/normativno-pravovyye-akty/')) {
            category = 'legal';
        } else if (path.startsWith('/press-sluzhba/obyavleniya/')) {
            category = 'announcement';
        } else if (path.startsWith('/press-sluzhba/mediagalereya')
                   || path.startsWith('/press-sluzhba/mediagalareya')) {
            category = 'media';
        } else if (path.startsWith('/upload/')) {
            category = 'file';
        } else if (path.startsWith('/press-sluzhba/')) {
            category = 'press';
        } else if (path.startsWith('/gorodskaya-vlast/')) {
            category = 'city_authority';
        } else if (path.startsWith('/zhizn-goroda/')) {
            category = 'city_life';
        } else if (path.startsWith('/gorod/')) {
            category = 'city';
        } else if (path.startsWith('/afisha-goroda/')
                   || path.startsWith('/kalendar-meropriyatiy/')) {
            category = 'events';
        } else if (path.startsWith('/content/')
                   || path.startsWith('/contents/')
                   || path.startsWith('/strucrure_sochi/')) {
            category = 'legacy';
        }
        ctx._source.content_category = category;
    }

    if (ctx._source.sort_date == null) {
        if (ctx._source.document_date != null) {
            ctx._source.sort_date = ctx._source.document_date;
        } else if (ctx._source.published_at != null) {
            ctx._source.sort_date = ctx._source.published_at;
        }
    }
}

"""

CONTROL_QUERIES = [
    ("поиск по слову с ё", {"query": {"match": {"text": "ёлка"}}}),
    (
        "поиск по номеру акта",
        {"query": {"term": {"document_number_normalized": "512-р"}}},
    ),
    ("фильтр рубрики", {"query": {"term": {"content_category": "legal"}}}),
    ("вложения", {"query": {"term": {"is_attachment": True}}}),
]


class Client:
    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

        if username:
            self.session.auth = (username, password)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        timeout: tuple[int, int] = (10, 300),
    ) -> dict[str, Any]:
        response = self.session.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            json=body,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )

        if not response.ok:
            raise RuntimeError(
                f"{method} {path} → HTTP {response.status_code}: "
                f"{response.text[:800]}"
            )

        return response.json() if response.content else {}

    def count(self, index: str) -> int:
        return int(self.request("GET", f"{index}/_count")["count"])

    def segment_documents(self, index: str) -> int:
        """
        Документы в сегментах, а не в поиске.

        `_count` идёт через поиск и на время заливки показывает ноль:
        `create_index` отключает обновление индекса, поэтому свежие
        документы поиску ещё не видны. Статистика сегментов отстаёт на
        время до сброса буфера, но растёт по-настоящему.
        """

        result = self.request("GET", f"{index}/_stats/docs")
        indices = result.get("indices") or {}

        for entry in indices.values():
            primaries = (entry or {}).get("primaries") or {}
            documents = primaries.get("docs") or {}

            return int(documents.get("count", 0) or 0)

        return 0

    def alias_targets(self, alias: str) -> list[str]:
        try:
            result = self.request("GET", f"_alias/{alias}")
        except RuntimeError:
            return []

        return sorted(result)


def create_index(
    client: Client,
    *,
    index: str,
    mapping_path: Path,
) -> None:
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))

    # На время массовой заливки отключаем обновление и увеличиваем интервал
    # слияний: так перенос идёт заметно быстрее. Рабочие значения ставятся
    # обратно в `finalize`.
    mapping.setdefault("settings", {}).setdefault("index", {})
    mapping["settings"]["index"]["refresh_interval"] = "-1"

    client.request("PUT", index, body=mapping)
    print(f"CREATED index={index}")


def start_reindex(
    client: Client,
    *,
    source: str,
    destination: str,
    slices: int,
) -> str:
    result = client.request(
        "POST",
        f"_reindex?wait_for_completion=false&slices={slices}&requests_per_second=-1",
        body={
            "conflicts": "proceed",
            "source": {
                "index": source,
                "size": 500,
            },
            "dest": {
                "index": destination,
                "op_type": "index",
            },
            "script": {
                "lang": "painless",
                "source": REINDEX_SCRIPT,
            },
        },
    )

    task = str(result["task"])
    print(f"REINDEX_TASK={task}")
    return task


def task_progress(status: dict[str, Any]) -> tuple[int, int]:
    """
    Сколько документов перенесено и сколько всего.

    У нарезанного `_reindex` (`slices` больше единицы) счётчики лежат в
    подзадачах, а верхнеуровневые остаются нулями. Монитор читал только
    верхние и показывал «0/0» до самого конца — на рабочем кластере
    8.19.17 это выглядело как зависший перенос, хотя он шёл нормально.

    Записи подзадач бывают пустыми: срез, который ещё не начался,
    приходит как `null`.
    """

    slices = status.get("slices") or []

    if slices:
        total = 0
        done = 0

        for entry in slices:
            if not isinstance(entry, dict):
                continue

            total += int(entry.get("total", 0) or 0)
            done += (
                int(entry.get("created", 0) or 0)
                + int(entry.get("updated", 0) or 0)
            )

        return done, total

    return (
        int(status.get("created", 0) or 0)
        + int(status.get("updated", 0) or 0),
        int(status.get("total", 0) or 0),
    )


def wait_for_task(
    client: Client,
    task: str,
    *,
    poll_seconds: int,
    destination: str = "",
) -> None:
    while True:
        result = client.request("GET", f"_tasks/{task}")

        if result.get("completed"):
            response = result.get("response", {})
            failures = response.get("failures", [])

            print(
                "REINDEX_DONE "
                f"created={response.get('created', 0)} "
                f"updated={response.get('updated', 0)} "
                f"batches={response.get('batches', 0)} "
                f"failures={len(failures)}"
            )

            if failures:
                for failure in failures[:5]:
                    print(
                        "  FAILURE: "
                        + json.dumps(failure, ensure_ascii=False)[:400]
                    )
                raise RuntimeError(
                    f"Перенос завершился с {len(failures)} ошибками"
                )

            return

        status = result.get("task", {}).get("status", {})
        done, total = task_progress(status)
        percent = (done / total * 100) if total else 0.0

        # Независимая мера того, что перенос идёт: счётчики задачи можно
        # прочитать неверно, как и вышло со срезами.
        #
        # Берётся из статистики сегментов, а не через `_count`: на время
        # заливки `create_index` ставит `refresh_interval: -1`, поэтому
        # поиск новых документов не видит и `_count` возвращал бы ноль
        # при любом реальном прогрессе. Статистика сегментов обновляется
        # по мере сброса буфера и отстаёт, но не обманывает.
        indexed = ""

        if destination:
            try:
                indexed = (
                    f", в сегментах {client.segment_documents(destination)}"
                )
            except RuntimeError:
                indexed = ""

        print(
            f"  прогресс: {done}/{total} ({percent:.1f}%){indexed}",
            flush=True,
        )
        time.sleep(poll_seconds)


def finalize(client: Client, *, index: str) -> None:
    client.request(
        "PUT",
        f"{index}/_settings",
        body={"index": {"refresh_interval": "5s"}},
    )
    client.request("POST", f"{index}/_refresh")

    # Один принудительный merge после массовой заливки: дальше сегменты
    # сливаются штатно. На большом индексе операция долгая, поэтому
    # выполняется один раз и до переключения alias.
    client.request(
        "POST",
        f"{index}/_forcemerge?max_num_segments=1",
        timeout=(10, 3600),
    )
    print(f"FINALIZED index={index}")


def verify(
    client: Client,
    *,
    source: str,
    destination: str,
) -> bool:
    source_count = client.count(source)
    destination_count = client.count(destination)

    print(f"VERIFY source={source_count} destination={destination_count}")

    ok = destination_count >= source_count * 0.999

    if not ok:
        print(
            "  [FAIL] в новом индексе меньше документов, "
            "чем в исходном"
        )

    for name, body in CONTROL_QUERIES:
        result = client.request(
            "POST",
            f"{destination}/_search",
            body={**body, "size": 0, "track_total_hits": True},
        )
        hits = result["hits"]["total"]["value"]
        print(f"  {name}: {hits}")

    empty_category = client.request(
        "POST",
        f"{destination}/_search",
        body={
            "size": 0,
            "track_total_hits": True,
            "query": {
                "bool": {
                    "must_not": [
                        {"exists": {"field": "content_category"}}
                    ]
                }
            },
        },
    )["hits"]["total"]["value"]

    print(f"  без рубрики: {empty_category}")

    if empty_category:
        print("  [FAIL] остались документы без content_category")
        ok = False

    print("VERIFY_OK" if ok else "VERIFY_FAILED")
    return ok


def switch_alias(
    client: Client,
    *,
    alias: str,
    destination: str,
) -> None:
    previous = client.alias_targets(alias)

    actions: list[dict[str, Any]] = [
        {"remove": {"index": name, "alias": alias}}
        for name in previous
    ]
    actions.append({"add": {"index": destination, "alias": alias}})

    client.request("POST", "_aliases", body={"actions": actions})

    print(
        f"ALIAS_SWITCHED alias={alias} "
        f"from={','.join(previous) or '(нет)'} to={destination}"
    )
    print(
        "Откат: python -m ops.reindex_v3 --rollback "
        f"--alias {alias} --previous {','.join(previous) or '<индекс>'}"
    )


def rollback_alias(
    client: Client,
    *,
    alias: str,
    previous: str,
) -> None:
    current = client.alias_targets(alias)

    actions: list[dict[str, Any]] = [
        {"remove": {"index": name, "alias": alias}}
        for name in current
    ]

    for name in previous.split(","):
        if name.strip():
            actions.append(
                {"add": {"index": name.strip(), "alias": alias}}
            )

    client.request("POST", "_aliases", body={"actions": actions})
    print(f"ALIAS_ROLLED_BACK alias={alias} to={previous}")


def main() -> int:
    parser = argparse.ArgumentParser()

    # Значения по умолчанию берутся из окружения. Внутри контейнера
    # Elasticsearch доступен по имени elasticsearch, а не по 127.0.0.1,
    # и без этого скрипт стучался бы сам в себя.
    parser.add_argument(
        "--es-url",
        default=os.getenv("ES_URL", "http://127.0.0.1:9200"),
    )
    parser.add_argument(
        "--username", default=os.getenv("ES_USERNAME", "")
    )
    parser.add_argument(
        "--password", default=os.getenv("ES_PASSWORD", "")
    )
    parser.add_argument(
        "--alias", default=os.getenv("ES_INDEX", "sochi_search")
    )
    parser.add_argument("--source", default="")
    parser.add_argument(
        "--destination",
        default=f"sochi_docs_v4_{date.today():%Y%m%d}",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path("elasticsearch/sochi_docs_v4.json"),
    )
    parser.add_argument("--slices", type=int, default=2)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--previous", default="")

    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "Создать пустой индекс и направить на него alias. "
            "Нужно при развёртывании с нуля, когда переносить нечего"
        ),
    )
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--reindex", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--switch", action="store_true")
    parser.add_argument("--rollback", action="store_true")

    arguments = parser.parse_args()

    client = Client(
        arguments.es_url,
        arguments.username,
        arguments.password,
    )

    if arguments.bootstrap:
        existing = client.alias_targets(arguments.alias)

        if existing:
            print(
                f"Alias {arguments.alias} уже указывает на "
                f"{', '.join(existing)} — создавать нечего."
            )
            return 0

        create_index(
            client,
            index=arguments.destination,
            mapping_path=arguments.mapping,
        )
        finalize(client, index=arguments.destination)
        client.request(
            "POST",
            "_aliases",
            body={
                "actions": [
                    {
                        "add": {
                            "index": arguments.destination,
                            "alias": arguments.alias,
                        }
                    }
                ]
            },
        )
        print(
            f"BOOTSTRAP_OK index={arguments.destination} "
            f"alias={arguments.alias}"
        )
        return 0

    if arguments.rollback:
        if not arguments.previous:
            print("Нужен --previous с именем прежнего индекса")
            return 2

        rollback_alias(
            client,
            alias=arguments.alias,
            previous=arguments.previous,
        )
        return 0

    source = arguments.source

    if not source:
        targets = client.alias_targets(arguments.alias)

        if len(targets) != 1:
            print(
                f"Alias {arguments.alias} указывает на {targets}. "
                "Укажите --source явно."
            )
            return 2

        source = targets[0]

    print(f"Источник: {source}")
    print(f"Назначение: {arguments.destination}\n")

    if arguments.create:
        create_index(
            client,
            index=arguments.destination,
            mapping_path=arguments.mapping,
        )

    if arguments.reindex:
        task = start_reindex(
            client,
            source=source,
            destination=arguments.destination,
            slices=arguments.slices,
        )
        wait_for_task(
            client,
            task,
            poll_seconds=arguments.poll_seconds,
            destination=arguments.destination,
        )
        finalize(client, index=arguments.destination)

    if arguments.verify:
        if not verify(
            client,
            source=source,
            destination=arguments.destination,
        ):
            return 1

    if arguments.switch:
        switch_alias(
            client,
            alias=arguments.alias,
            destination=arguments.destination,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
