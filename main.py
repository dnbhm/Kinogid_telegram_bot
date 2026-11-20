"""Запуск для сервисов с командой python main.py."""

import runpy


if __name__ == "__main__":
    runpy.run_module("kinogid", run_name="__main__")
