"""Общие правила прогона: маркер `live` и его пропуск по умолчанию.

Живой прогон движка поднимает настоящий процесс `hermes chat` и настоящую модель:
он проверяет связку целиком, но в обычном прогоне тестов не нужен и стоит денег и
секунд. Поэтому тест помечается `@pytest.mark.live` и по умолчанию пропускается;
включается руками: `pytest -m live`.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: живой прогон с настоящим процессом hermes chat")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Пропустить `live` без `-m live` в отборе: молча не запускать модель."""
    selection = str(config.getoption("-m") or "")
    if "live" in selection:
        return
    skip = pytest.mark.skip(reason="live: включается руками — pytest -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
