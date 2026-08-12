from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path("/opt/sochi-search")
APP_DIR = PROJECT_DIR / "app"
DATA_DIR = PROJECT_DIR / "data"
LOG_DIR = DATA_DIR / "logs"

INDEX_NAME = os.getenv("INDEX_NAME", "sochi_docs")

BASE_URL = os.getenv("BASE_URL", "https://sochi.ru").rstrip("/")
SITEMAP_URL = os.getenv("SITEMAP_URL", f"{BASE_URL}/sitemap.xml")

ELASTIC_URL = os.getenv("ELASTIC_URL", "http://127.0.0.1:9200")

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "18083"))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
CRAWL_LIMIT = int(os.getenv("CRAWL_LIMIT", "50000"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "200"))


USER_AGENT = os.getenv(
    "USER_AGENT",
    "SochiKnowledgeBot/1.0 (+internal search)"
)
CRAWL_DELAY = float(os.getenv("CRAWL_DELAY", "0.25"))
CRAWL_RETRIES = int(os.getenv("CRAWL_RETRIES", "2"))
CRAWL_RETRY_BACKOFF = float(os.getenv("CRAWL_RETRY_BACKOFF", "0.7"))
