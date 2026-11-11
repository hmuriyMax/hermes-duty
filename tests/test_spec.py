"""Спека дела `duty.yaml`: по тесту на каждое правило отказа плюс happy path.

Тест держит контракт `docs/SPEC.md`: каждая строка там — правило, каждая
правило — отказ до запуска. Поэтому проверяется не «функция вернула список», а
**что именно** она назвала: правило (`issue.rule`), строку в файле (`issue.line`)
и то, что годная спека остаётся годной (два примера из `examples/`).

Профили роли передаются явно (`PROFILES`): список профилей — снимок каталога
`~/.hermes/profiles/*`, а тест не должен зависеть от того, кого завели на этой
машине. Проверка `base` как rev тоже подставляется (`exists=`), кроме одного
теста с настоящим временным репозиторием — там видно, что `git rev-parse
--verify` действительно вызывается.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

# Профили берём те, что объявлены в спеке-источнике: роли этапов и адреса
# фактовых вопросов.
PROFILES = frozenset(
    {"default", "implementer", "tester", "reviewer", "publisher", "documenter", "researcher", "planner"}
)


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


# Имя совпадает с тем, под которым модуль грузит CLI: одна копия модуля на
# процесс, значит `SpecError` у теста и у CLI — один и тот же класс.
spec_module = load_module("hermes_duty_spec", "spec.py")
cli_module = load_module("hermes_duty_cli_spectests", "cli.py")

SpecError = spec_module.SpecError


def write(tmp_path: Path, text: str, name: str = "duty.yaml") -> Path:
    """Спека на диске: путь абсолютный, как требует загрузчик."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def issues(tmp_path: Path, text: str) -> list:
    """Отказы и предупреждения спеки — с подставленным списком профилей."""
    return spec_module.validate_spec(spec_module.load_spec(write(tmp_path, text)), profiles=PROFILES)


def rules(found: list) -> set[str]:
    return {issue.rule for issue in found}


def two_stages(extra: str = "", verdict: str = "verdict: {approve: next, changes: impl, max_rounds: 1}") -> str:
    """Минимальный скелет годной спеки: вход impl и ревью за ним.

    У `review` дерево выключено явно: умолчание этапа с `needs` — `worktree`, а
    отводить его здесь не от чего (`repo`/`base` не объявлены). Тесты этого
    файла проверяют правила графа и приговора, а не дерево.
    """
    return f"""
    duty: probe
    stages:
      - id: impl
        role: implementer
      - id: review
        role: reviewer
        needs: [impl]
        workspace: none
        {verdict}
    {extra}
    """


# ------------------------------------------------------------------ happy path


def test_full_example_is_ok(tmp_path: Path):
    """Полный пример из спеки-источника: ни одного отказа, база резолвится."""
    loaded = spec_module.load_spec(EXAMPLES / "duty.yaml")
    assert [stage.id for stage in loaded.stages] == [
        "impl",
        "verify",
        "check",
        "review",
        "gate",
        "publish",
        "docs",
    ]
    assert spec_module.errors(spec_module.validate_spec(loaded, profiles=PROFILES)) == []
    # Репозиторий примера живёт не на каждой машине, поэтому проверка `base`
    # проверяется на копии спеки со здешним путём: правило то же, дерево есть.
    text = (EXAMPLES / "duty.yaml").read_text(encoding="utf-8")
    local = write(tmp_path, text.replace("~/GolandProjects/sx-widgets-api", str(tmp_path)))
    assert spec_module.check_base(spec_module.load_spec(local), exists=lambda repo, rev: True) == []


def test_minimal_example_is_ok():
    """Минимум: два этапа, ни evidence, ни ask, ни repo — годная спека."""
    loaded = spec_module.load_spec(EXAMPLES / "duty-minimal.yaml")
    assert [stage.id for stage in loaded.stages] == ["impl", "review"]
    assert spec_module.validate_spec(loaded, profiles=PROFILES) == []
    assert spec_module.check_base(loaded) == []


def test_retry_on_is_not_a_boolean_key():
    """`retry: {max: 2, on: [...]}`: YAML 1.1 читает `on` как True — ключ контрактный."""
    loaded = spec_module.load_spec(EXAMPLES / "duty.yaml")
    impl = loaded.stage("impl")
    assert impl.retry is not None
    assert impl.retry.max == 2
    assert impl.retry.on == ("infra", "contract")


def test_defaults_when_field_absent():
    """Поле этапа не задано — значения по умолчанию из контракта, а не догадки."""
    loaded = spec_module.load_spec(EXAMPLES / "duty-minimal.yaml")
    impl = loaded.stage("impl")
    assert impl.workspace == "none"
    assert impl.needs == ()
    assert impl.evidence == ()
    assert impl.ask is None


# ------------------------------------------------------- загрузчик: путь и YAML


def test_relative_path_rejected():
    """Относительный путь — отказ: «откуда взялась спека» не восстановить в журнале."""
    with pytest.raises(SpecError) as caught:
        spec_module.load_spec("examples/duty.yaml")
    assert caught.value.rule == "path"


def test_missing_file_rejected(tmp_path: Path):
    with pytest.raises(SpecError) as caught:
        spec_module.load_spec(tmp_path / "нет-такого.yaml")
    assert caught.value.rule == "path"


def test_yaml_syntax_error_names_line(tmp_path: Path):
    """Битый YAML — отказ со строкой, а не «не читается» без места."""
    path = write(tmp_path, "duty: probe\nstages: [\n")
    with pytest.raises(SpecError) as caught:
        spec_module.load_spec(path)
    assert caught.value.rule == "yaml"
    assert caught.value.line >= 2


def test_unknown_top_level_key(tmp_path: Path):
    """Опечатка в ключе верхнего уровня — отказ, а не «молча проглотить»."""
    found = issues(tmp_path, two_stages("repository: ~/x"))
    assert "unknown-key" in rules(found)
    assert any("repository" in issue.message for issue in found)


def test_unknown_stage_key(tmp_path: Path):
    """`evidance` вместо `evidence` иначе выглядел бы как этап без доказательств."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidance:
              - {cmd: "go build ./...", must: 0}
        """,
    )
    assert "unknown-key" in rules(found)


def test_unknown_evidence_key(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {cmd: "go build ./...", must: 0, return: 0}
        """,
    )
    assert "unknown-key" in rules(found)


def test_missing_duty_rejected(tmp_path: Path):
    with pytest.raises(SpecError) as caught:
        spec_module.load_spec(write(tmp_path, "stages:\n  - id: impl\n    role: implementer\n"))
    assert caught.value.rule == "duty"


def test_empty_stages_rejected(tmp_path: Path):
    with pytest.raises(SpecError) as caught:
        spec_module.load_spec(write(tmp_path, "duty: probe\nstages: []\n"))
    assert caught.value.rule == "spec"


# ------------------------------------------------------------------------- роли


def test_unknown_role(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: progger
        """,
    )
    assert "role" in rules(found)
    assert "progger" in found[0].message


def test_default_profile_is_a_role(tmp_path: Path):
    """`default` — профиль Hermes без папки в `profiles/`: он годится как роль."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: default
        """,
    )
    assert "role" not in rules(found)


def test_known_profiles_reads_directory_once(tmp_path: Path):
    """Список профилей — снимок каталога, а не вызов `hermes profiles list`."""
    (tmp_path / "tester").mkdir()
    (tmp_path / "implementer").mkdir()
    (tmp_path / "notes.txt").write_text("не профиль", encoding="utf-8")
    assert spec_module.known_profiles(tmp_path) == frozenset({"default", "tester", "implementer"})


def test_known_profiles_honours_hermes_home(tmp_path: Path, monkeypatch):
    """Корень профилей берётся из `HERMES_HOME`: на Windows home он вне `~`."""
    (tmp_path / "profiles" / "implementer").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(spec_module, "_profiles_cache", None)
    assert spec_module.profiles_dir() == tmp_path / "profiles"
    assert "implementer" in spec_module.known_profiles()


def test_known_profiles_without_hermes_home_uses_home_dir(tmp_path: Path, monkeypatch):
    """Без `HERMES_HOME` остаётся прежний корень `~/.hermes/profiles`."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert spec_module.profiles_dir() == tmp_path / ".hermes" / "profiles"


def test_profiles_dir_of_a_profile_home_is_one_level_up(tmp_path: Path, monkeypatch):
    """`HERMES_HOME` профиля (`<root>/profiles/<role>`) — профили на уровень выше.

    В профильной оболочке `$HERMES_HOME/profiles` не существует, и валидатор видел
    один `default`: любая спека с ролями отказывала «профиля нет в Hermes», хотя
    профили есть. Красная проба: убрать переход на уровень выше — падает этот тест
    и приёмки `duty spec check` в профильной оболочке.
    """
    role_home = tmp_path / "profiles" / "implementer"
    role_home.mkdir(parents=True)
    (tmp_path / "profiles" / "tester").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(role_home))
    monkeypatch.setattr(spec_module, "_profiles_cache", None)

    assert spec_module.profiles_dir() == tmp_path / "profiles"
    assert spec_module.known_profiles() == frozenset({"default", "implementer", "tester"})
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
        """,
    )
    assert "role" not in rules(found)


def test_profile_home_without_parent_profiles_dir_keeps_its_own(tmp_path: Path, monkeypatch):
    """Одноимённый каталог рядом — ещё не каталог профилей: свой важнее."""
    home = tmp_path / "profiles" / "role"
    (home / "profiles" / "default").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(spec_module, "_profiles_cache", None)

    assert spec_module.profiles_dir() == home / "profiles"


# ------------------------------------------------------------------- граф этапов


def test_needs_unknown_id(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: review
            role: reviewer
            needs: [impl, verify]
        """,
    )
    assert "needs" in rules(found)
    assert any("verify" in issue.message for issue in found)


def test_parallel_with_unknown_id(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: publish
            role: publisher
            needs: [impl]
            parallel_with: [docs]
        """,
    )
    assert "parallel_with" in rules(found)


def test_cycle_rejected(tmp_path: Path):
    """Цикл в needs: у `next(spec, events)` нет ответа — дело не запускается."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            needs: [review]
          - id: review
            role: reviewer
            needs: [impl]
        """,
    )
    assert "cycle" in rules(found)


def test_two_entries_rejected(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: publish
            role: publisher
        """,
    )
    assert "entry" in rules(found)


def test_no_entry_rejected(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            needs: [review]
          - id: review
            role: reviewer
            needs: [impl]
        """,
    )
    assert "entry" in rules(found)
    assert "cycle" in rules(found)


def test_unreachable_stage(tmp_path: Path):
    """Этап, до которого нет пути от входа, не выполнится никогда — отказ до запуска."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: orphan
            role: documenter
            needs: [loop]
          - id: loop
            role: documenter
            needs: [orphan]
          - id: review
            role: reviewer
            needs: [impl]
            verdict: {approve: next, changes: impl, max_rounds: 1}
        """,
    )
    assert "reachable" in rules(found)


def test_duplicate_stage_id(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: impl
            role: reviewer
            needs: [impl]
        """,
    )
    assert "stage.id" in rules(found)


# --------------------------------------------------------------- приговор ревью


def test_verdict_changes_must_be_predecessor(tmp_path: Path):
    """Тот самый гейт: `changes` вперёд — возврат перескочил бы проверку."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: review
            role: reviewer
            needs: [impl]
            verdict: {approve: next, changes: publish, max_rounds: 2}
          - id: publish
            role: publisher
            needs: [review]
        """,
    )
    assert "verdict.changes" in rules(found)
    assert any("не предшественник" in issue.message for issue in found)


def test_verdict_changes_transitive_predecessor_is_allowed(tmp_path: Path):
    """Предшественник — транзитивно: `review` может вернуть в `impl` через `verify`."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: verify
            role: tester
            needs: [impl]
            workspace: none
          - id: review
            role: reviewer
            needs: [verify]
            workspace: none
            verdict: {approve: next, changes: impl, max_rounds: 2}
        """,
    )
    assert found == []


def test_verdict_changes_unknown_id(tmp_path: Path):
    found = issues(tmp_path, two_stages(verdict="verdict: {approve: next, changes: nowhere, max_rounds: 1}"))
    assert "verdict.changes" in rules(found)


def test_verdict_changes_missing(tmp_path: Path):
    found = issues(tmp_path, two_stages(verdict="verdict: {approve: next, max_rounds: 1}"))
    assert "verdict.changes" in rules(found)


def test_verdict_approve_unknown_id(tmp_path: Path):
    found = issues(tmp_path, two_stages(verdict="verdict: {approve: publish, changes: impl, max_rounds: 1}"))
    assert "verdict.approve" in rules(found)


def test_verdict_max_rounds_zero(tmp_path: Path):
    found = issues(tmp_path, two_stages(verdict="verdict: {approve: next, changes: impl, max_rounds: 0}"))
    assert "verdict.max_rounds" in rules(found)


# ------------------------------------------------------------ гейт публикации


def test_publish_without_review_warns_and_does_not_refuse(tmp_path: Path):
    """Публикация прямо от реализации — предупреждение, а не отказ (ENGINE-2.0 §8).

    Форма графа ничего не гарантирует: граф пишет тот же лид, что по нему идёт, и
    может его переписать. Поэтому `review-gate` и `publish-gate` остаются
    подсказкой (`severity: warning`), а отказом становится отсутствие фактов в
    `duty_start` (`dashboard/publish.py`). Вернуть этим правилам `error` — значит
    снова отказывать по форме и снова не ловить публикацию без ревью.
    """
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: implementer
  - id: publish
    role: publisher
    needs: [impl]
    workspace: none
""",
    )
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    # Два предупреждения об одном: нет приговора и нет вопроса человеку. Оба — про
    # этап публикации и оба с его `file:line`, поэтому проверяются вместе.
    assert [(issue.rule, issue.line, issue.severity) for issue in found] == [
        ("review-gate", 5, "warning"),
        ("publish-gate", 5, "warning"),
    ]
    assert spec_module.errors(found) == [], "спека годна: отказов нет"
    assert "этап 'publish' публикует, но в его предках нет ревью (role: reviewer)" in found[0].message
    assert str(found[0]).startswith(f"{path}:5: review-gate:")


def test_publish_after_review_is_ok(tmp_path: Path):
    """Приговор и вопрос человеку в предках — публикация законна."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: review
            role: reviewer
            needs: [impl]
            workspace: none
            verdict: {approve: next, changes: impl, max_rounds: 1}
          - id: gate
            kind: gate
            needs: [review]
            ask: {q: "Публикуем?", options: [публиковать, стоп]}
            allow: [публиковать]
          - id: publish
            role: publisher
            needs: [gate]
            workspace: none
        """,
    )
    assert found == []


def test_publish_without_gate_warns_even_after_review(tmp_path: Path):
    """Ревью есть, человека не спросили — предупреждение `publish-gate`, не отказ.

    Приговор ревьюера гейта не заменяет — он про дифф, а «ок» публикации даёт
    человек. Но и отказом форма графа больше не приходит: публикацию остановит
    `duty_start`, не найдя факта ответа (`docs/ENGINE-2.0.md`, раздел 8).
    """
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: implementer
  - id: review
    role: reviewer
    needs: [impl]
    workspace: none
    verdict: {approve: next, changes: impl, max_rounds: 1}
  - id: publish
    role: publisher
    needs: [review]
    workspace: none
""",
    )
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    assert [(issue.rule, issue.severity) for issue in found] == [("publish-gate", "warning")]
    assert "публикация требует вопроса человеку (kind: gate) в предках" in found[0].message
    assert str(found[0]).startswith(f"{path}:10: publish-gate:")


def test_publish_gate_without_allow_warns(tmp_path: Path):
    """Гейт без `allow`: продолжением стал бы любой вариант, включая «не публиковать»."""
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: implementer
  - id: review
    role: reviewer
    needs: [impl]
    workspace: none
    verdict: {approve: next, changes: impl, max_rounds: 1}
  - id: gate
    kind: gate
    needs: [review]
    ask: {q: "Публикуем?", options: [публиковать, стоп]}
  - id: publish
    role: publisher
    needs: [gate]
    workspace: none
""",
    )
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    assert [(issue.rule, issue.severity) for issue in found] == [("publish-gate", "warning")]
    assert "не объявляет allow" in found[0].message
    assert str(found[0]).startswith(f"{path}:10: publish-gate:")


def test_publish_after_stage_that_itself_after_review_is_ok(tmp_path: Path):
    """Вопрос и приговор — транзитивно: `publish` после `verify`, а `verify` после гейта."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: review
            role: reviewer
            needs: [impl]
            workspace: none
            verdict: {approve: next, changes: impl, max_rounds: 1}
          - id: gate
            kind: gate
            needs: [review]
            ask: {q: "Публикуем?", options: [публиковать, стоп]}
            allow: [публиковать]
          - id: verify
            role: tester
            needs: [gate]
            workspace: none
          - id: publish
            role: publisher
            needs: [verify]
            workspace: none
        """,
    )
    assert found == []


def test_review_downstream_of_publish_does_not_count(tmp_path: Path):
    """Ревью после публикации предком не считается: обгон остаётся обгоном."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: publish
            role: publisher
            needs: [impl]
            workspace: none
          - id: review
            role: reviewer
            needs: [publish]
            workspace: none
            verdict: {approve: next, changes: impl, max_rounds: 1}
        """,
    )
    assert [issue.rule for issue in found] == ["review-gate", "publish-gate"]
    assert all("publish" in issue.message for issue in found)


@pytest.mark.parametrize("role", spec_module.PUBLISHING_ROLES)
def test_every_publishing_role_needs_review(tmp_path: Path, role: str):
    """Список публикующих ролей — одна константа: деплой не обходит гейт мимо `publisher`."""
    loaded = spec_module.load_spec(
        write(
            tmp_path,
            f"""
            duty: probe
            stages:
              - id: impl
                role: implementer
              - id: publish
                role: {role}
                needs: [impl]
                workspace: none
            """,
        )
    )
    found = spec_module.validate_spec(loaded, profiles=PROFILES | {role})
    assert [issue.rule for issue in found] == ["review-gate", "publish-gate"]


def test_risky_stage_without_review_warns(tmp_path: Path):
    """`ask.risky: true` на этапе без ревью в предках — предупреждение о том же."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: ask
            role: documenter
            needs: [impl]
            workspace: none
            ask: {q: "Публикуем diff?", options: ["пушить", "стоп"], risky: true}
        """,
    )
    assert [(issue.rule, issue.severity) for issue in found] == [("review-gate", "warning")]
    assert "этап 'ask'" in found[0].message


def test_risky_stage_after_review_is_ok(tmp_path: Path):
    """Рискованный этап после ревью — законен: человек спрошен уже после приговора."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: review
            role: reviewer
            needs: [impl]
            workspace: none
            verdict: {approve: next, changes: impl, max_rounds: 1}
          - id: ask
            role: documenter
            needs: [review]
            workspace: none
            ask: {q: "Публикуем diff?", options: ["пушить", "стоп"], risky: true}
        """,
    )
    assert found == []


def test_plain_stage_needs_no_review(tmp_path: Path):
    """Гейт не расширяется на всех: этап, который ничего не публикует, ревью не требует."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: docs
            role: documenter
            needs: [impl]
            workspace: none
        """,
    )
    assert found == []


# ------------------------------------------------------------------ доказательства

FORBIDDEN = ["git push origin HEAD", "git commit -m x", "rm -rf build", "sudo make install"]


@pytest.mark.parametrize("cmd", FORBIDDEN)
def test_evidence_command_is_action_not_evidence(tmp_path: Path, cmd: str):
    """`git push`/`git commit`/`rm -rf`/`sudo` — действие, а не доказательство."""
    found = issues(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {{cmd: "{cmd}", must: 0}}
        """,
    )
    assert "evidence.forbidden" in rules(found)
    assert any("действие, а не доказательство" in issue.message for issue in found)


def test_evidence_empty_cmd(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {cmd: "  ", must: 0}
        """,
    )
    assert "evidence.cmd" in rules(found)


def test_evidence_must_not_integer(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {cmd: "go build ./...", must: "ноль"}
        """,
    )
    assert "evidence.must" in rules(found)


@pytest.mark.parametrize("timeout", [0, 3601])
def test_evidence_timeout_out_of_range(tmp_path: Path, timeout: int):
    found = issues(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {{cmd: "go test ./...", must: 0, timeout_sec: {timeout}}}
        """,
    )
    assert "evidence.timeout" in rules(found)


def test_evidence_timeout_not_integer(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {cmd: "go test ./...", must: 0, timeout_sec: "10m"}
        """,
    )
    assert "evidence.timeout" in rules(found)


def test_evidence_timeout_at_limit_is_ok(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {cmd: "go test ./...", must: 0, timeout_sec: 3600}
        """,
    )
    assert found == []


# ------------------------------------------------------------------- workspace


def test_worktree_requires_repo(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        base: origin/master
        stages:
          - id: impl
            role: implementer
            workspace: worktree
        """,
    )
    assert "workspace" in rules(found)
    assert any("repo не задан" in issue.message for issue in found)


def test_worktree_requires_base(tmp_path: Path):
    found = issues(
        tmp_path,
        f"""
        duty: probe
        repo: {tmp_path}
        stages:
          - id: impl
            role: implementer
            workspace: worktree
        """,
    )
    assert "workspace" in rules(found)
    assert any("base не задан" in issue.message for issue in found)


def test_workspace_unknown_value(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            workspace: container
        """,
    )
    assert "workspace" in rules(found)


def test_repo_must_be_absolute(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        repo: GolandProjects/sx-widgets-api
        base: origin/master
        stages:
          - id: impl
            role: implementer
            workspace: worktree
        """,
    )
    assert "repo" in rules(found)


def test_base_missing_rev_is_error(tmp_path: Path):
    """`base` не резолвится как rev — отказ: дереву этапа не от чего отвестись."""
    loaded = spec_module.load_spec(
        write(
            tmp_path,
            f"""
            duty: probe
            repo: {tmp_path}
            base: origin/master
            stages:
              - id: impl
                role: implementer
                workspace: worktree
            """,
        )
    )
    found = spec_module.check_base(loaded, exists=lambda repo, rev: False)
    assert [issue.rule for issue in found] == ["base"]
    assert found[0].severity == "error"
    assert "не существует как rev" in found[0].message


def test_base_unavailable_repo_is_warning(tmp_path: Path):
    """Репозиторий дела не выложен на машине — предупреждение, а не отказ."""
    loaded = spec_module.load_spec(EXAMPLES / "duty.yaml")
    found = spec_module.check_base(loaded, exists=lambda repo, rev: pytest.fail("git звать некуда"))
    assert [issue.severity for issue in found] == ["warning"]
    assert "не проверен" in found[0].message


@pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")
def test_base_rev_is_checked_by_git(tmp_path: Path):
    """Настоящий git: HEAD резолвится, выдуманная база — нет."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "duty", "GIT_AUTHOR_EMAIL": "duty@example.com",
           "GIT_COMMITTER_NAME": "duty", "GIT_COMMITTER_EMAIL": "duty@example.com"}
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                       env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    good = spec_module.load_spec(
        write(tmp_path, f"duty: probe\nrepo: {repo}\nbase: HEAD\nstages:\n  - id: impl\n    role: implementer\n    workspace: worktree\n")
    )
    assert spec_module.check_base(good) == []
    bad = spec_module.load_spec(
        write(
            tmp_path,
            f"duty: probe\nrepo: {repo}\nbase: origin/master\nstages:\n  - id: impl\n    role: implementer\n    workspace: worktree\n",
            name="duty-bad.yaml",
        )
    )
    assert [issue.rule for issue in spec_module.check_base(bad)] == ["base"]


# ------------------------------------------------- умолчание workspace (W17)


def test_stage_with_needs_defaults_to_worktree(tmp_path: Path):
    """Умолчание этапа с предшественником — `worktree` (E2E `d_221d021a`).

    Молчание в спеке больше не значит «дерева нет»: этап, продолжающий работу
    предшественника, получает дерево, отведённое от его коммита.
    """
    loaded = spec_module.load_spec(
        write(
            tmp_path,
            f"""
            duty: probe
            repo: {tmp_path}
            base: origin/master
            stages:
              - id: impl
                role: implementer
                workspace: worktree
              - id: verify
                role: tester
                needs: [impl]
            """,
        )
    )
    verify_stage = loaded.stage("verify")
    assert verify_stage.workspace == "worktree"
    assert verify_stage.workspace_default is True
    assert loaded.stage("impl").workspace_default is False
    assert spec_module.validate_spec(loaded, profiles=PROFILES) == []


def test_entry_stage_keeps_none_default(tmp_path: Path):
    """У входа дерево не отводится: прежнее умолчание `none` осталось как было."""
    loaded = spec_module.load_spec(
        write(
            tmp_path,
            """
            duty: probe
            stages:
              - id: impl
                role: implementer
            """,
        )
    )
    impl = loaded.stage("impl")
    assert (impl.workspace, impl.workspace_default) == ("none", True)


def test_explicit_none_after_predecessor_warns_about_artifacts(tmp_path: Path):
    """Явный `none` у этапа с `needs` и доказательствами — предупреждение, не отказ."""
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: implementer
  - id: verify
    role: tester
    needs: [impl]
    workspace: none
    evidence:
      - {cmd: "go test ./...", must: 0}
""",
    )
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    assert [(issue.rule, issue.line, issue.severity) for issue in found] == [
        ("artifacts-without-tree", 8, "warning")
    ]
    assert "не получает дерево предшественника" in found[0].message
    assert "артефакты предшественника в нём не появятся" in found[0].message


def test_warning_about_tree_does_not_make_the_spec_invalid(tmp_path: Path):
    """Предупреждение годную спеку отказом не делает: `none` бывает осознанным."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: verify
            role: tester
            needs: [impl]
            workspace: none
            evidence:
              - {cmd: "go test ./...", must: 0}
        """,
    )
    assert spec_module.errors(found) == []
    assert [issue.rule for issue in found] == ["artifacts-without-tree"]


def test_none_without_evidence_is_silent(tmp_path: Path):
    """Этап без дерева и без доказательств ничего не теряет: предупреждения нет."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: ask
            role: documenter
            needs: [impl]
            workspace: none
        """,
    )
    assert found == []


def test_default_worktree_without_repo_names_the_default(tmp_path: Path):
    """Умолчание без `repo`/`base` — отказ, и он называет умолчание, а не «просьбу»."""
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: verify
            role: tester
            needs: [impl]
        """,
    )
    assert rules(found) == {"workspace"}
    assert any("получает worktree по умолчанию" in issue.message for issue in found)
    assert any("дерево не нужно — укажи workspace: none" in issue.message for issue in found)
    assert {issue.severity for issue in found} == {"error"}


# ------------------------------------------------------------ вопрос и бюджет


def test_ask_to_human_values(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            ask: {to_human: always, max_hops: 1}
        """,
    )
    assert "ask.to_human" in rules(found)


def test_ask_never_is_allowed(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            ask: {to_human: never, max_hops: 0}
        """,
    )
    assert found == []


def test_ask_max_hops_negative(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            ask: {to_human: decisions_only, max_hops: -1}
        """,
    )
    assert "ask.max_hops" in rules(found)


@pytest.mark.parametrize(
    ("ask", "rule"),
    [
        ("{allowed: researcher, to_human: decisions_only}", "ask.allowed"),
        ('{q: "Публикуем?", options: "пушить", risky: true}', "ask.options"),
    ],
)
def test_ask_nested_type_errors(tmp_path: Path, ask: str, rule: str):
    """Тип вложенного поля — тоже отказ: иначе `allowed: researcher` молча стало бы пустым."""
    found = issues(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: impl
            role: implementer
            ask: {ask}
        """,
    )
    assert rule in rules(found)


def test_repo_must_be_text(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        repo: 42
        base: origin/master
        stages:
          - id: impl
            role: implementer
        """,
    )
    assert "repo" in rules(found)


@pytest.mark.parametrize("budget", ["{tokens: 0, minutes: 30}", "{tokens: 200000, minutes: -5}"])
def test_budget_must_be_positive(tmp_path: Path, budget: str):
    found = issues(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: impl
            role: implementer
            budget: {budget}
        """,
    )
    assert "budget" in rules(found)


def test_retry_on_empty(tmp_path: Path):
    found = issues(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            retry: {max: 2, on: []}
        """,
    )
    assert "retry.on" in rules(found)


def test_retry_on_capability_is_warning_not_error(tmp_path: Path):
    """`capability` движок не повторяет никогда — предупреждение, а не отказ.

    `retry.on` набор **сужает**, но не расширяет (`docs/ENGINE-2.0.md`, раздел 7):
    объявленный `capability` не попадает в эффективную политику, и повтора не
    будет. Спека говорит об этом предупреждением, а не отказом: старые файлы
    остаются валидными, а автор видит, что строка не работает.
    """
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: implementer
    retry: {max: 2, on: [infra, capability]}
""",
    )
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    assert [(issue.rule, issue.line, issue.severity) for issue in found] == [("retry.on", 5, "warning")]
    assert "capability движок не повторяет никогда" in found[0].message
    assert "класс сужается" in found[0].message
    assert spec_module.errors(found) == []


# --------------------------------------------------------------- строка отказа


def test_issue_points_at_offending_line(tmp_path: Path):
    """`file:line` указывает на строку с ошибкой, а не на файл целиком."""
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: progger
  - id: review
    role: reviewer
    needs: [impl]
    workspace: none
    verdict: {approve: next, changes: impl, max_rounds: 1}
""",
    )
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    assert [(issue.rule, issue.line) for issue in found] == [("role", 4)]
    assert str(found[0]).startswith(f"{path}:4: role:")


# ------------------------------------------------------------------------ CLI


def run_cli(argv: list[str], capsys) -> tuple[int, str]:
    code = cli_module.main(argv)
    captured = capsys.readouterr()
    return code, captured.out


def test_cli_check_example_ok(capsys):
    """Приёмка: `spec check` на примере из спеки-источника печатает OK."""
    code, out = run_cli(["spec", "check", str(EXAMPLES / "duty.yaml")], capsys)
    assert code == 0
    assert out.startswith("OK ")


def test_cli_check_relative_path_is_resolved(capsys):
    """CLI разворачивает путь в абсолютный сам: движок принимает только абсолютный."""
    code, out = run_cli(["spec", "check", "examples/duty-minimal.yaml"], capsys)
    assert code == 0
    assert os.path.isabs(out.split()[1])


def test_cli_check_names_the_defaulted_workspace(capsys, tmp_path: Path):
    """Приёмка W17: старая спека E2E без `workspace` — видно, что этап получает дерево."""
    path = write(
        tmp_path,
        f"""duty: probe
repo: {tmp_path}/нет-репозитория
base: origin/master
stages:
  - id: impl
    role: implementer
    workspace: worktree
  - id: verify
    role: tester
    needs: [impl]
""",
    )
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 0
    assert "impl=worktree · verify=worktree (по умолчанию)" in out


def test_cli_check_prints_warning_on_its_own_line(capsys, tmp_path: Path):
    """Предупреждение печатается отдельной строкой и код возврата не меняет."""
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: implementer
  - id: verify
    role: tester
    needs: [impl]
    workspace: none
    evidence:
      - {cmd: "go test ./...", must: 0}
""",
    )
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("OK ")
    assert any(line.startswith(f"{path}:8: artifacts-without-tree:") for line in lines)


def test_cli_check_broken_spec_names_line(capsys, tmp_path: Path):
    """Сломанная спека — отказ с `file:line: правило: текст`, код 1."""
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: implementer
  - id: review
    role: reviewer
    needs: [impl]
    verdict: {approve: next, changes: publish, max_rounds: 2}
  - id: publish
    role: publisher
    needs: [review]
""",
    )
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 1
    assert f"{path}:8: verdict.changes:" in out
    # Предупреждение здесь одно и оно честное: гейта перед публикацией в спеке нет
    # (`publish-gate` — подсказка, а не отказ, `docs/ENGINE-2.0.md`, раздел 8).
    assert out.strip().endswith("предупреждений 1")


def test_cli_check_json_shape(capsys, tmp_path: Path):
    path = write(tmp_path, "duty: probe\nstages:\n  - id: impl\n    role: progger\n")
    code, out = run_cli(["spec", "check", str(path), "--json"], capsys)
    assert code == 1
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["path"] == str(path)
    assert payload["duty"] == "probe"
    assert payload["issues"][0]["rule"] == "role"
    assert payload["issues"][0]["severity"] == "error"


def test_cli_check_publish_before_review_names_line(capsys, tmp_path: Path):
    """Приёмка: спека «publish после impl» — отказ `review-gate` с `file:line`."""
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: impl
    role: implementer
  - id: publish
    role: publisher
    needs: [impl]
""",
    )
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 1
    assert f"{path}:5: review-gate:" in out
    assert "в его предках нет ревью" in out


def test_cli_check_unreadable_spec_exit_2(capsys, tmp_path: Path):
    """Нечитаемая спека — код 2: не список отказов, а отсутствие спеки."""
    path = write(tmp_path, "duty: probe\nstages: [\n")
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 2
    assert "yaml:" in out
    assert "спека не читается" in out


def test_cli_spec_without_subcommand(capsys):
    code, _ = run_cli(["spec"], capsys)
    assert code == 2


# --------------------------------------------------------------- вид этапа (kind)


def shell_spec(extra: str = "", stage: str = "") -> str:
    """Спека с этапом-командой: вход `check`, за ним `review` профилем.

    `check` — этап-команда (`kind: shell`): у неё нет роли, зато есть `cmd`.
    `stage` заменяет объявление `check` — на нём проверяются запреты.
    """
    body = stage or "    kind: shell\n    cmd: \"pytest tests/ -q\"\n"
    return (
        "duty: probe\n"
        "stages:\n"
        "  - id: check\n"
        + body
        + "  - id: review\n"
        "    role: reviewer\n"
        "    needs: [check]\n"
        "    workspace: none\n"
        + extra
    )


def test_shell_stage_without_role_is_ok(tmp_path: Path):
    """`kind: shell` + `cmd` — годная спека: роль команде не нужна, и это не отказ."""
    found = issues(tmp_path, shell_spec())
    assert found == []
    loaded = spec_module.load_spec(write(tmp_path, shell_spec()))
    stage = loaded.stage("check")
    assert (stage.kind, stage.cmd, stage.must, stage.artifacts) == ("shell", "pytest tests/ -q", 0, ())


def test_stage_without_kind_is_agent(tmp_path: Path):
    """Спека 0.0.4 читается без правок: нет `kind` — этап-агент.

    В примере теперь два не-агента (`kind: gate` и `kind: shell`, 0.0.5), поэтому
    «нет `kind`» читается по этапам, где он не объявлен: остальные — агенты.
    """
    loaded = spec_module.load_spec(EXAMPLES / "duty.yaml")
    assert {stage.id for stage in loaded.stages if stage.kind != spec_module.DEFAULT_STAGE_KIND} == {
        "check",
        "gate",
    }
    assert spec_module.errors(spec_module.validate_spec(loaded, profiles=PROFILES)) == []


def test_shell_with_role_is_refused(tmp_path: Path):
    """`kind: shell` + `role` — отказ с `file:line` и правилом, а не тихий игнор."""
    path = write(
        tmp_path,
        """duty: probe
stages:
  - id: check
    kind: shell
    cmd: "true"
    role: implementer
""",
    )
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    assert [(issue.rule, issue.line) for issue in found] == [("kind.role", 6)]
    assert "исполняет команда, а не профиль" in found[0].message
    assert str(found[0]).startswith(f"{path}:6: kind.role:")


def test_shell_without_cmd_is_refused(tmp_path: Path):
    found = issues(tmp_path, shell_spec(stage="    kind: shell\n"))
    assert "kind.cmd" in rules(found)
    assert any("нечего запускать" in issue.message for issue in found)


def test_shell_refuses_ask(tmp_path: Path):
    """Вопрос человеку — поле агента: у команды `ask` отказывает, а не игнорируется."""
    found = issues(
        tmp_path,
        shell_spec(stage="    kind: shell\n    cmd: \"true\"\n    ask: {allowed: [tester]}\n"),
    )
    assert "kind.ask" in rules(found)
    assert any("не задаёт вопросов" in issue.message for issue in found)


def test_shell_refuses_verdict(tmp_path: Path):
    found = issues(
        tmp_path,
        shell_spec(
            stage="    kind: shell\n    cmd: \"true\"\n    verdict: {approve: next, changes: check, max_rounds: 1}\n"
        ),
    )
    assert "kind.verdict" in rules(found)


def test_shell_refuses_retry(tmp_path: Path):
    """Политика повтора у команды — `must`, а не класс отказа: `retry` отказывает."""
    found = issues(tmp_path, shell_spec(stage="    kind: shell\n    cmd: \"true\"\n    retry: {max: 1, on: [infra]}\n"))
    assert "kind.retry" in rules(found)
    assert any("это must" in issue.message for issue in found)


def test_shell_refuses_publishing_cmd(tmp_path: Path):
    """Публикующая команда запрещена и этапу-команде: у публикации один путь — publisher."""
    found = issues(tmp_path, shell_spec(stage="    kind: shell\n    cmd: \"git push origin main\"\n"))
    assert "cmd.forbidden" in rules(found)
    assert any("дело роли publisher" in issue.message for issue in found)


def test_shell_evidence_is_warning_not_refusal(tmp_path: Path):
    """`evidence` у команды не проверяется — предупреждение, а не отказ спеки."""
    found = issues(
        tmp_path,
        shell_spec(stage="    kind: shell\n    cmd: \"true\"\n    evidence: [{cmd: \"go build ./...\", must: 0}]\n"),
    )
    assert [(issue.rule, issue.severity) for issue in found] == [("kind.evidence", "warning")]


def test_unknown_kind_is_refused(tmp_path: Path):
    """Неизвестный вид этапа — отказ со списком допустимых: опечатка не «agent»."""
    found = issues(tmp_path, shell_spec(stage="    kind: wizard\n    cmd: \"true\"\n"))
    assert "kind" in rules(found)
    assert any("допустимо agent, shell, gate" in issue.message for issue in found)


def test_agent_stage_refuses_cmd(tmp_path: Path):
    """Поля команды у агента — отказ: объявленное и не прочитанное — ложь спеки."""
    found = issues(tmp_path, shell_spec(stage="    role: implementer\n    cmd: \"true\"\n"))
    assert "kind.cmd" in rules(found)


def test_shell_must_must_be_int(tmp_path: Path):
    found = issues(tmp_path, shell_spec(stage="    kind: shell\n    cmd: \"true\"\n    must: true\n"))
    assert "kind.must" in rules(found)


def test_shell_artifacts_must_be_paths(tmp_path: Path):
    found = issues(
        tmp_path,
        shell_spec(stage="    kind: shell\n    cmd: \"true\"\n    artifacts: [{path: report.txt}]\n"),
    )
    assert "kind.artifacts" in rules(found)


def test_cli_check_names_shell_kind(capsys, tmp_path: Path):
    """Приёмка: `spec check` на спеке с `kind: shell` — OK и вид этапа назван."""
    path = write(tmp_path, shell_spec())
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 0
    assert out.startswith("OK ")
    assert "kind: check=shell" in out


def test_cli_check_shell_with_role_names_line(capsys, tmp_path: Path):
    """Приёмка: `kind: shell` + `role` — отказ с `file:line` и правилом."""
    path = write(
        tmp_path,
        "duty: probe\nstages:\n  - id: check\n    kind: shell\n    cmd: \"true\"\n    role: implementer\n",
    )
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 1
    assert f"{path}:6: kind.role:" in out


# --------------------------------------------------------------- гейт


def gate_body(ask: str = "    ask: {q: \"Выкатываем?\", options: [да, нет]}\n", extra: str = "") -> str:
    """Объявление этапа-гейта: вопрос и варианты ответа — его работа.

    `ask` и `extra` подменяют объявление целиком: на них проверяются отказы
    (нет `ask.q`, нет вариантов, чужое поле).
    """
    return "    kind: gate\n" + ask + extra


def gate_spec(*, stage: str = "", after: str = "") -> str:
    """Спека `impl → gate → check`: гейт в середине цепочки, как в приёмке.

    Замысел тот же, что у `shell_spec`: у `gate` нет роли и команды, у `check`
    (shell) нет роли, у `impl` — профиль. `stage` заменяет объявление гейта,
    `after` добавляет хвост (например `budget`).
    """
    body = stage or gate_body(extra=after)
    return (
        "duty: probe\n"
        "stages:\n"
        "  - id: impl\n"
        "    role: implementer\n"
        "  - id: gate\n"
        + body
        + "    needs: [impl]\n"
        "  - id: check\n"
        "    kind: shell\n"
        "    cmd: \"true\"\n"
        "    needs: [gate]\n"
        "    workspace: none\n"
    )


def test_gate_stage_with_question_is_ok(tmp_path: Path):
    """Гейт с `ask.q`/`ask.options` и без роли — годная спека."""
    found = issues(tmp_path, gate_spec())
    assert found == []
    loaded = spec_module.load_spec(write(tmp_path, gate_spec()))
    stage = loaded.stage("gate")
    assert (stage.kind, stage.role, stage.ask.q) == ("gate", "", "Выкатываем?")
    assert stage.ask.options == ("да", "нет")
    # Дерева у гейта нет даже после предшественника: исполнять ему нечего.
    assert (stage.workspace, stage.workspace_default) == ("none", True)


def test_gate_without_ask_is_refused(tmp_path: Path):
    """Гейту нечего спрашивать без `ask` — отказ, а не «этап без вопросов»."""
    found = issues(tmp_path, gate_spec(stage="    kind: gate\n"))
    assert "kind.ask" in rules(found)
    assert any("нечего спрашивать" in issue.message for issue in found)


def test_gate_without_question_text_is_refused(tmp_path: Path):
    """`ask` без `q`: строка вопроса — работа гейта, и её место в спеке."""
    found = issues(tmp_path, gate_spec(stage=gate_body(ask="    ask: {options: [да, нет]}\n")))
    assert "ask.q" in rules(found)


def test_gate_without_options_is_refused(tmp_path: Path):
    """`ask` без `options`: человеку не из чего выбирать — отказ."""
    found = issues(tmp_path, gate_spec(stage=gate_body(ask="    ask: {q: \"Выкатываем?\"}\n")))
    assert "ask.options" in rules(found)


def test_gate_empty_option_is_refused(tmp_path: Path):
    """Пустой вариант ответа неотличим от «не ответили» — отказ."""
    found = issues(tmp_path, gate_spec(stage=gate_body(ask="    ask: {q: \"Выкатываем?\", options: [да, \"\"]}\n")))
    assert "ask.options" in rules(found)


def test_gate_allow_must_name_declared_option(tmp_path: Path):
    """`allow` вне `options` — отказ: такого ответа не выбрать, и он бы не сработал."""
    found = issues(
        tmp_path,
        gate_spec(stage=gate_body(extra="    allow: [да, может быть]\n")),
    )
    assert "allow" in rules(found)
    assert any("которого нет в ask.options" in issue.message for issue in found)


def test_gate_allow_inside_options_is_ok(tmp_path: Path):
    """`allow: [да]` сужает продолжение до одного варианта — это годная спека."""
    found = issues(tmp_path, gate_spec(stage=gate_body(extra="    allow: [да]\n")))
    assert found == []
    assert spec_module.load_spec(write(tmp_path, gate_spec(stage=gate_body(extra="    allow: [да]\n")))).stage("gate").allow == ("да",)


def test_gate_refuses_role(tmp_path: Path):
    """Приёмка: гейт с `role` — отказ (`kind.role`)."""
    found = issues(tmp_path, gate_spec(stage=gate_body(extra="    role: implementer\n")))
    assert "kind.role" in rules(found)


def test_gate_refuses_workspace(tmp_path: Path):
    """Гейт не работает в дереве: `workspace` у него — отказ, а не настройка."""
    found = issues(tmp_path, gate_spec(stage=gate_body(extra="    workspace: none\n")))
    assert "kind.workspace" in rules(found)


def test_gate_refuses_verdict_retry_cmd(tmp_path: Path):
    """`verdict`, `retry`, `cmd` (и поля команды) у гейта — отказы."""
    found = issues(
        tmp_path,
        gate_spec(
            stage=gate_body(
                extra=(
                    "    verdict: {approve: next, changes: impl, max_rounds: 1}\n"
                    "    retry: {max: 1, on: [infra]}\n"
                    "    cmd: \"true\"\n"
                    "    must: 0\n"
                )
            )
        ),
    )
    assert {"kind.verdict", "kind.retry", "kind.cmd", "kind.must"} <= rules(found)


def test_gate_refuses_role_routing_of_the_question(tmp_path: Path):
    """Маршрут вопроса у гейта не объявляется: он спрашивает человека напрямую."""
    found = issues(
        tmp_path,
        gate_spec(stage=gate_body(ask="    ask: {q: \"Выкатываем?\", options: [да, нет], allowed: [reviewer]}\n")),
    )
    assert "kind.ask" in rules(found)


def test_allow_on_agent_stage_is_refused(tmp_path: Path):
    """`allow` — поле гейта: у агента оно отказывает, а не игнорируется."""
    found = issues(tmp_path, two_stages(extra="    allow: [да]\n"))
    assert "kind.allow" in rules(found)


def test_gate_chain_graph_is_checked_as_usual(tmp_path: Path):
    """Гейт в цепочке с `needs` — обычный этап графа: цикл и вход проверяются как всегда."""
    ok = issues(tmp_path, gate_spec())
    assert ok == []
    cyclic = issues(
        tmp_path,
        "duty: probe\n"
        "stages:\n"
        "  - id: impl\n"
        "    role: implementer\n"
        "    needs: [gate]\n"
        "  - id: gate\n"
        + gate_body()
        + "    needs: [impl]\n",
    )
    assert "cycle" in rules(cyclic)


def test_cli_check_names_gate_kind(capsys, tmp_path: Path):
    """Приёмка: `spec check` на спеке с гейтом — OK и вид этапа назван."""
    path = write(tmp_path, gate_spec())
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 0
    assert out.startswith("OK ")
    assert "kind: gate=gate" in out


def test_cli_check_gate_without_question_names_line(capsys, tmp_path: Path):
    """Приёмка: гейт без `ask.q`/`ask.options` — отказ с `file:line`."""
    path = write(
        tmp_path,
        "duty: probe\nstages:\n  - id: gate\n    kind: gate\n    ask: {}\n",
    )
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 1
    assert f"{path}:5: ask.q:" in out
    assert f"{path}:5: ask.options:" in out


def test_cli_check_gate_with_role_names_line(capsys, tmp_path: Path):
    """Приёмка: гейт с `role` — отказ с `file:line` и правилом `kind.role`."""
    path = write(
        tmp_path,
        "duty: probe\nstages:\n  - id: gate\n    kind: gate\n"
        "    ask: {q: \"Выкатываем?\", options: [да, нет]}\n    role: implementer\n",
    )
    code, out = run_cli(["spec", "check", str(path)], capsys)
    assert code == 1
    assert f"{path}:6: kind.role:" in out


# ----------------------------------------------------------------------- хартия


def charter_spec(charter: str = "charter: {max_stages: 6}") -> str:
    """Спека с хартией: вход `impl` и ревью за ним — форма из `two_stages`."""
    return f"""
    duty: probe
    {charter}
    stages:
      - id: plan
        role: planner
      - id: impl
        role: implementer
        needs: [plan]
        workspace: none
      - id: review
        role: reviewer
        needs: [impl]
        workspace: none
        verdict: {{approve: next, changes: impl, max_rounds: 1}}
    """


def test_charter_defaults_when_block_absent(tmp_path: Path):
    """Спека без `charter` годна и получает дефолты: 12 этапов, 3 реплана, гейт выключен."""
    loaded = spec_module.load_spec(write(tmp_path, charter_spec("")))
    charter = loaded.charter
    assert charter.max_stages == 12
    assert charter.max_rounds_replan == 3
    assert charter.allowed_roles == ()
    assert charter.allowed_kinds == ("agent", "shell", "gate")
    assert (charter.plan_gate.min_stages, charter.plan_gate.budget_share) == (0, 0.0)
    assert spec_module.validate_spec(loaded, profiles=PROFILES) == []


def test_charter_is_parsed_as_declared(tmp_path: Path):
    """Объявленная хартия читается целиком — и строками, и числами гейта."""
    loaded = spec_module.load_spec(
        write(
            tmp_path,
            charter_spec(
                "charter: {max_stages: 6, max_rounds_replan: 1, allowed_roles: [implementer, tester], "
                "allowed_kinds: [agent, shell], plan_gate: {min_stages: 4, budget_share: 0.5}}"
            ),
        )
    )
    charter = loaded.charter
    assert (charter.max_stages, charter.max_rounds_replan) == (6, 1)
    assert charter.allowed_roles == ("implementer", "tester")
    assert charter.allowed_kinds == ("agent", "shell")
    assert (charter.plan_gate.min_stages, charter.plan_gate.budget_share) == (4, 0.5)
    assert spec_module.validate_spec(loaded, profiles=PROFILES) == []


def test_charter_unknown_key_is_refused(tmp_path: Path):
    """Неизвестный ключ внутри `charter` — отказ: опечатка иначе была бы дефолтом."""
    found = issues(tmp_path, charter_spec("charter: {max_stage: 6}"))
    assert "unknown-key" in rules(found)
    assert any("max_stage" in issue.message for issue in found)


def test_charter_max_stages_must_be_positive(tmp_path: Path):
    """`max_stages: 0` — граф не из чего собрать: отказ с `file:line` числа."""
    path = write(tmp_path, charter_spec("charter: {max_stages: 0}"))
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    offending = [issue for issue in found if issue.rule == "charter.max_stages"]
    assert offending and offending[0].line == 3
    assert "не меньше одного этапа" in offending[0].message


def test_charter_max_rounds_replan_not_negative(tmp_path: Path):
    found = issues(tmp_path, charter_spec("charter: {max_rounds_replan: -1}"))
    assert "charter.max_rounds_replan" in rules(found)


def test_charter_unknown_kind_is_refused(tmp_path: Path):
    """Вид этапа, которого нет (`daemon`), плану разрешить нельзя."""
    found = issues(tmp_path, charter_spec("charter: {allowed_kinds: [agent, daemon]}"))
    assert "charter.allowed_kinds" in rules(found)


def test_charter_unknown_role_is_refused(tmp_path: Path):
    """Роль, которой нет в Hermes, — обещание, которого движок не выполнит."""
    found = issues(tmp_path, charter_spec("charter: {allowed_roles: [implementer, architector]}"))
    assert "charter.allowed_roles" in rules(found)
    assert any("architector" in issue.message for issue in found)


@pytest.mark.parametrize(
    "charter, rule",
    [
        ("charter: [max_stages]", "charter"),
        ("charter: {max_stages: много}", "charter.max_stages"),
        ("charter: {max_rounds_replan: много}", "charter.max_rounds_replan"),
        ("charter: {allowed_roles: implementer}", "charter.allowed_roles"),
        ("charter: {allowed_kinds: agent}", "charter.allowed_kinds"),
        ("charter: {plan_gate: много}", "charter.plan_gate"),
        ("charter: {plan_gate: {budget_share: половина}}", "charter.plan_gate"),
        ("charter: {plan_gate: {share: 0.5}}", "unknown-key"),
    ],
)
def test_charter_nested_type_errors(tmp_path: Path, charter: str, rule: str):
    found = issues(tmp_path, charter_spec(charter))
    assert rule in rules(found)


@pytest.mark.parametrize(
    "charter",
    [
        "charter: {max_stages: 6, plan_gate: {min_stages: -1}}",
        "charter: {max_stages: 6, plan_gate: {min_stages: 9}}",
        "charter: {max_stages: 6, plan_gate: {budget_share: 1.5}}",
        "charter: {max_stages: 6, plan_gate: {budget_share: -0.1}}",
    ],
)
def test_charter_plan_gate_thresholds(tmp_path: Path, charter: str):
    """Гейт плана: порог не отрицательный и не выше потолка графа, доля — от 0 до 1."""
    found = issues(tmp_path, charter_spec(charter))
    assert "charter.plan_gate" in rules(found)


# ------------------------------------------------------------------- план дела

# Граф-заготовка: seed из спеки плюс принятые раньше этапы.
BASE_IDS = ("plan", "close")


def plan(raw: list, charter: Any = None) -> tuple:
    """`spec.parse_plan` с хартией по умолчанию и seed-графом `BASE_IDS`."""
    return spec_module.parse_plan(raw, charter=charter or spec_module.Charter(), base_ids=BASE_IDS)


def plan_rules(raw: list, charter: Any = None) -> set[str]:
    return {issue.rule for issue in plan(raw, charter)[1]}


def good_plan() -> list:
    """Годное предложение: исследование, реализация и проверка за ней."""
    return [
        # У роли без терминала evidence не объявляется (`NO_SHELL_ROLES`): её
        # работа — отчёт, и команду ей выполнить нечем.
        {"id": "research", "role": "researcher", "workspace": "none", "needs": ["plan"]},
        {"id": "impl", "role": "implementer", "needs": ["research"],
         "evidence": [{"cmd": "go build ./...", "must": 0}]},
        {"id": "check", "kind": "shell", "cmd": "go test ./...", "needs": ["impl"]},
    ]


def test_parse_plan_accepts_a_plain_growth():
    """Годное предложение: этапы разобраны, отказов нет, `needs` видит seed."""
    stages, found = plan(good_plan())
    assert [stage.id for stage in stages] == ["research", "impl", "check"]
    assert found == []
    assert stages[1].needs == ("research",)


def test_parse_plan_gives_code_stage_a_tree_when_deal_has_repo():
    """Этап плана, который делает код, получает каталог дела — не каталог попытки.

    Красная проба: с `workspace: none` у входа (предшественника нет) импл работал
    там, где никто не проверяет, а `commit.resolve` искал коммит в каталоге попытки
    (d_e575494f). Явно названный `workspace` план не переписывает.
    """
    stages, found = spec_module.parse_plan(
        [{"id": "impl", "role": "implementer", "evidence": [{"cmd": "go build ./...", "must": 0}]}],
        charter=spec_module.Charter(),
        base_ids=BASE_IDS,
        repo="/tmp/deal-repo",
    )
    assert found == []
    assert stages[0].workspace == "dir"

    explicit, _ = spec_module.parse_plan(
        [{"id": "impl", "role": "implementer", "workspace": "none", "needs": ["plan"],
          "evidence": [{"cmd": "go build ./...", "must": 0}]}],
        charter=spec_module.Charter(),
        base_ids=BASE_IDS,
        repo="/tmp/deal-repo",
    )
    assert explicit[0].workspace == "none", "решение лида не переписывается"


def test_parse_plan_refuses_evidence_on_role_without_terminal():
    """Команда-доказательство у роли без терминала — отказ плана, а не сюрприз попытки.

    У `reviewer`/`researcher`/координатора `terminal` выключен: `must=0` нечестно
    только заявляется (d_e575494f — ревью ушло в переосмысление на
    `evidence.missing`).
    """
    # Координатор в позиции работника отказывает раньше (`coordinator.scope`),
    # поэтому правило проверяется на тех ролях, которые в граф попадают.
    for role in ("reviewer", "researcher"):
        _, found = plan(
            [{"id": "check", "role": role, "needs": ["plan"], "evidence": [{"cmd": "make test", "must": 0}]}]
        )
        assert "plan.evidence.role" in {issue.rule for issue in found}, role
    # Роль с терминалом то же самое объявляет законно.
    _, ok = plan(
        [{"id": "impl", "role": "implementer", "needs": ["plan"], "evidence": [{"cmd": "make test", "must": 0}]}]
    )
    assert ok == []


def test_parse_plan_needs_may_point_at_the_seed():
    """Ссылка на этап, который уже есть в графе, законна — это и есть рост."""
    _, found = plan([{"id": "research", "role": "researcher", "workspace": "none",
                      "needs": ["plan"]}])
    assert found == []


def test_parse_plan_does_not_redefine_existing_id():
    """`id` из seed планом не переопределяется: иначе публикация правила бы себя."""
    _, found = plan([{"id": "close", "role": "researcher", "workspace": "none",
                      "evidence": [{"cmd": "ls", "must": 0}]}])
    assert "plan.id" in {issue.rule for issue in found}


def test_parse_plan_duplicate_id_inside_proposal():
    _, found = plan([
        {"id": "research", "role": "researcher", "workspace": "none", "evidence": [{"cmd": "ls", "must": 0}]},
        {"id": "research", "role": "researcher", "workspace": "none", "evidence": [{"cmd": "ls", "must": 0}]},
    ])
    assert "plan.id" in {issue.rule for issue in found}


def test_parse_plan_needs_unknown_id():
    _, found = plan([{"id": "impl", "role": "implementer", "needs": ["nowhere"],
                      "evidence": [{"cmd": "go build ./...", "must": 0}]}])
    assert "plan.needs" in {issue.rule for issue in found}
    assert any("nowhere" in issue.message for issue in found)


def test_parse_plan_refuses_self_reference():
    _, found = plan([{"id": "impl", "role": "implementer", "needs": ["impl"],
                      "evidence": [{"cmd": "go build ./...", "must": 0}]}])
    assert "plan.needs" in {issue.rule for issue in found}


def test_parse_plan_refuses_a_cycle_inside_the_proposal():
    """Цикл плана — граф, по которому дело не пойдёт никогда: отказ до принятия."""
    _, found = plan([
        {"id": "a", "role": "implementer", "needs": ["b"], "workspace": "none",
         "evidence": [{"cmd": "ls", "must": 0}]},
        {"id": "b", "role": "implementer", "needs": ["a"], "workspace": "none",
         "evidence": [{"cmd": "ls", "must": 0}]},
    ])
    assert "plan.cycle" in {issue.rule for issue in found}


def test_parse_plan_stage_kind_must_be_allowed_by_charter():
    charter = spec_module.Charter(allowed_kinds=("agent",))
    _, found = plan([{"id": "check", "kind": "shell", "cmd": "go build ./..."}], charter)
    assert "charter.allowed_kinds" in {issue.rule for issue in found}


def test_parse_plan_stage_role_must_be_allowed_by_charter():
    charter = spec_module.Charter(allowed_roles=("implementer", "tester"))
    _, found = plan([{"id": "research", "role": "researcher", "workspace": "none",
                      "evidence": [{"cmd": "ls", "must": 0}]}], charter)
    assert "charter.allowed_roles" in {issue.rule for issue in found}


def test_parse_plan_empty_allowed_roles_does_not_restrict():
    """Пустой `allowed_roles` — хартия молчит: роль проверяет спека, а не план."""
    _, found = plan([{"id": "research", "role": "researcher", "workspace": "none"}],
                    spec_module.Charter())
    assert found == []


def test_parse_plan_cannot_add_publishing_stage():
    """`publish` планом не добавляется: публикация объявляется в seed-спеке."""
    _, found = plan([{"id": "publish", "role": "publisher", "workspace": "none"}])
    assert "plan.publish" in {issue.rule for issue in found}


def test_parse_plan_cannot_add_the_coordinator_as_a_worker():
    """Координатор ведёт граф, а не работу: этап-исполнитель им — отказ.

    Кейс d_48e01a0e: план объявил `check` ролью координатора, и сверка доки с
    кодом легла в его контекст вместо этапа `researcher`.
    """
    _, found = plan([{"id": "check", "role": "coordinator", "workspace": "none"}])
    assert "coordinator.scope" in {issue.rule for issue in found}
    assert any("координатор" in issue.message for issue in found if issue.rule == "coordinator.scope")


@pytest.mark.parametrize("role", ["coordinator", "lead"])
def test_parse_plan_coordinator_scope_covers_the_old_role_name(role: str):
    """`lead` — прежнее имя того же координатора: правило одно на оба."""
    _, found = plan([{"id": "check", "role": role, "workspace": "none"}])
    assert "coordinator.scope" in {issue.rule for issue in found}


def test_parse_plan_stage_gets_the_default_question_address():
    """Этап плана без `ask` получает адрес: фактовый вопрос — исследователю.

    Ни одна спека и ни один план блока `ask` не объявляли (0 из 77 этапов), и все
    58 вопросов журнала ушли человеку с причиной `allowed_empty`.
    """
    stages, found = plan([{"id": "impl", "role": "implementer", "workspace": "none",
                           "evidence": [{"cmd": "go build ./...", "must": 0}]}])
    assert found == []
    address = stages[0].ask
    assert address is not None
    assert address.allowed == ("researcher",)
    assert (address.to_human, address.max_hops) == ("decisions_only", 2)


def test_parse_plan_keeps_the_declared_question_address():
    """Объявленный `ask` плана не переписывается умолчанием."""
    stages, found = plan([{"id": "impl", "role": "implementer", "workspace": "none",
                           "evidence": [{"cmd": "go build ./...", "must": 0}],
                           "ask": {"allowed": [], "max_hops": 0}}])
    assert found == []
    assert (stages[0].ask.allowed, stages[0].ask.max_hops) == ((), 0)


def test_parse_plan_researcher_is_not_asked_its_own_question():
    """Роль не отвечает на собственный вопрос: себе адреса умолчание не даёт."""
    stages, _ = plan([{"id": "research", "role": "researcher", "workspace": "none",
                       "evidence": [{"cmd": "ls docs", "must": 0}]}])
    assert stages[0].ask is None


def test_parse_plan_cannot_add_a_publishing_command():
    """`git push` в команде плана — тот же запрет, что у спеки: действие, не работа."""
    _, found = plan([{"id": "check", "kind": "shell", "cmd": "git push origin HEAD"}])
    assert "cmd.forbidden" in {issue.rule for issue in found}


def test_parse_plan_evidence_command_cannot_publish():
    _, found = plan([{"id": "impl", "role": "implementer",
                      "evidence": [{"cmd": "git push", "must": 0}]}])
    assert "evidence.forbidden" in {issue.rule for issue in found}


def test_parse_plan_producing_stage_needs_evidence():
    """Этап, который что-то производит, без evidence — не план, а надежда."""
    _, found = plan([{"id": "impl", "role": "implementer", "workspace": "worktree"}])
    assert "plan.evidence" in {issue.rule for issue in found}


def test_parse_plan_researcher_needs_no_evidence():
    """Ресерчер команд не запускает — требовать с него evidence значит требовать ложь.

    Кейс d_48e01a0e: лид не смог запланировать исследовательский этап (валидатор
    требовал `evidence {cmd, must}`, а у профиля researcher нет терминала) и
    сделал всю сверку кода с докой сам в этапе координатора.
    """
    _, found = plan([{"id": "research", "role": "researcher", "workspace": "none"}])
    assert "plan.evidence" not in {issue.rule for issue in found}
    # Остальным производящим этапам evidence по-прежнему обязателен.
    _, found_impl = plan([{"id": "impl", "role": "implementer", "workspace": "worktree"}])
    assert "plan.evidence" in {issue.rule for issue in found_impl}


def test_parse_plan_documenter_needs_no_evidence():
    """Документер пишет документ, а не код — от него тоже нельзя требовать evidence.

    `docs/ENGINE-1.0.md` §9 ставит `docs (documenter)` последним рабочим этапом
    («что изменилось и как это проверять»): его результат — заметка тикета и
    отчёт, дерева репозитория он не меняет. Требование `evidence {cmd, must}` от
    плана означало бы, что такой этап невозможно добавить туда, где он и нужен —
    в конец дела.
    """
    _, found = plan([{"id": "docs", "role": "documenter", "workspace": "none"}])
    assert "plan.evidence" not in {issue.rule for issue in found}
    _, found_impl = plan([{"id": "impl", "role": "implementer", "workspace": "worktree"}])
    assert "plan.evidence" in {issue.rule for issue in found_impl}


def test_parse_plan_reviewer_and_gate_need_no_evidence():
    """Ревьюер предъявляет приговор, гейт — ответ человека: evidence им не нужен."""
    _, found = plan([
        {"id": "check", "role": "reviewer", "workspace": "none", "needs": ["plan"],
         "verdict": {"approve": "next", "changes": "plan", "max_rounds": 1}},
        {"id": "gate", "kind": "gate", "needs": ["check"],
         "ask": {"q": "Продолжаем?", "options": ["да", "нет"]}},
    ])
    assert found == []


def test_parse_plan_shell_stage_needs_cmd():
    _, found = plan([{"id": "check", "kind": "shell", "needs": ["plan"]}])
    assert "kind.cmd" in {issue.rule for issue in found}


def test_parse_plan_parallel_with_is_declared_by_the_plan():
    """`parallel_with` план объявляет (1.0): параллель — объявление, а не догадка.

    Проверяется то, что видно до графа: запись принята и поле доехало до этапа.
    Исполнение пары одновременно — дело `transit._batch`, и оно проверяется
    `tests/test_plan_reorder.py`.
    """
    stages, found = plan([{"id": "impl", "role": "implementer", "parallel_with": ["close"],
                           "evidence": [{"cmd": "ls", "must": 0}]}])
    assert found == []
    assert stages[0].parallel_with == ("close",)


def test_parse_plan_reorder_names_only_needs_and_parallel_with():
    """Перестановка этапа из графа не переписывает его: роль в ней — отказ."""
    _, found = plan([{"id": "close", "role": "researcher", "needs": []}])
    assert "plan.id" in {issue.rule for issue in found}


def test_parse_plan_reorder_of_a_started_stage_is_refused():
    """Этап, у которого была попытка, план не переставляет — это дело приговора."""
    stages, found = spec_module.parse_plan(
        [{"id": "close", "needs": []}],
        charter=spec_module.Charter(),
        base_ids=BASE_IDS,
        base_needs={"plan": (), "close": ("plan",)},
        started_ids=("close",),
    )
    assert stages == ()
    assert "plan.reorder" in {issue.rule for issue in found}


def test_parse_plan_cost_is_parsed_and_refused_when_not_positive():
    """`cost` — цена предложения в токенах; ноль и мусор — отказ, а не «дешёво»."""
    assert spec_module.parse_plan_cost({"tokens": 500})[0] == {"tokens": 500}
    assert spec_module.parse_plan_cost(None) == (None, [])
    for raw in ({"tokens": 0}, {"tokens": "много"}, {"tok": 5}, 500):
        assert "plan.cost" in {issue.rule for issue in spec_module.parse_plan_cost(raw)[1]}


def test_parse_plan_over_max_stages_is_refused():
    """Потолок хартии считает весь граф: seed плюс предложение."""
    charter = spec_module.Charter(max_stages=4)
    _, found = plan(good_plan(), charter)
    assert "charter.max_stages" in {issue.rule for issue in found}
    assert any("дело не растёт" in issue.message for issue in found)


def test_parse_plan_stage_without_id_is_an_issue_not_an_exception():
    """Этап без `id` — отказ в списке (лид увидит причину), а не падение разбора."""
    stages, found = plan([{"role": "researcher"}, {"id": "impl", "role": "implementer",
                                                  "evidence": [{"cmd": "ls", "must": 0}]}])
    assert [stage.id for stage in stages] == ["impl"]
    assert "stage.id" in {issue.rule for issue in found}


def test_parse_plan_unknown_stage_key_is_refused():
    """Разбор этапа тот же, что у спеки: `evidance` не станет «evidence пуст»."""
    _, found = plan([{"id": "impl", "role": "implementer",
                      "evidance": [{"cmd": "ls", "must": 0}]}])
    assert "unknown-key" in {issue.rule for issue in found}


def test_parse_plan_empty_or_not_a_list():
    for raw in ([], None, "impl"):
        _, found = plan(raw)
        assert "plan.stages" in {issue.rule for issue in found}
