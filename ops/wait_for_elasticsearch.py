from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
import time
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


READY_STATUSES = {"yellow", "green"}
DEFAULT_URL = "http://127.0.0.1:9200"


@dataclass(frozen=True)
class ProbeResult:
    ready: bool
    detail: str
    status: Optional[str] = None


@dataclass(frozen=True)
class WaitResult:
    ready: bool
    attempts: int
    detail: str
    status: Optional[str] = None


def configured_url(explicit_url: Optional[str] = None) -> str:
    value = (
        explicit_url
        or os.getenv("ES_URL")
        or os.getenv("ELASTICSEARCH_URL")
        or DEFAULT_URL
    ).strip().rstrip("/")
    parsed = urlsplit(value)

    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Некорректный порт Elasticsearch") from error

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Некорректный URL Elasticsearch")

    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("URL Elasticsearch должен указывать на корень сервиса")

    return value


def display_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""

    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"

    if parsed.port is not None:
        host += ":" + str(parsed.port)

    return urlunsplit((parsed.scheme, host, "", "", ""))


def health_url(base_url: str) -> str:
    query = urlencode(
        {
            "wait_for_status": "yellow",
            "timeout": "5s",
            "filter_path": "cluster_name,status,timed_out",
        }
    )
    return base_url.rstrip("/") + "/_cluster/health?" + query


def probe_elasticsearch(
    base_url: str,
    request_timeout: float,
) -> ProbeResult:
    request = Request(
        health_url(base_url),
        headers={"User-Agent": "sochi-search-readiness/2.6.0"},
        method="GET",
    )

    try:
        with urlopen(request, timeout=request_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
        return ProbeResult(
            ready=False,
            detail=f"{type(error).__name__}: {error}",
        )

    if not isinstance(payload, dict):
        return ProbeResult(
            ready=False,
            detail="Elasticsearch вернул не JSON-объект",
        )

    status = str(payload.get("status") or "").strip().lower()
    timed_out = payload.get("timed_out") is True
    ready = status in READY_STATUSES and not timed_out
    detail = (
        f"status={status or 'unknown'}; "
        f"timed_out={str(timed_out).lower()}"
    )
    return ProbeResult(
        ready=ready,
        detail=detail,
        status=status or None,
    )


def wait_for_elasticsearch(
    *,
    base_url: str,
    timeout: float,
    interval: float,
    request_timeout: float,
    probe: Callable[[str, float], ProbeResult] = probe_elasticsearch,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> WaitResult:
    if timeout < 0 or interval <= 0 or request_timeout <= 0:
        raise ValueError("Параметры ожидания должны быть положительными")

    deadline = monotonic() + timeout
    attempts = 0
    last = ProbeResult(ready=False, detail="проверка не выполнялась")

    while True:
        attempts += 1
        last = probe(base_url, request_timeout)

        if last.ready:
            return WaitResult(
                ready=True,
                attempts=attempts,
                detail=last.detail,
                status=last.status,
            )

        remaining = deadline - monotonic()

        if remaining <= 0:
            return WaitResult(
                ready=False,
                attempts=attempts,
                detail=last.detail,
                status=last.status,
            )

        sleep(min(interval, remaining))


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ожидает готовности Elasticsearch не ниже yellow",
    )
    parser.add_argument("--url")
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--request-timeout", type=float, default=8.0)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    try:
        base_url = configured_url(arguments.url)
        print(
            "ELASTICSEARCH_WAIT_START",
            f"url={display_url(base_url)}",
            f"timeout={arguments.timeout:g}",
            flush=True,
        )
        result = wait_for_elasticsearch(
            base_url=base_url,
            timeout=arguments.timeout,
            interval=arguments.interval,
            request_timeout=arguments.request_timeout,
        )
    except ValueError as error:
        print(f"ELASTICSEARCH_WAIT_ERROR={error}", file=sys.stderr, flush=True)
        return 64

    if not result.ready:
        print(
            "ELASTICSEARCH_READY=0",
            f"attempts={result.attempts}",
            result.detail,
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(
        "ELASTICSEARCH_READY=1",
        f"status={result.status}",
        f"attempts={result.attempts}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
