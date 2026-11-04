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

# Схема пула: `_pool_questions` читает `status`, `session_key`, `text`, `text_full`.
POOL_SCHEMA = """
CREATE TABLE questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT,
    text        TEXT,
    text_full   TEXT,
    origin      TEXT,
    status      TEXT,
    kind        TEXT
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
        values = {"session_key": "t_000001", "text": "", "text_full": "", "origin": "duty", "status": "sent", "kind": "decision"}
        values.update(row)
        conn.execute(
            "INSERT INTO questions (session_key, text, text_full, origin, status, kind) VALUES (?,?,?,?,?,?)",
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
    assert "outcome=crashed" in out
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
