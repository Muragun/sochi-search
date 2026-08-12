"""
Проверка связок: модуль можно написать правильно и не подключить.

В ходе подготовки 2.5.0 это случилось дважды. Новый построитель запроса
`app_v2/search_query.py` был покрыт тестами, но `search_service` продолжал
использовать прежний. Новый движок OCR `crawler_v2/pdf_ocr_engine.py` был
написан и не вызывался ниоткуда. Ни один модульный тест этого не замечал:
каждый проверял свой модуль, а не то, что конвейер его использует.

Дополнительно сверяются аргументы командной строки из systemd-юнитов с
тем, что объявляет argparse: расхождение здесь приводит к падению службы
при первом же запуске.

Запуск:

    python -m unittest tests.test_wiring -v
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]

# Модули, которые обязаны быть достижимы из рабочего кода. Ключ — модуль,
# значение — где его использование ожидается.
REQUIRED_WIRING = {
    "app_v2.search_query": ("app_v2/search_service.py",),
    "crawler_v2.pdf_ocr_engine": ("crawler_v2/content_processing.py",),
    "crawler_v2.pdf_ocr_image": ("crawler_v2/pdf_ocr_engine.py",),
}

# Модули, про которые известно, что они не используются. Список
# намеренно явный: он не даёт тесту молча пропустить новые такие случаи.
#
# attachment_links_db — слой доступа к таблице attachment_links. Ни точкой
# входа, ни импортируемым модулем не является: url_deduplicate работает с
# той же таблицей напрямую через SQL. 342 строки мёртвого кода, оставлены
# как есть, чтобы не менять поведение перед установкой.
KNOWN_UNUSED = {
    "crawler_v2.attachment_links_db",
}

# Модули-точки входа: у них нет импортёров по определению.
ENTRY_POINTS = {
    "crawler_v2.incremental_worker",
    "crawler_v2.discovery_worker",
    "crawler_v2.discovery_worker_v2",
    "crawler_v2.content_cleanup_worker",
    "crawler_v2.publication_date_worker",
    "crawler_v2.legal_backfill_worker",
    "crawler_v2.pdf_extraction_worker",
    "crawler_v2.pdf_page_ocr_worker",
    "crawler_v2.pdf_ocr_state",
    "crawler_v2.bootstrap_state",
    "crawler_v2.data_maintenance",
    "crawler_v2.url_audit",
    "crawler_v2.url_deduplicate",
    "crawler_v2.url_duplicate_verify",
    "crawler_v2.legal_audit",
    "crawler_v2.html_structure_audit",
    "crawler_v2.legal_backfill_report",
    "crawler_v2.worker_report",
    "crawler_v2.state_stats",
    "app_v2.main",
    "ops.backup",
    "ops.verify_backup",
    "ops.wait_for_elasticsearch",
    "ops.validate_elasticsearch",
    "ops.reindex_v3",
}


def python_files() -> list[Path]:
    return [
        path
        for package in ("crawler_v2", "app_v2", "ops")
        for path in (SOURCE / package).glob("*.py")
        if path.name != "__init__.py"
    ]


def module_name(path: Path) -> str:
    return f"{path.parent.name}.{path.stem}"


def referenced_names(path: Path) -> set[str]:
    """Имена модулей, на которые ссылается файл, включая ленивые импорты."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.lstrip("."))
            if node.level and node.module:
                names.add(f"{path.parent.name}.{node.module}")
            elif node.level:
                for alias in node.names:
                    names.add(f"{path.parent.name}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)

    return names


class ModuleWiringTests(unittest.TestCase):
    def test_required_modules_are_actually_used(self) -> None:
        for module, expected_users in REQUIRED_WIRING.items():
            short = module.split(".", 1)[1]
            used_by = []

            for path in python_files():
                if module_name(path) == module:
                    continue

                names = referenced_names(path)

                if module in names or short in names:
                    used_by.append(
                        f"{path.parent.name}/{path.name}"
                    )

            self.assertTrue(
                used_by,
                msg=(
                    f"Модуль {module} не импортируется ниоткуда — "
                    "он написан, но не подключён к конвейеру"
                ),
            )

            for expected in expected_users:
                self.assertIn(
                    expected,
                    used_by,
                    msg=(
                        f"{module} должен использоваться в {expected}, "
                        f"а используется только в {used_by}"
                    ),
                )

    def test_no_orphan_modules(self) -> None:
        """Каждый модуль либо точка входа, либо кем-то импортируется."""
        all_modules = {module_name(p): p for p in python_files()}
        imported: set[str] = set()

        for path in python_files():
            names = referenced_names(path)

            for candidate in all_modules:
                short = candidate.split(".", 1)[1]

                if candidate in names or short in names:
                    if module_name(path) != candidate:
                        imported.add(candidate)

        orphans = sorted(
            set(all_modules) - imported - ENTRY_POINTS - KNOWN_UNUSED
        )

        self.assertEqual(
            orphans,
            [],
            msg=(
                "Эти модули никем не используются и не заявлены как "
                f"точки входа: {orphans}"
            ),
        )


class SystemdContractTests(unittest.TestCase):
    """Флаги из юнитов должны существовать в argparse."""

    @staticmethod
    def declared_flags(module_path: Path) -> set[str]:
        source = module_path.read_text(encoding="utf-8")
        return set(
            re.findall(
                r'add_argument\(\s*["\'](--[a-z0-9-]+)["\']',
                source,
            )
        )

    def unit_commands(self):
        for unit in (SOURCE / "systemd").rglob("*"):
            if not unit.is_file():
                continue

            text = unit.read_text(encoding="utf-8")

            for line in re.findall(
                r"ExecStart[^=]*=(.+?)(?=\n\[|\n[A-Z][a-zA-Z]*=|\Z)",
                text,
                flags=re.S,
            ):
                yield unit, " ".join(line.split())

    def test_unit_flags_exist_in_worker(self) -> None:
        checked = 0

        for unit, command in self.unit_commands():
            match = re.search(r"-m\s+([a-z_]+)\.([a-z_]+)", command)

            if not match:
                continue

            module_path = (
                SOURCE / match.group(1) / f"{match.group(2)}.py"
            )

            if not module_path.is_file():
                continue

            declared = self.declared_flags(module_path)

            if not declared:
                continue

            used = set(re.findall(r"(?<!\w)(--[a-z0-9-]+)", command))
            # Флаги самого flock, а не модуля.
            used -= {"--nonblock", "--conflict-exit-code"}

            missing = used - declared
            checked += 1

            self.assertEqual(
                missing,
                set(),
                msg=(
                    f"{unit.name} передаёт {sorted(missing)} модулю "
                    f"{match.group(1)}.{match.group(2)}, но такие "
                    "аргументы не объявлены — служба упадёт при старте"
                ),
            )

        self.assertGreater(
            checked,
            0,
            msg="Не найдено ни одной команды для проверки",
        )

    def test_batch_flags_are_declared(self) -> None:
        declared = self.declared_flags(
            SOURCE / "crawler_v2" / "incremental_worker.py"
        )

        for flag in (
            "--batch",
            "--worker-slot",
            "--batch-deadline-seconds",
            "--batch-documents",
            "--pdf-ocr-backfill",
        ):
            self.assertIn(flag, declared)


class OcrEngineWiringTests(unittest.TestCase):
    def test_pipeline_can_select_tesseract_engine(self) -> None:
        source = (
            SOURCE / "crawler_v2" / "content_processing.py"
        ).read_text(encoding="utf-8")

        self.assertIn("_ocr_page_with_tesseract", source)
        self.assertIn("PDF_OCR_ENGINE_TESSERACT", source)

    def test_engine_setting_matches_pipeline_constant(self) -> None:
        from crawler_v2.config import settings

        pipeline = (
            SOURCE / "crawler_v2" / "content_processing.py"
        ).read_text(encoding="utf-8")

        match = re.search(
            r'PDF_OCR_ENGINE_TESSERACT\s*=\s*"([^"]+)"',
            pipeline,
        )

        self.assertIsNotNone(match)
        self.assertEqual(
            settings.pdf_ocr_engine,
            match.group(1),
            msg=(
                "Значение CRAWL_PDF_OCR_ENGINE по умолчанию не совпадает "
                "с константой конвейера: новый движок не включится"
            ),
        )

    def test_invert_retry_is_disabled(self) -> None:
        # Замер на сервере: 12,24 с против 8,74 с при том же тексте.
        source = (
            SOURCE / "crawler_v2" / "pdf_ocr_engine.py"
        ).read_text(encoding="utf-8")

        self.assertIn("tessedit_do_invert=0", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class OcrCoverageSemanticsTests(unittest.TestCase):
    """
    Охват первого прохода считается по попыткам, а не по номеру страницы.

    Ограничение вида `page_index < N` выглядит эквивалентным, но на
    смешанном документе ведёт себя иначе: если первые листы имеют
    текстовый слой, а скан начинается с третьего, в окне охвата
    распознавать нечего, а всё нуждающееся в OCR лежит за его границей.
    На рабочем сервере это дало «PDF OCR страниц: 0, отложено в фон: 35»
    и максимум одну распознанную страницу за три часа.
    """

    def setUp(self) -> None:
        self.source = (
            SOURCE / "crawler_v2" / "content_processing.py"
        ).read_text(encoding="utf-8")

        start = self.source.index("within_coverage = (")
        self.clause = self.source[start:start + 260]

    def test_coverage_counts_attempts(self) -> None:
        self.assertIn("ocr_attempted_pages", self.clause)

    def test_coverage_does_not_use_page_index(self) -> None:
        self.assertNotIn(
            "page_index <",
            self.clause,
            msg=(
                "Охват снова ограничен номером страницы: смешанные "
                "документы перестанут распознаваться"
            ),
        )

    def test_zero_means_no_limit(self) -> None:
        self.assertIn("coverage_pages <= 0", self.clause)


class ExtractionVersionTests(unittest.TestCase):
    def test_version_distinguishes_new_engine(self) -> None:
        # Прежний движок помечал результат как pdf-ocr-v5. Если не поднять
        # строку, фоновый проход считает такие документы готовыми и
        # никогда их не переделает.
        from crawler_v2.pdf_ocr import PDF_EXTRACTION_VERSION

        self.assertNotEqual(PDF_EXTRACTION_VERSION, "pdf-ocr-v5")
        self.assertTrue(
            PDF_EXTRACTION_VERSION.startswith("pdf-ocr-v")
        )


class OcrScheduleTests(unittest.TestCase):
    """
    Каждый слот OCR должен иметь собственный таймер.

    Общий `.target` после первого запуска остаётся активным, а повторный
    старт уже активного target ничего не делает. На рабочем сервере это
    выглядело так: слоты отработали дважды (при `enable --now` и при
    первой активации target), после чего расписание молчало, а в журнале
    не появлялось ни одной строки.
    """

    def timer_files(self) -> list[Path]:
        return sorted(
            (SOURCE / "systemd").glob("sochi-search-pdf-ocr*")
        )

    def test_no_target_based_schedule(self) -> None:
        for path in self.timer_files():
            if path.suffix != ".timer":
                continue

            text = path.read_text(encoding="utf-8")

            # Проверяется именно директива Unit=, а не любое упоминание:
            # `WantedBy=timers.target` в секции [Install] законен.
            activated = re.findall(
                r"^\s*Unit\s*=\s*(\S+)",
                text,
                flags=re.M,
            )

            for name in activated:
                self.assertFalse(
                    name.endswith(".target"),
                    msg=(
                        f"{path.name} активирует {name}: повторные "
                        "срабатывания не будут запускать слоты"
                    ),
                )

    def test_template_timer_exists(self) -> None:
        names = {path.name for path in self.timer_files()}

        self.assertIn("sochi-search-pdf-ocr@.timer", names)

    def test_timer_activates_matching_slot(self) -> None:
        text = (
            SOURCE / "systemd" / "sochi-search-pdf-ocr@.timer"
        ).read_text(encoding="utf-8")

        self.assertIn("Unit=sochi-search-pdf-ocr@%i.service", text)


class ComposeEnvironmentTests(unittest.TestCase):
    """
    Каждый сервис в compose должен получать пути контейнера.

    YAML-слияние `<<: *app` заменяет блок `environment` целиком, а не
    дополняет его. Если сервис объявляет хотя бы одну свою переменную,
    переменные якоря пропадают и подхватываются значения из .env — с
    путями хост-системы. У API это приводило к привязке на 127.0.0.1
    внутри контейнера, то есть к недоступности снаружи.
    """

    def setUp(self) -> None:
        self.path = SOURCE / "docker-compose.app.yml"
        self.text = self.path.read_text(encoding="utf-8")

    def test_compose_exists(self) -> None:
        self.assertTrue(self.path.is_file())

    def test_every_environment_block_includes_anchor(self) -> None:
        blocks = re.findall(
            r"^(\s+)environment:\s*\n((?:\1\s+.*\n|\s*\n)*)",
            self.text,
            flags=re.M,
        )

        self.assertGreater(len(blocks), 1, msg="блоки environment не найдены")

        for indent, body in blocks:
            self.assertIn(
                "<<: *container-env",
                body,
                msg=(
                    "Блок environment без якоря container-env: сервис "
                    "потеряет пути внутри контейнера\n" + body[:200]
                ),
            )

    def test_api_binds_all_interfaces_inside_container(self) -> None:
        self.assertIn('APP_HOST: "0.0.0.0"', self.text)

    def test_api_port_is_not_published_to_world(self) -> None:
        # Проброс наружу должен быть на конкретный адрес.
        self.assertIn("${API_BIND:-127.0.0.1}", self.text)

    def test_entrypoint_handles_every_command(self) -> None:
        entrypoint = (
            SOURCE / "docker" / "entrypoint.sh"
        ).read_text(encoding="utf-8")

        commands = set(
            re.findall(r'command:\s*\["([a-z-]+)"\]', self.text)
        )

        self.assertTrue(commands)

        for command in commands:
            self.assertIn(
                f"{command})",
                entrypoint,
                msg=f"entrypoint не знает роль {command}",
            )


class ComposeNetworkTests(unittest.TestCase):
    """
    Elasticsearch и приложение должны стоять в одной сети Docker.

    Сервис без ключа `networks` попадает в сеть `default`, а приложение
    объявляет `search`. Разные сети — и имя `elasticsearch` из `ES_URL`
    не разрешается: контейнеры ждут кластер, который запущен и здоров, но
    им не виден. Симптом при этом выглядит как отказ Elasticsearch, а не
    как ошибка в compose, поэтому проверка вынесена в тест.
    """

    def setUp(self) -> None:
        self.base = (
            SOURCE / "docker-compose.yml"
        ).read_text(encoding="utf-8")
        self.app = (
            SOURCE / "docker-compose.app.yml"
        ).read_text(encoding="utf-8")

    def test_elasticsearch_joins_application_network(self) -> None:
        service = self.base.split("elasticsearch:", 1)[-1]
        declaration, _, _ = service.partition("\nvolumes:")

        self.assertIn(
            "networks:",
            declaration,
            msg="у elasticsearch нет блока networks — попадёт в default",
        )
        self.assertIn("- search", declaration)

    def test_both_files_name_the_same_network(self) -> None:
        for text, name in (
            (self.base, "docker-compose.yml"),
            (self.app, "docker-compose.app.yml"),
        ):
            self.assertIn(
                "name: sochi-search",
                text,
                msg=f"{name}: сеть search не закреплена по имени",
            )

    def test_elasticsearch_port_stays_on_loopback(self) -> None:
        # У кластера отключена собственная авторизация: всякий, кто
        # дотянется до 9200, читает и удаляет индекс.
        self.assertIn('"127.0.0.1:9200:9200"', self.base)
        self.assertNotIn('"0.0.0.0:9200', self.base)
        self.assertNotIn('"9200:9200"', self.base)

    def test_snapshot_repository_is_configured(self) -> None:
        # Без path.repo и тома перенос индекса на другую машину
        # невозможен: снимок просто некуда положить.
        self.assertIn("path.repo: /snapshots", self.base)
        self.assertIn("essnapshots:/snapshots", self.base)


class RequirementsTests(unittest.TestCase):
    """
    Каждая сторонняя библиотека, которую импортирует код, объявлена в
    requirements.txt.

    Случай, ради которого написан тест: форма входа объявляет параметры
    через `Form(...)`, а для этого FastAPI требует пакет
    `python-multipart`. Проверку он делает при регистрации обработчика,
    то есть при импорте модуля, — без пакета API не запускается вовсе.
    Ни один тест этого не замечал: в тестах роутер собирается заглушкой.
    """

    # Модули стандартной библиотеки и собственные пакеты проекта.
    LOCAL = {"app", "app_v2", "crawler_v2", "ops", "tests"}

    # Имя пакета в requirements не всегда совпадает с именем модуля.
    DISTRIBUTION_NAMES = {
        "bs4": "beautifulsoup4",
        "docx": "python-docx",
        "dotenv": "python-dotenv",
        "fitz": "pymupdf",
        "PIL": "Pillow",
        "yaml": "PyYAML",
    }

    def setUp(self) -> None:
        self.requirements = (
            SOURCE / "requirements.txt"
        ).read_text(encoding="utf-8").casefold()

    def declared(self, name: str) -> bool:
        package = self.DISTRIBUTION_NAMES.get(name, name)

        return re.search(
            rf"^{re.escape(package.casefold())}\b",
            self.requirements,
            flags=re.M,
        ) is not None

    def imported_modules(self) -> set[str]:
        found: set[str] = set()

        for path in sorted(SOURCE.glob("*/*.py")):
            if path.parts[-2] not in self.LOCAL - {"tests"}:
                continue

            tree = ast.parse(path.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(
                        alias.name.split(".")[0] for alias in node.names
                    )
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    if node.module:
                        found.add(node.module.split(".")[0])

        return found

    def test_form_handlers_declare_python_multipart(self) -> None:
        uses_form = any(
            "Form(" in path.read_text(encoding="utf-8")
            for path in (SOURCE / "app_v2").glob("*.py")
        )

        if not uses_form:
            self.skipTest("Form(...) в коде не используется")

        self.assertTrue(
            self.declared("python-multipart"),
            msg=(
                "Обработчик объявляет Form(...), но python-multipart нет "
                "в requirements.txt — FastAPI не поднимет API"
            ),
        )

    def test_every_third_party_import_is_declared(self) -> None:
        import sys

        standard = set(getattr(sys, "stdlib_module_names", ()))
        missing = sorted(
            name
            for name in self.imported_modules()
            if name not in standard
            and name not in self.LOCAL
            and not name.startswith("_")
            and not self.declared(name)
        )

        self.assertEqual(
            missing,
            [],
            msg=f"импортируются, но не объявлены в requirements: {missing}",
        )


class Python38RuntimeAnnotationTests(unittest.TestCase):
    """
    Аннотации, которые вычисляются во время выполнения, должны работать
    на Python 3.8 — версии рабочего сервера.

    `from __future__ import annotations` откладывает вычисление, но не
    отменяет его: FastAPI разбирает подписи обработчиков, а pydantic —
    поля моделей, и обе библиотеки передают строку аннотации в eval.
    На 3.8 нет ни `str | None` (PEP 604), ни `dict[str, Any]` (PEP 585),
    поэтому такая подпись роняет импорт модуля, а вместе с ним и весь
    API — не первый запрос, а запуск службы.

    Внутри обычных функций эти же формы безопасны: их никто не
    вычисляет. Поэтому проверяются только обработчики и модели.
    """

    BUILTIN_GENERICS = {
        "dict", "list", "tuple", "set", "frozenset", "type",
    }

    def unsafe_parts(self, node: ast.AST) -> list[str]:
        found: list[str] = []

        for item in ast.walk(node):
            if isinstance(item, ast.BinOp) and isinstance(item.op, ast.BitOr):
                found.append(ast.unparse(item))

            if (
                isinstance(item, ast.Subscript)
                and isinstance(item.value, ast.Name)
                and item.value.id in self.BUILTIN_GENERICS
            ):
                found.append(ast.unparse(item))

        return found

    @staticmethod
    def is_route_handler(node: ast.AST) -> bool:
        return any(
            "router" in ast.unparse(decorator)
            or ast.unparse(decorator).startswith("app.")
            for decorator in node.decorator_list
        )

    def test_route_handlers_and_models_avoid_new_syntax(self) -> None:
        problems: list[str] = []

        for path in sorted((SOURCE / "app_v2").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and self.is_route_handler(node):
                    found = []

                    for argument in (
                        list(node.args.args) + list(node.args.kwonlyargs)
                    ):
                        if argument.annotation:
                            found += self.unsafe_parts(argument.annotation)

                    if node.returns:
                        found += self.unsafe_parts(node.returns)

                    if found:
                        problems.append(
                            f"{path.name}:{node.lineno} обработчик "
                            f"{node.name}: {sorted(set(found))}"
                        )

                if isinstance(node, ast.ClassDef) and any(
                    "BaseModel" in ast.unparse(base) for base in node.bases
                ):
                    found = []

                    for statement in node.body:
                        if isinstance(statement, ast.AnnAssign):
                            found += self.unsafe_parts(statement.annotation)

                    if found:
                        problems.append(
                            f"{path.name}:{node.lineno} модель "
                            f"{node.name}: {sorted(set(found))}"
                        )

        self.assertEqual(
            problems,
            [],
            msg=(
                "Эти аннотации вычисляются во время выполнения и упадут "
                "на Python 3.8. Замена: Optional[X] и Dict[K, V] из "
                "typing.\n" + "\n".join(problems)
            ),
        )


class Python38SyntaxTests(unittest.TestCase):
    """
    Весь код должен разбираться синтаксисом Python 3.8.

    В контейнере стоит 3.12, но рабочий сервер остаётся на 3.8, и код
    один и тот же. Ошибка синтаксиса обнаруживается только при импорте
    модуля — то есть при запуске службы, а не при сборке.
    """

    def test_every_module_parses_as_python_38(self) -> None:
        problems: list[str] = []

        for path in sorted(SOURCE.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue

            try:
                ast.parse(
                    path.read_text(encoding="utf-8"),
                    feature_version=(3, 8),
                )
            except SyntaxError as error:
                problems.append(
                    f"{path.relative_to(SOURCE)}:{error.lineno} {error.msg}"
                )

        self.assertEqual(problems, [], msg="\n".join(problems))
