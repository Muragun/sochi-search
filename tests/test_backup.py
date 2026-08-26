from __future__ import annotations

import ast
import io
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from ops.backup import (
    RELEASE_VERSION,
    create_backup,
    safe_backup_root,
    sha256_file,
)
from ops.verify_backup import verify_archive


class OperationalBackupTests(unittest.TestCase):
    def create_project(self, root: Path) -> tuple[Path, Path, Path]:
        project = root / "sochi-search"
        database = project / "data" / "crawl_state.sqlite3"
        backup_root = project / "backups" / "operational"

        (project / "app_v2").mkdir(parents=True)
        (project / "crawler_v2").mkdir()
        (project / "ops").mkdir()
        (project / "tests").mkdir()
        (project / "ocr" / "tessdata").mkdir(
            parents=True
        )
        database.parent.mkdir(parents=True)

        (project / "app_v2" / "main.py").write_text(
            "VERSION = 'test'\n",
            encoding="utf-8",
        )
        (project / "crawler_v2" / "worker.py").write_text(
            "print('worker')\n",
            encoding="utf-8",
        )
        (project / ".env.test").write_text(
            "SECRET=only-in-archive\n",
            encoding="utf-8",
        )
        (project / "docker-compose.override.yml").write_text(
            "services:\n  elasticsearch:\n    restart: unless-stopped\n",
            encoding="utf-8",
        )
        (project / "ocr" / "tessdata" / "rus.traineddata").write_bytes(
            b"test-ocr-model"
        )

        connection = sqlite3.connect(database)

        try:
            connection.execute(
                "CREATE TABLE queue (id INTEGER PRIMARY KEY, status TEXT)"
            )
            connection.execute(
                "INSERT INTO queue (status) VALUES ('done')"
            )
            connection.commit()
        finally:
            connection.close()

        return project, database, backup_root

    def test_backup_is_private_consistent_and_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, database, backup_root = self.create_project(root)

            result = create_backup(
                project_dir=project,
                database=database,
                backup_root=backup_root,
                retention=14,
            )

            verification = verify_archive(result.archive)

            self.assertEqual(result.database_quick_check, "ok")
            self.assertEqual(
                verification["database_quick_check"],
                "ok",
            )
            # Версия сверяется с модулем, а не с записанной строкой:
            # иначе тест краснеет на каждом выпуске, ничего не проверяя.
            self.assertEqual(
                verification["metadata"]["release_version"],
                RELEASE_VERSION,
            )
            self.assertEqual(
                os.stat(result.archive).st_mode & 0o777,
                0o600,
            )
            self.assertTrue(
                Path(str(result.archive) + ".sha256").is_file()
            )

            with tarfile.open(result.archive, "r:gz") as archive:
                names = set(archive.getnames())

            self.assertIn("project/app_v2/main.py", names)
            self.assertIn("project/.env.test", names)
            self.assertIn("project/docker-compose.override.yml", names)
            self.assertIn(
                "project/ocr/tessdata/rus.traineddata",
                names,
            )
            self.assertIn("data/crawl_state.sqlite3", names)
            self.assertIn("manifest.sha256", names)

    def test_retention_removes_only_old_matching_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, database, backup_root = self.create_project(root)

            first = create_backup(
                project_dir=project,
                database=database,
                backup_root=backup_root,
                retention=1,
            )
            unrelated = backup_root / "keep-me.txt"
            unrelated.write_text("safe\n", encoding="utf-8")
            second = create_backup(
                project_dir=project,
                database=database,
                backup_root=backup_root,
                retention=1,
            )

            self.assertNotEqual(first.archive, second.archive)
            self.assertFalse(first.archive.exists())
            self.assertTrue(second.archive.exists())
            self.assertTrue(unrelated.exists())

    def test_live_wal_database_is_copied_without_main_file_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, database, backup_root = self.create_project(root)
            writer = sqlite3.connect(database)

            try:
                journal_mode = writer.execute(
                    "PRAGMA journal_mode = WAL"
                ).fetchone()[0]
                self.assertEqual(journal_mode.lower(), "wal")
                writer.execute(
                    "INSERT INTO queue (status) VALUES ('from-wal')"
                )
                writer.commit()

                wal_path = Path(str(database) + "-wal")
                self.assertTrue(wal_path.is_file())
                database_hash_before = sha256_file(database)

                result = create_backup(
                    project_dir=project,
                    database=database,
                    backup_root=backup_root,
                    retention=14,
                )

                self.assertEqual(
                    sha256_file(database),
                    database_hash_before,
                )

                with tarfile.open(result.archive, "r:gz") as archive:
                    snapshot_member = archive.extractfile(
                        "data/crawl_state.sqlite3"
                    )
                    self.assertIsNotNone(snapshot_member)
                    snapshot_bytes = snapshot_member.read()

                snapshot = root / "snapshot.sqlite3"
                snapshot.write_bytes(snapshot_bytes)
                restored = sqlite3.connect(snapshot)

                try:
                    statuses = {
                        row[0]
                        for row in restored.execute(
                            "SELECT status FROM queue"
                        )
                    }
                finally:
                    restored.close()

                self.assertEqual(
                    statuses,
                    {"done", "from-wal"},
                )
            finally:
                writer.close()

    def test_unsafe_archive_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "unsafe.tar.gz"

            with tarfile.open(archive_path, "w:gz") as archive:
                payload = b"escape"
                info = tarfile.TarInfo("../escape.txt")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

            with self.assertRaisesRegex(ValueError, "Опасный путь"):
                verify_archive(archive_path)

    def test_backup_root_guard_supports_python_38(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "sochi-search"
            allowed = project / "backups" / "operational"
            outside = root / "outside"
            (project / "backups").mkdir(parents=True)

            self.assertEqual(
                safe_backup_root(project, allowed),
                allowed.resolve(),
            )

            with self.assertRaisesRegex(
                ValueError,
                "Каталог резервных копий",
            ):
                safe_backup_root(project, outside)

        source = Path(__file__).parents[1] / "ops" / "backup.py"
        tree = ast.parse(
            source.read_text(encoding="utf-8")
        )
        unsupported_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "is_relative_to"
        ]
        self.assertEqual(unsupported_calls, [])

    def test_systemd_profile_supports_live_wal_backup(self) -> None:
        unit_path = (
            Path(__file__).parents[1]
            / "systemd"
            / "sochi-search-backup.service"
        )
        lines = [
            line.strip()
            for line in unit_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        self.assertIn("ProtectSystem=strict", lines)
        self.assertIn(
            "ConditionPathExists="
            "/opt/sochi-search/data/crawl_state.sqlite3",
            lines,
        )

        writable_paths = set()

        for line in lines:
            if line.startswith("ReadWritePaths="):
                writable_paths.update(
                    line.partition("=")[2].split()
                )

        self.assertIn(
            "/opt/sochi-search/backups",
            writable_paths,
        )
        self.assertIn(
            "/opt/sochi-search/data",
            writable_paths,
        )
        self.assertNotIn(
            "/opt/sochi-search",
            writable_paths,
        )


class RestorableSourceTests(unittest.TestCase):
    """
    Копия обязана содержать то, чем система запускается, а не только код.

    Список файлов в копии проверялся глазами, и в нём не хватало ровно
    того, без чего восстановление не заканчивается запуском: Dockerfile,
    docker-compose.app.yml с описанием всех семи ролей, точка входа
    образа, deploy.sh и юниты systemd. Восстанавливающий получал
    исходники и собирал окружение заново по памяти — в тот день, когда
    времени на это меньше всего.

    Незаявленные записи пропускаются молча (`if source.exists()`),
    поэтому ошибка в списке ничем не проявлялась: копия собиралась,
    отчитывалась об успехе и была неполной.

    Здесь список сверяется не сам с собой, а с содержимым репозитория:
    новый файл верхнего уровня, нужный для запуска, обязан быть в него
    добавлен.
    """

    PROJECT = Path(__file__).resolve().parents[1]

    # Чем система запускается. Список короткий и меняется редко, поэтому
    # перечислен явно: это и есть предмет проверки.
    LAUNCH_ESSENTIALS = (
        "Dockerfile",
        "docker",
        "docker-compose.yml",
        "docker-compose.app.yml",
        "docker-compose.override.yml",
        "deploy.sh",
        "systemd",
        "requirements.txt",
        "VERSION",
    )

    def test_launch_essentials_are_backed_up(self) -> None:
        from ops.backup import DEFAULT_SOURCE_ENTRIES

        missing = [
            name
            for name in self.LAUNCH_ESSENTIALS
            if name not in DEFAULT_SOURCE_ENTRIES
        ]

        self.assertEqual(
            missing,
            [],
            msg=(
                "Без этих файлов из копии не поднять систему, а они в "
                "неё не попадают: " + ", ".join(missing)
            ),
        )

    def test_every_listed_entry_exists(self) -> None:
        """
        Запись, которой нет в репозитории, — это забытая уборка.

        Так в списке годами оставался пакет `app`: первую версию сервиса
        удалили, строку — нет. Копирование пропускает несуществующее
        молча, поэтому строка не мешала и не значила ничего.
        """

        from ops.backup import DEFAULT_SOURCE_ENTRIES

        missing = [
            name
            for name in DEFAULT_SOURCE_ENTRIES
            if not (self.PROJECT / name).exists()
        ]

        self.assertEqual(
            missing,
            [],
            msg=(
                "Этих записей в репозитории нет — копирование пропустит "
                "их молча: " + ", ".join(missing)
            ),
        )


if __name__ == "__main__":
    unittest.main()
