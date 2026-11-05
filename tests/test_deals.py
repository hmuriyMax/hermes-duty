"""Владение своим делом: store, мутации, гейт и цикл вопроса (W5).

Проверяется то, чего не видно при чтении кода: каждая мутация пишет строку дела и
событие журнала **одной** транзакцией, повтор с теми же аргументами не плодит
событий, `ready` появляется только по закрытым родителям и переход гейта назван
событием, а не молчанием, и ни одна мутация не трогает `kanban.db`/`state.db`.

Отдельная группа — цикл вопроса своего дела (`d_…`): `session_key` пула
(`t_d_…`) и id дела должны сходиться в одну сторону и обратно, иначе
`question_asked` и `question_answered` живут под разными id и дело навсегда
остаётся «ждёт ответа» (находка координатора 03.11). Рядом — тот же цикл для
`t_…`: второй путь не должен сломаться от появления своего пространства id.

Фикстуры — свои маленькие `kanban.db`/`pool.db` в `tmp_path` по реальной схеме;
живые источники не читаются и не пишутся.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_deals", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

journal = plugin_api.journal
ask = plugin_api.ask
deals = plugin_api.deals

NOW = 1_800_000_000
BOARD = "stack"
KANBAN_DEAL = "t_0000aa"

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

# Схема пула скопирована с живого `messaging/pool.db` — колонки, которые читает и
# пишет imessage-gateway (`answered_via` нужен `sync_answers`).
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


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes: доска с одним kanban-делом, пустой пул, пустой `state.db`."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    board = root / "kanban" / "boards" / BOARD / "kanban.db"
    board.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(board)
    conn.executescript(KANBAN_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, created_at, block_kind) "
        "VALUES (?, 'kanban-дело', 'тело', 'implementer', 'blocked', ?, 'needs_input')",
        (KANBAN_DEAL, NOW - 3600),
    )
    conn.commit()
    conn.close()
    pool = root / "messaging" / "pool.db"
    pool.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(pool)
    conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()
    state_db = root / "profiles" / "implementer" / "state.db"
    state_db.parent.mkdir(parents=True)
    sqlite3.connect(state_db).close()
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def store(home: Path) -> sqlite3.Connection:
    """Соединение своего store — открывается мутациями, а не тестом."""
    conn = deals.open_store(home)
    assert conn is not None
    conn.row_factory = sqlite3.Row
    return conn


def deal_rows(home: Path) -> list[dict[str, Any]]:
    conn = store(home)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM deals ORDER BY created_at, id")]
    finally:
        conn.close()


def events(home: Path, deal_id: str | None = None) -> list[dict[str, Any]]:
    """События журнала в порядке `id` (payload разобран, как его отдаёт модуль)."""
    conn = store(home)
    try:
        return journal.read_events(conn, deal_id)
    finally:
        conn.close()


def kinds(home: Path, deal_id: str) -> list[str]:
    return [str(event["kind"]) for event in events(home, deal_id)]


def create(client: TestClient, **payload: Any) -> dict[str, Any]:
    """`POST /deals` со значениями по умолчанию: доска, заголовок, тело."""
    request = {"repo": BOARD, "title": "своё дело", "body": "спецификация"}
    request.update(payload)
    response = client.post("/deals", json=request)
    assert response.status_code == 200, response.text
    return response.json()


def mutate(client: TestClient, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """Мутация с проверкой кода 200: отказ должен быть виден отдельным тестом."""
    response = client.request(method, path, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def now_deals(client: TestClient) -> dict[str, dict[str, Any]]:
    """`/now` по id дела: один ответ на «что сейчас» для обоих пространств."""
    return {item["id"]: item for item in client.get("/now").json()["deals"]}


def source_stats(home: Path) -> dict[Path, tuple[int, int]]:
    """mtime и размер источников, которые мутации не имеют права трогать."""
    paths = [
        home / "kanban" / "boards" / BOARD / "kanban.db",
        home / "profiles" / "implementer" / "state.db",
        home / "messaging" / "pool.db",
    ]
    return {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in paths}


# --------------------------------------------------------- создание и store


def test_create_writes_deal_row_and_created_event(home: Path, client: TestClient) -> None:
    """`POST /deals`: строка store и `created` в журнале, id своего пространства."""
    body = create(client)

    assert body["id"].startswith("d_")
    assert body["status"] == "ready"
    assert body["title"] == "своё дело"
    assert body["mutation"] == {"events": 1, "idempotent": False}
    assert body["errors"] == []

    rows = deal_rows(home)
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]
    assert rows[0]["origin"] == "duty"
    assert rows[0]["body"] == "спецификация"

    assert kinds(home, body["id"]) == ["created"]
    created = events(home, body["id"])[0]
    assert created["board"] == BOARD
    assert created["source"] == "api"
    assert created["payload"]["parents"] == []
    assert created["payload"]["title"] == "своё дело"


def test_create_with_unknown_parent_is_error_not_500(home: Path, client: TestClient) -> None:
    """Несуществующий родитель — `errors[]` и 400, а не падение роута."""
    response = client.post("/deals", json={"repo": BOARD, "title": "ребёнок", "parents": ["d_deadbeef"]})

    assert response.status_code == 400
    body = response.json()
    assert body["deal"] is None
    assert body["errors"], body
    assert "d_deadbeef" in body["errors"][0]["reason"]
    assert deal_rows(home) == []


def test_create_on_unknown_board_is_error(home: Path, client: TestClient) -> None:
    """Доска, которой нет ни в kanban, ни в store, не заводится молча."""
    response = client.post("/deals", json={"repo": "no-such-board", "title": "дело"})

    assert response.status_code == 400
    assert "no-such-board" in response.json()["errors"][0]["reason"]
    assert deal_rows(home) == []


def test_create_requires_title_and_board(client: TestClient) -> None:
    """Пустой title или board — ошибка входа, а не дело без имени."""
    for payload in ({"repo": BOARD, "title": "  "}, {"repo": "", "title": "дело"}):
        response = client.post("/deals", json=payload)
        assert response.status_code == 400
        assert response.json()["errors"], payload


def test_no_kanban_origin_rows_ever(home: Path, client: TestClient) -> None:
    """`origin='kanban'` в `deals` не появляется ни в одном сценарии (инвариант 4)."""
    parent = create(client, title="родитель")
    child = create(client, title="ребёнок", parents=[parent["id"]])
    mutate(client, "POST", f"/deals/{child['id']}/block", json={"kind": "needs_input", "reason": "нет ТЗ"})
    mutate(client, "POST", f"/deals/{child['id']}/unblock", json={})
    mutate(client, "POST", f"/deals/{child['id']}/claim", json={"owner": "implementer"})
    mutate(client, "POST", f"/deals/{child['id']}/complete", json={"summary": "ok"})
    client.get("/now")
    client.get(f"/deal/{BOARD}/{child['id']}")

    conn = store(home)
    try:
        origins = {str(row[0]) for row in conn.execute("SELECT DISTINCT origin FROM deals")}
    finally:
        conn.close()
    assert origins == {"duty"}
    assert deal_rows(home), "строки должны быть — проверяется их origin"


# ------------------------------------------------ одна транзакция на мутацию


def test_event_write_failure_rolls_back_deal_row(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой записи события откатывает всё дело, а не половину (инвариант 2).

    Подменяется `journal.append_in_tx` — единственная точка, через которую
    мутация пишет журнал: если событие не легло, строки дела быть не должно.
    """
    app = FastAPI()
    app.include_router(plugin_api.router)

    def boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("журнал недоступен")

    monkeypatch.setattr(journal, "append_in_tx", boom)
    with pytest.raises(RuntimeError, match="журнал недоступен"):
        TestClient(app).post("/deals", json={"repo": BOARD, "title": "не должно остаться"})
    # store мог быть создан (файл и схема), но строки дела в нём нет
    assert deal_rows(home) == []


def test_event_write_failure_rolls_back_comment(home: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Тот же сбой на комментарии: строка `comments` не остаётся без события."""
    deal = create(client)
    mutate(client, "POST", f"/deals/{deal['id']}/comment", json={"author": "max", "body": "до сбоя"})

    def boom(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("журнал недоступен")

    monkeypatch.setattr(journal, "append_in_tx", boom)
    with pytest.raises(RuntimeError, match="журнал недоступен"):
        mutate(client, "POST", f"/deals/{deal['id']}/comment", json={"author": "max", "body": "после"})

    conn = store(home)
    try:
        bodies = [str(row[0]) for row in conn.execute("SELECT body FROM comments ORDER BY id")]
    finally:
        conn.close()
    assert bodies == ["до сбоя"]


def test_store_write_failure_rolls_back_event(
    home: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Обратная половина инварианта 2: событие легло, запись store упала — откат обоих.

    Первые два теста проверяют «событие не легло → нет строки». Здесь наоборот:
    подменяется `deals._set_status` — единственная запись строки дела из `_apply`,
    — и после падения события `blocked` в журнале быть не должно, а `status`
    обязан остаться прежним. Иначе транзакция «наполовину»: журнал говорит
    «заблокировано», а дело стоит в очереди.
    """
    deal = create(client)
    assert deal["status"] == "ready"

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("store недоступен")

    monkeypatch.setattr(deals, "_set_status", boom)
    with pytest.raises(RuntimeError, match="store недоступен"):
        mutate(client, "POST", f"/deals/{deal['id']}/block", json={"kind": "needs_input", "reason": "нет ТЗ"})

    assert kinds(home, deal["id"]) == ["created"]
    assert deal_rows(home)[0]["status"] == "ready"


# ------------------------------------------------------- идемпотентность (3)


def test_repeat_comment_adds_no_events(home: Path, client: TestClient) -> None:
    """Повтор того же комментария: 0 событий, вторая строка не пишется."""
    deal = create(client)
    first = mutate(client, "POST", f"/deals/{deal['id']}/comment", json={"author": "max", "body": "проверка"})
    second = mutate(client, "POST", f"/deals/{deal['id']}/comment", json={"author": "max", "body": "проверка"})

    assert first["mutation"] == {"events": 1, "idempotent": False}
    assert second["mutation"] == {"events": 0, "idempotent": True}
    assert kinds(home, deal["id"]) == ["created", "commented"]
    assert len(second["comments"]) == 1


def test_repeat_complete_and_block_add_no_events(home: Path, client: TestClient) -> None:
    """Повтор `complete`/`block` с теми же аргументами — тот же ответ без событий."""
    deal = create(client)
    mutate(client, "POST", f"/deals/{deal['id']}/complete", json={"summary": "ok"})
    again = mutate(client, "POST", f"/deals/{deal['id']}/complete", json={"summary": "ok"})
    assert again["mutation"] == {"events": 0, "idempotent": True}
    assert kinds(home, deal["id"]) == ["created", "completed"]

    other = create(client, title="второе")
    mutate(client, "POST", f"/deals/{other['id']}/block", json={"kind": "needs_input", "reason": "нет ТЗ"})
    repeat = mutate(client, "POST", f"/deals/{other['id']}/block", json={"kind": "needs_input", "reason": "нет ТЗ"})
    assert repeat["mutation"] == {"events": 0, "idempotent": True}
    assert kinds(home, other["id"]) == ["created", "blocked"]


def test_same_arguments_from_different_sources_are_different_facts(home: Path, client: TestClient) -> None:
    """`cli` и `api` — разные источники: один текст от них это два события."""
    deal = create(client)
    conn = store(home)
    try:
        deals.add_comment(conn, deal["id"], author="max", body="проверка", source="cli")
    finally:
        conn.close()

    assert kinds(home, deal["id"]) == ["created", "commented"]
    keys = [event["idempotency_key"] for event in events(home, deal["id"])]
    assert len(set(keys)) == len(keys)
    assert any(key.startswith("cli:") for key in keys)
    assert any(key.startswith("api:") for key in keys)


def test_repeat_unblock_is_idempotent_and_foreign_unblock_is_error(
    home: Path, client: TestClient
) -> None:
    """Повтор снятия блока — 0 событий; снятие без блока другим автором — ошибка.

    Два случая различаются ответом: «уже снято этим же вызовом» и «снимать нечего»
    нельзя отдавать одним кодом, иначе скрипт не поймёт, повтор это или ошибка.
    """
    deal = create(client)
    mutate(client, "POST", f"/deals/{deal['id']}/block", json={"kind": "needs_input", "reason": "нет ТЗ"})
    mutate(client, "POST", f"/deals/{deal['id']}/unblock", json={})
    again = mutate(client, "POST", f"/deals/{deal['id']}/unblock", json={})
    assert again["mutation"] == {"events": 0, "idempotent": True}
    assert kinds(home, deal["id"]) == ["created", "blocked", "unblocked"]

    response = client.post(f"/deals/{deal['id']}/unblock", json={"author": "кто-то"})
    assert response.status_code == 400
    assert "не в blocked" in response.json()["errors"][0]["reason"]


def test_archived_deal_is_not_reopened_by_commands(home: Path, client: TestClient) -> None:
    """`archived` — конечное состояние: команды жизни его не воскрешают."""
    deal = create(client)
    mutate(client, "PATCH", f"/deals/{deal['id']}", json={"status": "archived"})

    for path, payload in (
        ("complete", {"summary": "ok"}),
        ("claim", {"owner": "implementer"}),
        ("block", {"kind": "needs_input", "reason": "нет ТЗ"}),
        ("review", {"reviewer": "reviewer"}),
        ("changes", {"reason": "верни"}),
    ):
        response = client.post(f"/deals/{deal['id']}/{path}", json=payload)
        assert response.status_code == 400, (path, response.text)
        assert "archived" in response.json()["errors"][0]["reason"]

    assert kinds(home, deal["id"]) == ["created", "completed"]
    assert deal_rows(home)[0]["status"] == "archived"


def test_own_claim_without_expiry_is_not_dead_lease(
    home: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Своё дело: срок у `claim` не пишется, поэтому «протухший лизинг» не называется.

    `claim` своего дела пишет `claim_expires: NULL` (аренды в 0.0.3 нет), и через
    час после взятия дело не выдаётся за `dead_lease`: тот же ответ по тому же
    делу отдаёт `lease.state = "none"`, и два поля не должны противоречить.
    """
    deal = create(client)
    conn = store(home)
    try:
        deals.claim_deal(conn, deal["id"], owner="implementer", now=NOW)
    finally:
        conn.close()
    monkeypatch.setattr(plugin_api, "_now_sec", lambda: NOW + 3600)

    waiting = waiting_of(client, deal["id"])

    assert waiting["state"] != "dead_lease"
    assert "dead_lease" not in waiting["evidence"]
    assert now_deals(client)[deal["id"]]["waiting"]["state"] != "dead_lease"


# ----------------------------------------------------------------- ревью (4)


def test_review_then_collect_writes_verdict_and_close(home: Path, client: TestClient) -> None:
    """Ревью пройдено: `collected` и `completed` — два факта одной транзакции.

    Вердикт и закрытие — разные события: склеив их, потеряли бы, что ревью было.
    Повтор той же команды не дописывает ни одного из двух.
    """
    deal = create(client)
    review = mutate(
        client, "POST", f"/deals/{deal['id']}/complete", json={"summary": "на ревью", "review": True}
    )
    assert review["status"] == "review"
    assert review["deal"]["waiting"]["state"] == "wait_human"
    assert review["deal"]["waiting"]["human_kind"] == "status"

    collected = mutate(client, "POST", f"/deals/{deal['id']}/complete", json={"summary": "принято"})
    assert collected["status"] == "done"
    assert collected["mutation"] == {"events": 2, "idempotent": False}
    assert kinds(home, deal["id"]) == ["created", "review_requested", "collected", "completed"]

    again = mutate(client, "POST", f"/deals/{deal['id']}/complete", json={"summary": "принято"})
    assert again["mutation"] == {"events": 0, "idempotent": True}
    assert kinds(home, deal["id"]) == ["created", "review_requested", "collected", "completed"]


def test_changes_requested_waits_for_executor_not_human(home: Path, client: TestClient) -> None:
    """Возврат на доработку — `wait_slot` с причиной, а не «ждёт человека».

    Решение человек уже принял; ждёт дело исполнителя. Проверяется и приоритет:
    `claim` перебивает этот факт, чего `wait_human` не позволил бы.
    """
    deal = create(client)
    mutate(client, "POST", f"/deals/{deal['id']}/review", json={"reviewer": "reviewer"})
    mutate(client, "POST", f"/deals/{deal['id']}/changes", json={"reason": "нет тестов"})

    waiting = waiting_of(client, deal["id"])
    assert waiting["state"] == "wait_slot"
    assert waiting["human_kind"] is None
    assert "нет тестов" in waiting["evidence"]

    mutate(client, "POST", f"/deals/{deal['id']}/claim", json={"owner": "implementer"})
    assert waiting_of(client, deal["id"])["state"] != "wait_slot"


# --------------------------------------------------------------- гейт (5)
def test_child_ready_only_when_parent_done(home: Path, client: TestClient) -> None:
    """Ребёнок `todo`, пока родитель не закрыт; закрытие — событие `gate_opened`."""
    parent = create(client, title="родитель")
    child = create(client, title="ребёнок", parents=[parent["id"]])
    assert child["status"] == "todo"
    assert now_deals(client)[child["id"]]["waiting"]["state"] == "wait_gate"

    mutate(client, "POST", f"/deals/{parent['id']}/complete", json={"summary": "ok"})

    assert kinds(home, child["id"]) == ["created", "gate_opened"]
    opened = events(home, child["id"])[-1]
    assert opened["payload"] == {"from": "todo", "to": "ready", "parent": parent["id"]}
    assert now_deals(client)[child["id"]]["status"] == "ready"


def test_child_created_under_closed_parent_is_ready_not_wait_gate(
    home: Path, client: TestClient
) -> None:
    """Ребёнок, заведённый при уже закрытом родителе, не висит в `wait_gate`.

    `create` ставит `status=ready` по открытому гейту, и переход обязан быть
    фактом: снимок `created` говорит только «родители были», а не «гейт закрыт»,
    поэтому по нему fold называл `wait_gate` делу, готовому к работе.
    """
    parent = create(client, title="родитель")
    mutate(client, "POST", f"/deals/{parent['id']}/complete", json={"summary": "ok"})

    child = create(client, title="ребёнок", parents=[parent["id"]])

    assert child["status"] == "ready"
    assert kinds(home, child["id"]) == ["created", "gate_opened"]
    opened = events(home, child["id"])[-1]
    assert opened["payload"] == {
        "from": "todo",
        "to": "ready",
        "parent": parent["id"],
        "cause": "created",
    }
    assert now_deals(client)[child["id"]]["waiting"]["state"] != "wait_gate"
    assert waiting_of(client, child["id"])["state"] != "wait_gate"


def test_child_of_two_closed_parents_names_the_last_closed_one(
    home: Path, client: TestClient
) -> None:
    """Два закрытых родителя: событие называет того, чьё закрытие открыло гейт последним."""
    first = create(client, title="родитель 1")
    second = create(client, title="родитель 2")
    conn = store(home)
    try:
        deals.complete_deal(conn, first["id"], summary="ok", now=NOW)
        deals.complete_deal(conn, second["id"], summary="ok", now=NOW + 60)
    finally:
        conn.close()

    child = create(client, title="ребёнок", parents=[first["id"], second["id"]])

    opened = events(home, child["id"])[-1]
    assert opened["kind"] == "gate_opened"
    assert opened["payload"]["parent"] == second["id"]


def test_parent_leaving_done_closes_gate_with_event(home: Path, client: TestClient) -> None:
    """Возврат родителя на доработку: ребёнок снова ждёт гейт, переход назван событием.

    Родитель уходит из `done` штатным путём (`changes` — вердикт ревьюера), и
    ребёнок не остаётся молча в `ready`: строка меняется вместе с событием
    `gate_closed`, а fold говорит `wait_gate` с именем родителя.
    """
    parent = create(client, title="родитель")
    child = create(client, title="ребёнок", parents=[parent["id"]])
    mutate(client, "POST", f"/deals/{parent['id']}/complete", json={"summary": "ok"})
    assert now_deals(client)[child["id"]]["status"] == "ready"

    mutate(client, "POST", f"/deals/{parent['id']}/changes", json={"reason": "не принято"})

    assert kinds(home, child["id"]) == ["created", "gate_opened", "gate_closed"]
    closed = events(home, child["id"])[-1]
    assert closed["payload"] == {"from": "ready", "to": "todo", "parent": parent["id"]}
    child_now = now_deals(client)[child["id"]]
    assert child_now["status"] == "todo"
    assert child_now["waiting"]["state"] == "wait_gate"
    assert parent["id"] in child_now["waiting"]["evidence"]


def test_gate_reopens_on_second_close_without_duplicate_events(home: Path, client: TestClient) -> None:
    """Закрытие родителя второй раз открывает гейт новым фактом, а не повтором."""
    parent = create(client, title="родитель")
    child = create(client, title="ребёнок", parents=[parent["id"]])
    mutate(client, "POST", f"/deals/{parent['id']}/complete", json={"summary": "первый раз"})
    mutate(client, "POST", f"/deals/{parent['id']}/changes", json={"reason": "вернули"})
    mutate(client, "POST", f"/deals/{parent['id']}/complete", json={"summary": "второй раз"})

    assert kinds(home, child["id"]) == ["created", "gate_opened", "gate_closed", "gate_opened"]
    assert now_deals(client)[child["id"]]["status"] == "ready"


# ---------------------------------------------------- источники read-only (1)


def test_mutations_never_touch_kanban_or_pool(home: Path, client: TestClient) -> None:
    """mtime и размер `kanban.db`/`state.db`/`pool.db` не меняются (инвариант 1)."""
    before = source_stats(home)

    parent = create(client, title="родитель")
    child = create(client, title="ребёнок", parents=[parent["id"]])
    mutate(client, "PATCH", f"/deals/{child['id']}", json={"title": "переименовано"})
    mutate(client, "POST", f"/deals/{child['id']}/comment", json={"author": "max", "body": "текст"})
    mutate(client, "POST", f"/deals/{child['id']}/block", json={"kind": "needs_input", "reason": "нет ТЗ"})
    mutate(client, "POST", f"/deals/{child['id']}/unblock", json={})
    mutate(client, "POST", f"/deals/{child['id']}/claim", json={"owner": "implementer"})
    mutate(client, "POST", f"/deals/{child['id']}/attach", files={"file": ("note.txt", b"hello", "text/plain")})
    mutate(client, "POST", f"/deals/{child['id']}/review", json={"reviewer": "reviewer"})
    mutate(client, "POST", f"/deals/{child['id']}/changes", json={"reason": "поправь"})
    mutate(client, "POST", f"/deals/{child['id']}/complete", json={"summary": "ok"})
    mutate(client, "POST", f"/deals/{parent['id']}/complete", json={"summary": "ok"})
    client.get("/now")
    client.get(f"/deal/{BOARD}/{child['id']}")
    client.get(f"/deal/{BOARD}/{KANBAN_DEAL}")

    assert source_stats(home) == before


# ----------------------------------------------------- срез и карточка дела


def test_own_deal_visible_in_now_and_leaves_after_complete(home: Path, client: TestClient) -> None:
    """Своё дело в `/now` рядом с kanban, отличается префиксом и уходит после `done`."""
    deal = create(client)

    listed = now_deals(client)
    assert set(listed) == {deal["id"], KANBAN_DEAL}
    assert listed[deal["id"]]["status"] == "ready"
    assert listed[KANBAN_DEAL]["waiting"]["state"] == "wait_human"
    boards = {entry["slug"]: entry for entry in client.get("/now").json()["boards"]}
    assert boards[BOARD]["own_deals"] == 1
    assert boards[BOARD]["deals"] == 2

    mutate(client, "POST", f"/deals/{deal['id']}/complete", json={"summary": "ok"})

    assert deal["id"] not in now_deals(client)
    card = client.get(f"/deal/{BOARD}/{deal['id']}").json()
    assert card["status"] == "done"
    assert card["terminal"] is True
    assert card["deal"] is None
    assert [event["kind"] for event in card["timeline"]] == ["created", "completed"]


def test_closed_deal_stays_closed_after_comment_attach_and_patch(
    home: Path, client: TestClient
) -> None:
    """Закрытое дело не «воскресает» комментарием, вложением и правкой (правила 2, 12).

    `commented`/`attached`/`patched` состояния не меняют, поэтому закрытость
    считается по последнему state-changing событию. Без этого fold уходил в
    `no_live_fact` с `active: True`, и карточка в одном ответе говорила
    `terminal: true` и показывала состояние ожидания «status=done: нет живых
    фактов» — ложь на экране.
    """
    deal = create(client)
    mutate(client, "POST", f"/deals/{deal['id']}/complete", json={"summary": "готово"})
    mutate(client, "POST", f"/deals/{deal['id']}/comment", json={"author": "max", "body": "спасибо"})
    mutate(
        client, "POST", f"/deals/{deal['id']}/attach", files={"file": ("note.txt", b"hello", "text/plain")}
    )
    patched = mutate(client, "PATCH", f"/deals/{deal['id']}", json={"title": "переименовано после закрытия"})

    assert kinds(home, deal["id"]) == ["created", "completed", "commented", "attached", "patched"]
    assert patched["status"] == "done"
    assert patched["mutation"] == {"events": 1, "idempotent": False}
    # `patched` — правка спецификации, и её payload называет поля, а не состояние
    assert events(home, deal["id"])[-1]["payload"] == {"fields": ["title"]}
    assert journal.fold(events(home, deal["id"]))["active"] is False

    assert deal["id"] not in now_deals(client)

    card = client.get(f"/deal/{BOARD}/{deal['id']}").json()
    # Закрытость и отсутствие состояния согласованы в одном ответе: дело не
    # активно (у него нет `deal.waiting`), и терминальность это подтверждает.
    assert card["terminal"] is True
    assert card["deal"] is None
    assert "wait_slot" not in json.dumps(card, ensure_ascii=False)


def test_archived_patch_keeps_closed_deal_closed(home: Path, client: TestClient) -> None:
    """Обратный случай: `patch(status=archived)` закрывает честно, а не «воскрешает».

    Архив — state-changing факт (тот же `completed` со `status="archived"`),
    поэтому закрытость не снимается, а переносится: карточка говорит
    `terminal: true`, `status` отражает архив, состояния ожидания нет.
    """
    deal = create(client)
    mutate(client, "POST", f"/deals/{deal['id']}/complete", json={"summary": "готово"})
    archived = mutate(client, "PATCH", f"/deals/{deal['id']}", json={"status": "archived"})

    assert archived["status"] == "archived"
    assert kinds(home, deal["id"]) == ["created", "completed", "completed"]
    assert events(home, deal["id"])[-1]["payload"] == {"status": "archived"}
    folded = journal.fold(events(home, deal["id"]))
    assert folded["active"] is False
    assert "status=archived" in folded["evidence"]

    assert deal["id"] not in now_deals(client)
    card = client.get(f"/deal/{BOARD}/{deal['id']}").json()
    assert card["status"] == "archived"
    assert card["terminal"] is True
    assert card["deal"] is None


def test_deal_card_carries_comments_and_attachments(home: Path, client: TestClient) -> None:
    """`GET /deal`: комментарии и вложения своего дела — часть карточки."""
    deal = create(client)
    mutate(client, "POST", f"/deals/{deal['id']}/comment", json={"author": "max", "body": "первый"})
    attached = mutate(
        client, "POST", f"/deals/{deal['id']}/attach", files={"file": ("note.txt", b"hello", "text/plain")}
    )

    card = client.get(f"/deal/{BOARD}/{deal['id']}").json()
    assert [item["body"] for item in card["comments"]] == ["первый"]
    assert [item["filename"] for item in card["attachments"]] == ["note.txt"]
    attachment = card["attachments"][0]
    assert Path(attachment["path"]).read_bytes() == b"hello"
    assert attachment["size_bytes"] == 5
    assert attached["attachment"]["path"] == attachment["path"]
    # хеш содержимого — в имени файла и в payload события: одинаковый файл ложится
    # один раз, а строка `attachments` ссылается на тот же путь
    digest = events(home, deal["id"])[-1]["payload"]["sha256"]
    assert Path(attachment["path"]).name.startswith(digest)


def test_attach_repeat_keeps_one_file_and_one_event(home: Path, client: TestClient) -> None:
    """Тот же файл второй раз: 0 событий, одна строка, один файл на диске."""
    deal = create(client)
    mutate(client, "POST", f"/deals/{deal['id']}/attach", files={"file": ("note.txt", b"hello", "text/plain")})
    again = mutate(
        client, "POST", f"/deals/{deal['id']}/attach", files={"file": ("note.txt", b"hello", "text/plain")}
    )

    assert again["mutation"] == {"events": 0, "idempotent": True}
    assert kinds(home, deal["id"]) == ["created", "attached"]
    files = list((home / "duty" / "attachments" / deal["id"]).iterdir())
    assert len(files) == 1


def test_patch_cannot_set_lifecycle_status(home: Path, client: TestClient) -> None:
    """Через `PATCH` статус жизни не ставится: `done`/`review` — команды, не поле."""
    deal = create(client)
    response = client.patch(f"/deals/{deal['id']}", json={"status": "done"})

    assert response.status_code == 400
    assert "done" in response.json()["errors"][0]["reason"]
    assert deal_rows(home)[0]["status"] == "ready"

    archived = mutate(client, "PATCH", f"/deals/{deal['id']}", json={"status": "archived"})
    assert archived["status"] == "archived"
    assert archived["terminal"] is True


def test_unknown_deal_is_404_not_500(client: TestClient) -> None:
    """Мутация по несуществующему делу — 404 с причиной в `errors[]`."""
    for method, path, kwargs in (
        ("POST", "/deals/d_00000000/comment", {"json": {"body": "текст"}}),
        ("POST", "/deals/d_00000000/unblock", {"json": {}}),
        ("PATCH", "/deals/d_00000000", {"json": {"title": "имя"}}),
    ):
        response = client.request(method, path, **kwargs)
        assert response.status_code == 404, (path, response.text)
        assert response.json()["errors"]


def test_kanban_deal_is_not_mutable_through_store_routes(client: TestClient) -> None:
    """`t_…` не мутируется роутами своего store: префикс — граница владения."""
    response = client.post(f"/deals/{KANBAN_DEAL}/comment", json={"author": "max", "body": "текст"})

    assert response.status_code in (400, 404)
    assert response.json()["errors"]


# ---------------------------------------- цикл вопроса своего дела (находка)


def own_deal_with_question(client: TestClient, text: str = "Вопрос по делу") -> tuple[str, int]:
    """Своё дело плюс живой вопрос по нему через роут `/ask`."""
    deal = create(client)
    response = client.post(f"/ask/{BOARD}/{deal['id']}", json={"text": text})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    return deal["id"], int(body["question_id"])


def pool_rows(home: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM questions ORDER BY id")]
    finally:
        conn.close()


def close_pool_question(home: Path, question_id: int, *, answer: str | None) -> None:
    """Закрыть вопрос штатным путём пула: `answered` с ответом или `expired`."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        if answer is None:
            conn.execute("UPDATE questions SET status='expired' WHERE id = ?", (question_id,))
        else:
            conn.execute(
                "UPDATE questions SET status='answered', answer=?, answered_via='manual', "
                "answered_at='2026-11-03T10:00:00.000Z' WHERE id = ?",
                (answer, question_id),
            )
        conn.commit()
    finally:
        conn.close()


def sync_answers(home: Path) -> dict[str, Any]:
    """Проход `sync_answers` с тем же соединением, что у ингеста."""
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        return ask.sync_answers(conn, home)
    finally:
        conn.close()


def waiting_of(client: TestClient, deal_id: str) -> dict[str, Any]:
    card = client.get(f"/deal/{BOARD}/{deal_id}").json()
    assert card["deal"] is not None, card
    return card["deal"]["waiting"]


def test_own_deal_question_uses_t_prefixed_session_key(home: Path, client: TestClient) -> None:
    """Вопрос своего дела: пул видит ключ `t_d_…`, журнал — id дела `d_…`.

    Это и есть однозначный маппинг: `ask()` пишет факт под тем же id, который
    выведет `sync_answers`, иначе ответ уехал бы под `t_d_…`.
    """
    deal_id, question_id = own_deal_with_question(client)

    row = pool_rows(home)[0]
    assert row["id"] == question_id
    assert row["session_key"] == f"t_{deal_id}"
    assert ask.deal_id_of(row["session_key"]) == deal_id
    assert ask.session_key(deal_id) == f"t_{deal_id}"

    asked = [event for event in events(home, deal_id) if event["kind"] == "question_asked"]
    assert len(asked) == 1
    assert asked[0]["payload"]["session_key"] == f"t_{deal_id}"
    assert asked[0]["payload"]["question_id"] == question_id


def test_own_deal_question_clears_after_answer(home: Path, client: TestClient) -> None:
    """Цикл `d_…`: вопрос → ответ в пуле → `sync_answers` → ожидание снято."""
    deal_id, question_id = own_deal_with_question(client)
    waiting = waiting_of(client, deal_id)
    assert waiting["state"] == "wait_human"
    assert waiting["human_kind"] == "question"

    close_pool_question(home, question_id, answer="делай так")
    report = sync_answers(home)
    assert report["appended"] == 1

    answered = [event for event in events(home, deal_id) if event["kind"] == "question_answered"]
    assert len(answered) == 1
    assert answered[0]["payload"]["question_id"] == question_id
    waiting = waiting_of(client, deal_id)
    assert waiting["human_kind"] != "question", waiting
    assert waiting["state"] == "wait_slot"


def test_own_deal_question_clears_after_expiry(home: Path, client: TestClient) -> None:
    """Истечение TTL закрывает вопрос так же: дело не ждёт ответа, которого нет."""
    deal_id, question_id = own_deal_with_question(client)
    close_pool_question(home, question_id, answer=None)
    assert sync_answers(home)["appended"] == 1

    answered = [event for event in events(home, deal_id) if event["kind"] == "question_answered"]
    assert answered[0]["payload"]["answered_via"] == "expired"
    assert waiting_of(client, deal_id)["human_kind"] != "question"


def test_own_deal_live_question_visible_in_now(home: Path, client: TestClient) -> None:
    """`/now` для своего дела с живым вопросом говорит «ждёт ответа», а не «вопросов нет»."""
    deal_id, _ = own_deal_with_question(client)

    listed = now_deals(client)[deal_id]
    assert listed["waiting"]["state"] == "wait_human"
    assert listed["waiting"]["human_kind"] == "question"
    assert "вопрос" in listed["waiting"]["evidence"].lower()


def test_own_deal_question_repeat_returns_same_question(home: Path, client: TestClient) -> None:
    """Повторный `ask` по тому же делу не заводит второй вопрос и второе событие."""
    deal_id, question_id = own_deal_with_question(client)
    again = client.post(f"/ask/{BOARD}/{deal_id}", json={"text": "другой текст"}).json()

    assert int(again["question_id"]) == question_id
    assert again["deduped"] is True
    assert len(pool_rows(home)) == 1
    assert kinds(home, deal_id).count("question_asked") == 1


def test_late_answer_does_not_revive_closed_own_deal(home: Path, client: TestClient) -> None:
    """Норма блокера FIX-W5c: поздний ответ на вопрос не воскрешает закрытое дело.

    Штатный хвост собственного цикла вопросов: `create → ask → complete` и только
    потом `sync_answers` (ответ пришёл, когда дело уже завершили). `question_answered`
    не входит в state-changing виды, поэтому закрытость читается по `completed`:
    `fold` не активен, `GET /deal` отдаёт `terminal: true` и `deal: None`, слова
    `wait_slot` в ответе нет, а в `/now` дело не появляется.
    """
    deal_id, question_id = own_deal_with_question(client)
    mutate(client, "POST", f"/deals/{deal_id}/complete", json={"summary": "готово"})

    close_pool_question(home, question_id, answer="делай так")
    assert sync_answers(home)["appended"] == 1

    # Ответ лёг последней строкой журнала — именно этот порядок и ломал закрытость.
    assert kinds(home, deal_id)[-1] == "question_answered"
    folded = journal.fold(events(home, deal_id), now=NOW)
    assert folded["active"] is False
    assert folded["state"] is None
    assert folded["reason"] == "closed"

    response = client.get(f"/deal/{BOARD}/{deal_id}")
    assert response.status_code == 200, response.text
    card = response.json()
    assert card["terminal"] is True
    assert card["deal"] is None, card["deal"]
    assert "wait_slot" not in json.dumps(card, ensure_ascii=False)
    assert deal_id not in now_deals(client)


def test_kanban_deal_question_cycle_still_works(home: Path, client: TestClient) -> None:
    """`t_…`-путь не сломан: тот же цикл вопроса для наблюдаемого дела."""
    response = client.post(f"/ask/{BOARD}/{KANBAN_DEAL}", json={"text": "Вопрос по kanban-делу"})
    assert response.status_code == 200, response.text
    question_id = int(response.json()["question_id"])

    row = pool_rows(home)[0]
    assert row["session_key"] == KANBAN_DEAL
    assert ask.deal_id_of(row["session_key"]) == KANBAN_DEAL

    waiting = waiting_of(client, KANBAN_DEAL)
    assert waiting["human_kind"] == "question"

    close_pool_question(home, question_id, answer="ответ")
    assert sync_answers(home)["appended"] == 1
    assert [event["kind"] for event in events(home, KANBAN_DEAL)].count("question_answered") == 1
    assert waiting_of(client, KANBAN_DEAL)["human_kind"] != "question"


def test_unparsed_session_key_is_reported_not_skipped(home: Path, client: TestClient) -> None:
    """Неразобранный `session_key` — строка в `errors[]`, а не тихий `continue`."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        conn.execute(
            "INSERT INTO questions (session_key, text, origin, status, critical, answer, answered_via) "
            "VALUES ('чужой-ключ', 'вопрос', 'duty', 'answered', 0, 'ок', 'manual')"
        )
        conn.commit()
    finally:
        conn.close()

    report = sync_answers(home)
    assert report["appended"] == 0
    assert report["errors"], report
    assert "чужой-ключ" in json.dumps(report["errors"], ensure_ascii=False)


# ------------------------------------------------- таймлайн попыток (W18)
#
# Попытки своего дела видны в консоли: они строятся по журналу
# (`attempt_started` / `attempt_progress` / `attempt_ended`) и лизингу, а не по
# `ps`. Проверяется форма (`stage`, `attempt`, `started_at`, `ended_at`,
# `outcome`, `kind`, `reason`, `base`, `commit`, `pid`, `reaped`, `progress`),
# исходы (`ok`/`failed(kind)`/`running`/`orphan`/`unknown`), настоящая причина
# пустого таймлайна и то, что kanban-дело по-прежнему читается из `task_runs`.

BASE_ONE = "8d31edee15f8b15136b4ff1def2a49709621975d"
COMMIT_ONE = "8a2d5cb6bb4a380d53fdcf77af758f0b6e71a598"


@pytest.fixture()
def frozen(monkeypatch: pytest.MonkeyPatch) -> int:
    """Один `now` у роутов: длительность и возраст попыток не зависят от часов."""
    monkeypatch.setattr(plugin_api, "_now_sec", lambda: NOW)
    return NOW


def append_journal(home: Path, deal_id: str, items: list[tuple[str, int, dict[str, Any]]]) -> None:
    """Дописать факты журнала так, как их пишет движок: через `journal.append`."""
    conn = store(home)
    try:
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
                    "idempotency_key": f"test:{deal_id}:{kind}:{ts}:{index}",
                }
                for index, (kind, ts, payload) in enumerate(items)
            ],
        )
    finally:
        conn.close()


def set_lease(home: Path, deal_id: str, stage: str, attempt: int, *, expires_at: int) -> None:
    """Строка лизинга этапа — как её держит движок (`runner.lease_of` читает её же)."""
    conn = store(home)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (deal_id, stage, attempt, 4242, expires_at - 60, expires_at),
            )
    finally:
        conn.close()


def three_attempts(deal_id: str) -> list[tuple[str, int, dict[str, Any]]]:
    """Журнал дела с тремя попытками: `impl` ok, `verify` ok, `review` failed(contract).

    Форма payload — та же, что пишет `runner.spawn`: `base` (от чего этап
    отводился), `commit` (что произвёл), `lease_expires` при старте. Плюс шаги
    прогресса — у них нет `run_id`, пара ищется по `(stage, attempt)`.
    """
    return [
        ("attempt_started", NOW - 600, {
            "run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1, "pid": 111,
            "base": BASE_ONE, "lease_expires": NOW - 400, "contract": {"role": "implementer"},
        }),
        ("attempt_progress", NOW - 590, {"stage": "impl", "attempt": 1, "seq": 1, "phase": "tool_use"}),
        ("attempt_progress", NOW - 580, {"stage": "impl", "attempt": 1, "seq": 2, "phase": "tool_result"}),
        ("attempt_ended", NOW - 500, {
            "run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1, "outcome": "done",
            "kind": None, "reason": "", "base": BASE_ONE, "commit": COMMIT_ONE,
        }),
        ("attempt_started", NOW - 480, {
            "run_id": f"{deal_id}:verify#1", "stage": "verify", "attempt": 1, "pid": 222,
            "base": COMMIT_ONE, "lease_expires": NOW - 280, "contract": {"role": "tester"},
        }),
        ("attempt_ended", NOW - 420, {
            "run_id": f"{deal_id}:verify#1", "stage": "verify", "attempt": 1, "outcome": "done",
            "kind": None, "reason": "", "base": COMMIT_ONE, "commit": COMMIT_ONE,
        }),
        ("attempt_started", NOW - 400, {
            "run_id": f"{deal_id}:review#1", "stage": "review", "attempt": 1, "pid": 333,
            "base": COMMIT_ONE, "lease_expires": NOW - 200, "contract": {"role": "reviewer"},
        }),
        ("attempt_ended", NOW - 300, {
            "run_id": f"{deal_id}:review#1", "stage": "review", "attempt": 1, "outcome": "failed",
            "kind": "contract", "reason": "result.json нет", "base": COMMIT_ONE, "commit": None,
        }),
    ]


def test_own_attempts_come_from_the_journal(client: TestClient, home: Path, frozen: int) -> None:
    """Три попытки в журнале → count=3, порядок по старту и поля таймлайна на месте."""
    deal = create(client)
    append_journal(home, deal["id"], three_attempts(deal["id"]))

    body = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()

    assert body["count"] == 3
    assert body["note"] == ""
    assert [attempt["stage"] for attempt in body["attempts"]] == ["impl", "verify", "review"]
    assert [attempt["attempt"] for attempt in body["attempts"]] == [1, 1, 1]
    first = body["attempts"][0]
    assert first["started_at"] == NOW - 600
    assert first["ended_at"] == NOW - 500
    assert first["duration_sec"] == 100
    assert first["profile"] == "implementer"
    assert first["pid"] == 111
    assert first["run_id"] == f"{deal['id']}:impl#1"


def test_own_attempts_are_ordered_by_start_not_by_row(client: TestClient, home: Path) -> None:
    """Порядок — по старту, а не по `id` строки: ingest может отставать по времени."""
    deal = create(client)
    conn = store(home)
    try:
        with conn:
            # id 900 — стартовала позже, id 901 — раньше: по `id` порядок обратный.
            for ident, stage, ts in ((900, "impl", NOW - 100), (901, "verify", NOW - 500)):
                conn.execute(
                    "INSERT INTO events (id, ts, recorded_at, deal_id, board, kind, payload, source, "
                    "idempotency_key) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ident, ts, ts, deal["id"], BOARD, "attempt_started",
                        json.dumps({"run_id": f"{deal['id']}:{stage}#1", "stage": stage, "attempt": 1}),
                        "engine", f"test:{deal['id']}:{stage}",
                    ),
                )
    finally:
        conn.close()

    body = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()

    assert [attempt["stage"] for attempt in body["attempts"]] == ["verify", "impl"]


def test_own_attempt_outcomes_and_failure_class(client: TestClient, home: Path, frozen: int) -> None:
    """Исходы доезжают словами движка: `done` — успех, `failed(contract)` — отказ гейта."""
    deal = create(client)
    append_journal(home, deal["id"], three_attempts(deal["id"]))

    impl, _, review = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()["attempts"]

    assert (impl["outcome"], impl["kind"], impl["status"]) == ("done", "", "completed")
    assert (review["outcome"], review["kind"], review["status"]) == ("failed", "contract", "failed")
    assert review["reason"] == "result.json нет"
    # Страница печатает причину отказа из `error` — там то же слово, что в `reason`.
    assert review["error"] == review["reason"]


def test_own_attempt_base_commit_and_progress_arrive(client: TestClient, home: Path, frozen: int) -> None:
    """`base`/`commit`/`progress` — факты о дереве и шагах попытки, а не догадки."""
    deal = create(client)
    append_journal(home, deal["id"], three_attempts(deal["id"]))

    impl, verify, review = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()["attempts"]

    assert (impl["base"], impl["commit"]) == (BASE_ONE, COMMIT_ONE)
    assert impl["progress"] == 2
    assert verify["commit"] == COMMIT_ONE
    assert review["commit"] == ""  # неудача коммита не производит — поля нет, а не «нет коммита»
    assert review["progress"] == 0


def test_own_open_attempt_with_live_lease_is_running(client: TestClient, home: Path, frozen: int) -> None:
    """Незакрытая попытка с живым лизингом — `running`, длительность = возраст."""
    deal = create(client)
    append_journal(home, deal["id"], [
        ("attempt_started", NOW - 120, {
            "run_id": f"{deal['id']}:impl#1", "stage": "impl", "attempt": 1, "pid": 999,
            "lease_expires": NOW - 60,  # срок из старта уже прошёл…
        }),
    ])
    # …но лизинг продлён heartbeat'ом: живой срок — в таблице store.
    set_lease(home, deal["id"], "impl", 1, expires_at=NOW + 600)

    body = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()

    attempt = body["attempts"][0]
    assert attempt["status"] == "running"
    assert attempt["outcome"] == ""
    assert attempt["ended_at"] is None
    assert attempt["duration_sec"] == 120
    assert attempt["reason"] == ""


def test_own_open_attempt_with_expired_lease_is_orphan(client: TestClient, home: Path, frozen: int) -> None:
    """Лизинг истёк — попытка осиротела, и подсказка называет уборку."""
    deal = create(client)
    append_journal(home, deal["id"], [
        ("attempt_started", NOW - 600, {
            "run_id": f"{deal['id']}:impl#1", "stage": "impl", "attempt": 1, "pid": 999,
            "lease_expires": NOW - 300,
        }),
    ])

    attempt = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()["attempts"][0]

    assert attempt["status"] == "orphan"
    assert attempt["ended_at"] is None
    assert "bin/duty reap" in attempt["reason"]
    assert "лизинг истёк 300с назад" in attempt["reason"]


def test_own_open_attempt_without_lease_is_unknown(client: TestClient, home: Path, frozen: int) -> None:
    """Срока лизинга нет ни в журнале, ни в store — «неизвестно», а не «жив»."""
    deal = create(client)
    append_journal(home, deal["id"], [
        ("attempt_started", NOW - 60, {
            "run_id": f"{deal['id']}:impl#1", "stage": "impl", "attempt": 1, "pid": 999,
        }),
    ])

    attempt = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()["attempts"][0]

    assert attempt["status"] == "unknown"
    assert "неизвестно" in attempt["reason"]


def test_own_attempt_reaped_flag_arrives(client: TestClient, home: Path, frozen: int) -> None:
    """`reaped` доезжает: в консоли видно, что попытку закрыла уборка, а не исполнитель."""
    deal = create(client)
    append_journal(home, deal["id"], [
        ("attempt_started", NOW - 600, {
            "run_id": f"{deal['id']}:impl#1", "stage": "impl", "attempt": 1, "pid": 999,
            "lease_expires": NOW - 400,
        }),
        ("attempt_ended", NOW - 300, {
            "run_id": f"{deal['id']}:impl#1", "stage": "impl", "attempt": 1, "outcome": "failed",
            "kind": "infra", "reason": "процесс попытки не отчитался", "reaped": True,
        }),
    ])

    attempt = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()["attempts"][0]

    assert attempt["reaped"] is True
    assert attempt["kind"] == "infra"
    assert attempt["status"] == "failed"


def test_attempts_of_own_deal_without_attempts_name_the_real_reason(client: TestClient) -> None:
    """Своё дело без попыток: count=0 и настоящая причина, а не «спавна в 0.0.3 нет»."""
    deal = create(client)
    body = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()

    assert body["count"] == 0
    assert body["attempts"] == []
    assert body["task_id"] == deal["id"]
    assert "попыток не было" in body["note"]
    assert "0.0.3" not in body["note"]


def test_kanban_attempts_still_come_from_task_runs(client: TestClient, home: Path, frozen: int) -> None:
    """Регрессия: `t_`-дело по-прежнему читается из `task_runs` доски, а не из журнала."""
    board = home / "kanban" / "boards" / BOARD / "kanban.db"
    conn = sqlite3.connect(board)
    try:
        with conn:
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, summary, error, started_at, ended_at) "
                "VALUES (?, 'implementer', 'awaiting_merge', 'completed', 'готово', NULL, ?, ?)",
                (KANBAN_DEAL, NOW - 300, NOW - 120),
            )
    finally:
        conn.close()

    body = client.get(f"/duty/{BOARD}/{KANBAN_DEAL}/attempts").json()

    assert body["count"] == 1
    assert body["note"] == ""
    attempt = body["attempts"][0]
    assert attempt["profile"] == "implementer"
    assert attempt["outcome"] == "completed"
    assert attempt["start"] == NOW - 300
    assert attempt["duration_sec"] == 180


# ------------------------------------------------------------------ CLI (5)


def test_cli_create_comment_complete(home: Path, client: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI и роуты ходят в один модуль: `duty create/comment/complete` видны в store."""
    from importlib import util as importlib_util

    cli_path = Path(__file__).resolve().parents[1] / "dashboard" / "cli.py"
    spec = importlib_util.spec_from_file_location("hermes_duty_cli_deals", cli_path)
    assert spec is not None and spec.loader is not None
    cli = importlib_util.module_from_spec(spec)
    sys.modules[spec.name] = cli
    spec.loader.exec_module(cli)

    assert cli.main(["create", "--repo", BOARD, "--title", "из терминала", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    deal_id = payload["id"]
    assert deal_id.startswith("d_")
    assert cli.main(["comment", deal_id, "проверка"]) == 0
    assert cli.main(["complete", deal_id, "--summary", "ok"]) == 0
    capsys.readouterr()

    assert kinds(home, deal_id) == ["created", "commented", "completed"]
    assert deal_rows(home)[0]["status"] == "done"
    assert cli.main(["comment", "d_00000000", "нет такого"]) == 1


# -------------------------------------------- внешнее имя дела (W8a)


def cli_module() -> Any:
    """`dashboard/cli.py` — как его грузит плагин: по пути, без пакета."""
    name = "hermes_duty_cli_deals"
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parents[1] / "dashboard" / "cli.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_create_with_external_ref_records_it_in_row_and_journal(
    home: Path, client: TestClient
) -> None:
    """Внешнее имя дела — в строке store, в снимке `created` и в карточке."""
    body = create(client, external_ref="jira:RP-14080")

    assert body["created"] is True
    assert "existing" not in body
    assert body["external_ref"] == "jira:RP-14080"
    assert deal_rows(home)[0]["external_ref"] == "jira:RP-14080"

    created = events(home, body["id"])[0]
    assert created["payload"]["external_ref"] == "jira:RP-14080"
    card = client.get(f"/deal/{BOARD}/{body['id']}").json()
    assert card["external_ref"] == "jira:RP-14080"


def test_second_create_with_live_external_ref_returns_same_deal(
    home: Path, client: TestClient
) -> None:
    """Повторное «продолжить» при живом деле: тот же id, `created: false`, без событий."""
    first = create(client, external_ref="jira:RP-14080")
    second = create(client, external_ref="jira:RP-14080", title="нажали ещё раз")

    assert second["created"] is False
    assert second["existing"] is True
    assert second["id"] == first["id"]
    assert second["status"] == first["status"]
    assert first["id"] in second["reason"]
    assert second["mutation"] == {"events": 0, "idempotent": True}
    assert len(deal_rows(home)) == 1
    assert kinds(home, first["id"]) == ["created"]


def test_create_after_complete_is_new_deal_not_resurrection(home: Path, client: TestClient) -> None:
    """Закрытое дело не воскрешается: продолжение — новое дело, видно отдельно."""
    first = create(client, external_ref="jira:RP-14080")
    mutate(client, "POST", f"/deals/{first['id']}/complete", json={"summary": "пауза"})
    second = create(client, external_ref="jira:RP-14080")

    assert second["created"] is True
    assert "existing" not in second
    assert second["id"] != first["id"]

    rows = {row["id"]: row for row in deal_rows(home)}
    assert set(rows) == {first["id"], second["id"]}
    assert {row["external_ref"] for row in rows.values()} == {"jira:RP-14080"}
    assert rows[first["id"]]["status"] == "done"
    assert rows[second["id"]]["status"] == "ready"


def test_archived_deal_is_not_resurrected_by_external_ref(home: Path, client: TestClient) -> None:
    """`archived` — тоже закрытое: имя не возвращает дело с доски в работу."""
    first = create(client, external_ref="jira:RP-14080")
    mutate(client, "PATCH", f"/deals/{first['id']}", json={"status": "archived"})
    second = create(client, external_ref="jira:RP-14080")

    assert second["created"] is True
    assert second["id"] != first["id"]


def test_external_ref_dedups_only_on_its_own_board(home: Path, client: TestClient) -> None:
    """Внешнее имя ищется в пределах доски: на другой доске это другое дело."""
    other = "another"
    board = home / "kanban" / "boards" / other / "kanban.db"
    board.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(board)
    conn.executescript(KANBAN_SCHEMA)
    conn.commit()
    conn.close()

    first = create(client, external_ref="jira:RP-14080")
    second = create(client, repo=other, external_ref="jira:RP-14080")

    assert second["created"] is True
    assert second["id"] != first["id"]
    assert second["board"] == other


def test_dedup_window_lets_a_stale_live_deal_go(home: Path) -> None:
    """`dedup_window` — срок жизни имени: дело старше окна не переиспользуется."""
    conn = store(home)
    try:
        first = deals.create_deal(
            conn, repo=BOARD, title="первое", external_ref="jira:RP-7",
            now=NOW,
        )
        same = deals.create_deal(
            conn, repo=BOARD, title="повтор", external_ref="jira:RP-7",
            now=NOW + 10, dedup_window=60,
        )
        stale = deals.create_deal(
            conn, repo=BOARD, title="продолжение", external_ref="jira:RP-7",
            now=NOW + 600, dedup_window=60,
        )
    finally:
        conn.close()

    assert same["created"] is False
    assert same["deal"]["id"] == first["deal"]["id"]
    assert stale["created"] is True
    assert stale["deal"]["id"] != first["deal"]["id"]


def test_empty_external_ref_is_input_error(home: Path, client: TestClient) -> None:
    """Пустое имя — ошибка входа, а не второй способ сказать «без имени»."""
    response = client.post("/deals", json={"repo": BOARD, "title": "дело", "external_ref": "   "})

    assert response.status_code == 400
    assert "external_ref" in response.json()["errors"][0]["reason"]
    assert deal_rows(home) == []


def test_deal_without_external_ref_is_unchanged(home: Path, client: TestClient) -> None:
    """Регрессия: без имени `create` по-прежнему заводит новое дело на каждый вызов."""
    first = create(client)
    second = create(client)

    assert first["created"] is True
    assert second["created"] is True
    assert first["id"] != second["id"]
    assert {row["external_ref"] for row in deal_rows(home)} == {None}
    assert events(home, first["id"])[0]["payload"]["external_ref"] is None


def test_old_store_without_external_ref_column_is_still_readable(
    home: Path, client: TestClient
) -> None:
    """Чтение не мигрирует схему: старая `duty.db` читается, имени просто нет."""
    deal = create(client, external_ref="jira:RP-14080")
    raw = sqlite3.connect(deals.store_path(home))
    raw.execute("ALTER TABLE deals DROP COLUMN external_ref")
    raw.commit()
    raw.close()

    names = {item["id"] for item in client.get("/now").json()["deals"]}
    assert deal["id"] in names
    card = client.get(f"/deal/{BOARD}/{deal['id']}").json()
    assert card["external_ref"] is None

    # смотр экрана схему не доводит: колонки после чтения по-прежнему нет
    checked = sqlite3.connect(deals.store_path(home))
    columns = {row[1] for row in checked.execute("PRAGMA table_info(deals)")}
    checked.close()
    assert "external_ref" not in columns

    # запись доводит схему: колонка заводится миграцией при открытии на запись
    again = create(client, external_ref="jira:RP-14080", title="после миграции")
    assert again["created"] is True
    assert {row["external_ref"] for row in deal_rows(home)} == {None, "jira:RP-14080"}


def test_cli_create_with_external_ref_is_idempotent(
    home: Path, client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI: флаг `--external-ref` и `--json` с `deal_id`, `created`, `status`."""
    cli = cli_module()
    argv = [
        "create", "--repo", BOARD, "--title", "из терминала",
        "--external-ref", "jira:RP-14080", "--json",
    ]

    assert cli.main(argv) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["created"] is True
    assert first["deal_id"].startswith("d_")
    assert first["status"] == "ready"

    assert cli.main(argv) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] is False
    assert second["existing"] is True
    assert second["deal_id"] == first["deal_id"]
    assert len(deal_rows(home)) == 1
    assert kinds(home, first["deal_id"]) == ["created"]

    # человеческий вывод второго вызова называет повтор, а не создание
    assert cli.main(argv[:-1]) == 0
    text = capsys.readouterr().out
    assert "ДЕЛО УЖЕ ЕСТЬ" in text
    assert "jira:RP-14080" in text
    assert "нового дела нет" in text


# ------------------- попытка и лизинг своего дела в «Сейчас» (FIX-4 / B1, B2)
#
# Движок 0.0.4 спавнит попытки и держит лизинг (`runner.py`), поэтому у своего
# дела `attempt`/`lease` — факты журнала и store, а не постоянные `null`/`none`.
# Рядом — честная ветка: факта нет — это `no_signal`/`unknown`, а не «нет».

def append_attempt(
    home: Path,
    deal_id: str,
    *,
    stage: str = "impl",
    attempt: int = 1,
    pid: int,
    started_at: int,
    lease_expires: Any = None,
) -> None:
    """`attempt_started` своего дела — той же формой, что пишет `runner.step`."""
    conn = store(home)
    try:
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "kind": "attempt_started",
                    "ts": started_at,
                    "recorded_at": started_at,
                    "source": "engine",
                    "payload": {
                        "run_id": f"{deal_id}:{stage}#{attempt}",
                        "stage": stage,
                        "attempt": attempt,
                        "pid": pid,
                        "lease_expires": lease_expires,
                        "contract": {"role": "implementer"},
                    },
                    "idempotency_key": f"test:{deal_id}:{stage}:{attempt}",
                }
            ],
        )
    finally:
        conn.close()


def hold_lease(home: Path, deal_id: str, *, stage: str = "impl", attempt: int = 1, pid: int, expires_at: int) -> None:
    """Строка лизинга в store — то, что видит движок, взявший этап."""
    conn = store(home)
    try:
        conn.execute(
            "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) VALUES (?,?,?,?,?,?)",
            (deal_id, stage, attempt, pid, expires_at - 1800, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_own_deal_now_does_not_claim_the_engine_cannot_spawn(client: TestClient) -> None:
    """B1: `/now` своего дела не приписывает движку 0.0.3 — спавн и лизинг есть."""
    deal = create(client)

    listed = now_deals(client)[deal["id"]]

    evidence = listed["waiting"]["evidence"]
    assert listed["waiting"]["state"] == "wait_slot"
    assert "0.0.3" not in json.dumps(listed, ensure_ascii=False)
    assert "bin/duty run" in evidence
    assert "вручную" not in evidence


def test_own_deal_running_without_journal_fact_is_no_signal(home: Path, client: TestClient) -> None:
    """B1: `status=running` без живого факта журнала — `no_signal`, а не «попыток нет»."""
    deal = create(client)
    conn = store(home)
    try:
        conn.execute("UPDATE deals SET status = 'running' WHERE id = ?", (deal["id"],))
        conn.commit()
    finally:
        conn.close()

    listed = now_deals(client)[deal["id"]]

    assert listed["waiting"]["state"] == "no_signal"
    evidence = listed["waiting"]["evidence"]
    assert "идёт ли попытка — неизвестно" in evidence
    assert "0.0.3" not in evidence


def test_own_deal_live_attempt_and_lease_come_from_journal(home: Path, client: TestClient) -> None:
    """B2: живая попытка своего дела видна полем, а не постоянным `null`/`none`.

    Строка «Сейчас» показывает этап «impl · попытка 1 · идёт» из журнала; рядом
    та же попытка и её лизинг — из журнала и store, иначе одна карточка говорила
    бы два разных факта.
    """
    deal = create(client)
    moment = int(time.time())
    pid = os.getpid()
    append_attempt(home, deal["id"], pid=pid, started_at=moment - 90, lease_expires=moment + 300)
    hold_lease(home, deal["id"], pid=pid, expires_at=moment + 600)

    listed = now_deals(client)[deal["id"]]

    attempt = listed["attempt"]
    assert attempt is not None
    assert attempt["source"] == "journal"
    assert attempt["pid"] == pid
    assert attempt["pid_alive"] is True
    assert attempt["age_sec"] >= 90
    assert listed["stage"]["attempt"] == 1
    # Срок из store важнее срока из `attempt_started`: лизинг продлевается
    # heartbeat'ом, поэтому истекает через ~10 минут, а не через 5.
    assert listed["lease"]["state"] == "live"
    assert listed["lease"]["expires_in_sec"] > 300


def test_own_deal_attempt_without_lease_term_is_unknown_not_none(home: Path, client: TestClient) -> None:
    """B2: срока лизинга нет ни в store, ни в событии — `unknown`, а не «нет»."""
    deal = create(client)
    moment = int(time.time())
    append_attempt(home, deal["id"], pid=os.getpid(), started_at=moment - 30)

    listed = now_deals(client)[deal["id"]]

    assert listed["attempt"] is not None
    assert listed["lease"]["state"] == "unknown"
    assert listed["lease"]["expires_in_sec"] is None


def test_own_deal_with_closed_attempt_has_no_current_one(home: Path, client: TestClient) -> None:
    """B2: все попытки закрыты — идущей нет, и лизинг не выдумывается."""
    deal = create(client)
    moment = int(time.time())
    append_attempt(home, deal["id"], pid=os.getpid(), started_at=moment - 300, lease_expires=moment - 100)
    conn = store(home)
    try:
        journal.append(
            conn,
            [
                {
                    "deal_id": deal["id"],
                    "kind": "attempt_ended",
                    "ts": moment - 200,
                    "recorded_at": moment - 200,
                    "source": "engine",
                    "payload": {
                        "run_id": f"{deal['id']}:impl#1",
                        "stage": "impl",
                        "attempt": 1,
                        "outcome": "done",
                        "kind": None,
                        "reason": "",
                        "commit": "8a2d5cb6bb4a380d53fdcf77af758f0b6e71a598",
                    },
                    "idempotency_key": f"test:{deal['id']}:ended",
                }
            ],
        )
    finally:
        conn.close()

    listed = now_deals(client)[deal["id"]]

    assert listed["attempt"] is None
    assert listed["lease"]["state"] == "none"


# ------------------- «шага движка не было» — факт журнала (FIX-6 / остаток B1)
#
# Путь из вердикта RV-FINALb: попытки в журнале были, дело запарковано, человек
# снял блок (`unblock`/`decide continue`) → `status=ready`, живого факта нет.
# `fold` на таком журнале даёт `state=None`, и остаточная ветка говорила «шага
# движка ещё не было» **над** таймлайном тех самых попыток — одна команда называла
# шаги и отрицала их. Теперь фраза выбирается по числу попыток из журнала.


def end_attempt(
    home: Path,
    deal_id: str,
    *,
    stage: str = "impl",
    attempt: int = 1,
    started_at: int,
    outcome: str = "failed",
) -> None:
    """`attempt_ended` своего дела — той же формой, что пишет движок."""
    conn = store(home)
    try:
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "kind": "attempt_ended",
                    "ts": started_at + 120,
                    "recorded_at": started_at + 120,
                    "source": "engine",
                    "payload": {
                        "run_id": f"{deal_id}:{stage}#{attempt}",
                        "stage": stage,
                        "attempt": attempt,
                        "outcome": outcome,
                        "kind": "contract",
                        "reason": "result.json нет",
                        "base": None,
                        "commit": None,
                    },
                    "idempotency_key": f"test:{deal_id}:{stage}:{attempt}:ended",
                }
            ],
        )
    finally:
        conn.close()


def park_and_unblock(client: TestClient, deal_id: str) -> None:
    """Парковка движком и снятие блока человеком — ровно два факта из вердикта."""
    mutate(
        client,
        "POST",
        f"/deals/{deal_id}/block",
        json={"kind": "parked", "reason": "исчерпаны попытки: 2/2"},
    )
    mutate(client, "POST", f"/deals/{deal_id}/unblock", json={})


def test_own_deal_with_attempts_does_not_say_the_engine_never_started(
    home: Path, client: TestClient
) -> None:
    """FIX-6: попытки были — «шага движка ещё не было» неправда, и это один текст.

    Своё дело + `attempt_started`/`attempt_ended` ×2 + `blocked(parked)` +
    `unblocked` + `status=ready`: живого факта нет, но шаги движка в журнале есть.
    Доказательство называет именно это — и `deal`, и `/now` читают одну строку.
    """
    deal = create(client)
    moment = int(time.time())
    for number in (1, 2):
        started = moment - 3600 + number * 600
        append_attempt(
            home, deal["id"], attempt=number, pid=40_000 + number, started_at=started,
            lease_expires=started + 300,
        )
        end_attempt(home, deal["id"], attempt=number, started_at=started)
    park_and_unblock(client, deal["id"])

    assert client.get(f"/deal/{BOARD}/{deal['id']}").json()["status"] == "ready"
    listed = now_deals(client)[deal["id"]]

    evidence = listed["waiting"]["evidence"]
    assert listed["waiting"]["state"] == "wait_slot"
    assert "шага движка ещё не было" not in evidence
    assert "идущей попытки нет" in evidence
    assert "попыток в журнале: 2" in evidence
    assert "bin/duty run" in evidence
    # Тот же факт виден полем: идущей попытки нет, а закрытые — есть в таймлайне.
    assert listed["attempt"] is None
    timeline = client.get(f"/duty/{BOARD}/{deal['id']}/attempts").json()
    assert [row["attempt"] for row in timeline["attempts"]] == [1, 2]
    # Одна строка на обе поверхности: `/deal` не считает доказательство заново.
    card = client.get(f"/deal/{BOARD}/{deal['id']}").json()["deal"]
    assert card["waiting"] == listed["waiting"]


def test_own_deal_without_attempts_still_says_the_engine_never_started(
    home: Path, client: TestClient
) -> None:
    """FIX-6: у дела без попыток прежняя фраза остаётся — она про него и сказана."""
    deal = create(client)

    listed = now_deals(client)[deal["id"]]

    evidence = listed["waiting"]["evidence"]
    assert listed["waiting"]["state"] == "wait_slot"
    assert "шага движка ещё не было" in evidence
    assert "попыток в журнале" not in evidence
    assert "bin/duty run" in evidence
