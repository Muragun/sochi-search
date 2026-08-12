from __future__ import annotations

import argparse
import os
import pickle
import traceback
from pathlib import Path

import pymupdf

from .config import settings
from .pdf_ocr import extract_pdf_page_text


def write_result(
    path: Path,
    payload: dict,
) -> None:
    temporary = path.with_name(
        path.name + ".writing"
    )

    try:
        descriptor = os.open(
            str(temporary),
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC,
            0o600,
        )

        with os.fdopen(
            descriptor,
            "wb",
        ) as stream:
            pickle.dump(
                payload,
                stream,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(
            temporary,
            path,
        )

    finally:
        temporary.unlink(
            missing_ok=True
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Изолированный OCR одной страницы PDF"
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--page-index",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--minimum-length",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--force-header-ocr",
        action="store_true",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    document = None

    try:
        document = pymupdf.open(
            filename=str(arguments.input),
            filetype="pdf",
        )
        page = document.load_page(
            arguments.page_index
        )

        try:
            native_text = page.get_text(
                "text",
                sort=True,
            )
        except Exception:
            native_text = ""

        result = extract_pdf_page_text(
            page,
            native_text,
            tessdata=(
                settings.pdf_ocr_tessdata_path
            ),
            languages=(
                settings.pdf_ocr_languages
            ),
            dpi=settings.pdf_ocr_dpi,
            rotations=(
                settings.pdf_ocr_rotations
            ),
            minimum_length=(
                arguments.minimum_length
            ),
            ocr_enabled=True,
            force_ocr=(
                arguments.force_header_ocr
            ),
        )
        payload = {
            "status": "ok",
            "result": result,
        }

    except Exception as exc:
        traceback.print_exc()
        payload = {
            "status": "error",
            "message": (
                f"{type(exc).__name__}: {exc}"
            ),
        }

    finally:
        if document is not None:
            document.close()

    write_result(
        arguments.output,
        payload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
