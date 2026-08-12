from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tarfile
import tempfile
from typing import Any


MANIFEST_PATTERN = re.compile(
    r"^([0-9a-f]{64})  (.+)$"
)
MAX_ARCHIVE_MEMBERS = 100_000
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as source:
        for chunk in iter(
            lambda: source.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def validate_member_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)

    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "" in path.parts
    ):
        raise ValueError(
            f"Опасный путь в архиве: {name!r}"
        )

    return path


def extract_safely(
    archive_path: Path,
    destination: Path,
) -> None:
    with tarfile.open(
        archive_path,
        mode="r:gz",
    ) as archive:
        members = archive.getmembers()

        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                "В архиве слишком много элементов"
            )

        total_size = sum(
            member.size
            for member in members
            if member.isfile()
        )

        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(
                "Архив превышает допустимый размер"
            )

        for member in members:
            relative = validate_member_name(
                member.name
            )

            if not (
                member.isdir()
                or member.isfile()
            ):
                raise ValueError(
                    "Архив содержит ссылку или "
                    f"специальный файл: {member.name}"
                )

            target = destination.joinpath(
                *relative.parts
            )

            if member.isdir():
                target.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                continue

            source = archive.extractfile(member)

            if source is None:
                raise ValueError(
                    f"Не удалось прочитать {member.name}"
                )

            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with source, target.open("xb") as output:
                shutil.copyfileobj(
                    source,
                    output,
                    length=1024 * 1024,
                )


def parse_manifest(
    manifest: Path,
) -> dict[str, str]:
    expected: dict[str, str] = {}

    for line_number, raw_line in enumerate(
        manifest.read_text(
            encoding="utf-8"
        ).splitlines(),
        start=1,
    ):
        match = MANIFEST_PATTERN.fullmatch(
            raw_line
        )

        if match is None:
            raise ValueError(
                "Некорректная строка manifest.sha256 "
                f"№ {line_number}"
            )

        checksum, raw_path = match.groups()
        path = validate_member_name(raw_path)
        normalized = path.as_posix()

        if normalized in expected:
            raise ValueError(
                f"Повтор пути в manifest: {normalized}"
            )

        expected[normalized] = checksum

    if not expected:
        raise ValueError(
            "manifest.sha256 пуст"
        )

    return expected


def verify_directory(root: Path) -> dict[str, Any]:
    required = {
        "manifest.sha256",
        "metadata.json",
        "data/crawl_state.sqlite3",
    }
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }

    missing_required = required - actual_files

    if missing_required:
        raise ValueError(
            "В архиве отсутствуют обязательные файлы: "
            + ", ".join(sorted(missing_required))
        )

    manifest = parse_manifest(
        root / "manifest.sha256"
    )
    expected_files = set(manifest)
    unexpected = (
        actual_files
        - expected_files
        - {"manifest.sha256"}
    )
    missing = expected_files - actual_files

    if unexpected:
        raise ValueError(
            "Файлы отсутствуют в manifest: "
            + ", ".join(sorted(unexpected))
        )

    if missing:
        raise ValueError(
            "Файлы из manifest отсутствуют: "
            + ", ".join(sorted(missing))
        )

    for relative, expected_checksum in manifest.items():
        actual_checksum = sha256_file(
            root / relative
        )

        if actual_checksum != expected_checksum:
            raise ValueError(
                f"Не совпала SHA-256: {relative}"
            )

    metadata = json.loads(
        (root / "metadata.json").read_text(
            encoding="utf-8"
        )
    )

    if metadata.get("schema_version") != 1:
        raise ValueError(
            "Неизвестная версия формата резервной копии"
        )

    database = root / "data" / "crawl_state.sqlite3"
    connection = sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=30,
    )

    try:
        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]
    finally:
        connection.close()

    if quick_check != "ok":
        raise ValueError(
            f"SQLite quick_check: {quick_check}"
        )

    return {
        "metadata": metadata,
        "database_quick_check": str(quick_check),
        "file_count": len(actual_files),
    }


def verify_archive(
    archive_path: Path,
    *,
    extract_to: Path | None = None,
) -> dict[str, Any]:
    archive = archive_path.resolve()

    if not archive.is_file():
        raise FileNotFoundError(
            f"Не найден архив: {archive}"
        )

    with tempfile.TemporaryDirectory(
        prefix="sochi-search-verify-"
    ) as temporary_directory:
        root = Path(temporary_directory)
        extract_safely(
            archive,
            root,
        )
        result = verify_directory(root)

        if extract_to is not None:
            destination = extract_to.resolve()

            if destination == Path("/") or destination.exists():
                raise ValueError(
                    "Каталог --extract-to должен быть новым "
                    "и не может быть корнем файловой системы"
                )

            shutil.copytree(
                root,
                destination,
            )
            result["extracted_to"] = str(destination)

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Проверяет структуру, SHA-256 и SQLite "
            "резервной копии Sochi Search."
        )
    )
    parser.add_argument(
        "archive",
        type=Path,
    )
    parser.add_argument(
        "--extract-to",
        type=Path,
        default=None,
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_args()

    try:
        result = verify_archive(
            arguments.archive,
            extract_to=arguments.extract_to,
        )
    except Exception as error:
        print("BACKUP_VERIFY=FAILED")
        print(f"BACKUP_VERIFY_ERROR={error}")
        return 1

    metadata = result["metadata"]
    print("BACKUP_VERIFY=OK")
    print(
        "BACKUP_RELEASE_VERSION="
        f"{metadata.get('release_version')}"
    )
    print(
        "BACKUP_CREATED_AT="
        f"{metadata.get('created_at')}"
    )
    print(
        "BACKUP_SQLITE_QUICK_CHECK="
        f"{result['database_quick_check']}"
    )
    print(
        "BACKUP_FILE_COUNT="
        f"{result['file_count']}"
    )

    if result.get("extracted_to"):
        print(
            "BACKUP_EXTRACTED_TO="
            f"{result['extracted_to']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

