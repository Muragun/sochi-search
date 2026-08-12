"""
Построение запроса к Elasticsearch для индекса `sochi_docs_v3`.

Заменяет прежнюю конструкцию из одиннадцати `should`-веток. Проблемы прежней
схемы:

- баллы одиннадцати пересекающихся клауз складывались, поэтому документ,
  случайно совпавший сразу в нескольких полях, обгонял точное совпадение;
- `multi_match` перечислял поля `content`, `body`, `headings`, которых нет ни
  в mapping-данных, ни в записи crawler;
- подсветка запрашивалась по тем же несуществующим полям с `no_match_size`,
  из-за чего highlighter отрабатывал на каждом попадании впустую;
- фильтр рубрики строился как набор `prefix` по `url` — по пять хостов на
  каждый путь, и это повторялось для каждой из пяти фасет-корзин;
- `cardinality` с `precision_threshold` 40000 считалась семь раз за запрос.

Новая схема: одна обязательная часть (`must`) определяет попадание, набор
`should` только повышает уже найденное.

`rescore` намеренно не используется: вместе с `collapse` он пересчитывает
баллы до схлопывания, и порядок документов на первой странице перестаёт
соответствовать порядку внутри группы. Точные совпадения вместо этого
повышаются обычными `should`-клаузами по подполям `.exact`.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

DASHES = "‐‑‒–—―−"

SEARCH_FACET_CATEGORIES = (
    "news",
    "legal",
    "announcement",
    "media",
    "file",
)

# Точность 3000 даёт ошибку около 1 % при 10 тысячах документов и занимает
# примерно в тринадцать раз меньше памяти, чем прежние 40000.
CARDINALITY_PRECISION = 3000

# Поля и веса основного запроса.
#
# Список задан здесь, а не в настройках: он привязан к схеме v3, где у
# `title` и `text` есть подполя `.exact`, а у `document_kind` — подполе
# `.text`. Значение из .env выглядело бы как настройка, но при опечатке
# молча ломало бы поиск, а при переходе на другую схему — расходилось бы
# с ней. Прежняя настройка SEARCH_FIELDS перечисляла поля `content`,
# `body` и `headings`, которых обходчик никогда не заполнял.
QUERY_FIELDS = (
    "title^4",
    "attachment_title^3",
    "text",
    "section^0.5",
    "document_kind.text^2",
)

# Опечатки в запросе ищутся по меньшему набору: `section` и вид документа
# короткие, и нечёткое совпадение по ним даёт шум.
FUZZY_FIELDS = (
    "title^4",
    "attachment_title^3",
    "text",
)

DOCUMENT_NUMBER_RE = re.compile(
    r"(?:№|N[оo]?\.?)\s*\d{1,7}(?:\s*[-/]\s*[A-Za-zА-Яа-яЁё0-9]+)?"
    r"|\b\d{1,7}\s*-\s*[а-яё]{1,4}\b",
    flags=re.IGNORECASE,
)


def normalize_document_number(value: str) -> str:
    """Приводит «№ 512 – Р», «N512-P» и «512р» к одному виду."""

    normalized = str(value or "").strip()
    normalized = normalized.replace(" ", " ")

    for dash in DASHES:
        normalized = normalized.replace(dash, "-")

    normalized = re.sub(
        r"^\s*(?:№|N[оo]?\.?)\s*",
        "",
        normalized,
        flags=re.IGNORECASE,
    )

    # Латинские близнецы кириллических букв в номерах актов.
    table = str.maketrans(
        {
            "p": "р", "P": "р", "c": "с", "C": "с",
            "a": "а", "A": "а", "o": "о", "O": "о",
            "e": "е", "E": "е", "x": "х", "X": "х",
            "y": "у", "Y": "у", "k": "к", "K": "к",
            "m": "м", "M": "м", "t": "т", "T": "т",
            "h": "н", "H": "н", "b": "в", "B": "в",
        }
    )
    normalized = normalized.translate(table)

    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized)

    return normalized.strip().casefold()


def looks_like_document_number(query: str) -> bool:
    return bool(DOCUMENT_NUMBER_RE.search(query or ""))


def document_number_clauses(value: str, *, boost: float) -> list[dict[str, Any]]:
    normalized = normalize_document_number(value)

    if not normalized:
        return []

    compact = normalized.replace("-", "")

    return [
        {
            "term": {
                "document_number": {
                    "value": normalized,
                    "boost": boost,
                }
            }
        },
        {
            "match": {
                "document_number.text": {
                    "query": compact,
                    "operator": "and",
                    "boost": boost * 0.6,
                }
            }
        },
    ]


def date_filter(
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    """
    Диапазон по единой дате сортировки.

    Прежний вариант перебирал `sort_date`, `document_date` и `published_at`
    через `should`: документ попадал в выборку, если хотя бы одно из полей
    входило в диапазон, поэтому акт 2019 года с датой публикации 2024 года
    находился по обоим годам. `sort_date` заполняется как
    `document_date or published_at`, и одного поля достаточно.
    """

    date_range: dict[str, str] = {}

    if date_from:
        date_range["gte"] = date_from

    if date_to:
        date_range["lte"] = date_to

    return {
        "range": {
            "sort_date": date_range,
        }
    }


def category_filter(category: str) -> dict[str, Any]:
    """
    Фильтр рубрики по одному keyword-полю.

    `content_category` заполняется для каждого документа при индексации, в том
    числе при переносе старых записей. Перебор `prefix` по `url` больше не
    нужен.
    """

    normalized = str(category or "").strip().casefold()

    return {
        "term": {
            "content_category": normalized,
        }
    }


def build_filters(
    *,
    document_number: str | None = None,
    document_kind: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    doc_type: str | None = None,
    is_attachment: bool | None = None,
) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []

    if document_number:
        normalized = normalize_document_number(document_number)

        if normalized:
            filters.append(
                {
                    "bool": {
                        "should": document_number_clauses(
                            document_number,
                            boost=1.0,
                        ),
                        "minimum_should_match": 1,
                    }
                }
            )

    if document_kind:
        filters.append(
            {
                "term": {
                    "document_kind": str(document_kind).strip().casefold(),
                }
            }
        )

    if date_from or date_to:
        filters.append(date_filter(date_from, date_to))

    if doc_type:
        filters.append({"term": {"doc_type": doc_type}})

    if is_attachment is not None:
        filters.append({"term": {"is_attachment": bool(is_attachment)}})

    return filters


def build_query(query: str, filters: list[dict[str, Any]]) -> dict[str, Any]:
    raw_query = (query or "").strip()

    if not raw_query:
        return (
            {"bool": {"filter": filters}}
            if filters
            else {"match_all": {}}
        )

    # Обязательная часть: слова запроса должны найтись хотя бы в одном из
    # содержательных полей. `cross_fields` считает поля одним текстом, поэтому
    # запрос «постановление 512 субсидии» находит документ, у которого номер
    # лежит в одном поле, а слова — в другом.
    must: dict[str, Any] = {
        "multi_match": {
            "query": raw_query,
            "type": "cross_fields",
            "fields": list(QUERY_FIELDS),

            # «3<75%» читается так: до трёх слов включительно нужны все,
            # свыше трёх — три четверти.
            #
            # Просто «75%» здесь не годится: Elasticsearch округляет долю
            # вниз, и на коротком запросе она вырождается. У «постановление
            # о благоустройстве» после удаления стоп-слова остаётся два
            # слова, 75% от двух — это одно, и достаточно совпадения по
            # слову «постановление». На рабочем индексе такой запрос давал
            # 10277 документов, в верхних результатах благоустройства не
            # было. У «512-р» — два токена, «512» и «р», и хватало любого
            # из них: 2460 документов.
            "minimum_should_match": "3<75%",
        }
    }

    should: list[dict[str, Any]] = [
        # Точная форма слова важнее лемматизированной.
        {
            "match_phrase": {
                "title.exact": {
                    "query": raw_query,
                    "boost": 12,
                    "slop": 1,
                }
            }
        },
        {
            "match_phrase": {
                "text.exact": {
                    "query": raw_query,
                    "boost": 6,
                    "slop": 2,
                }
            }
        },
        {
            "match": {
                "title": {
                    "query": raw_query,
                    "operator": "and",
                    "boost": 4,
                }
            }
        },
    ]

    if looks_like_document_number(raw_query):
        should.extend(
            document_number_clauses(raw_query, boost=40)
        )

    bool_query: dict[str, Any] = {
        "must": [must],
        "should": should,
    }

    if filters:
        bool_query["filter"] = filters

    return {"bool": bool_query}


def build_aggregations(
    *,
    selected_category: str | None,
) -> dict[str, Any]:
    aggregations: dict[str, Any] = {
        "unique_documents": {
            "cardinality": {
                "field": "url",
                "precision_threshold": CARDINALITY_PRECISION,
            }
        },

        # Одна terms-агрегация вместо пяти filters-корзин с вложенной
        # cardinality: считается за один проход по значениям keyword-поля.
        "category_facets": {
            "terms": {
                "field": "content_category",
                "size": 32,
            },
            "aggs": {
                "unique_documents": {
                    "cardinality": {
                        "field": "url",
                        "precision_threshold": CARDINALITY_PRECISION,
                    }
                }
            },
        },
    }

    return aggregations


def build_search_body(
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
    highlight: bool = True,
) -> dict[str, Any]:
    raw_query = (query or "").strip()

    filters = build_filters(
        document_number=document_number,
        document_kind=document_kind,
        date_from=date_from,
        date_to=date_to,
        doc_type=doc_type,
        is_attachment=is_attachment,
    )

    selected = (
        str(content_category).strip().casefold()
        if content_category
        else None
    )

    # Рубрика применяется как `post_filter`, а не как обычный фильтр.
    # Обычный фильтр сузил бы и агрегации, и счётчики остальных вкладок
    # обнулились бы. `post_filter` сужает только выдачу.

    body: dict[str, Any] = {
        "from": offset,
        "size": limit,

        # Точное число фрагментов пользователю не показывается: в интерфейсе
        # выводится число документов из `unique_documents`. Считать все
        # совпадения на каждый запрос не нужно.
        "track_total_hits": 10000,

        "_source": [
            "url",
            "page_url",
            "file_url",
            "parent_url",
            "title",
            "section",
            # Раздел сайта, записанный обходчиком. Без него карточка
            # показывает вычисленную рубрику: она грубее и на страницах
            # с нестандартным адресом расходится с тем, что человек
            # видит на самом сайте.
            "site_section",
            "attachment_title",
            "text",
            "doc_type",
            "content_category",
            "page_number",
            "chunk_number",
            "published_at",
            "sort_date",
            "document_number",
            "document_date",
            "document_kind",
            "is_attachment",
            "text_extraction",
            "ocr_used",
        ],

        "query": build_query(raw_query, filters),

        "collapse": {
            "field": "url",
        },

        "aggs": build_aggregations(selected_category=selected),
    }

    if selected:
        body["post_filter"] = category_filter(selected)

    if raw_query:
        # Подсветка только по тем полям, которые действительно заполняются.
        # `text` размечен как `index_options: offsets`, поэтому unified
        # highlighter берёт позиции из индекса и не переразбирает фрагмент.
        body["highlight"] = {
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "encoder": "html",
            "fields": {
                "text": {
                    "fragment_size": 320,
                    "number_of_fragments": 2,
                    "no_match_size": 220,
                },
                "title": {
                    "number_of_fragments": 0,
                },
            },
        }

    # `rescore` здесь намеренно не используется: Elasticsearch не гарантирует
    # его совместимость с `collapse`, а схлопывание по `url` нужно, чтобы одна
    # страница не занимала всю выдачу своими фрагментами. Роль дополнительного
    # прохода играют `match_phrase` по `*.exact` в списке `should`.

    if sort == "date_desc":
        body["track_scores"] = True
        body["sort"] = [
            {"sort_date": {"order": "desc", "missing": "_last"}},
            {"_score": {"order": "desc"}},
        ]

    elif sort == "date_asc":
        body["track_scores"] = True
        body["sort"] = [
            {"sort_date": {"order": "asc", "missing": "_last"}},
            {"_score": {"order": "desc"}},
        ]

    elif not raw_query:
        body["sort"] = [
            {"sort_date": {"order": "desc", "missing": "_last"}}
        ]

    return body


def build_fuzzy_search_body(
    query: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    Осторожный резервный запрос на случай опечатки.

    Выполняется только после нулевого результата точного поиска, поэтому
    обычную релевантность и точные номера актов не смещает.
    """

    body = build_search_body(query, **kwargs)
    raw_query = (query or "").strip()

    if not raw_query:
        return body

    previous = body.get("query", {}).get("bool", {})

    fuzzy: dict[str, Any] = {
        "must": [
            {
                "multi_match": {
                    "query": raw_query,
                    "type": "best_fields",
                    "fields": list(FUZZY_FIELDS),
                    "fuzziness": "AUTO",
                    "prefix_length": 1,
                    "max_expansions": 24,
                    "minimum_should_match": "65%",
                }
            }
        ],
    }

    if previous.get("filter"):
        fuzzy["filter"] = previous["filter"]

    body["query"] = {"bool": fuzzy}

    return body
