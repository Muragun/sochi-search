from __future__ import annotations

import html
import re
from typing import Any

from .es_http import es_client
from crawler_v2.pdf_ocr import (
    looks_like_garbled_text,
)
from crawler_v2.text_repair import technical_filename


HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")

GENERIC_TITLE_PARTS = (
    "администрация города сочи",
    "официальное опубликование муниципальных правовых актов",
    "pdf-документ",
    "скачать документ",
    "скачать файл",
)

DOCUMENT_MARKERS = (
    "Постановление",
    "Распоряжение",
    "Приказ",
    "Решение",
    "Положение",
    "Извещение",
    "Объявление",
    "Протокол",
)

# Слова, которые обычно не определяют смысл пользовательского вопроса.
QUESTION_STOP_WORDS = {
    "что",
    "кто",
    "где",
    "когда",
    "как",
    "какой",
    "какая",
    "какие",
    "какое",
    "кого",
    "кому",
    "чем",
    "ли",
    "это",
    "есть",
    "нужно",
    "можно",
    "найти",
    "найди",
    "покажи",
    "расскажи",
}



def normalize_text(value: str) -> str:
    """Убирает повторяющиеся пробелы и переводы строк."""
    return WHITESPACE_RE.sub(
        " ",
        value or "",
    ).strip()


def remove_html_tags(value: str) -> str:
    """Удаляет HTML-теги из подсвеченного Elasticsearch-фрагмента."""
    without_tags = HTML_TAG_RE.sub("", value)
    decoded = html.unescape(without_tags)

    return normalize_text(decoded)


def truncate(
    value: str,
    limit: int = 500,
) -> str:
    """Сокращает длинный текст без ошибки на пустом значении."""
    normalized = normalize_text(value)

    if len(normalized) <= limit:
        return normalized

    return normalized[:limit].rstrip(" ,.;:-") + "…"


def query_words(query: str) -> list[str]:
    """Выделяет слова из пользовательского запроса."""
    return re.findall(
        r"[0-9A-Za-zА-Яа-яЁё№/-]+",
        query,
    )


def meaningful_query(query: str) -> str:
    """
    Убирает вопросные слова из длинных естественных вопросов.

    Например:
    'Какие документы нужны для комиссии'
    превращается примерно в:
    'документы для комиссии'.
    """
    original_words = query_words(query)

    meaningful_words = [
        word
        for word in original_words
        if word.casefold() not in QUESTION_STOP_WORDS
    ]

    # Не превращаем запрос в слишком короткий.
    if len(meaningful_words) >= 2:
        return " ".join(meaningful_words)

    return query.strip()


def get_first_text(
    source: dict[str, Any],
) -> str:
    """Получает основной текст документа."""
    for field_name in (
        "text",
        "content",
        "body",
        "description",
    ):
        value = source.get(field_name)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def is_generic_title(title: str) -> bool:
    """Определяет служебный заголовок сайта."""
    lowered = normalize_text(title).casefold()

    if not lowered:
        return True

    if technical_filename(lowered):
        return True

    if lowered in {
        "pdf",
        "файл",
        "документ",
    }:
        return True

    has_document_marker = any(
        marker.casefold() in lowered
        for marker in DOCUMENT_MARKERS
    )
    has_generic_part = any(
        part in lowered
        for part in GENERIC_TITLE_PARTS
    )

    if has_generic_part and not has_document_marker:
        return True

    if re.search(r"\d", lowered) or has_document_marker:
        return False

    return has_generic_part


def title_from_text(text: str) -> str:
    """
    Пытается сформировать полезный заголовок из текста документа.

    Например, вместо общего заголовка сайта возвращает:
    'Постановление администрации города Сочи от ... № ...'
    """
    normalized = normalize_text(text)

    if not normalized:
        return ""

    lowered = normalized.casefold()

    marker_positions: list[int] = []

    for marker in DOCUMENT_MARKERS:
        position = lowered.find(marker.casefold())

        if position >= 0:
            marker_positions.append(position)

    if not marker_positions:
        return truncate(normalized, 180)

    start = min(marker_positions)
    candidate = normalized[start : start + 320]

    # Стараемся закончить заголовок после закрывающей кавычки.
    quote_end_positions = [
        position
        for position in (
            candidate.find("»"),
            candidate.find('"', 20),
        )
        if position >= 40
    ]

    if quote_end_positions:
        end = min(quote_end_positions) + 1
        return truncate(candidate[:end], 240)

    # Иначе заканчиваем на границе предложения.
    sentence_ends = [
        position
        for position in (
            candidate.find(". ", 80),
            candidate.find("; ", 80),
        )
        if position >= 80
    ]

    if sentence_ends:
        end = min(sentence_ends) + 1
        return truncate(candidate[:end], 240)

    return truncate(candidate, 240)


RUSSIAN_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


# Здесь были ещё восемь конструкций: normalize_optional_filter,
# exact_text_filter, normalize_document_number, document_number_filter,
# date_filter, url_prefix_filter, content_category_filter и список
# SEARCH_REQUIRED_SOURCE_FIELDS — около трёхсот строк.
#
# Все они собирали запрос к прежней схеме индекса. С переходом на
# sochi_docs_v3 сборка переехала в app_v2/search_query.py, а эти остались
# лежать рядом: вторые реализации тех же функций, местами с другим
# поведением. Ни одна не вызывалась из рабочего кода — только из тестов,
# которые проверяли сами себя.

def format_document_date(value: str) -> str:
    """
    Переводит 2024-03-14 в «14 марта 2024».

    В заголовке результата ISO-дата выглядит инородно: пользователь ищет
    «постановление от 14 марта 2024», а не «от 2024-03-14».
    """

    raw = normalize_text(str(value or ""))
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)

    if not match:
        return raw

    year, month, day = (int(part) for part in match.groups())

    if not 1 <= month <= 12:
        return raw

    return (
        f"{day} {RUSSIAN_MONTHS_GENITIVE[month - 1]} {year}"
    )


def get_title(
    source: dict[str, Any],
    text: str,
    *,
    text_is_garbled: bool | None = None,
) -> str:
    """Возвращает наиболее полезный заголовок результата."""
    raw_title = source.get("title")

    title = (
        raw_title.strip()
        if isinstance(raw_title, str)
        else ""
    )

    attachment_raw = source.get(
        "attachment_title"
    )
    attachment_title = (
        attachment_raw.strip()
        if isinstance(
            attachment_raw,
            str,
        )
        else ""
    )

    if (
        attachment_title
        and not is_generic_title(
            attachment_title
        )
    ):
        return truncate(
            attachment_title,
            240,
        )

    if title and not is_generic_title(title):
        return truncate(title, 240)

    document_kind = normalize_text(
        str(
            source.get("document_kind")
            or ""
        )
    )
    document_number = normalize_text(
        str(
            source.get("document_number")
            or ""
        )
    )
    document_date = format_document_date(
        source.get("document_date") or ""
    )

    if document_kind and (
        document_number
        or document_date
    ):
        parts = [document_kind]

        if document_number:
            parts.append(
                f"№ {document_number}"
            )

        if document_date:
            parts.append(
                f"от {document_date}"
            )

        return truncate(
            " ".join(parts),
            240,
        )

    garbled = (
        looks_like_garbled_text(
            text,
            minimum_length=80,
        )
        if text_is_garbled is None
        else text_is_garbled
    )

    derived_title = (
        ""
        if garbled
        else title_from_text(text)
    )

    if derived_title:
        return derived_title

    section = source.get("section")

    if isinstance(section, str) and section.strip():
        return truncate(section, 240)

    if title:
        return truncate(title, 240)

    if attachment_title:
        return truncate(
            attachment_title,
            240,
        )

    return "Без названия"


def get_url(
    source: dict[str, Any],
) -> str:
    """Возвращает адрес исходного документа или страницы."""
    for field_name in (
        "url",
        "file_url",
        "page_url",
        "source_url",
        "link",
    ):
        value = source.get(field_name)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def make_snippet(
    text: str,
    query: str,
    limit: int = 520,
) -> str:
    """
    Формирует фрагмент вокруг точного запроса или одного из его слов.

    Это устраняет начало вида:
    'т развития конкуренции...'
    """
    normalized = normalize_text(text)

    if not normalized:
        return ""

    lowered_text = normalized.casefold()
    normalized_query = normalize_text(query)
    lowered_query = normalized_query.casefold()

    position = lowered_text.find(lowered_query)
    match_length = len(normalized_query)

    if position < 0:
        search_words = sorted(
            {
                word.casefold()
                for word in query_words(
                    meaningful_query(query)
                )
                if len(word) >= 3
            },
            key=len,
            reverse=True,
        )

        for word in search_words:
            position = lowered_text.find(word)

            if position >= 0:
                match_length = len(word)
                break

    if position < 0:
        return truncate(normalized, limit)

    left_limit = max(0, position - 170)
    right_limit = min(
        len(normalized),
        position + match_length + 280,
    )

    # Ищем нормальное начало предложения.
    sentence_start = left_limit

    for separator in (". ", "! ", "? ", "; ", "» "):
        found = normalized.rfind(
            separator,
            left_limit,
            position,
        )

        if found >= 0:
            sentence_start = max(
                sentence_start,
                found + len(separator),
            )

    # Ищем нормальное окончание предложения.
    sentence_end = right_limit
    possible_ends: list[int] = []

    for separator in (". ", "! ", "? ", "; "):
        found = normalized.find(
            separator,
            position + match_length,
            min(len(normalized), right_limit + 150),
        )

        if found >= 0:
            possible_ends.append(found + 1)

    if possible_ends:
        sentence_end = min(possible_ends)

    result = normalized[
        sentence_start:sentence_end
    ].strip(" ,.;:-")

    if sentence_start > 0:
        result = "…" + result

    if sentence_end < len(normalized):
        result += "…"

    return truncate(result, limit)
















# Построение запроса вынесено в отдельный модуль: старая версия искала по
# полям content, body и headings, которых нет ни в индексе, ни в записи
# crawler, и складывала баллы одиннадцати пересекающихся клауз.
from .search_query import (  # noqa: E402
    SEARCH_FACET_CATEGORIES,
    build_fuzzy_search_body,
    build_search_body,
    parse_query,
)


def snippet_query(query: str) -> str:
    """
    Запрос без служебных знаков — для поиска места в тексте.

    Фрагмент вокруг совпадения ищется обычным `str.find`, и кавычки с
    минусами в строке ему только мешают: «состав семьи» в ёлочках в тексте
    документа не встречается никогда. Берём содержимое кавычек и обычные
    слова, исключения выбрасываем.
    """

    parsed = parse_query(query)
    parts = [phrase for phrase in parsed.phrases]

    if parsed.text:
        parts.append(parsed.text)

    return " ".join(parts).strip() or normalize_text(query)

SEARCH_FACET_LABELS = {
    "all": "Все",
    "news": "Новости",
    "legal": "Нормативные акты",
    "announcement": "Объявления",
    "media": "Медиагалерея",
    "file": "Файлы и PDF",
}


def response_hits(
    response: dict[str, Any],
) -> list[Any]:
    hits_section = response.get("hits") or {}
    raw_hits = hits_section.get("hits") or []

    return (
        raw_hits
        if isinstance(raw_hits, list)
        else []
    )

def normalize_total(
    total_value: Any,
) -> int:
    if isinstance(total_value, int):
        return total_value

    if isinstance(total_value, dict):
        value = total_value.get("value", 0)

        if isinstance(value, int):
            return value

    return 0


def clean_site_text(
    value: Any,
    *,
    limit: int,
    remove_site_prefix: bool = True,
) -> str:
    if value is None:
        return ""

    cleaned = remove_html_tags(
        str(value)
    )

    cleaned = " ".join(
        cleaned.split()
    ).strip()

    if remove_site_prefix:
        prefixes = (
            "Администрация города Сочи - ",
            "Администрация города Сочи — ",
            "Администрация города Сочи – ",
            "Администрация города Сочи: ",
        )

        for prefix in prefixes:
            if cleaned.startswith(prefix):
                cleaned = cleaned[
                    len(prefix):
                ].strip()
                break

    navigation_markers = (
        " Новости Объявления СМИ о Сочи",
        " Объявления СМИ о Сочи",
        " СМИ о Сочи Конкурсы и инициативы",
        " О городе Общая информация Туризм",
        (
            " Городская власть "
            "Администрация города Деятельность"
        ),
        " Тематика: Архив",
        " Перейти к обсуждению",
        " Действия: Поделиться",
    )

    marker_positions = [
        cleaned.find(marker)
        for marker in navigation_markers
        if cleaned.find(marker) >= 0
    ]

    if marker_positions:
        cleaned = cleaned[
            :min(marker_positions)
        ].strip()

    return truncate(
        cleaned,
        limit,
    )


def content_category_from_url(
    url: str,
) -> tuple[str, str]:
    normalized = (
        url.lower()
        if url
        else ""
    )

    if "/press-sluzhba/novosti/" in normalized:
        return "news", "Новости"

    if (
        "/gorodskaya-vlast/"
        "normativno-pravovyye-akty/"
    ) in normalized:
        return (
            "legal",
            "Нормативные акты",
        )

    if (
        "/press-sluzhba/obyavleniya/"
        in normalized
    ):
        return (
            "announcement",
            "Объявления",
        )

    if any(
        path in normalized
        for path in (
            "/press-sluzhba/mediagalereya",
            "/press-sluzhba/mediagalareya",
        )
    ):
        return (
            "media",
            "Медиагалерея",
        )

    if "/press-sluzhba/" in normalized:
        return (
            "press",
            "Пресс-служба",
        )

    if "/gorodskaya-vlast/" in normalized:
        return (
            "city_authority",
            "Городская власть",
        )

    if "/zhizn-goroda/" in normalized:
        return (
            "city_life",
            "Жизнь города",
        )

    if "/afisha-goroda/" in normalized:
        return (
            "events",
            "Афиша города",
        )

    if (
        "/kalendar-meropriyatiy/"
        in normalized
    ):
        return (
            "events",
            "Календарь мероприятий",
        )

    if "/gorod/" in normalized:
        return (
            "city",
            "О городе",
        )

    if "/upload/" in normalized:
        return (
            "file",
            "Файлы",
        )

    if (
        "/content/" in normalized
        or "/contents/" in normalized
        or "/strucrure_sochi/" in normalized
    ):
        return (
            "legacy",
            "Архивные разделы",
        )

    return (
        "page",
        "Страница сайта",
    )


def repair_snippet_start(
    snippet: str,
    title: str,
) -> str:
    if not snippet or not title:
        return snippet

    if snippet.startswith(title):
        return snippet

    maximum_offset = min(
        25,
        len(title),
    )

    for offset in range(
        1,
        maximum_offset,
    ):
        title_suffix = title[offset:]

        if (
            title_suffix
            and snippet.startswith(
                title_suffix
            )
        ):
            return (
                title[:offset]
                + snippet
            )

    return snippet


def remove_repeated_title(
    snippet: str,
    title: str,
) -> str:
    if not snippet or not title:
        return snippet

    if not snippet.startswith(title):
        return snippet

    remainder = snippet[
        len(title):
    ].lstrip(
        " -–—:;,.\n\t"
    )

    repeated_position = remainder.find(
        title
    )

    if (
        repeated_position >= 0
        and repeated_position <= 100
    ):
        after_repeated = remainder[
            repeated_position
            + len(title):
        ].lstrip(
            " -–—:;,.\n\t"
        )

        if len(after_repeated) >= 40:
            return after_repeated

    if len(remainder) >= 80:
        return remainder

    return snippet


def clean_result_snippet(
    text: str,
    query: str,
    title: str,
    highlighted_text: str,
) -> str:
    if highlighted_text:
        candidate = clean_site_text(
            highlighted_text,
            limit=650,
        )
    else:
        candidate = clean_site_text(
            make_snippet(
                text,
                query,
                limit=650,
            ),
            limit=650,
        )

    candidate = repair_snippet_start(
        candidate,
        title,
    )

    candidate = remove_repeated_title(
        candidate,
        title,
    )

    candidate = clean_site_text(
        candidate,
        limit=520,
    )

    if len(candidate) >= 40:
        return candidate

    fallback = clean_site_text(
        text,
        limit=520,
    )

    fallback = repair_snippet_start(
        fallback,
        title,
    )

    fallback = remove_repeated_title(
        fallback,
        title,
    )

    return clean_site_text(
        fallback,
        limit=520,
    )


def normalize_hit(
    hit: dict[str, Any],
    query: str,
) -> dict[str, Any]:
    source = hit.get("_source") or {}
    highlight = hit.get("highlight") or {}

    text = get_first_text(source)
    url = get_url(source) or ""

    # Оценка качества текста перебирает все символы фрагмента и стоит
    # заметно. Считаем один раз на результат и передаём дальше.
    text_is_garbled = looks_like_garbled_text(
        text,
        minimum_length=80,
    )

    title = clean_site_text(
        get_title(
            source,
            text,
            text_is_garbled=text_is_garbled,
        ),
        limit=360,
    )

    text_highlight = ""

    text_fragments = highlight.get(
        "text"
    )

    if isinstance(
        text_fragments,
        list,
    ):
        for fragment in text_fragments:
            if isinstance(fragment, str):
                text_highlight = fragment
                break

    snippet = clean_result_snippet(
        text,
        query,
        title,
        text_highlight,
    )

    doc_type = source.get(
        "doc_type"
    )

    if doc_type == "pdf" and text_is_garbled:
        snippet = (
            "Текст этого PDF требует повторного "
            "распознавания. Документ можно открыть "
            "по ссылке."
        )

    (
        fallback_category_code,
        fallback_category_label,
    ) = content_category_from_url(
        url
    )

    category_code = str(
        source.get("content_category")
        or fallback_category_code
    )

    category_label = str(
        source.get("site_section")
        or fallback_category_label
    )

    raw_section = clean_site_text(
        source.get("section"),
        limit=240,
    )

    if category_code in {
        "news",
        "announcement",
        "media",
        "press",
        "events",
        "file",
        "legacy",
    }:
        display_section = category_label

    elif (
        not raw_section
        or raw_section == title
    ):
        display_section = category_label

    else:
        display_section = raw_section

    document_type = (
        doc_type
        or source.get("content_type")
    )

    return {
        "id": hit.get("_id"),
        "score": hit.get("_score"),

        "title": title,
        "title_html": "",

        "url": url,
        "page_url": source.get(
            "page_url"
        ),
        "file_url": source.get(
            "file_url"
        ),

        "snippet": snippet,

        # Клиент уже безопасно подсвечивает
        # текст самостоятельно.
        "snippet_html": "",

        "section": display_section,
        "content_category": category_code,
        "content_category_label": (
            category_label
        ),

        "site_section": (
            source.get("site_section")
            or category_label
        ),

        "doc_type": doc_type,
        "document_type": document_type,

        "document_number": source.get(
            "document_number"
        ),
        "document_date": source.get(
            "document_date"
        ),
        "published_at": source.get(
            "published_at"
        ),
        "sort_date": source.get(
            "sort_date"
        ),
        "document_kind": source.get(
            "document_kind"
        ),

        "parent_url": source.get(
            "parent_url"
        ),
        "attachment_title": source.get(
            "attachment_title"
        ),
        "is_attachment": source.get(
            "is_attachment"
        ),

        "content_type": source.get(
            "content_type"
        ),
        "source": source.get(
            "source"
        ),

        "page_number": source.get(
            "page_number"
        ),
        "chunk_number": source.get(
            "chunk_number"
        ),

        "updated_at": source.get(
            "updated_at"
        ),
        "fetched_at": source.get(
            "fetched_at"
        ),

        "extraction_version": source.get(
            "extraction_version"
        ),
        "text_extraction": source.get(
            "text_extraction"
        ),
        "ocr_used": bool(
            source.get("ocr_used")
        ),
        "source_page_count": source.get(
            "source_page_count"
        ),
        "ocr_page_count": source.get(
            "ocr_page_count"
        ),
        "ocr_attempted_page_count": source.get(
            "ocr_attempted_page_count"
        ),
        "ocr_rejected_page_count": source.get(
            "ocr_rejected_page_count"
        ),
        "unreadable_page_count": source.get(
            "unreadable_page_count"
        ),
    }


def search_documents(
    query: str,
    *,
    limit: int,
    offset: int,
    document_number: str | None = None,
    document_kind: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    doc_type: str | None = None,
    is_attachment: bool | None = None,
    content_category: str | None = None,
    sort: str = "relevance",
) -> dict[str, Any]:
    body = build_search_body(
        query,
        limit=limit,
        offset=offset,
        document_number=document_number,
        document_kind=document_kind,
        date_from=date_from,
        date_to=date_to,
        doc_type=doc_type,
        is_attachment=is_attachment,
        content_category=content_category,
        sort=sort,
    )

    exact_response = es_client.search(body)
    response = exact_response
    fallback_response: dict[str, Any] | None = None
    fallback_attempted = False
    fallback_applied = False

    if (
        offset == 0
        and bool(normalize_text(query))
        and not response_hits(exact_response)
    ):
        fallback_attempted = True

        fuzzy_body = build_fuzzy_search_body(
            query,
            limit=limit,
            offset=offset,
            document_number=document_number,
            document_kind=document_kind,
            date_from=date_from,
            date_to=date_to,
            doc_type=doc_type,
            is_attachment=is_attachment,
            content_category=content_category,
            sort=sort,
        )

        fuzzy_response = es_client.search(
            fuzzy_body
        )
        fallback_response = fuzzy_response

        if response_hits(fuzzy_response):
            response = fuzzy_response
            fallback_applied = True

    hits_section = response.get(
        "hits"
    ) or {}

    raw_hits = hits_section.get(
        "hits"
    ) or []

    items: list[
        dict[str, Any]
    ] = []

    used_urls: set[str] = set()

    # Кавычки и минус-слова нужны построителю запроса, но не поиску места
    # в тексте фрагмента.
    fragment_query = snippet_query(query)

    for raw_hit in raw_hits:
        if not isinstance(
            raw_hit,
            dict,
        ):
            continue

        item = normalize_hit(
            raw_hit,
            fragment_query,
        )

        url = str(
            item.get("url") or ""
        )

        deduplication_key = (
            url
            or str(
                item.get("id") or ""
            )
        )

        if deduplication_key in used_urls:
            continue

        used_urls.add(
            deduplication_key
        )

        items.append(item)

    raw_total = normalize_total(
        hits_section.get("total")
    )

    aggregations = response.get(
        "aggregations"
    ) or {}

    unique_aggregation = (
        aggregations.get(
            "unique_documents"
        )
        or {}
    )

    all_unique_total = (
        unique_aggregation.get(
            "value"
        )
    )

    if not isinstance(
        all_unique_total,
        (int, float),
    ):
        all_unique_total = raw_total

    all_unique_total = int(
        all_unique_total
    )

    selected_unique_total = (
        all_unique_total
    )

    category_aggregation = (
        aggregations.get(
            "category_facets"
        )
        or {}
    )

    raw_buckets = (
        category_aggregation.get(
            "buckets"
        )
        or []
    )

    # `terms` отдаёт список корзин с ключом в поле `key`, а прежняя
    # `filters`-агрегация отдавала словарь с именованными корзинами.
    # Приводим оба вида к словарю, чтобы разбор ниже не зависел от формы.
    if isinstance(raw_buckets, dict):
        category_buckets = raw_buckets
    else:
        category_buckets = {
            str(bucket.get("key")): bucket
            for bucket in raw_buckets
            if isinstance(bucket, dict)
        }

    if content_category:
        chosen = category_buckets.get(
            str(content_category).strip().casefold()
        ) or {}
        chosen_value = (
            (chosen.get("unique_documents") or {}).get("value")
        )

        if isinstance(chosen_value, (int, float)):
            selected_unique_total = int(chosen_value)
        else:
            selected_unique_total = len(items)

    facets: list[
        dict[str, Any]
    ] = [
        {
            "code": "all",
            "label": (
                SEARCH_FACET_LABELS["all"]
            ),
            "count": all_unique_total,
            "selected": (
                not content_category
            ),
        }
    ]

    for category in (
        SEARCH_FACET_CATEGORIES
    ):
        bucket = (
            category_buckets.get(
                category
            )
            or {}
        )

        cardinality = (
            bucket.get(
                "unique_documents"
            )
            or {}
        )

        count = cardinality.get(
            "value",
            0,
        )

        if not isinstance(
            count,
            (int, float),
        ):
            count = 0

        facets.append(
            {
                "code": category,
                "label": (
                    SEARCH_FACET_LABELS[
                        category
                    ]
                ),
                "count": int(count),
                "selected": (
                    content_category
                    == category
                ),
            }
        )

    active_filters: dict[
        str,
        Any,
    ] = {}

    if document_number:
        active_filters[
            "document_number"
        ] = document_number

    if document_kind:
        active_filters[
            "document_kind"
        ] = document_kind

    if date_from:
        active_filters[
            "date_from"
        ] = date_from

    if date_to:
        active_filters[
            "date_to"
        ] = date_to

    if doc_type:
        active_filters[
            "doc_type"
        ] = doc_type

    if is_attachment is not None:
        active_filters[
            "is_attachment"
        ] = is_attachment

    if content_category:
        active_filters[
            "content_category"
        ] = content_category

    return {
        "query": query,

        "normalized_query": (
            meaningful_query(query)
            if query
            else ""
        ),

        "filters": active_filters,
        "sort": sort,

        "total": selected_unique_total,
        "all_categories_total": (
            all_unique_total
        ),

        "raw_chunks_total": raw_total,

        "facets": facets,

        "limit": limit,
        "offset": offset,
        "items": items,

        "took_ms": sum(
            value
            for value in (
                exact_response.get("took"),
                (
                    fallback_response.get("took")
                    if fallback_response is not None
                    else None
                ),
            )
            if isinstance(value, (int, float))
        ),

        "match_mode": (
            "fuzzy"
            if fallback_applied
            else "exact"
        ),

        "fallback_attempted": fallback_attempted,
        "fallback_applied": fallback_applied,

        "timed_out": bool(
            exact_response.get("timed_out", False)
            or (
                fallback_response.get("timed_out", False)
                if fallback_response is not None
                else False
            )
        ),
    }


def build_extractive_answer(
    search_result: dict[str, Any],
) -> dict[str, Any]:
    """
    Возвращает честный поисковый ответ.

    Пока нейросеть не подключена, endpoint не утверждает,
    что самостоятельно сформировал юридический вывод.
    """
    items = search_result.get("items") or []

    notice = (
        "Краткий ответ составлен автоматически из найденных "
        "фрагментов. Для точной формулировки и применения "
        "информации откройте первоисточник."
    )

    base_response = {
        "query": search_result.get("query"),
        "normalized_query": search_result.get(
            "normalized_query"
        ),
        "filters": search_result.get("filters") or {},
        "mode": "extractive",
        "notice": notice,
    }

    if not items:
        return {
            **base_response,
            "answer": (
                "В проиндексированных материалах "
                "подходящая информация не найдена."
            ),
            "sources": [],
            "source_count": 0,
            "total": 0,
        }

    answer_excerpts: list[str] = []
    used_excerpts: set[str] = set()

    sources: list[dict[str, Any]] = []

    for item in items[:5]:
        title = str(
            item.get("title")
            or "Без названия"
        )

        snippet = str(
            item.get("snippet")
            or ""
        )

        normalized_excerpt = normalize_text(
            snippet
        ).casefold()

        if (
            snippet
            and normalized_excerpt
            not in used_excerpts
        ):
            used_excerpts.add(
                normalized_excerpt
            )
            answer_excerpts.append(
                truncate(snippet, 340)
            )

        sources.append(
            {
                "title": title,
                "url": item.get("url"),
                "page_number": item.get(
                    "page_number"
                ),
                "document_type": item.get(
                    "document_type"
                ),
                "document_number": item.get(
                    "document_number"
                ),
                "document_kind": item.get(
                    "document_kind"
                ),
                "document_date": item.get(
                    "document_date"
                ),
                "published_at": item.get(
                    "published_at"
                ),
                "section": item.get(
                    "section"
                ),
                "snippet": snippet,
                "score": item.get("score"),
            }
        )

    if answer_excerpts:
        answer_text = (
            "По наиболее релевантным материалам:\n\n"
            + "\n\n".join(
                f"• {excerpt}"
                for excerpt in answer_excerpts
            )
        )
    else:
        answer_text = (
            "Материалы найдены, но подходящие "
            "текстовые фрагменты отсутствуют."
        )

    return {
        **base_response,
        "answer": answer_text,
        "sources": sources,
        "source_count": len(sources),
        "total": search_result.get(
            "total",
            len(sources),
        ),
    }
