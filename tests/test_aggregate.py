"""Тесты агрегатора «сейчас»: таксономия ожиданий, лизинги, таймлайн попыток.

Фикстуры — маленькие `kanban.db`/`pool.db`, собранные в `tmp_path` по реальной
схеме (колонки скопированы из живых БД, не выдуманы). Живые БД в тестах не
участвуют: агрегатор получает корень Hermes через `HERMES_HOME`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

# Фиксированное «сейчас»: все возраста в тестах считаются от него.
NOW = 1_800_000_000

# Схема скопирована с живой `kanban.db` (колонки, которыми пользуется агрегатор,
# плюс обязательные NOT NULL из настоящего DDL).
KANBAN_SCHEMA = """
CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    current_run_id       INTEGER,
    block_kind           TEXT,
    block_recurrences    INTEGER NOT NULL DEFAULT 0,
    merged_into          TEXT
);
CREATE TABLE task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    worker_started_at   INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
"""

POOL_SCHEMA = """
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  project TEXT,
  text TEXT NOT NULL,
  text_full TEXT,
  origin TEXT,
  options_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','sent','answered','expired','pickup_gateway')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  sent_at TEXT,
  answered_at TEXT,
  answer TEXT,
  timeout REAL,
  critical INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'question',
  link TEXT
);
"""


# ------------------------------------------------------------------ фикстуры


def board_db(home: Path, slug: str) -> Path:
    """Путь к БД доски в фикстурном корне Hermes (`default` — корневой kanban.db)."""
    if slug == "default":
        return home / "kanban.db"
    return home / "kanban" / "boards" / slug / "kanban.db"


def make_board(home: Path, slug: str, schema: str = KANBAN_SCHEMA) -> Path:
    """Создать БД доски по реальной схеме (пустой файл агрегатор не увидит)."""
    path = board_db(home, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return path


def make_pool(home: Path) -> Path:
    path = home / "messaging" / "pool.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()
    return path


def add_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "todo",
    assignee: str = "implementer",
    title: str | None = None,
    created_at: int = NOW - 3600,
    started_at: int | None = None,
    claim_lock: str | None = None,
    claim_expires: int | None = None,
    consecutive_failures: int = 0,
    worker_pid: int | None = None,
    last_heartbeat_at: int | None = None,
    current_run_id: int | None = None,
    block_kind: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, created_at, started_at, "
        "claim_lock, claim_expires, consecutive_failures, worker_pid, last_heartbeat_at, "
        "current_run_id, block_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            title or f"дело {task_id}",
            "тело дела",
            assignee,
            status,
            created_at,
            started_at,
            claim_lock,
            claim_expires,
            consecutive_failures,
            worker_pid,
            last_heartbeat_at,
            current_run_id,
            block_kind,
        ),
    )


def add_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str = "running",
    profile: str = "implementer",
    started_at: int = NOW - 600,
    ended_at: int | None = None,
    outcome: str | None = None,
    worker_pid: int | None = None,
    last_heartbeat_at: int | None = None,
    claim_lock: str | None = None,
    claim_expires: int | None = None,
    error: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, "
        "worker_pid, last_heartbeat_at, claim_lock, claim_expires, error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            profile,
            status,
            started_at,
            ended_at,
            outcome,
            worker_pid,
            last_heartbeat_at,
            claim_lock,
            claim_expires,
            error,
        ),
    )
    return int(cur.lastrowid or 0)


def add_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    *,
    run_id: int | None = None,
    payload: dict | str | None = None,
    created_at: int = NOW - 120,
) -> int:
    raw = payload if isinstance(payload, str) or payload is None else json.dumps(payload, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
        (task_id, run_id, kind, raw, created_at),
    )
    return int(cur.lastrowid or 0)


def add_link(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)", (parent_id, child_id))


def add_question(conn: sqlite3.Connection, *, session_key: str, status: str = "sent", origin: str = "card",
                 text: str = "вопрос по делу", project: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO questions (session_key, project, text, text_full, origin, status, kind) "
        "VALUES (?,?,?,?,?,?,'question')",
        (session_key, project, text, text, origin, status),
    )
    return int(cur.lastrowid or 0)


def absent_pid() -> int:
    """pid, которого заведомо нет: `os.kill(pid, 0)` отвечает ProcessLookupError."""
    for candidate in range(4_000_000, 3_999_000, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            return candidate
    raise AssertionError("не нашли свободный pid для теста")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes для фикстуры: агрегатор читает его через HERMES_HOME."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def deals_by_id(result: dict) -> dict[str, dict]:
    return {deal["id"]: deal for deal in result["deals"]}


# --------------------------------------------------------------------- тесты


def test_clean_board_child_waits_for_unfinished_parent(home: Path) -> None:
    """Чистая доска: попытки нет, дело стоит на незавершённом родителе (wait_gate)."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_parent", status="todo", created_at=NOW - 7200)
    add_task(conn, "t_child", status="todo", created_at=NOW - 3600)
    add_link(conn, "t_parent", "t_child")
    conn.commit()
    conn.close()

    result = plugin_api.collect_now(home, NOW)
    deals = deals_by_id(result)

    assert result["errors"] == []
    child = deals["t_child"]
    assert child["attempt"] is None
    assert child["last_action"]["kind"] is None
    assert child["waiting"]["state"] == "wait_gate"
    assert "t_parent" in child["waiting"]["evidence"]
    # Родитель без родителей — очередь диспетчера, а не гейт.
    assert deals["t_parent"]["waiting"]["state"] == "wait_slot"
    # Готовый родитель снимает гейт: ребёнок больше не ждёт графа.
    conn = sqlite3.connect(board_db(home, "stack"))
    conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id='t_parent'", (NOW,))
    conn.commit()
    conn.close()
    after = deals_by_id(plugin_api.collect_now(home, NOW))
    assert after["t_child"]["waiting"]["state"] == "wait_slot"
    assert "незавершённые родители" not in after["t_child"]["waiting"]["evidence"]


def test_running_with_dead_pid_is_dead_lease(home: Path) -> None:
    """Ран `running`, pid мёртв, лизинг ещё жив и heartbeat свежий → dead_lease, не working."""
    pid = absent_pid()
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_dead", status="running", created_at=NOW - 500)
    run_id = add_run(
        conn,
        "t_dead",
        status="running",
        worker_pid=pid,
        started_at=NOW - 400,
        last_heartbeat_at=NOW - 20,
        claim_lock="mbp-test:1",
        claim_expires=NOW + 600,
    )
    conn.execute(
        "UPDATE tasks SET worker_pid=?, current_run_id=?, claim_lock='mbp-test:1', "
        "claim_expires=?, started_at=?, last_heartbeat_at=? WHERE id='t_dead'",
        (pid, run_id, NOW + 600, NOW - 400, NOW - 20),
    )
    conn.commit()
    conn.close()

    deal = deals_by_id(plugin_api.collect_now(home, NOW))["t_dead"]

    assert deal["attempt"]["run_id"] == run_id
    assert deal["attempt"]["pid_alive"] is False
    assert deal["attempt"]["age_sec"] == 400
    assert deal["lease"]["state"] == "live"
    assert deal["waiting"]["state"] == "dead_lease"
    assert "мёртв" in deal["waiting"]["evidence"]


def test_running_with_live_pid_and_stale_heartbeat_is_crashed(home: Path) -> None:
    """pid жив, но heartbeat молчит 15 минут → воркер завис, это crashed."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_hung", status="running", created_at=NOW - 2000)
    run_id = add_run(
        conn,
        "t_hung",
        status="running",
        worker_pid=os.getpid(),
        started_at=NOW - 1800,
        last_heartbeat_at=NOW - 900,
        claim_expires=NOW + 600,
    )
    conn.execute(
        "UPDATE tasks SET worker_pid=?, current_run_id=?, claim_expires=?, last_heartbeat_at=? "
        "WHERE id='t_hung'",
        (os.getpid(), run_id, NOW + 600, NOW - 900),
    )
    conn.commit()
    conn.close()

    deal = deals_by_id(plugin_api.collect_now(home, NOW))["t_hung"]

    assert deal["attempt"]["pid_alive"] is True
    assert deal["attempt"]["heartbeat_age_sec"] == 900
    assert deal["waiting"]["state"] == "crashed"
    assert "heartbeat 900с назад" in deal["waiting"]["evidence"]


def test_consecutive_failures_is_crashed_with_evidence(home: Path) -> None:
    """Счётчик отказов 2 — падение, даже если карточка уже вернулась в очередь."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_breaker", status="ready", created_at=NOW - 5000, consecutive_failures=2)
    run_id = add_run(
        conn,
        "t_breaker",
        status="crashed",
        started_at=NOW - 900,
        ended_at=NOW - 600,
        outcome="gave_up",
        error="retries exhausted 2/2",
    )
    conn.execute("UPDATE tasks SET current_run_id=NULL, last_failure_error='retries exhausted 2/2' WHERE id='t_breaker'")
    conn.commit()
    conn.close()

    deal = deals_by_id(plugin_api.collect_now(home, NOW))["t_breaker"]

    assert deal["consecutive_failures"] == 2
    assert deal["attempt"] is None
    assert deal["last_run"]["run_id"] == run_id
    assert deal["last_run"]["outcome"] == "gave_up"
    assert deal["waiting"]["state"] == "crashed"
    assert "consecutive_failures=2" in deal["waiting"]["evidence"]
    assert "outcome=gave_up" in deal["waiting"]["evidence"]


def test_ready_card_waits_for_busy_profile(home: Path) -> None:
    """`ready` при занятом том же профиле → wait_slot, а занятое дело — working."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_busy", status="running", created_at=NOW - 700)
    run_id = add_run(
        conn,
        "t_busy",
        status="running",
        worker_pid=os.getpid(),
        started_at=NOW - 660,
        last_heartbeat_at=NOW - 30,
        claim_expires=NOW + 300,
    )
    conn.execute(
        "UPDATE tasks SET worker_pid=?, current_run_id=?, claim_expires=?, last_heartbeat_at=? "
        "WHERE id='t_busy'",
        (os.getpid(), run_id, NOW + 300, NOW - 30),
    )
    add_task(conn, "t_queued", status="ready", created_at=NOW - 400)
    add_task(conn, "t_other", status="ready", assignee="tester", created_at=NOW - 300)
    conn.commit()
    conn.close()

    deals = deals_by_id(plugin_api.collect_now(home, NOW))

    assert deals["t_busy"]["waiting"]["state"] == "working"
    assert deals["t_queued"]["waiting"]["state"] == "wait_slot"
    assert "t_busy" in deals["t_queued"]["waiting"]["evidence"]
    # Чужой профиль занят не этим делом — очередь, но другой исполнитель свободен.
    assert deals["t_other"]["waiting"]["state"] == "wait_slot"


def test_attempts_timeline_is_ordered_with_durations(home: Path) -> None:
    """Два рана + события: таймлайн по возрастанию старта, длительности посчитаны."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_two", status="ready", created_at=NOW - 5000, consecutive_failures=1)
    first = add_run(
        conn,
        "t_two",
        status="crashed",
        started_at=NOW - 4000,
        ended_at=NOW - 3400,
        outcome="crashed",
        error="worker died",
    )
    second = add_run(
        conn,
        "t_two",
        status="crashed",
        started_at=NOW - 3300,
        ended_at=NOW - 3200,
        outcome="timed_out",
        error="max runtime",
    )
    add_event(conn, "t_two", "spawned", run_id=first, payload={"pid": 111}, created_at=NOW - 3990)
    add_event(conn, "t_two", "worker_result", run_id=first, payload={"summary": "упал"}, created_at=NOW - 3401)
    add_event(conn, "t_two", "spawned", run_id=second, payload={"pid": 222}, created_at=NOW - 3290)
    add_event(conn, "t_two", "commented", payload={"author": "lead"}, created_at=NOW - 3250)
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(plugin_api.router)
    body = TestClient(app).get("/duty/stack/t_two/attempts").json()

    assert body["task_id"] == "t_two"
    assert body["count"] == 2
    first_attempt, second_attempt = body["attempts"]
    assert (first_attempt["run_id"], second_attempt["run_id"]) == (first, second)
    assert first_attempt["duration_sec"] == 600
    assert second_attempt["duration_sec"] == 100
    assert [event["kind"] for event in first_attempt["events"]] == ["spawned", "worker_result"]
    assert [event["kind"] for event in second_attempt["events"]] == ["spawned"]
    assert first_attempt["events"][1]["summary"] == "упал"
    assert first_attempt["outcome"] == "crashed"
    assert first_attempt["error"] == "worker died"
    # Событие без run_id к попыткам не приклеивается.
    assert all(event["kind"] != "commented" for event in first_attempt["events"] + second_attempt["events"])


def test_broken_board_lands_in_errors_and_route_stays_200(home: Path, client: TestClient) -> None:
    """Битая доска (нет таблицы tasks) не валит роут: 200, доска в errors, живая доска в ответе."""
    healthy = make_board(home, "stack")
    conn = sqlite3.connect(healthy)
    add_task(conn, "t_ok", status="todo", created_at=NOW - 100)
    conn.commit()
    conn.close()

    make_board(home, "broken", schema="CREATE TABLE unrelated (x INTEGER);")
    (home / "kanban" / "boards" / "gar-bage").mkdir(parents=True)
    (home / "kanban" / "boards" / "gar-bage" / "kanban.db").write_bytes(b"not a database at all")

    response = client.get("/now")

    assert response.status_code == 200
    body = response.json()
    assert [deal["id"] for deal in body["deals"]] == ["t_ok"]
    broken = {error["board"]: error["reason"] for error in body["errors"]}
    assert "broken" in broken and "tasks" in broken["broken"]
    assert "gar-bage" in broken
    assert {board["slug"]: board["read"] for board in body["boards"]} == {
        "broken": False,
        "gar-bage": False,
        "stack": True,
    }


def test_wait_human_from_pool_question(home: Path) -> None:
    """Живой вопрос пула по делу → wait_human (id карточки в session_key и в тексте)."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_askcard", status="blocked", block_kind="needs_input", created_at=NOW - 900)
    add_task(conn, "t_asktext", status="blocked", block_kind="needs_input", created_at=NOW - 800)
    conn.commit()
    conn.close()

    pool = sqlite3.connect(make_pool(home))
    question_id = add_question(pool, session_key="t_askcard", origin="card", project="stack")
    add_question(pool, session_key="agent:main:local:dm", origin="session", text="уточни t_asktext, пожалуйста")
    add_question(pool, session_key="t_askcard", origin="card", status="answered", text="старый вопрос")
    pool.commit()
    pool.close()

    deals = deals_by_id(plugin_api.collect_now(home, NOW))

    assert deals["t_askcard"]["waiting"]["state"] == "wait_human"
    assert f"#{question_id}" in deals["t_askcard"]["waiting"]["evidence"]
    assert deals["t_asktext"]["waiting"]["state"] == "wait_human"


def test_capability_block_wins_over_human_fallback(home: Path) -> None:
    """Блок с typed-причиной `capability` — отдельное состояние, а не «ждёт человека»."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_cap", status="blocked", block_kind="capability", created_at=NOW - 700)
    add_task(conn, "t_plain", status="blocked", block_kind="needs_input", created_at=NOW - 600)
    add_event(
        conn,
        "t_cap",
        "blocked",
        payload={"kind": "capability", "reason": "нет доступа к репозиторию: role-access"},
        created_at=NOW - 690,
    )
    add_event(conn, "t_plain", "blocked", payload={"kind": "needs_input", "reason": "выбери вариант"}, created_at=NOW - 590)
    conn.commit()
    conn.close()

    deals = deals_by_id(plugin_api.collect_now(home, NOW))

    assert deals["t_cap"]["waiting"]["state"] == "capability"
    assert "role-access" in deals["t_cap"]["waiting"]["evidence"]
    assert deals["t_plain"]["waiting"]["state"] == "wait_human"


def test_ended_run_with_dead_pid_is_not_dead_lease(home: Path) -> None:
    """Мёртвый pid у уже завершённого рана — не dead_lease: лизинг кончился вместе с раном."""
    pid = absent_pid()
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_done_run", status="ready", created_at=NOW - 3000, worker_pid=pid, consecutive_failures=1)
    add_run(
        conn,
        "t_done_run",
        status="released",
        worker_pid=pid,
        started_at=NOW - 2000,
        ended_at=NOW - 1500,
        outcome="crashed",
    )
    conn.commit()
    conn.close()

    deal = deals_by_id(plugin_api.collect_now(home, NOW))["t_done_run"]

    assert deal["attempt"] is None
    assert deal["waiting"]["state"] == "crashed"


def test_state_and_board_filters(home: Path, client: TestClient) -> None:
    """`?board=` и `?state=` фильтруют один и тот же детерминированный срез."""
    for slug, task_id, status in (("stack", "t_a", "todo"), ("other", "t_b", "blocked")):
        conn = sqlite3.connect(make_board(home, slug))
        add_task(conn, task_id, status=status, created_at=NOW - 100)
        conn.commit()
        conn.close()

    everything = client.get("/now").json()
    assert everything["count"] == 2
    assert [deal["id"] for deal in everything["deals"]] == ["t_b", "t_a"]  # сортировка по таксономии

    one_board = client.get("/now", params={"board": "other"}).json()
    assert [deal["id"] for deal in one_board["deals"]] == ["t_b"]

    one_state = client.get("/now", params={"state": "wait_slot"}).json()
    assert [deal["id"] for deal in one_state["deals"]] == ["t_a"]
    assert one_state["by_state"]["wait_human"] == 1

    assert client.get("/now", params={"board": "nope"}).status_code == 404
    assert client.get("/now", params={"state": "nonsense"}).status_code == 400
    assert client.get("/duty/nope/t_a/attempts").status_code == 404
    assert client.get("/duty/stack/missing/attempts").status_code == 404


def test_sorting_is_deterministic_between_polls(home: Path) -> None:
    """Повторный срез на тех же данных даёт тот же порядок и те же возраста."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_z", status="todo", created_at=NOW - 5000)
    add_task(conn, "t_a", status="todo", created_at=NOW - 5000)
    add_task(conn, "t_m", status="todo", created_at=NOW - 100)
    run_id = add_run(
        conn,
        "t_m",
        status="running",
        worker_pid=os.getpid(),
        started_at=NOW - 2000,
        last_heartbeat_at=NOW - 10,
    )
    conn.execute("UPDATE tasks SET status='running', current_run_id=?, worker_pid=? WHERE id='t_m'", (run_id, os.getpid()))
    conn.commit()
    conn.close()

    first = plugin_api.collect_now(home, NOW)
    second = plugin_api.collect_now(home, NOW)
    # Возраст одинаковый (created_at совпадает) → решает id по возрастанию.
    assert [deal["id"] for deal in first["deals"]] == ["t_m", "t_a", "t_z"]
    assert [deal["waiting"]["state"] for deal in first["deals"]] == ["working", "wait_slot", "wait_slot"]
    assert first["deals"] == second["deals"]


def test_sources_untouched_by_now(home: Path) -> None:
    """Срез «сейчас» не меняет источники: mtime и размер до/после совпадают."""
    conn = sqlite3.connect(make_board(home, "stack"))
    add_task(conn, "t_ro", status="running", created_at=NOW - 100)
    run_id = add_run(conn, "t_ro", status="running", worker_pid=os.getpid(), started_at=NOW - 60, last_heartbeat_at=NOW - 5)
    conn.execute("UPDATE tasks SET worker_pid=?, current_run_id=? WHERE id='t_ro'", (os.getpid(), run_id))
    conn.commit()
    conn.close()

    sources = [board_db(home, "stack")]
    before = [(path.stat().st_mtime_ns, path.stat().st_size) for path in sources]
    time.sleep(0.01)
    plugin_api.collect_now(home, NOW)
    after = [(path.stat().st_mtime_ns, path.stat().st_size) for path in sources]

    assert before == after
    assert not list(home.rglob("*-wal")) and not list(home.rglob("*-journal"))
