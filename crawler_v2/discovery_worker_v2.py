from __future__ import annotations

import argparse
import re
import time
from collections import Counter, deque
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from crawler_v2.config import settings as crawler_settings
from crawler_v2.discovery_config import USER_AGENT, settings
from crawler_v2.discovery_db import DiscoveryDatabase
from crawler_v2.url_policy import (
    SUPPORTED_FILE_SUFFIXES,
    is_legacy_detail_url,
)
from crawler_v2.discovery_worker import (
    DiscoveryHttpClient,
    discover_from_sitemaps,
    load_robots,
    utc_now,
)


HTML_SUFFIXES = {"", ".php", ".html", ".htm", ".asp", ".aspx", ".cgi"}
INDEXABLE_SUFFIXES = HTML_SUFFIXES | set(SUPPORTED_FILE_SUFFIXES)
IGNORED_PATH_PREFIXES = ("/bitrix/", "/local/")
IGNORED_SCHEMES = ("tel:", "mailto:", "javascript:", "data:")
IGNORED_QUERY_PARAMETERS = {"gclid", "yclid", "fbclid", "_openstat"}
ALLOWED_QUERY_PARAMETERS = {"element_id", "section_id", "id", "year", "page"}
MODULE_VERSION = "2.5.0"
DEFAULT_MAX_PAGINATION_PAGE = settings.max_pagination_page
FETCH_PATH_OVERRIDES = {
    "/press-sluzhba/": "/press-sluzhba/novosti/",
    "/gorod/": "/gorod/obshchaya-informatsiya/gorod-sobytiye/",
    "/gorodskaya-vlast/": "/gorodskaya-vlast/administratsiya-goroda/structure/",
}
DEFAULT_SEEDS = (
    "https://sochi.ru/",
    "https://sochi.ru/press-sluzhba/",
    "https://sochi.ru/press-sluzhba/novosti/",
    "https://sochi.ru/press-sluzhba/obyavleniya/",
    "https://sochi.ru/gorod/",
    "https://sochi.ru/gorodskaya-vlast/",
    "https://sochi.ru/gorodskaya-vlast/normativno-pravovyye-akty/",
    "https://sochi.ru/gorodskaya-vlast/deyatelnost/protivodeystvie-korruptsii/",
    "https://sochi.ru/gorodskaya-vlast/deyatelnost/protivodeystvie-korruptsii/antikor-ekspert/",
)


def normalize_path(path: str) -> str:
    trailing_slash = path.endswith("/")
    parts: list[str] = []
    for part in re.sub(r"/{2,}", "/", path or "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    normalized = "/" + "/".join(parts)
    if trailing_slash and normalized != "/":
        normalized += "/"
    return normalized


def fetch_url(url: str) -> str:
    parsed = urlsplit(url)
    path = normalize_path(parsed.path)
    path = FETCH_PATH_OVERRIDES.get(path, path)
    return urlunsplit(("https", settings.canonical_host, path, parsed.query, ""))


def is_detail_path(path: str, query_items: list[tuple[str, str]]) -> bool:
    keys = {key.lower() for key, _ in query_items}
    if "element_id" in keys:
        return True
    normalized = path.rstrip("/").lower()
    if re.search(r"/press-sluzhba/novosti/\d+/\d+$", normalized):
        return True
    if re.search(r"/press-sluzhba/obyavleniya/\d+$", normalized):
        return True
    match = re.search(r"/(\d+)$", normalized)
    return bool(match and int(match.group(1)) >= 10000)


def canonicalize_html_url(raw_url: str, *, base_url: str | None = None) -> str | None:
    value = raw_url.strip()
    if not value or value.startswith("#") or value.lower().startswith(IGNORED_SCHEMES):
        return None
    absolute = urljoin(base_url or settings.root_url, value)
    try:
        parsed = urlsplit(absolute)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    hostname = (parsed.hostname or "").lower()
    allowed = {settings.canonical_host, "www." + settings.canonical_host}
    if hostname not in allowed:
        return None
    path = normalize_path(parsed.path)

    if is_legacy_detail_url(
        urlunsplit(("", "", path, parsed.query, ""))
    ):
        return None

    lowered_path = path.lower()
    if any(lowered_path.startswith(prefix) for prefix in IGNORED_PATH_PREFIXES):
        return None
    if re.match(r"^/(?:https?|ftp):/", lowered_path):
        return None
    if re.search(r"(?:^|/)(?:tel|mailto|javascript|data):", lowered_path):
        return None
    suffix = PurePosixPath(lowered_path).suffix
    if suffix not in INDEXABLE_SUFFIXES:
        return None
    raw_query_items = parse_qsl(parsed.query, keep_blank_values=True)
    detail = is_detail_path(path, raw_query_items)
    query_items: list[tuple[str, str]] = []
    for key, item_value in raw_query_items:
        lowered_key = key.lower()
        if lowered_key.startswith("utm_") or lowered_key in IGNORED_QUERY_PARAMETERS:
            continue
        is_pagination = lowered_key.startswith("pagen_") or lowered_key == "page"
        if is_pagination and detail:
            continue
        if not is_pagination and lowered_key not in ALLOWED_QUERY_PARAMETERS:
            continue
        query_items.append((key, item_value))
    query_items.sort(key=lambda item: (item[0].lower(), item[1]))
    return urlunsplit(
        (
            settings.canonical_scheme,
            settings.canonical_host,
            path,
            urlencode(query_items, doseq=True),
            "",
        )
    )


def is_pagination_url(url: str) -> bool:
    return any(
        key.lower().startswith("pagen_") or key.lower() == "page"
        for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)
    )


def pagination_page_number(url: str) -> int | None:
    values: list[int] = []
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        lowered_key = key.lower()
        if not (lowered_key.startswith("pagen_") or lowered_key == "page"):
            continue
        if not value.isdigit():
            return None
        values.append(int(value))
    return max(values) if values else None


def is_html_url(url: str) -> bool:
    return PurePosixPath(urlsplit(url).path.lower()).suffix in HTML_SUFFIXES


def candidate_row(url: str, discovered_from: str) -> dict[str, Any]:
    suffix = PurePosixPath(urlsplit(url).path.lower()).suffix
    is_pdf = suffix == ".pdf"
    is_file = suffix in SUPPORTED_FILE_SUFFIXES and not is_pdf
    content_types = {
        ".csv": "text/csv",
        ".docx": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        ".odt": "application/vnd.oasis.opendocument.text",
        ".ods": "application/vnd.oasis.opendocument.spreadsheet",
        ".pdf": "application/pdf",
        ".rtf": "application/rtf",
        ".txt": "text/plain",
        ".xlsx": (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    }
    return {
        "url": url,
        "page_url": None if is_pdf or is_file else url,
        "file_url": url if is_pdf or is_file else None,
        "source": "html",
        "doc_type": "pdf" if is_pdf else ("file" if is_file else "page"),
        "content_type": content_types.get(suffix),
        "discovered_from": discovered_from,
        "sitemap_lastmod": None,
    }


def discover_from_html(
    client: DiscoveryHttpClient,
    *,
    robots_parser: Any,
    seeds: Iterable[str],
    max_pages: int,
    max_depth: int,
    max_pagination_page: int,
    delay: float,
) -> tuple[dict[str, dict[str, Any]], int, list[str], dict[str, int]]:
    queue: deque[tuple[int, str]] = deque()
    queued: set[str] = set()
    fetched: set[str] = set()
    requested_urls: set[str] = set()
    candidates: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    pagination_queued: set[str] = set()
    pagination_limited: set[str] = set()
    detail_pages_not_fetched: set[str] = set()

    def enqueue(url: str, depth: int) -> None:
        if url in queued or url in fetched or not is_html_url(url):
            return
        parsed = urlsplit(url)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        if is_detail_path(parsed.path, query_items):
            detail_pages_not_fetched.add(url)
            return
        if is_pagination_url(url):
            page_number = pagination_page_number(url)
            if page_number is None or page_number < 2 or page_number > max_pagination_page:
                pagination_limited.add(url)
                return
            pagination_queued.add(url)
        queued.add(url)
        queue.append((depth, url))

    for seed in seeds:
        canonical = canonicalize_html_url(seed)
        if canonical:
            candidates.setdefault(canonical, candidate_row(canonical, "html-seed"))
            enqueue(canonical, 0)

    while queue and len(fetched) < max_pages and len(candidates) < settings.max_candidates:
        depth, page_url = queue.popleft()
        queued.discard(page_url)
        if page_url in fetched:
            continue
        request_url = fetch_url(page_url)
        if request_url in requested_urls:
            fetched.add(page_url)
            continue
        if robots_parser is not None and not robots_parser.can_fetch(USER_AGENT, request_url):
            continue
        fetched.add(page_url)
        requested_urls.add(request_url)
        if len(fetched) == 1 or len(fetched) % 100 == 0:
            print(
                "[HTML] загружено страниц:",
                len(fetched),
                "| очередь:",
                len(queue),
                "| кандидатов:",
                len(candidates),
                flush=True,
            )
        try:
            response = client.get(request_url)
            try:
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}")
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" not in content_type:
                    continue
                final_url = str(response.url)
                soup = BeautifulSoup(response.content, "html.parser")
            finally:
                response.close()
        except Exception as error:
            errors.append(f"{page_url} | {type(error).__name__}: {error}")
            continue

        for anchor in soup.find_all("a", href=True):
            canonical = canonicalize_html_url(anchor.get("href", ""), base_url=final_url)
            if canonical is None:
                continue
            pagination = is_pagination_url(canonical)
            if not pagination:
                candidates.setdefault(canonical, candidate_row(canonical, page_url))
            if not is_html_url(canonical):
                continue
            next_depth = depth if pagination else depth + 1
            if pagination or next_depth <= max_depth:
                enqueue(canonical, next_depth)

        if delay > 0:
            time.sleep(delay)

    stats = {
        "pagination_queued": len(pagination_queued),
        "pagination_limited": len(pagination_limited),
        "detail_pages_not_fetched": len(detail_pages_not_fetched),
        "remaining_queue": len(queue),
        "http_requests": len(requested_urls),
    }
    return candidates, len(fetched), errors, stats


def dedupe_key(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    return path.lower(), parsed.query.lower()


def newest_number(url: str) -> int:
    values = [int(value) for value in re.findall(r"\d+", urlsplit(url).path)]
    return max(values, default=0)


def classify_url(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/").lower()
    if PurePosixPath(path).suffix == ".pdf":
        return "pdf"
    if re.search(r"/press-sluzhba/novosti/\d+/\d+$", path):
        return "novosti"
    if re.search(r"/press-sluzhba/obyavleniya/\d+$", path):
        return "obyavleniya"
    if re.search(r"/antikor-ekspert/\d+$", path):
        return "antikor-ekspert"
    if is_detail_path(parsed.path, parse_qsl(parsed.query, keep_blank_values=True)):
        return "drugaya-kartochka"
    return "razdel-ili-stranitsa"


def selection_priority(row: dict[str, Any]) -> tuple[int, int, int, str]:
    """Prefer fresh HTML cards; keep ordinary pages and PDFs behind them."""
    category = classify_url(row["url"])
    if category in {
        "novosti",
        "obyavleniya",
        "antikor-ekspert",
        "drugaya-kartochka",
    }:
        tier = 0
    elif category == "razdel-ili-stranitsa":
        tier = 1
    else:
        tier = 2
    source_tier = 0 if row["source"] == "html" else 1
    return source_tier, tier, -newest_number(row["url"]), row["url"]


def suspicious_reason(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != settings.canonical_scheme:
        return "неверная схема"
    if (parsed.hostname or "").lower() != settings.canonical_host:
        return "чужой хост"
    lowered = url.lower()
    if any(marker in lowered for marker in ("/./", "/../", "tel:", "mailto:", "javascript:")):
        return "служебный или ненормализованный путь"
    if re.match(r"^/(?:https?|ftp):/", parsed.path.lower()):
        return "вложенная схема в пути"
    if is_pagination_url(url):
        return "пагинация попала в кандидаты"
    return None


def print_distribution(label: str, rows: list[dict[str, Any]]) -> None:
    counts = Counter(classify_url(row["url"]) for row in rows)
    print(label + ":")
    if not counts:
        print("  <пусто>")
        return
    for category, amount in sorted(counts.items()):
        print(f"  {category}: {amount}")


def print_samples(rows: list[dict[str, Any]], per_category: int) -> None:
    if per_category < 1:
        return
    grouped: dict[str, list[str]] = {}
    for row in rows:
        category = classify_url(row["url"])
        bucket = grouped.setdefault(category, [])
        if len(bucket) < per_category:
            bucket.append(row["url"])
    print("Примеры отобранных URL:")
    if not grouped:
        print("  <пусто>")
        return
    for category, urls in sorted(grouped.items()):
        print(f"  [{category}]")
        for url in urls:
            print("   ", url)


def insert_rows(database: DiscoveryDatabase, rows: list[dict[str, Any]], seen_at: str) -> int:
    inserted = 0
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            if is_legacy_detail_url(
                str(row["url"])
            ):
                continue

            payload = dict(row)
            payload["seen_at"] = seen_at
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO crawl_urls (
                    url, page_url, file_url, source, doc_type, content_type,
                    status, first_seen_at, last_seen_at, next_check_at,
                    is_active, discovered_at, discovered_from, sitemap_lastmod
                ) VALUES (
                    :url, :page_url, :file_url, :source, :doc_type, :content_type,
                    'discovered', :seen_at, :seen_at, :seen_at,
                    1, :seen_at, :discovered_from, :sitemap_lastmod
                )
                """,
                payload,
            )
            inserted += max(cursor.rowcount, 0)
        connection.commit()
    return inserted


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sitemap + HTML discovery for sochi.ru")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-new", type=int, default=settings.max_new_urls)
    parser.add_argument("--sitemap", action="append", default=[])
    parser.add_argument("--html-max-pages", type=int, default=3000)
    parser.add_argument("--html-max-depth", type=int, default=6)
    parser.add_argument(
        "--html-max-pagination-page",
        type=int,
        default=DEFAULT_MAX_PAGINATION_PAGE,
    )
    parser.add_argument("--report-samples", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.10)
    parser.add_argument("--html-only", action="store_true")
    parser.add_argument("--seed", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if (
        arguments.max_new < 1
        or arguments.html_max_pages < 1
        or arguments.html_max_depth < 0
        or arguments.html_max_pagination_page < 2
        or arguments.report_samples < 0
    ):
        raise SystemExit("Параметры лимитов должны быть положительными")

    started_at = utc_now()
    database = DiscoveryDatabase(crawler_settings.state_db_path)
    client = DiscoveryHttpClient()
    robots_parser, robots_sitemaps, robots_url = load_robots(client)

    sitemap_candidates: dict[str, dict[str, Any]] = {}
    sitemaps_seen = 0
    sitemap_errors = 0
    if not arguments.html_only:
        seed_sitemaps = list(
            dict.fromkeys([*arguments.sitemap, *robots_sitemaps, *settings.default_sitemaps])
        )
        sitemap_candidates, sitemaps_seen, sitemap_errors = discover_from_sitemaps(
            client,
            seed_sitemaps=seed_sitemaps,
            robots_parser=robots_parser,
        )
        for row in sitemap_candidates.values():
            row["source"] = "sitemap"

    html_seeds = list(dict.fromkeys([*arguments.seed, *DEFAULT_SEEDS]))
    print(
        f"[HTML] запуск безопасного обхода v{MODULE_VERSION}:",
        f"max_pages={arguments.html_max_pages}",
        f"max_depth={arguments.html_max_depth}",
        f"max_pagination_page={arguments.html_max_pagination_page}",
        flush=True,
    )
    html_candidates, html_fetched, html_errors, html_stats = discover_from_html(
        client,
        robots_parser=robots_parser,
        seeds=html_seeds,
        max_pages=arguments.html_max_pages,
        max_depth=arguments.html_max_depth,
        max_pagination_page=arguments.html_max_pagination_page,
        delay=max(arguments.delay, 0.0),
    )

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in sitemap_candidates.values():
        by_key.setdefault(dedupe_key(row["url"]), row)
    for row in html_candidates.values():
        by_key[dedupe_key(row["url"])] = row

    with database.connect() as connection:
        existing_urls = [row[0] for row in connection.execute("SELECT url FROM crawl_urls")]
    existing_keys = {dedupe_key(url) for url in existing_urls}
    new_rows = [row for key, row in by_key.items() if key not in existing_keys]
    new_rows.sort(key=selection_priority)
    selected_rows = new_rows[: arguments.max_new]

    print("=== SITEMAP + HTML DISCOVERY ===")
    print("robots.txt:", robots_url)
    print("Sitemap обработано:", sitemaps_seen)
    print("Ошибок sitemap:", sitemap_errors)
    print("HTML загружено:", html_fetched)
    print("Фактических HTTP-запросов:", html_stats["http_requests"])
    print("Ошибок HTML:", len(html_errors))
    print("Пагинаций поставлено в очередь:", html_stats["pagination_queued"])
    print("Дальних пагинаций отсечено:", html_stats["pagination_limited"])
    print("Карточек обнаружено без загрузки:", html_stats["detail_pages_not_fetched"])
    print("Осталось в очереди:", html_stats["remaining_queue"])
    print("Уникальных кандидатов:", len(by_key))
    print("Уже известных:", len(by_key) - len(new_rows))
    print("Новых найдено:", len(new_rows))
    print("Отобрано по лимиту:", len(selected_rows))
    print("Новых HTML:", sum(row["source"] == "html" for row in new_rows))
    print("Новых sitemap:", sum(row["source"] == "sitemap" for row in new_rows))
    print_distribution("Распределение всех новых URL", new_rows)
    print_distribution("Распределение отобранных URL", selected_rows)
    print_samples(selected_rows, arguments.report_samples)
    suspicious = [
        (row["url"], reason)
        for row in selected_rows
        if (reason := suspicious_reason(row["url"])) is not None
    ]
    print("Подозрительных среди отобранных:", len(suspicious))
    for url, reason in suspicious[:20]:
        print(reason, "|", url)
    print("Контрольные URL:")
    for url in (
        "http://sochi.ru/press-sluzhba/novosti/66/296754/",
        "http://sochi.ru/press-sluzhba/obyavleniya/296744/",
        "http://sochi.ru/gorodskaya-vlast/deyatelnost/protivodeystvie-korruptsii/antikor-ekspert/296722/",
    ):
        key = dedupe_key(url)
        if key in by_key:
            status = "НАЙДЕН"
        elif key in existing_keys:
            status = "УЖЕ В SQLITE"
        else:
            status = "НЕ НАЙДЕН"
        print(status, "|", url)
    if html_errors:
        print("Первые ошибки HTML:")
        for error in html_errors[:20]:
            print(error)

    if arguments.dry_run:
        print("[DRY RUN] SQLite и Elasticsearch не изменялись")
        return 0

    database.migrate()
    run_id = database.create_run(started_at=started_at, robots_url=robots_url)
    try:
        inserted = insert_rows(database, selected_rows, started_at)
        database.finish_run(
            run_id,
            finished_at=utc_now(),
            status="ok",
            sitemaps_seen=sitemaps_seen,
            sitemap_errors=sitemap_errors + len(html_errors),
            candidates_seen=len(by_key),
            known_urls=len(by_key) - len(new_rows),
            new_urls_found=len(new_rows),
            inserted_urls=inserted,
            message=f"html_fetched={html_fetched}; html_errors={len(html_errors)}",
        )
    except Exception as error:
        database.finish_run(
            run_id,
            finished_at=utc_now(),
            status="failed",
            sitemaps_seen=sitemaps_seen,
            sitemap_errors=sitemap_errors + len(html_errors),
            candidates_seen=len(by_key),
            known_urls=len(by_key) - len(new_rows),
            new_urls_found=len(new_rows),
            inserted_urls=0,
            message=f"{type(error).__name__}: {error}",
        )
        raise

    print("[OK] Добавлено новых URL:", inserted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
