"""Контракт формы P0-роутов: `/health`, `/sources`, `/now` и `/cards`.

Baseline — `fd6d102` (каркас + read-only `/health`): у `/health` было 11 ключей,
у `/sources` 19, у `/board/{slug}/tasks` 15. Здесь форма зафиксирована тестом, а
не ручной сверкой: удаление или добавление любого ключа в этих ответах роняет
тест.

0.0.6: доск нет. Ручка `/board/{slug}/tasks` удалена целиком (не алиасена: два
способа адресовать одно дело — две правды), а наблюдение за карточками kanban
живёт своим ответом `/cards` — статусы и лизинг карточек, без дел и без id.
Поэтому её форма и зафиксирована здесь вместо формы доски: 15 ключей доски
описывали дела, которых у duty больше нет, а `/cards` описывает ровно то, чем
карточка является — наблюдением.

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

deals = plugin_api.deals
journal = plugin_api.journal

NOW = 1_793_630_300

# Колонки скопированы с живой `kanban.db` — ровно те, что читает наблюдение.
CARD_SCHEMA = """
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

# Эталон формы: пути ключей ответа (массивы — по первому элементу). Пустые блоки
# (`null`) путей не дают, поэтому фикстура пишет ровно те факты, которые контракт
# закрепляет: идущую попытку и публикацию.
HEALTH_KEYS = {
    "ok",
    "version",
    "hermes_home",
    "store",
    "pool",
    "pool.present",
    "journal",
    "journal.events",
    "journal.deals",
    "mode",
}

SOURCES_KEYS = {
    "version",
    "hermes_home",
    "store",
    "store.path",
    "store.present",
    "repos",
    "repos.list",
    "repos.list[].repo",
    "repos.list[].deals",
    "repos.read",
    "repos.without_repo",
    "pool",
    "pool.path",
    "pool.by_origin_status",
    "pool.by_origin_status[].origin",
    "pool.by_origin_status[].status",
    "pool.by_origin_status[].n",
    "pool.total",
    "profiles",
    "cards",
    "cards.label",
    "cards.databases",
}

NOW_KEYS = {
    "now",
    "count",
    "deals",
    "deals[].repo",
    "deals[].id",
    "deals[].title",
    "deals[].assignee",
    "deals[].status",
    "deals[].priority",
    "deals[].consecutive_failures",
    "deals[].created_at",
    "deals[].workspace_kind",
    "deals[].waiting",
    "deals[].waiting.state",
    "deals[].waiting.human_kind",
    "deals[].waiting.evidence",
    "deals[].attempt",
    "deals[].attempt.run_id",
    "deals[].attempt.source",
    "deals[].attempt.start",
    "deals[].attempt.age_sec",
    "deals[].attempt.pid",
    "deals[].attempt.pid_alive",
    "deals[].attempt.heartbeat_age_sec",
    "deals[].attempt.run_status",
    "deals[].lease",
    "deals[].lease.claim_lock",
    "deals[].lease.expires_at",
    "deals[].lease.expires_in_sec",
    "deals[].lease.state",
    "deals[].stage",
    "deals[].stage.id",
    "deals[].stage.state",
    "deals[].stage.role",
    "deals[].stage.attempt",
    "deals[].stage.since",
    "deals[].stage.live",
    "deals[].budget",
    # 2.0: состояние лида по журналу (`dashboard/leadfeed.py`) — «лид: жив /
    # отсутствует / резюм 2 из 6». Живая сессия не спрашивается, поэтому поля
    # берутся из фактов `lead_*`.
    "deals[].lead",
    "deals[].lead.state",
    "deals[].lead.resumes",
    "deals[].lead.cap",
    "deals[].lead.since",
    "deals[].lead.last",
    "deals[].lead.reason",
    "deals[].lead.text",
    "deals[].lead.evidence",
    "deals[].published",
    "deals[].published.stage",
    "deals[].published.attempt",
    "deals[].published.sha",
    "deals[].published.branch",
    "deals[].published.mr_url",
    "deals[].published.ref",
    "deals[].published.verified_by",
    "deals[].last_action",
    "deals[].last_action.kind",
    "deals[].last_action.at",
    "deals[].last_action.age_sec",
    "deals[].last_action.summary",
    "deals[].last_run",
    "repos",
    "repos.list",
    "repos.list[].repo",
    "repos.list[].deals",
    "repos.read",
    "repos.without_repo",
    "by_state",
    "by_state.capability",
    "by_state.crashed",
    "by_state.dead_lease",
    "by_state.no_signal",
    "by_state.wait_gate",
    "by_state.wait_human",
    "by_state.wait_slot",
    "by_state.working",
    "crash_outcomes",
    "errors",
}

# `/cards`: наблюдение за карточками. Ни дела, ни адреса дела здесь нет.
CARD_KEYS = {
    "now",
    "kind",
    "label",
    "databases",
    "count",
    "cards",
    "cards[].name",
    "cards[].path",
    "cards[].read",
    "cards[].cards",
    "cards[].cards[].id",
    "cards[].cards[].title",
    "cards[].cards[].assignee",
    "cards[].cards[].status",
    "cards[].cards[].block_kind",
    "cards[].cards[].consecutive_failures",
    "cards[].cards[].created_at",
    "cards[].cards[].started_at",
    "cards[].cards[].completed_at",
    "cards[].cards[].run_id",
    "cards[].cards[].run_status",
    "cards[].cards[].lease",
    "cards[].truncated",
}


# `/deals` (0.0.7): список закрытых дел. Форма одна на веб, десктоп и CLI.
DEALS_KEYS = {
    "now",
    "statuses",
    "count",
    "total",
    "limit",
    "cursor",
    "next_cursor",
    "truncated",
    "note",
    "errors",
    "deals",
    # Число закрытых дел по репозиториям: по нему страница листает группы по
    # пятёрке на проект (0.0.9), а не общим потоком.
    "repos",
    "repos[].repo",
    "repos[].total",
    "deals[].id",
    "deals[].repo",
    "deals[].title",
    "deals[].assignee",
    "deals[].status",
    "deals[].created_at",
    "deals[].finished_at",
    "deals[].duration_sec",
    "deals[].cause",
    "deals[].stages",
    "deals[].attempts",
    "deals[].questions",
    "deals[].questions.asked",
    "deals[].questions.answered",
    "deals[].questions.waiting",
    "deals[].published",
    "deals[].published.stage",
    "deals[].published.attempt",
    "deals[].published.sha",
    "deals[].published.branch",
    "deals[].published.mr_url",
    "deals[].published.ref",
    "deals[].published.verified_by",
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
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Один фикстурный корень Hermes: дело в store, карточка в kanban, пул."""
    root = tmp_path / "hermes"
    root.mkdir()
    # Дело своё: id `d_<hex>`, группировка — `repo`; доска для него не нужна.
    conn = deals.open_store(root)
    assert conn is not None
    try:
        made = deals.create_deal(
            conn, repo="sx-widgets-api", title="дело для контракта", now=NOW - 300
        )
        deal_id = str(made["deal"]["id"])
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "kind": kind,
                    "ts": ts,
                    "recorded_at": ts,
                    "source": "engine",
                    "payload": payload,
                    "idempotency_key": f"contract:{kind}",
                }
                for kind, ts, payload in (
                    (
                        "attempt_started",
                        NOW - 200,
                        {
                            "run_id": f"{deal_id}:impl#1",
                            "stage": "impl",
                            "attempt": 1,
                            "pid": 999,
                            "lease_expires": NOW,
                            "contract": {"role": "implementer"},
                        },
                    ),
                    (
                        "published",
                        NOW - 100,
                        {
                            "stage": "publish",
                            "attempt": 1,
                            "sha": "a" * 40,
                            "branch": "release/v0.0.5",
                            "mr_url": "https://gitlab.example/mr/1",
                            "ref": "refs/heads/release/v0.0.5",
                            "verified_by": "git ls-remote",
                        },
                    ),
                )
            ],
        )
    finally:
        conn.close()

    # Карточка kanban — отдельный источник: наблюдение `/cards`, а не дело.
    board = root / "kanban" / "boards" / "stack" / "kanban.db"
    board.parent.mkdir(parents=True)
    conn = sqlite3.connect(board)
    conn.executescript(CARD_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, created_at, started_at, block_kind) "
        "VALUES ('t_c0d7ac01', 'карточка для контракта', 'implementer', 'blocked', "
        f"{NOW - 300}, {NOW - 250}, 'needs_input')"
    )
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
        f"VALUES ('t_c0d7ac01', 'implementer', 'running', 'completed', {NOW - 250}, NULL)"
    )
    conn.commit()
    conn.close()

    pool = root / "messaging" / "pool.db"
    pool.parent.mkdir()
    conn = sqlite3.connect(pool)
    conn.executescript(POOL_SCHEMA)
    conn.execute(
        "INSERT INTO questions (session_key, text, origin, status) "
        "VALUES ('t_c0d7ac01', 'вопрос для контракта', 'card', 'sent')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(plugin_api, "_now_sec", lambda: NOW)
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def test_health_shape_matches_baseline(client: TestClient) -> None:
    """`/health` — 10 ключей: store и пул на чтение, канбана в форме нет."""
    response = client.get("/health")

    assert response.status_code == 200
    keys = shape(response.json())
    assert len(keys) == 10
    assert keys == HEALTH_KEYS


def test_health_journal_counts_deals_without_events(client: TestClient) -> None:
    """Журнал называет свои числа; расхождения «карточки против журнала» нет."""
    body = client.get("/health").json()

    # Одно дело store, у него `created`, `attempt_started` и `published`.
    assert body["journal"] == {"events": 3, "deals": 1}
    assert "drift" not in body["journal"], "карточки не дела: расхождения с ними нет"


def test_now_does_not_create_or_write_the_journal(client: TestClient) -> None:
    """Срез ничего не пишет: число событий и дел то же, что до запроса."""
    before = client.get("/health").json()["journal"]

    body = client.get("/now").json()

    assert body["count"] == 1
    assert body["errors"] == []
    # Состояние взято из журнала своего дела, а не из карточки kanban.
    assert body["deals"][0]["id"].startswith("d_")
    assert body["deals"][0]["repo"] == "sx-widgets-api"
    assert client.get("/health").json()["journal"] == before


def test_now_shape_matches_baseline(client: TestClient) -> None:
    """`/now` — одна форма среза: дела, группировка по repo, таксономия ожиданий."""
    keys = shape(client.get("/now").json())

    assert len(keys) == len(NOW_KEYS)
    assert keys == NOW_KEYS


def test_sources_shape_matches_baseline(client: TestClient) -> None:
    """`/sources` — источники словами: store, репозитории дел, пул, карточки."""
    response = client.get("/sources")

    assert response.status_code == 200
    body = response.json()
    assert body["pool"] is not None
    keys = shape(body)
    assert len(keys) == len(SOURCES_KEYS)
    assert keys == SOURCES_KEYS


def test_cards_shape_matches_baseline(client: TestClient) -> None:
    """`/cards` — наблюдение: статус, ран и лизинг карточки, без дел."""
    response = client.get("/cards")

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "observation"
    assert body["count"] == 1
    assert body["cards"][0]["cards"][0]["id"] == "t_c0d7ac01"
    keys = shape(body)
    assert len(keys) == len(CARD_KEYS)
    assert keys == CARD_KEYS


def test_deals_shape_matches_baseline(client: TestClient, home: Path) -> None:
    """`/deals` — список закрытых дел: форма строки, по которой рисуют все три поверхности.

    Экран веба, экран десктопа и `bin/duty deals` читают ровно эти ключи, поэтому
    форма закреплена здесь: строка несёт записанный факт закрытия (`finished_at`,
    `cause`, `stages`) и числа журнала (`attempts`, `questions`, `duration_sec`,
    `published`), а не пересказ статуса.
    """
    conn = deals.open_store(home)
    assert conn is not None
    try:
        active = deals.deal_rows(conn)[0]
        deals.close_stages_done(conn, str(active["id"]), stages=["impl"], now=NOW - 50)
    finally:
        conn.close()

    response = client.get("/deals")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["deals"][0]["status"] == "done"
    keys = shape(body)
    assert len(keys) == len(DEALS_KEYS)
    assert keys == DEALS_KEYS


def test_board_route_is_gone(client: TestClient) -> None:
    """`/board/{slug}/tasks` удалён целиком: доски больше не адрес.

    Не алиас и не редирект — 404: второй способ адресовать одно дело дал бы две
    правды. Карточки смотрят через `/cards`, дела — через `/deal/{deal_id}`.
    """
    assert client.get("/board/stack/tasks").status_code == 404
    assert client.get("/board/nope/tasks").status_code == 404


def test_board_query_filter_is_refused_with_a_hint(client: TestClient) -> None:
    """`?board=` на `/now`, `/stats` и `/deals` — отказ словами, а не молчаливый полный срез."""
    for path in ("/now", "/stats", "/deals"):
        response = client.get(path, params={"board": "stack"})
        assert response.status_code == 400, path
        assert "repo" in response.json()["detail"], path


def test_all_responses_are_not_cacheable(client: TestClient) -> None:
    """Ни один ответ плагина не кэшируется: иначе протухший снимок выглядит живым.

    Проверяем и успешный ответ, и отказы плагина — кэш промежуточного слоя
    одинаково охотно держит оба, а «дела нет» из вчерашнего тела потом висит на
    экране. Удалённой ручки в списке нет: 404 без `cache-control` отдаёт уже
    FastAPI, а не плагин.
    """
    paths = (
        "/health",
        "/sources",
        "/now",
        "/cards",
        "/deals",
        "/deal/d_ffffffff",
        "/now?board=stack",
        "/deals?status=running",
    )
    for path in paths:
        response = client.get(path)
        assert response.headers.get("cache-control") == "no-store, no-cache, must-revalidate", path
