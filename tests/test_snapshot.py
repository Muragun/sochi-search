"""
Снимки индекса: что именно отправляется кластеру и что убирается.

Живой Elasticsearch здесь не нужен и не используется. Проверяется
решение, а не запись на диск: какие запросы уходят, в каком порядке,
что считается неудачей и какие снимки задача имеет право удалить.

Последнее — самая опасная часть. Ошибка в сторону «снял лишний снимок»
стоит места на диске. Ошибка в сторону «удалил нужный» стоит недель
распознавания, потому что удалять их некому больше.
"""

from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.snapshot import (  # noqa: E402
    SnapshotError,
    configured_indices,
    configured_url,
    create_snapshot,
    delete_snapshot,
    ensure_repository,
    expired_snapshots,
    list_snapshots,
    main,
    run_snapshot,
    snapshot_name,
)


class FakeCluster:
    """
    Кластер, который записывает обращения и отвечает заготовками.

    Ответы задаются по паре «метод и начало пути»: этого хватает, чтобы
    отличить регистрацию хранилища от создания снимка, и не хватает,
    чтобы тест начал зависеть от порядка параметров в строке запроса.
    """

    def __init__(self, responses=None, repository=None) -> None:
        self.calls = []
        self.responses = responses or {}
        self.repository = repository

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))

        for (want_method, prefix), response in self.responses.items():
            if method == want_method and path.startswith(prefix):
                if isinstance(response, Exception):
                    raise response

                return response

        if method == "GET" and path.startswith("/_snapshot/"):
            if self.repository is None:
                raise SnapshotError("404: хранилище не найдено")

            return self.repository

        return {}

    def methods_and_paths(self):
        return [(method, path) for method, path, _ in self.calls]

    def payload_for(self, method, prefix):
        for call_method, path, payload in self.calls:
            if call_method == method and path.startswith(prefix):
                return payload

        return None


SUCCESSFUL_SNAPSHOT = {
    "snapshot": {
        "snapshot": "auto-20260826t040000z",
        "state": "SUCCESS",
        "duration_in_millis": 4210,
        "shards": {"total": 3, "failed": 0, "successful": 3},
    }
}


class SnapshotNameTests(unittest.TestCase):
    def test_name_carries_prefix_and_utc_stamp(self) -> None:
        moment = datetime(2026, 8, 26, 4, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(
            snapshot_name("auto", now=moment),
            "auto-20260826t040000z",
        )

    def test_local_time_is_converted_to_utc(self) -> None:
        # Сервер стоит в Москве. Если бы метка бралась как есть, имена
        # снимков сортировались бы по местному времени, а сравнивались
        # бы с UTC в самом кластере.
        from datetime import timedelta

        moscow = timezone(timedelta(hours=3))
        moment = datetime(2026, 8, 26, 2, 30, 0, tzinfo=moscow)

        self.assertEqual(
            snapshot_name("auto", now=moment),
            "auto-20260825t233000z",
        )

    def test_names_sort_chronologically_as_strings(self) -> None:
        moments = [
            datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc),
        ]
        names = [snapshot_name("auto", now=moment) for moment in moments]

        self.assertEqual(names, sorted(names))

    def test_uppercase_prefix_is_folded(self) -> None:
        # Elasticsearch отказывается принимать имя с большой буквой.
        moment = datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc)

        self.assertEqual(
            snapshot_name("AUTO", now=moment),
            "auto-20260826t040000z",
        )

    def test_invalid_prefix_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            snapshot_name("не ascii")


class RepositoryTests(unittest.TestCase):
    def test_missing_repository_is_created(self) -> None:
        cluster = FakeCluster(repository=None)

        created = ensure_repository(
            cluster,
            repository="sochi",
            location="/snapshots",
        )

        self.assertTrue(created)
        self.assertIn(
            ("PUT", "/_snapshot/sochi"),
            cluster.methods_and_paths(),
        )
        self.assertEqual(
            cluster.payload_for("PUT", "/_snapshot/sochi"),
            {"type": "fs", "settings": {"location": "/snapshots"}},
        )

    def test_existing_repository_is_left_alone(self) -> None:
        cluster = FakeCluster(
            repository={
                "sochi": {
                    "type": "fs",
                    "settings": {"location": "/snapshots"},
                }
            }
        )

        created = ensure_repository(
            cluster,
            repository="sochi",
            location="/snapshots",
        )

        self.assertFalse(created)
        self.assertNotIn(
            "PUT",
            [method for method, _ in cluster.methods_and_paths()],
        )

    def test_repository_pointing_elsewhere_is_an_error(self) -> None:
        """
        Перерегистрация потеряла бы ссылку на снятые снимки.

        Случай не выдуманный: `path.repo` задаётся в compose, и при
        переносе на машину с другим путём молчаливая перерегистрация
        оставила бы систему с пустым хранилищем и уверенностью, что
        копии есть.
        """

        cluster = FakeCluster(
            repository={
                "sochi": {
                    "type": "fs",
                    "settings": {"location": "/mnt/old-snapshots"},
                }
            }
        )

        with self.assertRaises(SnapshotError) as caught:
            ensure_repository(
                cluster,
                repository="sochi",
                location="/snapshots",
            )

        self.assertIn("/mnt/old-snapshots", str(caught.exception))


class CreateSnapshotTests(unittest.TestCase):
    def test_request_waits_for_completion(self) -> None:
        cluster = FakeCluster(
            repository={"sochi": {"settings": {"location": "/snapshots"}}},
            responses={("PUT", "/_snapshot/sochi/auto-"): SUCCESSFUL_SNAPSHOT},
        )

        create_snapshot(
            cluster,
            repository="sochi",
            name="auto-20260826t040000z",
            indices=("sochi_search",),
            request_timeout=60.0,
        )

        method, path, _ = cluster.calls[0]

        self.assertEqual(method, "PUT")
        self.assertIn("wait_for_completion=true", path)

    def test_partial_snapshots_are_refused(self) -> None:
        # partial=true отдал бы «копию» без части шардов, и выглядела бы
        # она как удачная.
        cluster = FakeCluster(
            responses={("PUT", "/_snapshot/"): SUCCESSFUL_SNAPSHOT},
        )

        create_snapshot(
            cluster,
            repository="sochi",
            name="auto-20260826t040000z",
            indices=("sochi_search",),
            request_timeout=60.0,
        )

        payload = cluster.payload_for("PUT", "/_snapshot/")

        self.assertIs(payload["partial"], False)
        self.assertIs(payload["include_global_state"], False)

    def test_indices_are_passed_as_a_comma_list(self) -> None:
        cluster = FakeCluster(
            responses={("PUT", "/_snapshot/"): SUCCESSFUL_SNAPSHOT},
        )

        create_snapshot(
            cluster,
            repository="sochi",
            name="auto-20260826t040000z",
            indices=("sochi_search", "sochi_docs_v4"),
            request_timeout=60.0,
        )

        self.assertEqual(
            cluster.payload_for("PUT", "/_snapshot/")["indices"],
            "sochi_search,sochi_docs_v4",
        )


class ExpiredSnapshotTests(unittest.TestCase):
    """
    Уборка обязана трогать только свои снимки.

    `docs/DEPLOY.md` описывает ручной снимок под именем `full` — его
    снимают перед переездом, руками, и он должен пережить любое число
    ночных запусков.
    """

    def test_keeps_the_configured_number(self) -> None:
        names = [
            "auto-20260820t040000z",
            "auto-20260821t040000z",
            "auto-20260822t040000z",
        ]

        self.assertEqual(
            expired_snapshots(names, prefix="auto", retention=3),
            [],
        )

    def test_removes_the_oldest_beyond_retention(self) -> None:
        names = [
            "auto-20260820t040000z",
            "auto-20260821t040000z",
            "auto-20260822t040000z",
            "auto-20260823t040000z",
        ]

        self.assertEqual(
            expired_snapshots(names, prefix="auto", retention=2),
            [
                "auto-20260820t040000z",
                "auto-20260821t040000z",
            ],
        )

    def test_manual_snapshots_are_never_removed(self) -> None:
        names = [
            "full",
            "before-migration",
            "auto-20260820t040000z",
            "auto-20260821t040000z",
        ]

        self.assertEqual(
            expired_snapshots(names, prefix="auto", retention=1),
            ["auto-20260820t040000z"],
        )

    def test_zero_retention_removes_nothing(self) -> None:
        names = ["auto-%dt040000z" % day for day in range(20260801, 20260811)]

        self.assertEqual(
            expired_snapshots(names, prefix="auto", retention=0),
            [],
        )

    def test_unsorted_input_is_ordered_before_counting(self) -> None:
        names = [
            "auto-20260823t040000z",
            "auto-20260820t040000z",
            "auto-20260822t040000z",
        ]

        self.assertEqual(
            expired_snapshots(names, prefix="auto", retention=1),
            [
                "auto-20260820t040000z",
                "auto-20260822t040000z",
            ],
        )


class ListSnapshotTests(unittest.TestCase):
    def test_names_are_extracted_and_sorted(self) -> None:
        cluster = FakeCluster(
            responses={
                ("GET", "/_snapshot/sochi/_all"): {
                    "snapshots": [
                        {"snapshot": "auto-20260822t040000z"},
                        {"snapshot": "auto-20260820t040000z"},
                    ]
                }
            }
        )

        self.assertEqual(
            list_snapshots(cluster, repository="sochi"),
            [
                "auto-20260820t040000z",
                "auto-20260822t040000z",
            ],
        )

    def test_empty_repository_is_not_an_error(self) -> None:
        cluster = FakeCluster(
            responses={("GET", "/_snapshot/sochi/_all"): {"snapshots": []}}
        )

        self.assertEqual(
            list_snapshots(cluster, repository="sochi"),
            [],
        )


class RunSnapshotTests(unittest.TestCase):
    def working_cluster(self, existing=()):
        return FakeCluster(
            repository={
                "sochi": {
                    "type": "fs",
                    "settings": {"location": "/snapshots"},
                }
            },
            responses={
                ("PUT", "/_snapshot/sochi/auto-"): SUCCESSFUL_SNAPSHOT,
                ("GET", "/_snapshot/sochi/_all"): {
                    "snapshots": [
                        {"snapshot": name} for name in existing
                    ]
                },
            },
        )

    def test_successful_pass_reports_the_snapshot(self) -> None:
        cluster = self.working_cluster()

        result = run_snapshot(
            cluster,
            repository="sochi",
            location="/snapshots",
            indices=("sochi_search",),
            prefix="auto",
            retention=7,
            request_timeout=60.0,
            now=datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(result.name, "auto-20260826t040000z")
        self.assertEqual(result.state, "SUCCESS")
        self.assertEqual(result.shards_failed, 0)
        self.assertEqual(result.removed, ())

    def test_expired_snapshots_are_deleted(self) -> None:
        cluster = self.working_cluster(
            existing=[
                "auto-20260820t040000z",
                "auto-20260821t040000z",
                "auto-20260826t040000z",
            ]
        )

        result = run_snapshot(
            cluster,
            repository="sochi",
            location="/snapshots",
            indices=("sochi_search",),
            prefix="auto",
            retention=1,
            request_timeout=60.0,
            now=datetime(2026, 8, 26, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            result.removed,
            (
                "auto-20260820t040000z",
                "auto-20260821t040000z",
            ),
        )

        deleted = [
            path
            for method, path in cluster.methods_and_paths()
            if method == "DELETE"
        ]

        self.assertEqual(
            deleted,
            [
                "/_snapshot/sochi/auto-20260820t040000z",
                "/_snapshot/sochi/auto-20260821t040000z",
            ],
        )

    def test_failed_snapshot_stops_before_deleting_anything(self) -> None:
        """
        Главная защита задачи.

        Неудачный снимок с уборкой старых означает: годных копий стало
        меньше, негодная добавилась. Такой исход хуже, чем отсутствие
        задачи вообще, поэтому уборка выполняется только после успеха.
        """

        cluster = FakeCluster(
            repository={
                "sochi": {"settings": {"location": "/snapshots"}},
            },
            responses={
                ("PUT", "/_snapshot/sochi/auto-"): {
                    "snapshot": {
                        "state": "PARTIAL",
                        "shards": {"total": 3, "failed": 1},
                        "failures": [{"index": "sochi_search", "shard": 2}],
                    }
                },
                ("GET", "/_snapshot/sochi/_all"): {
                    "snapshots": [
                        {"snapshot": "auto-20260820t040000z"},
                        {"snapshot": "auto-20260821t040000z"},
                    ]
                },
            },
        )

        with self.assertRaises(SnapshotError):
            run_snapshot(
                cluster,
                repository="sochi",
                location="/snapshots",
                indices=("sochi_search",),
                prefix="auto",
                retention=1,
                request_timeout=60.0,
            )

        self.assertEqual(
            [
                path
                for method, path in cluster.methods_and_paths()
                if method == "DELETE"
            ],
            [],
        )

    def test_empty_index_list_is_refused(self) -> None:
        with self.assertRaises(SnapshotError):
            run_snapshot(
                self.working_cluster(),
                repository="sochi",
                location="/snapshots",
                indices=(),
                prefix="auto",
                retention=7,
                request_timeout=60.0,
            )


class DeleteSnapshotTests(unittest.TestCase):
    def test_name_is_escaped_in_the_path(self) -> None:
        cluster = FakeCluster()

        delete_snapshot(
            cluster,
            repository="sochi",
            name="auto-20260820t040000z",
        )

        self.assertEqual(
            cluster.methods_and_paths(),
            [("DELETE", "/_snapshot/sochi/auto-20260820t040000z")],
        )


class ConfigurationTests(unittest.TestCase):
    def test_url_must_point_at_the_service_root(self) -> None:
        with self.assertRaises(ValueError):
            configured_url("http://elasticsearch:9200/sochi_search")

    def test_url_scheme_is_checked(self) -> None:
        with self.assertRaises(ValueError):
            configured_url("ftp://elasticsearch:9200")

    def test_trailing_slash_is_trimmed(self) -> None:
        self.assertEqual(
            configured_url("http://elasticsearch:9200/"),
            "http://elasticsearch:9200",
        )

    def test_indices_are_split_on_commas(self) -> None:
        self.assertEqual(
            configured_indices("sochi_search, sochi_docs_v4 "),
            ("sochi_search", "sochi_docs_v4"),
        )

    def test_blank_index_list_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            configured_indices(" , ")


class BackupRoleTests(unittest.TestCase):
    """
    Роль `backup` обязана делать обе копии, даже когда одна отказала.

    Соблазн написать это одной строкой (`ops.backup && ops.snapshot`)
    велик, и результат был бы такой: недоступный кластер оставляет
    систему без снимка — что понятно — и заодно без копии очереди, что
    уже никак не следует из причины. Задачи независимы, значит и
    выполняться должны обе.

    Проверяется настоящий запуск: функция берётся из entrypoint.sh, а
    вместо python подставляется заглушка, которая записывает вызовы и
    отвечает заданным кодом.
    """

    # Точка входа лежит по-разному в репозитории и в образе, и это не
    # случайность: Dockerfile кладёт её сразу в PATH
    # (`COPY docker/entrypoint.sh /usr/local/bin/entrypoint`), а каталога
    # `docker/` в образе нет вовсе.
    #
    # Проверка нужна в обеих средах — она про поведение роли, а не про
    # раскладку файлов, — поэтому ищется в обоих местах. Первая версия
    # знала только про репозиторий и внутри образа падала с кодом 127:
    # «команда не найдена», без единого слова о том, какая.
    ENTRYPOINT_LOCATIONS = (
        Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh",
        Path("/usr/local/bin/entrypoint"),
    )

    @classmethod
    def entrypoint(cls):
        for candidate in cls.ENTRYPOINT_LOCATIONS:
            if candidate.is_file():
                return candidate

        return None

    def setUp(self) -> None:
        if self.entrypoint() is None:
            self.skipTest(
                "точка входа не найдена ни в репозитории, ни в PATH: "
                + ", ".join(
                    str(path) for path in self.ENTRYPOINT_LOCATIONS
                )
            )

    def run_pass(self, *, failing_module: str = ""):
        """Возвращает (код возврата, список вызванных модулей)."""

        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls.txt"
            stub = root / "python"

            stub.write_text(
                "#!/usr/bin/env bash\n"
                "module=\"\"\n"
                "while [ $# -gt 0 ]; do\n"
                "    if [ \"$1\" = '-m' ]; then module=\"$2\"; fi\n"
                "    shift\n"
                "done\n"
                "echo \"$module\" >> %s\n"
                "[ \"$module\" = '%s' ] && exit 1\n"
                "exit 0\n" % (calls, failing_module),
                encoding="utf-8",
            )
            stub.chmod(0o755)

            script = (
                "set -uo pipefail\n"
                "ROLE=backup\n"
                "PYTHON=%s\n"
                "log() { :; }\n"
                # Из entrypoint берётся только определение функции:
                # выполнять его целиком означало бы запустить роль.
                "eval \"$(sed -n '/^run_backup_pass()/,/^}/p' %s)\"\n"
                "run_backup_pass\n"
            ) % (stub, self.entrypoint())

            completed = subprocess.run(
                ["bash", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )

            called = (
                calls.read_text(encoding="utf-8").split()
                if calls.exists()
                else []
            )

        return completed.returncode, called

    def test_both_copies_are_made(self) -> None:
        code, called = self.run_pass()

        self.assertEqual(code, 0)
        self.assertEqual(called, ["ops.backup", "ops.snapshot"])

    def test_failed_snapshot_does_not_cancel_the_queue_copy(self) -> None:
        code, called = self.run_pass(failing_module="ops.snapshot")

        self.assertIn("ops.backup", called)
        self.assertNotEqual(code, 0, "неуспех обязан быть виден коду возврата")

    def test_failed_queue_copy_does_not_cancel_the_snapshot(self) -> None:
        code, called = self.run_pass(failing_module="ops.backup")

        self.assertIn(
            "ops.snapshot",
            called,
            msg=(
                "Снимок не сделан из-за отказа копии очереди — задачи "
                "независимы, и связывать их нечем"
            ),
        )
        self.assertNotEqual(code, 0)


class CommandLineTests(unittest.TestCase):
    def test_bad_url_exits_with_usage_code(self) -> None:
        # 64 — то же, чем отвечает проверка готовности: «неверно
        # вызвано», а не «кластер отказал».
        complaint = StringIO()

        with redirect_stderr(complaint):
            code = main(["--url", "ftp://nowhere:9200"])

        self.assertEqual(code, 64)
        self.assertIn("SNAPSHOT_ERROR=", complaint.getvalue())


if __name__ == "__main__":
    unittest.main()
