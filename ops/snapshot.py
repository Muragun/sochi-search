"""
Снимки индекса по расписанию.

Ежедневная резервная копия (`ops/backup.py`) забирает очередь SQLite и
код. Индекса в ней нет. Долгое время это выглядело допустимым: индекс
считается производным — очередь цела, значит документы можно разобрать
заново.

Считать так неверно, и вот почему. Заново — это не «перекачать», а
«перераспознать»: большая часть корпуса это сканы без текстового слоя,
и OCR полного корпуса занимает недели. Первый проход по двум страницам
каждого документа — 26 часов, остальное фоном. То есть потеря тома
Elasticsearch стоит не часов, а месяца работы двух слотов
распознавания.

Снимок для этого в проекте уже был: `path.repo: /snapshots` и том
`essnapshots` объявлены в compose, а `docs/DEPLOY.md` описывает, как
снять и восстановить. Не было только одного — чтобы это происходило
само. Процедура переезда выполняется раз в год, при переезде; резервная
копия нужна в тот день, когда её никто не собирался делать.

Снимки в Elasticsearch инкрементальные: в хранилище пишутся только
сегменты, которых там ещё нет. Поэтому ежедневный снимок стоит
примерно столько, сколько изменилось за сутки, а не размер индекса.

Почему не SLM. У Elasticsearch есть встроенное управление снимками по
расписанию, и в бесплатной лицензии оно доступно. Отказ сознательный:
политика SLM — это состояние внутри кластера, которое не видно из
репозитория, не проверяется тестами и теряется вместе с кластером,
ровно в том случае, ради которого всё и делается. Здесь та же работа
описана кодом, запускается той же ролью, что и остальные задачи, и
проверяется тем же набором тестов.

Запуск:

    python -m ops.snapshot
    python -m ops.snapshot --retention 14
    python -m ops.snapshot --list
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


DEFAULT_URL = "http://127.0.0.1:9200"
DEFAULT_REPOSITORY = "sochi"
DEFAULT_LOCATION = "/snapshots"
# Сколько своих снимков хранить по умолчанию.
#
# Три, а не семь, и причина в арифметике конкретной машины. Развёртывание
# требует 20 ГБ свободного места, индекс занимает около восьми, и
# хранилище снимков лежит на том же диске. Первый снимок стоит как весь
# индекс; следующие дописывают только новые сегменты — но пока идёт
# фоновое распознавание, документы переписываются, сегменты сливаются, и
# «только новые» получается заметно больше, чем на неподвижном индексе.
#
# Семь точек восстановления на таком диске — это тот же способ его
# заполнить, от которого только что закрыли журналы контейнеров. Три
# суток отката хватает, чтобы заметить беду, а поднять значение можно
# одной переменной — это вопрос про свободное место, и решать его надо,
# посмотрев на место.
DEFAULT_RETENTION = 3
DEFAULT_PREFIX = "auto"
DEFAULT_REQUEST_TIMEOUT = 900.0

USER_AGENT = "sochi-search-snapshot/2.6.0"

# Имя снимка: префикс, метка времени UTC и ничего больше. По нему
# видно, что снимок автоматический, и он сортируется как строка — то
# есть срок годности считается без разбора дат.
NAME_TIMESTAMP_FORMAT = "%Y%m%dt%H%M%Sz"

# Elasticsearch принимает в именах снимков только нижний регистр и
# ограниченный набор знаков.
SNAPSHOT_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class SnapshotError(RuntimeError):
    """Отказ, о котором нужно сообщить понятной строкой, а не трассировкой."""


@dataclass(frozen=True)
class SnapshotResult:
    repository: str
    name: str
    indices: Tuple[str, ...]
    state: str
    shards_total: int
    shards_failed: int
    duration_millis: int
    removed: Tuple[str, ...] = ()
    repository_created: bool = False
    failures: Tuple[str, ...] = field(default=())


def configured_url(explicit_url: Optional[str] = None) -> str:
    """Тот же разбор URL, что и у проверки готовности."""

    value = (
        explicit_url
        or os.getenv("ES_URL")
        or os.getenv("ELASTICSEARCH_URL")
        or DEFAULT_URL
    ).strip().rstrip("/")

    parsed = urlsplit(value)

    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Некорректный порт Elasticsearch") from error

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Некорректный URL Elasticsearch")

    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(
            "URL Elasticsearch должен указывать на корень сервиса"
        )

    return value


def snapshot_name(
    prefix: str,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Имя снимка, по которому видно и происхождение, и время."""

    moment = now or datetime.now(timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime(
        NAME_TIMESTAMP_FORMAT
    )
    name = "{0}-{1}".format(prefix.strip().lower(), stamp)

    if not SNAPSHOT_NAME_PATTERN.match(name):
        raise ValueError(
            "Недопустимое имя снимка: {0}. Elasticsearch принимает "
            "нижний регистр, цифры, дефис, точку и подчёркивание".format(
                name
            )
        )

    return name


def http_call(
    base_url: str,
    request_timeout: float,
) -> Callable[..., Any]:
    """Транспорт поверх urllib. Отделён, чтобы тесты обходились без сети."""

    def call(
        method: str,
        path: str,
        payload: Optional[dict] = None,
    ) -> Any:
        body = None
        headers = {"User-Agent": USER_AGENT}

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            base_url.rstrip("/") + path,
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urlopen(request, timeout=request_timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise SnapshotError(
                "Elasticsearch ответил {0} на {1} {2}: {3}".format(
                    error.code,
                    method,
                    path,
                    detail,
                )
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise SnapshotError(
                "Не удалось обратиться к Elasticsearch: {0}".format(error)
            ) from error

        if not raw:
            return {}

        try:
            return json.loads(raw)
        except ValueError as error:
            raise SnapshotError(
                "Elasticsearch вернул не JSON на {0} {1}".format(
                    method, path
                )
            ) from error

    return call


def ensure_repository(
    call: Callable[..., Any],
    *,
    repository: str,
    location: str,
) -> bool:
    """
    Заводит файловое хранилище снимков, если его ещё нет.

    Возвращает True, если хранилище создано этим вызовом.

    Проверка `location` не формальность: `path.repo` задаётся в compose,
    и при переносе на машину с другим путём регистрация молча указывала
    бы в никуда. Несовпадение — отказ, а не тихая перерегистрация:
    переписать чужое хранилище означало бы потерять ссылку на уже
    снятые снимки.
    """

    path = "/_snapshot/" + quote(repository, safe="")

    try:
        existing = call("GET", path)
    except SnapshotError:
        existing = None

    if existing:
        settings = (
            existing.get(repository, {}).get("settings", {})
            if isinstance(existing, dict)
            else {}
        )
        current = str(settings.get("location") or "")

        if current and current != location:
            raise SnapshotError(
                "Хранилище {0} уже указывает на {1}, а запрошено {2}. "
                "Снимки, снятые раньше, лежат по прежнему пути — "
                "перерегистрация их потеряет".format(
                    repository, current, location
                )
            )

        return False

    call(
        "PUT",
        path,
        {
            "type": "fs",
            "settings": {"location": location},
        },
    )

    return True


def create_snapshot(
    call: Callable[..., Any],
    *,
    repository: str,
    name: str,
    indices: Tuple[str, ...],
    request_timeout: float,
) -> dict:
    """Снимает снимок и дожидается его завершения."""

    query = urlencode({"wait_for_completion": "true"})
    path = "/_snapshot/{0}/{1}?{2}".format(
        quote(repository, safe=""),
        quote(name, safe=""),
        query,
    )

    payload = {
        "indices": ",".join(indices),
        # Настройки уровня кластера в снимок не идут. Алиасы к ним не
        # относятся — они лежат в метаданных самого индекса и
        # восстанавливаются вместе с ним, а `ES_INDEX` в этом проекте
        # как раз алиас.
        #
        # Восстанавливать же глобальное состояние прямо вредно: оно
        # перезапишет настройки кластера-приёмника целиком, включая те,
        # которых на кластере-источнике не было.
        "include_global_state": False,
        # Снимок с недоступным шардом не нужен: он выглядит как копия и
        # ею не является.
        "partial": False,
    }

    response = call("PUT", path, payload)

    if not isinstance(response, dict):
        raise SnapshotError("Elasticsearch вернул не объект на создание снимка")

    return response.get("snapshot", response)


def list_snapshots(
    call: Callable[..., Any],
    *,
    repository: str,
) -> List[str]:
    """Имена снимков в хранилище, в порядке возрастания."""

    path = "/_snapshot/{0}/_all".format(quote(repository, safe=""))
    response = call("GET", path)

    if not isinstance(response, dict):
        return []

    names = [
        str(item.get("snapshot"))
        for item in response.get("snapshots", [])
        if isinstance(item, dict) and item.get("snapshot")
    ]

    return sorted(names)


def expired_snapshots(
    names: List[str],
    *,
    prefix: str,
    retention: int,
) -> List[str]:
    """
    Снимки сверх срока годности.

    Считаются только свои: имя начинается с префикса. Снимок, снятый
    руками при переезде (`docs/DEPLOY.md` называет его `full`), под
    уборку не попадает — иначе автоматическая задача удаляла бы то,
    что человек снял намеренно.
    """

    if retention <= 0:
        return []

    marker = prefix.strip().lower() + "-"
    own = sorted(name for name in names if name.startswith(marker))

    if len(own) <= retention:
        return []

    return own[: len(own) - retention]


def delete_snapshot(
    call: Callable[..., Any],
    *,
    repository: str,
    name: str,
) -> None:
    path = "/_snapshot/{0}/{1}".format(
        quote(repository, safe=""),
        quote(name, safe=""),
    )
    call("DELETE", path)


def run_snapshot(
    call: Callable[..., Any],
    *,
    repository: str,
    location: str,
    indices: Tuple[str, ...],
    prefix: str,
    retention: int,
    request_timeout: float,
    now: Optional[datetime] = None,
) -> SnapshotResult:
    """Полный цикл: хранилище, снимок, уборка старых."""

    if not indices:
        raise SnapshotError("Не задано, что снимать")

    repository_created = ensure_repository(
        call,
        repository=repository,
        location=location,
    )

    name = snapshot_name(prefix, now=now)

    snapshot = create_snapshot(
        call,
        repository=repository,
        name=name,
        indices=indices,
        request_timeout=request_timeout,
    )

    shards = snapshot.get("shards", {}) if isinstance(snapshot, dict) else {}
    state = str(snapshot.get("state") or "UNKNOWN")
    failed = int(shards.get("failed") or 0)

    failures = tuple(
        str(item)
        for item in (snapshot.get("failures") or [])
    )

    # Снимок с отказавшим шардом — это не снимок. Уборка старых при
    # таком исходе не выполняется: удалить годную копию, оставив
    # негодную, хуже, чем не убрать ничего.
    if state != "SUCCESS" or failed:
        raise SnapshotError(
            "Снимок {0} завершился как {1}, отказавших шардов {2}: {3}".format(
                name,
                state,
                failed,
                "; ".join(failures) or "подробностей нет",
            )
        )

    removed: List[str] = []

    for expired in expired_snapshots(
        list_snapshots(call, repository=repository),
        prefix=prefix,
        retention=retention,
    ):
        delete_snapshot(
            call,
            repository=repository,
            name=expired,
        )
        removed.append(expired)

    return SnapshotResult(
        repository=repository,
        name=name,
        indices=indices,
        state=state,
        shards_total=int(shards.get("total") or 0),
        shards_failed=failed,
        duration_millis=int(snapshot.get("duration_in_millis") or 0),
        removed=tuple(removed),
        repository_created=repository_created,
        failures=failures,
    )


def configured_indices(explicit: Optional[str]) -> Tuple[str, ...]:
    raw = explicit or os.getenv("ES_INDEX") or "sochi_search"

    values = tuple(
        item.strip()
        for item in raw.split(",")
        if item.strip()
    )

    if not values:
        raise ValueError("Пустой список индексов")

    return values


def environment_int(name: str, default: int) -> int:
    raw = os.getenv(name)

    if raw is None or not raw.strip():
        return default

    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            "{0} должно быть целым числом, а не «{1}»".format(name, raw)
        )


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Снимает снимок индекса Elasticsearch и убирает устаревшие."
        ),
    )
    parser.add_argument("--url")
    parser.add_argument(
        "--repository",
        default=os.getenv("ES_SNAPSHOT_REPOSITORY", DEFAULT_REPOSITORY),
    )
    parser.add_argument(
        "--location",
        default=os.getenv("ES_SNAPSHOT_LOCATION", DEFAULT_LOCATION),
        help=(
            "Путь внутри контейнера Elasticsearch. Должен совпадать с "
            "path.repo из docker-compose.yml"
        ),
    )
    parser.add_argument(
        "--indices",
        default=None,
        help="Через запятую. По умолчанию — ES_INDEX",
    )
    parser.add_argument(
        "--prefix",
        default=os.getenv("ES_SNAPSHOT_PREFIX", DEFAULT_PREFIX),
        help=(
            "Приставка в имени снимка. Уборка трогает только снимки с "
            "этой приставкой"
        ),
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=None,
        help="Сколько своих снимков хранить. 0 отключает уборку",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Только показать снимки в хранилище и выйти",
    )

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    arguments = parse_arguments(argv)

    try:
        base_url = configured_url(arguments.url)
        indices = configured_indices(arguments.indices)
        retention = (
            arguments.retention
            if arguments.retention is not None
            else environment_int(
                "ES_SNAPSHOT_RETENTION",
                DEFAULT_RETENTION,
            )
        )
    except ValueError as error:
        print("SNAPSHOT_ERROR={0}".format(error), file=sys.stderr, flush=True)
        return 64

    call = http_call(base_url, arguments.request_timeout)

    if arguments.list:
        try:
            names = list_snapshots(
                call,
                repository=arguments.repository,
            )
        except SnapshotError as error:
            print(
                "SNAPSHOT_ERROR={0}".format(error),
                file=sys.stderr,
                flush=True,
            )
            return 1

        print("SNAPSHOT_COUNT={0}".format(len(names)))

        for name in names:
            print("SNAPSHOT_NAME={0}".format(name))

        return 0

    try:
        result = run_snapshot(
            call,
            repository=arguments.repository,
            location=arguments.location,
            indices=indices,
            prefix=arguments.prefix,
            retention=retention,
            request_timeout=arguments.request_timeout,
        )
    except SnapshotError as error:
        print("SNAPSHOT_OK=0", flush=True)
        print("SNAPSHOT_ERROR={0}".format(error), file=sys.stderr, flush=True)
        return 1

    print("SNAPSHOT_OK=1")
    print("SNAPSHOT_REPOSITORY={0}".format(result.repository))
    print("SNAPSHOT_NAME={0}".format(result.name))
    print("SNAPSHOT_INDICES={0}".format(",".join(result.indices)))
    print("SNAPSHOT_STATE={0}".format(result.state))
    print("SNAPSHOT_SHARDS={0}".format(result.shards_total))
    print("SNAPSHOT_DURATION_MS={0}".format(result.duration_millis))
    print("SNAPSHOT_REMOVED_EXPIRED={0}".format(len(result.removed)))

    if result.repository_created:
        print("SNAPSHOT_REPOSITORY_CREATED=1")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
