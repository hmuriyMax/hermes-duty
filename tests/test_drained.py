"""Попытку прервал рестарт рантайма — это не проигранная попытка (0.2.2).

35 из 91 провала за неделю — «процесс попытки умер/таймаут/убит», и кластеры
совпадают с рестартами гейтвея до секунды: 13.11 18:39:20 — 5 падений в трёх
делах; 17.11 четыре рестарта, а в `boards/duty/workspaces/d_95f55146-check3-1/
.duty/stderr.log` прямо `Error: runtime_draining`. Движок писал это `infra`-провалом
этапа, и при `max=1` дело сразу парковалось вопросом человеку.

Признак (`failures.DRAINED`) — факт, а не догадка по тексту: маркер дренажа из
закрытого списка в stderr попытки или смерть от сигнала рестарта. Политику этапа
он не списывает, и повтор поднимает диспетчер, а не серия движка.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_793_900_000
DEAL = "d_drained"


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec_module = _load("duty_spec_drained", "spec.py")
failures = _load("duty_failures_drained", "failures.py")
transit = _load("duty_transit_drained", "transit.py")
runner = _load("duty_runner_drained", "runner.py")


def test_drain_marker_in_stderr_is_not_a_policy_failure() -> None:
    """Код 1 + маркер `runtime_draining` в stderr — `drained`, а не `infra`."""
    reason = failures.drained(1, "Error: runtime_draining")

    assert reason.startswith(failures.DRAIN_TEXT)
    assert "runtime_draining" in reason


def test_drain_signal_is_not_a_policy_failure() -> None:
    """Попытка, убитая SIGINT рестарта (код 130), — тоже `drained`.

    `SIGTERM` (143) дренажом без маркера не считается: им же гасит попытку наш
    таймаут и рука человека, а RP-14338 показал честный инфра-отказ с кодом 143.
    """
    assert failures.drained(130, "").startswith(failures.DRAIN_TEXT)
    assert failures.drained(-2, "").startswith(failures.DRAIN_TEXT)
    assert failures.drained(143, "") == ""
    # Обычный ненулевой код — не дренаж: это отказ работы.
    assert failures.drained(1, "компилятор: undefined: x") == ""


def test_classify_marks_a_restarted_attempt_as_drained() -> None:
    """Классификация движка: попытку прервал рестарт рантайма — класс `drained`."""
    ended = failures.classify(result=None, exit_code=130)

    assert (ended.outcome, ended.kind) == (failures.OUTCOME_FAILED, failures.DRAINED)
    assert failures.DRAIN_TEXT in ended.reason
    # Наша собственная остановка по сроку — по-прежнему инфра, и причина называет
    # бюджет, а не «процесс упал сам».
    late = failures.classify(result=None, timed_out=True, timeout_sec=1500)
    assert (late.outcome, late.kind) == (failures.OUTCOME_FAILED, failures.INFRA)
    assert "упал по бюджету 25 мин" in late.reason
    # Обычная инфра без маркера — тоже инфра.
    plain = failures.classify(result=None, exit_code=7, result_error="result.json нет")
    assert (plain.outcome, plain.kind) == (failures.OUTCOME_FAILED, failures.INFRA)


def test_drained_is_not_a_policy_class() -> None:
    """`drained` не класс политики: `CLASSES` его не знает, `retry.on` не объявить."""
    assert failures.DRAINED not in failures.CLASSES
    assert failures.DRAINED not in failures.CLASS_TEXT
    # Имя у факта всё же есть: «неизвестный класс» на честной попытке читался бы
    # как отказ работы.
    assert failures.describe_class(failures.DRAINED) == failures.DRAINED_TEXT


def test_drained_attempt_does_not_burn_the_stage_policy() -> None:
    """Политика этапа не списывается: парковки нет, предохранитель не срабатывает."""
    policy = failures.Policy(max=1, on=(failures.INFRA, failures.CONTRACT))

    assert failures.decide_park(kind=failures.DRAINED, attempts=1, total=1, policy_=policy) is None
    # Обычный инфра-отказ при `max=1` — парковка, как и была: судейство не ослаблено.
    assert failures.decide_park(kind=failures.INFRA, attempts=2, total=2, policy_=policy) == (
        failures.REASON_RETRIES
    )
    # Предохранитель на дело дренаж не поднимает.
    drained_events = [
        {"kind": "attempt_ended",
         "payload": {"stage": "impl", "outcome": "failed", "kind": failures.DRAINED}}
        for _ in range(failures.MAX_ROUNDS_PER_DEAL)
    ]
    assert failures.total_failures(drained_events) == 0


def _journal(kind: str) -> list[dict[str, Any]]:
    return [
        {"id": 1, "ts": NOW, "deal_id": DEAL, "kind": "attempt_started",
         "payload": {"stage": "impl", "attempt": 1, "run_id": "impl:1"}, "source": "engine"},
        {"id": 2, "ts": NOW + 10, "deal_id": DEAL, "kind": "attempt_ended",
         "payload": {"stage": "impl", "attempt": 1, "outcome": "failed", "kind": kind,
                     "reason": failures.DRAIN_TEXT},
         "source": "engine"},
    ]


def _spec(tmp_path: Path, retry: str) -> Any:
    path = tmp_path / "duty.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            duty: дренаж
            stages:
              - id: impl
                role: implementer
                retry: {retry}
            """
        ),
        encoding="utf-8",
    )
    return spec_module.load_spec(path)


def test_drained_attempt_returns_the_stage_to_ready(tmp_path: Path) -> None:
    """В срезе дела этап снова `pending`, счётчик отказов пуст, попытка не сожжена.

    `d_95f55146` при `max=1` парковалось на первом же рестарте гейтвея: журнал
    писал `infra`-провал, счётчик класса исчерпывался, дело ждало человека.
    """
    spec = _spec(tmp_path, "{max: 1, on: [infra]}")
    journal = _journal(failures.DRAINED)

    decision = transit.next_stage(spec, journal, NOW + 20)
    states = transit.stage_states(spec, journal)

    assert states["impl"].state == "pending"
    assert states["impl"].failures == ()
    assert "рестарт рантайма" in states["impl"].reason
    # Следующая попытка этапа поднимается, а не паркуется вопросом человеку.
    assert (decision.kind, decision.pair) == ("run", [("impl", 2)])


def test_ordinary_infra_still_burns_the_attempt(tmp_path: Path) -> None:
    """Обычный `infra` без маркера по-прежнему списывает попытку."""
    spec = _spec(tmp_path, "{max: 0, on: [infra]}")
    journal = _journal(failures.INFRA)

    decision = transit.next_stage(spec, journal, NOW + 20)

    assert decision.kind == "failed"
    assert transit.stage_states(spec, journal)["impl"].failures == (("infra", 1),)


def test_lead_contract_carries_the_budget_rule() -> None:
    """Правило бюджета — в границах лида: по измеренной длительности, браузер с 2×.

    Живое занижение: 1200 с у `shots` (`d_fe6c91d0`), 1500 с у `check`
    (`d_76b269ce`), 1800 с у `impl` (`d_b9c480c8`) — этапы умирали по часам, а лид
    читал это как отказ работы.
    """
    spec = spec_module.Spec(
        path=Path("/tmp/duty.yaml"),
        duty="бюджет",
        repo=None,
        base=None,
        stages=(),
        charter=spec_module.Charter(declared=True, allowed_roles=("lead",)),
    )
    stage = spec_module.Stage(id="plan", role="lead", workspace="none", needs=())
    contract = runner.build_contract(
        spec,
        {"id": DEAL, "title": "бюджет", "body": ""},
        stage,
        1,
        workdir=Path("/tmp/attempt"),
        tree=None,
        cwd=Path("/tmp/attempt"),
        previous=[],
        deadline_sec=600,
    )

    rule = [line for line in contract["boundaries"] if "бюджет этапа оценивай" in line]
    assert rule, contract["boundaries"]
    assert "прошлым прогонам" in rule[0]
    assert "не меньше 2×" in rule[0]


def test_shell_stage_restart_is_drained_not_infra() -> None:
    """Этап-команда, погашенный рестартом рантайма, — `drained`, а не `infra`.

    Сборка и тесты живут в `kind: shell`, и их гибель на рестарте гейтвея тоже
    списывала политику: в `d_95f55146-check3-1/.duty/stderr.log` прямо
    `Error: runtime_draining`, а движок писал `infra`. Признаки те же два, что у
    попытки-агента — маркер в stderr и SIGINT рестарта.
    """
    by_marker = runner.classify_shell(
        exit_code=1, timed_out=False, must=0, timeout_sec=1800, stderr="Error: runtime_draining\n"
    )
    by_signal = runner.classify_shell(exit_code=130, timed_out=False, must=0, timeout_sec=1800)

    assert (by_marker[0], by_marker[1]) == (runner.OUTCOME_FAILED, failures.DRAINED)
    assert failures.DRAIN_TEXT in by_marker[2]
    assert (by_signal[0], by_signal[1]) == (runner.OUTCOME_FAILED, failures.DRAINED)


def test_successful_command_with_the_marker_in_stderr_is_not_drained() -> None:
    """Успешная команда с маркером в выводе — `done`: маркер пишет она, а не рантайм.

    Shell-этап, который ищет `runtime_draining` в логе, не должен возвращаться в
    pending из-за собственного вывода. Ненулевой код без маркера — по-прежнему
    отказ контракта («код 7, ждали 0»), а не дренаж: судейство не ослаблено.
    """
    done = runner.classify_shell(
        exit_code=0, timed_out=False, must=0, timeout_sec=1800, stderr="runtime_draining: 3\n"
    )
    plain = runner.classify_shell(exit_code=7, timed_out=False, must=0, timeout_sec=1800)

    assert done == (runner.OUTCOME_DONE, "", "")
    assert (plain[0], plain[1]) == (runner.OUTCOME_FAILED, failures.CONTRACT)


def test_shell_timeout_is_named_by_budget_like_an_agent_attempt() -> None:
    """Падение shell-этапа по часам называется бюджетом и подсказывает расчёт.

    Сборка и тесты — это как раз `kind: shell` с `timeout_sec`; старое «команда не
    дожила: таймаут этапа N с» читалось лидом как отказ работы.
    """
    outcome, kind, reason = runner.classify_shell(
        exit_code=None, timed_out=True, must=0, timeout_sec=1500
    )

    assert (outcome, kind) == (runner.OUTCOME_FAILED, failures.INFRA)
    assert "упал по бюджету 25 мин" in reason
    assert "прошлым прогонам" in reason
    assert "не меньше 2×" in reason

