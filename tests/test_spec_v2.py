"""Спека 2.0: только хартия — `docs/ENGINE-2.0.md`, разделы 3 и 12.

Четыре обещания карточки, каждое проверяется своим тестом:

1. спека 2.0 **без `stages`** проходит `duty spec check`: файл объявляет хартию,
   а этапы создаст лид (`duty_plan`). Дело без единого этапа — законное состояние,
   поэтому `entry` на пустом графе не отказывает;
2. спека 1.x со `stages` остаётся валидной, а объявленные этапы уходят лиду
   **в бриф** seed-подсказкой («вот как дело задумывали»), а не автографом;
3. `parse_plan` проверяет **одиночный этап** (`duty_start`) теми же правилами, что
   пачку: evidence, роль, вид — набор отказов совпадает с точностью до порядка;
4. `duty spec check` судит seed-этапы хартией (`spec.check_seed`), но правилами
   роста графа — нет: публикующий этап, координатор и вход без evidence в seed
   законны, это право 1.x (`docs/ENGINE-1.0.md`).

Профили ролей подставляются явно: список профилей — снимок каталога
`~/.hermes/profiles/*`, и тест не должен зависеть от того, кого завели на машине.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = 1_790_000_000

# Роли фикстур: `staging-tester` объявлен хартией 2.0 (раздел 3 контракта),
# `lead` — профиль, под которым живёт роль координатора (`spec.spawn_profile`).
PROFILES = frozenset(
    {
        "default",
        "documenter",
        "implementer",
        "lead",
        "planner",
        "publisher",
        "researcher",
        "reviewer",
        "staging-tester",
        "tester",
    }
)


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


# Имя модуля спеки то же, что у CLI (`SPEC_MODULE_NAME`): одна копия на процесс,
# иначе `spec_report` проверял бы спеку другим экземпляром валидатора.
spec_module = load_module("hermes_duty_spec", "spec.py")
cli_module = load_module("hermes_duty_cli_spectests_v2", "cli.py")
lead_module = load_module("hermes_duty_lead", "lead.py")


def write(tmp_path: Path, text: str) -> Path:
    """Спека на диске: путь абсолютный, как требует загрузчик."""
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def rules(found: list) -> set[str]:
    return {issue.rule for issue in found}


# ---------------------------------------------- 1. спека 2.0 без `stages` годна


def test_charter_only_spec_passes_spec_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Приёмка: `duty spec check` на спеке 2.0 — OK, этапов ноль, отказов ноль.

    Проверяется тот же путь, что у человека (`cli.spec_report`), а не только
    `validate_spec`: «проходит `spec check`» — это обещание про команду. Профили
    подменяются, потому что `spec_report` берёт их снимком с машины.
    """
    monkeypatch.setattr(spec_module, "known_profiles", lambda root=None: PROFILES)
    path = FIXTURES / "spec-v2-charter.yaml"

    payload = cli_module.spec_report(str(path))

    assert payload["ok"] is True, payload["issues"]
    assert (payload["errors"], payload["stages"]) == (0, 0)
    assert payload["issues"] == []
    rendered = cli_module.render_spec_check(payload)
    assert rendered.startswith("OK ") and "этапов 0" in rendered


def test_charter_only_spec_parses_the_2_0_ceiling_keys() -> None:
    """Хартия 2.0 читается целиком: потолки и публикация — рядом с прежними ключами."""
    loaded = spec_module.load_spec(FIXTURES / "spec-v2-charter.yaml")

    assert loaded.stages == () and loaded.raw_stages == ()
    charter = loaded.charter
    assert (charter.max_stages, charter.max_attempts, charter.max_lead_resumes) == (12, 3, 6)
    assert (charter.plan_gate.min_stages, charter.plan_gate.budget_share) == (5, 0.3)
    assert (charter.publish.human_gate, charter.publish.reviewer_role) == (True, "reviewer")
    assert spec_module.validate_spec(loaded, profiles=PROFILES) == []


def test_absent_charter_keys_get_the_1x_defaults(tmp_path: Path) -> None:
    """Спека без `charter` — прежние дефолты: `max_attempts: 3`, `max_lead_resumes: 6`."""
    loaded = spec_module.load_spec(write(tmp_path, "duty: probe\n"))

    assert (loaded.charter.max_attempts, loaded.charter.max_lead_resumes) == (
        spec_module.DEFAULT_MAX_ATTEMPTS,
        spec_module.DEFAULT_MAX_LEAD_RESUMES,
    )
    assert loaded.charter.publish == spec_module.Publish()
    assert spec_module.validate_spec(loaded, profiles=PROFILES) == []


@pytest.mark.parametrize(
    "charter, rule",
    [
        ("{max_attempts: 0}", "charter.max_attempts"),
        ("{max_attempts: много}", "charter.max_attempts"),
        ("{max_lead_resumes: -1}", "charter.max_lead_resumes"),
        ("{max_lead_resumes: много}", "charter.max_lead_resumes"),
        ("{publish: много}", "charter.publish"),
        ('{publish: {human_gate: "нет"}}', "charter.publish.human_gate"),
        ('{publish: {reviewer_role: ""}}', "charter.publish.reviewer_role"),
        ("{publish: {gate: true}}", "unknown-key"),
    ],
)
def test_charter_2_0_keys_are_refused_on_junk(tmp_path: Path, charter: str, rule: str) -> None:
    """Новые ключи хартии отказывают на мусоре там же, где прежние: `file:line`."""
    found = spec_module.validate_spec(
        spec_module.load_spec(write(tmp_path, f"duty: probe\ncharter: {charter}\n")),
        profiles=PROFILES,
    )
    assert rule in rules(found)


# ------------------------------- 2. спека 1.x: seed уходит лиду в бриф подсказкой


def test_v1_seed_spec_passes_and_reaches_the_brief() -> None:
    """Приёмка: 1.x годна, а её `stages` лид видит в брифе — и знает, что это seed.

    Бриф собирается настоящий (`lead.brief`), а не строкой: проверяется то, что
    модель прочитает первым заходом, — этапы, их роли/виды и оговорка, что план
    пишет она сама.
    """
    loaded = spec_module.load_spec(FIXTURES / "spec-v1-seed.yaml")
    assert spec_module.errors(spec_module.validate_spec(loaded, profiles=PROFILES)) == []

    deal = {"id": "d_seed0001", "title": "seed", "status": "waiting", "repo": None, "spec": str(loaded.path)}
    text = lead_module.brief(deal, [], lead_module.state([]), spec=loaded, now=NOW)

    assert "## Хартия и seed-план" in text
    assert "- plan: роль implementer, вид agent, needs: —" in text
    assert "- verify: роль tester, вид agent, needs: plan" in text
    assert "- review: роль reviewer, вид agent, needs: verify" in text
    assert "seed-план — подсказка «как дело задумывали», а не приговор" in text
    assert "план создаёшь ты" in text


def test_seed_over_the_plan_ceiling_is_refused(tmp_path: Path) -> None:
    """Seed не гниёт: файл не объявляет больше этапов, чем держит хартия."""
    found = spec_module.validate_spec(
        spec_module.load_spec(
            write(
                tmp_path,
                """
                duty: probe
                charter: {max_stages: 1}
                stages:
                  - id: impl
                    role: implementer
                    workspace: none
                  - id: review
                    role: reviewer
                    needs: [impl]
                    workspace: none
                """,
            )
        ),
        profiles=PROFILES,
    )

    offending = [issue for issue in found if issue.rule == "charter.max_stages"]
    assert offending and offending[0].severity == spec_module.ERROR
    assert "хартия держит не больше 1" in offending[0].message


def test_seed_evidence_on_role_without_terminal_is_refused(tmp_path: Path) -> None:
    """`evidence` у роли без терминала (ревьюер) не выполнится никогда — отказ."""
    found = spec_module.validate_spec(
        spec_module.load_spec(
            write(
                tmp_path,
                """
                duty: probe
                stages:
                  - id: impl
                    role: implementer
                    workspace: none
                  - id: review
                    role: reviewer
                    needs: [impl]
                    workspace: none
                    evidence:
                      - {cmd: "true", must: 0}
                """,
            )
        ),
        profiles=PROFILES,
    )

    assert "plan.evidence.role" in rules(found)


def test_seed_may_declare_publisher_and_coordinator(tmp_path: Path) -> None:
    """Права seed 1.x сохранены: публикация и координатор объявляются файлом.

    План такого добавить не вправе (`plan.publish`, `coordinator.scope`,
    `plan.evidence`), и именно поэтому эти правила из `check_seed` выброшены —
    иначе живые спеки 1.x (`continue-*`, `0.1.0-live-plan`) перестали бы
    запускаться в день релиза 2.0.
    """
    loaded = spec_module.load_spec(
        write(
            tmp_path,
            """
            duty: seed-rights
            charter: {allowed_roles: [publisher, reviewer], allowed_kinds: [agent]}
            stages:
              - id: coordinator
                role: coordinator
              - id: publish
                role: publisher
                needs: [coordinator]
                workspace: none
            """,
        )
    )

    found = spec_module.validate_spec(loaded, profiles=PROFILES)

    assert spec_module.errors(found) == [], "seed-права 1.x не отказывают"
    assert not rules(found) & {"plan.publish", "coordinator.scope", "plan.evidence"}


# --------------------------------- 3. одиночный этап проверяется как пачка


@pytest.mark.parametrize(
    "stage, rule",
    [
        ({"id": "impl", "role": "implementer", "workspace": "none"}, "plan.evidence"),
        (
            {
                "id": "check",
                "role": "tester",
                "workspace": "none",
                "evidence": [{"cmd": "true", "must": 0}],
            },
            "charter.allowed_roles",
        ),
        ({"id": "check", "kind": "shell", "cmd": "true"}, "charter.allowed_kinds"),
    ],
)
def test_single_stage_is_refused_like_a_batch(stage: dict[str, Any], rule: str) -> None:
    """Приёмка: `parse_plan` на одной записи даёт те же отказы, что на пачке.

    `duty_start` в 2.0 проверяет этап поодиночке (раздел 3 контракта), и слабее
    пачки он отказывать не вправе: evidence, роль и вид — те же правила.
    """
    charter = spec_module.Charter(allowed_roles=("implementer",), allowed_kinds=("agent",))

    single, single_issues = spec_module.parse_plan(stage, charter=charter, base_ids=())
    batch, batch_issues = spec_module.parse_plan([stage], charter=charter, base_ids=())

    assert [issue.rule for issue in single_issues] == [issue.rule for issue in batch_issues]
    assert [item.id for item in single] == [item.id for item in batch] == [stage["id"]]
    assert rule in rules(single_issues)


def test_single_stage_that_is_not_a_mapping_is_refused() -> None:
    """Не словарь — не этап: отказ `plan.stages`, а не выдуманная запись."""
    stages, found = spec_module.parse_plan("impl", charter=spec_module.Charter(), base_ids=())

    assert stages == ()
    assert rules(found) == {"plan.stages"}
