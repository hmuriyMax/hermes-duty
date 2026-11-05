"""Контракт формы P0-роутов: `/health`, `/sources`, `/board/`.

Baseline — `fd6d102` (каркас + read-only `/health`): у `/health` 11 ключей,
у `/sources` было 19, у `/board/` 15 (см. `evidence.json` карточки
`t_ed22e46e`). Здесь форма зафиксирована тестом, а не ручной сверкой: удаление
или добавление любого ключа в этих ответах роняет тест.

`/sources` 19 → 23 в 0.0.2: добавлен список выбывших досок (`archived[]` с
`slug`/`path`/`reason`) — осознанное расширение, а не дрейф: архив перестал быть
источником среза, но обязан быть видимым, иначе «доски нет» и «доска в архиве»
неотличимы.

Источники — свои маленькие БД в `tmp_path` по реальной схеме, живые не читаются.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_contract", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

# Колонки скопированы с живой `kanban.db` — ровно те, что читает P0-роут.
BOARD_SCHEMA = """
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

# Эталон формы: пути ключей ответа (массивы — по первому элементу).
# 0.0.3: `/health` дополнен блоком `journal: {events, deals, drift: {empty_journal}}`
# — расхождение журнала и kanban видно числом, а не на глаз (docs/JOURNAL.md,
# инвариант 5). Это осознанное расширение формы: 11 ключей → 16.
HEALTH_KEYS = {
    "ok",
    "version",
    "hermes_home",
    "kanban",
    "kanban.databases",
    "kanban.readable",
    "kanban.task_rows",
    "pool",
    "pool.present",
    "pool.total",
    "journal",
    "journal.events",
    "journal.deals",
    "journal.drift",
    "journal.drift.empty_journal",
    "mode",
}

SOURCES_KEYS = {
    "version",
    "hermes_home",
    "boards",
    "boards[].slug",
    "boards[].path",
    "boards[].size_bytes",
    "boards[].mtime",
    "boards[].readable",
    "boards[].tasks",
    "boards[].blocked",
    "boards[].running",
    "archived",
    "archived[].slug",
    "archived[].path",
    "archived[].reason",
    "pool",
    "pool.path",
    "pool.by_origin_status",
    "pool.by_origin_status[].origin",
    "pool.by_origin_status[].status",
    "pool.by_origin_status[].n",
    "pool.total",
    "profiles",
}

BOARD_KEYS = {
    "slug",
    "path",
    "count",
    "tasks",
    "tasks[].id",
    "tasks[].title",
    "tasks[].assignee",
    "tasks[].status",
    "tasks[].block_kind",
    "tasks[].consecutive_failures",
    "tasks[].worker_pid",
    "tasks[].current_run_id",
    "tasks[].created_at",
    "tasks[].started_at",
    "tasks[].completed_at",
}


def shape(obj: Any, prefix: str = "") -> set[str]:
    """Пути ключей ответа: значение не важно, важна форма контракта."""
    paths: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.add(path)
            paths |= shape(value, path)
    elif isinstance(obj, list) and obj:
        paths |= shape(obj[0], f"{prefix}[]")
    return paths


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Один фикстурный корень Hermes: доска `stack` с одним делом и пул с вопросом."""
    home = tmp_path / "hermes"
    board = home / "kanban" / "boards" / "stack" / "kanban.db"
    board.parent.mkdir(parents=True)
    conn = sqlite3.connect(board)
    conn.executescript(BOARD_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, block_kind) "
        "VALUES ('t_c0d7ac01', 'дело для контракта', 'implementer', 'blocked', 1793630000, "
        "'needs_input')"
    )
    conn.commit()
    conn.close()

    pool = home / "messaging" / "pool.db"
    pool.parent.mkdir()
    conn = sqlite3.connect(pool)
    conn.executescript(POOL_SCHEMA)
    conn.execute(
        "INSERT INTO questions (session_key, text, origin, status) "
        "VALUES ('t_c0d7ac01', 'вопрос для контракта', 'card', 'sent')"
    )
    conn.commit()
    conn.close()

    # Выбывшая доска под `_archived/`: её форма в `/sources.archived` тоже часть
    # контракта, поэтому в фикстуре она есть (иначе `shape()` не увидит список).
    retired = home / "kanban" / "boards" / "_archived" / "retired" / "kanban.db"
    retired.parent.mkdir(parents=True)
    conn = sqlite3.connect(retired)
    conn.executescript(BOARD_SCHEMA)
    conn.commit()
    conn.close()

    monkeypatch.setenv("HERMES_HOME", str(home))
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def test_health_shape_matches_baseline(client: TestClient) -> None:
    """`/health` — 16 ключей: baseline 11 плюс `journal.*` (0.0.3)."""
    response = client.get("/health")

    assert response.status_code == 200
    keys = shape(response.json())
    assert len(keys) == 16
    assert keys == HEALTH_KEYS


def test_health_journal_drift_counts_deals_without_events(client: TestClient, tmp_path: Path) -> None:
    """Журнала ещё нет → 0 событий и все активные дела в расхождении (не «всё хорошо»)."""
    journal = client.get("/health").json()["journal"]

    assert journal == {"events": 0, "deals": 0, "drift": {"empty_journal": 1}}
    # `/health` не создаёт файл журнала как побочный эффект чтения.
    assert not (tmp_path / "hermes" / "duty" / "duty.db").exists()


def test_now_does_not_create_or_write_the_journal(client: TestClient, tmp_path: Path) -> None:
    """Срез без `?ingest=1` ничего не пишет: журнал не появляется даже файлом."""
    body = client.get("/now").json()

    assert body["count"] == 1
    assert body["errors"] == []
    assert body["deals"][0]["waiting"]["evidence"].startswith("journal: empty — состояние из kanban")
    assert not (tmp_path / "hermes" / "duty" / "duty.db").exists()


def test_sources_shape_matches_baseline(client: TestClient) -> None:
    """`/sources` — 23 ключа: baseline 19 плюс `archived[]` (0.0.2)."""
    response = client.get("/sources")

    assert response.status_code == 200
    body = response.json()
    assert body["pool"] is not None
    keys = shape(body)
    assert len(keys) == 23
    assert keys == SOURCES_KEYS


def test_board_tasks_shape_matches_baseline(client: TestClient) -> None:
    """`/board/` — 15 ключей, как в baseline `fd6d102`."""
    response = client.get("/board/stack/tasks")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    keys = shape(body)
    assert len(keys) == 15
    assert keys == BOARD_KEYS


def test_all_responses_are_not_cacheable(client: TestClient) -> None:
    """Ни один ответ плагина не кэшируется: иначе протухший снимок выглядит живым.

    Проверяем и успешный ответ, и 404 — кэш промежуточного слоя одинаково охотно
    держит оба, а «доска недоступна» из вчерашнего тела потом висит на экране.
    """
    for path in ("/health", "/sources", "/now", "/board/stack/tasks", "/board/nope/tasks"):
        response = client.get(path)
        assert response.headers.get("cache-control") == "no-store, no-cache, must-revalidate", path
