"""CLI `bin/duty`: те же функции, что у роутов, и те же коды возврата.

Проверяется то, что нельзя проверить глазами при ревью и что заявлено в карточке
W3: `--json` — ровно форма роута (сверка с ответом `/now`, `/stats`, `/deal`,
`/duty/.../attempts` через `TestClient`), коды 0/1/2 взяты из того, что решил
роут, `watch` переживает деградацию источников и печатает `errors[]` строкой, а
ни одна команда ничего не пишет в чужие БД и не создаёт журнал.

Фикстуры свои и маленькие (`tmp_path` + `HERMES_HOME`): живые БД не читаются.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "dashboard" / "cli.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_cli_tests", CLI_PATH)
assert _SPEC is not None and _SPEC.loader is not None
cli = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cli
_SPEC.loader.exec_module(cli)

NOW = 1_800_000_000

# Колонки скопированы с живой `kanban.db`: те же, что читают роуты.
SCHEMA = """
CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_at           INTEGER,
    started_at           INTEGER,
    completed_at         INTEGER,
    worker_pid           INTEGER,
    current_run_id       INTEGER,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    block_kind           TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    workspace_kind       TEXT
);
CREATE TABLE task_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT NOT NULL,
    profile           TEXT,
    status            TEXT,
    outcome           TEXT,
    error             TEXT,
    summary           TEXT,
    started_at        INTEGER,
    ended_at          INTEGER,
    worker_pid        INTEGER,
    last_heartbeat_at INTEGER,
    claim_lock        TEXT,
    claim_expires     INTEGER
);
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER
);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
"""

# Схема пула: `_pool_questions` читает `status`, `session_key`, `text`, `text_full`,
# а `ask.answer_of` — текст ответа (`answer`) у запаркованного дела.
POOL_SCHEMA = """
CREATE TABLE questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT,
    text        TEXT,
    text_full   TEXT,
    origin      TEXT,
    status      TEXT,
    kind        TEXT,
    answer      TEXT
);
"""


def make_board(path: Path, tasks=(), runs=(), events=()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for task in tasks:
        row = {
            "id": "t_000001",
            "title": "дело",
            "body": "",
            "assignee": "implementer",
            "status": "ready",
            "priority": 0,
            "created_at": NOW - 3600,
            "started_at": None,
            "completed_at": None,
            "worker_pid": None,
            "current_run_id": None,
            "claim_lock": None,
            "claim_expires": None,
            "block_kind": None,
            "consecutive_failures": 0,
            "workspace_kind": "scratch",
        }
        row.update(task)
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, created_at, started_at,"
            " completed_at, worker_pid, current_run_id, claim_lock, claim_expires, block_kind,"
            " consecutive_failures, workspace_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()),
        )
    for run in runs:
        row = {
            "task_id": "t_000001",
            "profile": "implementer",
            "status": "done",
            "outcome": "completed",
            "error": None,
            "summary": None,
            "started_at": NOW - 3000,
            "ended_at": NOW - 2900,
            "worker_pid": None,
            "last_heartbeat_at": None,
            "claim_lock": None,
            "claim_expires": None,
        }
        row.update(run)
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, error, summary, started_at,"
            " ended_at, worker_pid, last_heartbeat_at, claim_lock, claim_expires)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()),
        )
    for event in events:
        row = {"task_id": "t_000001", "run_id": None, "kind": "note", "payload": json.dumps({}), "created_at": NOW - 100}
        row.update(event)
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
            tuple(row.values()),
        )
    conn.commit()
    conn.close()
    return path


def make_pool(home: Path, rows=()) -> Path:
    path = home / "messaging" / "pool.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(POOL_SCHEMA)
    for row in rows:
        values = {
            "session_key": "t_000001",
            "text": "",
            "text_full": "",
            "origin": "duty",
            "status": "sent",
            "kind": "decision",
            "answer": None,
        }
        values.update(row)
        conn.execute(
            "INSERT INTO questions (session_key, text, text_full, origin, status, kind, answer)"
            " VALUES (?,?,?,?,?,?,?)",
            tuple(values.values()),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Пустой корень Hermes с одной доской `b1` и живым (пустым) пулом."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    make_board(
        tmp_path / "kanban" / "boards" / "b1" / "kanban.db",
        tasks=[{"id": "t_aaa001", "title": "первое дело", "status": "blocked", "block_kind": "needs_input"}],
        events=[{"task_id": "t_aaa001", "kind": "blocked", "payload": json.dumps({"reason": "нужно решение"})}],
    )
    make_pool(tmp_path)
    return tmp_path


@pytest.fixture()
def client(home):
    """Роуты того же модуля, что грузит CLI: сверять формы можно буквально."""
    app = FastAPI()
    app.include_router(cli.api().router)
    return TestClient(app)


@pytest.fixture()
def frozen_now(monkeypatch):
    """Один и тот же `now` у CLI и у роута: иначе `now` в ответах не сравнить."""
    monkeypatch.setattr(cli.api(), "_now_sec", lambda: NOW)
    return NOW


def run(argv, capsys):
    """Запустить команду как из терминала: `(код, stdout, stderr)`."""
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ------------------------------------------------------------------ одна форма


def test_now_json_is_exactly_the_route_payload(home, client, frozen_now, capsys):
    code, out, _ = run(["now", "--json"], capsys)
    from_cli = json.loads(out)
    from_route = client.get("/now").json()

    assert code == 0
    assert from_cli == from_route
    assert from_cli["count"] == 1
    assert from_cli["deals"][0]["id"] == "t_aaa001"


def test_now_json_honours_board_and_state_filters(home, client, frozen_now, capsys):
    code, out, _ = run(["now", "--board", "b1", "--state", "wait_human", "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/now", params={"board": "b1", "state": "wait_human"}).json()


def test_stats_json_is_exactly_the_route_payload(home, client, frozen_now, capsys):
    code, out, _ = run(["stats", "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/stats").json()


def test_stats_json_passes_board_and_since_to_the_same_function(home, client, frozen_now, capsys):
    since = NOW - 60
    code, out, _ = run(["stats", "--board", "b1", "--since", str(since), "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/stats", params={"board": "b1", "since": since}).json()


def test_deal_json_is_exactly_the_route_payload(home, client, frozen_now, capsys):
    code, out, _ = run(["deal", "b1", "t_aaa001", "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/deal/b1/t_aaa001").json()


def test_attempts_json_is_exactly_the_route_payload(home, client, frozen_now, capsys):
    make_board(
        home / "kanban" / "boards" / "b1" / "kanban.db",
        tasks=[{"id": "t_aaa001", "title": "первое дело", "status": "review"}],
        runs=[{"task_id": "t_aaa001", "status": "awaiting_merge", "outcome": "completed", "summary": "готово"}],
    )
    code, out, _ = run(["attempts", "b1", "t_aaa001", "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/duty/b1/t_aaa001/attempts").json()


# Ключи, которые `why` добавляет к форме роута: факты журнала, которых в роуте
# нет. Их ровно два, и набор закрыт — иначе «--json = форма роута» перестанет быть
# проверяемым утверждением.
WHY_EXTRA_KEYS = ("verdict", "failures")


def test_why_json_is_the_deal_route_payload(home, client, frozen_now, capsys):
    code, out, _ = run(["why", "b1", "t_aaa001", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    route = client.get("/deal/b1/t_aaa001").json()
    assert {key: value for key, value in payload.items() if key not in WHY_EXTRA_KEYS} == route
    assert payload["failures"]["found"] is False
    assert payload["failures"]["note"].startswith("журнала ещё нет")


def test_json_flag_works_before_the_command_too(home, client, frozen_now, capsys):
    code, out, _ = run(["--json", "now"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/now").json()


# ------------------------------------------------------------------- коды 0/1/2


def test_missing_board_is_code_1(home, capsys):
    code, out, err = run(["now", "--board", "nope"], capsys)
    assert code == cli.EXIT_NOT_FOUND
    assert out == ""
    assert "not found" in err


def test_missing_task_is_code_1(home, capsys):
    for argv in (["deal", "b1", "t_zzz"], ["attempts", "b1", "t_zzz"], ["why", "b1", "t_zzz"]):
        code, _, err = run(argv, capsys)
        assert code == cli.EXIT_NOT_FOUND
        assert "not found" in err


def test_unknown_state_is_code_2(home, capsys):
    code, _, err = run(["now", "--state", "bogus"], capsys)
    assert code == cli.EXIT_SOURCE
    assert "unknown state" in err


def test_unreadable_board_is_code_2(home, capsys):
    (home / "kanban" / "boards" / "broken").mkdir()
    (home / "kanban" / "boards" / "broken" / "kanban.db").write_bytes(b"not a database")
    code, out, _ = run(["now", "--json"], capsys)
    payload = json.loads(out)

    # Ответ приходит (роут отвечает 200), но часть источников не прочитана — код 2.
    assert code == cli.EXIT_SOURCE
    assert any(error["board"] == "broken" for error in payload["errors"])
    assert payload["count"] == 1


def test_unreadable_board_on_stats_is_code_2(home, capsys):
    (home / "kanban" / "boards" / "broken").mkdir()
    (home / "kanban" / "boards" / "broken" / "kanban.db").write_bytes(b"not a database")
    code, _, _ = run(["stats", "--json"], capsys)
    assert code == cli.EXIT_SOURCE


def test_degradation_is_named_in_text_output_not_only_in_json(home, capsys):
    (home / "kanban" / "boards" / "broken").mkdir()
    (home / "kanban" / "boards" / "broken" / "kanban.db").write_bytes(b"not a database")
    code, out, _ = run(["now"], capsys)

    assert code == cli.EXIT_SOURCE
    assert "! источник broken" in out
    assert "t_aaa001" in out  # дела читаемых досок напечатаны, ответ не потерян


def test_clean_home_without_pool_reports_source_error(home, capsys):
    (home / "messaging" / "pool.db").unlink()
    code, out, _ = run(["now", "--json"], capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_SOURCE
    assert any("pool.db" in error["board"] for error in payload["errors"])


def test_since_accepts_unix_and_iso_date():
    assert cli.since_arg("1800000000") == 1800000000
    assert cli.since_arg("2027-01-15") == int(time.mktime((2027, 1, 15, 0, 0, 0, 0, 0, -1)))
    assert cli.since_arg("2027-01-15 10:30") == int(time.mktime((2027, 1, 15, 10, 30, 0, 0, 0, -1)))


def test_bad_since_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as raised:
        run(["stats", "--since", "вчера"], capsys)
    assert raised.value.code == cli.EXIT_SOURCE


# ------------------------------------------------------------------ текст команд


def test_now_prints_the_columns_from_the_card(home, frozen_now, capsys):
    code, out, _ = run(["now"], capsys)
    assert code == 0
    for header in ("ДОСКА", "ID", "ОЖИДАНИЕ", "ВОЗРАСТ", "ДОКАЗАТЕЛЬСТВО"):
        assert header in out
    assert "t_aaa001" in out
    assert "wait_human/status" in out
    assert "status=blocked" in out  # доказательство — текст бэкенда, не пересказ CLI


def test_deal_prints_facts_and_attempt_timeline(home, frozen_now, capsys):
    make_board(
        home / "kanban" / "boards" / "b1" / "kanban.db",
        tasks=[{"id": "t_aaa001", "title": "первое дело", "status": "review", "assignee": "tester"}],
        runs=[
            {"task_id": "t_aaa001", "status": "done", "outcome": "crashed", "error": "boom"},
            {"task_id": "t_aaa001", "status": "awaiting_merge", "outcome": "completed", "summary": "готово"},
        ],
        events=[{"task_id": "t_aaa001", "run_id": 2, "kind": "worker_bound", "created_at": NOW - 50}],
    )
    code, out, _ = run(["deal", "b1", "t_aaa001"], capsys)

    assert code == 0
    assert "попытка:" in out
    assert "лизинг:" in out
    assert "ПОПЫТКИ" in out
    # Исход попытки и её ошибка — из `task_runs` доски; формат строки один на оба
    # пространства дел (`W18`): исход (длительность).
    assert "crashed (1 мин 40 с)" in out
    assert "ошибка: boom" in out
    assert "итог: готово" in out
    assert "worker_bound" in out


def test_attempts_prints_only_the_timeline(home, frozen_now, capsys):
    make_board(
        home / "kanban" / "boards" / "b1" / "kanban.db",
        tasks=[{"id": "t_aaa001", "title": "первое дело", "status": "review"}],
        runs=[{"task_id": "t_aaa001", "status": "done", "outcome": "completed"}],
    )
    code, out, _ = run(["attempts", "b1", "t_aaa001"], capsys)

    assert code == 0
    assert out.startswith("ПОПЫТКИ")
    assert "лизинг:" not in out


# ------------------------------- попытки своего дела в консоли (W18)
#
# Таймлайн своего дела строится по журналу: консоль печатает этап и номер,
# исход с длительностью и причину отказа, а `base`/`commit` — коротким sha.

BASE_ONE = "8d31edee15f8b15136b4ff1def2a49709621975d"
COMMIT_ONE = "8a2d5cb6bb4a380d53fdcf77af758f0b6e71a598"


def write_three_attempts(home: Path, deal_id: str) -> None:
    """Журнал своего дела с тремя попытками — той же формой, что пишет движок."""
    conn = cli.api().deals.open_store(home)
    assert conn is not None
    try:
        cli._load_journal().append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "board": "b1",
                    "kind": kind,
                    "ts": ts,
                    "recorded_at": ts,
                    "source": "engine",
                    "payload": payload,
                    "idempotency_key": f"test:{deal_id}:{kind}:{ts}:{index}",
                }
                for index, (kind, ts, payload) in enumerate(
                    [
                        ("attempt_started", NOW - 600, {
                            "run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1,
                            "pid": 111, "base": BASE_ONE, "lease_expires": NOW - 400,
                            "contract": {"role": "implementer"},
                        }),
                        ("attempt_progress", NOW - 590, {
                            "stage": "impl", "attempt": 1, "seq": 1, "phase": "tool_use",
                        }),
                        ("attempt_progress", NOW - 580, {
                            "stage": "impl", "attempt": 1, "seq": 2, "phase": "tool_result",
                        }),
                        ("attempt_ended", NOW - 500, {
                            "run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1,
                            "outcome": "done", "kind": None, "reason": "",
                            "base": BASE_ONE, "commit": COMMIT_ONE,
                        }),
                        ("attempt_started", NOW - 400, {
                            "run_id": f"{deal_id}:review#1", "stage": "review", "attempt": 1,
                            "pid": 333, "base": COMMIT_ONE, "lease_expires": NOW - 200,
                            "contract": {"role": "reviewer"},
                        }),
                        ("attempt_ended", NOW - 300, {
                            "run_id": f"{deal_id}:review#1", "stage": "review", "attempt": 1,
                            "outcome": "failed", "kind": "contract", "reason": "result.json нет",
                            "base": COMMIT_ONE, "commit": None,
                        }),
                    ]
                )
            ],
        )
    finally:
        conn.close()


def test_attempts_json_of_own_deal_is_exactly_the_route_payload(home, client, frozen_now, capsys):
    """`--json` своего дела — дословно ответ роута, как и у kanban-дела."""
    deal_id = own_deal(home)
    write_three_attempts(home, deal_id)

    code, out, _ = run(["attempts", "b1", deal_id, "--json"], capsys)

    assert code == 0
    assert json.loads(out) == client.get(f"/duty/b1/{deal_id}/attempts").json()


def test_attempts_of_own_deal_prints_stage_outcome_reason_and_short_sha(home, frozen_now, capsys):
    """Строка попытки: этап и номер, исход (длительность), причина плюс `base`/`commit`."""
    deal_id = own_deal(home)
    write_three_attempts(home, deal_id)

    code, out, _ = run(["attempts", "b1", deal_id], capsys)

    assert code == 0
    assert out.startswith("ПОПЫТКИ")
    assert "impl#1 · implementer · done (1 мин 40 с)" in out
    assert "review#1 · reviewer · failed(contract)" in out
    assert "причина: result.json нет" in out
    assert "base 8d31ede" in out
    assert "commit 8a2d5cb" in out
    assert "событий прогресса: 2" in out


def test_attempts_of_own_deal_without_attempts_prints_the_note(home, frozen_now, capsys):
    """Пустой таймлайн своего дела называет настоящую причину, а не «спавна нет»."""
    deal_id = own_deal(home)

    code, out, _ = run(["attempts", "b1", deal_id], capsys)

    assert code == 0
    assert "попыток не было" in out
    assert "0.0.3" not in out


# -------------------- факты попытки своего дела в консоли (FIX-4 / B2)
#
# `bin/duty deal` и строка «Сейчас» — одна карточка: попытка и лизинг читаются из
# журнала и store. До FIX-4 консоль печатала «попытка: нет» прямо перед
# таймлайном этой же попытки.

def start_live_attempt(
    home: Path, deal_id: str, *, pid: int, started_at: int, expires_at: int
) -> None:
    """Живая попытка своего дела: `attempt_started` плюс строка лизинга в store."""
    api = cli.api()
    journal = cli._load_journal()
    conn = api.deals.open_store(home)
    assert conn is not None
    try:
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "board": "b1",
                    "kind": "attempt_started",
                    "ts": started_at,
                    "recorded_at": started_at,
                    "source": "engine",
                    "payload": {
                        "run_id": f"{deal_id}:impl#1",
                        "stage": "impl",
                        "attempt": 1,
                        "pid": pid,
                        "lease_expires": expires_at,
                        "contract": {"role": "implementer"},
                    },
                    "idempotency_key": f"test:{deal_id}:impl#1",
                }
            ],
        )
        conn.execute(
            "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) VALUES (?,?,?,?,?,?)",
            (deal_id, "impl", 1, pid, started_at, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_deal_of_own_deal_with_live_attempt_does_not_say_attempt_absent(home, frozen_now, capsys):
    """B2: консоль печатает ту же живую попытку, что и «Сейчас», а не «попытки нет»."""
    deal_id = own_deal(home)
    start_live_attempt(home, deal_id, pid=os.getpid(), started_at=NOW - 60, expires_at=NOW + 600)

    code, out, _ = run(["deal", "b1", deal_id], capsys)

    assert code == cli.EXIT_OK
    assert "попытка: нет" not in out
    assert "pid жив" in out
    assert "факты из journal" in out
    # Чего в карточке нет, то и не подставляется: ни `—` вместо статуса рана,
    # ни возраста heartbeat, которого журнал не пишет, ни `(—)` вместо claim_lock.
    assert "heartbeat не записан" in out
    assert "heartbeat — назад" not in out
    assert "(—)" not in out
    assert "лизинг: live, истекает через 10 мин" in out

    # Вторая поверхность того же факта: строка «Сейчас» не противоречит карточке.
    row = next(item for item in cli.api().now()["deals"] if item["id"] == deal_id)
    assert row["attempt"]["pid"] == os.getpid()
    assert row["attempt"]["pid_alive"] is True
    assert row["lease"]["state"] == "live"


def test_terminal_deal_has_no_invented_waiting_state(home, frozen_now, capsys):
    make_board(
        home / "kanban" / "boards" / "b1" / "kanban.db",
        tasks=[{"id": "t_done01", "title": "закрытое дело", "status": "done"}],
    )
    code, out, _ = run(["deal", "b1", "t_done01"], capsys)
    assert code == 0
    assert "не считается: дело закрыто" in out

    code, out, _ = run(["why", "b1", "t_done01"], capsys)
    assert code == 0
    assert "доказательство: не записано" in out


def test_why_prints_backend_evidence_verbatim(home, frozen_now, capsys):
    code, out, _ = run(["why", "b1", "t_aaa001"], capsys)
    evidence = cli.api().deal("b1", "t_aaa001")["deal"]["waiting"]["evidence"]

    assert code == 0
    assert evidence in out  # доказательство — слова бэкенда, CLI его не переписывает
    assert "факты, из которых оно собрано:" in out


def seed_attempt(home: Path, *, deal_id: str = "t_aaa001", stage: str = "impl", attempt: int = 1) -> None:
    """Факты попытки в журнал плагина: `why <этап>` читает именно их."""
    journal = cli._load_journal()
    conn = journal.open_journal(home)
    assert conn is not None
    base = {
        "deal_id": deal_id,
        "board": "b1",
        "source": "engine",
    }
    event = {
        **base,
        "ts": NOW - 600,
        "recorded_at": NOW - 600,
        "kind": "attempt_started",
        "payload": {"stage": stage, "attempt": attempt, "pid": 999, "run_id": f"{stage}#{attempt}"},
        "idempotency_key": f"test:{deal_id}:{stage}:{attempt}:started",
    }
    journal.append(conn, [event])
    ended = {
        **base,
        "ts": NOW - 540,
        "recorded_at": NOW - 540,
        "kind": "attempt_ended",
        "payload": {
            "stage": stage,
            "attempt": attempt,
            "run_id": f"{stage}#{attempt}",
            "outcome": "failed",
            "kind": "contract",
            "reason": "result.json:evidence[0].exit: evidence.exit: exit=1, а объявлен must=0",
            "details": {
                "workdir": str(home / "kanban" / "boards" / "b1" / "workspaces" / f"{deal_id}-{stage}-{attempt}"),
                "verdict": {
                    "ok": False,
                    "kind": "contract",
                    "details": [
                        "result.json:evidence[0].exit: evidence.exit: exit=1, а объявлен must=0",
                        "result.json:artifacts[0].sha256: artifact.sha256: хеш не тот",
                    ],
                },
            },
        },
        "idempotency_key": f"test:{deal_id}:{stage}:{attempt}:ended",
    }
    journal.append(conn, [ended])
    conn.close()


def test_why_with_stage_prints_last_attempt_and_verdict(home, frozen_now, capsys):
    seed_attempt(home)
    code, out, _ = run(["why", "b1", "t_aaa001", "impl"], capsys)

    assert code == 0
    assert "последняя попытка этапа impl:" in out
    assert "попытка #1" in out
    assert "outcome=failed" in out and "kind=contract" in out
    assert "причина: result.json:evidence[0].exit" in out
    assert "отказы гейта (2):" in out
    assert "evidence.exit" in out and "artifact.sha256" in out


def test_why_with_stage_reports_the_last_one_when_there_were_many(home, frozen_now, capsys):
    seed_attempt(home, attempt=1)
    seed_attempt(home, attempt=2)
    code, out, _ = run(["why", "b1", "t_aaa001", "impl"], capsys)
    assert code == 0
    assert "попытка #2" in out


def test_why_with_stage_says_when_attempts_are_missing(home, frozen_now, capsys):
    code, out, _ = run(["why", "b1", "t_aaa001", "impl"], capsys)
    assert code == 0
    assert "последняя попытка этапа impl:" in out
    assert "журнала ещё нет" in out


def test_why_json_with_stage_adds_verdict_without_touching_the_route_form(home, client, frozen_now, capsys):
    seed_attempt(home)
    code, out, _ = run(["why", "b1", "t_aaa001", "impl", "--json"], capsys)
    payload = json.loads(out)

    assert code == 0
    route = client.get("/deal/b1/t_aaa001").json()
    assert {key: value for key, value in payload.items() if key not in WHY_EXTRA_KEYS} == route
    assert payload["verdict"]["attempt"] == 1
    assert payload["verdict"]["kind"] == "contract"
    assert len(payload["verdict"]["details"]) == 2
    # Факты отказов едут отдельным ключом: класс, счётчик и `retry_at` считает
    # политика, а не терминал.
    assert payload["failures"]["found"] is True
    assert payload["failures"]["items"][-1]["kind"] == "contract"
    assert payload["failures"]["items"][-1]["counter"] == "1/1 contract"


def test_stats_prints_percentiles_and_first_try(home, frozen_now, capsys):
    make_board(
        home / "kanban" / "boards" / "b1" / "kanban.db",
        tasks=[
            {"id": "t_aaa001", "title": "раз", "status": "done", "created_at": NOW - 100},
            {"id": "t_bbb002", "title": "два", "status": "done", "created_at": NOW - 200},
        ],
        runs=[
            {"task_id": "t_aaa001", "status": "done", "outcome": "completed", "started_at": NOW - 90},
            {"task_id": "t_bbb002", "status": "done", "outcome": "crashed", "started_at": NOW - 190},
        ],
    )
    code, out, _ = run(["stats"], capsys)

    assert code == 0
    assert "p50" in out and "p90" in out
    assert "с первой попытки: 1/2 (50%)" in out
    assert "crashed 1" in out


def test_stats_since_counts_only_deals_created_after_it(home, frozen_now, capsys):
    make_board(
        home / "kanban" / "boards" / "b1" / "kanban.db",
        tasks=[
            {"id": "t_old001", "title": "старое", "status": "done", "created_at": NOW - 10_000},
            {"id": "t_new001", "title": "новое", "status": "done", "created_at": NOW - 100},
        ],
        runs=[
            {"task_id": "t_old001", "status": "done", "outcome": "completed", "started_at": NOW - 9_000},
            {"task_id": "t_new001", "status": "done", "outcome": "crashed", "started_at": NOW - 90},
        ],
    )
    full = cli.api().stats()
    windowed = cli.api().stats(since=NOW - 1000)

    assert full["totals"]["tasks_total"] == 2
    assert windowed["totals"]["tasks_total"] == 1
    assert windowed["totals"]["runs_by_outcome"] == {"crashed": 1}
    assert windowed["totals"]["first_try"] == {"ok": 0, "with_runs": 1, "share": 0.0}
    # Ожидание пересчитано по окну (10 с), а не взято из полного среза (10 и 1000 с):
    # p90 полной выборки — 1000 с, у окна — 10 с.
    assert full["totals"]["waiting_sec"]["p90"] == 1000
    assert windowed["totals"]["waiting_sec"]["p90"] == 10
    assert windowed["totals"]["waiting_sec"]["count"] == 1


# ----------------------------------------------------------------------- watch


def test_watch_redraws_every_tick(home, frozen_now):
    stream = io.StringIO()
    code = cli.run_watch(interval=0, ticks=2, stream=stream)
    body = stream.getvalue()

    assert code == cli.EXIT_OK
    assert body.count("ДЕЛА СЕЙЧАС") == 2
    assert "t_aaa001" in body


def test_watch_survives_source_degradation(home, frozen_now):
    (home / "kanban" / "boards" / "broken").mkdir()
    (home / "kanban" / "boards" / "broken" / "kanban.db").write_bytes(b"not a database")
    stream = io.StringIO()

    code = cli.run_watch(interval=0, ticks=2, stream=stream)
    body = stream.getvalue()

    # Деградация не роняет тик: причина строкой, дела остальных досок напечатаны.
    assert code == cli.EXIT_OK
    assert body.count("ДЕЛА СЕЙЧАС") == 2
    assert "! источник broken" in body
    assert "t_aaa001" in body


def test_watch_of_missing_board_exits_1_instead_of_looping(home, capsys):
    # `ticks` — страховка теста: если сопоставление 404 сломается, прогон кончится
    # тремя тиками с кодом 0, а не зависанием.
    code = cli.run_watch(interval=0, board="nope", ticks=3, stream=io.StringIO())
    assert code == cli.EXIT_NOT_FOUND


def test_watch_with_bad_arguments_fails_fast_instead_of_looping(home):
    stream = io.StringIO()
    code = cli.run_watch(interval=0, state="bogus", ticks=3, stream=stream)
    assert code == cli.EXIT_SOURCE
    assert "unknown state" in stream.getvalue()


def test_watch_keeps_ticking_after_a_failed_tick(home, frozen_now, monkeypatch):
    """Отказ посреди работы — строка и следующий тик, а не выход."""
    route = cli.api().now
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise cli.SourceFailure("journal: недоступен")
        return route(**kwargs)

    monkeypatch.setattr(cli.api(), "now", flaky)
    stream = io.StringIO()
    code = cli.run_watch(interval=0, ticks=3, stream=stream)
    body = stream.getvalue()

    assert code == cli.EXIT_OK
    assert calls["n"] == 3
    assert "! источники: journal: недоступен" in body
    assert body.count("ДЕЛА СЕЙЧАС") == 2  # первый и третий тик напечатаны


def test_watch_json_is_one_route_payload_per_tick(home, client, frozen_now):
    stream = io.StringIO()
    code = cli.run_watch(interval=0, ticks=2, as_json=True, stream=stream)
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]

    assert code == cli.EXIT_OK
    assert len(lines) == 2
    assert json.loads(lines[0]) == json.loads(lines[1]) == client.get("/now").json()


def test_watch_json_keeps_stdout_parsable_on_source_degradation(home, frozen_now, monkeypatch, capsys):
    """`watch --json` при отказе источника: stdout — только JSON, причина в stderr.

    Построчный потребитель (`duty --json watch | jq -c`) не должен получать из
    stdout строку `! источники: …`: в json-режиме деградация идёт в stderr, а
    stdout остаётся потоком ответов роута.
    """
    route = cli.api().now
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise cli.SourceFailure("journal: недоступен")
        return route(**kwargs)

    monkeypatch.setattr(cli.api(), "now", flaky)
    stream = io.StringIO()
    code = cli.run_watch(interval=0, ticks=3, as_json=True, stream=stream)

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert code == cli.EXIT_OK
    assert calls["n"] == 3
    assert len(lines) == 2
    for line in lines:
        json.loads(line)
    assert "! источники: journal: недоступен" in capsys.readouterr().err


# ---------------------------------------------------------- чтение без записи


def test_no_command_writes_to_foreign_databases(home, frozen_now, monkeypatch):
    board = home / "kanban" / "boards" / "b1" / "kanban.db"
    pool = home / "messaging" / "pool.db"
    board_before = board.stat().st_mtime_ns
    pool_before = pool.stat().st_mtime_ns
    journal = home / "duty" / "duty.db"

    for argv in (
        ["now"],
        ["now", "--json"],
        ["deal", "b1", "t_aaa001"],
        ["attempts", "b1", "t_aaa001"],
        ["why", "b1", "t_aaa001"],
        ["stats"],
    ):
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        cli.main(argv)

    assert board.stat().st_mtime_ns == board_before
    assert pool.stat().st_mtime_ns == pool_before
    assert not journal.exists()  # CLI не «включает» журнал как побочный эффект


def test_help_without_command_is_not_an_error(capsys):
    code, out, _ = run([], capsys)
    assert code == cli.EXIT_OK
    assert "usage: duty" in out


# --------------------------------------------- решение человека: close и decide
#
# Запаркованное дело ждёт **решения**, а не ответа: ответ приходит текстом
# (`question_answered`), а `continue|stop` — командой. Эти тесты держат обе
# половины: закрытие дела (`close`) и одну команду на оба решения (`decide`),
# плюс границу «текст ответа — не сигнал» (`docs/RUNBOOK.md`).

ANSWER = "stop: прогон закрыт координатором — это E2E-дело, продолжать не нужно"


def own_deal(home: Path, *, title: str = "своё дело") -> str:
    """Своё дело в store (`d_<hex>`): у него есть журнал и переходы."""
    payload = cli.api().deals_create(home, {"board": "b1", "title": title}, source="cli")
    return str(payload["id"])


def park_deal(home: Path, deal_id: str, *, question_id: int = 293, answer: str = ANSWER) -> None:
    """Запарковать дело как движок: вопрос, парковка и ответ человека в пуле.

    Порядок фактов — журнальный: вопрос, парковка, ответ. Текст ответа живёт
    только в пуле (`questions.answer`), в журнале остаётся `answer_len` — ровно
    так их пишут `ask()` и `sync_answers`.
    """
    api = cli.api()
    journal = cli._load_journal()
    conn = api.deals.open_store(home)
    assert conn is not None
    try:
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "board": "b1",
                    "kind": "question_asked",
                    "ts": NOW - 30,
                    "recorded_at": NOW - 30,
                    "source": "ask",
                    "payload": {
                        "question_id": question_id,
                        "session_key": f"t_{deal_id}",
                        "routing": {"target": "human", "reason": "risky", "stage": "verify", "attempt": 1},
                    },
                    "idempotency_key": f"test:{deal_id}:asked:{question_id}",
                }
            ],
        )
        api.deals.set_blocked(
            conn,
            deal_id,
            kind="parked",
            reason="Попытки исчерпаны: 1/1 contract на этапе verify",
            author="engine",
            source="engine",
            details={
                "kind": "contract",
                "stage": "verify",
                "attempts": 1,
                "limit": 1,
                "question_id": question_id,
                "park_reason": "retries_exhausted",
            },
        )
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "board": "b1",
                    "kind": "question_answered",
                    "ts": NOW - 10,
                    "recorded_at": NOW - 10,
                    "source": "ask",
                    "payload": {"question_id": question_id, "answer_len": len(answer), "answered_via": "manual"},
                    "idempotency_key": f"test:{deal_id}:answered:{question_id}",
                }
            ],
        )
    finally:
        conn.close()
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        pool.execute(
            "INSERT INTO questions (id, session_key, text, origin, status, kind, answer)"
            " VALUES (?,?,?,'duty','answered','question',?)",
            (question_id, f"t_{deal_id}", "вопрос по делу", answer),
        )
        pool.commit()
    finally:
        pool.close()


def journal_events(home: Path, deal_id: str) -> list[dict]:
    """События журнала дела — то, против чего сверяются тесты мутаций."""
    journal = cli._load_journal()
    conn = journal.open_journal(home, create=False)
    assert conn is not None
    try:
        return journal.read_events(conn, deal_id)
    finally:
        conn.close()


def deal_status(home: Path, deal_id: str) -> str:
    """Статус своего дела из store — намерение жизненного цикла."""
    api = cli.api()
    conn = api.deals.open_store(home, create=False, readonly=True)
    assert conn is not None
    try:
        row = api.deals.deal_row(conn, deal_id)
        assert row is not None
        return str(row["status"])
    finally:
        conn.close()


def test_close_writes_completed_with_human_cause(home, frozen_now, capsys):
    """`close` пишет один `completed` с причиной человека и закрывает дело."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, out, _ = run(["close", deal_id, "--reason", "прогон закрыт координатором"], capsys)

    assert code == cli.EXIT_OK
    assert "ЗАКРЫТО" in out and "done" in out
    completed = [event for event in journal_events(home, deal_id) if event["kind"] == "completed"]
    assert len(completed) == 1
    payload = cli._load_journal().payload_of(completed[0])
    assert payload["cause"] == "closed_by_human"
    assert payload["reason"] == "прогон закрыт координатором"
    assert payload["stage"] == "verify"
    assert payload["from"] == "wait_human/decision"
    assert deal_status(home, deal_id) == "done"
    # Закрытое дело больше не в ожидающих: журнал говорит `completed`.
    assert deal_id not in [item["id"] for item in cli.api().now()["deals"]]


def test_close_is_idempotent(home, frozen_now, capsys):
    """Повторный `close` с теми же словами не пишет второй факт."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    first, _, _ = run(["close", deal_id, "--reason", "закрыто"], capsys)
    code, out, _ = run(["close", deal_id, "--reason", "закрыто"], capsys)

    assert (first, code) == (cli.EXIT_OK, cli.EXIT_OK)
    assert "событий записано: 0" in out and "повтор" in out
    kinds = [event["kind"] for event in journal_events(home, deal_id)]
    assert kinds.count("completed") == 1


def test_close_refuses_to_rewrite_another_close(home, frozen_now, capsys):
    """Закрытое другим фактом дело не перезакрывается молча."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)
    run(["close", deal_id, "--reason", "закрыто"], capsys)

    code, _, err = run(["close", deal_id, "--reason", "другие слова"], capsys)

    assert code == cli.EXIT_SOURCE
    assert "уже закрыто" in err
    assert [e["kind"] for e in journal_events(home, deal_id)].count("completed") == 1


def test_decide_stop_closes_with_stopped_cause_and_the_answer_as_reason(home, frozen_now, capsys):
    """`decide stop` без `--reason` закрывает дело словами самого человека."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, out, _ = run(["decide", deal_id, "stop"], capsys)

    assert code == cli.EXIT_OK
    assert "ОСТАНОВЛЕНО" in out
    completed = [event for event in journal_events(home, deal_id) if event["kind"] == "completed"]
    assert len(completed) == 1
    payload = cli._load_journal().payload_of(completed[0])
    assert payload["cause"] == "stopped_by_human"
    assert payload["reason"] == ANSWER  # ответ человека, а не выдуманная причина
    assert deal_status(home, deal_id) == "done"


def test_decide_continue_unparks_the_deal(home, frozen_now, capsys):
    """`decide continue` пишет тот же факт, что `unblock`: парковка снята."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, out, _ = run(["decide", deal_id, "continue"], capsys)

    assert code == cli.EXIT_OK
    assert "ПРОДОЛЖЕНО" in out
    events = journal_events(home, deal_id)
    last = cli._load_journal().payload_of(events[-1])
    assert events[-1]["kind"] == "unblocked"
    assert last["previous_kind"] == "parked"
    assert deal_status(home, deal_id) in ("ready", "todo")
    assert [e["kind"] for e in events].count("completed") == 0


def test_decide_on_a_deal_nobody_asked_about_refuses_without_writing(home, frozen_now, capsys):
    """Дело без парковки и без блока — отказ входа, а не тихая запись."""
    deal_id = own_deal(home)
    before = [event["kind"] for event in journal_events(home, deal_id)]

    code, out, err = run(["decide", deal_id, "stop"], capsys)

    assert code == cli.EXIT_SOURCE
    assert out == ""
    assert "не запарковано" in err and "status=ready" in err
    assert [event["kind"] for event in journal_events(home, deal_id)] == before
    assert deal_status(home, deal_id) == "ready"


def test_decide_rejects_an_unknown_decision_word(home, frozen_now, capsys):
    """Решений ровно два: иначе слово не решение, а опечатка."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, _, err = run(["decide", deal_id, "maybe"], capsys)

    assert code == cli.EXIT_SOURCE
    assert "неизвестно" in err and "continue" in err and "stop" in err
    assert [e["kind"] for e in journal_events(home, deal_id)].count("completed") == 0


def test_answer_text_is_not_a_command(home, frozen_now, capsys):
    """`stop: …` в ответе пула — текст, а не сигнал: дело остаётся запаркованным."""
    deal_id = own_deal(home)
    park_deal(home, deal_id, answer="stop: закрываем, продолжать не нужно")

    payload = cli.api().collect_now(home, NOW, ingest=True)
    shown = next(item for item in payload["deals"] if item["id"] == deal_id)

    assert shown["waiting"]["state"] == "wait_human"
    assert shown["waiting"]["human_kind"] == "decision"
    assert [e["kind"] for e in journal_events(home, deal_id)].count("completed") == 0
    # Та же граница и во вторую сторону: `continue: …` тоже ничего не поднимает.
    park_deal(home, own_deal(home), question_id=294, answer="continue: давай дальше")
    assert len([item for item in cli.api().now()["deals"] if item["waiting"]["human_kind"] == "decision"]) == 2


def test_evidence_names_the_answer_and_the_decision_command(home, frozen_now, capsys):
    """Доказательство говорит, что ответ есть и чего не хватает: команды."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, out, _ = run(["why", "b1", deal_id], capsys)
    evidence = next(
        item for item in cli.api().now()["deals"] if item["id"] == deal_id
    )["waiting"]["evidence"]

    assert code == cli.EXIT_OK
    assert "ответ #293 получен: stop: прогон закрыт координатором" in evidence
    assert f"нужно решение: bin/duty decide {deal_id} continue|stop" in evidence
    assert evidence in out  # CLI не переписывает текст бэкенда
    # В таблице «сейчас» доказательство обрезано по ширине колонки, но состояние
    # названо целиком: `wait_human/decision` — это и есть «ответ есть, решение нет».
    code, out, _ = run(["now"], capsys)
    assert code == cli.EXIT_OK
    assert "wait_human/decision" in out


# ------------------- W21: `decide stop` на бегущем деле без живого драйвера
#
# Живой случай: попытка погибла при рестарте гейтвея, драйвера нет, а лизинг
# живёт ещё 40 минут — остановить было нечем, `decide stop` отказывал «дело
# решения человека не ждёт». Правило: `running` без живого лизинга останавливать
# можно (и факт этот пишется), живой лизинг — по-прежнему отказ и ни одной записи.

def running_deal(home: Path, *, lease_expires: Optional[int] = None) -> str:
    """Дело в работе, как его оставляет шаг движка: `claimed` плюс попытка.

    `lease_expires` задан — в store лежит строка лизинга с этим сроком (живая или
    протухшая, это решает тест); не задан — лизинга нет вовсе, то есть драйвера
    тоже нет. pid в попытке — свой: «процесс попытки жив, а драйвера нет» — ровно
    тот живой случай из карточки (гейтвей перезапустили, лизинг остался).
    """
    deal_id = own_deal(home)
    api = cli.api()
    conn = api.deals.open_store(home)
    assert conn is not None
    try:
        api.deals.claim_deal(conn, deal_id, owner="duty", source="engine", now=NOW)
        cli._load_journal().append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "board": "b1",
                    "kind": "attempt_started",
                    "ts": NOW - 60,
                    "recorded_at": NOW - 60,
                    "source": "engine",
                    "payload": {
                        "run_id": f"{deal_id}:impl#1",
                        "stage": "impl",
                        "attempt": 1,
                        "pid": os.getpid(),
                        "lease_expires": lease_expires,
                        "contract": {"role": "implementer"},
                    },
                    "idempotency_key": f"test:{deal_id}:impl#1",
                }
            ],
        )
        if lease_expires is not None:
            conn.execute(
                "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (deal_id, "impl", 1, os.getpid(), NOW - 60, lease_expires),
            )
            conn.commit()
    finally:
        conn.close()
    return deal_id


@pytest.fixture()
def frozen_deals_now(frozen_now, monkeypatch):
    """Часы store заморожены: срок лизинга в тесте — `NOW`, а не живые часы.

    `frozen_now` замораживает только часы роутов, а `deals._now_sec` читает
    настоящий момент: лизинг «истёк 10 секунд назад» по часам теста оказался бы
    живым ещё на годы, и правило «нет живого драйвера» проверить было бы нельзя.
    """
    monkeypatch.setattr(cli.api().deals, "_now_sec", lambda: NOW)
    return NOW


def leases_of(home: Path, deal_id: str) -> list[dict]:
    """Строки лизинга дела — единственный признак «кто-то работает»."""
    api = cli.api()
    conn = api.deals.open_store(home, create=False, readonly=True)
    assert conn is not None
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT stage, pid, expires_at FROM leases WHERE deal_id = ?", (deal_id,)
            )
        ]
    finally:
        conn.close()


def test_decide_stop_closes_a_running_deal_without_a_driver(home, frozen_deals_now, capsys):
    """Бегущее дело без лизинга: остановка разрешена и пишется фактом."""
    deal_id = running_deal(home)

    code, out, _ = run(["decide", deal_id, "stop", "--reason", "попытка погибла"], capsys)

    assert code == cli.EXIT_OK
    assert "ОСТАНОВЛЕНО" in out and "done" in out
    completed = [event for event in journal_events(home, deal_id) if event["kind"] == "completed"]
    assert len(completed) == 1
    payload = cli._load_journal().payload_of(completed[0])
    assert payload["cause"] == "stopped_by_human"
    assert payload["reason"] == "попытка погибла"
    # `from` — состояние **журнала** на момент закрытия (`working`: попытка идёт),
    # а не слово строки store: причина закрытия не пересказывает статус.
    assert payload["from"] == "working"
    assert deal_status(home, deal_id) == "done"
    assert leases_of(home, deal_id) == []
    # Закрытое дело уходит из «сейчас»: журнал и срез согласны.
    assert deal_id not in [item["id"] for item in cli.api().now()["deals"]]


def test_decide_stop_refuses_a_running_deal_held_by_a_live_lease(home, frozen_deals_now, capsys):
    """Живой лизинг: отказ и **ни одной записи** — чужую работу не рвём."""
    deal_id = running_deal(home, lease_expires=NOW + 600)
    before = [event["kind"] for event in journal_events(home, deal_id)]

    code, out, err = run(["decide", deal_id, "stop"], capsys)

    assert code == cli.EXIT_SOURCE
    assert out == ""
    assert "живой лизинг" in err and "impl" in err
    assert [event["kind"] for event in journal_events(home, deal_id)] == before
    assert deal_status(home, deal_id) == "running"
    assert leases_of(home, deal_id), "живой лизинг не снимается никогда"


def test_decide_stop_after_the_lease_expired_is_allowed(home, frozen_deals_now, capsys):
    """Срок лизинга прошёл — драйвера нет, остановка разрешена и лизинг снят."""
    deal_id = running_deal(home, lease_expires=NOW - 10)

    code, out, _ = run(["decide", deal_id, "stop", "--reason", "гейтвей перезапустили"], capsys)

    assert code == cli.EXIT_OK
    assert "ОСТАНОВЛЕНО" in out
    assert deal_status(home, deal_id) == "done"
    assert leases_of(home, deal_id) == [], "протухший лизинг снимается тем же фактом"
    completed = [event for event in journal_events(home, deal_id) if event["kind"] == "completed"]
    assert len(completed) == 1
    assert cli._load_journal().payload_of(completed[0])["cause"] == "stopped_by_human"


def test_decide_continue_on_a_running_deal_still_refuses(home, frozen_deals_now, capsys):
    """Исключение ровно одно: `continue` бегущему делу нечего продолжать."""
    deal_id = running_deal(home)
    before = [event["kind"] for event in journal_events(home, deal_id)]

    code, out, err = run(["decide", deal_id, "continue"], capsys)

    assert code == cli.EXIT_SOURCE
    assert out == ""
    assert "status=running" in err
    assert [event["kind"] for event in journal_events(home, deal_id)] == before
    assert deal_status(home, deal_id) == "running"


# ------------- «шага движка не было» над таймлайном (FIX-6 / остаток B1)
#
# Своё дело с попытками, запаркованное и разблокированное человеком, живого факта
# не имеет — и `bin/duty deal` печатал «шага движка ещё не было» прямо над
# таймлайном этих попыток. Теперь доказательство называет то, что подтверждено:
# идущей попытки нет, следующего шага не было. Текст — один на три поверхности.


def test_deal_of_unblocked_own_deal_does_not_contradict_its_attempt_timeline(
    home, frozen_now, capsys
):
    """FIX-6: «доказательство:» не отрицает шаги, которые напечатаны ниже."""
    deal_id = own_deal(home)
    write_three_attempts(home, deal_id)

    code, _, _ = run(["block", deal_id, "исчерпаны попытки: 2/2", "--kind", "parked"], capsys)
    assert code == cli.EXIT_OK
    code, _, _ = run(["unblock", deal_id], capsys)
    assert code == cli.EXIT_OK
    assert deal_status(home, deal_id) in ("ready", "todo")

    code, out, _ = run(["deal", "b1", deal_id], capsys)

    assert code == cli.EXIT_OK
    evidence = next(line for line in out.splitlines() if line.startswith("доказательство:"))
    assert "шага движка ещё не было" not in evidence
    assert "идущей попытки нет" in evidence
    assert "попыток в журнале: 2" in evidence
    # Таймлайн ниже — те же шаги: строка доказательства и он больше не спорят.
    assert "impl#1" in out and "review#1" in out

    # Тот же факт на остальных поверхностях: `/now` и `why` называют то же
    # состояние ожидания и ту же строку доказательства.
    row = next(item for item in cli.api().now()["deals"] if item["id"] == deal_id)
    assert row["waiting"]["state"] == "wait_slot"
    assert row["waiting"]["evidence"].endswith(evidence.split("доказательство: ", 1)[1])

    code, why_out, _ = run(["why", "b1", deal_id], capsys)
    assert code == cli.EXIT_OK
    assert row["waiting"]["evidence"] in why_out
    assert "состояние ожидания: wait_slot" in why_out
