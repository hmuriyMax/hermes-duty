"""`duty_integrate`: работа дела переезжает в рабочую ветку человека.

Проверяется то, чего не видно в docstring, — что происходит с git на самом деле:

1. рабочая ветка — предок вершины дела: перенос быстрый, вершина рабочей ветки
   становится вершиной дела, а записанная при заведении ветка (`created.payload.
   branch`) важнее текущего HEAD;
2. ветки разошлись: `--no-ff`, то есть явный коммит слияния с подписью дела;
3. конфликт: отказ с именами файлов, `git merge --abort`, дерево остаётся чистым,
   рабочая ветка не сдвигается;
4. грязный рабочий каталог: отказ до любых действий (перенос затёр бы чужие правки);
5. работа уже в рабочей ветке: факт с `already: true`, ничего не двигается;
6. повтор с тем же `idempotency_key`: второго факта нет, второго слияния тоже;
7. закрытое дело: отказ — переносить в нём нечего.

Репозитории — одноразовые, в `tmp_path`: живой checkout плагина эти тесты не
трогают (он и не обязан быть чистым). Часы не читаются — `now` передаётся
аргументом, как и в остальных слоях.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

NOW = 1_792_000_000
DEAL = "d_integrate"

SPEC = """
duty: перенос работы
repo: {repo}
charter:
  max_stages: 6
  allowed_roles: [implementer]
  allowed_kinds: [agent, shell, gate]
stages:
  - id: impl
    role: implementer
    workspace: none
    evidence:
      - {{cmd: "true", must: 0}}
"""


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
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
tools = load_module("hermes_duty_tools", "tools.py")


# ------------------------------------------------------------------ фикстуры


def git(repo: Path, *args: str) -> str:
    """`git` в одноразовом репозитории; ненулевой код — падение теста, а не ответ."""
    done = subprocess.run(
        [
            "git",
            "-c", "user.name=duty-test",
            "-c", "user.email=duty-test@local",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr or done.stdout}"
    return done.stdout.strip()


def init_repo(path: Path) -> Path:
    """Репозиторий с одной веткой `main` и одним файлом в ней."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    (path / "file.txt").write_text("база\n", encoding="utf-8")
    git(path, "add", "file.txt")
    git(path, "commit", "-m", "base")
    return path


def commit(repo: Path, message: str, text: str = "", name: str = "file.txt") -> str:
    """Коммит в текущей ветке: без `text` — пустой (`--allow-empty`)."""
    if text:
        (repo / name).write_text(text, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "-m", message)
    else:
        git(repo, "commit", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def write_spec(tmp_path: Path, repo: Path) -> Any:
    path = tmp_path / "duty.yaml"
    path.write_text(SPEC.format(repo=repo), encoding="utf-8")
    return spec_module.load_spec(path)


def make_deal(conn: sqlite3.Connection, spec: Any, repo: Path, branch: Optional[str] = None) -> dict[str, Any]:
    """Дело с репозиторием и спекой; `branch` — рабочая ветка, снятая при заведении."""
    created = deals.create_deal(
        conn,
        repo=str(repo),
        branch=branch,
        title="Перенос работы",
        body="",
        now=NOW,
        spec_path=str(spec.path),
    )
    deal = deals.deal_row(conn, str(created["deal"]["id"]))
    assert deal is not None
    return deal


def append(conn: sqlite3.Connection, deal: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    journal.append(
        conn,
        [
            {
                "ts": NOW + 1,
                "recorded_at": NOW + 1,
                "deal_id": str(deal["id"]),
                "kind": kind,
                "payload": payload,
                "source": "engine",
                "idempotency_key": f"test:{deal['id']}:{kind}",
            }
        ],
    )


def kinds(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[str]:
    return [str(event["kind"]) for event in journal.read_events(conn, str(deal["id"]))]


def refusal(call: Any, *args: Any, **kwargs: Any) -> str:
    """Причина отказа инструмента: отказ — исключение, ответ готовит обработчик."""
    with pytest.raises(tools.ToolsError) as caught:
        call(*args, **kwargs)
    return str(caught.value)


def deal_branch(deal: dict[str, Any]) -> str:
    return f"duty/{deal['id']}"


# --------------------------------------------------- 1. быстрый перенос (ff)


def test_integrate_fast_forwards_into_the_recorded_branch(tmp_path: Path) -> None:
    """Рабочая ветка — предок вершины дела: перенос быстрый, цель — записанная.

    Работа дела лежит поверх рабочей ветки, а каталог стоит на `main`: цель берётся
    из `created.payload.branch`, а не из текущего HEAD, и ссылка рабочей ветки
    едет вперёд без слияния. Каталог при этом не трогается — на нём другая ветка.
    """
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "work")
    git(repo, "checkout", "main")
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, repo)
    conn = open_store(home)
    deal = make_deal(conn, spec, repo, branch="work")
    git(repo, "checkout", "-b", deal_branch(deal), "work")
    probe = commit(repo, "probe", "работа дела\n")
    git(repo, "checkout", "main")

    answer = tools.integrate(str(deal["id"]), "int-1", home=home, now=NOW)

    assert answer["ok"] is True
    assert answer["integrated"] == {
        "from": deal_branch(deal),
        "into": "work",
        "commit": probe,
        "fast_forward": True,
        "already": False,
    }
    assert git(repo, "rev-parse", "work") == probe
    # Текущая ветка осталась на месте: перенос двигает рабочую, а не ту, что открыта.
    assert git(repo, "rev-parse", "main") != probe
    assert git(repo, "status", "--porcelain") == ""
    assert kinds(conn, deal)[-1] == "integrated"


# ------------------------------------------- 2. разошедшиеся ветки (--no-ff)


def test_integrate_merges_diverged_branches_with_no_ff(tmp_path: Path) -> None:
    """Ветки разошлись: явный коммит слияния с подписью дела, работа на месте.

    `--no-ff` не косметика: по коммиту слияния видно, что перенос был, — быстрый
    перенос и слияние выглядят в `git log` по-разному, и путать их нельзя.
    """
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "work")
    git(repo, "checkout", "main")
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, repo)
    conn = open_store(home)
    deal = make_deal(conn, spec, repo)
    git(repo, "checkout", "-b", deal_branch(deal), "main")
    probe = commit(repo, "deal side", "работа дела\n", name="deal.txt")
    git(repo, "checkout", "work")
    work_tip = commit(repo, "work side", "работа человека\n")

    answer = tools.integrate(str(deal["id"]), "int-2", home=home, now=NOW)

    assert answer["integrated"]["fast_forward"] is False
    assert answer["integrated"]["already"] is False
    assert answer["integrated"]["commit"] == probe
    merged = git(repo, "rev-parse", "HEAD")
    assert merged != work_tip
    assert git(repo, "log", "-1", "--format=%s", "HEAD") == f"[{deal['id']}] merge {deal_branch(deal)}"
    assert len(git(repo, "log", "-1", "--format=%p", "HEAD").split()) == 2
    # Обе стороны на месте: слияние не выбрало одну из веток.
    assert (repo / "file.txt").read_text(encoding="utf-8") == "работа человека\n"
    assert (repo / "deal.txt").read_text(encoding="utf-8") == "работа дела\n"
    assert git(repo, "merge-base", "--is-ancestor", work_tip, "HEAD") == ""
    assert git(repo, "status", "--porcelain") == ""


# ------------------------------------------------------- 3. конфликт слияния


def test_integrate_aborts_on_conflict_and_leaves_the_tree_clean(tmp_path: Path) -> None:
    """Конфликт: отказ с файлами, `merge --abort`, дерево и рабочая ветка целы."""
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "work")
    git(repo, "checkout", "main")
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, repo)
    conn = open_store(home)
    deal = make_deal(conn, spec, repo)
    git(repo, "checkout", "-b", deal_branch(deal), "main")
    commit(repo, "deal side", "работа дела\n")
    git(repo, "checkout", "work")
    work_tip = commit(repo, "work side", "работа человека\n")

    reason = refusal(tools.integrate, str(deal["id"]), "int-3", home=home, now=NOW)

    assert "file.txt" in reason
    assert "merge --abort" in reason
    assert "перенос делает человек" in reason
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "rev-parse", "HEAD") == work_tip
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert kinds(conn, deal) == ["created"]


# --------------------------------------------------- 4. грязный рабочий каталог


def test_integrate_refuses_on_a_dirty_worktree(tmp_path: Path) -> None:
    """Незакоммиченная работа в каталоге: отказ до git-действий, ветки не двигаются."""
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "work")
    git(repo, "checkout", "main")
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, repo)
    conn = open_store(home)
    deal = make_deal(conn, spec, repo)
    git(repo, "checkout", "-b", deal_branch(deal), "work")
    commit(repo, "probe", "работа дела\n")
    git(repo, "checkout", "work")
    before = git(repo, "rev-parse", "work")
    (repo / "file.txt").write_text("чужая правка\n", encoding="utf-8")

    reason = refusal(tools.integrate, str(deal["id"]), "int-4", home=home, now=NOW)

    assert "грязный" in reason
    assert git(repo, "rev-parse", "work") == before
    assert kinds(conn, deal) == ["created"]


# --------------------------------------------------- 5. работа уже перенесена


def test_integrate_reports_already_when_the_work_is_in_the_branch(tmp_path: Path) -> None:
    """Вершина дела уже вошла в рабочую ветку: `already: true`, ничего не двигается."""
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "work")
    git(repo, "checkout", "main")
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, repo)
    conn = open_store(home)
    deal = make_deal(conn, spec, repo)
    git(repo, "checkout", "-b", deal_branch(deal), "work")
    probe = commit(repo, "probe", "работа дела\n", name="deal.txt")
    git(repo, "checkout", "work")
    git(repo, "merge", "--ff-only", deal_branch(deal))
    moved = commit(repo, "work side", "работа человека\n")
    assert git(repo, "rev-parse", "duty/" + str(deal["id"])) == probe

    answer = tools.integrate(str(deal["id"]), "int-5", home=home, now=NOW)

    assert answer["integrated"] == {
        "from": deal_branch(deal),
        "into": "work",
        "commit": probe,
        "fast_forward": False,
        "already": True,
    }
    assert git(repo, "rev-parse", "work") == moved
    assert git(repo, "rev-parse", "main") != moved


# ---------------------------------------------------------------- 6. повтор


def test_integrate_repeat_writes_no_second_fact(tmp_path: Path) -> None:
    """Повтор с тем же ключом: прежний ответ, один факт, второго слияния нет."""
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "work")
    git(repo, "checkout", "main")
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, repo)
    conn = open_store(home)
    deal = make_deal(conn, spec, repo, branch="work")
    git(repo, "checkout", "-b", deal_branch(deal), "work")
    probe = commit(repo, "probe", "работа дела\n")
    git(repo, "checkout", "main")

    first = tools.integrate(str(deal["id"]), "int-6", home=home, now=NOW)
    after_first = kinds(conn, deal)
    second = tools.integrate(str(deal["id"]), "int-6", home=home, now=NOW + 60)

    assert second["repeated"] is True
    assert second["events"] == 0
    assert second["integrated"] == first["integrated"]
    assert kinds(conn, deal) == after_first
    assert kinds(conn, deal).count("integrated") == 1
    assert git(repo, "rev-parse", "work") == probe


# ---------------------------------------------------------- 7. закрытое дело


def test_integrate_refuses_on_a_closed_deal(tmp_path: Path) -> None:
    """Закрытое дело: отказ словами, git не трогается, факта нет."""
    repo = init_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "work")
    git(repo, "checkout", "main")
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, repo)
    conn = open_store(home)
    deal = make_deal(conn, spec, repo, branch="work")
    git(repo, "checkout", "-b", deal_branch(deal), "work")
    commit(repo, "probe", "работа дела\n")
    git(repo, "checkout", "main")
    before = git(repo, "rev-parse", "work")
    append(
        conn,
        deal,
        "completed",
        {"cause": deals.CLOSE_CAUSE_LEAD, "summary": "итог лида", "stage": "close"},
    )

    reason = refusal(tools.integrate, str(deal["id"]), "int-7", home=home, now=NOW)

    assert "закрыто" in reason
    assert git(repo, "rev-parse", "work") == before
    assert "integrated" not in kinds(conn, deal)
