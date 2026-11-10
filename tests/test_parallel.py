"""Два этапа пачки идут одновременно (1.0).

Проверяется то, что видно без модели, — правило 3 `docs/ENGINE-1.0.md`:
одновременно идут только этапы, объявленные друг другу `parallel_with`, движок
ведёт не больше `MAX_PARALLEL` попыток за шаг, а каждая попытка остаётся своей:
свой лизинг, свой процесс, свой факт конца.

1. два этапа с `parallel_with` стартуют **в одном шаге** и оба стоят с лизингом:
   к моменту спавна второй попытки первая ещё не закрыта (`attempt_started` без
   `attempt_ended`), и её лизинг на месте — то есть это параллель, а не очередь;
2. пока движок смотрит за первым процессом, лизинг второго продлевается: живая
   попытка не протухает только потому, что за ней ещё не смотрят;
3. предел `MAX_PARALLEL` соблюдается: третий объявленный параллельным этап ждёт
   своей пачки и стартует следующим шагом;
4. падение одной попытки не отменяет вторую: обе получают свой `attempt_ended`;
5. срез этапов (`transit.stage_states`, `stagefeed.stages`) показывает оба этапа
   идущими одновременно.

Часы нигде не читаются: `now` передаётся аргументом, как и в остальных слоях.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from test_driver import (  # noqa: E402 - пакета нет, pytest кладёт `tests/` в путь
    NOW,
    Fake as DriverFake,
    deals,
    driver,
    events,
    journal,
    kinds,
    leases,
    make_deal,
    make_home,
    open_store,
    run as drive,
    runner,
    transit,
    write_spec,
)

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути — под тем же именем, что и у остального кода."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stagefeed = load_module("hermes_duty_stagefeed", "stagefeed.py")


# Пара, объявленная параллельной: `docs` говорит «можно вместе с impl».
PARALLEL = """
duty: пачка
stages:
  - id: impl
    role: implementer
    workspace: none
  - id: docs
    role: documenter
    workspace: none
    parallel_with: [impl]
"""

# Три этапа, объявленные параллельными друг другу: предел пачки — два.
THREE = """
duty: пачка из трёх
stages:
  - id: impl
    role: implementer
    workspace: none
  - id: docs
    role: documenter
    workspace: none
    parallel_with: [impl, check]
  - id: check
    role: tester
    workspace: none
    parallel_with: [impl, docs]
"""


class Watching(DriverFake):
    """Спавн, который перед каждым запуском снимает факт: кто стоит и с лизингом.

    Снимок берётся в момент спавна — это единственное место, где видно разницу
    между «оба этапа идут» и «второй пошёл после смерти первого»: в конце шага
    оба лизинга уже сняты, и по журналу постфактум их не различить.
    """

    def __init__(self, *, conn: sqlite3.Connection, deal: dict[str, Any], spec: Any, plan: Optional[dict[str, str]] = None) -> None:
        super().__init__(plan)
        self.conn = conn
        self.deal = deal
        self.spec = spec
        self.seen: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], cwd: Path) -> Any:
        contract = json.loads(Path(cmd[list(cmd).index("--query-file") + 1]).read_text(encoding="utf-8"))
        facts = events(self.conn, self.deal)
        self.seen.append(
            {
                "stage": str(contract["stage"]),
                "leases": sorted(row["stage"] for row in leases(self.conn, self.deal)),
                "kinds": [str(event["kind"]) for event in facts],
                "states": {stage: state.state for stage, state in transit.stage_states(self.spec, facts).items()},
                "stopped": sum(process.stopped for process in self.processes),
            }
        )
        return super().__call__(cmd, cwd)


def prefix_through(conn: sqlite3.Connection, deal: dict[str, Any], kind: str, *, number: int = 1) -> list[dict[str, Any]]:
    """Журнал до `number`-го факта `kind` включительно — снимок «в середине шага»."""
    prefix: list[dict[str, Any]] = []
    seen = 0
    for event in events(conn, deal):
        prefix.append(event)
        if str(event["kind"]) == kind:
            seen += 1
            if seen == number:
                break
    assert seen == number, f"в журнале нет {number}-го {kind}"
    return prefix


# ------------------------------------------- 1. два этапа стоят одновременно


def test_two_stages_declared_parallel_start_in_one_step_with_both_leases(tmp_path: Path) -> None:
    """Оба этапа пачки подняты одним шагом: у каждого свой процесс и свой лизинг.

    Проверка идёт по снимку в момент спавна второй попытки: первая уже записала
    `attempt_started`, ещё не закрыта (`attempt_ended` нет), её процесс не погашен,
    а её лизинг на месте. Последовательный движок этот тест не проходит — он
    поднимает второй этап только после смерти первого.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, PARALLEL)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Watching(conn=conn, deal=deal, spec=spec)

    decision = transit.next_stage(spec, [], NOW)
    assert decision.stages == ("impl", "docs"), decision

    report = drive(conn, spec, deal, home, fake)

    assert [call["stage"] for call in fake.calls] == ["impl", "docs"], "одна пачка — один шаг"
    assert [row["n"] for row in report["steps"]] == [1, 1], "две строки шага, а не два шага"
    # Снимок второго спавна: первый этап жив, держит лизинг и ещё не закрыт.
    second = fake.seen[1]
    assert second["leases"] == ["docs", "impl"], second["leases"]
    assert second["kinds"].count("attempt_started") == 1, second["kinds"]
    assert "attempt_ended" not in second["kinds"], "первая попытка ещё не закрыта"
    assert second["stopped"] == 0, "первый процесс погашен до спавна второго"
    assert second["states"] == {"impl": "running", "docs": "pending"}, second["states"]
    # Каждая попытка закрывается своим фактом, и обе закончились.
    ended = [event["payload"]["stage"] for event in events(conn, deal) if event["kind"] == "attempt_ended"]
    assert sorted(ended) == ["docs", "impl"], ended
    assert [row["outcome"] for row in report["steps"]] == ["done", "done"]


def test_the_slice_shows_both_stages_running(tmp_path: Path) -> None:
    """Пока открыты обе попытки, срез называет `running` у обоих — без догадок.

    Источник среза — журнал: две открытые попытки и есть «два этапа идут»,
    поэтому `transit.stage_states` и лента этапов отвечают одинаково, а решение
    ждёт их обе, а не запускает заново.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, PARALLEL)
    deal = make_deal(conn, spec_path=spec.path)

    drive(conn, spec, deal, home, Watching(conn=conn, deal=deal, spec=spec))

    during = prefix_through(conn, deal, "attempt_started", number=2)
    states = transit.stage_states(spec, during)
    assert {stage: state.state for stage, state in states.items()} == {
        "impl": "running",
        "docs": "running",
    }
    rows = {row["id"]: row["state"] for row in stagefeed.stages(spec, during, NOW)}
    assert rows == {"impl": "running", "docs": "running"}, rows
    decision = transit.next_stage(spec, during, NOW)
    assert decision.kind == "wait" and decision.what == "attempt", decision


# ------------------------------------------- 2. лизинг соседа не протухает


class Standing:
    """Процесс, который стоит: строк нет, сам не выходит, пока его не остановят."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.exit_code: Optional[int] = None
        self.stopped = 0

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        return None

    def finished(self) -> bool:
        return bool(self.stopped)

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.exit_code

    def stop(self, grace: float = 10) -> Optional[int]:
        self.stopped += 1
        self.exit_code = -15
        return self.exit_code


class Ticking:
    """Монотонные часы шага: время идёт по `step` на каждое чтение."""

    def __init__(self, step: float = 25.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_a_sibling_keeps_its_lease_while_the_engine_watches_the_first(tmp_path: Path) -> None:
    """Пока движок следит за первым процессом пачки, лизинг второго продлевается.

    Второй процесс пачки живой, хотя за ним ещё не смотрят: без продления его
    лизинг протух бы на сроке этапа, и соседний движок забрал бы идущую работу.
    Проверяется сроком в строке лизинга, а не вызовом `renew`.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, PARALLEL)
    deal = make_deal(conn, spec_path=spec.path)
    assert runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=4242, ttl=60, now=NOW)
    assert runner.lease(conn, deal=deal, stage="docs", attempt=1, pid=777, ttl=60, now=NOW)
    stage = spec.stage("impl")
    assert stage is not None

    runner.follow(
        conn,
        Standing(),
        deal=deal,
        stage=stage,
        attempt=1,
        ttl=60,
        deadline=120.0,
        budget_tokens=None,
        monotonic=Ticking(),
        clock=lambda: NOW + 30,
        others=(("docs", 777, 60),),
    )

    held = runner.lease_of(conn, deal, "docs") or {}
    assert held.get("expires_at") == NOW + 90, "лизинг соседа не продлён: он протух бы раньше работы"
    own = runner.lease_of(conn, deal, "impl") or {}
    assert own.get("expires_at") == NOW + 90


# ------------------------------------------------------- 3. предел параллели


def test_the_parallel_limit_is_respected(tmp_path: Path) -> None:
    """Третий объявленный параллельным этап ждёт своей пачки (`MAX_PARALLEL`).

    Предел живёт в `transit` рядом с остальными лимитами слоя: пачка — это столько
    процессов модели и деревьев, сколько движок готов вести сразу.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, THREE)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Watching(conn=conn, deal=deal, spec=spec)

    decision = transit.next_stage(spec, [], NOW)
    assert transit.MAX_PARALLEL == 2
    assert decision.stages == ("impl", "docs"), "третий ждёт: пачка не больше предела"

    report = drive(conn, spec, deal, home, fake)

    assert [call["stage"] for call in fake.calls] == ["impl", "docs", "check"]
    assert [row["n"] for row in report["steps"]] == [1, 1, 2], "третий этап — следующим шагом"


# -------------------------------------- 4. команда и агент в одной пачке


SHELL_PAIR = """
duty: пачка с командой
stages:
  - id: check
    kind: shell
    cmd: "sleep 0.2; true"
  - id: impl
    role: implementer
    workspace: none
    parallel_with: [check]
"""


def test_a_shell_stage_and_an_agent_stage_stand_together(tmp_path: Path) -> None:
    """Команда этапа и попытка агента поднимаются вместе: оба вида этапов равны.

    Пачка не спрашивает вид этапа: `check` — настоящий процесс (`/bin/sh`), `impl`
    — фейковый спавн, и к приходу агента команда уже стоит со своим лизингом.
    Иначе параллель работала бы только для половины спеки.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, SHELL_PAIR)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Watching(conn=conn, deal=deal, spec=spec)

    def spawn(cmd: list[str], cwd: Path, *args: Any, **kwargs: Any) -> Any:
        """Команду этапа поднимает настоящий спавн, агента — фейк."""
        if cmd[0] == runner.SHELL_BIN:
            return runner.spawn_process(cmd, cwd, *args, **kwargs)
        return fake(cmd, cwd)

    report = drive(conn, spec, deal, home, fake, spawn=spawn)

    assert [call["stage"] for call in fake.calls] == ["impl"], "агент поднят в том же шаге"
    assert [row["n"] for row in report["steps"]] == [1, 1]
    # К приходу агента команда уже стоит: факт есть, лизинг есть, конца нет.
    during = fake.seen[0]
    assert during["kinds"].count("attempt_started") == 1, during["kinds"]
    assert "attempt_ended" not in during["kinds"], during["kinds"]
    assert during["leases"] == ["check", "impl"], during["leases"]
    ended = {
        str(event["payload"]["stage"]): str(event["payload"]["outcome"])
        for event in events(conn, deal)
        if event["kind"] == "attempt_ended"
    }
    assert ended == {"check": "done", "impl": "done"}, ended


# ------------------------------------------ 5. падение одной не отменяет вторую

def test_one_failed_attempt_does_not_cancel_the_other(tmp_path: Path) -> None:
    """Падение одной попытки пачки не отменяет вторую: обе закрываются своими фактами.

    Иначе параллель была бы лотереей: сосед падал бы не сам, а «за компанию», и
    его работа терялась бы без отказа.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, PARALLEL)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Watching(conn=conn, deal=deal, spec=spec, plan={"impl": "fail"})

    report = drive(conn, spec, deal, home, fake)

    assert [call["stage"] for call in fake.calls] == ["impl", "docs"], "вторая попытка поднялась"
    assert [row["outcome"] for row in report["steps"]] == ["failed", "done"]
    assert [row["kind"] for row in report["steps"]] == ["infra", None]
    ended = {
        str(event["payload"]["stage"]): str(event["payload"]["outcome"])
        for event in events(conn, deal)
        if event["kind"] == "attempt_ended"
    }
    assert ended == {"impl": "failed", "docs": "done"}, ended
    assert kinds(conn, deal).count("attempt_ended") == 2
