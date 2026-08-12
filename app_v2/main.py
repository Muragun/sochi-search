from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .es_http import (
    ElasticsearchRequestError,
    es_client,
)
from .search_service import (
    build_extractive_answer,
    search_documents,
)
from .pipeline_status import (
    PipelineStatusUnavailable,
    build_pipeline_status,
)
from .admin import router as admin_router


app = FastAPI(
    title="Поиск по сайту Сочи",
    description=(
        "Поиск по страницам и документам сайта sochi.ru, "
        "проиндексированным в Elasticsearch."
    ),
    version="2.5.0",
)


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

ALLOWED_DOC_TYPES = {
    None,
    "file",
    "page",
    "pdf",
}

ALLOWED_CATEGORIES = {
    None,
    "news",
    "legal",
    "announcement",
    "media",
    "press",
    "city_authority",
    "city_life",
    "city",
    "events",
    "file",
    "page",
    "legacy",
}

MAX_RESULT_WINDOW = 10000

ALLOWED_SORTS = {
    "relevance",
    "date_desc",
    "date_asc",
}

# Служебный раздел закрыт Basic-авторизацией внутри самого роутера:
# поиск остаётся открытым, управление — нет.
app.include_router(admin_router)

app.mount(
    "/assets",
    StaticFiles(
        directory=str(STATIC_DIR),
    ),
    name="assets",
)


def normalize_search_options(
    *,
    q: Optional[str],
    document_number: Optional[str],
    document_kind: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    doc_type: Optional[str],
    is_attachment: Optional[bool],
    content_category: Optional[str],
    sort: str,
    require_query: bool = False,
) -> dict:
    """Нормализует общие параметры /search и /answer."""
    normalized_query = (
        q.strip()
        if q
        else ""
    )

    if (
        normalized_query
        and len(normalized_query) < 2
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Текстовый запрос должен "
                "содержать минимум два символа"
            ),
        )

    if require_query and len(normalized_query) < 2:
        raise HTTPException(
            status_code=422,
            detail=(
                "Для краткого ответа введите "
                "минимум два символа"
            ),
        )

    normalized_doc_type = (
        doc_type.strip().lower()
        if doc_type
        else None
    )

    if normalized_doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                "doc_type должен быть "
                "page, pdf или file"
            ),
        )

    normalized_category = (
        content_category.strip().lower()
        if content_category
        else None
    )

    if normalized_category not in ALLOWED_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail="Неизвестный раздел сайта",
        )

    normalized_sort = sort.strip().lower()

    if normalized_sort not in ALLOWED_SORTS:
        raise HTTPException(
            status_code=422,
            detail=(
                "sort должен быть relevance, "
                "date_desc или date_asc"
            ),
        )

    if (
        date_from is not None
        and date_to is not None
        and date_from > date_to
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "date_from не может быть "
                "позже date_to"
            ),
        )

    normalized_number = (
        document_number.strip()
        if document_number
        else None
    )

    normalized_kind = (
        document_kind.strip()
        if document_kind
        else None
    )

    has_search_condition = any(
        (
            bool(normalized_query),
            bool(normalized_number),
            bool(normalized_kind),
            date_from is not None,
            date_to is not None,
            bool(normalized_doc_type),
            bool(normalized_category),
            is_attachment is not None,
        )
    )

    if not has_search_condition:
        raise HTTPException(
            status_code=422,
            detail=(
                "Введите текст запроса "
                "или задайте хотя бы один фильтр"
            ),
        )

    return {
        "query": normalized_query,
        "document_number": normalized_number,
        "document_kind": normalized_kind,
        "date_from": (
            date_from.isoformat()
            if date_from
            else None
        ),
        "date_to": (
            date_to.isoformat()
            if date_to
            else None
        ),
        "doc_type": normalized_doc_type,
        "is_attachment": is_attachment,
        "content_category": normalized_category,
        "sort": normalized_sort,
    }


def elasticsearch_http_exception(
    exc: ElasticsearchRequestError,
) -> HTTPException:
    detail = {
        "message": str(exc),
        "elasticsearch_status": exc.status_code,
        "elasticsearch_response": exc.response_body,
    }

    return HTTPException(
        status_code=502,
        detail=detail,
    )


@app.get(
    "/ui",
    include_in_schema=False,
)
@app.get(
    "/ui/",
    include_in_schema=False,
)
def search_ui() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html"
    )


@app.get("/")
def root() -> dict:
    return {
        "service": "sochi-search-api",
        "version": app.version,
        "ui": "/ui",
        "admin": "/admin",
        "docs": "/docs",
        "health": "/health/ready",
        "search": "/search?q=текст",
        "answer": "/answer?q=вопрос",
        "pipeline_status": "/pipeline/status",
    }


@app.get("/health/live")
def health_live() -> dict:
    """
    Показывает, что процесс FastAPI запущен.

    Elasticsearch здесь не проверяется.
    """
    return {
        "status": "ok",
        "service": "sochi-search-api",
        "version": app.version,
    }


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    """
    Проверяет Elasticsearch и наличие индекса.
    """
    try:
        info = es_client.info()
        cluster = es_client.cluster_health()
        mapping = es_client.index_mapping()

        resolved_indices = sorted(mapping.keys())
        index_exists = bool(resolved_indices)
        cluster_status = cluster.get("status")

        ready = (
            index_exists
            and cluster_status != "red"
        )

        payload = {
            "status": "ready" if ready else "not_ready",
            "elasticsearch": True,
            "elasticsearch_version": (
                info.get("version", {}).get("number")
            ),
            "cluster_name": cluster.get("cluster_name"),
            "cluster_status": cluster_status,
            "index": settings.es_index,
            "index_exists": index_exists,
            "resolved_indices": resolved_indices,
        }

        return JSONResponse(
            content=payload,
            status_code=200 if ready else 503,
        )

    except ElasticsearchRequestError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "elasticsearch": False,
                "index": settings.es_index,
                "error": str(exc),
                "elasticsearch_status": exc.status_code,
                "elasticsearch_response": exc.response_body,
            },
        )


@app.get("/stats")
def stats() -> dict:
    try:
        count_response = es_client.count()
        cluster = es_client.cluster_health()
    except ElasticsearchRequestError as exc:
        raise elasticsearch_http_exception(exc) from exc

    return {
        "index": settings.es_index,
        "documents": count_response.get("count", 0),
        "cluster_status": cluster.get("status"),
        "active_shards": cluster.get("active_shards"),
        "unassigned_shards": cluster.get(
            "unassigned_shards"
        ),
    }


@app.get("/pipeline/status")
def pipeline_status() -> JSONResponse:
    """Read-only progress of crawler and auxiliary data queues."""
    es_documents = None
    es_available = True

    try:
        es_documents = int(
            es_client.count().get("count", 0)
        )
    except ElasticsearchRequestError:
        es_available = False

    try:
        payload = build_pipeline_status(
            settings.state_db_path,
            elasticsearch_documents=es_documents,
            elasticsearch_available=es_available,
        )
    except PipelineStatusUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "message": str(exc),
            },
        )

    return JSONResponse(
        content=payload,
        headers={
            "Cache-Control": "no-store",
        },
    )


@app.get("/search")
def search(
    q: Optional[str] = Query(
        default=None,
        max_length=300,
        description=(
            "Необязательный текстовый запрос. "
            "Можно искать только по номеру "
            "или виду документа."
        ),
    ),

    limit: int = Query(
        default=settings.search_default_limit,
        ge=1,
        le=settings.search_max_limit,
    ),

    offset: int = Query(
        default=0,
        ge=0,
        le=9000,
    ),

    document_number: Optional[str] = Query(
        default=None,
        max_length=100,
        description=(
            "Точный номер документа, "
            "например 30-р"
        ),
    ),

    document_kind: Optional[str] = Query(
        default=None,
        max_length=100,
        description=(
            "Тип документа: постановление, "
            "распоряжение, решение и т. п."
        ),
    ),

    date_from: Optional[date] = Query(
        default=None,
        description=(
            "Дата документа от, "
            "формат YYYY-MM-DD"
        ),
    ),

    date_to: Optional[date] = Query(
        default=None,
        description=(
            "Дата документа до, "
            "формат YYYY-MM-DD"
        ),
    ),

    doc_type: Optional[str] = Query(
        default=None,
        max_length=20,
        description=(
            "Тип содержимого: page, pdf или file"
        ),
    ),

    is_attachment: Optional[bool] = Query(
        default=None,
        description=(
            "Только вложения или "
            "только основные страницы"
        ),
    ),

    content_category: Optional[str] = Query(
        default=None,
        max_length=40,
        description=(
            "Раздел сайта: news, legal, "
            "announcement, media, press, "
            "city_authority, city_life, city, "
            "events, file, page или legacy"
        ),
    ),

    sort: str = Query(
        default="relevance",
        max_length=20,
        description=(
            "relevance, date_desc "
            "или date_asc"
        ),
    ),
) -> dict:
    options = normalize_search_options(
        q=q,
        document_number=document_number,
        document_kind=document_kind,
        date_from=date_from,
        date_to=date_to,
        doc_type=doc_type,
        is_attachment=is_attachment,
        content_category=content_category,
        sort=sort,
    )

    # from + size не должно превышать index.max_result_window (10000),
    # иначе Elasticsearch отвечает ошибкой на последней странице выдачи.
    if offset + limit > MAX_RESULT_WINDOW:
        limit = max(1, MAX_RESULT_WINDOW - offset)

    try:
        return search_documents(
            options.pop("query"),
            limit=limit,
            offset=offset,
            **options,
        )

    except ElasticsearchRequestError as exc:
        raise elasticsearch_http_exception(
            exc
        ) from exc


@app.get("/answer")
def answer(
    q: str = Query(
        min_length=2,
        max_length=500,
        description="Вопрос пользователя",
    ),
    limit: int = Query(
        default=5,
        ge=1,
        le=10,
    ),
    document_number: Optional[str] = Query(
        default=None,
        max_length=100,
    ),
    document_kind: Optional[str] = Query(
        default=None,
        max_length=100,
    ),
    date_from: Optional[date] = Query(
        default=None,
    ),
    date_to: Optional[date] = Query(
        default=None,
    ),
    doc_type: Optional[str] = Query(
        default=None,
        max_length=20,
    ),
    is_attachment: Optional[bool] = Query(
        default=None,
    ),
    content_category: Optional[str] = Query(
        default=None,
        max_length=40,
    ),
) -> dict:
    options = normalize_search_options(
        q=q,
        document_number=document_number,
        document_kind=document_kind,
        date_from=date_from,
        date_to=date_to,
        doc_type=doc_type,
        is_attachment=is_attachment,
        content_category=content_category,
        sort="relevance",
        require_query=True,
    )

    try:
        search_result = search_documents(
            options.pop("query"),
            limit=limit,
            offset=0,
            **options,
        )
    except ElasticsearchRequestError as exc:
        raise elasticsearch_http_exception(exc) from exc

    return build_extractive_answer(
        search_result
    )
