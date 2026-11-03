"""`ask()` — единственный канал вопросов (контракт `docs/JOURNAL.md`).

Проверяется то, что нельзя увидеть глазами при ревью: живой вопрос по делу
возвращается как есть (второй строки в пуле нет), сбой пула не оставляет в
журнале ни вопроса, ни его тени, ответ доходит до журнала ровно один раз, а
`waiting.human_kind` отличает «есть вопрос» от «человек нужен, вопроса нет».

Фикстуры — свои маленькие `kanban.db`/`pool.db` в `tmp_path` по реальной схеме;
живые источники не читаются.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_ask", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

journal = plugin_api.journal
ask = plugin_api.ask

NOW = 1_800_000_000
DEAL = "t_0001"

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
    worker_pid          INTEGER,
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

# Схема пула скопирована с живого `messaging/pool.db` (колонки, которые читает
# и пишет imessage-gateway); `answered_via` нужен `sync_answers`.
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


# ------------------------------------------------------------------ фикстуры


def make_board(home: Path, slug: str = "stack") -> Path:
    """Доска в `kanban/boards/<slug>/kanban.db` с одним заблокированным делом."""
    path = home / "kanban" / "boards" / slug / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(KANBAN_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, created_at, block_kind) "
        "VALUES (?, 'дело с вопросом', 'тело дела', 'implementer', 'blocked', ?, 'needs_input')",
        (DEAL, NOW - 3600),
    )
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


def pool_rows(home: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM questions ORDER BY id")]
    finally:
        conn.close()


def journal_events(home: Path) -> list[dict[str, Any]]:
    conn = journal.open_journal(home, create=False)
    assert conn is not None
    try:
        return journal.read_events(conn)
    finally:
        conn.close()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes: доска с заблокированным делом и пустой пул вопросов."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    make_board(root)
    make_pool(root)
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def deal_facts(home: Path) -> dict[str, Any]:
    """Факты дела для прямого вызова `ask()` (без HTTP)."""
    return {"id": DEAL, "board": "stack", "home": home}


def ask_once(home: Path, **kwargs: Any) -> int:
    """Прямой вызов `ask()` с соединением журнала, как это делает роут."""
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        return ask.ask(
            conn,
            deal_facts(home),
            kwargs.pop("text", "Вопрос по делу"),
            kwargs.pop("text_full", "полная версия вопроса"),
            **kwargs,
        )
    finally:
        conn.close()


# ------------------------------------------------------- создание вопроса


def test_ask_writes_pool_row_and_journal_event(home: Path) -> None:
    """Вопрос: строка пула по схеме пула + `question_asked` в журнале после вставки."""
    question_id = ask_once(home, options=["да", "нет"])

    rows = pool_rows(home)
    assert len(rows) == 1
    row = rows[0]
    assert question_id == row["id"]
    assert row["session_key"] == "t_0001"
    assert row["origin"] == ask.ASK_ORIGIN == "duty"
    assert row["kind"] == "decision"
    assert row["project"] == "stack"
    assert row["status"] == "pending"
    assert row["text"] == "Вопрос по делу"
    assert row["text_full"] == "полная версия вопроса"
    assert json.loads(row["options_json"]) == ["да", "нет"]
    assert row["critical"] == 0
    assert row["timeout"] == ask.ASK_TIMEOUT_HOURS

    events = journal_events(home)
    assert [event["kind"] for event in events] == ["question_asked"]
    asked = events[0]
    assert asked["deal_id"] == DEAL
    assert asked["board"] == "stack"
    assert asked["source"] == "ask"
    assert asked["idempotency_key"] == f"ask:{question_id}:question_asked"
    assert asked["payload"] == {
        "question_id": question_id,
        "session_key": "t_0001",
        "critical": 0,
        "timeout": ask.ASK_TIMEOUT_HOURS,
    }


def test_second_ask_returns_same_question_without_duplicate(home: Path) -> None:
    """Второй `ask()` по тому же делу: тот же `question_id`, одна строка, одно событие."""
    first = ask_once(home, text="Первый вопрос")
    second = ask_once(home, text="Другой текст — тот же вопрос")

    assert second == first
    rows = pool_rows(home)
    assert len(rows) == 1
    assert rows[0]["text"] == "Первый вопрос"
    assert [event["kind"] for event in journal_events(home)] == ["question_asked"]


def test_ask_adopts_live_question_of_another_producer(home: Path) -> None:
    """Живой вопрос, созданный не нами (origin='card'), возвращается как есть."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.execute(
        "INSERT INTO questions (session_key, text, origin, status, critical, timeout) "
        "VALUES ('t_0001', 'вопрос сканера карточек', 'card', 'sent', 1, NULL)"
    )
    conn.commit()
    conn.close()

    question_id = ask_once(home)

    rows = pool_rows(home)
    assert len(rows) == 1
    assert question_id == rows[0]["id"]
    assert rows[0]["origin"] == "card"
    events = journal_events(home)
    assert [event["kind"] for event in events] == ["question_asked"]
    # Факты вопроса взяты из строки пула, а не выдуманы: он критичный и без срока.
    assert events[0]["payload"]["critical"] == 1
    assert events[0]["payload"]["timeout"] is None


def test_ask_marks_long_waiting_deal_critical(home: Path) -> None:
    """Блок старше `ASK_CRITICAL_SEC` → `critical=1` и вопрос без срока."""
    now = int(time.time())
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        journal.append(
            conn,
            [
                {
                    "ts": now - ask.ASK_CRITICAL_SEC - 600,
                    "recorded_at": now - ask.ASK_CRITICAL_SEC - 600,
                    "deal_id": DEAL,
                    "board": "stack",
                    "kind": "blocked",
                    "payload": {"block_kind": "needs_input", "reason": "нужно решение"},
                    "source": "ingest",
                    "idempotency_key": "ingest:stack:t_0001:blocked:1",
                }
            ],
        )
    finally:
        conn.close()

    ask_once(home)

    row = pool_rows(home)[0]
    assert row["critical"] == 1
    assert row["timeout"] is None


# ---------------------------------------------------------- сбои пула


def test_missing_pool_raises_and_writes_no_event(home: Path) -> None:
    """Нет файла пула → `AskError`, в журнале ни одного события."""
    (home / "messaging" / "pool.db").unlink()

    with pytest.raises(ask.AskError):
        ask_once(home)

    assert journal_events(home) == []


def test_readonly_pool_raises_and_writes_no_event(home: Path) -> None:
    """Пул только на чтение → вставка не проходит, события в журнале нет."""
    db = home / "messaging" / "pool.db"
    os.chmod(db, 0o444)
    try:
        with pytest.raises(ask.AskError):
            ask_once(home)
    finally:
        os.chmod(db, 0o644)

    assert journal_events(home) == []
    assert pool_rows(home) == []


def test_ask_route_reports_pool_failure_in_errors(client: TestClient, home: Path) -> None:
    """Сбой пула на роуте: 503 и `errors[]`, где назван пул и дело."""
    (home / "messaging" / "pool.db").unlink()

    response = client.post("/ask/stack/t_0001", json={"text": "Вопрос"})

    assert response.status_code == 503
    body = response.json()
    assert body["question_id"] is None
    assert body["errors"][0]["board"] == "messaging/pool.db"
    assert "t_0001" in body["errors"][0]["reason"]
    assert journal_events(home) == []


# ------------------------------------------------------------ маршрут


def test_ask_route_creates_then_dedups(client: TestClient) -> None:
    """Роут: первый вызов создаёт вопрос, второй возвращает тот же (`deduped`)."""
    first = client.post(
        "/ask/stack/t_0001",
        json={"text": "Даёшь ок?", "text_full": "### Вопрос\n\nРазвёрнутая версия", "options": ["ок", "нет"]},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["errors"] == []
    assert body["deduped"] is False
    question_id = body["question_id"]

    second = client.post("/ask/stack/t_0001", json={"text": "повтор"})
    assert second.status_code == 200
    assert second.json() == {"question_id": question_id, "deduped": True, "errors": []}


def test_ask_route_validates_deal_and_text(client: TestClient) -> None:
    """Неизвестное дело — 404, пустой текст — 400, кривые options — 400."""
    assert client.post("/ask/stack/t_dead", json={"text": "?"}).status_code == 404
    assert client.post("/ask/nope/t_0001", json={"text": "?"}).status_code == 404
    assert client.post("/ask/stack/t_0001", json={"text": "   "}).status_code == 400
    bad_options = client.post("/ask/stack/t_0001", json={"text": "?", "options": [{"label": "ок"}]})
    assert bad_options.status_code == 400


def test_ask_route_does_not_duplicate_and_marks_not_cacheable(client: TestClient) -> None:
    """Ответ роута не кэшируется: иначе «вопрос создан» переживёт сам вопрос."""
    response = client.post("/ask/stack/t_0001", json={"text": "Вопрос"})

    assert response.headers.get("cache-control") == "no-store, no-cache, must-revalidate"


# ----------------------------------------------------------- sync_answers


def answer_own_question(home: Path, answer: str = "ок", via: str = "manual") -> int:
    """Ответить на единственный свой вопрос пула штатным путём (UPDATE пула пула)."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        conn.execute(
            "UPDATE questions SET status='answered', answer=?, answered_via=?, "
            "answered_at='2026-11-03T10:00:00.000Z' WHERE origin='duty' AND status IN ('pending','sent')",
            (answer, via),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM questions WHERE origin='duty' ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return int(row[0])


def test_sync_answers_is_idempotent(home: Path) -> None:
    """Два прохода `sync_answers` → ровно одно событие `question_answered`."""
    question_id = ask_once(home)
    answer_own_question(home)

    conn = journal.open_journal(home)
    assert conn is not None
    try:
        first = ask.sync_answers(conn, home)
        second = ask.sync_answers(conn, home)
    finally:
        conn.close()

    assert first["appended"] == 1
    assert first["errors"] == []
    assert second["appended"] == 0
    answered = [event for event in journal_events(home) if event["kind"] == "question_answered"]
    assert len(answered) == 1
    assert answered[0]["payload"] == {
        "question_id": question_id,
        "answer_len": len("ок"),
        "answered_via": "manual",
    }
    assert answered[0]["deal_id"] == DEAL
    assert answered[0]["board"] == "stack"
    assert answered[0]["ts"] == int(datetime.fromisoformat("2026-11-03T10:00:00+00:00").timestamp())


def test_sync_answers_ignores_foreign_questions(home: Path) -> None:
    """Отвеченный вопрос чужого продюсера — не наш факт: событий нет."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.execute(
        "INSERT INTO questions (session_key, text, origin, status, answer, answered_via) "
        "VALUES ('t_0002', 'чужой вопрос', 'card', 'answered', 'да', 'manual')"
    )
    conn.commit()
    conn.close()

    journal_conn = journal.open_journal(home)
    assert journal_conn is not None
    try:
        report = ask.sync_answers(journal_conn, home)
    finally:
        journal_conn.close()

    assert report == {"appended": 0, "errors": []}
    assert journal_events(home) == []


def test_sync_answers_reports_missing_pool(home: Path) -> None:
    """Пул недоступен — причина в `errors[]`, а не «ответов нет»."""
    (home / "messaging" / "pool.db").unlink()

    conn = journal.open_journal(home)
    assert conn is not None
    try:
        report = ask.sync_answers(conn, home)
    finally:
        conn.close()

    assert report["appended"] == 0
    assert report["errors"][0]["board"] == "messaging/pool.db"


def test_ingest_pass_syncs_answers(client: TestClient) -> None:
    """`POST /ingest` дочитывает ответы пула: блок `answers` и идемпотентность."""
    question_id = ask_once(Path(os.environ["HERMES_HOME"]))
    answer_own_question(Path(os.environ["HERMES_HOME"]))

    first = client.post("/ingest").json()
    second = client.post("/ingest").json()

    assert first["answers"] == {"appended": 1, "errors": []}
    assert second["answers"] == {"appended": 0, "errors": []}
    assert question_id in {
        event["payload"]["question_id"]
        for event in journal_events(Path(os.environ["HERMES_HOME"]))
        if event["kind"] == "question_answered"
    }


# ----------------------------------------------------- human_kind в /now


def test_human_kind_status_until_a_question_exists(client: TestClient) -> None:
    """Блок без вопроса: `human_kind=status` — человек нужен, вопроса нет."""
    waiting = client.get("/now").json()["deals"][0]["waiting"]

    assert waiting["state"] == "wait_human"
    assert waiting["human_kind"] == "status"


def test_human_kind_question_after_ask(client: TestClient) -> None:
    """После `ask()` дело ждёт ответа на конкретный вопрос: `human_kind=question`."""
    question_id = client.post("/ask/stack/t_0001", json={"text": "Даёшь ок?"}).json()["question_id"]

    waiting = client.get("/now").json()["deals"][0]["waiting"]

    assert waiting["state"] == "wait_human"
    assert waiting["human_kind"] == "question"
    assert f"#{question_id}" in waiting["evidence"]


# --------------------------------------------- запись в пул — только ask.py


def test_only_ask_module_writes_the_pool() -> None:
    """Запись в пул — только через `ask.py`; остальной код его не меняет.

    Проверка по файлам: ни один модуль плагина, кроме `ask.py`, не содержит
    пишущих запросов к `questions`. Соединения к пулу остальных модулей идут
    через `?mode=ro` (`journal.connect_ro`, `plugin_api._connect_ro`).
    """
    dashboard = MODULE_PATH.parent
    writers = [
        "INSERT INTO questions",
        "REPLACE INTO questions",
        "UPDATE questions",
        "DELETE FROM questions",
    ]
    offenders: list[str] = []
    for path in sorted(dashboard.glob("*.py")):
        if path.name == "ask.py":
            continue
        source = path.read_text(encoding="utf-8")
        for writer in writers:
            if writer in source:
                offenders.append(f"{path.name}: {writer}")
    assert offenders == []


def test_read_routes_do_not_touch_the_pool(client: TestClient, home: Path) -> None:
    """Поведением: чтение пула не меняет его файл (mtime и размер те же)."""
    db = home / "messaging" / "pool.db"
    before = (db.stat().st_mtime_ns, db.stat().st_size)

    client.get("/now")
    client.get("/deal/stack/t_0001")
    client.get("/health")
    client.get("/sources")
    client.get("/now", params={"ingest": 1})

    after = (db.stat().st_mtime_ns, db.stat().st_size)
    assert after == before
    assert pool_rows(home) == []
