"""План не растит дерево без базы: worktree-этап требует `base` (0.2.2).

Живое дело `d_95f55146` 17.11 18:14: seed-спека кнопки «Продолжить» несла `repo`,
но не `base`; лид запланировал worktree-этап, дерево отводилось «от ''», и
`git worktree add` падал `fatal: invalid reference:` — десять инфра-падений
подряд (`impl` ×5, `impl2` ×5), потому что падение мгновенное и лид успевал
добавить второй такой же этап. Дело поехало только после того, как человек вписал
`base` руками.

Правило судится там, где появляется НОВЫЙ этап — в `spec.parse_plan`, — и не
применяется на перечитывании журнала: у уже созданных дел с пустой базой отказ
затопил бы граф.
"""

from __future__ import annotations

import importlib.util
import subprocess
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


spec_module = _load("duty_spec_plan_base", "spec.py")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """Репозиторий дела с одним коммитом: база, которую можно проверить git'ом."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, head


def _work_stage(workspace: str | None = "worktree") -> dict[str, Any]:
    """Этап плана, который делает работу: вид agent, роль с терминалом, evidence есть."""
    stage: dict[str, Any] = {
        "id": "impl",
        "role": "implementer",
        "evidence": [{"cmd": "git rev-parse HEAD", "must": 0}],
    }
    if workspace is not None:
        stage["workspace"] = workspace
    return stage


def _issues(*, repo: Any = None, base: Any = None, raw: Any = None) -> list[Any]:
    """`parse_plan` с явными `repo`/`base`: без них базу не из чего проверить."""
    _stages, found = spec_module.parse_plan(
        raw if raw is not None else [_work_stage()],
        charter=spec_module.Charter(),
        base_stages={},
        repo=repo,
        base=base,
    )
    return found


def test_worktree_stage_without_base_is_refused() -> None:
    """Пустая база при worktree-этапе — отказ `plan.base`, а не падение попытки."""
    found = _issues(repo="/tmp/deal-repo", base=None)

    assert [issue.rule for issue in found] == ["plan.base"]
    assert "дело создано спекой без base" in found[0].message
    assert "workspace: none" in found[0].message


def test_worktree_stage_without_repo_is_refused() -> None:
    """Без `repo` дерево тоже отводить не от чего: отказ того же правила."""
    found = _issues(repo=None, base=None)

    assert [issue.rule for issue in found] == ["plan.base"]
    assert "repo=— не назван" in found[0].message


def test_worktree_stage_with_unresolvable_base_names_the_string(tmp_path: Path) -> None:
    """База, которой нет в репозитории, — отказ, и он называет проверенную строку."""
    repo, head = _repo(tmp_path)

    found = _issues(repo=str(repo), base="deadbeef")

    assert [issue.rule for issue in found] == ["plan.base"]
    assert "deadbeef" in found[0].message
    assert str(repo) in found[0].message
    # А существующий коммит — не отказ: проверка не «на всякий случай».
    assert _issues(repo=str(repo), base=head) == []


def test_base_is_not_probed_when_the_repo_is_not_here(tmp_path: Path) -> None:
    """Репозитория нет на этой машине — доказательства нет, и отказа тоже.

    Та же граница, что у `spec.check_base`: «git не ответил» — не улика. Иначе
    план, приехавший из чужого окружения, отказывал бы на здоровой базе.
    """
    assert _issues(repo=str(tmp_path / "no-such-repo"), base="main") == []


def test_workspace_none_without_base_is_accepted() -> None:
    """`workspace: none` дерева не отводит — пустая база ему не мешает."""
    assert _issues(repo="/tmp/deal-repo", base=None, raw=[_work_stage("none")]) == []


def test_seed_spec_with_repo_and_without_base_still_passes_spec_check(tmp_path: Path) -> None:
    """Обратная совместимость: seed-спека с `repo` без `base` проходит `spec check`.

    Правило живёт в плане, а не в валидаторе seed: у уже созданных дел с пустой
    базой новый отказ затопил бы граф (`references/engine-map.md`), а план —
    единственное место, где появляется НОВЫЙ worktree-этап. Поэтому отказ
    `plan.base` в `SEED_RULES` не входит, и `check_seed` его не отдаёт.
    """
    path = tmp_path / "duty.yaml"
    path.write_text(
        textwrap.dedent(
            """
            duty: продолжаем
            repo: /tmp/deal-repo
            stages:
              - id: plan
                role: coordinator
                workspace: none
              - id: impl
                role: implementer
                needs: [plan]
                workspace: none
                evidence:
                  - cmd: "go build ./..."
                    must: 0
            """
        ),
        encoding="utf-8",
    )
    spec = spec_module.load_spec(path)

    assert spec.base is None
    assert spec_module.errors(spec_module.validate_spec(spec, profiles=spec_module.known_profiles())) == []
    assert spec_module.check_seed(spec) == []
    # Перечитывание журнала базу не судит: отказ там откатил бы граф к seed у
    # уже принятых ревизий.
    _stages, replayed = spec_module.parse_plan(
        [_work_stage()], charter=spec_module.Charter(), base_stages={}, replay=True
    )
    assert replayed == []
