"""Перезапуск CLI интерпретатором агента: метка — аргумент, а не окружение.

Проверяется ровно то, что сломалось в деле `d_9ea4d348`: метка перезапуска
наследовалась дочерними процессами, и этап `check`, внутри которого зовут
`bin/duty`, падал кодом 2 «интерпретатор агента не найден» на ровном месте.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

CLI_PATH = Path(__file__).resolve().parent.parent / "dashboard" / "cli.py"


def load_cli():
    """Загрузить `dashboard/cli.py` так же, как это делает `bin/duty`."""
    spec = importlib.util.spec_from_file_location("hermes_duty_cli_test", CLI_PATH)
    assert spec is not None and spec.loader is not None, f"CLI не загрузился: {CLI_PATH}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cli():
    return load_cli()


def test_mark_is_an_argument_not_environment(cli, monkeypatch) -> None:
    """Метки в окружении нет вовсе, а в argv после exec — есть."""
    for name in list(os.environ):
        if "REEXEC" in name:
            monkeypatch.delenv(name, raising=False)
    assert not hasattr(cli, "REEXEC_MARK"), "метка снова читается из окружения"
    assert cli.REEXEC_ARG.startswith("--")


def test_child_does_not_inherit_the_mark(cli, monkeypatch) -> None:
    """Дочерний `bin/duty` зовётся из этапа и не видит метки родителя.

    Раньше здесь стояла переменная окружения: этап дела наследовал её от движка и
    отказывался работать. Теперь наследоваться нечему — метка живёт в argv одного
    exec.
    """
    seen: dict[str, object] = {}

    def fake_execv(candidate: str, argv: list[str]) -> None:  # noqa: ARG001
        seen["argv"] = list(argv)
        seen["env"] = {name: value for name, value in os.environ.items() if "REEXEC" in name}

    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None if name == "fastapi" else object())
    monkeypatch.setattr(cli.os, "execv", fake_execv)
    monkeypatch.setattr(cli, "_agent_pythons", lambda: [sys.executable + ".other"])
    with pytest.raises(SystemExit):
        cli._ensure_interpreter(["spec", "check", "deals/x.yaml"])

    assert seen["argv"] == [sys.executable + ".other", str(CLI_PATH), cli.REEXEC_ARG, "spec", "check", "deals/x.yaml"]
    assert seen["env"] == {}, f"метка утекла в окружение: {seen['env']}"


def test_mark_in_argv_stops_the_second_restart(cli, monkeypatch, capsys) -> None:
    """Второй заход с меткой не перезапускается, а честно отказывает кодом 2."""
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None if name == "fastapi" else object())
    monkeypatch.setattr(cli.os, "execv", lambda *a, **k: pytest.fail("второй перезапуск"))
    with pytest.raises(SystemExit) as exit_info:
        cli._ensure_interpreter([cli.REEXEC_ARG, "spec", "check", "deals/x.yaml"])
    assert exit_info.value.code == cli.EXIT_SOURCE
    assert "интерпретатор агента не найден" in capsys.readouterr().err


def test_main_does_not_see_the_mark(cli, monkeypatch) -> None:
    """Команда разбирается без метки: иначе argparse увидел бы лишний аргумент."""
    parsed: dict[str, object] = {}

    monkeypatch.setattr(cli, "_ensure_interpreter", lambda argv: None)
    monkeypatch.setattr(cli, "build_parser", lambda: _Parser(parsed))
    monkeypatch.setattr(sys, "argv", ["duty", cli.REEXEC_ARG, "spec", "check", "deals/x.yaml"])

    assert cli.main(None) == cli.EXIT_OK
    assert parsed["words"] == ["spec", "check", "deals/x.yaml"]


class _Parser:
    """Минимальный разборщик: запоминает слова и не мешает вернуть код 0."""

    def __init__(self, seen: dict[str, object]) -> None:
        self.seen = seen

    def parse_args(self, words):
        self.seen["words"] = list(words)
        return type("Args", (), {"command": None})()

    def print_help(self) -> None:
        return None
