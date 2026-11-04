"""Дерево попытки отводится от коммита, а не от ветки.

Живой прогон E2E 04.11 упёрся ровно в это: `git worktree add <путь> main` отказывает
`fatal: 'main' is already used by worktree at …`, потому что ветка `main` уже занята
главным checkout'ом. Второе дело с той же базой не могло начать работу вообще.
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


def _repo(tmp_path: Path) -> Path:
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
    return repo


def _spec(tmp_path: Path, repo: Path) -> Any:
    spec_module = _load("duty_spec_worktree_base", "spec.py")
    path = tmp_path / "duty.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            duty: probe
            repo: {repo}
            base: main
            stages:
              - id: impl
                role: implementer
                workspace: worktree
            """
        ),
        encoding="utf-8",
    )
    return spec_module.load_spec(path)


def test_worktree_from_branch_already_checked_out(tmp_path: Path) -> None:
    """База-ветка занята главным checkout'ом: дерево всё равно создаётся."""
    runner = _load("duty_runner_worktree_base", "runner.py")
    repo = _repo(tmp_path)
    spec = _spec(tmp_path, repo)
    stage = spec.stages[0]

    tree, cwd = runner.prepare_tree(spec, stage, tmp_path / "attempt")

    assert tree is not None and tree.is_dir() and cwd == tree
    assert (tree / "a.txt").read_text(encoding="utf-8") == "ok\n"


def test_attempt_tree_is_detached_not_on_the_branch(tmp_path: Path) -> None:
    """Попытка отведена от коммита: ветку она не держит и не двигает."""
    runner = _load("duty_runner_worktree_detach", "runner.py")
    repo = _repo(tmp_path)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    spec = _spec(tmp_path, repo)

    tree, _ = runner.prepare_tree(spec, spec.stages[0], tmp_path / "attempt")
    assert tree is not None

    branch = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "HEAD", "дерево попытки должно быть в detached HEAD"
    assert subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip() == head


def test_two_attempts_from_same_base_do_not_collide(tmp_path: Path) -> None:
    """Два дела с одной базой: второе дерево не падает на «already used»."""
    runner = _load("duty_runner_worktree_two", "runner.py")
    spec_module = _load("duty_spec_worktree_two", "spec.py")
    repo = _repo(tmp_path)
    path = tmp_path / "duty.yaml"
    path.write_text(
        textwrap.dedent(
            f"""
            duty: probe
            repo: {repo}
            base: main
            stages:
              - id: impl
                role: implementer
                workspace: worktree
            """
        ),
        encoding="utf-8",
    )
    spec = spec_module.load_spec(path)

    first, _ = runner.prepare_tree(spec, spec.stages[0], tmp_path / "one")
    second, _ = runner.prepare_tree(spec, spec.stages[0], tmp_path / "two")

    assert first is not None and second is not None
    assert first != second
    assert first.is_dir() and second.is_dir()
