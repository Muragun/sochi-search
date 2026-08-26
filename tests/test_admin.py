"""
Тесты служебного раздела.

FastAPI в окружении проверки может отсутствовать, поэтому проверяется то,
что от него не зависит: описания задач, сборка команд, блокировки, и —
через разбор исходника — что раздел закрыт авторизацией, а роутер
подключён к приложению.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compat import requires_repository, unparse  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from app_v2.admin_tasks import (  # noqa: E402
    TASKS,
    TASKS_BY_KEY,
    TaskRunner,
)


@requires_repository
class TaskDefinitionTests(unittest.TestCase):
    def test_every_task_has_human_description(self) -> None:
        for task in TASKS:
            self.assertTrue(task.title.strip())
            self.assertGreater(
                len(task.description), 30,
                msg=f"{task.key}: описание слишком короткое",
            )

    def test_keys_are_unique(self) -> None:
        keys = [task.key for task in TASKS]

        self.assertEqual(len(keys), len(set(keys)))

    def test_lock_names_are_stable(self) -> None:
        """
        Задача из панели берёт тот же файл блокировки, что и
        соответствующий systemd-юнит, иначе запуск кнопкой столкнётся с
        запуском по расписанию.

        Базовые юниты (`sochi-search-worker.service` и другие) пока живут
        только на сервере: в пакете лежат лишь drop-in-файлы. Поэтому
        сверка идёт с явным списком, а не с содержимым каталога systemd.
        """

        expected = {
            "discovery": "discovery-worker.lock",
            "worker": "incremental-worker.lock",
            "publication_date": "publication-date-worker.lock",
            "content_cleanup": "content-cleanup-worker.lock",
            "reindex_url": "incremental-worker.lock",
            # У фонового распознавания намеренно свой файл: слоты
            # расписания заняты своими, панель работает третьим.
            "pdf_ocr": "pdf-ocr-worker-panel.lock",
        }

        for task in TASKS:
            self.assertEqual(
                task.lock_name,
                expected[task.key],
                msg=f"{task.key}: блокировка разошлась с расписанием",
            )

    def test_shipped_units_use_expected_locks(self) -> None:
        units = " ".join(
            path.read_text(encoding="utf-8")
            for path in (SOURCE / "systemd").rglob("*")
            if path.is_file()
        )

        # Проверяем только те, чьи юниты действительно есть в пакете.
        self.assertIn("discovery-worker.lock", units)
        self.assertIn("pdf-ocr-worker-%i.lock", units)

    def test_command_uses_flock_and_module(self) -> None:
        runner = TaskRunner()

        for task in TASKS:
            command = runner.build_command(task, url=None)

            self.assertEqual(command[0], "/usr/bin/flock")
            self.assertIn("--nonblock", command)
            self.assertIn(task.module, command)

    def test_url_task_receives_url(self) -> None:
        runner = TaskRunner()
        task = TASKS_BY_KEY["reindex_url"]

        command = runner.build_command(
            task, url="https://sochi.ru/upload/a.pdf"
        )

        self.assertIn("--url", command)
        self.assertIn("https://sochi.ru/upload/a.pdf", command)

    def test_plain_task_ignores_url(self) -> None:
        runner = TaskRunner()
        command = runner.build_command(
            TASKS_BY_KEY["worker"], url="https://sochi.ru/x"
        )

        self.assertNotIn("--url", command)

    def test_task_arguments_are_declared_in_module(self) -> None:
        """Аргументы задачи должны существовать в argparse её модуля."""

        for task in TASKS:
            module_path = SOURCE.joinpath(
                *task.module.split(".")
            ).with_suffix(".py")

            self.assertTrue(
                module_path.is_file(),
                msg=f"{task.key}: нет модуля {task.module}",
            )

            declared = set(
                re.findall(
                    r'add_argument\(\s*["\'](--[a-z0-9-]+)["\']',
                    module_path.read_text(encoding="utf-8"),
                )
            )
            used = {
                argument
                for argument in task.arguments
                if argument.startswith("--")
            }

            if task.accepts_url:
                used.add("--url")

            self.assertEqual(
                used - declared,
                set(),
                msg=(
                    f"{task.key}: панель передаёт {sorted(used - declared)}, "
                    f"а {task.module} таких аргументов не объявляет"
                ),
            )


class AdminSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (
            SOURCE / "app_v2" / "admin.py"
        ).read_text(encoding="utf-8")

    def test_every_route_requires_authentication(self) -> None:
        tree = ast.parse(self.source)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue

            is_route = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {"get", "post", "put", "delete"}
                for decorator in node.decorator_list
            )

            if not is_route:
                continue

            if node.name in {"login_page", "login_submit", "logout"}:
                # Страница входа и выход по определению открыты.
                continue

            defaults = [
                unparse(value)
                for value in node.args.defaults
            ]

            self.assertTrue(
                any("require_admin" in value for value in defaults),
                msg=(
                    f"Обработчик {node.name} не требует авторизации: "
                    "служебный раздел должен быть закрыт целиком"
                ),
            )

    def test_missing_password_closes_section(self) -> None:
        # Без заданного пароля раздел обязан отвечать ошибкой, а не
        # пускать всех подряд.
        self.assertIn("SERVICE_UNAVAILABLE", self.source)

    def test_comparison_is_constant_time(self) -> None:
        auth = (
            SOURCE / "app_v2" / "admin_auth.py"
        ).read_text(encoding="utf-8")

        self.assertIn("secrets.compare_digest", auth)

    def test_password_is_not_compared_in_plain_text(self) -> None:
        auth = (
            SOURCE / "app_v2" / "admin_auth.py"
        ).read_text(encoding="utf-8")

        self.assertIn("sha256", auth)

    def test_url_is_restricted_to_site(self) -> None:
        self.assertIn("http://sochi.ru", self.source)


class AdminWiringTests(unittest.TestCase):
    def test_router_is_included_in_application(self) -> None:
        main = (SOURCE / "app_v2" / "main.py").read_text(encoding="utf-8")

        self.assertIn("admin_router", main)
        self.assertIn("include_router", main)

    def test_panel_assets_exist(self) -> None:
        static = SOURCE / "app_v2" / "static"

        for name in ("admin.html", "admin.css", "admin.js"):
            self.assertTrue(
                (static / name).is_file(), msg=f"нет {name}"
            )

    def test_panel_avoids_inner_html(self) -> None:
        """
        Разметка собирается через createElement.

        Проверяется присваивание, а не упоминание: в самом файле есть
        комментарий о том, что innerHTML не используется.
        """

        script = (
            SOURCE / "app_v2" / "static" / "admin.js"
        ).read_text(encoding="utf-8")

        for pattern in (
            r"\.innerHTML\s*=",
            r"\.outerHTML\s*=",
            r"insertAdjacentHTML",
            r"document\.write",
        ):
            self.assertIsNone(
                re.search(pattern, script),
                msg=f"в admin.js найден {pattern}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SessionCookieTests(unittest.TestCase):
    """Подписанная кука: подделка, просрочка и смена пароля."""

    def setUp(self) -> None:
        import os

        self.previous = dict(os.environ)
        os.environ["ADMIN_USERNAME"] = "admin"
        os.environ["ADMIN_PASSWORD"] = "очень-длинный-пароль"
        os.environ.pop("ADMIN_PASSWORD_SHA256", None)
        os.environ.pop("ADMIN_SECRET_KEY", None)

    def tearDown(self) -> None:
        import os

        os.environ.clear()
        os.environ.update(self.previous)

    def test_correct_password_accepted(self) -> None:
        from app_v2.admin_auth import check_password

        self.assertTrue(check_password("admin", "очень-длинный-пароль"))

    def test_wrong_password_rejected(self) -> None:
        from app_v2.admin_auth import check_password

        self.assertFalse(check_password("admin", "другой"))

    def test_wrong_username_rejected(self) -> None:
        from app_v2.admin_auth import check_password

        self.assertFalse(check_password("root", "очень-длинный-пароль"))

    def test_valid_token_round_trip(self) -> None:
        from app_v2.admin_auth import issue_token, verify_token

        self.assertEqual(verify_token(issue_token("admin")), "admin")

    def test_tampered_token_rejected(self) -> None:
        from app_v2.admin_auth import issue_token, verify_token

        token = issue_token("admin")

        self.assertIsNone(verify_token(token[:-3] + "aaa"))

    def test_expired_token_rejected(self) -> None:
        import time

        from app_v2.admin_auth import issue_token, verify_token

        old = issue_token("admin", now=time.time() - 100_000)

        self.assertIsNone(verify_token(old))

    def test_password_change_invalidates_sessions(self) -> None:
        import os

        from app_v2.admin_auth import issue_token, verify_token

        token = issue_token("admin")
        os.environ["ADMIN_PASSWORD"] = "новый-пароль"

        self.assertIsNone(
            verify_token(token),
            msg="смена пароля обязана обесценивать выданные куки",
        )

    def test_unconfigured_auth_raises(self) -> None:
        import os

        from app_v2.admin_auth import AuthNotConfigured, check_password

        os.environ.pop("ADMIN_PASSWORD", None)
        os.environ.pop("ADMIN_PASSWORD_SHA256", None)

        with self.assertRaises(AuthNotConfigured):
            check_password("admin", "что угодно")

    def test_login_page_exists(self) -> None:
        self.assertTrue(
            (SOURCE / "app_v2" / "static" / "admin-login.html").is_file()
        )


class AdminUrlValidationTests(unittest.TestCase):
    """
    Адрес для переиндексации проверяется по имени узла, а не по началу
    строки.

    Прежняя проверка `url.startswith("http://sochi.ru")` пропускала
    `http://sochi.ru@evil.example/` (узел здесь evil.example),
    `https://sochi.ru.evil.example/` и `https://sochi.ruX`. Задача
    `reindex_url` скачала бы такой адрес от имени сервера и положила
    чужой документ в индекс.
    """

    def allowed_hosts(self):
        source = (SOURCE / "app_v2" / "admin.py").read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue

            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue

                if target.id != "ALLOWED_HOSTS":
                    continue

                value = node.value

                # frozenset({...}) — вызов, literal_eval его не берёт.
                if isinstance(value, ast.Call) and value.args:
                    value = value.args[0]

                return set(ast.literal_eval(value))

        self.fail("ALLOWED_HOSTS не найден в admin.py")

    def test_startswith_check_is_gone(self) -> None:
        source = (SOURCE / "app_v2" / "admin.py").read_text(encoding="utf-8")

        self.assertNotIn(
            'startswith(("http://sochi.ru"',
            source,
            msg="вернулась проверка по началу строки — она обходится",
        )
        self.assertIn("urlsplit", source)

    def test_hostname_check_rejects_lookalikes(self) -> None:
        hosts = self.allowed_hosts()

        rejected = [
            "http://sochi.ru@evil.example/x",
            "https://sochi.ru.evil.example/x",
            "https://sochi.ruX",
            "http://sochi.ru.attacker.tld/",
            "file:///etc/passwd",
            "http://127.0.0.1:9200/_all",
        ]

        for url in rejected:
            parts = urlsplit(url)
            passes = (
                parts.scheme in {"http", "https"}
                and (parts.hostname or "").lower() in hosts
            )
            self.assertFalse(passes, msg=f"проходит проверку: {url}")

        for url in ("http://sochi.ru/doc", "https://www.sochi.ru/a/b"):
            parts = urlsplit(url)
            self.assertIn((parts.hostname or "").lower(), hosts, msg=url)


class AdminAuthEncodingTests(unittest.TestCase):
    """
    Кириллический логин не должен ронять вход.

    `secrets.compare_digest` со строками требует ASCII: на логине
    «админ» он бросал TypeError, и форма входа отвечала пятисоткой
    вместо «неверный логин или пароль». Достижимо кем угодно без
    авторизации — достаточно ввести русское слово.
    """

    def setUp(self) -> None:
        self.saved = {
            key: os.environ.get(key)
            for key in ("ADMIN_USERNAME", "ADMIN_PASSWORD_SHA256")
        }

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_non_ascii_username_does_not_raise(self) -> None:
        from app_v2 import admin_auth

        os.environ["ADMIN_USERNAME"] = "админ"
        os.environ["ADMIN_PASSWORD_SHA256"] = hashlib.sha256(
            "секрет".encode("utf-8")
        ).hexdigest()

        self.assertTrue(admin_auth.check_password("админ", "секрет"))
        self.assertFalse(admin_auth.check_password("админ", "другой"))
        self.assertFalse(admin_auth.check_password("чужой", "секрет"))
        self.assertFalse(admin_auth.check_password("admin", "секрет"))
