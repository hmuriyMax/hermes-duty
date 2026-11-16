"""План переставляет этапы, правит неначатые, объявляет параллель и называет цену (1.0).

Проверяется контракт `docs/ENGINE-1.0.md` (разделы 4, 5 и 8) там, где его видно
без модели:

1. перестановка **неначатого** этапа применяется: принятая ревизия меняет `needs`
   этапа графа, а не добавляет его копию, и решение идёт по новому порядку;
2. правка **неначатого** этапа применяется так же: `cmd`, `budget` и `evidence`
   лид меняет у своего же этапа, а не заводит второй рядом (RP-14338), и
   зависимости этапа при этом сохраняются, если запись их не назвала;
3. перестановка этапа, у которого была попытка, — `plan_rejected` с отказом
   «этап уже начат», а правка такого этапа — отказ `plan.edit` с его именем;
   граф в обоих случаях остаётся прежним (сделанное возвращает приговор);
4. `parallel_with` из предложения доезжает до графа, и сразу готовые этапы
   запускаются одной пачкой — параллель объявлена, а не угадана;
5. `cost.tokens` двигает срабатывание `plan_gate`: цена плана важнее суммы
   этапных бюджетов, а без цены порог считается по ним (фолбэк);
6. цикл в `needs`, замкнутый перестановкой через существующие этапы, — отказ
   `plan.cycle`, а не граф, по которому дело не пойдёт никогда;
7. принятая перестановка остаётся графом и после того, как её этап начали:
   граф не сжимается задним числом.

Часы нигде не читаются — `now` передаётся аргументом, как и в остальных слоях.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

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


# ------------------------------------------------- 2. правка неначатого этапа


def test_edit_of_a_not_started_stage_reaches_the_graph(tmp_path: Path) -> None:
    """`check` ещё не начинали: правка команды, бюджета и доказательств принята.

    RP-14338: лид хотел сузить команду и добавить бюджет своему же `check`,
    получал `plan.id` на каждую ревизию, обошёл этапом `check2`, а мёртвый
    `check` остался в графе. Здесь граф (а не только разбор) несёт новые поля, а
    этап остаётся один.
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(spec, [{
        "id": "check",
        "cmd": "go vet ./...",
        "budget": {"tokens": 900},
        "evidence": [{"cmd": "go list ./...", "must": 0}],
        "needs": ["plan"],
    }])
    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_accepted"]

    graph = {stage.id: stage for stage in transit.graph(spec, facts)}
    assert tuple(graph) == ("plan", "impl", "check")
    assert graph["check"].cmd == "go vet ./..."
    assert graph["check"].budget.tokens == 900
    assert [item.cmd for item in graph["check"].evidence] == ["go list ./..."]
    assert graph["check"].needs == ("plan",)
    assert graph["check"].role == "implementer", "поля, которых правка не назвала, — из спеки"


def test_edit_of_a_started_stage_is_refused_and_names_the_stage(tmp_path: Path) -> None:
    """`impl` начинали — правка отклонена, в отказе имя этапа и факт, не только правило."""
    spec = write_spec(tmp_path, SEED)
    facts, why = report(
        spec, [{"id": "impl", "budget": {"tokens": 900}}], journal_events=events(started("impl"))
    )

    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_rejected"]
    issues = facts[1]["payload"]["issues"]
    assert any("plan.edit" in issue and "'impl'" in issue and "уже начинался" in issue for issue in issues), issues
    assert needs_of(transit.graph(spec, facts)) == {"plan": (), "impl": ("plan",), "check": ("impl",)}


# Граф с параллелью в спеке: правка не вправе её потерять — `parallel_with`
# объявлен человеком, а не той записью, которая правит бюджет.
SEED_PARALLEL = """
duty: параллель в спеке
stages:
  - id: plan
    role: implementer
    workspace: none
  - id: impl
    role: implementer
    needs: [plan]
    workspace: none
  - id: docs
    role: implementer
    needs: [plan]
    parallel_with: [impl]
    workspace: none
"""


@pytest.mark.parametrize(
    "record",
    [
        {"cmd": "go vet ./..."},
        {"budget": {"tokens": 500}},
        {"evidence": [{"cmd": "go list ./...", "must": 0}]},
    ],
    ids=["cmd", "budget", "evidence"],
)
def test_edit_keeps_the_dependencies_the_record_does_not_name(tmp_path: Path, record: dict[str, Any]) -> None:
    """Правка этапа **с зависимостями** и без `needs` в записи: порядок тот же.

    Запись без `needs:` приходит с пустым полем (`_str_tuple`), и правка `cmd`
    иначе обнуляла бы зависимости: `check` стал бы входом графа, и движок поднял
    бы его сразу, вне порядка (`RP-14338` — лид правит свой этап).
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(spec, [{"id": "check", **record}])
    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_accepted"]

    graph = transit.graph(spec, facts)
    assert needs_of(graph) == {"plan": (), "impl": ("plan",), "check": ("impl",)}
    assert graph[-1].id == "check" and graph[-1].needs, "этап не стал входом графа"

    journal_events = facts + events(
        ("attempt_ended", {"stage": "impl", "attempt": 1, "outcome": "done", "kind": None, "run_id": "impl#1", "reason": ""})
    )
    # `check` ждёт `impl`, а не идёт первым: входом он не стал.
    assert transit.next_stage(spec, journal_events, NOW).pair == [("check", 1)]


def test_edit_keeps_the_parallel_the_spec_declared(tmp_path: Path) -> None:
    """`parallel_with` — тоже зависимость: правка без него её не стирает."""
    spec = write_spec(tmp_path, SEED_PARALLEL)
    facts, why = report(spec, [{"id": "docs", "budget": {"tokens": 500}}])
    assert why == ""

    graph = {stage.id: stage for stage in transit.graph(spec, facts)}
    assert tuple(graph["docs"].parallel_with) == ("impl",)
    assert tuple(graph["docs"].needs) == ("plan",)
    assert graph["docs"].budget.tokens == 500


def test_reorder_keeps_the_field_the_record_does_not_name(tmp_path: Path) -> None:
    """Перестановка не трогает то, чего не назвала: `parallel_with` остаётся.

    `needs` и `parallel_with` — разные рёбра, и запись, которая двигает одно,
    не вправе стереть другое: `needs: []` значит «этап стал входом», а
    отсутствие поля — «как было» (`spec.parse_plan`, `order`).
    """
    spec = write_spec(tmp_path, SEED_PARALLEL)
    facts, why = report(spec, [{"id": "docs", "needs": ["plan", "impl"]}])
    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_accepted"]

    graph = {stage.id: stage for stage in transit.graph(spec, facts)}
    assert tuple(graph["docs"].needs) == ("plan", "impl"), "названный `needs` — порядок целиком"
    assert tuple(graph["docs"].parallel_with) == ("impl",), "неназванный `parallel_with` стёрт"


def test_reorder_still_names_the_order_entirely(tmp_path: Path) -> None:
    """Перестановка задаёт порядок целиком: `needs: []` в ней — «этап стал входом».

    Смягчить это значило бы отнять у лида единственный способ убрать ребро:
    «не назвал» и «назвал пустым» — разные записи, и вторая значит «вход».
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(spec, [{"id": "check", "needs": []}])
    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_accepted"]
    assert needs_of(transit.graph(spec, facts)) == {"plan": (), "impl": ("plan",), "check": ()}


def test_edit_of_an_occupied_id_is_applied_by_the_engine(tmp_path: Path) -> None:
    """Проба F4 на родителе (`57adcb0`): правка `cmd` под занятым id.

    Проверяется поведение движка, а не форма вызова: на `57adcb0` эта запись
    получала `plan_rejected`/`plan.id` — правки не было вовсе (лид обходил этапом
    `check2`, мёртвый `check` оставался в графе). Здесь тот же путь
    (`runner.report_plan`, без шимов `base_stages`) даёт принятый план.
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(spec, [{"id": "check", "cmd": "go vet ./..."}])

    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_accepted"], facts
    graph = {stage.id: stage for stage in transit.graph(spec, facts)}
    assert graph["check"].cmd == "go vet ./..."


def test_needs_of_a_started_stage_are_still_reordered(tmp_path: Path) -> None:
    """`needs`/`parallel_with` — не правка: перестановка начатого этапа отказывает по-своему."""
    spec = write_spec(tmp_path, SEED)
    moved, _why = report(spec, [{"id": "check", "needs": ["plan"]}], journal_events=events(started("impl")))
    assert [fact["kind"] for fact in moved] == ["plan_proposed", "plan_accepted"]
    assert needs_of(transit.graph(spec, moved))["check"] == ("plan",)

    refused, _why = report(spec, [{"id": "impl", "needs": []}], journal_events=events(started("impl")))
    assert any("plan.reorder" in issue for issue in refused[1]["payload"]["issues"])


# ------------------------------------------------- 3. перестановка начатого


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
    """Перестановка начатого этапа называет порядок: `must` в ней — отказ `plan.edit`."""
    spec = write_spec(tmp_path, SEED)
    facts, _why = report(
        spec, [{"id": "impl", "must": 7, "needs": ["plan"]}], journal_events=events(started("impl"))
    )

    assert facts[-1]["kind"] == "plan_rejected"
    assert any("plan.edit" in issue for issue in facts[-1]["payload"]["issues"])


def test_noop_reorder_of_a_started_stage_is_accepted(tmp_path: Path) -> None:
    """Запись «как есть» у начатого этапа — не отказ: граф от неё не меняется.

    Лид, назвавший этап с теми же `needs`, что уже в графе, терял ревизию целиком
    (`d_2fc749fb` rev4: `plan` с `needs: []` и `close` с `needs: ["plan"]` — ровно
    граф, `plan.reorder`, дело запарковано). Отказ остаётся на реальную смену
    порядка — её и проверяет соседний кейс.
    """
    spec = write_spec(tmp_path, SEED)
    facts, why = report(
        spec, [{"id": "impl", "needs": ["plan"]}], journal_events=events(started("impl"))
    )

    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_accepted"], facts
    assert needs_of(transit.graph(spec, facts))["impl"] == ("plan",)


# ---------------------------------------------- 4. параллель из предложения


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


# ------------------------------------------------------- 5. цена предложения


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


# ------------------------------------------------------- 6. цикл в needs


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


# ------------------------------- 8. закладки дела: вход и закрытие — работа лида
#
# Живое дело d_92177017 (13.11): план лида не назвал вход, и в ленте висели два
# этапа «без дела» — `coordinator` (его работа — сам план, а план пишет лид
# инструментом `duty_plan`) и `close` (его работа — приговор лида в `duty_close`).
# Вторая половина была опаснее косметики: `close` оставался с `needs: [coordinator]`,
# то есть ребра от работы плана к закрытию не было вовсе.

BOOKENDS = """
duty: закладки
charter:
  max_stages: 8
  max_rounds_replan: 2
stages:
  - id: coordinator
    role: coordinator
    workspace: none
  - id: close
    role: coordinator
    needs: [coordinator]
    workspace: none
"""


def bookends(tmp_path: Path, name: str = "bookends.yaml") -> Any:
    return write_spec(tmp_path, BOOKENDS, name=name)


def report_from(spec: Any, stage_id: str, stages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """`runner.report_plan` от имени названного этапа плана (`report` знает только `plan`)."""
    stage = spec.stage(stage_id)
    assert stage is not None
    return runner.report_plan(
        {"id": DEAL}, spec, stage, {"plan": {"stages": stages, "reason": "порядок"}}, 1, events=(), now=NOW
    )


def test_close_waits_for_the_tail_of_the_plan(tmp_path: Path) -> None:
    """`close` получает хвост плана: закрытие не раньше работы, которую план объявил.

    Вход (`coordinator`) закрывается фактом принятия — его работу лид сделал
    инструментом. Поэтому единственная зависимость `close`, кроме входа, — работа
    плана; без неё дело можно было закрыть, не начав её.
    """
    spec = bookends(tmp_path)
    facts, why = report_from(spec, "coordinator", [work("impl", needs=())])
    assert why == ""

    graph = transit.graph(spec, facts)
    assert needs_of(graph)["close"] == ("coordinator", "impl")
    assert needs_of(graph)["coordinator"] == ()

    decision = transit.next_stage(spec, facts, NOW)
    assert decision.stages == ("impl",), decision.reason


def test_a_plan_fact_named_by_profile_still_links_the_close(tmp_path: Path) -> None:
    """Старый журнал с `source_stage: "lead"` читается так же, как новый.

    До правки предложение писало в `source_stage` профиль, и `close` оставался с
    `needs: [coordinator]`: ребро от плана к закрытию терялось молча.
    """
    spec = bookends(tmp_path)
    facts = events(
        proposed(1, [work("impl", needs=())], source_stage="lead"),
        accepted(1),
    )

    graph = transit.graph(spec, facts)
    assert needs_of(graph)["close"] == ("coordinator", "impl")


def test_accepted_revision_closes_the_plan_stage_from_any_state(tmp_path: Path) -> None:
    """Принятая ревизия закрывает вход, даже если репланов было больше потолка.

    `max_rounds_replan` ставит этапу плана `failed`; приём ревизии после этого —
    успех этапа, и он обязан его снять. Иначе закрытое дело объявлялось бы
    проваленным (`d_92177017`: вход остался `failed` после принятой ревизии 6).
    """
    spec = bookends(tmp_path)  # max_rounds_replan: 2
    facts = events(
        ("plan_rethink", {"failed_stage": "check", "kind": "contract", "round": 1}),
        ("plan_rejected", {"rev": 1, "why": "план отклонён"}),
        ("plan_rejected", {"rev": 2, "why": "план отклонён"}),
        ("plan_rejected", {"rev": 3, "why": "план отклонён"}),
    )
    spent = transit.stage_states(spec, facts)
    assert spent["coordinator"].state == "failed", "потолок репланов исчерпан"

    # Факт дописывается последним: `events()` нумерует с единицы, и склейка двух
    # пачек дала бы приём раньше отказов — свёртка читает журнал по `id`.
    facts = facts + [{**events(("plan_accepted", {"rev": 4, "by": "lead"}))[0], "id": len(facts) + 1}]
    closed = transit.stage_states(spec, facts)
    assert closed["coordinator"].state == "done"
    assert closed["coordinator"].fail_kind == ""


def test_stage_slice_marks_the_bookends_as_the_lead_work(tmp_path: Path) -> None:
    """Срез помечает закладки (`by_lead`): поверхность не рисует их «ждёт хода».

    Иначе лента выглядит как забытая работа: два этапа ждут хода, которого не
    будет никогда (`d_92177017`, `coordinator` и `close`).
    """
    spec = bookends(tmp_path)
    facts, _why = report_from(spec, "coordinator", [work("impl", needs=())])
    rows = {row["id"]: row for row in stagefeed.stages(spec, facts, NOW)}

    assert rows["coordinator"]["by_lead"] is True
    assert rows["close"]["by_lead"] is True
    assert rows["impl"]["by_lead"] is False
