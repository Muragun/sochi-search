from __future__ import annotations

import argparse

from .es import get_es
from .settings import INDEX_NAME


INDEX_BODY = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "title": {
                "type": "text",
                "analyzer": "russian",
                "fields": {
                    "raw": {"type": "keyword", "ignore_above": 512}
                },
            },
            "section": {
                "type": "text",
                "analyzer": "russian",
                "fields": {
                    "raw": {"type": "keyword", "ignore_above": 512}
                },
            },
            "text": {"type": "text", "analyzer": "russian"},
            "source": {"type": "keyword"},
            "doc_type": {"type": "keyword"},
            "page_url": {"type": "keyword"},
            "file_url": {"type": "keyword"},
            "url": {"type": "keyword"},
            "page_number": {"type": "integer"},
            "chunk_number": {"type": "integer"},
            "hash": {"type": "keyword"},
            "content_type": {"type": "keyword"},
            "fetched_at": {"type": "date"},
            "updated_at": {"type": "date"},
        }
    },
}


def create_index(reset: bool = False) -> None:
    es = get_es()

    if reset:
        es.indices.delete(index=INDEX_NAME, ignore_unavailable=True)
        print(f"Deleted index if it existed: {INDEX_NAME}")
    else:
        try:
            if es.indices.exists(index=INDEX_NAME):
                print(f"Index already exists: {INDEX_NAME}")
                return
        except Exception as e:
            print(f"Could not check index existence, will try to create anyway: {e}")

    es.indices.create(index=INDEX_NAME, **INDEX_BODY)
    print(f"Created index: {INDEX_NAME}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="recreate the index")
    args = parser.parse_args()

    create_index(reset=args.reset)
