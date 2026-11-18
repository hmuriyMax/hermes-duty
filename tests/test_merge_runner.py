"""Механика этапа-слияния (0.2.2): полосы, ветки, merge-коммит, резолвер.

Спека слоя уже проверена (`tests/test_merge_spec.py`): `kind: merge` разбирается,
полоса в общем каталоге отказывает, `plan.parallel.fanin` предупреждает. Здесь
проверяется **движок**: каждая полоса ведёт свою ветку, слияние собирает их в
один коммит, зависимый этап получает этот коммит базой, конфликт поднимает
резолвера с обоими диффами, а отказ называет файлы и оставляет деревья полос.

Ни одна попытка не поднимает модель: полосы — фейковый спавн, слияние делает
движок, резолвер — фейк, который реально разводит конфликт в живом репозитории и
коммитит (иначе merge-коммита не было бы вовсе, и тест проверял бы подделку).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Метка времени всех фактов теста: одинаковый `ts` ещё и проверяет, что порядок
# слияния берётся из объявления, а не из часов.
NOW = 1_790_000_000

POOL_SCHEMA = """
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  project TEXT,
  text TEXT NOT NULL,
  text_full TEXT,
  origin TEXT,
  options_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  sender_rowid INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  sent_at TEXT,
  answered_at TEXT,
  answer TEXT,
  timeout REAL,
  critical INTEGER NOT NULL DEFAULT 0,
  notified_at TEXT,
  reminder_every REAL NOT NULL DEFAULT 4.0,
  kind TEXT NOT NULL DEFAULT 'question',
  link TEXT,
  answered_via TEXT,
  external_notified_at TEXT,
  external_ref TEXT
);
"""


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина под тем же именем, что у остального кода: одна копия схемы."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = load_module("hermes_duty_runner", "runner.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
spec_module = load_module("hermes_duty_spec", "spec.py")
evidence_module = load_module("hermes_duty_evidence", "evidence.py")


# ------------------------------------------------------------------ фикстуры


def make_home(tmp_path: Path) -> Path:
    """Корень Hermes: своя `duty/duty.db` и свой пул вопросов."""
    home = tmp_path / "home"
    (home / "messaging").mkdir(parents=True)
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()
    return home


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(conn: sqlite3.Connection) -> dict[str, Any]:
    created = deals.create_deal(conn, repo="duty", title="Две полосы", body="", now=NOW)
    return deals.deal_row(conn, str(created["deal"]["id"]))


def git(cwd: Path, *args: str) -> str:
    """`git` в каталоге с фиксированным автором: иначе коммит упрётся в настройки."""
    done = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def git_repo(tmp_path: Path) -> Path:
    """Настоящий репозиторий с одним коммитом: полосы отводятся от него."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("ok\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    return repo


def lanes_spec(tmp_path: Path, repo: Path, *, names: tuple[str, str] = ("impl_b", "impl_a")) -> Any:
    """Спека двух полос и слияния: полосы в своих деревьях, порядок — объявленный.

    Порядок `needs` слияния назван нарочно не алфавитным: движок обязан вливать
    полосы в объявленном порядке, и тест это видит.
    """
    left, right = names
    return write_spec(
        tmp_path,
        f"""
        duty: lanes
        repo: {repo}
        base: HEAD
        stages:
          - id: impl_a
            role: implementer
            workspace: worktree
            parallel_with: [impl_b]
          - id: impl_b
            role: implementer
            workspace: worktree
            parallel_with: [impl_a]
          - id: join
            kind: merge
            needs: [{left}, {right}]
            workspace: worktree
          - id: gates
            role: tester
            workspace: worktree
            needs: [join]
        """,
    )


def write_spec(tmp_path: Path, text: str) -> Any:
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


class Fake:
    """Фейковый спавн попытки: пишет заданный отчёт и создаёт файлы в дереве."""

    def __init__(
        self,
        *,
        result: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, str]] = None,
        pack: bool = True,
    ) -> None:
        self.result: dict[str, Any] = result if result is not None else {"status": "done"}
        self.files = dict(files or {})
        self.pack = pack
        self.calls: list[dict[str, Any]] = []
        self.pid = 4242
        self.exit_code = 0
        self.exit: Optional[int] = 0
        self.stderr_text = ""
        self.stderr_head = ""

    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "Fake":
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        for name, text in self.files.items():
            target = Path(cwd) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self._report(Path(cmd[list(cmd).index("--query-file") + 1]), Path(cwd))
        return self

    def _report(self, query_file: Path, cwd: Path) -> None:
        contract = json.loads(query_file.read_text(encoding="utf-8"))
        target = Path(str(contract.get("report_to") or (query_file.parent / "result.json")))
        report = dict(self.result)
        if self.pack and "evidence" not in report:
            records: list[dict[str, Any]] = [
                {"cmd": str(item.get("cmd")), "exit": int(item.get("must", 0)), "started_at": NOW}
                for item in contract.get("evidence_to_produce") or []
            ]
            report["evidence"] = records
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        return None

    def finished(self) -> bool:
        return True

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.exit

    def stop(self, grace: float = 10) -> Optional[int]:
        return self.exit


class Lane(Fake):
    """Полоса: пишет свой файл и предъявляет его артефактом.

    Артефакт, а не коммит: коммит делает движок (`commit_tree`), и именно он потом
    становится базой слияния — тест проверяет движок, а не исполнителя.
    """

    def __init__(self, name: str, text: str) -> None:
        super().__init__(files={name: text})
        self.name = name

    def _report(self, query_file: Path, cwd: Path) -> None:
        contract = json.loads(query_file.read_text(encoding="utf-8"))
        target = Path(str(contract.get("report_to") or (query_file.parent / "result.json")))
        digest = __import__("hashlib").sha256((Path(cwd) / self.name).read_bytes()).hexdigest()
        report = {
            "status": "done",
            "summary": f"полоса {self.name}",
            "artifacts": [{"path": self.name, "sha256": digest}],
            "evidence": [],
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


class Resolver(Fake):
    """Резолвер: разводит конфликт в дереве и отчитывается слиянием.

    Контракт читается до работы — тест проверяет его содержимое на диске (оба
    диффа, конфликтные файлы), а не только в памяти. Коммит делается в состоянии
    merge, поэтому получает двух родителей — как у настоящего резолвера.
    """

    def __init__(
        self,
        *,
        text: Optional[str] = "from a and from b\n",
        sides: Optional[list[dict[str, Any]]] = None,
        taken: str = "",
        why: str = "",
        commit: bool = True,
        status: str = "done",
    ) -> None:
        super().__init__()
        self.text = text
        self.sides = sides
        self.taken = taken
        self.why = why
        self.commit = commit
        self.status = status
        self.contracts: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "Resolver":
        query_file = Path(cmd[list(cmd).index("--query-file") + 1])
        contract = json.loads(query_file.read_text(encoding="utf-8"))
        self.contracts.append(contract)
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        merge = contract.get("merge") or {}
        if self.text is not None and merge.get("conflict_files"):
            name = str(merge["conflict_files"][0])
            (Path(cwd) / name).write_text(self.text, encoding="utf-8")
            git(cwd, "add", name)
            if self.commit:
                git(cwd, "commit", "-q", "--no-edit")
        sha = git(cwd, "rev-parse", "HEAD") if self.commit else ""
        sides = self.sides
        if sides is None:
            sides = [
                {"lane": str(lane["lane"]), "sha": str(lane["sha"]), "intent": "свой файл", "joined": "влит"}
                for lane in merge.get("lanes") or []
            ]
        report: dict[str, Any] = {
            "status": self.status,
            "summary": "конфликт разведён",
            "evidence": [],
            "commit": sha,
            "merge": {
                "files": list(merge.get("conflict_files") or []),
                "sides": sides,
                "taken": self.taken,
                "why": self.why,
            },
        }
        target = Path(str(contract.get("report_to") or (query_file.parent / "result.json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return self


def events(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    return journal.read_events(conn, str(deal["id"]))


def ended_of(conn: sqlite3.Connection, deal: dict[str, Any], stage: str) -> dict[str, Any]:
    found = [
        dict(event["payload"])
        for event in events(conn, deal)
        if str(event["kind"]) == "attempt_ended" and str((event.get("payload") or {}).get("stage")) == stage
    ]
    assert found, f"этап {stage} не закрывался"
    return found[-1]


def lane_run(conn, spec, deal, home, stage: str, name: str, text: str) -> str:
    """Попытка полосы: свой файл, свой коммит (его делает движок) — и её ветка."""
    report = runner.run_stage(conn, spec, deal, stage, 1, home=home, spawn=Lane(name, text), clock=lambda: NOW)
    assert report["outcome"] == "done", report
    ended = ended_of(conn, deal, stage)
    assert ended["details"]["branch"] == runner.lane_branch(deal, stage), ended["details"]
    return str(ended["commit"])


def tree_of(home: Path, deal: dict[str, Any], stage: str) -> Path:
    return runner.attempt_dir(home, deal, stage, 1) / "tree"


# ------------------------------------- 1. чистое слияние: один merge-коммит


def test_two_lanes_from_one_base_merge_into_a_single_commit(tmp_path: Path) -> None:
    """Две полосы из общей базы сливаются в ОДИН коммит, и обе работы в нём.

    Это и есть fan-in, ради которого версия делалась: `parallel_with` был разрешён,
    а зависимый этап отказывался строиться, потому что дерево от одной полосы
    теряло работу другой (`d_c13d53da` 16.11, `d_1c53fbd0` — три отказа того же вида).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    sha_a = lane_run(conn, spec, deal, home, "impl_a", "a.txt", "from a\n")
    sha_b = lane_run(conn, spec, deal, home, "impl_b", "b.txt", "from b\n")
    assert sha_a != sha_b

    join = Fake()  # чистое слияние процесса не поднимает вовсе
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=join, clock=lambda: NOW)
    assert report["outcome"] == "done", json.dumps(report, ensure_ascii=False)
    assert join.calls == [], "чистое слияние не поднимает агента"

    merge_sha = str(report["commit"])
    ended = ended_of(conn, deal, "join")
    parents = git(repo, "rev-list", "--parents", "-n", "1", merge_sha).split()
    assert len(parents) == 3, f"merge-коммит с двумя родителями: {parents}"
    assert set(parents[1:]) == {sha_a, sha_b}, "в слиянии обе полосы, ни одна не потеряна"

    merged_tree = tree_of(home, deal, "join")
    assert (merged_tree / "a.txt").read_text(encoding="utf-8") == "from a\n"
    assert (merged_tree / "b.txt").read_text(encoding="utf-8") == "from b\n"
    assert ended["details"]["merge"]["order"] == ["impl_b", "impl_a"], "порядок объявления"


def test_the_stage_after_the_merge_is_based_on_the_merge_commit(tmp_path: Path) -> None:
    """Зависимый этап получает базой merge-коммит, а не одну из полос.

    Без этого fan-in остаётся мёртвым: `base_of_stage` видел два расходящихся
    коммита и отказывал «предшественники разошлись», даже когда слияние уже было.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    lane_run(conn, spec, deal, home, "impl_a", "a.txt", "from a\n")
    lane_run(conn, spec, deal, home, "impl_b", "b.txt", "from b\n")
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=Fake(), clock=lambda: NOW)
    merge_sha = str(report["commit"])

    gates = next(stage for stage in spec.stages if stage.id == "gates")
    assert runner.base_of_stage(spec, gates, events(conn, deal)) == merge_sha


def test_a_diverging_pair_without_a_merge_still_refuses_the_base(tmp_path: Path) -> None:
    """Граф без `merge` не отменяет отказа: «предшественники разошлись» остаётся.

    Отказ честен, пока слияния никто не объявил: дерево от одной полосы потеряло
    бы работу другой. `merge` — единственный ход, который его снимает.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    sha_a = lane_run(conn, spec, deal, home, "impl_a", "a.txt", "from a\n")
    sha_b = lane_run(conn, spec, deal, home, "impl_b", "b.txt", "from b\n")
    dependent = spec_module.Stage(id="gates", role="tester", needs=("impl_a", "impl_b"), workspace="worktree")
    with pytest.raises(runner.BaseError) as refused:
        runner.base_of_stage(SimpleNamespace(repo=str(repo), base="HEAD", stages=()), dependent, events(conn, deal))
    message = str(refused.value)
    assert sha_a[:12] in message and sha_b[:12] in message, message


def test_lanes_are_cleaned_up_after_the_merge_and_the_deal_branch_holds_it(tmp_path: Path) -> None:
    """Успешное слияние убирает ветки и деревья полос, дело держится merge-коммитом.

    Уборка идёт только после закрепления merge-коммита: до этого ветка полосы —
    единственная ссылка на её работу, и удалять её значило бы потерять сторону.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    lane_run(conn, spec, deal, home, "impl_a", "a.txt", "from a\n")
    lane_run(conn, spec, deal, home, "impl_b", "b.txt", "from b\n")
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=Fake(), clock=lambda: NOW)

    merge_sha = str(report["commit"])
    branches = git(repo, "branch", "--list", "--format=%(refname:short)")
    assert f"duty/{deal['id']}" in branches.splitlines()
    assert git(repo, "rev-parse", f"duty/{deal['id']}") == merge_sha
    assert runner.lane_branch(deal, "impl_a") not in branches.splitlines(), "ветка полосы убрана после слияния"
    assert runner.lane_branch(deal, "impl_b") not in branches.splitlines()
    assert not tree_of(home, deal, "impl_a").exists(), "дерево полосы убрано после слияния"
    assert not tree_of(home, deal, "impl_b").exists()
    cleaned = ended_of(conn, deal, "join")["details"]["merge"]["cleaned"]
    assert len(cleaned) == 2 and not ended_of(conn, deal, "join")["details"]["merge"]["kept"]


# ------------------------------------------ 2. конфликт: резолвер с двумя диффами


def test_a_conflict_raises_a_resolver_with_both_diffs(tmp_path: Path) -> None:
    """Конфликт двух полос поднимает резолвера: файлы, оба диффа, требования.

    Стратегией конфликт не «закрывается»: движок не делает `merge --abort`, он
    оставляет состояние в дереве этапа и поднимает агента-резолвера ролью этапа.
    Контракт несёт то, без чего намерения обеих сторон не сохранить: конфликтные
    файлы и по диффу на каждую полосу.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    sha_a = lane_run(conn, spec, deal, home, "impl_a", "shared.txt", "from a\n")
    sha_b = lane_run(conn, spec, deal, home, "impl_b", "shared.txt", "from b\n")

    resolver = Resolver()
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=resolver, clock=lambda: NOW)
    assert report["outcome"] == "done", report
    assert len(resolver.contracts) == 1, "на конфликт поднимается ровно один резолвер"

    contract = resolver.contracts[0]
    merge = contract["merge"]
    assert merge["conflict_files"] == ["shared.txt"]
    assert merge["order"] == ["impl_b", "impl_a"], "порядок слияния — объявление"
    lanes = {str(lane["lane"]): lane for lane in merge["lanes"]}
    assert set(lanes) == {"impl_a", "impl_b"}
    assert "from a" in lanes["impl_a"]["diff"] and "from b" in lanes["impl_b"]["diff"], lanes
    assert lanes["impl_a"]["sha"] == sha_a and lanes["impl_b"]["sha"] == sha_b
    boundaries = " ".join(contract["boundaries"])
    assert "сохрани намерения ОБЕИХ сторон" in boundaries
    assert "merge.files" not in boundaries and "report_schema" in contract
    assert "merge" in contract["report_schema"], "форма отчёта слияния — в контракте"

    # Слияние собрано резолвером: коммит с двумя родителями и содержимым обеих сторон.
    merge_sha = str(report["commit"])
    parents = git(repo, "rev-list", "--parents", "-n", "1", merge_sha).split()
    assert len(parents) == 3, parents
    merged = tree_of(home, deal, "join") / "shared.txt"
    assert merged.read_text(encoding="utf-8") == "from a and from b\n"


# ------------------------------- 3. «взял одну сторону целиком» без обоснования


def test_a_side_taken_whole_without_a_reason_is_refused(tmp_path: Path) -> None:
    """«Взял theirs целиком» без обоснования — отказ `merge.dropped`, а не решение.

    Резолвер вправе выбросить сторону, но обязан сказать почему: тихий выбор
    одной стороны — ровно та потеря логики, от которой версия уберегает.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    lane_run(conn, spec, deal, home, "impl_a", "shared.txt", "from a\n")
    lane_run(conn, spec, deal, home, "impl_b", "shared.txt", "from b\n")

    resolver = Resolver(text="from b\n", taken="impl_b")
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=resolver, clock=lambda: NOW)
    assert report["outcome"] == "failed", report
    assert report["kind"] == "contract"
    assert "merge.dropped" in str(report["reason"]), report["reason"]


def test_the_same_side_dropped_with_a_reason_is_accepted(tmp_path: Path) -> None:
    """Названная причина делает тот же выбор законным: отказ не про выбор, а про тишину."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    lane_run(conn, spec, deal, home, "impl_a", "shared.txt", "from a\n")
    lane_run(conn, spec, deal, home, "impl_b", "shared.txt", "from b\n")

    resolver = Resolver(text="from b\n", taken="impl_b", why="сторона a повторяла ту же строку другими словами")
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=resolver, clock=lambda: NOW)
    assert report["outcome"] == "done", report


# --------------------------- 4. неразрешимый конфликт: класс, имена, деревья на месте


def test_an_unresolved_conflict_names_the_files_and_keeps_the_lane_trees(tmp_path: Path) -> None:
    """Не смог — отказ `merge.conflict` с файлами и обоими sha, деревья полос целы.

    Дело не «умирает молча» и не теряет работы сторон: обе ветки и оба дерева
    остаются на диске, а причина называет всё, что нужно для продолжения.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    sha_a = lane_run(conn, spec, deal, home, "impl_a", "shared.txt", "from a\n")
    sha_b = lane_run(conn, spec, deal, home, "impl_b", "shared.txt", "from b\n")

    resolver = Resolver(text=None, commit=False, status="refused")
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=resolver, clock=lambda: NOW)
    assert report["outcome"] == "failed", report
    assert report["kind"] == "contract"
    reason = str(report["reason"])
    assert "merge.conflict" in reason
    assert "shared.txt" in reason
    assert sha_a[:12] in reason and sha_b[:12] in reason, reason

    branches = git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()
    assert runner.lane_branch(deal, "impl_a") in branches, "ветка полосы осталась при отказе"
    assert runner.lane_branch(deal, "impl_b") in branches
    assert tree_of(home, deal, "impl_a").is_dir(), "дерево полосы осталось при отказе"
    assert tree_of(home, deal, "impl_b").is_dir()
    kept = ended_of(conn, deal, "join")["details"]["tree_kept"]
    assert kept == str(tree_of(home, deal, "join")), "состояние конфликта сохранено в дереве этапа"


def test_the_conflict_state_is_reused_by_the_retry(tmp_path: Path) -> None:
    """Повтор продолжает развод файлов в сохранённом дереве, а не начинает заново.

    Дерево конфликта уберегается (`tree_kept`), и следующий резолвер видит те же
    конфликтные файлы: `merge --abort` не делается, состояние merge живёт в дереве.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    lane_run(conn, spec, deal, home, "impl_a", "shared.txt", "from a\n")
    lane_run(conn, spec, deal, home, "impl_b", "shared.txt", "from b\n")
    runner.run_stage(
        conn, spec, deal, "join", 1, home=home,
        spawn=Resolver(text=None, commit=False, status="refused"), clock=lambda: NOW,
    )

    retry = Resolver()
    report = runner.run_stage(conn, spec, deal, "join", 2, home=home, spawn=retry, clock=lambda: NOW)
    assert report["outcome"] == "done", report
    assert retry.contracts[0]["merge"]["conflict_files"] == ["shared.txt"]
    # Что уже влито до столкновения, резолвер видит по ancestry, а не по пустому
    # списку: порядок `impl_b, impl_a`, конфликт на второй — влита первая.
    assert retry.contracts[0]["merge"]["applied"] == ["impl_b"]


# ------------------------------------- 5. порядок слияния детерминирован


def test_the_merge_order_is_the_declared_one_and_repeats(tmp_path: Path) -> None:
    """Тот же план дважды даёт тот же порядок: он берётся из `needs`, а не из часов.

    Иначе один и тот же план давал бы разные merge-коммиты — и «результат повтора
    плана» перестал бы быть воспроизводимым.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    lane_run(conn, spec, deal, home, "impl_a", "a.txt", "from a\n")
    lane_run(conn, spec, deal, home, "impl_b", "b.txt", "from b\n")

    join = next(stage for stage in spec.stages if stage.id == "join")
    first = runner.merge_lanes(spec, join, events(conn, deal))
    second = runner.merge_lanes(spec, join, events(conn, deal))
    assert [lane["lane"] for lane in first] == ["impl_b", "impl_a"]
    assert first == second


# ------------------------------------- 6. гейт пакета: свидетель и стороны


def merge_stage_of(needs: tuple[str, ...] = ("impl_a", "impl_b")) -> Any:
    """Этап слияния для гейта: две полосы, своё дерево, роль по умолчанию."""
    return spec_module.Stage(
        id="join",
        role=spec_module.MERGE_RESOLVER_ROLE,
        kind=spec_module.MERGE_KIND,
        workspace="worktree",
        needs=needs,
    )


def sides_of(*lanes: str, **extra: Any) -> dict[str, Any]:
    """Блок `merge` отчёта: стороны названы, файлы перечислены."""
    block: dict[str, Any] = {
        "files": list(extra.pop("files", ["shared.txt"])),
        "sides": [
            {"lane": lane, "sha": "0" * 40, "intent": "свой файл", "joined": "влит"} for lane in lanes
        ],
    }
    block.update(extra)
    return block


def test_a_single_parent_commit_is_not_a_merge_witness(tmp_path: Path) -> None:
    """Коммит с одним родителем свидетелем слияния не принимается (`merge.parents`).

    Иначе «слито» доказывалось бы обычным коммитом поверх одной из полос, а вторая
    осталась бы за бортом — ровно тот дефект, ради которого версия делалась.
    """
    repo = git_repo(tmp_path)
    (repo / "b.txt").write_text("from b\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "single parent")
    commit = git(repo, "rev-parse", "HEAD")
    result = {
        "status": "done",
        "commit": commit,
        "summary": "слито",
        "evidence": [],
        "merge": sides_of("impl_a", "impl_b"),
    }
    verdict = evidence_module.verify(
        result,
        merge_stage_of(),
        tmp_path,
        attempt_started_at=NOW,
        now=NOW,
        root=repo,
        base="HEAD~1",
    )
    assert not verdict.ok
    assert evidence_module.RULE_MERGE_PARENTS in verdict.reason(), verdict.reason()


def test_a_lane_missing_from_the_report_is_a_dropped_side(tmp_path: Path) -> None:
    """Полоса, о которой отчёт молчит, — та же потеря (`merge.dropped`).

    Отчёт обязан назвать обе стороны: иначе «вошла молча» и «потеряна» неотличимы.
    """
    repo = git_repo(tmp_path)
    commit = git(repo, "rev-parse", "HEAD")
    result = {
        "status": "done",
        "commit": commit,
        "summary": "слито",
        "evidence": [],
        "merge": sides_of("impl_a"),
    }
    verdict = evidence_module.verify(
        result,
        merge_stage_of(),
        tmp_path,
        attempt_started_at=NOW,
        now=NOW,
        root=repo,
        base=commit,
    )
    assert not verdict.ok
    assert evidence_module.RULE_MERGE_DROPPED in verdict.reason(), verdict.reason()
    assert "impl_b" in verdict.reason()


# ------------------------------------- 7. слитое дерево проверяется гейтами


def warnings_of(stages: list[Any]) -> set[str]:
    graph = spec_module.Spec(
        path=Path("<graph>"), duty="graph", repo=None, base=None, stages=tuple(stages)
    )
    return {issue.rule for issue in spec_module.gate_warnings(graph)}


def test_a_merge_without_a_checking_stage_warns(tmp_path: Path) -> None:
    """За слиянием без проверки `spec check` предупреждает: полосы зелены, merge — нет.

    Требование 6 контракта версии: «зелёная полоса + зелёная полоса ≠ зелёный
    merge», поэтому собранное дерево обязан проверить отдельный этап. Забывший
    лид слышит предупреждение сразу, а не узнаёт о пропуске на закрытии дела.
    """
    merge = merge_stage_of()
    after = spec_module.Stage(id="close", role="coordinator", workspace="none", needs=("join",))
    assert "plan.merge.gates" in warnings_of([merge, after])


def test_a_tester_after_the_merge_silences_the_warning(tmp_path: Path) -> None:
    """Проверяющий этап за слиянием снимает предупреждение — ревьюер, тестер, команда.

    Гейт — это проверка собранного дерева, а не второй этап слияния: тестер,
    ревьюер с приговором и `kind: shell` с доказательствами годятся одинаково.
    """
    merge = merge_stage_of()
    gates = spec_module.Stage(id="gates", role="tester", workspace="worktree", needs=("join",))
    assert "plan.merge.gates" not in warnings_of([merge, gates])


# ------------------------- 8. три полосы: конфликт не на последней не теряет хвост


def three_lanes_spec(tmp_path: Path, repo: Path) -> Any:
    """Спека трёх полос: порядок объявления — `impl_a`, `impl_b`, `impl_c`.

    Полосы правят один файл у первых двух и свой у третьей — так конфликт
    случается на средней полосе, а хвост остаётся невлитым, если движок считает
    слияние собранным по одному конфликту.
    """
    return write_spec(
        tmp_path,
        f"""
        duty: lanes-three
        repo: {repo}
        base: HEAD
        stages:
          - id: impl_a
            role: implementer
            workspace: worktree
            parallel_with: [impl_b, impl_c]
          - id: impl_b
            role: implementer
            workspace: worktree
            parallel_with: [impl_a, impl_c]
          - id: impl_c
            role: implementer
            workspace: worktree
            parallel_with: [impl_a, impl_b]
          - id: join
            kind: merge
            needs: [impl_a, impl_b, impl_c]
            workspace: worktree
          - id: gates
            role: tester
            workspace: worktree
            needs: [join]
        """,
    )


def test_a_conflict_on_the_middle_lane_still_merges_the_tail(tmp_path: Path) -> None:
    """Конфликт на средней полосе не теряет хвост: все три коммита в свидетеле.

    Вливание идёт по порядку `needs`, и резолвер собирал только конфликтную пару.
    Полоса, стоящая после конфликтной, оставалась невлитой, а её ветка удалялась
    как «влитая» — работа третьей стороны пропадала (находка ревью на трёх
    полосах). Хвост доводит движок: свидетель — коммит, где предок КАЖДАЯ полоса.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = three_lanes_spec(tmp_path, repo)
    sha_a = lane_run(conn, spec, deal, home, "impl_a", "shared.txt", "from a\n")
    sha_b = lane_run(conn, spec, deal, home, "impl_b", "shared.txt", "from b\n")
    sha_c = lane_run(conn, spec, deal, home, "impl_c", "c.txt", "from c\n")

    resolver = Resolver()
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=resolver, clock=lambda: NOW)
    assert report["outcome"] == "done", json.dumps(report, ensure_ascii=False)
    merge = resolver.contracts[0]["merge"]
    assert merge["order"] == ["impl_a", "impl_b", "impl_c"], "порядок объявления"
    assert merge["conflict_files"] == ["shared.txt"]
    assert merge["applied"] == ["impl_a"], "резолвер видит, что уже влито до столкновения"

    witness = str(report["commit"])
    for sha, lane in ((sha_a, "impl_a"), (sha_b, "impl_b"), (sha_c, "impl_c")):
        assert git(repo, "merge-base", "--is-ancestor", sha, witness) == "", (
            f"работа полосы {lane} не в свидетеле"
        )
    parents = git(repo, "rev-list", "--parents", "-n", "1", witness).split()
    assert len(parents) >= 3, f"свидетель — merge-коммит: {parents}"

    merged_tree = tree_of(home, deal, "join")
    assert (merged_tree / "shared.txt").read_text(encoding="utf-8") == "from a and from b\n"
    assert (merged_tree / "c.txt").read_text(encoding="utf-8") == "from c\n", "хвост влит"

    details = ended_of(conn, deal, "join")["details"]["merge"]
    assert details["order"] == ["impl_a", "impl_b", "impl_c"]
    assert len(details["cleaned"]) == 3 and not details["kept"], details
    branches = git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()
    for lane in ("impl_a", "impl_b", "impl_c"):
        assert runner.lane_branch(deal, lane) not in branches, f"ветка {lane} убрана после слияния"


def test_a_report_that_forgets_the_tail_lane_is_refused(tmp_path: Path) -> None:
    """Отчёт, забывший полосу за конфликтной, — `merge.dropped`, а не зелёный пакет.

    Хвост влит движком, но отчёт обязан назвать КАЖДУЮ полосу: иначе «вошла
    молча» и «потеряна» неотличимы, и обещанию отчёта нельзя верить на слово.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = three_lanes_spec(tmp_path, repo)
    sha_a = lane_run(conn, spec, deal, home, "impl_a", "shared.txt", "from a\n")
    sha_b = lane_run(conn, spec, deal, home, "impl_b", "shared.txt", "from b\n")
    sha_c = lane_run(conn, spec, deal, home, "impl_c", "c.txt", "from c\n")

    resolver = Resolver(
        sides=[
            {"lane": "impl_a", "sha": sha_a, "intent": "свой файл", "joined": "влит"},
            {"lane": "impl_b", "sha": sha_b, "intent": "свой файл", "joined": "влит"},
        ]
    )
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=resolver, clock=lambda: NOW)
    assert report["outcome"] == "failed", report
    assert report["kind"] == "contract"
    assert evidence_module.RULE_MERGE_DROPPED in str(report["reason"]), report["reason"]
    assert "impl_c" in str(report["reason"])
    assert sha_c  # полоса не выдумана тестом: её коммит назван выше

    branches = git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()
    assert runner.lane_branch(deal, "impl_c") in branches, "ветка невлитой полосы сохранена"
    assert tree_of(home, deal, "impl_c").is_dir(), "дерево невлитой полосы сохранено"


def test_the_tail_lane_that_conflicts_too_keeps_every_tree(tmp_path: Path) -> None:
    """Хвост, который тоже конфликтует, — отказ с файлами, все деревья полос целы.

    Резолвер развёл пару, но следующая полоса правит тот же файл: движок называет
    новый конфликт и НЕ убирает ни одной полосы — в каждой лежит работа стороны.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = three_lanes_spec(tmp_path, repo)
    lane_run(conn, spec, deal, home, "impl_a", "shared.txt", "from a\n")
    lane_run(conn, spec, deal, home, "impl_b", "shared.txt", "from b\n")
    lane_run(conn, spec, deal, home, "impl_c", "shared.txt", "from c\n")

    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=Resolver(), clock=lambda: NOW)
    assert report["outcome"] == "failed", report
    assert report["kind"] == "contract"
    assert evidence_module.RULE_MERGE_CONFLICT in str(report["reason"]), report["reason"]
    assert "shared.txt" in str(report["reason"])
    details = ended_of(conn, deal, "join")["details"]["merge"]
    assert not details["cleaned"] and details["kept"] == [], "уборки не было вовсе"
    branches = git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()
    for lane in ("impl_a", "impl_b", "impl_c"):
        assert runner.lane_branch(deal, lane) in branches, f"ветка {lane} сохранена"
        assert tree_of(home, deal, lane).is_dir(), f"дерево {lane} сохранено"


def test_a_lane_outside_the_witness_is_a_dropped_side(tmp_path: Path) -> None:
    """Полоса, чей коммит не предок свидетеля, — `merge.dropped` при двух родителях.

    Число родителей отвечает «это merge-коммит», а не «вошли все полосы»: отчёт
    «все три влиты» + два родителя проходил зелёным, и потерянная сторона
    считалась слитой. Ancestry — единственная проверка, отличающая «вошла» от
    «названа».
    """
    repo = git_repo(tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    lanes: list[str] = []
    for name in ("a", "b", "c"):
        git(repo, "checkout", "-q", "-b", f"lane_{name}", base)
        (repo / f"{name}.txt").write_text(f"from {name}\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", f"lane {name}")
        lanes.append(git(repo, "rev-parse", "HEAD"))
    # Свидетель — слияние двух полос из трёх: третья осталась за бортом.
    git(repo, "checkout", "-q", "lane_a")
    git(repo, "merge", "--no-ff", "--no-edit", "-qm", "join two", lanes[1])
    commit = git(repo, "rev-parse", "HEAD")
    brief = {
        "base": lanes[0],
        "conflict_files": [],
        "applied": [],
        "lanes": [
            {"lane": "impl_a", "sha": lanes[0], "branch": "b", "tree": None, "diff": ""},
            {"lane": "impl_b", "sha": lanes[1], "branch": "b", "tree": None, "diff": ""},
            {"lane": "impl_c", "sha": lanes[2], "branch": "b", "tree": None, "diff": ""},
        ],
    }
    result = {
        "status": "done",
        "commit": commit,
        "summary": "слито",
        "evidence": [],
        "merge": {
            "files": [],
            "sides": [
                {"lane": f"impl_{name}", "sha": sha, "intent": "свой файл", "joined": "влит"}
                for name, sha in zip("abc", lanes)
            ],
        },
    }
    verdict = evidence_module.verify(
        result,
        merge_stage_of(("impl_a", "impl_b", "impl_c")),
        tmp_path,
        attempt_started_at=NOW,
        now=NOW,
        root=repo,
        base=lanes[0],
        merge_brief=brief,
    )
    assert not verdict.ok, verdict.details
    assert evidence_module.RULE_MERGE_DROPPED in verdict.reason(), verdict.reason()
    assert "impl_c" in verdict.reason()
