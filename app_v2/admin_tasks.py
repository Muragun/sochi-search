"""
Запуск обслуживающих задач из браузера.

Задачи выполняются теми же командами, что и systemd-юниты, и берут те же
файлы блокировки. Поэтому запуск из панели не может столкнуться с
запуском по расписанию: второй экземпляр просто завершится с кодом 75.

Привилегий не требуется: процесс порождается от того же пользователя, что
и API, в отдельной сессии, поэтому переживает перезапуск самого API.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings


@dataclass(frozen=True)
class TaskDefinition:
    key: str
    title: str
    description: str
    module: str
    arguments: tuple[str, ...]
    lock_name: str
    danger: bool = False
    accepts_url: bool = False


TASKS: tuple[TaskDefinition, ...] = (
    TaskDefinition(
        key="discovery",
        title="Обход сайта",
        description=(
            "Ищет новые адреса на sochi.ru и добавляет их в очередь. "
            "Сам по себе ничего не индексирует."
        ),
        module="crawler_v2.discovery_worker_v2",
        arguments=(
            "--html-only",
            "--html-max-pages", "1200",
            "--html-max-depth", "4",
            "--html-max-pagination-page", "120",
            "--max-new", "10000",
            "--report-samples", "5",
            "--delay", "0.15",
        ),
        lock_name="discovery-worker.lock",
    ),
    TaskDefinition(
        key="worker",
        title="Разобрать очередь",
        description=(
            "Скачивает и индексирует документы из очереди. "
            "За один запуск берёт до ста адресов."
        ),
        module="crawler_v2.incremental_worker",
        arguments=("--limit", "100"),
        lock_name="incremental-worker.lock",
    ),
    TaskDefinition(
        key="pdf_ocr",
        title="Распознать PDF",
        description=(
            "Фоновое распознавание отсканированных документов. "
            "Обрабатывает пачку до внутреннего дедлайна."
        ),
        module="crawler_v2.incremental_worker",
        arguments=(
            "--pdf-ocr-backfill",
            "--batch",
            "--worker-slot", "panel",
            "--no-http-cache",
        ),
        lock_name="pdf-ocr-worker-panel.lock",
    ),
    TaskDefinition(
        key="publication_date",
        title="Заполнить даты",
        description=(
            "Определяет даты публикации у документов, у которых их нет. "
            "Без даты не работают фильтр по годам и сортировка."
        ),
        module="crawler_v2.publication_date_worker",
        arguments=("--limit", "200"),
        lock_name="publication-date-worker.lock",
    ),
    TaskDefinition(
        key="content_cleanup",
        title="Почистить тексты",
        description=(
            "Убирает служебные фрагменты вроде «Номер документа:» "
            "из текста страниц."
        ),
        module="crawler_v2.content_cleanup_worker",
        arguments=("--limit", "200"),
        lock_name="content-cleanup-worker.lock",
    ),
    TaskDefinition(
        key="reindex_url",
        title="Переиндексировать документ",
        description=(
            "Скачивает и разбирает один конкретный адрес заново. "
            "Полезно, когда документ выглядит неправильно."
        ),
        module="crawler_v2.incremental_worker",
        arguments=("--limit", "1", "--no-http-cache"),
        lock_name="incremental-worker.lock",
        accepts_url=True,
    ),
)

TASKS_BY_KEY = {task.key: task for task in TASKS}


def state_directory() -> Path:
    return Path(
        os.getenv(
            "ADMIN_TASK_DIR",
            str(settings.state_db_path.parent.parent / "run" / "admin-tasks"),
        )
    )


class TaskRunner:
    """Порождает задачу и следит за её состоянием через файлы."""

    def __init__(self) -> None:
        self.directory = state_directory()
        self.directory.mkdir(parents=True, exist_ok=True)

    def log_path(self, key: str) -> Path:
        return self.directory / f"{key}.log"

    def status_path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def read_status(self, key: str) -> dict[str, Any]:
        path = self.status_path(key)

        if not path.is_file():
            return {"state": "never", "key": key}

        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"state": "unknown", "key": key}

        if status.get("state") != "running":
            return status

        pid = int(status.get("pid") or 0)

        if pid and self._alive(pid):
            status["elapsed_seconds"] = int(
                time.time() - float(status.get("started_at_epoch") or 0)
            )
            return status

        # Процесс исчез, а завершения никто не записал: API мог быть
        # перезапущен во время работы задачи.
        status["state"] = "finished"
        status["note"] = "завершилась во время перезапуска API"
        self.status_path(key).write_text(
            json.dumps(status, ensure_ascii=False),
            encoding="utf-8",
        )
        return status

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

        return True

    def is_running(self, key: str) -> bool:
        return self.read_status(key).get("state") == "running"

    def build_command(
        self,
        task: TaskDefinition,
        *,
        url: str | None,
    ) -> list[str]:
        project = Path(__file__).resolve().parents[1]
        lock = project / "run" / task.lock_name
        lock.parent.mkdir(parents=True, exist_ok=True)

        command = [
            "/usr/bin/flock",
            "--nonblock",
            "--conflict-exit-code", "75",
            str(lock),
            sys.executable,
            "-B",
            "-m",
            task.module,
            *task.arguments,
        ]

        if task.accepts_url and url:
            command.extend(["--url", url])

        return command

    def start(
        self,
        key: str,
        *,
        url: str | None = None,
        started_by: str = "",
    ) -> dict[str, Any]:
        task = TASKS_BY_KEY.get(key)

        if task is None:
            raise KeyError(key)

        if task.accepts_url and not url:
            raise ValueError("Для этой задачи нужен адрес документа")

        if self.is_running(key):
            raise RuntimeError("Задача уже выполняется")

        command = self.build_command(task, url=url)
        log = self.log_path(key)
        project = Path(__file__).resolve().parents[1]

        with log.open("wb") as stream:
            header = (
                f"# {task.title}\n"
                f"# запустил: {started_by or 'неизвестно'}\n"
                f"# команда: {' '.join(shlex.quote(p) for p in command)}\n"
                f"# начало: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )
            stream.write(header.encode("utf-8"))
            stream.flush()

            process = subprocess.Popen(
                command,
                cwd=str(project),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        status = {
            "key": key,
            "title": task.title,
            "state": "running",
            "pid": process.pid,
            "started_by": started_by,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started_at_epoch": time.time(),
            "url": url or "",
        }

        self.status_path(key).write_text(
            json.dumps(status, ensure_ascii=False),
            encoding="utf-8",
        )

        return status

    def stop(self, key: str) -> dict[str, Any]:
        status = self.read_status(key)

        if status.get("state") != "running":
            raise RuntimeError("Задача не выполняется")

        pid = int(status.get("pid") or 0)

        if pid:
            try:
                # Снимаем всю группу: flock и дочерние процессы тоже.
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        status["state"] = "stopped"
        status["stopped_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.status_path(key).write_text(
            json.dumps(status, ensure_ascii=False),
            encoding="utf-8",
        )

        return status

    def tail(self, key: str, *, lines: int = 200) -> str:
        path = self.log_path(key)

        if not path.is_file():
            return "Задача ещё не запускалась."

        window = 256_000

        # Читаем именно хвост, а не файл целиком. Журнал долгого обхода —
        # сотни мегабайт; прежний read_bytes() поднимал его в память
        # полностью на каждое нажатие «Журнал», а в режиме слежения это
        # повторялось каждые пять секунд.
        try:
            with path.open("rb") as source:
                size = source.seek(0, os.SEEK_END)
                source.seek(max(0, size - window))
                data = source.read()
        except OSError as exc:
            return f"Не удалось прочитать журнал: {exc}"

        text = data.decode("utf-8", errors="replace")
        rows = text.split("\n")

        # Первая строка после смещения почти всегда обрезана посередине.
        if size > window and len(rows) > 1:
            rows = rows[1:]

        return "\n".join(rows[-lines:])

    def overview(self) -> list[dict[str, Any]]:
        result = []

        for task in TASKS:
            status = self.read_status(task.key)
            result.append(
                {
                    "key": task.key,
                    "title": task.title,
                    "description": task.description,
                    "accepts_url": task.accepts_url,
                    "state": status.get("state", "never"),
                    "started_at": status.get("started_at"),
                    "started_by": status.get("started_by"),
                    "elapsed_seconds": status.get("elapsed_seconds"),
                    "note": status.get("note"),
                }
            )

        return result


runner = TaskRunner()
