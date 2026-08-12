from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import requests

from .config import settings


def fetch_hash(
    session: requests.Session,
    url: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "url": url,
        "success": False,
        "status_code": None,
        "final_url": None,
        "content_type": None,
        "content_length": 0,
        "sha256": None,
        "error": None,
    }

    try:
        response = session.get(
            url,
            timeout=(
                settings.connect_timeout,
                settings.read_timeout,
            ),
            allow_redirects=True,
            stream=True,
        )

        result["status_code"] = (
            response.status_code
        )
        result["final_url"] = response.url
        result["content_type"] = (
            response.headers.get(
                "Content-Type"
            )
        )

        response.raise_for_status()

        digest = hashlib.sha256()
        total = 0

        for chunk in response.iter_content(
            chunk_size=1024 * 128
        ):
            if not chunk:
                continue

            total += len(chunk)

            if total > settings.max_download_bytes:
                raise RuntimeError(
                    "Файл превышает "
                    "MAX_DOWNLOAD_BYTES"
                )

            digest.update(chunk)

        result["content_length"] = total
        result["sha256"] = (
            digest.hexdigest()
        )
        result["success"] = True

        return result

    except Exception as exc:
        result["error"] = str(exc)
        return result


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "report",
        help="JSON-отчёт url_audit",
    )

    parser.add_argument(
        "--output",
        help="Путь итогового JSON",
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    report_path = Path(
        arguments.report
    )

    report = json.loads(
        report_path.read_text(
            encoding="utf-8"
        )
    )

    session = requests.Session()

    session.headers.update(
        {
            "User-Agent": (
                settings.user_agent
            )
        }
    )

    verified_groups = []

    for group in report.get(
        "duplicate_groups",
        [],
    ):
        fetched = []

        for row in group["rows"]:
            fetched.append(
                fetch_hash(
                    session,
                    str(row["url"]),
                )
            )

        hashes = {
            item["sha256"]
            for item in fetched
            if item["success"]
        }

        successful_count = sum(
            1
            for item in fetched
            if item["success"]
        )

        verified_groups.append(
            {
                "canonical_url": (
                    group["canonical_url"]
                ),
                "database_rows": (
                    group["count"]
                ),
                "successful_fetches": (
                    successful_count
                ),
                "all_current_files_equal": (
                    successful_count
                    == len(fetched)
                    and len(hashes) == 1
                ),
                "files": fetched,
            }
        )

    result = {
        "source_report": str(
            report_path
        ),
        "groups": verified_groups,
    }

    if arguments.output:
        output_path = Path(
            arguments.output
        )
    else:
        output_path = Path(
            "logs/url-duplicate-verification.json"
        )

    output_path.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )

    print()
    print("report:", output_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
