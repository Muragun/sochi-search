"""
Создание базы очереди обхода, если её ещё нет.

Схема `crawl_urls` объявлена ровно в одном месте — `crawler_v2/state_db.py`,
а вызывался её `initialize()` только из разовых утилит `bootstrap_state`
и `state_stats`. На машине, развёрнутой с нуля, базы не появлялось: и
обход, и разбор очереди падали с «Таблица crawl_urls не найдена.
Сначала выполните импорт SQLite», хотя импортировать было нечего —
корпус собирается заново.

Скрипт идемпотентен: `CREATE TABLE IF NOT EXISTS` на существующей базе
ничего не меняет и не трогает данные. Поэтому его можно звать при каждом
старте контейнера и при каждом запуске deploy.sh.

    python -m ops.init_state
    python -m ops.init_state --database /app/data/crawl_state.sqlite3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler_v2.config import settings  # noqa: E402
from crawler_v2.state_db import CrawlStateDatabase  # noqa: E402


def ensure_state_database(database_path: Path) -> bool:
    """Создаёт схему. Возвращает True, если базы до этого не было."""

    existed = database_path.is_file()

    database_path.parent.mkdir(parents=True, exist_ok=True)
    CrawlStateDatabase(database_path).initialize()

    return not existed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Создать базу очереди обхода, если её нет"
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=settings.state_db_path,
    )
    arguments = parser.parse_args()

    created = ensure_state_database(arguments.database)

    if created:
        print(f"STATE_DB_CREATED path={arguments.database}")
    else:
        print(f"STATE_DB_READY path={arguments.database}")

    database = CrawlStateDatabase(arguments.database)
    print(f"STATE_DB_URLS count={database.count_urls()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
