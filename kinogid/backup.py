"""Согласованная копия SQLite, включая данные из WAL: python -m kinogid.backup путь."""

import sqlite3
import sys
from pathlib import Path

from .config import Config


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python -m kinogid.backup backups/kinogid.sqlite3")
    config = Config.from_env()
    source_path = config.db_path.resolve()
    destination = Path(sys.argv[1]).resolve()
    if source_path == destination:
        raise SystemExit("Путь копии должен отличаться от рабочей базы")
    if not source_path.is_file():
        raise SystemExit("Рабочая база ещё не создана")
    if destination.exists():
        raise SystemExit("Файл копии уже существует. Выберите новое имя.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    print("Резервная копия сохранена:", destination)


if __name__ == "__main__":
    main()
