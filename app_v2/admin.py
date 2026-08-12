"""
Раздел управления: состояние, очереди и запуск задач из браузера.

Весь раздел закрыт Basic-авторизацией. Поиск остаётся открытым: им
пользуются сотрудники, а управление — единицы.

Логин и пароль берутся из окружения. Пароль можно задать в открытом виде
(`ADMIN_PASSWORD`) или его SHA-256 (`ADMIN_PASSWORD_SHA256`); второй
вариант предпочтителен, потому что `.env` попадает в резервные копии.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from pydantic import BaseModel

from .admin_auth import (
    COOKIE_NAME,
    AuthNotConfigured,
    admin_username,
    check_password,
    expected_password_hash,
    issue_token,
    session_ttl,
    verify_token,
)

from .admin_tasks import TASKS_BY_KEY, runner
from .es_http import ElasticsearchRequestError, es_client
from .pipeline_status import PipelineStatusUnavailable, build_pipeline_status

STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter(prefix="/admin", tags=["Управление"])


def require_admin(request: Request) -> str:
    """
    Пускает по подписанной куке или по заголовку Basic.

    Кука — для человека, который вошёл через форму. Basic — для curl и
    скриптов: без него сводку состояния нельзя было бы забрать из
    командной строки.
    """

    if not expected_password_hash():
        # Молча пускать всех опаснее, чем не работать.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Управление не настроено: задайте ADMIN_PASSWORD_SHA256 "
                "или ADMIN_PASSWORD в .env"
            ),
        )

    token = request.cookies.get(COOKIE_NAME, "")
    username = verify_token(token) if token else None

    if username:
        return username

    header = request.headers.get("Authorization", "")

    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(
                header[6:], validate=True
            ).decode("utf-8")
            given_user, _, given_password = decoded.partition(":")
        except Exception:
            given_user = given_password = ""

        try:
            if check_password(given_user, given_password):
                return given_user
        except AuthNotConfigured:
            pass

    accepts_html = "text/html" in request.headers.get("accept", "")

    if accepts_html:
        # Браузеру показываем страницу входа, а не окно Basic.
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="Требуется вход",
            headers={"Location": "/admin/login"},
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Требуется вход",
        headers={"WWW-Authenticate": 'Basic realm="Управление"'},
    )


class TaskRequest(BaseModel):
    # Optional и Dict вместо `str | None` и `dict[...]` — намеренно.
    #
    # FastAPI и pydantic вычисляют аннотации обработчиков и полей
    # моделей во время выполнения, поэтому `from __future__ import
    # annotations` здесь не помогает: строка «str | None» всё равно
    # передаётся в eval. На рабочем сервере Python 3.8, где ни PEP 604,
    # ни dict[...] ещё не работают, — и API не запускался бы вовсе.
    url: Optional[str] = None


@router.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin-login.html")


@router.post("/login", include_in_schema=False)
def login_submit(
    username: str = Form(default=""),
    password: str = Form(default=""),
) -> Response:
    try:
        valid = check_password(username, password)
    except AuthNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    if not valid:
        return RedirectResponse(
            "/admin/login?error=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    response = RedirectResponse(
        "/admin", status_code=status.HTTP_303_SEE_OTHER
    )
    response.set_cookie(
        COOKIE_NAME,
        issue_token(admin_username()),
        max_age=session_ttl(),
        httponly=True,
        samesite="lax",
        # secure=True включается автоматически, если контур переведут на
        # HTTPS: во внутренней сети сейчас обычный http.
        secure=os.getenv("ADMIN_COOKIE_SECURE", "").strip().lower()
        in {"1", "true", "yes", "on"},
        path="/admin",
    )

    return response


@router.get("/logout", include_in_schema=False)
def logout() -> Response:
    response = RedirectResponse(
        "/admin/login", status_code=status.HTTP_303_SEE_OTHER
    )
    response.delete_cookie(COOKIE_NAME, path="/admin")
    return response


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
def admin_page(_: str = Depends(require_admin)) -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@router.get("/api/state")
def admin_state(user: str = Depends(require_admin)) -> Dict[str, Any]:
    """Сводка: кластер, индекс, очереди и задачи одним запросом."""

    result: dict[str, Any] = {"user": user}

    try:
        info = es_client.info()
        health = es_client.cluster_health()
        count = es_client.count()

        result["elasticsearch"] = {
            "available": True,
            "version": info.get("version", {}).get("number"),
            "cluster_name": health.get("cluster_name"),
            "cluster_status": health.get("status"),
            "documents": count.get("count"),
            "index": es_client.index_name,
        }
    except ElasticsearchRequestError as exc:
        result["elasticsearch"] = {
            "available": False,
            "error": str(exc),
        }

    try:
        result["pipeline"] = build_pipeline_status()
    except PipelineStatusUnavailable as exc:
        result["pipeline"] = {"status": "unavailable", "error": str(exc)}

    result["tasks"] = runner.overview()
    result["services"] = service_states()

    return result


SERVICE_UNITS = (
    ("sochi-search-api-v2.service", "API поиска"),
    ("sochi-search-worker.timer", "Разбор очереди"),
    ("sochi-search-discovery.timer", "Обход сайта"),
    ("sochi-search-publication-date.timer", "Даты публикации"),
    ("sochi-search-content-cleanup.timer", "Очистка текстов"),
    ("sochi-search-pdf-ocr@1.timer", "Распознавание, слот 1"),
    ("sochi-search-pdf-ocr@2.timer", "Распознавание, слот 2"),
    ("sochi-search-backup.timer", "Резервное копирование"),
)


def service_states() -> list[dict[str, Any]]:
    """
    Состояние служб через systemctl.

    Только чтение: `is-active` и `is-enabled` доступны без привилегий.
    Управление службами панель намеренно не даёт — для этого нужен root,
    а задачи запускаются напрямую, без systemd.
    """

    result = []

    for unit, label in SERVICE_UNITS:
        state = {"unit": unit, "label": label}

        for field, argument in (
            ("active", "is-active"),
            ("enabled", "is-enabled"),
        ):
            try:
                completed = subprocess.run(
                    ["systemctl", argument, unit],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    text=True,
                )
                state[field] = (completed.stdout or "").strip() or "unknown"
            except Exception:
                state[field] = "unknown"

        result.append(state)

    return result


@router.post("/api/tasks/{key}")
def start_task(
    key: str,
    request: Optional[TaskRequest] = None,
    user: str = Depends(require_admin),
) -> Dict[str, Any]:
    if key not in TASKS_BY_KEY:
        raise HTTPException(status_code=404, detail="Неизвестная задача")

    url = (request.url if request else None) or None

    if url:
        url = url.strip()

        if not url.startswith(("http://sochi.ru", "https://sochi.ru")):
            raise HTTPException(
                status_code=400,
                detail="Адрес должен начинаться с http://sochi.ru",
            )

    try:
        return runner.start(key, url=url, started_by=user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/tasks/{key}/stop")
def stop_task(
    key: str,
    _: str = Depends(require_admin),
) -> Dict[str, Any]:
    if key not in TASKS_BY_KEY:
        raise HTTPException(status_code=404, detail="Неизвестная задача")

    try:
        return runner.stop(key)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/tasks/{key}/log")
def task_log(
    key: str,
    lines: int = Query(default=200, ge=10, le=2000),
    _: str = Depends(require_admin),
) -> JSONResponse:
    if key not in TASKS_BY_KEY:
        raise HTTPException(status_code=404, detail="Неизвестная задача")

    return JSONResponse(
        {
            "key": key,
            "status": runner.read_status(key),
            "log": runner.tail(key, lines=lines),
        }
    )


@router.get("/api/journal/{unit}")
def unit_journal(
    unit: str,
    lines: int = Query(default=200, ge=10, le=2000),
    _: str = Depends(require_admin),
) -> JSONResponse:
    """
    Журнал systemd-службы, если он доступен на чтение.

    Обычному пользователю журналы системных служб видны только при
    членстве в группе `systemd-journal` или `adm`. Если доступа нет,
    возвращается понятное объяснение, а не пустой ответ.
    """

    known = {name for name, _label in SERVICE_UNITS}

    if unit not in known:
        raise HTTPException(status_code=404, detail="Неизвестная служба")

    try:
        completed = subprocess.run(
            [
                "journalctl",
                "-u", unit.replace(".timer", ".service"),
                "-n", str(lines),
                "--no-pager",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            text=True,
        )
        output = completed.stdout or ""
    except FileNotFoundError:
        output = "journalctl недоступен в этом окружении."
    except Exception as exc:
        output = f"Не удалось прочитать журнал: {exc}"

    if "No journal files were found" in output or not output.strip():
        output = (
            "Журнал недоступен пользователю, от которого работает API.\n"
            "Чтобы включить: добавить в юнит API строку\n"
            "  SupplementaryGroups=systemd-journal\n"
            "и выполнить systemctl daemon-reload с перезапуском службы."
        )

    return JSONResponse({"unit": unit, "log": output})
