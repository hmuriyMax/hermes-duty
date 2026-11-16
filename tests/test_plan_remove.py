"""План удаляет неначатый этап, чтобы освободить потолок `charter.max_stages`.

Свидетельство — живое дело `d_76b269ce` (14.11, хартия `max_stages: 6`): лид не мог
поправить начатый этап («правку начатого возвращает приговор ревью») и не мог
вырасти («граф расти не может — потолок хартии»), а мёртвый этап от прошлой
ревизии занимал место. Третий ход — снять неначатый этап — отсутствовал.

Проверяется: удаление принимается и убирает этап из графа; удалять нельзя начатый,
каркасный и несуществующий этап; наследник удалённого обязан назвать новое ребро;
потолок после удаления снова свободен.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_792_000_000
DEAL = "d_remove"


def load_module(name: str, filename: str) -> Any:
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

# Seed: вход, работа и потолок 3 — ровно столько, чтобы лишний этап не влезал.
SEED = """
duty: удаление
charter:
  max_stages: 3
  max_rounds_replan: 1
stages:
  - id: plan
    role: implementer
    workspace: none
  - id: impl
    role: implementer
    needs: [plan]
    workspace: none
"""


def write_spec(tmp_path: Path, text: str) -> Any:
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def events(*items: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": index + 1, "ts": NOW, "deal_id": DEAL, "kind": kind, "payload": payload, "source": "engine"}
        for index, (kind, payload) in enumerate(items)
    ]


def work(stage_id: str, *, needs: tuple[str, ...] = ("plan",), **extra: Any) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "id": stage_id,
        "role": "implementer",
        "needs": list(needs),
        "workspace": "none",
        "evidence": [{"cmd": "true", "must": 0}],
    }
    stage.update(extra)
    return stage


def plan(stages: list[dict[str, Any]], *, base: Any = None, started: tuple[str, ...] = ()) -> tuple:
    """`parse_plan` по хартии seed-спеки: без гейта плана и без движка."""
    charter = spec_module.Charter(max_stages=3, max_rounds_replan=1)
    return spec_module.parse_plan(
        stages,
        charter=charter,
        base_stages=base if base is not None else BASE,
        started_ids=started,
    )


BASE = {
    "plan": spec_module.Stage(id="plan", role="implementer", workspace="none"),
    "impl": spec_module.Stage(id="impl", role="implementer", needs=("plan",), workspace="none"),
    "check2": spec_module.Stage(id="check2", role="implementer", needs=("impl",), workspace="none"),
}


def test_removing_a_not_started_stage_is_accepted_and_frees_the_ceiling() -> None:
    """`{id, remove: true}` снимает мёртвый этап, и потолок снова пускает новый.

    Потолок хартии 3, в графе `plan` + `impl` + `check2` — место кончилось.
    Удаление `check2` и добавление `docs` в одной ревизии проходят: без удаления
    такая запись отвечала `charter.max_stages` и лид оставался без третьего хода.
    """
    without, found = plan([{"id": "check2", "remove": True}, work("docs")])
    assert found == [], [issue.message for issue in found]
    assert [stage.id for stage in without] == ["docs"]

    refused, issues = plan([work("docs")])
    assert any(issue.rule == "charter.max_stages" for issue in issues), [issue.message for issue in issues]
    assert "check2" in next(issue.message for issue in issues if issue.rule == "charter.max_stages")


def test_removed_stage_leaves_the_graph(tmp_path: Path) -> None:
    """Граф читается по журналу: этап, снятый ревизией, из него уходит.

    `transit.graph` пересобирает граф разбором принятых ревизий; удаление
    переживает перечитывание, иначе дело продолжало бы показывать мёртвый этап, а
    потолок остался бы занятым.
    """
    spec = write_spec(tmp_path, SEED)
    facts = events(
        ("attempt_started", {"stage": "plan", "attempt": 1, "run_id": "plan#1"}),
        ("attempt_ended", {"stage": "plan", "attempt": 1, "outcome": "done", "kind": None, "reason": ""}),
        ("plan_proposed", {"rev": 1, "stages": [work("check2", needs=("impl",))], "reason": "", "source_stage": "plan"}),
        ("plan_accepted", {"rev": 1, "by": "engine"}),
        ("plan_proposed", {"rev": 2, "stages": [{"id": "check2", "remove": True}], "reason": "", "source_stage": "plan"}),
        ("plan_accepted", {"rev": 2, "by": "engine"}),
    )

    graph = {stage.id for stage in transit.graph(spec, facts)}

    assert "check2" not in graph, "ревизия 2 сняла этап"
    assert {"plan", "impl"} <= graph


def test_removing_a_started_stage_is_refused() -> None:
    """Сделанное возвращает приговор ревью, а не удаление из графа."""
    _, found = plan([{"id": "check2", "remove": True}], started=("check2",))
    assert [issue.rule for issue in found] == ["plan.remove.started"], [issue.message for issue in found]


def test_removing_the_frame_stage_is_refused() -> None:
    """Вход и закрытие объявляет спека: без них дело не закрыть."""
    _, found = plan([{"id": "plan", "remove": True}])
    assert [issue.rule for issue in found] == ["plan.remove.frame"], [issue.message for issue in found]


def test_removing_an_unknown_stage_is_refused() -> None:
    """Удалять можно только этап, который в графе есть."""
    _, found = plan([{"id": "nowhere", "remove": True}])
    assert [issue.rule for issue in found] == ["plan.remove"], [issue.message for issue in found]


def test_removal_cannot_carry_extra_fields() -> None:
    """Удаление называет только `id`/`remove`: правку делает запись без `remove`."""
    _, found = plan([{"id": "check2", "remove": True, "must": 7}])
    assert [issue.rule for issue in found] == ["plan.remove"], [issue.message for issue in found]


def test_a_heir_of_a_removed_stage_must_name_a_new_edge() -> None:
    """Наследник удалённого этапа обязан назвать новое ребро в том же предложении.

    Иначе он ждал бы вечно: готовность считается по графу, а удалённого в графе
    больше нет.
    """
    _, found = plan([{"id": "check2", "remove": True}, work("docs", needs=("check2",))])
    assert [issue.rule for issue in found] == ["plan.remove.needs"], [issue.message for issue in found]
