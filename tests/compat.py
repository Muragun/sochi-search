"""
Совместимость тестов с Python 3.8.

Проект заявляет поддержку 3.8 и на рабочем сервере работает именно на нём.
При этом сам набор тестов на 3.8 не запускался: три проверки пользовались
`ast.unparse` (появился в 3.9) и `sys.stdlib_module_names` (в 3.10). На
машине разработчика с 3.11 они проходили, на сервере падали — то есть
единственный способ проверить систему перед выкладкой на той же версии,
на которой она работает, был сломан.

Здесь обе возможности заменяются на то, что есть в 3.8. Замены нарочно
скромные: они должны отвечать на тот же вопрос, а не воспроизводить
поведение оригиналов целиком.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import sysconfig


def unparse(node: ast.AST) -> str:
    """
    Текстовое представление узла — достаточное, чтобы искать в нём имена.

    `ast.unparse` восстанавливает исходный код; здесь это избыточно.
    Проверки, ради которых он вызывался, спрашивают лишь «встречается ли
    в выражении имя `require_admin`» и «наследуется ли класс от
    `BaseModel`». Для такого вопроса хватает перечисления имён, атрибутов
    и строковых констант.
    """

    if hasattr(ast, "unparse"):
        return ast.unparse(node)

    parts: list[str] = []

    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            parts.append(item.id)
        elif isinstance(item, ast.Attribute):
            parts.append(item.attr)
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            parts.append(item.value)

    return " ".join(parts)


def standard_library_modules() -> set:
    """
    Имена модулей стандартной библиотеки.

    В 3.10 для этого есть `sys.stdlib_module_names`. В 3.8 его нет, и
    `getattr(sys, ..., ())` возвращал пустое множество — из-за чего
    проверка объявленных зависимостей считала незаявленными все сорок
    стандартных модулей, от `json` до `sqlite3`.

    Список собирается по каталогу стандартной библиотеки: так он верен
    для той версии, на которой запущен, и не устаревает при обновлении.
    """

    names = set(sys.builtin_module_names)

    declared = getattr(sys, "stdlib_module_names", None)

    if declared:
        return names | set(declared)

    stdlib = sysconfig.get_paths().get("stdlib")

    if not stdlib:
        return names

    root = pathlib.Path(stdlib)

    if not root.is_dir():
        return names

    for entry in root.iterdir():
        if entry.suffix == ".py":
            names.add(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            names.add(entry.name)

    # Модули-расширения: fcntl, _socket и прочие лежат отдельно.
    dynamic_load = root / "lib-dynload"

    if dynamic_load.is_dir():
        for entry in dynamic_load.iterdir():
            names.add(entry.name.split(".", 1)[0])

    return names
