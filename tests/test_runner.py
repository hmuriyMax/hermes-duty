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
import os
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
transit = load_module("hermes_duty_transit", "transit.py")


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
        conn, repo="duty", title=title, body=body, now=NOW
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
        files: Optional[dict[str, str]] = None,
    ) -> None:
        self.lines = [json.dumps(line, ensure_ascii=False) + "\n" for line in (lines or [])]
        self.result = result
        self.exit_code = exit_code
        self.hang = hang
        self.pid = pid
        self.broken_result = broken_result
        self.pack = pack
        # Файлы, которые «исполнитель» создаёт в дереве попытки: тест проверяет,
        # что работу этапа видит следующий этап (регрессия W14).
        self.files = dict(files or {})
        self.calls: list[dict[str, Any]] = []
        self.stopped = 0
        self.exit = None if hang else exit_code
        self.stderr_text = "fake stderr tail"

    # --- то, что нужно `runner.follow`
    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "Fake":
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        for name, text in self.files.items():
            target = Path(cwd) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
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
        правилах, что и живая попытка, а не на подставленном «совпало». Заданные
        `files` становятся артефактами отчёта; без них кладётся `notes.txt`.
        """
        report = dict(self.result or {})
        if self.files:
            report.setdefault(
                "artifacts",
                [
                    {
                        "path": name,
                        "sha256": hashlib.sha256((Path(cwd) / name).read_bytes()).hexdigest(),
                    }
                    for name in self.files
                ],
            )
        else:
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


class Answering(Fake):
    """Спавн роли-адресата: на контракт ответа (`answer_report`) отвечает `answer`.

    Вопрос этапа, адресованный роли, — отдельный процесс (`runner.ask_role`), и
    его отчёт формата попытки не совпадает: у него `question`/`asked` и нет
    evidence. Один фейк на два спавна отвечает по контракту, а не по порядку
    вызовов: попытка этапа получает `result`, роль — `answer`.
    """

    def __init__(self, *, answer: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.answer = {"answer": {"text": "в db/migrations"}} if answer is None else answer

    def _report(self, query_file: Path, cwd: Path) -> None:
        try:
            contract = json.loads(query_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            contract = {}
        if not isinstance(contract, dict) or not contract.get("answer_report"):
            super()._report(query_file, cwd)
            return
        target = Path(str(contract.get("report_to") or (query_file.parent / "result.json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.answer, ensure_ascii=False), encoding="utf-8")


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
        "risky": False,
        "default": "",
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
        {
            "stage": "impl",
            "commit": "",
            "artifacts": ["/tmp/diff.patch"],
            "summary": "поправил шапку",
            "verdict": None,
        },
        {
            "stage": "verify",
            "commit": "",
            "artifacts": [],
            "summary": "тесты зелёные",
            "verdict": "changes",
        },
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


def test_deal_branch_holds_the_commit(tmp_path: Path) -> None:
    """Коммит попытки закреплён веткой дела, а не висит в отвязанном HEAD.

    Дело `d_9ea4d348` показало цену: две попытки отчитались `done`, коммиты
    существовали, но ни одна ветка на них не указывала — работу (1576 строк)
    пришлось закреплять руками, иначе первый `git gc` забрал бы её.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = worktree_spec(tmp_path, repo)
    run(conn, spec, deal, home, Fake(result={"status": "done", "summary": "готово"}, pack=True))

    ended = only(conn, deal, "attempt_ended")
    commit = ended["payload"]["commit"]
    details = ended["payload"]["details"]
    assert details["branch"] == f"duty/{deal['id']}"
    assert details["branch_error"] is None

    # Ссылка настоящая: ветка существует и содержит коммит.
    listed = subprocess.run(
        ["git", "-C", str(repo), "branch", "--contains", commit], capture_output=True, text=True
    ).stdout
    assert f"duty/{deal['id']}" in listed

    # Дерево попытки при этом ветку не занимает: оно в detached HEAD, поэтому
    # следующая попытка того же дела не спорит с ним за имя.
    tree = runner.attempt_dir(home, deal, "impl", 1) / "tree"
    head = subprocess.run(
        ["git", "-C", str(tree), "symbolic-ref", "--quiet", "--short", "HEAD"], capture_output=True, text=True
    )
    assert head.returncode != 0, f"дерево заняло ветку: {head.stdout.strip()}"


def test_branch_anchor_failure_is_a_fact_not_a_verdict(tmp_path: Path) -> None:
    """Отказ `git branch -f` не отменяет коммит: работа есть, причина — в событии."""
    deal = {"id": "d_probe"}
    broken = tmp_path / "не-репозиторий"
    broken.mkdir()
    branch, reason = runner.anchor_commit(broken, deal, "0" * 40)
    assert branch == "duty/d_probe"
    assert "git branch -f" in reason


def test_worktree_kept_when_attempt_failed_with_work(tmp_path: Path) -> None:
    """Упавшая попытка с незакоммиченной работой дерево НЕ удаляет.

    Правило было перевёрнуто: дерево убиралось ровно тогда, когда коммита ещё не
    было, и падение уносило единственную копию правок (дело d_7d92affb: 1300
    строк на 55-й минуте — и пусто). Теперь путь сохранённого дерева — факт
    события (`tree_kept`), а пустое дерево по-прежнему убирается.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = worktree_spec(tmp_path, repo)
    fake = Fake(exit_code=2, files={"work-in-progress.py": "print('правка')\n"})
    run(conn, spec, deal, home, fake)

    tree = runner.attempt_dir(home, deal, "impl", 1) / "tree"
    assert tree.is_dir(), "дерево с работой удалено"
    assert (tree / "work-in-progress.py").is_file()
    ended = only(conn, deal, "attempt_ended")
    details = ended["payload"]["details"]
    assert ended["payload"]["outcome"] == "failed"
    assert details["cleaned"] is False
    assert details["tree_kept"] == str(tree)
    assert str(tree) in subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
    ).stdout


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


RESCUE_REPORT = {
    "status": "done",
    "summary": "разведка завершена, план готов",
    "plan": {"stages": [{"id": "group", "role": "implementer"}], "reason": "по журналу"},
    "evidence": [],
}


def plan_spec(tmp_path: Path) -> Any:
    """Спека с этапом-планом без дерева: как seed-спека кнопки «Продолжить»."""
    return write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: plan
            role: lead
            workspace: none
        """,
    )


def run_stage(conn: sqlite3.Connection, spec: Any, deal: dict[str, Any], home: Path, fake: Any) -> dict[str, Any]:
    """Один шаг движка этапа `plan` с подменённым спавном и неподвижными часами."""
    return runner.run_stage(conn, spec, deal, "plan", 1, home=home, clock=lambda: NOW, spawn=fake)


def test_report_said_but_not_written_is_rescued(tmp_path: Path) -> None:
    """Read-only роль отчиталась текстом — раннер вынимает отчёт из ответа.

    Дело d_2fc749fb: у лида нет `write_file`, отчёт ушёл финальной фразой, и
    попытка падала контрактом «result.json нет» при готовом вердикте. Раннер
    записывает файл за роль и честно помечает это в журнале.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = plan_spec(tmp_path)
    fake = Fake(
        result=None,
        exit_code=0,
        lines=[
            {"type": "text", "text": "Проверяю заметку по тикету и историю."},
            {"type": "tool_use", "name": "read_file"},
            {"type": "text", "text": f"Итог разведки:\n```json\n{json.dumps(RESCUE_REPORT, ensure_ascii=False)}\n```"},
        ],
    )
    report = run_stage(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("done", None)
    result_file = runner.result_path(runner.attempt_dir(home, deal, "plan", 1))
    assert json.loads(result_file.read_text(encoding="utf-8")) == RESCUE_REPORT
    rows = conn.execute("SELECT author, body FROM comments").fetchall()
    assert len(rows) == 1 and rows[0][0] == "engine"
    assert "из финального ответа" in rows[0][1]


def test_plain_json_answer_is_rescued_only_with_status(tmp_path: Path) -> None:
    """Голый JSON-объект тоже спасается; без `status` — нет (это не отчёт)."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = plan_spec(tmp_path)
    fake = Fake(
        result=None,
        exit_code=0,
        lines=[{"type": "text", "text": json.dumps(RESCUE_REPORT, ensure_ascii=False)}],
    )
    report = run_stage(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("done", None)

    home2 = make_home(tmp_path / "no-status")
    conn2 = open_store(home2)
    deal2 = make_deal(conn2)
    spec2 = plan_spec(tmp_path / "no-status")
    fake2 = Fake(
        result=None,
        exit_code=0,
        lines=[{"type": "text", "text": 'Вот структура: {"summary": "просто пример"} — не отчёт.'}],
    )
    report2 = run_stage(conn2, spec2, deal2, home2, fake2)
    assert (report2["outcome"], report2["kind"]) == ("failed", "contract")
    assert "result.json нет" in report2["reason"]


def test_written_result_is_not_rescued(tmp_path: Path) -> None:
    """Отчёт записан файлом — спасения нет, и факта «commented» не появляется."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = plan_spec(tmp_path)
    fake = Fake(result=dict(RESCUE_REPORT), exit_code=0)
    report = run_stage(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("done", None)
    assert "commented" not in kinds(conn, deal)


def test_plan_contract_teaches_the_proposal_form(tmp_path: Path) -> None:
    """Границы этапа плана называют форму предложения — до первого отказа хартии.

    Дело d_2fc749fb: лид узнавал «перестановка называет только needs/parallel_with»
    из повторных plan_rejected и жёг попытки реплана на ошибках формы. Правила
    формы приезжают в контракте, вместе с хартией и графом.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = plan_spec(tmp_path)
    run_stage(conn, spec, deal, home, Fake(result=None, exit_code=0, lines=[]))

    contract_file = runner.contract_path(runner.attempt_dir(home, deal, "plan", 1), "plan", 1)
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    boundaries = "\n".join(contract["boundaries"])
    assert "needs/parallel_with" in boundaries
    assert "непустой" in boundaries
    assert "приговор выносит close своим вердиктом" in boundaries
    assert "не называется вовсе" in boundaries
    assert "список команд [{cmd" in boundaries
    # Хвост этапа-плана: хартия и граф в контракте, как и было.
    assert "charter" in contract and "plan" in contract


def test_plan_contract_carries_the_last_rejection_issues(tmp_path: Path) -> None:
    """Попытка реплана видит отказы прошлого предложения — и не повторяет их вслепую.

    Кейс d_bdf8edfb: девять ревизий с одной и той же прозой в evidence — каждая
    попытка стартовала, не зная, что именно отклонил валидатор. Теперь
    `plan.last_rejected` в контракте несёт `why` и `issues[]` последнего отказа.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = plan_spec(tmp_path)
    journal.append(
        conn,
        [
            {
                "ts": 1794300000,
                "recorded_at": 1794300000,
                "deal_id": str(deal["id"]),
                "kind": "plan_rejected",
                "payload": {
                    "rev": 1,
                    "why": "план ревизии 1 отклонён: отказов 2",
                    "issues": [
                        "evidence: evidence — список доказательств",
                        "budget: budget — словарь {tokens, minutes}",
                    ],
                },
                "source": "engine",
                "idempotency_key": "test:plan_rejected:1",
            }
        ],
    )

    run_stage(conn, spec, deal, home, Fake(result=None, exit_code=0, lines=[]))
    contract_file = runner.contract_path(runner.attempt_dir(home, deal, "plan", 1), "plan", 1)
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    last = contract["plan"]["last_rejected"]
    assert "отказов 2" in last["why"]
    assert any("evidence" in issue for issue in last["issues"])


def test_contract_tells_how_to_deliver_the_report_without_write_file(
    tmp_path: Path,
) -> None:
    """Граница отчёта не лжёт роли про write_file: оба пути доставки названы.

    Кейс d_2fc749fb, попытки 1 и 6: контракт требовал «пишите write_file», роли
    инструмент не принадлежал, лид тратил попытку на охоту за ним и выносил
    приговор текстом вместо JSON. Формулировка теперь описывает оба пути: файл
    (роль с write_file) и fenced JSON в финальном ответе (раннер спасает сам).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = plan_spec(tmp_path)
    run_stage(conn, spec, deal, home, Fake(result=None, exit_code=0, lines=[]))

    contract_file = runner.contract_path(runner.attempt_dir(home, deal, "plan", 1), "plan", 1)
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    boundaries = "\n".join(contract["boundaries"])
    assert "роль с write_file кладёт его в файл" in boundaries
    assert "раннер спасёт его и запишет сам" in boundaries
    assert "fenced" in boundaries


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


def asking_spec(tmp_path: Path, ask_line: str) -> Any:
    """Спека с объявленным адресом вопросов у единственного этапа."""
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: impl
            role: implementer
            workspace: none
            ask: {ask_line}
        """,
    )


def blocked(question: dict[str, Any]) -> Fake:
    """Отчёт попытки, которая просит решения: движок обязан его адресовать."""
    return Fake(
        result={"status": "blocked", "summary": "нужен ответ", "question": question},
    )


def addressed(conn, deal) -> dict[str, Any]:
    """Адрес из факта `question_asked`: `{target, reason, stage, attempt}`."""
    payload = only(conn, deal, "question_asked")["payload"]
    address = payload.get("routing")
    assert isinstance(address, dict), "вопрос без адреса: кому он задан — неизвестно"
    return address


def test_factual_question_is_addressed_to_the_declared_role(tmp_path: Path) -> None:
    """Фактовый вопрос → роль из `ask.allowed`; адрес и причина — в журнале.

    Решает спека, а не модель: в контракте попытки адрес объявлен, и тот же адрес
    попадает в `question_asked.payload.routing` — иначе через месяц никто не
    скажет, почему вопрос ушёл роли, а не человеку. С 0.1.0 вопрос роли идёт
    **движком** (`ask_role`): пул не участвует, дело не паркуется, а ответ роли
    ложится в журнал фактом с автором (`tests/test_ask_role.py`).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = asking_spec(tmp_path, "{allowed: [researcher], to_human: decisions_only, max_hops: 2}")
    fake = Answering(result={"status": "blocked", "summary": "нужен ответ", "question": {"text": "где лежат миграции?", "kind": "fact"}})
    report = run(conn, spec, deal, home, fake)

    assert report["question_target"] == "role:researcher"
    assert report["question_reason"] == "declared_role"
    assert addressed(conn, deal) == {
        "target": "role:researcher",
        "reason": "declared_role",
        "stage": "impl",
        "attempt": 1,
        "hops": 1,
    }
    assert [call["cmd"][call["cmd"].index("-p") + 1] for call in fake.calls] == [
        "implementer",
        "researcher",
    ], "вопрос задаёт роль из `ask.allowed`, а не человек"
    assert "blocked" not in kinds(conn, deal), "вопрос роли дело не паркует"
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        assert pool.execute("SELECT count(*) FROM questions").fetchone()[0] == 0, (
            "вопрос роли в пул не пишется: второго канала нет"
        )
    finally:
        pool.close()


def test_factual_question_without_roles_is_routing_failed(tmp_path: Path) -> None:
    """`allowed` пуст → человек, причина `allowed_empty` в событии, а не догадка."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = asking_spec(tmp_path, "{allowed: [], to_human: decisions_only, max_hops: 2}")
    report = run(conn, spec, deal, home, blocked({"text": "где лежат миграции?", "kind": "fact"}))

    assert report["question_target"] == "human_routing_failed"
    assert addressed(conn, deal)["reason"] == "allowed_empty"
    assert addressed(conn, deal)["target"] == "human_routing_failed"


def test_risky_question_goes_to_human_even_with_roles(tmp_path: Path) -> None:
    """Властный вопрос (`risky: true`) — человек, даже когда роли объявлены."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = asking_spec(tmp_path, "{allowed: [publisher], risky: true, max_hops: 2}")
    fake = blocked({"text": "Публикуем diff?", "options": ["пушить", "стоп"]})
    report = run(conn, spec, deal, home, fake)

    assert report["question_target"] == "human"
    assert addressed(conn, deal)["reason"] == "risky"


def test_hops_are_counted_by_the_journal(tmp_path: Path) -> None:
    """Передачи по цепочке считаются фактами журнала, а не счётом в памяти.

    Вопрос без адреса (заданный до W5 или мимо движка) хопом не считается:
    приписать его этапу значило бы оборвать цепочку раньше объявленного.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    seed(conn, deal, "question_asked", {"question_id": 1}, key="old-without-address")
    seed(
        conn,
        deal,
        "question_asked",
        {"question_id": 2, "routing": {"target": "role:researcher", "stage": "impl"}},
        key="first-hop",
    )
    seed(
        conn,
        deal,
        "question_asked",
        {"question_id": 3, "routing": {"target": "role:researcher", "stage": "review"}},
        key="other-stage",
    )

    assert runner.hops_done(conn, deal, "impl") == 1


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


def test_token_budget_exceeded_without_result_is_capability(tmp_path: Path) -> None:
    """Превышение бюджета без отчёта — `capability`: процесс снят, отчёта нет."""
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
        result=None,
    )
    report = run(conn, spec, deal, home, fake)
    assert (report["outcome"], report["kind"]) == ("failed", "capability")
    assert fake.stopped == 1
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["details"]["over_budget"] is True
    assert ended["payload"]["details"]["usage_tokens"] == 5000


def test_finished_report_wins_over_token_budget(tmp_path: Path) -> None:
    """Готовый отчёт сильнее потолка: сделанная работа не выбрасывается.

    Так умерла попытка `impl-finished#3` дела `d_a7040118` (2026-11-06): воркер
    закоммитил работу и написал `result.json` со `status=done` и тремя
    доказательствами с кодом 0, а движок снял его за токены и записал
    `failed`/`capability` — коммит остался сиротой.
    """
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
        # `pack=True` — отчёт с пакетом: этап с ролью проходит гейт доказательств,
        # и проверяется ровно то, что нужно: чей вердикт сильнее — потолка или отчёта.
        result={"status": "done", "summary": "работа сделана"},
        pack=True,
    )
    report = run(conn, spec, deal, home, fake)

    # Процесс всё равно снят по потолку — но исход берётся из отчёта.
    assert fake.stopped == 1
    assert report["outcome"] == "done"
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
        pack=True,
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


def test_text_deltas_become_one_say_event_per_phrase(tmp_path: Path) -> None:
    """Дельты текста — один факт на фразу: видно, что агент говорил, а не молчал.

    Поток отдаёт `text` по дельте на токен. Если писать их в журнал как есть, одна
    фраза станет тысячей строк, и «думал» утонет в собственном весе. Раннер копит
    дельты и пишет одно событие: превью первой строкой и длительность куска.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = none_spec(tmp_path)
    fake = Fake(
        lines=[
            {"type": "system", "subtype": "init", "session_id": ""},
            {"type": "text", "text": "Сначала "},
            {"type": "text", "text": "посмотрю\nфайл."},
            {"type": "tool_use", "name": "read_file", "tool_call_id": "1"},
            {"type": "tool_result", "name": "read_file", "tool_call_id": "1", "duration_ms": 5},
            {"type": "text", "text": "Готово: правка на месте."},
            {"type": "result", "session_id": "local-888", "tokens": {"total": 10}},
        ],
        result={"status": "done"},
        pack=True,
    )
    run(conn, spec, deal, home, fake)

    progress = [event for event in events(conn, deal) if event["kind"] == "attempt_progress"]
    says = [event["payload"] for event in progress if event["payload"].get("phase") == "say"]
    assert [payload["preview"] for payload in says] == ["Сначала посмотрю файл.", "Готово: правка на месте."]
    assert all("duration_ms" in payload for payload in says), "кусок текста измеряется временем"
    # Фраза не съедает вызов: инструмент считается отдельно и остаётся один.
    folded = journal.fold(events(conn, deal), now=NOW)
    assert folded["progress"]["tools"] == 1
    # Сказанное двигает живость: молчащая попытка и думающая вслух — разные вещи.
    assert folded["progress"]["last_at"] == NOW

    # Превью короткое, но обрезка названа числом: «фраза была ровно 120 символов»
    # и «фраза обрезана до 120» — разные факты, и молчать о разнице нельзя.
    text = "слово " * 200
    long_say = runner._say_event(deal, "impl", 1, 9, text, 1000, NOW)
    assert len(long_say["payload"]["preview"]) <= runner.SAY_PREVIEW_CHARS
    assert "\n" not in long_say["payload"]["preview"]
    assert long_say["payload"]["len"] == len(" ".join(text.split()))
    assert long_say["payload"]["len"] > len(long_say["payload"]["preview"])


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
    fake = Fake(result={"status": "done"}, pack=True)
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


# ------------------------------------------------------------- база этапа (W14)


def fact(kind: str, payload: dict[str, Any], *, ts: int = NOW, key: str = "test:fact") -> dict[str, Any]:
    """Событие журнала в памяти: проверка базы этапа не требует БД."""
    return {
        "ts": ts,
        "recorded_at": ts,
        "deal_id": "d_probe",
        "kind": kind,
        "payload": payload,
        "source": "test",
        "idempotency_key": key,
    }


def ended_of(conn: sqlite3.Connection, deal: dict[str, Any], stage_id: str) -> dict[str, Any]:
    """`attempt_ended` конкретного этапа: в журнале их несколько, этап — разный."""
    found = [
        event
        for event in events(conn, deal)
        if str(event["kind"]) == "attempt_ended"
        and journal.payload_of(event).get("stage") == stage_id
    ]
    assert len(found) == 1, f"attempt_ended этапа {stage_id}: {len(found)}"
    return found[0]


def started_of(conn: sqlite3.Connection, deal: dict[str, Any], stage_id: str, attempt: int) -> dict[str, Any]:
    """`attempt_started` конкретной попытки: номер попытки — часть факта."""
    found = [
        event
        for event in events(conn, deal)
        if str(event["kind"]) == "attempt_started"
        and journal.payload_of(event).get("stage") == stage_id
        and int(journal.payload_of(event).get("attempt") or 0) == int(attempt)
    ]
    assert len(found) == 1, f"attempt_started {stage_id}#{attempt}: {len(found)}"
    return found[0]


def git_head(path: Path) -> str:
    """`git rev-parse HEAD` в дереве или репозитории — фактическая ревизия."""
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def git_out(path: Path, *args: str) -> str:
    """Вывод git-команды в дереве: так проверяется содержимое и чистота дерева."""
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    ).stdout


def git_commit_object(repo: Path, *, parent: str, message: str) -> str:
    """Коммит-объект поверх `parent` без движения ветки и рабочего дерева.

    `commit-tree` даёт ровно то, что нужно проверке родства: два коммита можно
    сделать и линейными, и расходящимися, не трогая ни `main`, ни checkout.
    """
    tree = git_out(repo, "rev-parse", f"{parent}^{{tree}}").strip()
    made = subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
            "commit-tree", tree, "-p", parent, "-m", message,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return made.stdout.strip()


def chain_repo_spec(tmp_path: Path, repo: Path) -> Any:
    """impl → verify, оба с деревом: цепочка, на которой E2E и упал."""
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
          - id: verify
            role: tester
            workspace: worktree
            needs: [impl]
        """,
    )


def split_repo_spec(tmp_path: Path, repo: Path, *, needs: str) -> Any:
    """Два предшественника и этап, которому нужны оба: проверка родства коммитов."""
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        repo: {repo}
        base: HEAD
        stages:
          - id: a
            role: implementer
            workspace: worktree
          - id: b
            role: implementer
            workspace: worktree
          - id: both
            role: tester
            workspace: worktree
            needs: {needs}
        """,
    )


def test_base_of_stage_without_needs_is_the_deal_base(tmp_path: Path) -> None:
    """Первый этап отводится от `spec.base`: база дела и база этапа — одно."""
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)
    assert runner.base_of_stage(spec, spec.stage("impl"), []) == "HEAD"


def test_base_of_stage_without_predecessor_result_is_the_deal_base(tmp_path: Path) -> None:
    """Предшественник без коммита и без дерева: отводить не от чего — `spec.base`."""
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)
    head = git_head(repo)
    seeded = [
        fact(
            "attempt_ended",
            {"stage": "impl", "attempt": 1, "outcome": "done", "result": {"summary": "ок"}},
        )
    ]
    assert runner.base_of_stage(spec, spec.stage("verify"), seeded) == head


def test_base_of_stage_takes_the_predecessor_commit(tmp_path: Path) -> None:
    """Этап с `needs` отводится от коммита предшественника, а не от базы дела."""
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)
    made = git_commit_object(repo, parent=git_head(repo), message="работа impl")
    seeded = [
        fact(
            "attempt_ended",
            {
                "stage": "impl",
                "attempt": 1,
                "outcome": "done",
                "commit": made,
                "result": {"summary": "готово", "artifacts": [{"path": "a.txt", "sha256": "…"}]},
            },
        )
    ]
    assert runner.base_of_stage(spec, spec.stage("verify"), seeded) == made


def test_base_of_stage_ignores_a_failed_predecessor_attempt(tmp_path: Path) -> None:
    """Провал предшественника базой не бывает: годится только успешная попытка."""
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)
    head = git_head(repo)
    made = git_commit_object(repo, parent=head, message="чужая работа")
    seeded = [
        fact(
            "attempt_ended",
            {"stage": "impl", "attempt": 1, "outcome": "failed", "kind": "contract", "commit": made},
            ts=NOW,
        )
    ]
    assert runner.base_of_stage(spec, spec.stage("verify"), seeded) == head


def test_second_stage_tree_is_detached_from_the_predecessor_commit(tmp_path: Path) -> None:
    """Дерево второго этапа стоит на коммите первого; ветку движок не двигает."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)
    head = git_head(repo)

    run(conn, spec, deal, home, Fake(result={"status": "done", "summary": "готово"}, pack=True))
    impl_commit = ended_of(conn, deal, "impl")["payload"]["commit"]
    assert impl_commit, "движок обязан записать коммит принятой попытки"

    second = runner.run_stage(
        conn,
        spec,
        deal,
        "verify",
        1,
        home=home,
        spawn=Fake(result={"status": "done", "summary": "проверил"}, pack=True),
        clock=lambda: NOW,
    )
    assert (second["outcome"], second["base"]) == ("done", impl_commit)

    tree = runner.attempt_dir(home, deal, "verify", 1) / "tree"
    assert git_head(tree) == impl_commit, "второй этап отведён от коммита первого"
    assert git_head(repo) == head, "ветку движок не двигает: её двигает публикация"
    assert git_out(tree, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"


def test_engine_commits_the_work_the_executor_did_not(tmp_path: Path) -> None:
    """Исполнитель отчитался артефактами без коммита — движок фиксирует работу сам."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)

    run(conn, spec, deal, home, Fake(result={"status": "done", "summary": "готово"}, pack=True))
    payload = ended_of(conn, deal, "impl")["payload"]

    assert "commit" not in (payload["result"] or {}), "исполнитель коммита не делал"
    assert len(str(payload["commit"])) == 40, payload["commit"]
    tree = runner.attempt_dir(home, deal, "impl", 1) / "tree"
    assert git_head(tree) == payload["commit"]
    assert git_out(tree, "status", "--porcelain").strip() == "", "дерево попытки чистое"
    tracked = git_out(tree, "show", "--name-only", "--format=", payload["commit"])
    assert "notes.txt" in tracked, "работа попытки попала в коммит"


def test_predecessor_artifacts_are_visible_in_the_next_stage_tree(tmp_path: Path) -> None:
    """Регрессия E2E `d_a2a26b55`: файл, созданный на `impl`, есть в дереве `verify`.

    Ровно этот случай и уронил живой прогон: `verify` получил чистое дерево от
    базы дела и не нашёл `dashboard/failures.py`, который сделал `impl`.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)
    made = "def failures():\n    return []\n"

    run(
        conn,
        spec,
        deal,
        home,
        Fake(result={"status": "done", "summary": "добавил"}, files={"dashboard/failures.py": made}, pack=True),
    )
    runner.run_stage(
        conn,
        spec,
        deal,
        "verify",
        1,
        home=home,
        spawn=Fake(result={"status": "done", "summary": "проверил"}, pack=True),
        clock=lambda: NOW,
    )

    tree = runner.attempt_dir(home, deal, "verify", 1) / "tree"
    assert (tree / "dashboard" / "failures.py").read_text(encoding="utf-8") == made
    assert ended_of(conn, deal, "verify")["payload"]["outcome"] == "done"


def test_contract_previous_carries_the_predecessor_commit(tmp_path: Path) -> None:
    """`previous` несёт коммит и артефакты предшественника, а не только артефакты."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)

    run(conn, spec, deal, home, Fake(result={"status": "done", "summary": "готово"}, pack=True))
    impl_commit = ended_of(conn, deal, "impl")["payload"]["commit"]
    runner.run_stage(
        conn,
        spec,
        deal,
        "verify",
        1,
        home=home,
        spawn=Fake(result={"status": "done", "summary": "проверил"}, pack=True),
        clock=lambda: NOW,
    )

    workdir = runner.attempt_dir(home, deal, "verify", 1)
    contract = json.loads(runner.contract_path(workdir, "verify", 1).read_text(encoding="utf-8"))
    previous = contract["previous"]
    assert len(previous) == 1
    assert previous[0]["stage"] == "impl"
    assert previous[0]["commit"] == impl_commit
    assert previous[0]["artifacts"] and previous[0]["artifacts"][0]["path"] == "notes.txt"
    assert previous[0]["summary"] == "готово"


def test_attempt_started_records_the_real_base(tmp_path: Path) -> None:
    """`attempt_started.payload.base` называет ревизию, от которой этап работал."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)
    head = git_head(repo)

    run(conn, spec, deal, home, Fake(result={"status": "done", "summary": "готово"}, pack=True))
    impl_commit = ended_of(conn, deal, "impl")["payload"]["commit"]
    runner.run_stage(
        conn,
        spec,
        deal,
        "verify",
        1,
        home=home,
        spawn=Fake(result={"status": "done", "summary": "проверил"}, pack=True),
        clock=lambda: NOW,
    )

    assert started_of(conn, deal, "impl", 1)["payload"]["base"] == head
    assert started_of(conn, deal, "verify", 1)["payload"]["base"] == impl_commit


def test_repeat_attempt_of_the_stage_uses_the_same_base(tmp_path: Path) -> None:
    """Повтор попытки отводится от той же базы: «что видел этап» воспроизводимо."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)

    run(conn, spec, deal, home, Fake(result={"status": "done", "summary": "готово"}, pack=True))
    impl_commit = ended_of(conn, deal, "impl")["payload"]["commit"]
    runner.run_stage(
        conn, spec, deal, "verify", 1, home=home, spawn=Fake(exit_code=2), clock=lambda: NOW
    )
    runner.run_stage(
        conn,
        spec,
        deal,
        "verify",
        2,
        home=home,
        spawn=Fake(result={"status": "done", "summary": "проверил"}, pack=True),
        clock=lambda: NOW,
    )

    assert started_of(conn, deal, "verify", 1)["payload"]["base"] == impl_commit
    assert started_of(conn, deal, "verify", 2)["payload"]["base"] == impl_commit


def test_two_needs_with_linear_commits_take_the_later(tmp_path: Path) -> None:
    """Два предшественника с линейными коммитами: база — тот, что содержит обоих.

    Проверяется в обе стороны: порядок в журнале не решает, решает родство.
    """
    repo = git_repo(tmp_path)
    spec = split_repo_spec(tmp_path, repo, needs="[a, b]")
    head = git_head(repo)
    first = git_commit_object(repo, parent=head, message="a")
    second = git_commit_object(repo, parent=first, message="b")
    stage = spec.stage("both")

    a_first = [
        fact("attempt_ended", {"stage": "a", "attempt": 1, "outcome": "done", "commit": first}, ts=NOW),
        fact("attempt_ended", {"stage": "b", "attempt": 1, "outcome": "done", "commit": second}, ts=NOW + 10),
    ]
    assert runner.base_of_stage(spec, stage, a_first) == second

    # Предок записан позже потомка: база всё равно потомок — работа предка уже в нём.
    b_first = [
        fact("attempt_ended", {"stage": "a", "attempt": 1, "outcome": "done", "commit": second}, ts=NOW + 10),
        fact("attempt_ended", {"stage": "b", "attempt": 1, "outcome": "done", "commit": first}, ts=NOW),
    ]
    assert runner.base_of_stage(spec, stage, b_first) == second


def test_divergent_predecessors_stop_the_stage_with_contract(tmp_path: Path) -> None:
    """Два расходящихся коммита без общего потомка — `failed(contract)`, а не выбор."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = split_repo_spec(tmp_path, repo, needs="[a, b]")
    head = git_head(repo)
    left = git_commit_object(repo, parent=head, message="a")
    right = git_commit_object(repo, parent=head, message="b")
    seed(
        conn,
        deal,
        "attempt_ended",
        {"stage": "a", "attempt": 1, "outcome": "done", "commit": left},
        key="test:a:ended",
        ts=NOW,
    )
    seed(
        conn,
        deal,
        "attempt_ended",
        {"stage": "b", "attempt": 1, "outcome": "done", "commit": right},
        key="test:b:ended",
        ts=NOW + 10,
    )
    fake = Fake(result={"status": "done", "summary": "готово"}, pack=True)

    report = runner.run_stage(
        conn, spec, deal, "both", 1, home=home, spawn=fake, clock=lambda: NOW
    )

    assert (report["outcome"], report["kind"]) == ("failed", "contract"), report
    assert "разошлись" in report["reason"] and left[:12] in report["reason"]
    assert fake.calls == [], "попытка не поднимается, пока база не выведена"
    assert not (runner.attempt_dir(home, deal, "both", 1) / "tree").exists()
    ended = ended_of(conn, deal, "both")
    assert (ended["payload"]["outcome"], ended["payload"]["kind"]) == ("failed", "contract")


def test_base_of_stage_reports_an_unresolvable_predecessor_commit(tmp_path: Path) -> None:
    """Коммит предшественника назван, но его нет в репозитории — отказ, а не тишина."""
    repo = git_repo(tmp_path)
    spec = chain_repo_spec(tmp_path, repo)
    seeded = [
        fact(
            "attempt_ended",
            {"stage": "impl", "attempt": 1, "outcome": "done", "commit": "0" * 40},
        )
    ]
    with pytest.raises(runner.BaseError) as caught:
        runner.base_of_stage(spec, spec.stage("verify"), seeded)
    assert "не резолвится" in str(caught.value)


# ------------------------------------------------------------- приговор ревью
#
# Гейт графа (`dashboard/transit.py`, правило 2) требует **факт уровня этапа** —
# `collected`/`changes_requested` с `stage` в payload. Пишет его движок по
# структурному полю отчёта (`report_schema.verdict`), а не кто-то руками: живой
# прогон `d_e25f89ba` встал навсегда в `wait(verdict)`, потому что писать было
# некому. Тесты ниже держат именно эту цепочку: отчёт → факт журнала → решение
# гейта, а не «функция что-то вернула».

REVIEW_CHAIN = """
        duty: probe
        stages:
          - id: impl
            role: implementer
            workspace: none
          - id: review
            role: reviewer
            needs: [impl]
            workspace: none
            verdict: {approve: next, changes: impl, max_rounds: 2}
          - id: gate
            kind: gate
            needs: [review]
            ask: {q: "Продолжаем?", options: [да, нет]}
        """


def review_chain(tmp_path: Path) -> Any:
    """Спека живой формы: `review{verdict} → gate`, дерево никому не нужно."""
    return write_spec(tmp_path, REVIEW_CHAIN)


def verdict_line() -> int:
    """Строка ключа `verdict` в спеке пробы: отказ обязан назвать именно её."""
    text = textwrap.dedent(REVIEW_CHAIN).splitlines()
    return next(index for index, row in enumerate(text, start=1) if "verdict: {approve" in row)


def seed_passed(conn, deal, stage: str = "impl", attempt: int = 1) -> None:
    """Предшественник пройден: `impl` `done` до попытки ревью."""
    seed(conn, deal, "attempt_started", {"stage": stage, "attempt": attempt}, key=f"test:{stage}:start")
    seed(
        conn,
        deal,
        "attempt_ended",
        {"stage": stage, "attempt": attempt, "outcome": "done", "kind": None},
        key=f"test:{stage}:end",
    )


def run_review(conn, spec, deal, home, verdict: Any, *, attempt: int = 1) -> dict[str, Any]:
    """Шаг ревью с подменённым спавном: отчёт — `done` плюс приговор (или без)."""
    result: dict[str, Any] = {"status": "done", "summary": "ревью прошло"}
    if verdict is not None:
        result["verdict"] = verdict
    fake = Fake(result=result, pack=True)
    return runner.run_stage(
        conn, spec, deal, "review", attempt, home=home, clock=lambda: NOW, spawn=fake
    )


def review_ended(conn, deal, stage: str = "review") -> dict[str, Any]:
    """`attempt_ended` ревью: в журнале есть и попытка предшественника."""
    found = [
        event
        for event in events(conn, deal)
        if event["kind"] == "attempt_ended" and event["payload"].get("stage") == stage
    ]
    assert len(found) == 1, f"попыток {stage}: {len(found)}"
    return found[0]


def test_review_verdict_opens_the_gate(tmp_path: Path) -> None:
    """`verdict.approve` в отчёте — факт `collected` этапа, и гейт открывается.

    Дефект `d_e25f89ba`: ревьюер писал «одобрено» словом в `summary`, факта
    уровня этапа не было, и `transit` на каждом повторе отдавал
    `wait(verdict)`. Проверяется вся цепочка: поле отчёта → событие журнала с
    `payload.stage='review'` → решение `run(gate)`.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = review_chain(tmp_path)
    seed_passed(conn, deal)

    report = run_review(conn, spec, deal, home, {"action": "approve", "target": "", "reason": "дифф чистый"})

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    fact = only(conn, deal, "collected")
    assert fact["payload"] == {
        "stage": "review",
        "target": "next",
        "reason": "дифф чистый",
        "attempt": 1,
    }
    assert fact["source"] == runner.ENGINE_SOURCE
    decision = transit.next_stage(spec, events(conn, deal), NOW)
    assert (decision.kind, decision.stages) == ("run", ("gate",))
    assert transit.stage_states(spec, events(conn, deal))["gate"].state == "pending"


def test_review_changes_returns_to_the_named_stage(tmp_path: Path) -> None:
    """`verdict.changes` — факт `changes_requested`, и граф возвращает на цель."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = review_chain(tmp_path)
    seed_passed(conn, deal)

    report = run_review(conn, spec, deal, home, {"action": "changes", "target": "impl", "reason": "нет тестов"})

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    fact = only(conn, deal, "changes_requested")
    assert fact["payload"] == {
        "stage": "review",
        "target": "impl",
        "reason": "нет тестов",
        "attempt": 1,
    }
    decision = transit.next_stage(spec, events(conn, deal), NOW)
    assert (decision.kind, decision.pair) == ("run", [("impl", 2)])


def test_review_without_verdict_is_contract(tmp_path: Path) -> None:
    """`done` без приговора — отказ, а не тишина: иначе дефект вернётся молчанием.

    Отказ выдаёт гейт пакета (`evidence.witness`, 0.0.5): у этапа с приговором
    свидетель один — `verdict`, и `commit|artifacts` его не заменяют, даже если
    ревьюер приложил файл-свидетельство.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = review_chain(tmp_path)
    seed_passed(conn, deal)

    report = run_review(conn, spec, deal, home, None)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert "нужен verdict {action, target, reason}" in report["reason"]
    assert "commit/artifacts не требуются" in report["reason"]
    assert "collected" not in kinds(conn, deal)
    assert review_ended(conn, deal)["payload"]["kind"] == runner.FAIL_CONTRACT


def test_empty_verdict_is_refused_with_the_spec_line(tmp_path: Path) -> None:
    """Пустой приговор проходит форму и отказывает смыслом: `file:line` спеки в причине.

    Гейт пакета видит только «`verdict` — словарь», а какой именно приговор от
    этапа ждут, знает движок (`runner.report_verdict`): отказ обязан назвать
    строку спеки, иначе человек ищет требование по коду.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = review_chain(tmp_path)
    seed_passed(conn, deal)

    report = run_review(conn, spec, deal, home, {})

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert f"{spec.path}:{verdict_line()}" in report["reason"]
    assert "verdict.action=None" in report["reason"]
    assert "collected" not in kinds(conn, deal)


def test_review_verdict_without_work_package_is_accepted(tmp_path: Path) -> None:
    """Приговор и есть свидетель этапа ревью: без commit и artifacts пакет принят.

    Дефект FIX-W: гейт требовал `commit|artifacts` безусловно, и `review` с
    честным приговором получал `failed(contract)` на каждом повторе. Дерево
    ревьюер не меняет — называть работу ему нечем.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = review_chain(tmp_path)
    seed_passed(conn, deal)
    fake = Fake(
        result={
            "status": "done",
            "summary": "ревью прошло",
            "evidence": [],
            "verdict": {"action": "approve", "target": "", "reason": "дифф чистый"},
        }
    )

    report = runner.run_stage(conn, spec, deal, "review", 1, home=home, spawn=fake, clock=lambda: NOW)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    ended = review_ended(conn, deal)["payload"]
    assert ended["details"]["witness"] == "verdict"
    assert ended["details"]["verdict"]["witness"] == "verdict"
    assert only(conn, deal, "collected")["payload"]["stage"] == "review"


def test_review_verdict_with_unknown_target_is_contract(tmp_path: Path) -> None:
    """Приговор адресует этап, которого нет, — отказ `contract` с `file:line`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = review_chain(tmp_path)
    seed_passed(conn, deal)

    report = run_review(conn, spec, deal, home, {"action": "approve", "target": "нет-такого", "reason": ""})

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert "verdict.target='нет-такого'" in report["reason"]
    assert f"{spec.path}:{verdict_line()}" in report["reason"]
    assert "collected" not in kinds(conn, deal)


def test_review_verdict_beyond_max_rounds_is_contract(tmp_path: Path) -> None:
    """Возвратов больше `max_rounds` — отказ, а не бесконечный возврат.

    Раунды считаются по журналу (`changes_requested` этого же этапа), поэтому
    проверка не зависит от памяти процесса: после рестарта лимит тот же.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = review_chain(tmp_path)
    seed_passed(conn, deal)
    for round_no in (1, 2):
        seed(
            conn,
            deal,
            "changes_requested",
            {"stage": "review", "target": "impl", "reason": "круг", "attempt": round_no},
            key=f"test:review:changes:{round_no}",
        )

    report = run_review(conn, spec, deal, home, {"action": "changes", "target": "impl", "reason": "ещё раз"}, attempt=3)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert "возвратов на доработку уже 2 при max_rounds=2" in report["reason"]
    assert [event["payload"].get("attempt") for event in events(conn, deal) if event["kind"] == "changes_requested"] == [1, 2]


# ------------------------------------------------------------- shell-этап


def shell_spec(tmp_path: Path, cmd: str, *, extra: str = "", needs: str = "") -> Any:
    """Спека с этапом-командой: `cmd` в `check`, дерева нет (workspace: none)."""
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: check
            kind: shell
            cmd: {json.dumps(cmd)}
        {textwrap.indent(textwrap.dedent(needs), "    ") if needs else ""}
        {textwrap.indent(textwrap.dedent(extra), "    ") if extra else ""}
        """,
    )


def run_shell(conn, spec, deal, home, **kwargs):
    """Один шаг shell-этапа: настоящий процесс, неподвижные часы."""
    kwargs.setdefault("clock", lambda: NOW)
    return runner.run_stage(conn, spec, deal, "check", 1, home=home, **kwargs)


def test_shell_stage_runs_command_and_ends_done(tmp_path: Path) -> None:
    """Команда дошла до конца с кодом `must` — попытка `done`, и это видно в журнале.

    Проверяется ровно то, что записалось: исход, `source: shell` у обоих событий,
    `exit_code`, `duration_sec`, команда и хвосты вывода. Файл полного лога —
    тоже факт: журнал хранит хвост, а разбирать отказ человек будет по файлу.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = shell_spec(tmp_path, "echo hello; echo oops >&2")
    report = run_shell(conn, spec, deal, home)

    assert report["started"] is True
    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    started = only(conn, deal, "attempt_started")
    ended = only(conn, deal, "attempt_ended")
    assert (started["source"], ended["source"]) == (runner.SHELL_SOURCE, runner.SHELL_SOURCE)
    assert started["payload"]["stage_kind"] == "shell"
    assert started["payload"]["cmd"] == ["/bin/sh", "-c", "echo hello; echo oops >&2"]
    assert started["payload"]["shell_cmd"] == "echo hello; echo oops >&2"
    details = ended["payload"]["details"]
    assert details["cmd"] == "echo hello; echo oops >&2"
    assert details["exit_code"] == 0
    assert details["duration_sec"] >= 0
    assert details["stdout_tail"] == ["hello"]
    assert details["stderr_tail"] == ["oops"]
    assert Path(details["stdout_path"]).read_text(encoding="utf-8") == "hello\n"
    assert Path(details["stderr_path"]).read_text(encoding="utf-8") == "oops\n"
    assert ended["payload"]["result"]["summary"].startswith("shell: echo hello")


def test_shell_nonzero_exit_is_contract(tmp_path: Path) -> None:
    """Код 1 при `must: 0` — класс `contract` с причиной «код 1, ждали 0» и хвостом stderr.

    Это договор shell-этапа: процесс дошёл до конца и сказал «нет», поэтому вина
    на контракте команды, а не на инфраструктуре. Тот же тест — красная проба:
    если убрать сверку с `must`, он падает («код 1 при must 0 даёт contract»).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = shell_spec(tmp_path, "echo boom >&2; exit 1")
    report = run_shell(conn, spec, deal, home)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert report["reason"] == "код 1, ждали 0"
    ended = only(conn, deal, "attempt_ended")
    payload = ended["payload"]
    assert payload["reason"] == "код 1, ждали 0"
    assert payload["details"]["exit_code"] == 1
    assert payload["details"]["stderr_tail"] == ["boom"]
    assert "result" not in payload


def test_shell_result_json_does_not_decide_the_outcome(tmp_path: Path) -> None:
    """Договор — код выхода, а не `result.json`: даже «done» в файле не спасает `exit 1`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = shell_spec(tmp_path, 'mkdir -p .duty && echo \'{"status": "done"}\' > .duty/result.json; exit 2')
    report = run_shell(conn, spec, deal, home)
    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert report["reason"] == "код 2, ждали 0"


def test_shell_must_accepts_its_own_code(tmp_path: Path) -> None:
    """`must: 1` — команда, для которой «нашлось расхождение» и есть успех."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = shell_spec(tmp_path, "exit 1", extra="    must: 1\n")
    report = run_shell(conn, spec, deal, home)
    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    assert only(conn, deal, "attempt_ended")["payload"]["details"]["exit_code"] == 1


def test_shell_artifacts_are_checked_by_evidence_gate(tmp_path: Path) -> None:
    """Объявленные артефакты проверяет тот же гейт: файл есть, sha256 — по содержимому."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = shell_spec(
        tmp_path, "printf 'ok' > report.txt", extra="    artifacts: [report.txt]\n"
    )
    report = run_shell(conn, spec, deal, home)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    payload = only(conn, deal, "attempt_ended")["payload"]
    assert payload["details"]["verdict"]["ok"] is True
    artifacts = payload["result"]["artifacts"]
    assert [item["path"] for item in artifacts] == ["report.txt"]
    assert artifacts[0]["sha256"] == hashlib.sha256(b"ok").hexdigest()


def test_shell_missing_artifact_is_contract(tmp_path: Path) -> None:
    """Объявленного файла нет — отказ гейта `artifact.missing`, а не `done`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = shell_spec(tmp_path, "true", extra="    artifacts: [report.txt]\n")
    report = run_shell(conn, spec, deal, home)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert "artifact.missing" in report["reason"]
    payload = only(conn, deal, "attempt_ended")["payload"]
    assert payload["details"]["verdict"]["ok"] is False


def test_shell_absolute_artifact_path_is_refused(tmp_path: Path) -> None:
    """Абсолютный путь — вне дерева этапа: отказ `artifact.path`, даже если файл есть."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    outside = tmp_path / "outside.txt"
    outside.write_text("чужое\n", encoding="utf-8")
    spec = shell_spec(tmp_path, "true", extra=f"    artifacts: [{outside}]\n")
    report = run_shell(conn, spec, deal, home)
    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert "artifact.path" in report["reason"]


def test_classify_shell_timeout_and_signal_are_infra() -> None:
    """«Команда не дожила» — `infra`, как у агента: таймаут и смерть по сигналу."""
    assert runner.classify_shell(exit_code=None, timed_out=True, must=0, timeout_sec=30) == (
        runner.OUTCOME_FAILED,
        runner.FAIL_INFRA,
        "команда не дожила: таймаут этапа 30 с",
    )
    assert runner.classify_shell(exit_code=-15, timed_out=False, must=0, timeout_sec=30) == (
        runner.OUTCOME_FAILED,
        runner.FAIL_INFRA,
        "команда не дожила: сигнал 15",
    )
    assert runner.classify_shell(exit_code=None, timed_out=False, must=0, timeout_sec=30)[1] == runner.FAIL_INFRA


def test_follow_shell_stops_the_command_at_the_deadline(tmp_path: Path) -> None:
    """Таймаут этапа — наш факт: команда, которая не уложилась, гасится, а не ждётся."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    stage = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: check
            kind: shell
            cmd: "sleep 30"
            budget: {minutes: 1}
        """,
    ).stage("check")
    workdir = tmp_path / "attempt"
    workdir.mkdir()
    process = runner.spawn_process(["/bin/sh", "-c", "sleep 30"], workdir, None, logs=workdir)
    began = time.monotonic()
    followed = runner.follow_shell(
        conn,
        process,
        deal=deal,
        stage=stage,
        attempt=1,
        ttl=60,
        deadline=began + 0.3,
        monotonic=time.monotonic,
        clock=lambda: NOW,
    )
    process.close_logs()
    assert followed.timed_out is True
    assert time.monotonic() - began < 10
    assert process.exit_code is not None


def test_shell_env_drops_role_and_tokens(tmp_path: Path) -> None:
    """Окружение команды — как у попытки, но без роли и токенов: права этап не просил."""
    env = runner.shell_env(
        {"PATH": "/bin", "HERMES_HOME": "/h", "HERMES_PROFILE": "publisher", "GITLAB_TOKEN": "t", "LANG": "ru"}
    )
    assert env == {"PATH": "/bin", "HERMES_HOME": "/h", "LANG": "ru"}


def test_shell_stage_in_worktree_keeps_tree_and_commit(tmp_path: Path) -> None:
    """Дерево этапа работает как раньше: команда бежит в нём, работа фиксируется коммитом."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = write_spec(
        tmp_path,
        f"""
        duty: probe
        repo: {repo}
        base: HEAD
        stages:
          - id: check
            kind: shell
            workspace: worktree
            cmd: "printf 'report' > report.txt"
        """,
    )
    report = run_shell(conn, spec, deal, home)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    tree = Path(str(report["tree"]))
    assert (tree / "report.txt").read_text(encoding="utf-8") == "report"
    payload = only(conn, deal, "attempt_ended")["payload"]
    assert payload["commit"], "принятая работа зафиксирована коммитом в дереве попытки"
    assert tree.exists(), "успешная попытка дерево оставляет"


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



# ---------------------------------------------------------------- гейт


def gate_spec(
    tmp_path: Path,
    *,
    ask: str = 'q: "Выкатываем?", options: [да, нет]',
    extra: str = "",
    needs: str = "",
    gate_needs: str = "",
    after: str = "",
) -> Any:
    """Спека с этапом-гейтом: вопрос человеку и (по желанию) хвост/предшественник.

    Строки собираются по одной, а не подстановкой в общий шаблон: `dedent` в
    `write_spec` выровнял бы вставку по общему отступу и сломал YAML — проверка
    спеки от этого падала бы не тем отказом, который проверяет тест.
    """
    lines = ["duty: probe", "stages:"]
    if needs:
        lines.append(textwrap.indent(textwrap.dedent(needs).strip("\n"), "  "))
    lines += ["  - id: gate", "    kind: gate", f"    ask: {{{ask}}}"]
    if gate_needs:
        lines.append(f"    needs: [{gate_needs}]")
    if extra:
        lines.append(textwrap.indent(textwrap.dedent(extra).strip("\n"), "    "))
    if after:
        lines.append(textwrap.indent(textwrap.dedent(after).strip("\n"), "  "))
    return write_spec(tmp_path, "\n".join(lines) + "\n")


def run_gate(conn, spec, deal, home, *, attempt: int = 1) -> dict[str, Any]:
    """Один шаг гейта: неподвижные часы, никакой процесс не нужен."""
    return runner.run_stage(conn, spec, deal, "gate", attempt, home=home, clock=lambda: NOW)


def pool_rows(home: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM questions ORDER BY id")]
    finally:
        conn.close()


def answer_gate(home: Path, question_id: Any, text: str | None, *, status: str = "answered") -> None:
    """Человек ответил (или вопрос истёк) — так вопрос закрывает пул, не duty."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        conn.execute(
            "UPDATE questions SET status = ?, answer = ?, answered_at = ?, answered_via = ? WHERE id = ?",
            (status, text, "2026-01-01T00:00:00.000Z", None if status == "answered" else None, int(question_id)),
        )
        conn.commit()
    finally:
        conn.close()


def sync(conn, home: Path) -> dict[str, Any]:
    """Дочитать пул в журнал — то, что в живом прогоне делает драйвер."""
    return load_module("hermes_duty_ask", "ask.py").sync_answers(conn, home)


def test_gate_asks_human_and_waits(tmp_path: Path) -> None:
    """Гейт спрашивает человека и уходит ждать: ни процесса, ни лизинга, ни дерева.

    Проверяется то, что записалось: `question_asked` с адресом этапа в
    `routing`, вопрос в пуле с вариантами ответа и сроком, дело в `blocked` с
    `needs_input`, и **отсутствие** `attempt_started` — попытки-процесса у гейта
    нет, а её факт закрыла бы уборка осиротевших.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path)
    report = run_gate(conn, spec, deal, home)

    assert report["started"] is True
    question_id = report["question_id"]
    # Исхода у вопроса нет: его назовёт ответ человека, и пустое поле «исход»
    # здесь читалось бы как решение, которого ещё не было.
    assert "outcome" not in report
    assert f"вопрос #{question_id}" in report["reason"]
    asked = only(conn, deal, "question_asked")
    assert asked["source"] == "ask"
    assert asked["payload"]["question_id"] == question_id
    assert asked["payload"]["routing"]["stage"] == "gate"
    assert asked["payload"]["routing"]["attempt"] == 1
    assert "attempt_started" not in kinds(conn, deal)
    assert runner.lease_of(conn, deal, "gate") is None

    row = pool_rows(home)[0]
    assert (row["status"], row["critical"]) == ("pending", 0)
    assert json.loads(row["options_json"]) == ["да", "нет"]
    # Умолчание срока — общий срок вопроса в секундах: `budget.minutes` гейт не задавал.
    assert row["timeout"] == load_module("hermes_duty_ask", "ask.py").ASK_TIMEOUT_HOURS * 3600.0

    blocked = only(conn, deal, "blocked")
    assert blocked["payload"]["block_kind"] == "needs_input"
    assert blocked["payload"]["question_id"] == question_id
    assert "гейт gate" in blocked["payload"]["reason"]


def test_gate_second_pass_does_not_ask_again(tmp_path: Path) -> None:
    """Повторный шаг до ответа второго вопроса не задаёт: человек отвечает на один."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path)
    first = run_gate(conn, spec, deal, home)
    second = run_gate(conn, spec, deal, home)

    assert second["started"] is False
    assert second["question_id"] == first["question_id"]
    assert "ответа ещё нет" in second["reason"]
    assert len(pool_rows(home)) == 1
    assert len([e for e in events(conn, deal) if e["kind"] == "question_asked"]) == 1


def test_gate_answer_inside_allow_is_done(tmp_path: Path) -> None:
    """Ответ из `allow` («да») закрывает этап: `attempt_ended` done с цитатой ответа."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path)
    asked = run_gate(conn, spec, deal, home)
    answer_gate(home, asked["question_id"], "да")
    assert sync(conn, home)["appended"] == 1

    report = run_gate(conn, spec, deal, home, attempt=2)

    assert (report["started"], report["outcome"], report["kind"]) == (True, runner.OUTCOME_DONE, None)
    assert report["answer"] == "да"
    assert report["stage_kind"] == "gate"
    ended = only(conn, deal, "attempt_ended")
    assert (ended["payload"]["outcome"], ended["payload"].get("kind")) == (runner.OUTCOME_DONE, None)
    details = ended["payload"]["details"]
    assert (details["stage_kind"], details["answer"], details["allow"]) == (
        "gate",
        "да",
        ["да", "нет"],
    )
    assert details["question_id"] == asked["question_id"]
    assert "attempt_started" not in kinds(conn, deal)


def test_gate_answer_outside_allow_is_contract(tmp_path: Path) -> None:
    """Ответ «нет» при `allow: [да]` — отказ класса `contract` с цитатой ответа.

    Это красная проба приёмки: если убрать сверку с `allow`, гейт объявит `done`
    на «нет», и тест упадёт на первой же проверке.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path, extra="allow: [да]")
    asked = run_gate(conn, spec, deal, home)
    answer_gate(home, asked["question_id"], "нет")
    sync(conn, home)

    report = run_gate(conn, spec, deal, home, attempt=2)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert "«нет»" in report["reason"] and "вне allow" in report["reason"]
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["kind"] == runner.FAIL_CONTRACT
    assert ended["payload"]["details"]["answer"] == "нет"
    # Отказ человека виден поверхности: решение «дальше не идём» — факт, а не тишина.
    blocked = [e for e in events(conn, deal) if e["kind"] == "blocked"]
    assert blocked and "дальше дело не идёт" in blocked[-1]["payload"]["reason"]


def test_gate_allow_defaults_to_every_option(tmp_path: Path) -> None:
    """Без `allow` продолжением считается любой объявленный вариант, а не любой текст."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path)
    asked = run_gate(conn, spec, deal, home)
    answer_gate(home, asked["question_id"], "наверное")
    sync(conn, home)

    report = run_gate(conn, spec, deal, home, attempt=2)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_FAILED, runner.FAIL_CONTRACT)
    assert "«наверное»" in report["reason"]


def test_gate_budget_minutes_is_the_question_deadline(tmp_path: Path) -> None:
    """`budget.minutes` гейта — срок ответа в пуле, а не бюджет процесса."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path, extra="budget: {minutes: 5}")
    run_gate(conn, spec, deal, home)

    row = pool_rows(home)[0]
    assert row["timeout"] == 300.0
    # Срок существует, чтобы однажды истечь: критичный вопрос пул не истекает.
    assert row["critical"] == 0


def test_gate_expired_question_is_not_an_answer(tmp_path: Path) -> None:
    """Истёкший вопрос — не ответ: гейт говорит «человек не ответил», а не судит тишину."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path)
    asked = run_gate(conn, spec, deal, home)
    answer_gate(home, asked["question_id"], None, status="expired")
    sync(conn, home)

    report = run_gate(conn, spec, deal, home, attempt=2)

    assert report["started"] is False
    assert "закрыт по сроку" in report["reason"]
    assert "attempt_ended" not in kinds(conn, deal)


def test_gate_runs_without_tree_even_with_needs(tmp_path: Path) -> None:
    """У гейта нет дерева даже после предшественника: работы, для которой оно нужно, нет.

    Спека без `repo`/`base` проходит проверку именно потому, что `gate` не просит
    `worktree`: будь иначе, умолчание этапа с `needs` потребовало бы репозиторий.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(
        tmp_path,
        needs="""
        - id: impl
          role: implementer
        """,
        gate_needs="impl",
    )
    assert spec.stage("gate").workspace == "none"
    assert spec_module.validate_spec(spec, profiles=frozenset({"implementer"})) == []
    report = run_gate(conn, spec, deal, home)
    assert report["started"] is True
    assert not (home / "worktrees").exists()


def test_run_stage_dispatches_gate_kind(tmp_path: Path) -> None:
    """`kind: gate` — диспетчер вида этапа, а не режим агента: спавн не зовётся."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path)

    def no_spawn(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - не должен зваться
        raise AssertionError("гейт не спавнит процесс")

    report = runner.run_stage(conn, spec, deal, "gate", 1, home=home, spawn=no_spawn, clock=lambda: NOW)
    assert report["question_id"] > 0


def test_gate_stage_does_not_go_through_the_package_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гейт не проходит проверку пакета: его факты пишет движок, а не отчёт агента.

    Приёмка FIX-W: свидетель (`commit|artifacts`/`verdict`/`published`) относится к
    отчёту исполнителя. У гейта отчёта нет вовсе, и если бы он попадал в
    `evidence.verify`, любой ответ человека закрывался бы отказом «работа не
    названа» — вопрос стал бы неотвечаемым.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = gate_spec(tmp_path)
    asked = run_gate(conn, spec, deal, home)
    answer_gate(home, asked["question_id"], "да")
    assert sync(conn, home)["appended"] == 1

    def boom(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - не должен зваться
        raise AssertionError("гейт пакета к этапу-гейту не применяется")

    monkeypatch.setattr(runner.evidence_module, "verify", boom)

    report = run_gate(conn, spec, deal, home, attempt=2)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    ended = ended_of(conn, deal, "gate")["payload"]
    assert ended["details"]["stage_kind"] == runner.STAGE_KIND_GATE
    assert "result" not in ended
    assert "witness" not in ended["details"]


# ---------------------------------------------------------------- публикация


def git_origin(tmp_path: Path) -> tuple[Path, Path]:
    """Репозиторий с настоящим `origin`: `git ls-remote` проверяется живым git.

    Без origin сверка публикации была бы проверена на подставленном «нашлось»:
    тогда тест не поймал бы ни отсутствующий sha, ни чужую ветку.
    """
    repo = git_repo(tmp_path)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main"], check=True)
    return repo, origin


def head_of(repo: Path, rev: str = "HEAD") -> str:
    """Полный sha ревизии — то, с чем движок сверяет одобренный коммит."""
    got = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", rev], capture_output=True, text=True, check=True
    )
    return got.stdout.strip()


def commit_sha(tree: str, parent: str, stamp: int) -> str:
    """sha коммита по его содержимому: `git` считает ровно это, а нам нужен перебор.

    Дата в подписи меняет sha1 целиком, поэтому пара коммитов с общим началом
    находится без единого запуска git.
    """
    body = (
        f"tree {tree}\nparent {parent}\n"
        f"author t <t@t> {stamp} +0000\ncommitter t <t@t> {stamp} +0000\n\namb\n"
    )
    raw = body.encode("utf-8")
    return hashlib.sha1(b"commit %d\0" % len(raw) + raw).hexdigest()


def ambiguous_prefix(repo: Path, origin: Path) -> str:
    """Начало sha, под которое в origin подходят **два разных** коммита.

    Неоднозначность проверяется живой: две ссылки в настоящем `origin`, а не
    подставленный вывод `ls-remote`. Пару ищет перебор дат, а в репозиторий
    попадают ровно те коммиты, чьи sha сравнивались (сверка `commit-tree`).
    """
    tree = head_of(repo, "HEAD^{tree}")
    parent = head_of(repo, "HEAD")
    width = 4
    seen: dict[str, int] = {}
    takens = {parent[:width], tree[:width]}
    pair: Optional[tuple[int, int]] = None
    for stamp in range(1_700_000_000, 1_700_100_000):
        prefix = commit_sha(tree, parent, stamp)[:width]
        if prefix in takens:
            continue
        if prefix in seen:
            pair = (seen[prefix], stamp)
            break
        seen[prefix] = stamp
    assert pair is not None, "пара коммитов с общим началом не нашлась"
    made: list[str] = []
    for stamp in pair:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_AUTHOR_DATE": f"{stamp} +0000",
            "GIT_COMMITTER_DATE": f"{stamp} +0000",
        }
        sha = subprocess.run(
            ["git", "-C", str(repo), "commit-tree", tree, "-p", parent, "-m", "amb"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        ).stdout.strip()
        # Коммит в репозитории — ровно тот, чей sha сравнивался: иначе перебор
        # доказывал бы не то, что попало в origin.
        assert sha == commit_sha(tree, parent, stamp)
        tagged = subprocess.run(
            ["git", "-C", str(repo), "tag", f"amb-{stamp}", sha], capture_output=True, text=True
        )
        assert tagged.returncode == 0, tagged.stderr
        made.append(sha)
    assert len(made) == 2, made
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "--tags"], check=True)
    return made[0][:width]


def publish_spec(tmp_path: Path, repo: Path, *, allow: str = "да", edits: str = "") -> Any:
    """Спека «impl → гейт → publish»: вопрос человеку и публикующий этап за ним."""
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        repo: {repo}
        base: HEAD
        stages:
          - id: impl
            role: implementer
            workspace: none
          - id: gate
            kind: gate
            needs: [impl]
            ask: {{q: "Публикуем этот коммит?", options: [да, нет]}}
            allow: [{allow}]
        {textwrap.indent(textwrap.dedent(edits), "    ") if edits else ""}
          - id: publish
            role: publisher
            needs: [gate]
            workspace: worktree
        """,
    )


def publish_report(sha: str, **extra: Any) -> dict[str, Any]:
    """Отчёт публикующего этапа той формы, какую принимает гейт evidence.

    Своего коммита у публикации нет — дерево не меняется, и `commit` в отчёте
    гейт отверг бы как «чужое готовое» (он был предком базы). Работу называет
    пакет (`pack=True` в `Fake`: файл-свидетельство), а факт публикации —
    `published`, который движок сверит с origin.
    """
    published: dict[str, Any] = {"sha": sha}
    published.update(extra.pop("published", {}) if isinstance(extra.get("published"), dict) else {})
    report: dict[str, Any] = {"status": "done", "summary": "запушил", "published": published}
    report.update(extra)
    return report


def run_publish(conn, spec, deal, home, fake, *, attempt: int = 1) -> dict[str, Any]:
    """Один шаг публикующего этапа: часы неподвижны, спавн подменён фейком."""
    return runner.run_stage(
        conn, spec, deal, "publish", attempt, home=home, spawn=fake, clock=lambda: NOW
    )


def approve(
    conn, spec, deal, home, *, answer: str = "да", attempt: int = 1
) -> dict[str, Any]:
    """Пройти гейт: вопрос, ответ человека в пуле, приговор гейта."""
    asked = runner.run_stage(conn, spec, deal, "gate", attempt, home=home, clock=lambda: NOW)
    answer_gate(home, asked["question_id"], answer)
    sync(conn, home)
    run_gate(conn, spec, deal, home, attempt=attempt + 1)
    return asked


def test_publish_without_gate_question_is_refused_before_spawn(tmp_path: Path) -> None:
    """Гейт в предках есть, но человека не спрашивали — публикация не поднимается.

    Отказ пишется как у исчерпанного потолка (`attempt_ended` без
    `attempt_started`): лизинг означал бы этап за живым процессом, которого нет.
    """
    repo, _origin = git_origin(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)

    def no_spawn(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - не должен зваться
        raise AssertionError("без «ок» публикацию не спавнят")

    report = runner.run_stage(
        conn, spec, deal, "publish", 1, home=home, spawn=no_spawn, clock=lambda: NOW
    )

    assert report["started"] is False
    assert report["refused"] is True
    assert report["kind"] == runner.FAIL_CONTRACT
    assert "публикация требует вопроса человеку (kind: gate) в предках" in report["reason"]
    assert "attempt_started" not in kinds(conn, deal)
    ended = only(conn, deal, "attempt_ended")
    assert ended["payload"]["details"]["refused"] is True
    assert ended["payload"]["details"]["stage_kind"] == "agent"
    blocked = only(conn, deal, "blocked")
    assert blocked["payload"]["block_kind"] == "needs_input"
    assert runner.lease_of(conn, deal, "publish") is None


def test_publish_after_the_tree_moved_is_contract(tmp_path: Path) -> None:
    """Красная проба карточки: человек одобрил один коммит, а в дереве уже другой.

    Снимаешь сверку `approval.commit` с текущей базой (в `publish.approval` или
    вызов в `runner.run_stage`) — эта попытка уходит публиковать чужое дерево, и
    тест падает на первой же проверке.
    """
    repo, _origin = git_origin(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    approved = head_of(repo, "HEAD")
    asked = approve(conn, spec, deal, home)
    assert only(conn, deal, "question_asked")["payload"]["routing"]["approved_commit"] == approved

    # После «ок» предшественник отработал ещё раз и произвёл другой коммит.
    (repo / "after.txt").write_text("после ок\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "later"],
        check=True,
    )
    moved = head_of(repo, "HEAD")
    assert moved != approved
    seed(
        conn,
        deal,
        "attempt_ended",
        {"run_id": "impl#2", "stage": "impl", "attempt": 2, "outcome": "done", "kind": None, "commit": moved},
        key="test:impl:2",
    )

    def no_spawn(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover - не должен зваться
        raise AssertionError("подменённое дерево не публикуется")

    report = runner.run_stage(
        conn, spec, deal, "publish", 1, home=home, spawn=no_spawn, clock=lambda: NOW
    )

    assert report["refused"] is True
    assert report["kind"] == runner.FAIL_CONTRACT
    assert f"одобрен commit {approved[:12]}" in report["reason"]
    assert f"в дереве {moved[:12]}" in report["reason"]
    assert asked["question_id"] > 0
    assert "attempt_started" not in kinds(conn, deal)
    # Отказ — факт журнала: попытку не поднимали, и лизинга у этапа нет.
    assert [event for event in events(conn, deal) if event["kind"] == "blocked"][-1]["payload"]["block_kind"] == "needs_input"
    assert runner.lease_of(conn, deal, "publish") is None


def test_publish_report_without_a_work_package_is_accepted(tmp_path: Path) -> None:
    """Публикующий этап называет `published`, а не дерево: пакет принят без commit/artifacts.

    Красная проба FIX-W: живой прогон TF3-c отказывал каждой публикации
    (`result.json:artifacts: required: status: done без commit и без artifacts`),
    потому что гейт требовал работу от этапа, который дерево не меняет. Свидетель
    публикации — `published`, и его движок сверяет с origin строже дерева.
    """
    repo, _origin = git_origin(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    approve(conn, spec, deal, home)
    pushed = head_of(repo, "HEAD")
    fake = Fake(
        result={
            "status": "done",
            "summary": "запушил",
            "evidence": [],
            "published": {"sha": pushed, "branch": "main"},
        }
    )

    report = run_publish(conn, spec, deal, home, fake)

    assert (report["outcome"], report["kind"]) == (runner.OUTCOME_DONE, None)
    ended = ended_of(conn, deal, "publish")["payload"]
    assert ended["details"]["witness"] == "published"
    fact = only(conn, deal, "published")["payload"]
    assert (fact["sha"], fact["ref"], fact["verified_by"]) == (pushed, "refs/heads/main", runner.PUBLISH_VERIFY)


def test_publish_of_the_approved_commit_is_confirmed_by_origin(tmp_path: Path) -> None:
    """Публикация идёт: sha из origin подтверждает заявку, в журнале факт `published`."""
    repo, _origin = git_origin(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    asked = approve(conn, spec, deal, home)
    pushed = head_of(repo, "HEAD")
    fake = Fake(result=publish_report(pushed, published={"sha": pushed, "branch": "main", "mr_url": "https://mr/1"}), pack=True)

    report = run_publish(conn, spec, deal, home, fake)

    assert report["outcome"] == runner.OUTCOME_DONE
    # Контракт назвал одобренный коммит: исполнитель публикует его, а не «то, что найдёт».
    contract = json.loads(runner.contract_path(runner.attempt_dir(home, deal, "publish", 1), "publish", 1).read_text(encoding="utf-8"))
    assert contract["approved"]["commit"] == pushed
    assert contract["approved"]["question_id"] == str(asked["question_id"])
    fact = only(conn, deal, "published")["payload"]
    assert (fact["sha"], fact["branch"], fact["mr_url"]) == (pushed, "main", "https://mr/1")
    assert fact["verified_by"] == runner.PUBLISH_VERIFY
    assert fact["ref"] == "refs/heads/main"
    assert ended_of(conn, deal, "publish")["payload"]["outcome"] == runner.OUTCOME_DONE


def test_publish_claim_by_a_short_sha_is_confirmed_by_origin(tmp_path: Path) -> None:
    """Сокращённый sha (так пишет публикатор) — факт, а не «нет в origin».

    Приёмка FIX-S4a: полное равенство объявляло честную публикацию ложью, потому
    что исполнитель пишет семь знаков, а `ls-remote` отдаёт сорок. Факт обязан
    нести **полный** sha из origin, а заявку — рядом (`claimed_sha`).
    """
    repo, _origin = git_origin(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    approve(conn, spec, deal, home)
    pushed = head_of(repo, "HEAD")
    fake = Fake(result=publish_report(pushed[:7], published={"branch": "main"}), pack=True)

    report = run_publish(conn, spec, deal, home, fake)

    assert report["outcome"] == runner.OUTCOME_DONE
    fact = only(conn, deal, "published")["payload"]
    assert fact["sha"] == pushed
    assert fact["claimed_sha"] == pushed[:7]
    assert fact["ref"] == "refs/heads/main"
    assert fact["verified_by"] == runner.PUBLISH_VERIFY


def test_publish_claim_of_an_ambiguous_sha_is_contract(tmp_path: Path) -> None:
    """Начало sha, под которое подходят два коммита, — отказ, а не «нашли».

    Молчаливое «нашлось» выбрало бы один из двух коммитов за исполнителя: он
    мог опубликовать любой из них, и движок не имеет права угадывать.
    """
    repo, origin = git_origin(tmp_path)
    prefix = ambiguous_prefix(repo, origin)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    approve(conn, spec, deal, home)
    fake = Fake(result=publish_report(prefix, published={"branch": ""}), pack=True)

    report = run_publish(conn, spec, deal, home, fake)

    assert report["outcome"] == runner.OUTCOME_FAILED
    assert report["kind"] == runner.FAIL_CONTRACT
    assert f"sha {prefix}… подходит к нескольким ссылкам" in report["reason"]
    assert "refs/tags/amb-" in report["reason"]
    assert "published" not in kinds(conn, deal)


def test_publish_claim_that_is_not_a_sha_is_refused_before_git(tmp_path: Path) -> None:
    """Мусор в `sha` — отказ до сети: у репозитория ниже даже нет origin.

    `ls-remote` на «ветке вместо коммита» либо не ответит, либо совпадёт случайно,
    и причина отказа назвала бы git, а не заявку.
    """
    repo = git_repo(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    approve(conn, spec, deal, home)
    fake = Fake(result=publish_report("release/v0.0.5", published={"branch": ""}), pack=True)

    report = run_publish(conn, spec, deal, home, fake)

    assert report["outcome"] == runner.OUTCOME_FAILED
    assert report["kind"] == runner.FAIL_CONTRACT
    assert "не sha" in report["reason"]
    assert "ls-remote" not in report["reason"]
    assert "published" not in kinds(conn, deal)


def test_publish_claim_missing_in_origin_is_contract(tmp_path: Path) -> None:
    """Заявка без подтверждения — отказ `contract`, а не факт: движок верит origin."""
    repo, _origin = git_origin(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    approve(conn, spec, deal, home)
    fake = Fake(
        result=publish_report(
            "c" * 40, published={"sha": "c" * 40, "branch": "main", "mr_url": ""}
        ),
        pack=True,
    )

    report = run_publish(conn, spec, deal, home, fake)

    assert report["outcome"] == runner.OUTCOME_FAILED
    assert report["kind"] == runner.FAIL_CONTRACT
    assert "публикация заявлена, но не найдена" in report["reason"]
    assert "ccc" in report["reason"]
    assert "published" not in kinds(conn, deal)


def test_publish_of_another_commit_is_contract(tmp_path: Path) -> None:
    """В origin нашёлся коммит, но одобряли другой — отказ `contract`, а не факт.

    Человек одобрил `HEAD` (он же база дерева), а исполнитель запихнул в origin
    старый коммит по тегу: ссылка реальна, `ls-remote` его видит — и именно
    поэтому сверка нужна не только с origin, но и с одобрением.
    """
    repo, _origin = git_origin(tmp_path)
    first = head_of(repo, "HEAD")
    (repo / "b.txt").write_text("другое\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "next"],
        check=True,
    )
    second = head_of(repo, "HEAD")
    # Метка на первый коммит делает его видимым в `ls-remote` без ветки.
    subprocess.run(["git", "-C", str(repo), "tag", "old", first], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "origin", "main", "--tags"], check=True)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    approve(conn, spec, deal, home)
    assert only(conn, deal, "question_asked")["payload"]["routing"]["approved_commit"] == second
    fake = Fake(result=publish_report(first), pack=True)

    report = run_publish(conn, spec, deal, home, fake)

    assert report["outcome"] == runner.OUTCOME_FAILED
    assert report["kind"] == runner.FAIL_CONTRACT
    assert f"опубликован commit {first[:12]}" in report["reason"]
    assert f"а одобрен {second[:12]}" in report["reason"]
    assert "published" not in kinds(conn, deal)


def test_published_event_is_not_a_state_change(tmp_path: Path) -> None:
    """`published` — факт о дереве, а не о деле: закрытость дела он не открывает."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    seed(conn, deal, "published", {"stage": "publish", "attempt": 1, "sha": "a" * 40}, key="test:pub")

    folded = journal.fold(events(conn, deal), now=NOW)

    assert folded["last_kind"] == "published"
    assert "publish" not in [str(item) for item in folded.get("stages") or {}]


def test_gate_no_closes_the_deal_without_publication(tmp_path: Path) -> None:
    """«Не публиковать» — решение, а не сбой: дело закрывается причиной «человек не дал ок».

    Приёмка карточки: ответ «не публиковать» → дело закрыто без публикации. Отказ
    гейта остаётся фактом этапа, но дело не висит открытым отказом и не ждёт
    повторного вопроса — человек сказал ровно то, что хотел.
    """
    repo, _origin = git_origin(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = publish_spec(tmp_path, repo)
    asked = runner.run_stage(conn, spec, deal, "gate", 1, home=home, clock=lambda: NOW)
    answer_gate(home, asked["question_id"], "нет")
    sync(conn, home)

    report = run_gate(conn, spec, deal, home, attempt=2)

    assert report["closed"] is True
    assert "человек не дал ок" in report["reason"]
    closed = only(conn, deal, "completed")
    assert closed["payload"]["cause"] == deals.CLOSE_CAUSE_HUMAN
    assert closed["payload"]["from"] == "wait_human/status"
    assert "человек не дал ок" in closed["payload"]["reason"]
    # Публикации не было и быть не могло: факт `published` пишет только движок
    # после `git ls-remote`, а до неё дело не дошло.
    assert "published" not in kinds(conn, deal)
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "done"


def test_publish_gate_question_carries_commit_stat_and_review(tmp_path: Path) -> None:
    """Вопрос человеку несёт то, что он иначе искал бы руками: коммит, `--stat`, ревью.

    Приёмка: «вопрос гейта содержит commit (короткий sha), --stat, ссылку на отчёт
    ревьюера и вердикт». Всё четыре — из журнала и git, поэтому проверяются на
    тексте вопроса в пуле, а не на пересказе.
    """
    repo, _origin = git_origin(tmp_path)
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = write_spec(
        tmp_path,
        f"""
        duty: probe
        repo: {repo}
        base: HEAD
        stages:
          - id: impl
            role: implementer
            workspace: none
          - id: review
            role: reviewer
            needs: [impl]
            workspace: none
            verdict: {{approve: next, changes: impl, max_rounds: 1}}
          - id: gate
            kind: gate
            needs: [review]
            ask: {{q: "Публикуем этот коммит?", options: [публиковать, не публиковать]}}
            allow: [публиковать]
          - id: publish
            role: publisher
            needs: [gate]
            workspace: worktree
        """,
    )
    report_dir = tmp_path / "review-work"
    report_dir.mkdir()
    seed(
        conn,
        deal,
        "attempt_ended",
        {
            "stage": "review",
            "attempt": 1,
            "outcome": "done",
            "kind": None,
            "reason": "дифф чистый",
            "details": {"workdir": str(report_dir)},
        },
        key="test:review:1",
    )
    base = head_of(repo, "HEAD")

    asked = run_gate(conn, spec, deal, home)

    row = pool_rows(home)[0]
    text = row["text_full"]
    assert asked["started"] is True
    assert f"одобряется commit {base[:12]}" in text
    # `--stat` называет файл и число: человек видит объём, а не «что-то изменилось».
    assert "a.txt" in text and "|" in text
    assert f"отчёт ревьюера: {report_dir / '.duty' / 'result.json'}" in text
    # Вердикт называется словами отчёта: `reason` у успешной попытки движок не пишет.
    assert "вердикт: одобрено: дифф чистый" in text
    assert "не публиковать" in text
    # Адрес вопроса несёт одобренный коммит: по нему сверяется дерево перед спавном.
    assert row["text"] == "Публикуем этот коммит?"



def test_reviewer_contract_warns_against_gitlab_crawl(tmp_path: Path) -> None:
    """Контракт review-этапа учит: локальный дифф, evidence первыми, свой started_at.

    Кейс d_c8551c18: ревьюер краулил MR через GitLab API пофайлово (410
    get_file), сжёг бюджет попытки (11.4M токенов, capability), а одна попытка
    сдала evidence с чужим started_at из прошлой попытки (evidence.stale).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: review
            role: reviewer
        """,
    )
    runner.run_stage(conn, spec, deal, "review", 1, home=home, clock=lambda: NOW, spawn=Fake(result=None, exit_code=0, lines=[]))
    contract_file = runner.contract_path(runner.attempt_dir(home, deal, "review", 1), "review", 1)
    boundaries = "\n".join(json.loads(contract_file.read_text(encoding="utf-8"))["boundaries"])
    assert "GitLab API пофайлово" in boundaries
    assert "команды evidence прогоняются первыми" in boundaries
    assert "evidence.stale" in boundaries


def test_lead_plan_contract_does_not_carry_reviewer_boundaries(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = plan_spec(tmp_path)
    run_stage(conn, spec, deal, home, Fake(result=None, exit_code=0, lines=[]))
    contract_file = runner.contract_path(runner.attempt_dir(home, deal, "plan", 1), "plan", 1)
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    # план-этап ведёт lead — правила ревьюера ему не нужны
    boundaries = "\n".join(contract["boundaries"])
    assert "GitLab API пофайлово" not in boundaries


def test_plan_contract_carries_the_rethink_diagnosis(tmp_path: Path) -> None:
    """Контракт плана после переосмысления несёт диагноз и порядок действий.

    Кейс d_c8551c18: review падал шесть раз по одной причине, а лид не видел
    диагноза. Переосмысление обязано прийти лиду с фактом: какой этап, класс,
    причина последнего отказа и что делать сначала.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = plan_spec(tmp_path)
    events = [
        {
            "ts": int(NOW),
            "recorded_at": int(NOW),
            "deal_id": str(deal["id"]),
            "kind": "plan_rethink",
            "payload": {"stage": "review", "kind": "contract", "attempt": 5, "why": "evidence.missing", "total": 9, "round": 1},
            "source": "engine",
            "idempotency_key": "probe:rethink:1",
        }
    ]
    contract = runner.plan_payload(spec, events)
    assert contract["rethink"]["why"] == "evidence.missing"
    assert contract["rethink"]["stage"] == "review"
    assert contract["rethink"]["total"] == 9
    assert "ревизия плана — лишь один из исходов" in contract["rethink"]["how"]
