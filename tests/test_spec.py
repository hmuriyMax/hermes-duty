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
    """Минимальный скелет годной спеки: вход impl и ревью за ним."""
    return f"""
    duty: probe
    stages:
      - id: impl
        role: implementer
      - id: review
        role: reviewer
        needs: [impl]
        {verdict}
    {extra}
    """


# ------------------------------------------------------------------ happy path


def test_full_example_is_ok(tmp_path: Path):
    """Полный пример из спеки-источника: ни одного отказа, база резолвится."""
    loaded = spec_module.load_spec(EXAMPLES / "duty.yaml")
    assert [stage.id for stage in loaded.stages] == ["impl", "verify", "review", "publish", "docs"]
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
          - id: review
            role: reviewer
            needs: [verify]
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


def test_publish_without_review_is_error(tmp_path: Path):
    """Публикация прямо от реализации — ровно «PUB обогнал RV»: спека не запускается."""
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
    found = spec_module.validate_spec(spec_module.load_spec(path), profiles=PROFILES)
    assert [(issue.rule, issue.line, issue.severity) for issue in found] == [("review-gate", 5, "error")]
    assert "этап 'publish' публикует, но в его предках нет ревью (role: reviewer)" in found[0].message
    assert str(found[0]).startswith(f"{path}:5: review-gate:")


def test_publish_after_review_is_ok(tmp_path: Path):
    """Ревью в предках — гейт закрыт: публикация после приговора законна."""
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
            verdict: {approve: next, changes: impl, max_rounds: 1}
          - id: publish
            role: publisher
            needs: [review]
        """,
    )
    assert found == []


def test_publish_after_stage_that_itself_after_review_is_ok(tmp_path: Path):
    """Ревью — транзитивно: `publish` после `verify`, а `verify` после `review`, законен."""
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
            verdict: {approve: next, changes: impl, max_rounds: 1}
          - id: verify
            role: tester
            needs: [review]
          - id: publish
            role: publisher
            needs: [verify]
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
          - id: review
            role: reviewer
            needs: [publish]
            verdict: {approve: next, changes: impl, max_rounds: 1}
        """,
    )
    assert [issue.rule for issue in found] == ["review-gate"]
    assert "publish" in found[0].message


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
            """,
        )
    )
    found = spec_module.validate_spec(loaded, profiles=PROFILES | {role})
    assert [issue.rule for issue in found] == ["review-gate"]


def test_risky_stage_without_review_is_error(tmp_path: Path):
    """`ask.risky: true` на этапе без ревью в предках — тоже публикация без приговора."""
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
            ask: {q: "Публикуем diff?", options: ["пушить", "стоп"], risky: true}
        """,
    )
    assert [issue.rule for issue in found] == ["review-gate"]
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
            verdict: {approve: next, changes: impl, max_rounds: 1}
          - id: ask
            role: documenter
            needs: [review]
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
    """`capability` повтором обычно не рассасывается — предупреждение, а не отказ.

    Осознанный выбор автора спеки законен: движок всё равно решает по политике,
    а валидатор не запрещает то, что объявлено нарочно.
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
    assert "capability обычно не рассасывается повтором; если это осознанно — оставь как есть" in found[0].message
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
    assert out.strip().endswith("предупреждений 0")


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
