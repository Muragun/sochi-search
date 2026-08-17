from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .es import get_es, ping_es
from .settings import INDEX_NAME


app = FastAPI(title="Sochi Search API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def search_docs(query: str, limit: int = 10, doc_type: Optional[str] = None) -> Dict[str, Any]:
    es = get_es()

    filters = [{"term": {"source": "sochi.ru"}}]
    if doc_type:
        filters.append({"term": {"doc_type": doc_type}})

    body = {
        "size": limit,
        "_source": [
            "title", "section", "text", "source", "doc_type",
            "page_url", "file_url", "url", "page_number",
            "chunk_number", "updated_at", "content_type"
        ],
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["title^4", "section^2", "text"],
                            "type": "best_fields",
                            "operator": "or"
                        }
                    }
                ],
                "filter": filters
            }
        },
        "highlight": {
            "fields": {
                "text": {
                    "fragment_size": 240,
                    "number_of_fragments": 2
                },
                "title": {},
                "section": {}
            }
        }
    }

    res = es.search(index=INDEX_NAME, body=body)
    hits = res["hits"]["hits"]

    items = []
    for hit in hits:
        src = hit["_source"]
        highlight = hit.get("highlight", {})
        snippet = ""

        if highlight.get("text"):
            snippet = highlight["text"][0]
        elif highlight.get("title"):
            snippet = highlight["title"][0]
        elif highlight.get("section"):
            snippet = highlight["section"][0]
        else:
            snippet = (src.get("text") or "")[:300]

        items.append({
            "id": hit["_id"],
            "score": hit.get("_score", 0),
            "title": src.get("title", ""),
            "section": src.get("section", ""),
            "source": src.get("source", ""),
            "doc_type": src.get("doc_type", ""),
            "page_url": src.get("page_url", ""),
            "file_url": src.get("file_url", ""),
            "url": src.get("url", ""),
            "page_number": src.get("page_number", None),
            "chunk_number": src.get("chunk_number", None),
            "updated_at": src.get("updated_at", ""),
            "snippet": snippet,
        })

    return {
        "query": query,
        "count": len(items),
        "items": items,
    }


def get_doc(doc_id: str) -> Dict[str, Any]:
    es = get_es()
    try:
        res = es.get(index=INDEX_NAME, id=doc_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Документ не найден: {doc_id}") from e

    return {
        "id": res["_id"],
        **res["_source"],
    }




@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "elasticsearch": ping_es(),
        "index": INDEX_NAME,
    }


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(10, ge=1, le=50),
    doc_type: Optional[str] = Query(None),
) -> Dict[str, Any]:
    return search_docs(q, limit=limit, doc_type=doc_type)


@app.get("/doc/{doc_id}")
def doc(doc_id: str) -> Dict[str, Any]:
    return get_doc(doc_id)


@app.get("/answer")
def answer(
    q: str = Query(..., min_length=1),
    limit: int = Query(5, ge=1, le=10),
) -> Dict[str, Any]:
    found = search_docs(q, limit=limit)
    items = found["items"]

    if not items:
        return {
            "query": q,
            "answer": "Ничего не найдено.",
            "sources": [],
            "used_llm": False,
        }

    answer_text = "\n\n".join(
        [
            f"{i+1}. {item['title']}\n"
            f"Страница: {item['page_url']}\n"
            f"Файл: {item['file_url'] or '-'}\n"
            f"Фрагмент: {item['snippet']}"
            for i, item in enumerate(items[:5])
        ]
    )
    used_llm = False

    return {
        "query": q,
        "answer": answer_text,
        "sources": items,
        "used_llm": used_llm,
    }
