from __future__ import annotations

import argparse
import os
import pickle
import traceback
from pathlib import Path

from .content_processing import (
    CorruptPdfError,
    EmptyContentError,
    _extract_pdf_in_process,
)


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
            "Изолированное извлечение текста PDF"
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
        "--content-type",
        default="application/pdf",
    )
    parser.add_argument(
        "--final-url",
        required=True,
    )
    parser.add_argument(
        "--ocr-mode",
        choices=("fast", "bounded"),
        default="bounded",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    try:
        content = _extract_pdf_in_process(
            arguments.input,
            content_type=arguments.content_type,
            final_url=arguments.final_url,
            ocr_mode=arguments.ocr_mode,
        )
        payload = {
            "status": "ok",
            "content": content,
        }

    except CorruptPdfError as exc:
        payload = {
            "status": "error",
            "error_kind": "corrupt",
            "message": str(exc),
        }

    except EmptyContentError as exc:
        payload = {
            "status": "error",
            "error_kind": "empty",
            "message": str(exc),
        }

    except Exception as exc:
        traceback.print_exc()
        payload = {
            "status": "error",
            "error_kind": "runtime",
            "message": (
                f"{type(exc).__name__}: {exc}"
            ),
        }

    write_result(
        arguments.output,
        payload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
