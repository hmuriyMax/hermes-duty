"""Прогон задачи через плагин (`bin/duty demo`) — контракт, а не скриншот.

Тест держит то, ради чего прогон существует: он проходит дело по спеке и
проверяет, что учёт и поверхности говорят правду. Если прогон перестанет
сходиться (вопрос не уводит в `wait_human`, поздний ответ воскрешает закрытое
дело, `kanban.db` изменился), `run_demo` бросит `DemoError` — и этот тест упадёт
вместе с ним, без отдельного списка правил.

Песочный корень подставляется явно (`tmp_path`): живой `~/.hermes` открывается
только на чтение DDL, а `HERMES_HOME` прогон восстанавливает сам.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str) -> object:
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def api() -> Any:
    return load_module("hermes_duty_plugin_api", "plugin_api.py")


@pytest.fixture(scope="module")
def demo(api: Any) -> Any:
    # demo берёт api извне: тот же модуль, что у роутов, второй копии нет.
    return load_module("hermes_duty_demo", "demo.py")


@pytest.fixture()
def report(api: Any, demo: Any, tmp_path: Path) -> dict:
    home = tmp_path / "hermes-demo"
    return demo.run_demo(api, home=home, live=Path.home() / ".hermes", keep=True)


def test_demo_passes_all_checks(report: dict) -> None:
    assert report["ok"] is True
    failed = [check for check in report["checks"] if not check["ok"]]
    assert failed == [], failed
    assert len(report["checks"]) >= 7


def test_demo_writes_own_ledger_only(report: dict) -> None:
    """Журнал дела: created первым, terminated completed, вопрос и ответ — врозь."""
    kinds = [event["kind"] for event in report["ledger"]]
    assert kinds[0] == "created"
    assert kinds.count("question_asked") == 2
    assert kinds.count("question_answered") == 2
    assert kinds[-1] == "question_answered", "поздний ответ — последний факт таймлайна"


def test_demo_question_reaches_pool_with_deal_key(report: dict) -> None:
    questions = report["pool"]["questions"]
    assert len(questions) == 2
    deal = report["deal"]
    for question in questions:
        # session_key — тот, по которому sync_answers найдёт факт: иначе ответ
        # «уйдёт в другое дело» (класс B1 из ревью `ask()`).
        assert question["session_key"] == f"t_{deal}"
        assert question["status"] == "answered"
        assert question["kind"] == "decision"


def test_demo_does_not_revive_closed_deal(report: dict) -> None:
    """Закрытое дело не появляется в срезе и не имеет состояния ожидания."""
    assert report["deal"] not in report["now"]["ids"]


def test_demo_report_is_json_serialisable(report: dict) -> None:
    payload = json.loads(json.dumps(report, ensure_ascii=False))
    assert payload["deal"] == report["deal"]
    assert payload["engine_todo"], "граница «чего ещё нет» обязана быть в отчёте"


def test_demo_restores_hermes_home(api: Any, demo: Any, tmp_path: Path) -> None:
    before = os.environ.get("HERMES_HOME")
    demo.run_demo(api, home=tmp_path / "sand", live=Path.home() / ".hermes")
    assert os.environ.get("HERMES_HOME") == before


def test_demo_cleans_up_sandbox(demo: Any, api: Any, tmp_path: Path) -> None:
    """Без `--keep` песочница удаляется: прогон не оставляет мусор в TMPDIR."""
    home = tmp_path / "sandbox-clean"
    report = demo.run_demo(api, home=home, live=Path.home() / ".hermes")
    assert report["keep"] is False
    assert not home.exists()


def test_demo_leaves_live_pool_and_kanban_untouched(api: Any, demo: Any, tmp_path: Path) -> None:
    """Живые БД не меняются: прогон только читает из них DDL.

    Это компенсация за исключение `demo.py` из структурного теста инварианта
    «в пул пишет только ask()»: исключение узкое, поэтому поведение проверяется
    напрямую — mtime и размер живых файлов до и после.
    """
    live = Path.home() / ".hermes"
    watched = [live / "messaging" / "pool.db", live / "kanban" / "boards" / "hermes-duty" / "kanban.db"]
    before = [(path.stat().st_mtime_ns, path.stat().st_size) for path in watched if path.exists()]
    demo.run_demo(api, home=tmp_path / "sand-live", live=live)
    after = [(path.stat().st_mtime_ns, path.stat().st_size) for path in watched if path.exists()]
    assert before == after


def test_demo_refuses_live_root_as_sandbox(demo: Any, tmp_path: Path) -> None:
    """Песочница не может быть живым корнем — иначе прогон писал бы в боевой пул."""
    live = tmp_path / "live"
    live.mkdir()
    with pytest.raises(demo.DemoError):
        demo.build_sandbox(live, live)
