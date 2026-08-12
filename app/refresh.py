from __future__ import annotations

from .create_index import create_index
from .indexer import run_index


if __name__ == "__main__":
    create_index(reset=False)
    result = run_index()
    print(result)
