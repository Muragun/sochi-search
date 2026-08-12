from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from elasticsearch.helpers import bulk

from .crawler import crawl_site, canonicalize_url
from .es import get_es
from .parser import chunk_text, extract_document_bytes
from .settings import INDEX_NAME, REQUEST_TIMEOUT, USER_AGENT


SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def make_id(*parts: str) -> str:
    import hashlib
    raw = "|".join(p or "" for p in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def fetch_binary(url: str) -> tuple[bytes, str]:
    resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content, (resp.headers.get("content-type") or "").lower()


def build_actions():
    records = crawl_site()

    for rec in records:
        kind = rec["kind"]
        source = rec.get("source", "sochi.ru")
        page_url = canonicalize_url(rec.get("page_url", "") or "")
        file_url = canonicalize_url(rec.get("file_url", "") or "")
        title = rec.get("title") or file_url.split("/")[-1] or page_url
        section = rec.get("section") or title
        updated_at = rec.get("updated_at") or now_iso()

        if kind == "page":
            text = rec.get("text", "")
            if not text:
                continue

            for chunk_no, chunk in enumerate(chunk_text(text), start=1):
                content_hash = sha256_text(chunk)
                doc_id = make_id(source, "page", page_url, str(chunk_no))

                yield {
                    "_op_type": "index",
                    "_index": INDEX_NAME,
                    "_id": doc_id,
                    "_source": {
                        "title": title,
                        "section": section,
                        "text": chunk,
                        "source": source,
                        "doc_type": "page",
                        "page_url": page_url,
                        "file_url": "",
                        "url": page_url,
                        "page_number": 1,
                        "chunk_number": chunk_no,
                        "hash": content_hash,
                        "content_type": rec.get("content_type", "text/html"),
                        "fetched_at": now_iso(),
                        "updated_at": updated_at,
                    },
                }

        elif kind == "file":
            if not file_url:
                continue

            try:
                data, content_type = fetch_binary(file_url)
            except Exception as e:
                print(f"[ERROR] download failed: {file_url} -> {e}")
                continue

            filename = Path(urlparse(file_url).path).name or file_url

            try:
                pages = extract_document_bytes(filename, content_type, data)
            except Exception as e:
                print(f"[ERROR] parse failed: {file_url} -> {e}")
                continue

            if not pages:
                continue

            for page in pages:
                page_number = int(page.get("page_number") or 1)
                section_name = page.get("section") or section

                for chunk_no, chunk in enumerate(chunk_text(page["text"]), start=1):
                    if not chunk:
                        continue

                    content_hash = sha256_text(chunk)
                    doc_id = make_id(source, "file", page_url, file_url, str(page_number), str(chunk_no))

                    yield {
                        "_op_type": "index",
                        "_index": INDEX_NAME,
                        "_id": doc_id,
                        "_source": {
                            "title": title,
                            "section": section_name,
                            "text": chunk,
                            "source": source,
                            "doc_type": rec.get("doc_type", "file"),
                            "page_url": page_url,
                            "file_url": file_url,
                            "url": file_url,
                            "page_number": page_number,
                            "chunk_number": chunk_no,
                            "hash": content_hash,
                            "content_type": content_type or rec.get("content_type", ""),
                            "fetched_at": now_iso(),
                            "updated_at": updated_at,
                        },
                    }


def run_index() -> dict:
    es = get_es()
    actions = build_actions()

    try:
        success_count, errors = bulk(
            client=es,
            actions=actions,
            chunk_size=100,
            request_timeout=120,
            raise_on_error=False,
            stats_only=False,
        )
    except Exception as e:
        return {"indexed": 0, "failed": 1, "error": str(e)}

    es.indices.refresh(index=INDEX_NAME)

    return {
        "indexed": success_count,
        "failed": len(errors) if errors else 0,
    }


if __name__ == "__main__":
    result = run_index()
    print(result)
