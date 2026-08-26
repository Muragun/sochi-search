from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_OCR_TESSDATA = (
    Path(__file__).resolve().parents[1]
    / "ocr"
    / "tessdata"
)


def env_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    return raw_value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
) -> int:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        return default

    return max(value, minimum)


def env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    try:
        value = float(raw_value)
    except ValueError:
        return default

    value = max(value, minimum)

    if maximum is not None:
        value = min(value, maximum)

    return value


def env_list(
    name: str,
    default: str,
) -> tuple[str, ...]:
    raw_value = os.getenv(name, default)

    return tuple(
        value.strip().lower()
        for value in raw_value.split(",")
        if value.strip()
    )


def env_rotations(
    name: str,
    default: str,
) -> tuple[int, ...]:
    result: list[int] = []

    for raw_value in os.getenv(
        name,
        default,
    ).split(","):
        try:
            value = int(
                raw_value.strip()
            ) % 360
        except ValueError:
            continue

        if value not in {
            0,
            90,
            180,
            270,
        }:
            continue

        if value not in result:
            result.append(value)

    if 0 not in result:
        result.insert(0, 0)

    return tuple(result)


@dataclass(frozen=True)
class CrawlerSettings:
    es_url: str
    es_index: str
    es_username: str
    es_password: str
    es_verify_tls: bool

    state_db_path: Path
    scroll_ttl: str
    batch_size: int

    allowed_hosts: tuple[str, ...]
    user_agent: str

    connect_timeout: int
    read_timeout: int
    max_download_bytes: int
    pdf_max_download_bytes: int
    download_temp_dir: Path
    download_min_free_bytes: int

    chunk_size: int
    chunk_overlap: int
    min_text_length: int

    pdf_ocr_enabled: bool
    pdf_ocr_tessdata_path: Path
    pdf_ocr_languages: str
    pdf_ocr_dpi: int
    pdf_ocr_max_pages: int
    pdf_ocr_rotations: tuple[int, ...]
    pdf_ocr_page_timeout_seconds: int
    pdf_ocr_document_budget_seconds: int

    pdf_ocr_engine: str
    pdf_ocr_binary: str
    pdf_ocr_psm: int
    pdf_ocr_oem: int
    pdf_ocr_threads: int
    pdf_ocr_minimum_dpi: int
    pdf_ocr_maximum_dpi: int
    pdf_ocr_pixel_budget: int
    pdf_ocr_deskew: bool
    pdf_ocr_binarize: bool
    pdf_ocr_sauvola_k: float
    pdf_ocr_retry_sauvola_k: float
    pdf_ocr_confidence_floor: float
    pdf_ocr_retry_rotations: tuple[int, ...]
    pdf_ocr_ladder_budget_seconds: int
    pdf_ocr_priority_pages: int
    pdf_ocr_coverage_pages: int
    pdf_ocr_batch_documents: int
    pdf_ocr_batch_deadline_seconds: int
    pdf_ocr_cache_dir: Path
    pdf_ocr_cache_max_bytes: int
    pdf_fast_processing_timeout_seconds: int
    pdf_processing_timeout_seconds: int

    worker_batch_limit: int
    recheck_hours: int
    retry_hours: int
    max_retry_attempts: int
    missing_retry_hours: int
    delete_missing_after: int
    lock_timeout_minutes: int


settings = CrawlerSettings(
    es_url=os.getenv(
        "ES_URL",
        "http://127.0.0.1:9200",
    ).rstrip("/"),

    es_index=os.getenv(
        "ES_INDEX",
        "sochi_search",
    ),

    es_username=os.getenv(
        "ES_USERNAME",
        "",
    ),

    es_password=os.getenv(
        "ES_PASSWORD",
        "",
    ),

    es_verify_tls=env_bool(
        "ES_VERIFY_TLS",
        True,
    ),

    state_db_path=Path(
        os.getenv(
            "CRAWL_STATE_DB",
            "/opt/sochi-search/data/crawl_state.sqlite3",
        )
    ),

    scroll_ttl=os.getenv(
        "CRAWL_SCROLL_TTL",
        "2m",
    ),

    batch_size=env_int(
        "CRAWL_BATCH_SIZE",
        1000,
        minimum=100,
    ),

    allowed_hosts=env_list(
        "CRAWL_ALLOWED_HOSTS",
        "sochi.ru",
    ),

    # Значение по умолчанию, а не пример: без CRAWL_USER_AGENT в
    # настройках сайт видит именно его. Раньше здесь стояло
    # «SochiSearchBot/2.0» — другое имя и версия трёхлетней давности, —
    # а в примере настроек «SochiSearch/2.5.0». Две строки, ни одна не
    # совпадала с VERSION, и по журналу сайта нельзя было понять, что за
    # обходчик пришёл.
    #
    # Пояснение в скобках — не украшение. По этой строке администратор
    # sochi.ru решает, что делать с незнакомым обходчиком: «внутренний
    # поиск муниципалитета» и «неизвестный бот» — разные решения. На
    # рабочей машине пояснение было и раньше; при сведении значений оно
    # едва не потерялось.
    user_agent=os.getenv(
        "CRAWL_USER_AGENT",
        "SochiSearch/2.6.0 (+internal municipal search index)",
    ),

    connect_timeout=env_int(
        "CRAWL_CONNECT_TIMEOUT",
        10,
        minimum=1,
    ),

    read_timeout=env_int(
        "CRAWL_READ_TIMEOUT",
        60,
        minimum=1,
    ),

    max_download_bytes=env_int(
        "CRAWL_MAX_DOWNLOAD_BYTES",
        50 * 1024 * 1024,
        minimum=1024,
    ),

    pdf_max_download_bytes=env_int(
        "CRAWL_PDF_MAX_DOWNLOAD_BYTES",
        150 * 1024 * 1024,
        minimum=1024 * 1024,
    ),

    download_temp_dir=Path(
        os.getenv(
            "CRAWL_DOWNLOAD_TEMP_DIR",
            str(
                Path(
                    os.getenv(
                        "SOCHI_SEARCH_DIR",
                        "/opt/sochi-search",
                    )
                )
                / "run"
                / "downloads"
            ),
        )
    ),

    download_min_free_bytes=env_int(
        "CRAWL_DOWNLOAD_MIN_FREE_BYTES",
        512 * 1024 * 1024,
        minimum=0,
    ),

    chunk_size=env_int(
        "CRAWL_CHUNK_SIZE",
        3500,
        minimum=500,
    ),

    chunk_overlap=env_int(
        "CRAWL_CHUNK_OVERLAP",
        350,
        minimum=0,
    ),

    min_text_length=env_int(
        "CRAWL_MIN_TEXT_LENGTH",
        80,
        minimum=1,
    ),

    pdf_ocr_enabled=env_bool(
        "CRAWL_PDF_OCR_ENABLED",
        True,
    ),

    pdf_ocr_tessdata_path=Path(
        os.getenv(
            "CRAWL_PDF_OCR_TESSDATA",
            str(DEFAULT_OCR_TESSDATA),
        )
    ),

    # Только русский.
    #
    # Замер на синтетических страницах с известным эталоном: добавление
    # `eng` не даёт ни одного лишнего верного слова, но разрешает Tesseract
    # выбирать латинские буквы там, где он не уверен. На выцветшей копии
    # это дало 155 полностью латинских «слов» из 261 — ту самую глифовую
    # кашу, из-за которой документ уходил в `skipped_empty`. Точность на
    # той же странице 32,6 % против 49,7 % у `rus`, время вдвое больше.
    # Номер акта при `rus+eng` пришёл как «512-p» с латинской «p» — для
    # поиска это другой номер.
    #
    # Для двуязычных вложений значение остаётся настройкой.
    pdf_ocr_languages=os.getenv(
        "CRAWL_PDF_OCR_LANGUAGES",
        "rus",
    ).strip()
    or "rus",

    pdf_ocr_dpi=min(
        env_int(
            "CRAWL_PDF_OCR_DPI",
            180,
            minimum=150,
        ),
        400,
    ),

    pdf_ocr_max_pages=env_int(
        "CRAWL_PDF_OCR_MAX_PAGES",
        200,
        minimum=1,
    ),

    pdf_ocr_rotations=env_rotations(
        "CRAWL_PDF_OCR_ROTATIONS",
        "0",
    ),

    pdf_ocr_page_timeout_seconds=env_int(
        "CRAWL_PDF_OCR_PAGE_TIMEOUT_SECONDS",
        45,
        minimum=5,
    ),

    pdf_ocr_document_budget_seconds=env_int(
        "CRAWL_PDF_OCR_DOCUMENT_BUDGET_SECONDS",
        6 * 60,
        minimum=30,
    ),

    pdf_ocr_engine=os.getenv(
        "CRAWL_PDF_OCR_ENGINE",
        "tesseract-cli",
    ).strip() or "tesseract-cli",

    pdf_ocr_binary=os.getenv(
        "CRAWL_TESSERACT_BINARY",
        "tesseract",
    ).strip() or "tesseract",

    # 3 — автоматическая сегментация без определения ориентации. На
    # замерах psm=6 на зашумлённой странице уходил в комбинаторный разбор
    # и занимал десятки секунд вместо двух.
    pdf_ocr_psm=env_int(
        "CRAWL_PDF_OCR_PSM",
        3,
        minimum=0,
    ),

    # 1 — только LSTM.
    pdf_ocr_oem=env_int(
        "CRAWL_PDF_OCR_OEM",
        1,
        minimum=0,
    ),

    # Один поток на процесс: параллелизм даётся числом воркеров, а не
    # OpenMP внутри Tesseract. Иначе несколько воркеров конкурируют за одни
    # и те же ядра и общая пропускная способность падает.
    pdf_ocr_threads=env_int(
        "CRAWL_PDF_OCR_THREADS",
        1,
        minimum=1,
    ),

    # Замер на сервере: время Tesseract определяется числом текстовых
    # строк, а не площадью растра. Рост 3,9 → 8,0 Мпикс дал +8 % времени и
    # +0,7 % текста. Поэтому рендерим в родном разрешении скана (обычно
    # 200), экономя память и подготовку, а не гонимся за высоким dpi.
    pdf_ocr_minimum_dpi=env_int(
        "CRAWL_PDF_OCR_MINIMUM_DPI",
        200,
        minimum=72,
    ),

    pdf_ocr_maximum_dpi=env_int(
        "CRAWL_PDF_OCR_MAXIMUM_DPI",
        400,
        minimum=72,
    ),

    # Потолок площади растра. Крупноформатный лист (план, схема) при 300 dpi
    # даёт десятки мегапикселей и в одиночку съедает бюджет документа.
    pdf_ocr_pixel_budget=env_int(
        "CRAWL_PDF_OCR_PIXEL_BUDGET",
        8_000_000,
        minimum=1_000_000,
    ),

    pdf_ocr_deskew=env_bool(
        "CRAWL_PDF_OCR_DESKEW",
        True,
    ),

    pdf_ocr_binarize=env_bool(
        "CRAWL_PDF_OCR_BINARIZE",
        True,
    ),

    # Насколько порог Сауволы отходит от локального среднего. Строгое
    # значение лучше на обычной архивной копии (98,4 % против 93,9 %),
    # мягкое — на сильно засвеченной (54,6 % против 49,7 %). Одного
    # значения на обе страницы нет, поэтому мягкое стало второй ступенью.
    pdf_ocr_sauvola_k=env_float(
        "CRAWL_PDF_OCR_SAUVOLA_K",
        0.15,
        minimum=0.01,
        maximum=0.9,
    ),

    pdf_ocr_retry_sauvola_k=env_float(
        "CRAWL_PDF_OCR_RETRY_SAUVOLA_K",
        0.10,
        minimum=0.01,
        maximum=0.9,
    ),

    # Ниже этой средней уверенности страница пробуется ещё раз.
    #
    # Замер: чистый скан — 95,8, обычная архивная копия — 90,2, выцветшая
    # копия с точностью 50 % — 23,7, повёрнутая страница — 19,9. Порог 65
    # лежит в пустоте между двумя группами. Ноль выключает повторы.
    pdf_ocr_confidence_floor=env_float(
        "CRAWL_PDF_OCR_CONFIDENCE_FLOOR",
        65.0,
        minimum=0.0,
        maximum=100.0,
    ),

    # Повороты перебираются только для страниц, на которых Tesseract сам
    # сообщил о низкой уверенности. Порядок — по частоте: альбомный скан
    # встречается чаще перевёрнутого.
    pdf_ocr_retry_rotations=env_rotations(
        "CRAWL_PDF_OCR_RETRY_ROTATIONS",
        "90,270,180",
    ),

    # Общий потолок времени на страницу вместе со всеми повторами.
    pdf_ocr_ladder_budget_seconds=env_int(
        "CRAWL_PDF_OCR_LADDER_BUDGET_SECONDS",
        150,
        minimum=10,
    ),

    pdf_ocr_priority_pages=env_int(
        "CRAWL_PDF_OCR_PRIORITY_PAGES",
        2,
        minimum=1,
    ),

    # Первый проход по всему корпусу распознаёт только начало документа:
    # вид акта, номер, дата и первые абзацы. Так весь корпус становится
    # искомым за часы, а не за недели. Ноль — без ограничения.
    pdf_ocr_coverage_pages=env_int(
        "CRAWL_PDF_OCR_COVERAGE_PAGES",
        2,
        minimum=0,
    ),

    pdf_ocr_batch_documents=env_int(
        "CRAWL_PDF_OCR_BATCH_DOCUMENTS",
        25,
        minimum=1,
    ),

    pdf_ocr_batch_deadline_seconds=env_int(
        "CRAWL_PDF_OCR_BATCH_DEADLINE_SECONDS",
        20 * 60,
        minimum=60,
    ),

    pdf_ocr_cache_dir=Path(
        os.getenv(
            "CRAWL_PDF_OCR_CACHE_DIR",
            str(
                Path(
                    os.getenv(
                        "SOCHI_SEARCH_DIR",
                        "/opt/sochi-search",
                    )
                )
                / "run"
                / "pdf-cache"
            ),
        )
    ),

    pdf_ocr_cache_max_bytes=env_int(
        "CRAWL_PDF_OCR_CACHE_MAX_BYTES",
        8 * 1024 * 1024 * 1024,
        minimum=0,
    ),

    pdf_fast_processing_timeout_seconds=env_int(
        "CRAWL_PDF_FAST_PROCESS_TIMEOUT_SECONDS",
        2 * 60,
        minimum=30,
    ),

    pdf_processing_timeout_seconds=env_int(
        "CRAWL_PDF_PROCESS_TIMEOUT_SECONDS",
        10 * 60,
        minimum=60,
    ),

    worker_batch_limit=env_int(
        "CRAWL_WORKER_BATCH_LIMIT",
        10,
        minimum=1,
    ),

    recheck_hours=env_int(
        "CRAWL_RECHECK_HOURS",
        168,
        minimum=1,
    ),

    retry_hours=env_int(
        "CRAWL_RETRY_HOURS",
        6,
        minimum=1,
    ),

    max_retry_attempts=env_int(
        "CRAWL_MAX_RETRY_ATTEMPTS",
        5,
        minimum=1,
    ),

    missing_retry_hours=env_int(
        "CRAWL_MISSING_RETRY_HOURS",
        24,
        minimum=1,
    ),

    delete_missing_after=env_int(
        "CRAWL_DELETE_MISSING_AFTER",
        3,
        minimum=1,
    ),

    lock_timeout_minutes=env_int(
        "CRAWL_LOCK_TIMEOUT_MINUTES",
        60,
        minimum=1,
    ),
)


if settings.chunk_overlap >= settings.chunk_size:
    raise RuntimeError(
        "CRAWL_CHUNK_OVERLAP должен быть меньше "
        "CRAWL_CHUNK_SIZE"
    )
