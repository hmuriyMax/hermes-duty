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


# ------------------------------------------------- слияние полос (0.2.2)


def test_demo_merges_two_lanes_into_one_commit(report: dict) -> None:
    """Две полосы живут своими ветками, а слияние даёт ОДИН коммит с двумя родителями.

    Ради этого версия и делалась: полоса, объявленная параллельной, не двигает
    ветку дела, а fan-in собирает обе в один коммит, на котором стоят зависимые
    этапы (`d_c13d53da`: две полосы, зависимый этап не построился).
    """
    clean = report["merge"]["clean"]
    assert clean["deal_branch_before"] == "", "полоса коммит дела не двигает"
    assert clean["lanes"]["impl_a"] != clean["lanes"]["impl_b"]
    assert len(clean["parents"]) == 3, clean["parents"]
    assert set(clean["parents"][1:]) == set(clean["lanes"].values())
    assert clean["base_of_dependent"] == clean["merge"], "зависимый этап — на merge-коммите"
    assert clean["deal_branch"] == clean["merge"], "ветка дела на merge-коммите"
    assert clean["order"] == ["impl_b", "impl_a"], "порядок вливания — объявление в needs"
    assert clean["files"] == {"a.txt": "работа полосы a\n", "b.txt": "работа полосы b\n"}


def test_demo_raises_a_resolver_on_a_lane_conflict(report: dict) -> None:
    """Конфликт двух полос поднимает резолвера с обоими диффами, а не стратегию.

    Отказ первой попытки — исход «не смог»: движок называет файлы и сохраняет
    деревья полос; повтор разводит обе стороны и даёт merge-коммит с двумя
    родителями, после чего ветки полос убираются.
    """
    conflict = report["merge"]["conflict"]
    assert conflict["files"] == ["shared.txt"]
    assert "merge.conflict" in conflict["refused"] and "shared.txt" in conflict["refused"]
    assert conflict["kept_at_refusal"] == {"impl_a": True, "impl_b": True}, "деревья полос целы при отказе"
    assert set(conflict["diffs"]) == {"impl_a", "impl_b"}, "в контракте дифф каждой полосы"
    assert "строка от a" in conflict["diffs"]["impl_a"]
    assert "строка от b" in conflict["diffs"]["impl_b"]
    assert "строка от a" in conflict["merged_text"] and "строка от b" in conflict["merged_text"]
    assert len(conflict["parents"]) == 3, "слияние собрано merge-коммитом"
    assert len(conflict["lanes_cleaned"]) == 2, "ветки полос убраны после успешного слияния"


def test_demo_keeps_the_lane_repo_inside_the_sandbox(report: dict) -> None:
    """Репозиторий полос живёт в песочнице: прогон не трогает живые каталоги."""
    assert report["merge"]["repo"].startswith(report["home"]), report["merge"]["repo"]
