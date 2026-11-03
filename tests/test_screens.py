"""Контракт экранов 0.0.2: `/deal/{board}/{id}`, `/stats` и веб-сборка.

Экраны «Дело» и «Здоровье» считаются по тем же read-only источникам, что и
`/now`, поэтому фикстуры здесь свои, маленькие: `tmp_path` + `HERMES_HOME`,
живые БД не читаются.

Проверяется не рендер, а то, что нельзя проверить глазами при ревью:
распределение ожидания считается методом ближайшего ранга по объединённой
выборке (а не средним перцентилей досок), «с первой попытки» — по первому по
времени рану, обрезанный срез помечен и назван в `warnings[]`, недоступная доска
не роняет ответ и не попадает в итог, а завершённое дело не получает
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


def test_stats_first_try_and_wait_distribution(client, tmp_path):
    # Три дела: первая попытка успешна у двух, у третьего — падение; ожидание
    # до первой попытки 100/300/800 с. Метод ближайшего ранга: p50 — 2-й из 3
    # (300), p90 и p99 — 3-й (800), максимум 800.
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
    assert totals["waiting_sec"] == {"p50": 300, "p90": 800, "p99": 800, "count": 3, "max": 800}
    assert totals["runs_by_outcome"] == {"completed": 3, "crashed": 1}
    assert "completed" in stats["success_outcomes"]
    assert "crashed" in stats["crash_outcomes"]
    # срез не обрезан — распределение полное, предупреждать не о чем
    assert stats["warnings"] == []


def test_stats_wait_percentiles_nearest_rank(client, tmp_path):
    # Ожидания 1..10 с: ближайший ранг даёт p50 = 5 (позиция 5 из 10) и p90 = 9
    # (позиция 9), p99 — позиция 10, то есть максимум; интерполяция дала бы 5.5 и
    # 9.1, а медиана чётного ряда — 5.5. Тест фиксирует именно метод.
    make_board(
        tmp_path / "kanban.db",
        tasks=[
            {"id": f"t_p{i:02d}", "status": "done", "created_at": NOW - 1000 + i}
            for i in range(1, 11)
        ],
        runs=[
            {"task_id": f"t_p{i:02d}", "outcome": "completed", "started_at": NOW - 1000 + i + i}
            for i in range(1, 11)
        ],
    )
    totals = client.get("/stats").json()["totals"]

    # ожидание дела i — ровно i секунд (created_at = NOW - 1000 + i, started_at = +i)
    assert totals["waiting_sec"] == {"p50": 5, "p90": 9, "p99": 10, "count": 10, "max": 10}


def test_stats_wait_single_sample_has_all_percentiles(client, tmp_path):
    # Один элемент: любой перцентиль — это он сам, а не None и не 0.
    make_board(
        tmp_path / "kanban.db",
        tasks=[{"id": "t_one", "status": "done", "created_at": NOW - 107}],
        runs=[{"task_id": "t_one", "outcome": "completed", "started_at": NOW - 100}],
    )
    totals = client.get("/stats").json()["totals"]

    assert totals["waiting_sec"] == {"p50": 7, "p90": 7, "p99": 7, "count": 1, "max": 7}


def test_stats_wait_is_over_pooled_sample(client, tmp_path):
    # Доска A: ожидания 0 и 0. Доска B: 1000 с. Перцентиль по каждой доске отдельно
    # дал бы 0 и 1000; объединённая выборка 0,0,1000 — p50 = 0, p90 = 1000.
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

    assert totals["waiting_sec"] == {"p50": 0, "p90": 1000, "p99": 1000, "count": 3, "max": 1000}
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


def test_stats_without_runs_reports_null_share_and_empty_distribution(client, tmp_path):
    make_board(tmp_path / "kanban.db", tasks=[{"id": "t_noruns", "status": "todo"}])
    totals = client.get("/stats").json()["totals"]

    assert totals["first_try"] == {"ok": 0, "with_runs": 0, "share": None}
    # «нет данных» — не «ждали 0 секунд»: перцентили null, count 0
    assert totals["waiting_sec"] == {"p50": None, "p90": None, "p99": None, "count": 0, "max": None}


def test_stats_truncated_board_marks_distribution_incomplete(client, tmp_path, monkeypatch):
    # Срез доски обрезан лимитом: распределение посчитано, но по неполной выборке,
    # и это названо предупреждением верхнего уровня — иначе `totals.waiting_sec`
    # читался бы как полный.
    monkeypatch.setattr(plugin_api, "STATS_TASK_LIMIT", 2)
    make_board(
        tmp_path / "kanban.db",
        tasks=[
            {"id": "t_tr1", "status": "done", "created_at": NOW - 1000},
            {"id": "t_tr2", "status": "done", "created_at": NOW - 900},
            {"id": "t_tr3", "status": "done", "created_at": NOW - 800},
        ],
        runs=[
            {"task_id": "t_tr1", "outcome": "completed", "started_at": NOW - 900},
            {"task_id": "t_tr2", "outcome": "completed", "started_at": NOW - 800},
            {"task_id": "t_tr3", "outcome": "completed", "started_at": NOW - 700},
        ],
    )
    stats = client.get("/stats").json()
    totals = stats["totals"]

    assert stats["boards"][0]["truncated"] is True
    assert totals["tasks_total"] == 2  # в срез вошли первые два дела по created_at
    assert totals["waiting_sec"] == {"p50": 100, "p90": 100, "p99": 100, "count": 2, "max": 100}
    assert len(stats["warnings"]) == 1
    assert "срез обрезан лимитом" in stats["warnings"][0]
    assert stats["warnings"][0].startswith("доска default")


def test_stats_board_distribution_is_not_averaged_across_boards(client, tmp_path):
    # Перцентили доски считаются по её собственной выборке — не подмешивая итог.
    make_board(
        tmp_path / "kanban" / "boards" / "a" / "kanban.db",
        tasks=[{"id": "t_x1", "status": "done", "created_at": NOW - 100}],
        runs=[{"task_id": "t_x1", "started_at": NOW - 40}],
    )
    make_board(
        tmp_path / "kanban" / "boards" / "b" / "kanban.db",
        tasks=[{"id": "t_y1", "status": "done", "created_at": NOW - 100}],
        runs=[{"task_id": "t_y1", "started_at": NOW - 100}],
    )
    stats = client.get("/stats").json()
    per_board = {board["slug"]: board["waiting_sec"] for board in stats["boards"]}

    assert per_board["a"] == {"p50": 60, "p90": 60, "p99": 60, "count": 1, "max": 60}
    assert per_board["b"] == {"p50": 0, "p90": 0, "p99": 0, "count": 1, "max": 0}
    assert stats["totals"]["waiting_sec"] == {"p50": 0, "p90": 60, "p99": 60, "count": 2, "max": 60}
