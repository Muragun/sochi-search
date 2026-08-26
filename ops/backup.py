from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import tarfile
import tempfile
from typing import Iterable


RELEASE_VERSION = "2.6.0"

# Что кладётся в копию из рабочего каталога.
#
# Список должен отвечать на один вопрос: хватит ли этого, чтобы поднять
# систему на чистой машине? Раньше не хватало. В копию попадал код, но
# не попадало то, чем он запускается: ни Dockerfile, ни
# docker-compose.app.yml, где описаны все семь ролей, ни точка входа
# образа, ни deploy.sh, ни юниты systemd для установки без Docker.
# Восстановившийся получал исходники и собирал окружение заново по
# памяти.
#
# Отдельно про запись "app": это удалённый пакет первой версии сервиса.
# Несуществующие записи пропускаются молча, поэтому строка ничего не
# ломала и просто не значила ничего — ровно тот случай, когда список
# перестают читать.
DEFAULT_SOURCE_ENTRIES = (
    "app_v2",
    "crawler_v2",
    "elasticsearch",
    "ocr",
    "tests",
    "ops",
    "docker",
    "systemd",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.app.yml",
    "docker-compose.override.yml",
    "deploy.sh",
    "VERSION",
    "OPERATIONS.md",
)

IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "backups",
    "data",
    "logs",
    "run",
}


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    checksum: str
    database_quick_check: str
    removed_archives: tuple[Path, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as source:
        for chunk in iter(
            lambda: source.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def should_ignore(relative_path: Path) -> bool:
    if any(
        part in IGNORED_DIRECTORY_NAMES
        for part in relative_path.parts
    ):
        return True

    name = relative_path.name

    return (
        name.endswith((".pyc", ".pyo"))
        or ".backup-" in name
        or ".bak-" in name
        or name.startswith(".release-")
    )


def copy_source_entry(
    source: Path,
    destination: Path,
) -> None:
    if source.is_symlink():
        return

    if source.is_file():
        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        shutil.copy2(source, destination)
        return

    if not source.is_dir():
        return

    destination.mkdir(
        parents=True,
        exist_ok=True,
    )

    for child in sorted(
        source.rglob("*"),
        key=lambda path: path.as_posix(),
    ):
        relative = child.relative_to(source)

        if should_ignore(relative) or child.is_symlink():
            continue

        target = destination / relative

        if child.is_dir():
            target.mkdir(
                parents=True,
                exist_ok=True,
            )
        elif child.is_file():
            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            shutil.copy2(child, target)


def backup_sqlite(
    database: Path,
    destination: Path,
) -> str:
    if not database.is_file():
        raise FileNotFoundError(
            f"Не найдена SQLite: {database}"
        )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    source_uri = database.resolve().as_uri() + "?mode=ro"
    try:
        source = sqlite3.connect(
            source_uri,
            uri=True,
            timeout=30,
        )
    except sqlite3.OperationalError as error:
        raise RuntimeError(
            "Не удалось открыть SQLite для согласованного чтения: "
            f"{database.resolve()} ({error})"
        ) from error

    target = sqlite3.connect(
        destination,
        timeout=30,
    )

    try:
        source.execute("PRAGMA query_only = ON")
        source.execute("PRAGMA busy_timeout = 30000")
        target.execute("PRAGMA busy_timeout = 30000")
        source.backup(
            target,
            pages=256,
            sleep=0.05,
        )
        target.commit()

        quick_check = target.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]
    finally:
        target.close()
        source.close()

    if quick_check != "ok":
        raise RuntimeError(
            "SQLite quick_check резервной копии: "
            f"{quick_check}"
        )

    return str(quick_check)


def iter_stage_files(stage: Path) -> Iterable[Path]:
    return sorted(
        (
            path
            for path in stage.rglob("*")
            if path.is_file()
        ),
        key=lambda path: path.relative_to(stage).as_posix(),
    )


def write_manifest(stage: Path) -> Path:
    manifest = stage / "manifest.sha256"
    lines = []

    for path in iter_stage_files(stage):
        if path == manifest:
            continue

        relative = path.relative_to(stage).as_posix()
        lines.append(
            f"{sha256_file(path)}  {relative}"
        )

    manifest.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    return manifest


def safe_backup_root(
    project_dir: Path,
    backup_root: Path,
) -> Path:
    project = project_dir.resolve()
    root = backup_root.resolve()
    allowed_parent = (project / "backups").resolve()

    try:
        root.relative_to(allowed_parent)
    except ValueError:
        is_inside_backup_directory = False
    else:
        is_inside_backup_directory = True

    if (
        root == Path("/")
        or root == project
        or not is_inside_backup_directory
    ):
        raise ValueError(
            "Каталог резервных копий должен находиться "
            f"внутри {allowed_parent}"
        )

    return root


def remove_expired_archives(
    backup_root: Path,
    retention: int,
) -> tuple[Path, ...]:
    if retention < 1:
        raise ValueError(
            "retention должен быть не меньше 1"
        )

    archives = sorted(
        backup_root.glob(
            "sochi-search-backup-*.tar.gz"
        ),
        key=lambda path: path.name,
        reverse=True,
    )

    removed: list[Path] = []

    for archive in archives[retention:]:
        checksum = Path(
            str(archive) + ".sha256"
        )
        archive.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        removed.append(archive)

    return tuple(removed)


def create_backup(
    *,
    project_dir: Path,
    database: Path,
    backup_root: Path,
    retention: int = 14,
) -> BackupResult:
    project = project_dir.resolve()

    if project == Path("/") or not project.is_dir():
        raise ValueError(
            f"Некорректный каталог проекта: {project}"
        )

    root = safe_backup_root(
        project,
        backup_root,
    )
    root.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )
    os.chmod(root, 0o700)

    lock_path = root / ".backup.lock"

    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(
                lock_file.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as error:
            raise RuntimeError(
                "Другая резервная копия уже выполняется"
            ) from error

        timestamp = datetime.now(
            timezone.utc
        ).strftime("%Y%m%dT%H%M%S.%fZ")
        archive_name = (
            f"sochi-search-backup-{timestamp}.tar.gz"
        )
        final_archive = root / archive_name
        temporary_archive = root / (
            f".{archive_name}.tmp"
        )
        stage = Path(
            tempfile.mkdtemp(
                prefix=".backup-stage-",
                dir=root,
            )
        )

        try:
            project_stage = stage / "project"
            project_stage.mkdir(
                parents=True,
                exist_ok=True,
            )

            for entry_name in DEFAULT_SOURCE_ENTRIES:
                source = project / entry_name

                if source.exists() or source.is_symlink():
                    copy_source_entry(
                        source,
                        project_stage / entry_name,
                    )

            for environment_file in sorted(
                project.glob(".env*")
            ):
                if environment_file.is_file() and not environment_file.is_symlink():
                    copy_source_entry(
                        environment_file,
                        project_stage / environment_file.name,
                    )

            database_stage = (
                stage
                / "data"
                / "crawl_state.sqlite3"
            )
            quick_check = backup_sqlite(
                database.resolve(),
                database_stage,
            )

            metadata = {
                "schema_version": 1,
                "release_version": RELEASE_VERSION,
                "created_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "hostname": socket.gethostname(),
                "project_dir": str(project),
                "database": str(database.resolve()),
                "database_quick_check": quick_check,
            }

            (stage / "metadata.json").write_text(
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )

            write_manifest(stage)

            with tarfile.open(
                temporary_archive,
                mode="w:gz",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                for path in sorted(
                    stage.rglob("*"),
                    key=lambda item: item.relative_to(stage).as_posix(),
                ):
                    archive.add(
                        path,
                        arcname=path.relative_to(stage).as_posix(),
                        recursive=False,
                    )

            os.chmod(temporary_archive, 0o600)
            os.replace(
                temporary_archive,
                final_archive,
            )
            checksum_value = sha256_file(
                final_archive
            )
            checksum_file = Path(
                str(final_archive) + ".sha256"
            )
            checksum_file.write_text(
                f"{checksum_value}  {final_archive.name}\n",
                encoding="utf-8",
            )
            os.chmod(checksum_file, 0o600)

            removed = remove_expired_archives(
                root,
                retention,
            )

            return BackupResult(
                archive=final_archive,
                checksum=checksum_value,
                database_quick_check=quick_check,
                removed_archives=removed,
            )
        finally:
            temporary_archive.unlink(
                missing_ok=True
            )
            shutil.rmtree(
                stage,
                ignore_errors=True,
            )


def parse_args() -> argparse.Namespace:
    project_default = Path(
        os.getenv(
            "SOCHI_SEARCH_DIR",
            "/opt/sochi-search",
        )
    )

    parser = argparse.ArgumentParser(
        description=(
            "Создаёт согласованную резервную копию "
            "Sochi Search без остановки сервисов."
        )
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=project_default,
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=int(
            os.getenv(
                "SOCHI_SEARCH_BACKUP_RETENTION",
                "14",
            )
        ),
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    project = arguments.project_dir.resolve()
    database = (
        arguments.database
        or project / "data" / "crawl_state.sqlite3"
    )
    backup_root = (
        arguments.backup_root
        or project / "backups" / "operational"
    )

    try:
        result = create_backup(
            project_dir=project,
            database=database,
            backup_root=backup_root,
            retention=arguments.retention,
        )
    except Exception as error:
        print("BACKUP_OK=0")
        print(f"BACKUP_ERROR={error}")
        return 1

    print("BACKUP_OK=1")
    print(f"BACKUP_FILE={result.archive}")
    print(f"BACKUP_SHA256={result.checksum}")
    print(
        "BACKUP_SQLITE_QUICK_CHECK="
        f"{result.database_quick_check}"
    )
    print(
        "BACKUP_REMOVED_EXPIRED="
        f"{len(result.removed_archives)}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
