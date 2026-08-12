from __future__ import annotations

import json

from .config import settings
from .state_db import CrawlStateDatabase


def main() -> None:
    database = CrawlStateDatabase(
        settings.state_db_path
    )

    database.initialize()

    statistics = database.get_statistics()

    print(
        json.dumps(
            {
                "database": str(
                    settings.state_db_path
                ),
                **statistics,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
