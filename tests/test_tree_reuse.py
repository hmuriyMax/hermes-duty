"""Повтор после падения продолжается в сохранённом дереве, а не с базы.

Живой кейс 09.11 (`d_d5901481`): попытку `impl_close#1` убил разрыв гейтвея
(`Gateway disconnected`, код 1, `result.json` нет). Движок уберёг дерево
(`tree_kept`, 212 строк, 932 зелёных теста) — и тут же собрался начать повтор от
базы, то есть выбросить ровно эту работу. Координатору пришлось закрыть дело и
завести второе от коммита: смена id, лишняя спека, потерянная провенанс-цепочка.
Здесь проверяется, что повтор идёт в то же дерево и этот факт записан.
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
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init",
        ],
        check=True,
    )
    return repo


def _spec(tmp_path: Path, repo: Path) -> Any:
    spec_module = _load("duty_spec_tree_reuse", "spec.py")
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


def _deal(deal_id: str = "d_0000aaaa") -> dict[str, Any]:
    return {"id": deal_id}


class _Store:
    """Журнал в памяти: ровно то, что читает `kept_tree`."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def ended(self, *, stage: str, attempt: int, tree_kept: str | None) -> None:
        details = {} if tree_kept is None else {"tree_kept": tree_kept}
        self.events.append(
            {
                "kind": "attempt_ended",
                "payload": {"stage": stage, "attempt": attempt, "details": details},
            }
        )


def _patch_journal(runner: Any, store: _Store) -> None:
    runner.journal = type(
        "J",
        (),
        {
            "read_events": staticmethod(lambda conn, deal_id: list(store.events)),
            "append": staticmethod(lambda conn, events: store.events.extend(events)),
        },
    )


def test_kept_tree_is_the_tree_the_failed_attempt_named(tmp_path: Path) -> None:
    """Сохранённое дерево берётся из факта `details.tree_kept` прошлой попытки."""
    runner = _load("duty_runner_tree_reuse_kept", "runner.py")
    tree = tmp_path / "tree"
    tree.mkdir()
    store = _Store()
    store.ended(stage="impl", attempt=1, tree_kept=str(tree))
    _patch_journal(runner, store)

    assert runner.kept_tree(None, _deal(), "impl", 2) == tree


def test_tree_gone_from_disk_is_a_record_not_a_tree(tmp_path: Path) -> None:
    """Запись о дереве, которого уже нет, — не повод начинать в пустоте."""
    runner = _load("duty_runner_tree_reuse_gone", "runner.py")
    store = _Store()
    store.ended(stage="impl", attempt=1, tree_kept=str(tmp_path / "нет-такого"))
    _patch_journal(runner, store)

    assert runner.kept_tree(None, _deal(), "impl", 2) is None


def test_latest_ended_attempt_decides(tmp_path: Path) -> None:
    """Решает последняя закончившаяся попытка: она не сохранила дерево — начинаем с базы."""
    runner = _load("duty_runner_tree_reuse_latest", "runner.py")
    tree = tmp_path / "tree"
    tree.mkdir()
    store = _Store()
    store.ended(stage="impl", attempt=1, tree_kept=str(tree))
    store.ended(stage="impl", attempt=2, tree_kept=None)
    _patch_journal(runner, store)

    assert runner.kept_tree(None, _deal(), "impl", 3) is None


def test_prepare_tree_continues_in_the_kept_tree(tmp_path: Path) -> None:
    """Дерево от той же базы переиспользуется: нового worktree не заводят."""
    runner = _load("duty_runner_tree_reuse_prepare", "runner.py")
    repo = _repo(tmp_path)
    spec = _spec(tmp_path, repo)
    stage = spec.stages[0]
    first, _ = runner.prepare_tree(spec, stage, tmp_path / "attempt-1")
    (first / "a.txt").write_text("правка, которую нельзя потерять\n", encoding="utf-8")

    tree, cwd = runner.prepare_tree(spec, stage, tmp_path / "attempt-2", reuse=first)

    assert tree == first and cwd == first
    assert (tree / "a.txt").read_text(encoding="utf-8") == "правка, которую нельзя потерять\n"
    assert not (tmp_path / "attempt-2" / "tree").exists()


def test_tree_from_another_base_is_not_reused(tmp_path: Path) -> None:
    """Дерево от чужой базы не годится: в нём другой код, этап проверил бы не то."""
    runner = _load("duty_runner_tree_reuse_foreign", "runner.py")
    repo = _repo(tmp_path)
    spec = _spec(tmp_path, repo)
    stage = spec.stages[0]
    foreign, _ = runner.prepare_tree(spec, stage, tmp_path / "attempt-1")
    # Ветка мимо базы: свой корень, общих предков нет.
    _git(foreign, "checkout", "-q", "--orphan", "other")
    (foreign / "b.txt").write_text("другой код\n", encoding="utf-8")
    _git(foreign, "add", ".")
    _git(foreign, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "other")

    tree, _ = runner.prepare_tree(
        spec, stage, tmp_path / "attempt-2", reuse=foreign
    )

    assert tree != foreign, "дерево от чужой базы взято быть не должно"
    assert tree is not None and (tree / "a.txt").is_file()


def test_open_tree_writes_the_reuse_fact(tmp_path: Path) -> None:
    """Повтор в сохранённом дереве виден в журнале фактом `tree_reused`."""
    runner = _load("duty_runner_tree_reuse_open", "runner.py")
    repo = _repo(tmp_path)
    spec = _spec(tmp_path, repo)
    stage = spec.stages[0]
    first, _ = runner.prepare_tree(spec, stage, tmp_path / "attempt-1")
    store = _Store()
    store.ended(stage="impl", attempt=1, tree_kept=str(first))
    _patch_journal(runner, store)

    tree, cwd = runner.open_tree(
        None,
        _deal(),
        spec,
        stage,
        "impl",
        2,
        tmp_path / "attempt-2",
        "main",
        clock=lambda: 1_700_000_000,
    )

    assert tree == first and cwd == first
    facts = [e for e in store.events if e.get("kind") == "tree_reused"]
    assert len(facts) == 1
    assert facts[0]["payload"] == {"stage": "impl", "attempt": 2, "from": str(first)}


def test_first_attempt_has_nothing_to_continue(tmp_path: Path) -> None:
    """У первой попытки прошлого дерева нет — и факта о продолжении тоже."""
    runner = _load("duty_runner_tree_reuse_first", "runner.py")
    repo = _repo(tmp_path)
    spec = _spec(tmp_path, repo)
    stage = spec.stages[0]
    store = _Store()
    _patch_journal(runner, store)

    tree, _ = runner.open_tree(
        None,
        _deal(),
        spec,
        stage,
        "impl",
        1,
        tmp_path / "attempt-1",
        "main",
        clock=lambda: 1_700_000_000,
    )

    assert tree is not None and tree.is_dir()
    assert [e for e in store.events if e.get("kind") == "tree_reused"] == []
