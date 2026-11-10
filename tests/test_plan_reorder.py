"""План переставляет этапы, объявляет параллель и называет свою цену (1.0).

Проверяется контракт `docs/ENGINE-1.0.md` (разделы 4, 5 и 8) там, где его видно
без модели:

1. перестановка **неначатого** этапа применяется: принятая ревизия меняет `needs`
   этапа графа, а не добавляет его копию, и решение идёт по новому порядку;
2. перестановка этапа, у которого была попытка, — `plan_rejected` с отказом
   «этап уже начат», а граф остаётся прежним (сделанное возвращает приговор);
3. `parallel_with` из предложения доезжает до графа, и сразу готовые этапы
   запускаются одной пачкой — параллель объявлена, а не угадана;
4. `cost.tokens` двигает срабатывание `plan_gate`: цена плана важнее суммы
   этапных бюджетов, а без цены порог считается по ним (фолбэк);
5. цикл в `needs`, замкнутый перестановкой через существующие этапы, — отказ
   `plan.cycle`, а не граф, по которому дело не пойдёт никогда;
6. принятая перестановка остаётся графом и после того, как её этап начали:
   граф не сжимается задним числом.

Часы нигде не читаются — `now` передаётся аргументом, как и в остальных слоях.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

NOW = 1_792_000_000
DEAL = "d_reorder"


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути: пакета нет, грузим так же, как это делает движок."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec_module = load_module("hermes_duty_spec", "spec.py")
transit = load_module("hermes_duty_transit", "transit.py")
journal = load_module("hermes_duty_journal", "journal.py")
runner = load_module("hermes_duty_runner", "runner.py")
stagefeed = load_module("hermes_duty_stagefeed", "stagefeed.py")


# Seed: план, работа и проверка за ней. Гейтов плана нет — порог проверяется
# отдельной спекой, иначе он срабатывал бы в тестах, которые к нему не про.
SEED = """
duty: перестановка
charter:
  max_stages: 8
  max_rounds_replan: 1
stages:
  - id: plan
    role: implementer
    workspace: none
  - id: impl
    role: implementer
    needs: [plan]
    workspace: none
  - id: check
    role: implementer
    needs: [impl]
    workspace: none
"""

# Дело с потолком и порогом по доле бюджета: 1000 токенов, гейт после 50%.
COST = """
duty: цена плана
budget: {tokens: 1000}
charter:
  max_stages: 8
  max_rounds_replan: 1
  plan_gate: {budget_share: 0.5}
stages:
  - id: plan
    role: implementer
    workspace: none
  - id: impl
    role: implementer
    needs: [plan]
    workspace: none
"""


def write_spec(tmp_path: Path, text: str, name: str = "duty.yaml") -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def events(*items: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """События журнала в форме `journal.read_events`: id растёт, `ts` один."""
    return [
        {"id": index + 1, "ts": NOW, "deal_id": DEAL, "kind": kind, "payload": payload, "source": "engine"}
        for index, (kind, payload) in enumerate(items)
    ]


def started(stage: str, attempt: int = 1) -> tuple[str, dict[str, Any]]:
    return ("attempt_started", {"stage": stage, "attempt": attempt, "run_id": f"{stage}#{attempt}"})


def proposed(rev: int, stages: list[dict[str, Any]], **extra: Any) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"rev": rev, "stages": stages, "reason": "порядок", "source_stage": "plan"}
    payload.update(extra)
    return ("plan_proposed", payload)


def accepted(rev: int) -> tuple[str, dict[str, Any]]:
    return ("plan_accepted", {"rev": rev, "by": "engine"})


def report(
    spec: Any, stages: list[dict[str, Any]], *, journal_events: Any = (), cost: Any = None
) -> tuple[list[dict[str, Any]], str]:
    """`runner.report_plan` от имени этапа плана: факты и причина отказа."""
    stage = spec.stage("plan")
    assert stage is not None
    plan: dict[str, Any] = {"stages": stages, "reason": "порядок"}
    if cost is not None:
        plan["cost"] = cost
    return runner.report_plan({"id": DEAL}, spec, stage, {"plan": plan}, 1, events=list(journal_events), now=NOW)


def needs_of(graph: Any) -> dict[str, tuple[str, ...]]:
    """Рёбра графа словарём: «какой этап после кого стоит»."""
    return {stage.id: tuple(stage.needs) for stage in graph}


def work(stage_id: str, *, needs: tuple[str, ...] = ("plan",), **extra: Any) -> dict[str, Any]:
    """Новый этап предложения: работа, которую можно проверить (evidence есть)."""
    stage: dict[str, Any] = {
        "id": stage_id,
        "role": "implementer",
        "needs": list(needs),
        "workspace": "none",
        "evidence": [{"cmd": "true", "must": 0}],
    }
    stage.update(extra)
    return stage


# ------------------------------------------------- 1. перестановка неначатого


def test_reorder_of_a_stage_that_has_not_started_moves_it(tmp_path: Path) -> None:
    """`check` едет перед `impl`: перестановка меняет рёбра, а не добавляет этап.

    Ничего не начато, поэтому план вправе переставить порядок: принятая ревизия
    даёт `check.needs: [plan]`, и работа идёт по новому порядку — первым `impl`
    (объявление в спеке), `check` уже готов следом, а не ждёт `impl`.
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(spec, [{"id": "check", "needs": ["plan"]}])
    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_accepted"]

    graph = transit.graph(spec, facts)
    assert tuple(stage.id for stage in graph) == ("plan", "impl", "check")
    assert needs_of(graph) == {"plan": (), "impl": ("plan",), "check": ("plan",)}

    journal_events = facts + events(("attempt_ended", {
        "stage": "plan", "attempt": 1, "outcome": "done", "kind": None, "run_id": "plan#1", "reason": "",
    }))
    decision = transit.next_stage(spec, journal_events, NOW)
    assert decision.pair == [("impl", 1)]


def test_reorder_survives_the_stage_starting(tmp_path: Path) -> None:
    """Принятая перестановка не сжимается, когда её этап начали.

    Проверка «этап уже начат» стоит в момент предложения: граф, перечитанный
    потом, обязан помнить перестановку, иначе после первого же запуска порядок
    вернулся бы к файлу.
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(spec, [{"id": "check", "needs": ["plan"]}])
    assert why == ""
    journal_events = facts + events(started("check"))
    assert needs_of(transit.graph(spec, journal_events))["check"] == ("plan",)


# ------------------------------------------------- 2. перестановка начатого


def test_reorder_of_a_started_stage_is_refused(tmp_path: Path) -> None:
    """`impl` уже начинали — перестановка отклонена, граф прежний.

    Сделанное возвращает приговор ревью (`verdict.changes`), а не план: иначе лид
    обходил бы ревью, переставляя этап, работу которого уже приняли.
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(spec, [{"id": "impl", "needs": []}], journal_events=events(started("impl")))

    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_rejected"]
    issues = facts[1]["payload"]["issues"]
    assert any("plan.reorder" in issue and "уже начат" in issue for issue in issues), issues
    assert needs_of(transit.graph(spec, facts)) == {"plan": (), "impl": ("plan",), "check": ("impl",)}


def test_reorder_cannot_rewrite_the_stage_it_moves(tmp_path: Path) -> None:
    """Перестановка называет только `needs`/`parallel_with`: роль в ней — отказ."""
    spec = write_spec(tmp_path, SEED)
    facts, _why = report(spec, [{"id": "check", "role": "reviewer", "needs": ["plan"]}])

    assert facts[-1]["kind"] == "plan_rejected"
    assert any("plan.id" in issue for issue in facts[-1]["payload"]["issues"])


# ---------------------------------------------- 3. параллель из предложения


def test_parallel_with_declared_by_the_plan_starts_both(tmp_path: Path) -> None:
    """`parallel_with` доезжает до графа: два готовых этапа идут одной пачкой.

    Параллель — объявление, а не догадка: `docs` сказал «вместе с impl», и решение
    называет оба этапа в одном шаге. Без объявления `_batch` взял бы только первый.
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(spec, [work("docs", parallel_with=["impl"])])
    assert why == ""

    graph = transit.graph(spec, facts)
    assert dict((stage.id, tuple(stage.parallel_with)) for stage in graph)["docs"] == ("impl",)

    journal_events = facts + events(("attempt_ended", {
        "stage": "plan", "attempt": 1, "outcome": "done", "kind": None, "run_id": "plan#1", "reason": "",
    }))
    decision = transit.next_stage(spec, journal_events, NOW)
    assert decision.stages == ("impl", "docs")


def test_stage_slice_names_the_author_and_the_parallel_of_added_stages(tmp_path: Path) -> None:
    """Срез поверхностей: у добавленного этапа `added_by: lead` и его параллель.

    Поверхность рисует «добавлен лидом» и параллель по факту графа, а не по имени
    этапа и не по совпадению времени старта.
    """
    spec = write_spec(tmp_path, SEED)
    facts, _why = report(spec, [work("docs", parallel_with=["impl"])])
    rows = {row["id"]: row for row in stagefeed.stages(spec, facts, NOW)}

    assert rows["docs"]["added_by"] == "lead"
    assert rows["docs"]["parallel_with"] == ["impl"]
    assert rows["impl"]["added_by"] == ""
    assert rows["impl"]["parallel_with"] == []


# ------------------------------------------------------- 4. цена предложения


def test_declared_cost_moves_the_plan_gate(tmp_path: Path) -> None:
    """Цена плана решает порог: 800 из 1000 — гейт, 10 из 1000 — нет.

    Этапные бюджеты в предложении крошечные (10 токенов), поэтому фолбэк порога не
    проходит. Названная цена — проходит: план, который стоит дорого, спрашивает
    человека, а не пробирается по сумме своих же этапных бюджетов.
    """
    spec = write_spec(tmp_path, COST, name="cost.yaml")
    cheap = [work("research", budget={"tokens": 10})]

    facts, why = report(spec, cheap, cost={"tokens": 800})
    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed"]
    assert facts[0]["payload"]["cost"] == {"tokens": 800}
    assert needs_of(transit.graph(spec, facts)) == {"plan": (), "impl": ("plan",)}

    fallback, _why = report(spec, cheap)
    assert [fact["kind"] for fact in fallback] == ["plan_proposed", "plan_accepted"]


def test_cost_gate_asks_the_human_about_the_named_price(tmp_path: Path) -> None:
    """Причина гейта называет цену плана, а не сумму этапных бюджетов."""
    spec = write_spec(tmp_path, COST, name="cost.yaml")
    why = runner.plan_gate_reason(
        spec, (), transit.graph(spec, ()), {"tokens": 800}
    )
    assert "800" in why and "50%" in why, why


def test_broken_cost_is_refused(tmp_path: Path) -> None:
    """`cost: {tokens: 0}` — отказ: «бесплатно» это утверждение, а не молчание."""
    spec = write_spec(tmp_path, COST, name="cost.yaml")
    facts, _why = report(spec, [work("research")], cost={"tokens": 0})

    assert facts[-1]["kind"] == "plan_rejected"
    assert any("plan.cost" in issue for issue in facts[-1]["payload"]["issues"])


# ------------------------------------------------------- 5. цикл в needs


def test_cycle_closed_by_a_reorder_is_refused(tmp_path: Path) -> None:
    """Цикл через существующие этапы — тот же отказ, что цикл внутри плана.

    `plan.needs: [check]` поверх seed `check → impl → plan` замыкает круг: по
    такому графу дело не пошло бы никогда, и ждать было бы нечего.
    """
    spec = write_spec(tmp_path, SEED)
    facts, _why = report(spec, [{"id": "plan", "needs": ["check"]}])

    assert facts[-1]["kind"] == "plan_rejected"
    assert any("plan.cycle" in issue for issue in facts[-1]["payload"]["issues"])
    assert needs_of(transit.graph(spec, facts)) == {"plan": (), "impl": ("plan",), "check": ("impl",)}


def test_plan_proposal_keeps_the_seed_order_for_unknown_reorder(tmp_path: Path) -> None:
    """Мусор в журнале граф не растит и не переставляет: правило 8.

    Журнал с принятой ревизией, которую разбор отвергает (ссылка на несуществующий
    этап), оставляет seed-граф — читатель не второй валидатор, но и не верит на
    слово.
    """
    spec = write_spec(tmp_path, SEED)
    journal_events = events(proposed(1, [{"id": "check", "needs": ["nowhere"]}]), accepted(1))

    assert needs_of(transit.graph(spec, journal_events)) == {
        "plan": (),
        "impl": ("plan",),
        "check": ("impl",),
    }
    assert "nowhere" not in journal.payload_of(journal_events[0])["stages"][0]["id"]
