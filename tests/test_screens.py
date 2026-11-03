"""Контракт экранов 0.0.2: `/deal/{board}/{id}`, `/stats` и веб-сборка.

Экраны «Дело» и «Здоровье» считаются по тем же read-only источникам, что и
`/now`, поэтому фикстуры здесь свои, маленькие: `tmp_path` + `HERMES_HOME`,
живые БД не читаются.

Проверяется не рендер, а то, что нельзя проверить глазами при ревью:
медиана считается по объединённой выборке (а не средним медиан), «с первой
попытки» — по первому по времени рану, обрезанный срез помечен, недоступная
доска не роняет ответ и не попадает в итог, а завершённое дело не получает
придуманного состояния ожидания.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_screens", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

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


def make_board(path: Path, tasks=(), runs=(), events=()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def test_deal_returns_active_facts_and_body(client, tmp_path):
    make_board(
        tmp_path / "kanban" / "boards" / "b1" / "kanban.db",
        tasks=[{"id": "t_aaa001", "status": "blocked", "block_kind": "needs_input", "body": "спека дела"}],
        events=[{"task_id": "t_aaa001", "kind": "blocked", "payload": json.dumps({"reason": "нужно решение"})}],
    )
    body = client.get("/deal/b1/t_aaa001").json()

    assert body["board"] == "b1"
    assert body["id"] == "t_aaa001"
    assert body["terminal"] is False
    assert body["body"] == "спека дела"
    assert body["body_truncated"] is False
    # факты — тот же набор, что в /now: состояние ожидания посчитано бэкендом
    assert body["deal"] is not None
    assert body["deal"]["waiting"]["state"] == "wait_human"
    assert "блок" in body["deal"]["waiting"]["evidence"].lower() or body["deal"]["waiting"]["evidence"]
    assert body["deal"]["last_action"]["kind"] == "blocked"


def test_deal_of_finished_task_has_no_invented_state(client, tmp_path):
    make_board(tmp_path / "kanban.db", tasks=[{"id": "t_bbb002", "status": "done", "body": "x"}])
    body = client.get("/deal/default/t_bbb002").json()

    assert body["terminal"] is True
    assert body["deal"] is None  # состояния ожидания у завершённого дела нет


def test_deal_unknown_task_and_board(client, tmp_path):
    make_board(tmp_path / "kanban.db", tasks=[{"id": "t_ccc003", "status": "ready"}])
    assert client.get("/deal/default/t_zzz999").status_code == 404
    assert client.get("/deal/nope/t_ccc003").status_code == 404


def test_deal_body_is_capped(client, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_api, "DEAL_BODY_LIMIT", 10)
    make_board(tmp_path / "kanban.db", tasks=[{"id": "t_ddd004", "status": "ready", "body": "0123456789XYZ"}])
    body = client.get("/deal/default/t_ddd004").json()

    assert body["body"] == "0123456789"
    assert body["body_truncated"] is True


def test_stats_first_try_and_median_wait(client, tmp_path):
    # Три дела: первая попытка успешна у двух, у третьего — падение; ожидание
    # до первой попытки 100/300/800 с → медиана 300.
    make_board(
        tmp_path / "kanban.db",
        tasks=[
            {"id": "t_s1", "status": "done", "created_at": NOW - 1000},
            {"id": "t_s2", "status": "done", "created_at": NOW - 1000},
            {"id": "t_s3", "status": "done", "created_at": NOW - 1000},
        ],
        runs=[
            {"task_id": "t_s1", "outcome": "completed", "started_at": NOW - 900},
            {"task_id": "t_s2", "outcome": "completed", "started_at": NOW - 700},
            {"task_id": "t_s3", "outcome": "crashed", "started_at": NOW - 200},
            {"task_id": "t_s3", "outcome": "completed", "started_at": NOW - 100},
        ],
    )
    stats = client.get("/stats").json()
    totals = stats["totals"]

    assert totals["tasks_total"] == 3
    assert totals["runs_total"] == 4
    assert totals["first_try"] == {"ok": 2, "with_runs": 3, "share": pytest.approx(2 / 3)}
    assert totals["wait"]["median_sec"] == 300
    assert totals["wait"]["samples"] == 3
    assert totals["runs_by_outcome"] == {"completed": 3, "crashed": 1}
    assert "completed" in stats["success_outcomes"]
    assert "crashed" in stats["crash_outcomes"]


def test_stats_median_is_over_pooled_sample(client, tmp_path):
    # Доска A: ожидания 0 и 0 (медиана 0). Доска B: 1000 с. Медиана медиан дала
    # бы 0 или 1000 в зависимости от способа, медиана объединённой выборки — 0,
    # а вот доля считается по сумме дел. Проверяем именно объединение: 0,0,1000.
    make_board(
        tmp_path / "kanban" / "boards" / "a" / "kanban.db",
        tasks=[{"id": "t_a1", "status": "done", "created_at": NOW - 100}, {"id": "t_a2", "status": "done", "created_at": NOW - 100}],
        runs=[
            {"task_id": "t_a1", "started_at": NOW - 100},
            {"task_id": "t_a2", "started_at": NOW - 100},
        ],
    )
    make_board(
        tmp_path / "kanban" / "boards" / "b" / "kanban.db",
        tasks=[{"id": "t_b1", "status": "done", "created_at": NOW - 1100}],
        runs=[{"task_id": "t_b1", "started_at": NOW - 100}],
    )
    totals = client.get("/stats").json()["totals"]

    assert totals["wait"]["samples"] == 3
    assert totals["wait"]["median_sec"] == 0
    assert totals["first_try"]["with_runs"] == 3


def test_stats_reports_unreadable_board_without_failing(client, tmp_path):
    (tmp_path / "kanban" / "boards" / "broken").mkdir(parents=True)
    (tmp_path / "kanban" / "boards" / "broken" / "kanban.db").write_bytes(b"")  # 0 байт
    make_board(tmp_path / "kanban.db", tasks=[{"id": "t_ok1", "status": "ready"}])

    stats = client.get("/stats").json()
    slugs = {board["slug"]: board for board in stats["boards"]}

    assert slugs["broken"]["read"] is False
    assert slugs["broken"].get("note") == "empty db"
    assert any(err["board"] == "broken" for err in stats["errors"])
    # недоступная доска не попадает в итог, но ответ остаётся 200
    assert stats["totals"]["tasks_total"] == 1


def test_stats_without_runs_reports_null_share_and_median(client, tmp_path):
    make_board(tmp_path / "kanban.db", tasks=[{"id": "t_noruns", "status": "todo"}])
    totals = client.get("/stats").json()["totals"]

    assert totals["first_try"] == {"ok": 0, "with_runs": 0, "share": None}
    assert totals["wait"] == {"median_sec": None, "samples": 0}
