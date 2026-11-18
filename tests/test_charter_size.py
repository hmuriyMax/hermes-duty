"""Потолок графа масштабируется размером дела: `charter.size: S|M|L` (0.2.2).

Живой журнал: старый шаблон чартера держал 6 этапов, а дела недели росли до 7–12
(`d_c13d53da` — 12 этапов и 10 попыток), а тупик «правку начатого не принимаю и
граф расти не может» выходил вопросом человеку (`d_76b269ce`, вопрос #664). Одна
цифра на все дела этого не решает: размер дела оценивает тот, кто его заводит, и
потолок — пресет по размеру. Явный `max_stages` главнее пресета, чартер без `size`
живёт на умолчании движка — так старые спеки читаются без правки файла.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec_module = _load("duty_spec_charter_size", "spec.py")
runner = _load("duty_runner_charter_size", "runner.py")


def _spec(tmp_path: Path, charter: str) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(f"duty: потолок\ncharter:\n{charter}\n"), encoding="utf-8")
    return spec_module.load_spec(path)


def test_size_presets_scale_the_ceiling(tmp_path: Path) -> None:
    """`charter.size` задаёт потолок пресетом: S/M/L — 8/16/24 живых этапов."""
    small = _spec(tmp_path / "s", "  size: S\n")
    medium = _spec(tmp_path / "m", "  size: M\n")
    large = _spec(tmp_path / "l", "  size: L\n")

    assert (small.charter.size, small.charter.max_stages) == ("S", 8)
    assert (medium.charter.size, medium.charter.max_stages) == ("M", 16)
    assert (large.charter.size, large.charter.max_stages) == ("L", 24)


def test_explicit_max_stages_beats_the_preset(tmp_path: Path) -> None:
    """Явный `max_stages` главнее пресета: хартия говорит числом — число и берётся."""
    spec = _spec(tmp_path, "  size: L\n  max_stages: 10\n")

    assert (spec.charter.size, spec.charter.max_stages) == ("L", 10)


def test_charter_without_size_stays_valid_on_the_default(tmp_path: Path) -> None:
    """Чартер без `size` валиден и живёт на умолчании движка."""
    spec = _spec(tmp_path, "  max_rounds_replan: 1\n")

    assert spec.charter.size == ""
    assert spec.charter.max_stages == spec_module.DEFAULT_MAX_STAGES
    assert spec_module.errors(spec_module.validate_spec(spec, profiles=spec_module.known_profiles())) == []


def test_unknown_size_is_refused(tmp_path: Path) -> None:
    """`size` вне S/M/L — отказ, а не молчаливое умолчание: потолок не угадывается."""
    spec = _spec(tmp_path, "  size: XL\n")

    assert "charter.size" in {issue.rule for issue in spec.parse_issues}


def test_small_ceiling_for_a_big_deal_is_a_warning(tmp_path: Path) -> None:
    """Маленький потолок — предупреждение `spec check`, а не отказ.

    Текст ровно про тупик: при падении этапа лид не сможет ни поправить его, ни
    добавить замену (`d_76b269ce`, вопрос #664).
    """
    spec = _spec(tmp_path, "  size: L\n  max_stages: 6\n")

    found = spec_module.validate_spec(spec, profiles=spec_module.known_profiles())

    assert spec_module.errors(found) == []
    warnings = [issue for issue in found if issue.rule == "charter.max_stages"]
    assert warnings, [str(issue) for issue in found]
    assert "мал для дела размера L" in warnings[0].message
    assert "ни поправить его, ни добавить замену" in warnings[0].message


def test_charter_payload_names_where_the_ceiling_comes_from(tmp_path: Path) -> None:
    """Лид видит не только число, но и источник потолка: пресет размера или хартию."""
    preset = runner.charter_payload(_spec(tmp_path / "preset", "  size: M\n"))

    assert (preset["max_stages"], preset["size"]) == (16, "M")
    assert preset["max_stages_source"] == "size M"

    plain = runner.charter_payload(_spec(tmp_path / "plain", "  max_stages: 5\n"))
    assert plain["max_stages_source"] == "explicit max_stages or default"


def test_live_and_removed_stages_fit_the_ceiling(tmp_path: Path) -> None:
    """N живых + M снятых при `max_stages: N` принимаются, а отказ называет место.

    Потолок считает живые этапы: снятый записью `{id, remove: true}` место
    освобождает (сделано в 0.2.1 — здесь это проверяется рядом с пресетом).
    """
    base = {
        "plan": spec_module.Stage(id="plan", role="lead", workspace="none"),
        "impl": spec_module.Stage(id="impl", role="implementer", needs=("plan",), workspace="none"),
        "dead": spec_module.Stage(id="dead", role="implementer", needs=("plan",), workspace="none"),
    }
    charter = spec_module.Charter(max_stages=3, allowed_roles=("implementer", "tester"))
    proposal = [
        {"id": "dead", "remove": True},
        {"id": "check", "role": "tester", "needs": ["impl"], "workspace": "none",
         "evidence": [{"cmd": "go test ./...", "must": 0}]},
    ]

    stages, found = spec_module.parse_plan(proposal, charter=charter, base_stages=base)

    assert found == []
    assert [stage.id for stage in stages] == ["check"]

    _over, refused = spec_module.parse_plan(
        proposal + [{"id": "check2", "role": "tester", "needs": ["check"], "workspace": "none",
                     "evidence": [{"cmd": "go test ./...", "must": 0}]}],
        charter=charter,
        base_stages=base,
    )
    assert "charter.max_stages" in {issue.rule for issue in refused}
    message = next(issue.message for issue in refused if issue.rule == "charter.max_stages")
    # Отказ называет, чем занято место, и как его освободить (сделано в 0.2.1).
    assert "check" in message and "check2" in message
    assert "remove: true" in message
    assert "dead" not in message  # снятый этап место не занимает
