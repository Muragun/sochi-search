from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .parser import extract_html_page
from .settings import CRAWL_LIMIT, CRAWL_RETRIES, CRAWL_RETRY_BACKOFF, REQUEST_TIMEOUT, SITEMAP_URL, USER_AGENT, CRAWL_DELAY


LOG_DIR = Path("/opt/sochi-search/data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
CRAWL_ERRORS_LOG = LOG_DIR / "crawl_errors.log"


def log_error(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {message}\n"
    with CRAWL_ERRORS_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def is_allowed_sochi_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "sochi.ru" or host.endswith(".sochi.ru")


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    })

    retry = Retry(
        total=CRAWL_RETRIES,
        connect=CRAWL_RETRIES,
        read=CRAWL_RETRIES,
        status=CRAWL_RETRIES,
        backoff_factor=CRAWL_RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


SESSION = _make_session()


def fetch(url: str) -> requests.Response:
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        raise requests.RequestException(f"{url}: {e}") from e

    if resp.status_code >= 400:
        raise requests.HTTPError(f"{url}: HTTP {resp.status_code}", response=resp)

    return resp


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap_xml(xml_bytes: bytes) -> tuple[list[str], list[str]]:
    root = ET.fromstring(xml_bytes)
    name = _local_name(root.tag)

    sitemap_urls: list[str] = []
    page_urls: list[str] = []

    if name == "sitemapindex":
        for loc in root.findall(".//{*}sitemap/{*}loc"):
            if loc.text:
                sitemap_urls.append(loc.text.strip())
    elif name == "urlset":
        for loc in root.findall(".//{*}url/{*}loc"):
            if loc.text:
                page_urls.append(loc.text.strip())

    return sitemap_urls, page_urls


def collect_urls_from_sitemap(start_sitemap_url: str = SITEMAP_URL, limit: int = CRAWL_LIMIT) -> list[str]:
    seen_sitemaps: set[str] = set()
    seen_urls: set[str] = set()
    result: list[str] = []

    queue = [start_sitemap_url]

    while queue and len(result) < limit:
        sitemap_url = canonicalize_url(queue.pop(0))
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)

        try:
            resp = fetch(sitemap_url)
        except Exception as e:
            log_error(f"[SITEMAP SKIP] {sitemap_url} -> {e}")
            continue

        try:
            sitemap_urls, page_urls = parse_sitemap_xml(resp.content)
        except Exception as e:
            log_error(f"[SITEMAP PARSE SKIP] {sitemap_url} -> {e}")
            continue

        for sm in sitemap_urls:
            sm = canonicalize_url(sm)
            if sm not in seen_sitemaps:
                queue.append(sm)

        for url in page_urls:
            url = canonicalize_url(url)
            if not is_allowed_sochi_url(url):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            result.append(url)
            if len(result) >= limit:
                break

    return result


def extract_pdf_links(html: str, base_url: str) -> list[str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []

    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        href = canonicalize_url(href)
        if not is_allowed_sochi_url(href):
            continue
        if ".pdf" in href.lower() or href.lower().endswith(".pdf"):
            links.append(href)

    return sorted(set(links))


def crawl_url(url: str) -> list[dict]:
    url = canonicalize_url(url)

    if not is_allowed_sochi_url(url):
        return []

    try:
        resp = fetch(url)
    except Exception as e:
        log_error(f"[SKIP] {url} -> {e}")
        return []

    content_type = (resp.headers.get("content-type") or "").lower()
    records: list[dict] = []

    if "pdf" in content_type or url.lower().endswith(".pdf"):
        records.append({
            "kind": "file",
            "source": "sochi.ru",
            "page_url": url,
            "file_url": url,
            "title": url.split("/")[-1] or url,
            "doc_type": "pdf",
            "content_type": content_type or "application/pdf",
            "updated_at": now_iso(),
        })
        return records

    if "html" in content_type or "<html" in resp.text[:500].lower():
        page = extract_html_page(resp.text, url)
        records.append({
            "kind": "page",
            "source": "sochi.ru",
            "page_url": url,
            "file_url": "",
            "title": page["title"],
            "section": page["title"],
            "text": page["text"],
            "doc_type": "page",
            "content_type": content_type or "text/html",
            "updated_at": now_iso(),
        })

        for pdf_url in extract_pdf_links(resp.text, url):
            records.append({
                "kind": "file",
                "source": "sochi.ru",
                "page_url": url,
                "file_url": pdf_url,
                "title": page["title"],
                "section": page["title"],
                "doc_type": "pdf",
                "content_type": "application/pdf",
                "updated_at": now_iso(),
            })

        return records

    records.append({
        "kind": "file",
        "source": "sochi.ru",
        "page_url": url,
        "file_url": url,
        "title": url.split("/")[-1] or url,
        "section": url.split("/")[-1] or url,
        "doc_type": "file",
        "content_type": content_type,
        "updated_at": now_iso(),
    })
    return records


def crawl_site(limit: int = CRAWL_LIMIT) -> list[dict]:
    try:
        urls = collect_urls_from_sitemap(SITEMAP_URL, limit=limit)
    except Exception as e:
        log_error(f"[FATAL SITEMAP] {e}")
        return []

    all_records: list[dict] = []

    for i, url in enumerate(urls, start=1):
        print(f"[{i}/{len(urls)}] {url}")
        try:
            all_records.extend(crawl_url(url))
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log_error(f"[CRAWL ERROR] {url} -> {e}")
        time.sleep(CRAWL_DELAY)

    return all_records
