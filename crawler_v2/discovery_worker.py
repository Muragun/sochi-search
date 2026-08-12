from __future__ import annotations

import argparse
import gzip
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import settings as crawler_settings
from .discovery_config import (
    ALLOWED_HOSTS,
    CONNECT_TIMEOUT,
    MAX_DOWNLOAD_BYTES,
    READ_TIMEOUT,
    USER_AGENT,
    settings,
)
from .discovery_db import DiscoveryDatabase
from .url_policy import (
    SUPPORTED_FILE_SUFFIXES,
    is_legacy_detail_url,
)


IGNORED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
    ".webm",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".xml",
    ".rss",
}

SUPPORTED_EXTENSIONS = {
    "",
    ".php",
    ".html",
    ".htm",
    ".asp",
    ".aspx",
    ".cgi",
} | set(SUPPORTED_FILE_SUFFIXES)

FILE_CONTENT_TYPES = {
    ".csv": "text/csv",
    ".docx": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ),
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet"
    ),
}

IGNORED_QUERY_PARAMETERS = {
    "gclid",
    "yclid",
    "fbclid",
    "_openstat",
}

IGNORED_PATH_PREFIXES = (
    "/bitrix/",
    "/local/",
)


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def host_allowed(
    hostname: str,
) -> bool:
    lowered = hostname.lower()

    for allowed_host in ALLOWED_HOSTS:
        if (
            lowered == allowed_host
            or lowered.endswith(
                "." + allowed_host
            )
        ):
            return True

    return False


def canonicalize_url(
    raw_url: str,
) -> str | None:
    try:
        parsed = urlsplit(
            raw_url.strip()
        )
    except ValueError:
        return None

    if parsed.scheme.lower() not in {
        "http",
        "https",
    }:
        return None

    hostname = (
        parsed.hostname
        or ""
    ).lower()

    if not hostname or not host_allowed(
        hostname
    ):
        return None

    if hostname == (
        "www." + settings.canonical_host
    ):
        hostname = settings.canonical_host

    path = parsed.path or "/"

    path = re.sub(
        r"/{2,}",
        "/",
        path,
    )

    if is_legacy_detail_url(
        urlunsplit(("", "", path, parsed.query, ""))
    ):
        return None

    lowered_path = path.lower()

    if any(
        lowered_path.startswith(prefix)
        for prefix in IGNORED_PATH_PREFIXES
    ):
        return None

    suffix = PurePosixPath(
        lowered_path
    ).suffix

    if suffix in IGNORED_EXTENSIONS:
        return None

    if (
        suffix
        and suffix not in SUPPORTED_EXTENSIONS
    ):
        return None

    query_items = []

    for key, value in parse_qsl(
        parsed.query,
        keep_blank_values=True,
    ):
        lowered_key = key.lower()

        if (
            lowered_key.startswith("utm_")
            or lowered_key
            in IGNORED_QUERY_PARAMETERS
        ):
            continue

        query_items.append(
            (
                key,
                value,
            )
        )

    query_items.sort()

    normalized_query = urlencode(
        query_items,
        doseq=True,
    )

    return urlunsplit(
        (
            settings.canonical_scheme,
            hostname,
            path,
            normalized_query,
            "",
        )
    )


def local_name(tag: str) -> str:
    return tag.rsplit(
        "}",
        maxsplit=1,
    )[-1].lower()


def child_text(
    element: ElementTree.Element,
    name: str,
) -> str | None:
    for child in element:
        if local_name(child.tag) != name:
            continue

        if child.text:
            value = child.text.strip()

            if value:
                return value

    return None


class DiscoveryHttpClient:
    def __init__(self) -> None:
        self.session = requests.Session()

        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            backoff_factor=1,
            status_forcelist=(
                429,
                500,
                502,
                503,
                504,
            ),
            allowed_methods=frozenset(
                {
                    "GET",
                }
            ),
            respect_retry_after_header=True,
            raise_on_status=False,
        )

        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=5,
            pool_maxsize=5,
        )

        self.session.mount(
            "http://",
            adapter,
        )
        self.session.mount(
            "https://",
            adapter,
        )

        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "application/xml,text/xml,"
                    "text/plain,*/*;q=0.5"
                ),
            }
        )

    def get(
        self,
        url: str,
    ) -> requests.Response:
        response = self.session.get(
            url,
            timeout=(
                CONNECT_TIMEOUT,
                READ_TIMEOUT,
            ),
            allow_redirects=True,
        )

        if len(response.content) > (
            MAX_DOWNLOAD_BYTES
        ):
            response.close()

            raise RuntimeError(
                "Ответ превышает допустимый "
                f"размер: {url}"
            )

        return response


def load_robots(
    client: DiscoveryHttpClient,
) -> tuple[
    RobotFileParser | None,
    list[str],
    str,
]:
    robots_url = urljoin(
        settings.root_url,
        "/robots.txt",
    )

    try:
        response = client.get(
            robots_url
        )
    except Exception as exc:
        print(
            f"[WARN] robots.txt: {exc}",
            file=sys.stderr,
        )

        return (
            None,
            [],
            robots_url,
        )

    try:
        if response.status_code >= 400:
            print(
                "[WARN] robots.txt вернул "
                f"HTTP {response.status_code}",
                file=sys.stderr,
            )

            return (
                None,
                [],
                robots_url,
            )

        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(
            response.text.splitlines()
        )

        sitemap_urls = (
            parser.site_maps()
            or []
        )

        return (
            parser,
            list(sitemap_urls),
            robots_url,
        )

    finally:
        response.close()


def parse_sitemap(
    body: bytes,
    *,
    sitemap_url: str,
) -> tuple[
    list[str],
    list[dict[str, str | None]],
]:
    if (
        sitemap_url.lower().endswith(".gz")
        or body.startswith(b"\x1f\x8b")
    ):
        body = gzip.decompress(body)

    root = ElementTree.fromstring(
        body
    )

    root_type = local_name(
        root.tag
    )

    nested_sitemaps: list[str] = []
    page_urls: list[
        dict[str, str | None]
    ] = []

    if root_type == "sitemapindex":
        for item in root:
            if local_name(
                item.tag
            ) != "sitemap":
                continue

            location = child_text(
                item,
                "loc",
            )

            if location:
                nested_sitemaps.append(
                    location
                )

        return (
            nested_sitemaps,
            page_urls,
        )

    if root_type != "urlset":
        raise RuntimeError(
            "Неизвестный корневой элемент "
            f"Sitemap: {root_type}"
        )

    for item in root:
        if local_name(
            item.tag
        ) != "url":
            continue

        location = child_text(
            item,
            "loc",
        )

        if not location:
            continue

        page_urls.append(
            {
                "url": location,
                "lastmod": child_text(
                    item,
                    "lastmod",
                ),
            }
        )

    return (
        nested_sitemaps,
        page_urls,
    )


def discover_from_sitemaps(
    client: DiscoveryHttpClient,
    *,
    seed_sitemaps: list[str],
    robots_parser: RobotFileParser | None,
) -> tuple[
    dict[str, dict[str, Any]],
    int,
    int,
]:
    queue = deque(seed_sitemaps)
    visited_sitemaps: set[str] = set()

    candidates: dict[
        str,
        dict[str, Any],
    ] = {}

    errors = 0

    while (
        queue
        and len(visited_sitemaps)
        < settings.max_sitemaps
        and len(candidates)
        < settings.max_candidates
    ):
        sitemap_url = queue.popleft()

        if sitemap_url in visited_sitemaps:
            continue

        visited_sitemaps.add(
            sitemap_url
        )

        try:
            response = client.get(
                sitemap_url
            )

            try:
                if response.status_code >= 400:
                    raise RuntimeError(
                        "HTTP "
                        f"{response.status_code}"
                    )

                nested, page_urls = (
                    parse_sitemap(
                        response.content,
                        sitemap_url=(
                            response.url
                        ),
                    )
                )

            finally:
                response.close()

        except Exception as exc:
            errors += 1

            print(
                "[WARN] Sitemap "
                f"{sitemap_url}: {exc}",
                file=sys.stderr,
            )

            continue

        print(
            "[SITEMAP] "
            f"{sitemap_url}: "
            f"nested={len(nested)} "
            f"urls={len(page_urls)}"
        )

        for nested_url in nested:
            parsed = urlsplit(
                nested_url
            )

            hostname = (
                parsed.hostname
                or ""
            )

            if (
                parsed.scheme in {
                    "http",
                    "https",
                }
                and host_allowed(
                    hostname
                )
            ):
                queue.append(
                    nested_url
                )

        for item in page_urls:
            original_url = str(
                item["url"]
            )

            if (
                robots_parser is not None
                and not robots_parser.can_fetch(
                    USER_AGENT,
                    original_url,
                )
            ):
                continue

            canonical_url = (
                canonicalize_url(
                    original_url
                )
            )

            if canonical_url is None:
                continue

            suffix = PurePosixPath(
                urlsplit(
                    canonical_url
                ).path.lower()
            ).suffix

            if suffix == ".pdf":
                doc_type = "pdf"
            elif suffix in SUPPORTED_FILE_SUFFIXES:
                doc_type = "file"
            else:
                doc_type = "page"

            candidates[canonical_url] = {
                "url": canonical_url,

                "page_url": (
                    canonical_url
                    if doc_type == "page"
                    else None
                ),

                "file_url": (
                    canonical_url
                    if doc_type in {"pdf", "file"}
                    else None
                ),

                "doc_type": doc_type,

                "content_type": (
                    FILE_CONTENT_TYPES.get(suffix)
                ),

                "discovered_from": (
                    sitemap_url
                ),

                "sitemap_lastmod": (
                    item.get(
                        "lastmod"
                    )
                ),
            }

            if (
                len(candidates)
                >= settings.max_candidates
            ):
                break

    return (
        candidates,
        len(visited_sitemaps),
        errors,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Обнаружение новых URL "
            "через robots.txt и Sitemap"
        )
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Ничего не записывать "
            "в SQLite"
        ),
    )

    parser.add_argument(
        "--max-new",
        type=int,
        default=settings.max_new_urls,
        help=(
            "Максимум новых URL "
            "за один запуск"
        ),
    )

    parser.add_argument(
        "--sitemap",
        action="append",
        default=[],
        help=(
            "Дополнительный Sitemap URL"
        ),
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    if arguments.max_new < 1:
        print(
            "--max-new должен быть больше нуля",
            file=sys.stderr,
        )
        return 2

    database = DiscoveryDatabase(
        crawler_settings.state_db_path
    )

    database.migrate()

    client = DiscoveryHttpClient()

    robots_parser, robots_sitemaps, (
        robots_url
    ) = load_robots(client)

    seed_sitemaps = list(
        dict.fromkeys(
            [
                *arguments.sitemap,
                *robots_sitemaps,
                *settings.default_sitemaps,
            ]
        )
    )

    print(
        "[ROBOTS] "
        f"url={robots_url} "
        f"sitemaps={len(robots_sitemaps)}"
    )

    print(
        "[START] Sitemap seeds:"
    )

    for sitemap_url in seed_sitemaps:
        print(f"  {sitemap_url}")

    candidates, sitemaps_seen, (
        sitemap_errors
    ) = discover_from_sitemaps(
        client,
        seed_sitemaps=seed_sitemaps,
        robots_parser=robots_parser,
    )

    if not candidates:
        print(
            "[ERROR] Ни одного URL "
            "из Sitemap не обнаружено",
            file=sys.stderr,
        )
        return 1

    known = database.known_urls(
        candidates.keys()
    )

    new_rows = [
        row
        for url, row in candidates.items()
        if url not in known
    ]

    selected_rows = new_rows[
        :arguments.max_new
    ]

    print()
    print("=" * 90)
    print(
        "Sitemap обработано:",
        sitemaps_seen,
    )
    print(
        "Ошибок Sitemap:",
        sitemap_errors,
    )
    print(
        "Кандидатов:",
        len(candidates),
    )
    print(
        "Уже известных:",
        len(known),
    )
    print(
        "Новых найдено:",
        len(new_rows),
    )
    print(
        "Выбрано для добавления:",
        len(selected_rows),
    )

    print()
    print("Первые новые URL:")

    for row in selected_rows[:30]:
        print(
            f"[{row['doc_type']}] "
            f"{row['url']}"
        )

    if arguments.dry_run:
        print()
        print(
            "[DRY RUN] SQLite не изменена"
        )
        return 0

    started_at = utc_now()

    run_id = database.create_run(
        started_at=started_at,
        robots_url=robots_url,
    )

    try:
        inserted, existing = (
            database.insert_discovered(
                selected_rows,
                seen_at=started_at,
            )
        )

        database.finish_run(
            run_id,
            finished_at=utc_now(),
            status="success",
            sitemaps_seen=sitemaps_seen,
            sitemap_errors=sitemap_errors,
            candidates_seen=len(
                candidates
            ),
            known_urls=len(known),
            new_urls_found=len(
                new_rows
            ),
            inserted_urls=inserted,
            message=(
                f"Existing during insert: "
                f"{existing}"
            ),
        )

    except Exception as exc:
        database.finish_run(
            run_id,
            finished_at=utc_now(),
            status="failed",
            sitemaps_seen=sitemaps_seen,
            sitemap_errors=sitemap_errors,
            candidates_seen=len(
                candidates
            ),
            known_urls=len(known),
            new_urls_found=len(
                new_rows
            ),
            inserted_urls=0,
            message=str(exc),
        )

        raise

    print()
    print(
        "[OK] Добавлено новых URL:",
        inserted,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
