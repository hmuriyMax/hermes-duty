"""Движок: лизинг, дерево попытки, контракт файлом, спавн, телеметрия.

Ни один тест не поднимает модель: `spawn` подменяется фейком, который пишет
заданный поток и `result.json`. Проверяется то, что движок **записывает**:
события журнала, строку лизинга, файл контракта и фактическую команду спавна.
Живой прогон одного тривиального этапа — отдельный тест с `@pytest.mark.live`
(по умолчанию пропущен, включается `pytest -m live`).

Схемы `pool.db` и репозитория — свои маленькие копии по реальной форме: тест
движка не должен трогать ни живые БД, ни живые профили.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Метка времени всех фактов теста: одинаковый `ts` ещё и проверяет порядок
# применения событий внутри одной секунды (`KIND_ORDER`).
NOW = 1_790_000_000

# Схема пула imessage-gateway — та, в которую пишет `ask()`.
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
    """Модуль плагина по пути — под тем же именем, что и у остального кода."""
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


def make_deal(conn: sqlite3.Connection, *, title: str = "Починить шапку", body: str = "") -> dict[str, Any]:
    """Своё дело в store: id, доска, статус — то, что движок видит на входе."""
    created = deals.create_deal(
        conn, board="duty", title=title, body=body, known_boards=["duty"], now=NOW
    )
    return deals.deal_row(conn, str(created["deal"]["id"]))


def write_spec(tmp_path: Path, text: str) -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def git_repo(tmp_path: Path) -> Path:
    """Настоящий репозиторий с одним коммитом: worktree проверяется живым git."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        check=True,
    )
    return repo


def none_spec(tmp_path: Path, *, role: str = "implementer") -> Any:
    """Минимальная спека без дерева: этап-гейт, которому нужен только приговор."""
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: impl
            role: {role}
            workspace: none
        """,
    )


def worktree_spec(tmp_path: Path, repo: Path, *, extra: str = "") -> Any:
    """Спека с деревом попытки от реального репозитория."""
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        repo: {repo}
        base: HEAD
        stages:
          - id: impl
            role: implementer
            workspace: worktree
        {textwrap.indent(textwrap.dedent(extra), "    ") if extra else ""}
        """,
    )


class Fake:
    """Фейковый спавн: пишет заданный поток и `result.json`, помнит команды.

    Адрес отчёта берётся из контракта — того самого файла, что движок передал
    ключом `--query-file`: так тест проверяет контракт на диске до спавна, а не
    только в памяти.

    `pack=True` — исполнитель следует контракту: кладёт в рабочий каталог файл
    `notes.txt` и дописывает к отчёту пакет (артефакт с настоящим `sha256` и
    записи evidence по объявленным командам с кодом `must`). Без пакета отчёт
    проходит только у этапов, где заявлять нечего.
    """

    def __init__(
        self,
        *,
        lines: Optional[list[dict[str, Any]]] = None,
        result: Optional[dict[str, Any]] = None,
        exit_code: int = 0,
        hang: bool = False,
        pid: int = 4242,
        broken_result: bool = False,
        pack: bool = False,
    ) -> None:
        self.lines = [json.dumps(line, ensure_ascii=False) + "\n" for line in (lines or [])]
        self.result = result
        self.exit_code = exit_code
        self.hang = hang
        self.pid = pid
        self.broken_result = broken_result
        self.pack = pack
        self.calls: list[dict[str, Any]] = []
        self.stopped = 0
        self.exit = None if hang else exit_code
        self.stderr_text = "fake stderr tail"

    # --- то, что нужно `runner.follow`
    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "Fake":
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        self._report(Path(cmd[list(cmd).index("--query-file") + 1]), Path(cwd))
        return self

    def _report(self, query_file: Path, cwd: Path) -> None:
        target = query_file.parent / "result.json"
        try:
            contract = json.loads(query_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            contract = {}
        if isinstance(contract, dict) and contract.get("report_to"):
            target = Path(str(contract["report_to"]))
        if self.broken_result:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{это не json", encoding="utf-8")
            return
        if self.result is None:
            return
        report = self._packed(contract, cwd) if self.pack else self.result
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    def _packed(self, contract: Any, cwd: Path) -> dict[str, Any]:
        """Отчёт вместе с пакетом: артефакт в дереве и записи объявленных команд.

        Хеш считается по настоящему файлу — тест проверяет гейт на тех же
        правилах, что и живая попытка, а не на подставленном «совпало».
        """
        report = dict(self.result or {})
        notes = cwd / "notes.txt"
        notes.write_text("приговор: changes\n", encoding="utf-8")
        digest = hashlib.sha256(notes.read_bytes()).hexdigest()
        report.setdefault("artifacts", [{"path": notes.name, "sha256": digest}])
        declared = contract.get("evidence_to_produce") if isinstance(contract, dict) else None
        report.setdefault(
            "evidence",
            [
                {"cmd": str(item.get("cmd")), "exit": int(item.get("must", 0)), "started_at": NOW}
                for item in (declared or [])
                if isinstance(item, dict)
            ],
        )
        report.setdefault("summary", "готово")
        return report

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        if self.lines:
            return self.lines.pop(0)
        return None

    def finished(self) -> bool:
        if self.hang and not self.stopped:
            return False
        return not self.lines

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.exit

    def stop(self, grace: float = 10) -> Optional[int]:
        self.stopped += 1
        if self.exit is None:
            self.exit = -15
        return self.exit


def run(conn, spec, deal, home, fake, **kwargs):
    """Один шаг движка с подменённым спавном и неподвижными часами."""
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("spawn", fake)
    return runner.run_stage(conn, spec, deal, "impl", 1, home=home, **kwargs)


def events(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    return journal.read_events(conn, str(deal["id"]))


def kinds(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[str]:
    return [str(event["kind"]) for event in events(conn, deal)]


def only(conn: sqlite3.Connection, deal: dict[str, Any], kind: str) -> dict[str, Any]:
    found = [event for event in events(conn, deal) if str(event["kind"]) == kind]
    assert len(found) == 1, f"событий {kind}: {len(found)}"
    return found[0]


def seed(conn, deal, kind: str, payload: dict[str, Any], *, key: str, ts: int = NOW) -> None:
    """Факт в журнал руками: предыстория, которую движок должен прочитать."""
    journal.append(
        conn,
        [
            {
                "ts": ts,
                "recorded_at": ts,
                "deal_id": str(deal["id"]),
                "board": str(deal["board"]),
                "kind": kind,
                "payload": payload,
                "source": "test",
                "idempotency_key": key,
            }
        ],
    )


# -------------------------------------------------------------------- лизинг


def test_lease_taken_only_once(tmp_path: Path) -> None:
    """Два процесса не поведут один этап: второй берёт лизинг и получает `False`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    assert runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=101, ttl=1800, now=NOW) is True
    assert runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=202, ttl=1800, now=NOW) is False
    held = runner.lease_of(conn, deal, "impl")
    assert held is not None and held["pid"] == 101
    # Чужой этап — это другой лизинг, а не тот же самый.
    assert runner.lease(conn, deal=deal, stage="review", attempt=1, pid=202, ttl=1800, now=NOW) is True


def test_lease_expires_and_can_be_taken_again(tmp_path: Path) -> None:
    """Протухший лизинг забирает следующий: срок — данные, а не догадка."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    assert runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=101, ttl=60, now=NOW) is True
    assert runner.lease(conn, deal=deal, stage="impl", attempt=2, pid=202, ttl=60, now=NOW + 61) is True
    held = runner.lease_of(conn, deal, "impl")
    assert held is not None and (held["pid"], held["attempt"]) == (202, 2)


def test_lease_renewed_only_by_owner(tmp_path: Path) -> None:
    """Продление — по heartbeat'у владельца; чужой продлить не может."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=101, ttl=60, now=NOW)
    assert runner.renew(conn, deal=deal, stage="impl", pid=202, ttl=60, now=NOW + 10) is False
    assert runner.renew(conn, deal=deal, stage="impl", pid=101, ttl=60, now=NOW + 10) is True
    held = runner.lease_of(conn, deal, "impl")
    assert held is not None and held["expires_at"] == NOW + 70


def test_hand_over_moves_lease_to_attempt_pid(tmp_path: Path) -> None:
    """Лизинг движка становится лизингом попытки: pid переезжает, срок новый."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=101, ttl=60, now=NOW)
    moved = runner.hand_over(
        conn, deal=deal, stage="impl", from_pid=101, pid=909, attempt=1, ttl=1800, now=NOW
    )
    assert moved is True
    held = runner.lease_of(conn, deal, "impl")
    assert held is not None and (held["pid"], held["expires_at"]) == (909, NOW + 1800)
    assert runner.renew(conn, deal=deal, stage="impl", pid=101, ttl=60, now=NOW) is False


def test_release_frees_the_stage(tmp_path: Path) -> None:
    """Отпущенный лизинг свободен сразу, а не после истечения срока."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=101, ttl=1800, now=NOW)
    assert runner.release(conn, deal=deal, stage="impl", pid=101) is True
    assert runner.lease_of(conn, deal, "impl") is None
    assert runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=202, ttl=1800, now=NOW) is True


def test_lease_ttl_comes_from_budget(tmp_path: Path) -> None:
    """TTL лизинга — `budget.minutes` этапа, по умолчанию 30 минут."""
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            budget: {minutes: 5}
          - id: review
            role: reviewer
            needs: [impl]
        """,
    )
    assert runner.ttl_sec(spec.stage("impl")) == 300
    assert runner.ttl_sec(spec.stage("review")) == runner.DEFAULT_TTL_MIN * 60


# --------------------------------------------------- контракт и предыстория


def test_contract_file_holds_the_attempt_input(tmp_path: Path) -> None:
    """Контракт пишется файлом до спавна: дело, роль, каталог, отчёт, evidence."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, title="Починить шапку", body="Нужен фикс в шапке")
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {cmd: "go build ./...", must: 0}
            ask: {allowed: [researcher], to_human: decisions_only, max_hops: 2}
        """,
    )
    fake = Fake(result={"status": "done", "summary": "готово"})
    run(conn, spec, deal, home, fake)

    workdir = runner.attempt_dir(home, deal, "impl", 1)
    assert workdir.parent == home / "kanban" / "boards" / "duty" / "workspaces"
    contract_file = runner.contract_path(workdir, "impl", 1)
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    assert contract["deal"] == deal["id"]
    assert contract["stage"] == "impl"
    assert contract["attempt"] == 1
    assert contract["role"] == "implementer"
    assert contract["workdir"] == str(workdir)
    assert contract["goal"] == "Починить шапку\n\nНужен фикс в шапке"
    assert contract["report_to"] == str(workdir / ".duty" / "result.json")
    assert contract["evidence_to_produce"] == [{"cmd": "go build ./...", "must": 0}]
    assert contract["questions"] == {
        "allowed": ["researcher"],
        "to_human": "decisions_only",
        "max_hops": 2,
    }
    assert contract["boundaries"], "границы попытки обязаны быть названы"
    assert "answer_this_attempt" not in contract
    # cwd процесса — каталог попытки: дерева у `workspace: none` нет.
    assert fake.calls[0]["cwd"] == workdir


def test_contract_carries_previous_artifacts(tmp_path: Path) -> None:
    """Отчёты предшественников — из журнала: артефакты, резюме, вердикт."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = write_spec(
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
        """,
    )
    seed(
        conn,
        deal,
        "attempt_ended",
        {
            "stage": "impl",
            "attempt": 1,
            "outcome": "done",
            "result": {"summary": "поправил шапку", "artifacts": ["/tmp/diff.patch"]},
        },
        key="test:impl:ended",
    )
    seed(
        conn,
        deal,
        "attempt_ended",
        {"stage": "verify", "attempt": 1, "outcome": "done", "result": {"summary": "тесты зелёные"}},
        key="test:verify:ended",
    )
    seed(conn, deal, "changes_requested", {"stage": "verify", "reason": "нужен тест"}, key="test:verify:changes")

    previous = runner.previous_reports(conn, deal, spec, "review")
    assert previous == [
        {"stage": "impl", "artifacts": ["/tmp/diff.patch"], "summary": "поправил шапку", "verdict": None},
        {"stage": "verify", "artifacts": [], "summary": "тесты зелёные", "verdict": "changes"},
    ]


def test_resume_attempt_answers_with_session(tmp_path: Path) -> None:
    """После ответа человека попытка продолжается резюмом (`RESUME-OK`)."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    seed(
        conn,
        deal,
        "attempt_started",
        {"stage": "impl", "attempt": 1, "run_id": "impl#1", "pid": 1},
        key="test:impl:started",
    )
    seed(
        conn,
        deal,
        "attempt_ended",
        {
            "stage": "impl",
            "attempt": 1,
            "outcome": "done",
            "details": {"session_id": "local-abc123"},
        },
        key="test:impl:ended",
    )
    fake = Fake(result={"status": "done", "summary": "сделал по ответу"})
    report = runner.run_stage(
        conn, spec, deal, "impl", 2, home=home, answer="делай по варианту Б", spawn=fake, clock=lambda: NOW
    )

    contract = json.loads(
        runner.contract_path(runner.attempt_dir(home, deal, "impl", 2), "impl", 2).read_text(encoding="utf-8")
    )
    assert contract["answer_this_attempt"] == "делай по варианту Б"
    assert contract["session_id"] == "local-abc123"
    command = fake.calls[0]["cmd"]
    assert command[:4] == [runner.HERMES_BIN, "chat", "-p", "implementer"]
    assert "--resume" in command and "local-abc123" in command
    query_file = Path(command[command.index("--query-file") + 1])
    assert query_file.name == "answer-impl-2.txt"
    assert "делай по варианту Б" in query_file.read_text(encoding="utf-8")
    assert report["session_id"] == "local-abc123"


# --------------------------------------------------------------------- дерево


def test_worktree_created_and_kept_on_success(tmp_path: Path) -> None:
    """Успешная попытка оставляет дерево: разбор ошибки идёт по нему же."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = worktree_spec(tmp_path, repo)
    fake = Fake(result={"status": "done", "summary": "готово"}, pack=True)
    report = run(conn, spec, deal, home, fake)

    tree = runner.attempt_dir(home, deal, "impl", 1) / "tree"
    assert tree.is_dir() and (tree / "a.txt").is_file()
    assert fake.calls[0]["cwd"] == tree
    assert str(tree) in subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
    ).stdout
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["outcome"] == "done"
    assert ended["payload"]["details"]["tree"] == str(tree)
    assert ended["payload"]["details"]["cleaned"] is False
    assert report["tree"] == str(tree)


def test_worktree_removed_when_attempt_failed(tmp_path: Path) -> None:
    """Упавшая попытка дерево удаляет, а путь и причину называет в событии."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = worktree_spec(tmp_path, repo)
    fake = Fake(exit_code=2)
    run(conn, spec, deal, home, fake)

    tree = runner.attempt_dir(home, deal, "impl", 1) / "tree"
    assert not tree.exists()
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["outcome"] == "failed"
    assert ended["payload"]["kind"] == "infra"
    assert ended["payload"]["details"]["tree"] == str(tree)
    assert ended["payload"]["details"]["cleaned"] is True
    assert str(tree) not in subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
    ).stdout


def test_workspace_dir_works_in_deal_catalog(tmp_path: Path) -> None:
    """`workspace: dir` — каталог дела из спеки, без worktree."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = write_spec(
        tmp_path,
        f"""
        duty: probe
        repo: {repo}
        stages:
          - id: impl
            role: implementer
            workspace: dir
        """,
    )
    fake = Fake(result={"status": "done"})
    report = run(conn, spec, deal, home, fake)
    assert fake.calls[0]["cwd"] == repo
    assert report["tree"] is None
    assert not (repo / "tree").exists()
    contract = json.loads(
        runner.contract_path(runner.attempt_dir(home, deal, "impl", 1), "impl", 1).read_text(encoding="utf-8")
    )
    assert contract["tree"] is None
    assert contract["boundaries"][0].startswith(f"рабочий каталог попытки: {repo}")


def test_workspace_none_has_no_tree(tmp_path: Path) -> None:
    """`workspace: none` — дерева нет вовсе: этап-гейт только выносит приговор."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(result={"status": "done"})
    report = run(conn, spec, deal, home, fake)
    assert report["tree"] is None
    assert Path(report["workdir"]).is_dir()
    assert "дерева нет" in json.loads(
        runner.contract_path(Path(report["workdir"]), "impl", 1).read_text(encoding="utf-8")
    )["boundaries"][0]


# ---------------------------------------------------------- смерть попытки


def test_timeout_is_infra_and_process_is_stopped(tmp_path: Path) -> None:
    """Таймаут — наш факт: SIGTERM, затем SIGKILL через 10 с, класс `infra`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            budget: {minutes: 1}
        """,
    )
    fake = Fake(hang=True)
    ticks = iter(range(10_000_000))
    report = run(conn, spec, deal, home, fake, monotonic=lambda: float(next(ticks)) * 10)

    assert report["outcome"] == "failed" and report["kind"] == "infra"
    assert fake.stopped == 1
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["details"]["timed_out"] is True
    assert "таймаут" in ended["payload"]["reason"]
    assert runner.lease_of(conn, deal, "impl") is None


def test_missing_result_with_zero_exit_is_contract(tmp_path: Path) -> None:
    """Процесс вышел с нулём и не отчитался — это `contract`, а не успех."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(exit_code=0)
    report = run(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("failed", "contract")
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["details"]["cleanup"] is None
    assert "result.json нет" in ended["payload"]["reason"]


def test_exit_code_without_result_is_infra(tmp_path: Path) -> None:
    """Код выхода ≠ 0 без отчёта — упал сам процесс: класс `infra`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(exit_code=7)
    report = run(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("failed", "infra")
    assert "код выхода 7" in report["reason"]


def test_result_finished_is_done_with_payload(tmp_path: Path) -> None:
    """Принятый пакет — конец попытки: `done` плюс `payload.result` и вердикт гейта."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(result={"status": "done", "summary": "приговор: changes"}, pack=True)
    report = run(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("done", None)
    assert report["verdict"]["ok"] is True
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["result"]["status"] == "done"
    assert ended["payload"]["result"]["artifacts"][0]["path"] == "notes.txt"
    assert ended["payload"]["details"]["exit_code"] == 0
    assert ended["payload"]["details"]["verdict"]["ok"] is True
    assert report["question_id"] is None


def test_gate_refuses_bare_done_and_puts_the_issue_into_the_reason(tmp_path: Path) -> None:
    """«Готово» без пакета этап не закрывает: `failed(contract)` с `file:line` в причине."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            evidence:
              - {cmd: "go test ./...", must: 0}
        """,
    )
    report = run(conn, spec, deal, home, Fake(result={"status": "done", "summary": "готово"}))

    assert (report["outcome"], report["kind"]) == ("failed", "contract")
    assert report["verdict"]["ok"] is False
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["outcome"] == "failed"
    assert ended["payload"]["kind"] == "contract"
    assert ended["payload"]["reason"] == report["reason"]
    assert "result.json:" in report["reason"], "отказ обязан называть точку в отчёте"
    assert ended["payload"]["details"]["verdict"]["details"]
    # События «попытка закрыта done» не появилось: гейт стоит до записи факта.
    assert ended["payload"]["outcome"] != "done"


def test_gate_budget_overrun_in_the_report_is_capability(tmp_path: Path) -> None:
    """Пакет сходится, но отчёт сам говорит о перерасходе — `failed(capability)`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            budget: {tokens: 1000}
        """,
    )
    fake = Fake(result={"status": "done", "usage": {"total_tokens": 5000}}, pack=True)
    report = run(conn, spec, deal, home, fake)

    assert (report["outcome"], report["kind"]) == ("failed", "capability")
    assert report["verdict"]["kind"] == "capability"
    ended = only(conn, deal, "attempt_ended")
    assert "budget.tokens" in ended["payload"]["reason"]


def test_gate_refusal_removes_the_failed_worktree(tmp_path: Path) -> None:
    """Отказ гейта — упавшая попытка: дерево убирается, путь и причина в событии."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = worktree_spec(tmp_path, repo)
    report = run(conn, spec, deal, home, Fake(result={"status": "done", "summary": "готово"}))

    tree = runner.attempt_dir(home, deal, "impl", 1) / "tree"
    assert (report["outcome"], report["kind"]) == ("failed", "contract")
    assert not tree.exists()
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["details"]["cleaned"] is True


def test_broken_result_is_contract(tmp_path: Path) -> None:
    """`result.json` есть, но не разбирается — исполнитель не отчитался."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(broken_result=True, exit_code=0)
    report = run(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("failed", "contract")
    assert "не разбирается" in report["reason"]


def test_blocked_result_parks_the_deal_with_question(tmp_path: Path) -> None:
    """`status: blocked` — вопрос через `ask()`, дело в `wait_human`, не отказ."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(
        result={
            "status": "blocked",
            "summary": "нужно решение по схеме",
            "question": {"text": "Какую схему берём?", "options": ["A", "B"]},
        }
    )
    report = run(conn, spec, deal, home, fake)

    assert (report["outcome"], report["kind"]) == ("done", None)
    assert report["question_id"] is not None
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    pool.row_factory = sqlite3.Row
    row = pool.execute("SELECT * FROM questions").fetchone()
    assert row["text"] == "Какую схему берём?"
    assert row["origin"] == "duty"
    assert json.loads(row["options_json"]) == ["A", "B"]
    assert deals.deal_row(conn, deal["id"])["status"] == "blocked"
    folded = journal.fold(events(conn, deal), now=NOW)
    assert (folded["state"], folded["human_kind"]) == ("wait_human", "question")
    assert "question_asked" in kinds(conn, deal)


def test_stderr_goes_to_file_not_journal(tmp_path: Path) -> None:
    """Stderr попытки — файлом рядом с отчётом: журнал не заменяет лог."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(result={"status": "done"})
    run(conn, spec, deal, home, fake)
    ended = only(conn, deal, "attempt_ended")
    stderr_file = Path(str(ended["payload"]["details"]["stderr_path"]))
    assert stderr_file.name == "stderr.log"
    assert stderr_file.read_text(encoding="utf-8").strip() == "fake stderr tail"
    assert "fake stderr tail" not in json.dumps(ended["payload"], ensure_ascii=False)


def test_refused_result_is_contract(tmp_path: Path) -> None:
    """`status: refused` — отказ исполнителя, класс `contract`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(result={"status": "refused", "summary": "нет доступа к репозиторию"})
    report = run(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("failed", "contract")
    assert report["question_id"] is None


def test_token_budget_exceeded_is_capability(tmp_path: Path) -> None:
    """Превышение бюджета токенов останавливает попытку: класс `capability`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            budget: {tokens: 1000}
        """,
    )
    fake = Fake(
        lines=[
            {"type": "system", "subtype": "init", "session_id": ""},
            {"type": "result", "session_id": "local-x", "tokens": {"total": 5000}},
        ],
        result={"status": "done"},
    )
    report = run(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("failed", "capability")
    assert fake.stopped == 1
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["details"]["over_budget"] is True
    assert ended["payload"]["details"]["usage_tokens"] == 5000


# ------------------------------------------------------------------ телеметрия


def test_tool_events_become_progress_events(tmp_path: Path) -> None:
    """Каждый `tool_use`/`tool_result` — событие: видно, что делается сейчас."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(
        lines=[
            {"type": "system", "subtype": "init", "session_id": ""},
            {"type": "tool_use", "name": "read_file", "tool_call_id": "1"},
            {"type": "tool_result", "name": "read_file", "tool_call_id": "1", "duration_ms": 12},
            {"type": "tool_use", "name": "terminal", "tool_call_id": "2"},
            {"type": "tool_result", "name": "terminal", "tool_call_id": "2", "duration_ms": 340},
            {"type": "result", "session_id": "local-777", "tokens": {"total": 100}},
        ],
        result={"status": "done"},
    )
    run(conn, spec, deal, home, fake)

    progress = [event for event in events(conn, deal) if event["kind"] == "attempt_progress"]
    assert len(progress) == 4
    assert [event["payload"]["phase"] for event in progress] == [
        "tool_use",
        "tool_result",
        "tool_use",
        "tool_result",
    ]
    assert [event["payload"]["seq"] for event in progress] == [1, 2, 3, 4]
    assert progress[1]["payload"]["duration_ms"] == 12
    assert "duration_ms" not in progress[0]["payload"], "у вызова длительности нет — её не выдумываем"

    folded = journal.fold(events(conn, deal), now=NOW)
    assert folded["progress"] == {"tools": 2, "last_tool": "terminal", "last_at": NOW}
    assert folded["reason"] == "no_live_fact" and folded["state"] is None


def test_progress_does_not_change_state_after_end(tmp_path: Path) -> None:
    """Поздний прогресс закрытое дело не воскрешает и состояние не меняет."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    seed(conn, deal, "created", {"title": "дело"}, key="test:created")
    seed(conn, deal, "completed", {"status": "done"}, key="test:completed")
    seed(
        conn,
        deal,
        "attempt_progress",
        {"stage": "impl", "attempt": 1, "phase": "tool_use", "name": "terminal"},
        key="test:progress",
        ts=NOW + 10,
    )
    folded = journal.fold(events(conn, deal), now=NOW + 10)
    assert folded["active"] is False and folded["state"] is None
    assert folded["progress"]["tools"] == 1


def test_progress_with_garbage_payload_does_not_crash_fold(tmp_path: Path) -> None:
    """Мусорный `payload` телеметрии срез не роняет: фаза просто не читается."""
    folded = journal.fold(
        [
            {"id": 1, "ts": NOW, "kind": "created", "deal_id": "d_1", "payload": {"title": "дело"}},
            {"id": 2, "ts": NOW, "kind": "attempt_progress", "deal_id": "d_1", "payload": "не JSON"},
            {"id": 3, "ts": NOW, "kind": "attempt_progress", "deal_id": "d_1", "payload": {"phase": "???"}},
        ],
        now=NOW,
    )
    assert folded["progress"] is None
    assert folded["reason"] == "no_live_fact"


def test_closed_kind_set_still_rejects_unknown(tmp_path: Path) -> None:
    """Набор расширен одним видом: `attempt_progress` пишется, `tool_used` — нет."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    event = {
        "ts": NOW,
        "recorded_at": NOW,
        "deal_id": str(deal["id"]),
        "board": str(deal["board"]),
        "kind": "attempt_progress",
        "payload": {"phase": "tool_use", "name": "read_file"},
        "source": "test",
        "idempotency_key": "test:progress:1",
    }
    assert journal.append(conn, [event]) == 1
    with pytest.raises(ValueError):
        journal.append(conn, [{**event, "kind": "tool_used", "idempotency_key": "test:tool_used"}])
    assert "attempt_progress" in journal.EVENT_KINDS
    assert "attempt_progress" in journal.NON_STATE_KINDS


# ------------------------------------------------------------------ атомарность


def test_store_failure_rolls_back_the_start_event(tmp_path: Path) -> None:
    """Сбой store откатывает всё: события нет, лизинга нет, процесс остановлен."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    conn.execute(
        "CREATE TRIGGER no_lease_move BEFORE UPDATE ON leases "
        "BEGIN SELECT RAISE(ABORT, 'store упал'); END"
    )
    conn.commit()
    fake = Fake(result={"status": "done"})
    with pytest.raises(sqlite3.Error):
        run(conn, spec, deal, home, fake)
    assert "attempt_started" not in kinds(conn, deal)
    assert runner.lease_of(conn, deal, "impl") is None
    assert fake.stopped == 1
    assert deals.deal_row(conn, deal["id"])["status"] == "ready"


# ------------------------------------------------------- лизинг и решение


def test_busy_stage_is_not_started_again(tmp_path: Path) -> None:
    """Занятый этап не спавнит второй процесс: лизинг говорит `False` первым."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=999, ttl=1800, now=NOW)
    fake = Fake(result={"status": "done"})
    report = run(conn, spec, deal, home, fake)
    assert report["started"] is False
    assert "pid=999" in report["reason"]
    assert fake.calls == []
    assert [kind for kind in kinds(conn, deal) if kind != "created"] == []


def test_started_event_records_the_real_command(tmp_path: Path) -> None:
    """Фактическая команда спавна — в событии, а не в отчёте исполнителя."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(result={"status": "done"})
    run(conn, spec, deal, home, fake)
    started = only(conn, deal, "attempt_started")
    command = started["payload"]["cmd"]
    assert command == fake.calls[0]["cmd"]
    assert command[1] == "chat" and command[2] == "-p" and command[3] == "implementer"
    assert "--query-file" in command
    assert command[command.index("--format") + 1] == "stream-json"
    assert "--oneshot" in command
    assert Path(command[command.index("--query-file") + 1]).name == "in-impl-1.json"
    assert started["payload"]["contract"]["stage"] == "impl"
    assert started["payload"]["lease_expires"] == NOW + runner.DEFAULT_TTL_MIN * 60
    assert deals.deal_row(conn, deal["id"])["status"] == "running"


def test_step_runs_only_the_run_decision(tmp_path: Path) -> None:
    """`step` исполняет `run`; на `wait`/`park` он ничего не запускает."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(result={"status": "done"})

    waited = runner.step(
        conn, spec, deal, _decision("wait", what="attempt"), home=home, spawn=fake, clock=lambda: NOW
    )
    assert waited["started"] == 0 and fake.calls == []

    ran = runner.step(
        conn,
        spec,
        deal,
        _decision("run", stages=("impl",), attempts=(1,)),
        home=home,
        spawn=fake,
        clock=lambda: NOW,
    )
    assert ran["started"] == 1
    assert len(fake.calls) == 1
    assert ran["runs"][0]["stage"] == "impl"


def test_step_and_transit_agree_on_what_happened(tmp_path: Path) -> None:
    """Слои сходятся: события шага дают `transit` тот же вывод о деле.

    Движок пишет факты, решение читает их (`dashboard/transit.py`): успешная
    единственная попытка — дело закрыто, отчёт со `status: blocked` — ждём ответа
    человека, а не «этап пройден». Это стык двух слоёв, и он проверяется по
    журналу, а не по возвращаемому словарю шага.
    """
    transit = load_module("hermes_duty_transit", "transit.py")
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = none_spec(tmp_path)

    done_deal = make_deal(conn, title="пройдёт")
    run(conn, spec, done_deal, home, Fake(result={"status": "done"}, pack=True))
    assert transit.next_stage(spec, events(conn, done_deal), NOW).kind == "done"

    parked_deal = make_deal(conn, title="спросит")
    run(
        conn,
        spec,
        parked_deal,
        home,
        Fake(result={"status": "blocked", "question": {"text": "Какую схему берём?"}}),
    )
    decision = transit.next_stage(spec, events(conn, parked_deal), NOW)
    assert (decision.kind, decision.what) == ("wait", "question")


def _decision(kind: str, **fields: Any) -> Any:
    """Решение `transit` без его модуля: движку нужны `kind` и `pair`."""
    stages = tuple(fields.get("stages") or ())
    attempts = tuple(fields.get("attempts") or ())
    return type(
        "Decision",
        (),
        {
            "kind": kind,
            "stages": stages,
            "attempts": attempts,
            "pair": list(zip(stages, attempts)),
            "what": fields.get("what", ""),
        },
    )()


# ---------------------------------------------------------------- живой прогон


@pytest.mark.live
def test_live_trivial_stage(tmp_path: Path) -> None:
    """Живой прогон одного тривиального этапа (`workspace: none`) через гейт.

    Включается руками: `pytest -m live`. Поднимает настоящий `hermes chat` под
    профилем `tester`, поэтому в обычном прогоне пропущен: он проверяет связку
    «движок → процесс → поток → событие → гейт», а не логику шага.

    Исход зависит от исполнителя, и это честно: если модель предъявила пакет —
    `done`; если отписалась словом «ок» без артефактов и evidence — гейт P3 её не
    принимает, и попытка закрывается `failed(contract)` с отказом в причине.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, title="Скажи ок", body="Ответь одним словом: ок")
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: tester
            workspace: none
            budget: {minutes: 2}
        """,
    )
    started = time.monotonic()
    report = runner.run_stage(conn, spec, deal, "impl", 1, home=home)
    assert report["outcome"] in (runner.OUTCOME_DONE, runner.OUTCOME_FAILED), report
    assert report["session_id"].startswith("local-")
    assert time.monotonic() - started < 120
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["result"]["status"] in runner.OK_STATUSES
    assert ended["payload"]["details"]["usage_tokens"]
    if report["outcome"] == runner.OUTCOME_FAILED:
        assert report["kind"] == runner.FAIL_CONTRACT
        assert report["verdict"] and report["verdict"]["ok"] is False

