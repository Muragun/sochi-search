from __future__ import annotations

import argparse
import re
from typing import Any

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

from .config import settings


CANDIDATE_SELECTORS = (
    "main",
    "article",
    "[role='main']",
    "#content",
    ".content",
    ".page-content",
    ".content-page",
    ".main-content",
    ".news-detail",
    ".news-detail__content",
    ".news-item-detail",
    ".detail",
    ".detail-text",
    ".text",
    ".article",
    ".article-content",
)


def compact_text(value: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def element_text(element: Tag) -> str:
    return compact_text(
        element.get_text(
            " ",
            strip=True,
        )
    )


def describe_element(
    element: Tag,
) -> dict[str, Any]:
    text = element_text(element)

    return {
        "tag": element.name,
        "id": element.get("id"),
        "class": " ".join(
            element.get("class", [])
        ),
        "text_length": len(text),
        "links": len(
            element.select("a[href]")
        ),
        "paragraphs": len(
            element.select("p")
        ),
        "headings": len(
            element.select(
                "h1, h2, h3, h4"
            )
        ),
        "preview": text[:350],
    }


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "url",
        help="URL страницы для анализа",
    )

    arguments = parser.parse_args()

    response = requests.get(
        arguments.url,
        headers={
            "User-Agent": settings.user_agent,
        },
        timeout=(
            settings.connect_timeout,
            settings.read_timeout,
        ),
        allow_redirects=True,
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.content,
        "html.parser",
    )

    for element in soup.select(
        "script, style, noscript, svg"
    ):
        element.decompose()

    print("URL:", arguments.url)
    print("FINAL URL:", response.url)
    print(
        "TITLE TAG:",
        compact_text(
            soup.title.get_text(
                " ",
                strip=True,
            )
        )
        if soup.title
        else None,
    )

    print()
    print("=" * 100)
    print("H1:")

    for heading in soup.select("h1"):
        print(
            "-",
            compact_text(
                heading.get_text(
                    " ",
                    strip=True,
                )
            ),
        )

    print()
    print("=" * 100)
    print("КАНДИДАТЫ ПО CSS-СЕЛЕКТОРАМ:")

    found_ids: set[int] = set()

    for selector in CANDIDATE_SELECTORS:
        elements = soup.select(selector)

        if not elements:
            continue

        best = max(
            elements,
            key=lambda item: len(
                element_text(item)
            ),
        )

        found_ids.add(id(best))

        info = describe_element(best)

        print()
        print("-" * 100)
        print("selector:", selector)

        for key, value in info.items():
            print(f"{key}: {value}")

    print()
    print("=" * 100)
    print("КРУПНЕЙШИЕ БЛОКИ СТРАНИЦЫ:")

    candidates = []

    for element in soup.find_all(
        ["main", "article", "section", "div"]
    ):
        if not isinstance(element, Tag):
            continue

        text = element_text(element)

        if len(text) < 200:
            continue

        candidates.append(
            (
                len(text),
                element,
            )
        )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    shown = 0

    for _, element in candidates:
        if id(element) in found_ids:
            continue

        info = describe_element(element)

        print()
        print("-" * 100)

        for key, value in info.items():
            print(f"{key}: {value}")

        shown += 1

        if shown >= 15:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
