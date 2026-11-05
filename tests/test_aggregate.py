"""Тесты агрегатора «сейчас» без досок (0.0.6): таксономия ожиданий по журналу.

Срез «сейчас» собирается из двух источников: строки своего store
(`dashboard/deals.py`) и фактов журнала (`dashboard/journal.py`) плюс таблицы
лизингов. Карточки kanban — наблюдение (`/cards`) и в срез дел не входят, поэтому
доски в фикстурах нет: тесты строят дело в store и пишут его факты журналом.

Фикстуры — маленькие `duty.db`/`pool.db` в `tmp_path` по реальной схеме; живые
БД не читаются и не пишутся: агрегатор получает корень Hermes через `HERMES_HOME`.
"""

from __future__ import annotations

import importlib.util
import itertools
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
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

journal = plugin_api.journal
deals = plugin_api.deals

# Фиксированное «сейчас»: все возраста в тестах считаются от него.
NOW = 1_800_000_000

# Счётчик ключей идемпотентности: у каждого факта свой, иначе `INSERT OR IGNORE`
# молча отбросит вторую попытку с тем же видом и той же секундой.
_SEQ = itertools.count(1)

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
  link TEXT,
  answered_via TEXT
);
"""


# ------------------------------------------------------------------ фикстуры

# Схема kanban-БД карточек — ровно те колонки, которые читает наблюдение `/cards`
# (`plugin_api._cards_of`), плюс лизинг рана: наблюдение показывает статус, ран и
# срок лизинга, а не дела.
CARD_SCHEMA = """
CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    current_run_id       INTEGER,
    block_kind           TEXT
);
CREATE TABLE task_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL,
    profile       TEXT,
    status        TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER,
    outcome       TEXT,
    lease_expires INTEGER
);
"""


def make_pool(home: Path) -> Path:
    """Пул вопросов по реальной схеме; повторный вызов не пересоздаёт таблицу."""
    path = home / "messaging" / "pool.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='questions'"
    ).fetchone()
    if not exists:
        conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()
    return path


def store(home: Path) -> sqlite3.Connection:
    """Соединение своего store: схему (и журнал рядом) доводит открытие на запись."""
    conn = deals.open_store(home)
    assert conn is not None
    conn.row_factory = sqlite3.Row
    return conn


def make_deal(
    home: Path,
    *,
    status: str = "todo",
    repo: str | None = "stack",
    created_at: int = NOW - 3600,
    parents: tuple[str, ...] = (),
    assignee: str | None = "implementer",
    title: str = "дело",
) -> str:
    """Своё дело в store: строка плюс `created` — как их пишет мутация."""
    conn = store(home)
    try:
        result = deals.create_deal(
            conn,
            repo=repo,
            title=title,
            assignee=assignee,
            parents=parents,
            now=created_at,
        )
        deal_id = str(result["deal"]["id"])
        if status != str(result["deal"]["status"]):
            # Статус жизни ставится командой, а не созданием: в тесте — в строку.
            with conn:
                conn.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))
    finally:
        conn.close()
    return deal_id


def append(
    home: Path,
    deal_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    ts: int = NOW - 120,
) -> None:
    """Дописать факт журнала дела так, как его пишет движок."""
    conn = journal.open_journal(home)
    assert conn is not None
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
                    "payload": payload or {},
                    "idempotency_key": f"test:{deal_id}:{kind}:{next(_SEQ)}",
                }
            ],
        )
    finally:
        conn.close()


def attempt_started(
    home: Path,
    deal_id: str,
    *,
    stage: str = "impl",
    attempt: int = 1,
    pid: int | None = 4242,
    started_at: int = NOW - 300,
    lease_expires: int | None = None,
    role: str = "implementer",
) -> str:
    """`attempt_started` — той же формой, что пишет движок (`runner.step`)."""
    run_id = f"{deal_id}:{stage}#{attempt}"
    append(
        home,
        deal_id,
        "attempt_started",
        {
            "run_id": run_id,
            "stage": stage,
            "attempt": attempt,
            "pid": pid,
            "lease_expires": lease_expires,
            "contract": {"role": role},
        },
        ts=started_at,
    )
    return run_id


def attempt_ended(
    home: Path,
    deal_id: str,
    *,
    stage: str = "impl",
    attempt: int = 1,
    outcome: str = "done",
    kind: str | None = None,
    reason: str = "",
    ended_at: int = NOW - 200,
    commit: str | None = None,
) -> str:
    """`attempt_ended` — исход попытки словами движка."""
    run_id = f"{deal_id}:{stage}#{attempt}"
    append(
        home,
        deal_id,
        "attempt_ended",
        {
            "run_id": run_id,
            "stage": stage,
            "attempt": attempt,
            "outcome": outcome,
            "kind": kind,
            "reason": reason,
            "base": None,
            "commit": commit,
        },
        ts=ended_at,
    )
    return run_id


def hold_lease(
    home: Path,
    deal_id: str,
    *,
    stage: str = "impl",
    attempt: int = 1,
    pid: int = 4242,
    expires_at: int,
    taken_at: int | None = None,
) -> None:
    """Строка лизинга этапа — то, что читает движок и `_own_attempt_facts`."""
    conn = store(home)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (deal_id, stage, attempt, pid, taken_at or expires_at - 300, expires_at),
            )
    finally:
        conn.close()


def add_question(
    home: Path,
    *,
    session_key: str,
    status: str = "sent",
    origin: str = "duty",
    text: str = "вопрос по делу",
    project: str | None = None,
) -> int:
    """Живой вопрос пула по делу: `session_key` — `t_<deal_id>`."""
    conn = sqlite3.connect(make_pool(home))
    try:
        cur = conn.execute(
            "INSERT INTO questions (session_key, project, text, text_full, origin, status, kind) "
            "VALUES (?,?,?,?,?,?,'question')",
            (session_key, project, text, text, origin, status),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


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
    """Корень Hermes для фикстуры: агрегатор читает его через HERMES_HOME.

    Пул вопросов создаётся по умолчанию (как в живом Hermes): отсутствие
    `messaging/pool.db` — отдельный сценарий, его проверяют свои тесты.
    """
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    make_pool(root)
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def deals_by_id(result: dict) -> dict[str, dict]:
    return {deal["id"]: deal for deal in result["deals"]}


# --------------------------------------------------------------------- тесты


def test_gate_child_waits_for_unfinished_parent(home: Path) -> None:
    """Гейт: ребёнок стоит на незавершённом родителе, родитель — очередь слота."""
    parent = make_deal(home, status="todo", created_at=NOW - 7200, title="родитель")
    child = make_deal(
        home, status="todo", created_at=NOW - 3600, parents=(parent,), title="ребёнок"
    )

    result = plugin_api.collect_now(home, NOW)
    indexed = deals_by_id(result)

    assert result["errors"] == []
    assert indexed[child]["attempt"] is None
    assert indexed[child]["last_action"]["kind"] == "created"
    assert indexed[child]["waiting"]["state"] == "wait_gate"
    assert parent in indexed[child]["waiting"]["evidence"]
    # Родитель без родителей — очередь диспетчера, а не гейт.
    assert indexed[parent]["waiting"]["state"] == "wait_slot"

    # Закрытие родителя открывает гейт фактом журнала ребёнка: дело уходит в очередь.
    conn = store(home)
    try:
        deals.complete_deal(conn, parent, summary="готово", now=NOW)
    finally:
        conn.close()

    after = deals_by_id(plugin_api.collect_now(home, NOW))
    assert after[child]["status"] == "ready"
    assert after[child]["waiting"]["state"] == "wait_slot"
    assert "незавершённые родители" not in after[child]["waiting"]["evidence"]


def test_attempt_with_dead_pid_is_crashed(home: Path) -> None:
    """Незакрытая попытка с мёртвым pid — падение, а не работа и не лизинг."""
    pid = absent_pid()
    deal_id = make_deal(home, status="running", created_at=NOW - 500)
    run_id = attempt_started(home, deal_id, pid=pid, started_at=NOW - 400)
    hold_lease(home, deal_id, pid=pid, expires_at=NOW + 600)

    deal = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]

    assert deal["attempt"]["run_id"] == run_id
    assert deal["attempt"]["source"] == "journal"
    assert deal["attempt"]["pid_alive"] is False
    assert deal["attempt"]["age_sec"] == 400
    assert deal["lease"]["state"] == "live"
    assert deal["waiting"]["state"] == "crashed"
    assert "мёртв" in deal["waiting"]["evidence"]


def test_attempt_with_live_pid_is_working(home: Path) -> None:
    """Незакрытая попытка с живым pid — `working`; heartbeat журнал не пишет."""
    deal_id = make_deal(home, status="running", created_at=NOW - 700)
    attempt_started(home, deal_id, pid=os.getpid(), started_at=NOW - 600)
    hold_lease(home, deal_id, pid=os.getpid(), expires_at=NOW + 300)

    deal = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]

    assert deal["attempt"]["pid_alive"] is True
    # Сердцебиение в журнал не пишется: поля пусты, и это названо словами.
    assert deal["attempt"]["heartbeat_age_sec"] is None
    assert deal["attempt"]["run_status"] is None
    assert deal["waiting"]["state"] == "working"
    assert "heartbeat в журнале не пишется" in deal["waiting"]["evidence"]
    assert deal["lease"]["state"] == "live"
    assert deal["lease"]["expires_in_sec"] == 300


def test_attempt_with_unknown_pid_state_is_not_reported_as_missing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.kill` ответил прочим `OSError` (`pid_alive=None`) → «состояние pid неизвестно»."""
    deal_id = make_deal(home, status="running", created_at=NOW - 700)
    attempt_started(home, deal_id, pid=50123, started_at=NOW - 600)
    # Живость решают два места: fold журнала и поля попытки в ответе — оба на шве.
    monkeypatch.setattr(journal, "pid_alive", lambda pid: None)
    monkeypatch.setattr(plugin_api, "_pid_alive", lambda pid: None)

    deal = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]

    assert deal["attempt"]["pid_alive"] is None
    assert deal["waiting"]["state"] == "no_signal"
    assert "состояние pid 50123 неизвестно" in deal["waiting"]["evidence"]
    assert "не записан" not in deal["waiting"]["evidence"]


def test_attempt_without_pid_says_pid_not_recorded(home: Path) -> None:
    """Попытка без pid: «pid не записан» — и без «None» в тексте доказательства."""
    deal_id = make_deal(home, status="running", created_at=NOW - 700)
    attempt_started(home, deal_id, pid=None, started_at=NOW - 600)

    deal = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]

    assert deal["attempt"]["pid"] is None
    assert deal["waiting"]["state"] == "no_signal"
    assert "pid не записан" in deal["waiting"]["evidence"]
    assert "None" not in deal["waiting"]["evidence"]


def test_claim_with_expired_term_is_dead_lease(home: Path) -> None:
    """Лизинг взят, срок истёк, попытки за ним нет — `dead_lease`."""
    deal_id = make_deal(home, status="ready", created_at=NOW - 900)
    append(
        home,
        deal_id,
        "claimed",
        {"claim_lock": "mbp-test:1", "claim_expires": NOW - 300},
        ts=NOW - 800,
    )

    deal = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]

    assert deal["waiting"]["state"] == "dead_lease"
    assert "mbp-test:1" in deal["waiting"]["evidence"]
    assert "срок истёк" in deal["waiting"]["evidence"]


def test_claim_without_term_is_not_dead_lease(home: Path) -> None:
    """`claimed` без срока: срок — данные, а не происхождение записи, — жалобы нет."""
    deal_id = make_deal(home, status="ready", created_at=NOW - 900)
    append(home, deal_id, "claimed", {"claim_lock": "mbp-test:1"}, ts=NOW - 800)

    deal = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]

    assert deal["waiting"]["state"] == "wait_slot"
    assert "нет живых фактов" in deal["waiting"]["evidence"]


def test_lease_row_without_attempt_is_visible(home: Path) -> None:
    """Строка лизинга без `attempt_started` — факт store, а не «лизинга нет»."""
    deal_id = make_deal(home, status="ready", created_at=NOW - 400)
    hold_lease(home, deal_id, expires_at=NOW + 120)

    deal = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]

    assert deal["attempt"] is None
    assert deal["lease"]["state"] == "live"
    assert deal["lease"]["expires_at"] == NOW + 120
    assert deal["lease"]["expires_in_sec"] == 120


def test_wait_human_from_pool_question(home: Path) -> None:
    """Живой вопрос пула по делу → wait_human, и номер вопроса назван в evidence."""
    asked = make_deal(home, status="blocked", created_at=NOW - 900)
    silent = make_deal(home, status="blocked", created_at=NOW - 800)
    question_id = add_question(home, session_key=f"t_{asked}", project="stack")
    # Факт вопроса пишет `ask()` — вместе со строкой пула, под тем же `deal_id`.
    append(
        home,
        asked,
        "question_asked",
        {"question_id": question_id, "session_key": f"t_{asked}", "critical": 1},
        ts=NOW - 850,
    )

    indexed = deals_by_id(plugin_api.collect_now(home, NOW))

    assert indexed[asked]["waiting"]["state"] == "wait_human"
    assert indexed[asked]["waiting"]["human_kind"] == "question"
    assert f"#{question_id}" in indexed[asked]["waiting"]["evidence"]
    # У дела без вопроса тот же статус, но вид ожидания другой: «по статусу».
    assert indexed[silent]["waiting"]["state"] == "wait_human"
    assert indexed[silent]["waiting"]["human_kind"] == "status"


def test_capability_block_wins_over_human_fallback(home: Path) -> None:
    """Блок с typed-причиной `capability` — отдельное состояние, а не «ждёт человека»."""
    blocked = make_deal(home, status="blocked", created_at=NOW - 700)
    plain = make_deal(home, status="blocked", created_at=NOW - 600)
    append(
        home,
        blocked,
        "blocked",
        {"block_kind": "capability", "reason": "нет доступа к репозиторию: role-access"},
        ts=NOW - 690,
    )
    append(
        home,
        plain,
        "blocked",
        {"block_kind": "needs_input", "reason": "выбери вариант"},
        ts=NOW - 590,
    )

    indexed = deals_by_id(plugin_api.collect_now(home, NOW))

    assert indexed[blocked]["waiting"]["state"] == "capability"
    assert "role-access" in indexed[blocked]["waiting"]["evidence"]
    assert indexed[plain]["waiting"]["state"] == "wait_human"


def test_review_requested_waits_for_verdict_and_return_is_a_slot(home: Path) -> None:
    """Сдача в ревью — `wait_human`; возврат на доработку — `wait_slot` с причиной."""
    deal_id = make_deal(home, status="review", created_at=NOW - 900)
    append(home, deal_id, "review_requested", {"reviewer": "reviewer"}, ts=NOW - 800)

    waiting = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]["waiting"]
    assert waiting["state"] == "wait_human"
    assert "вердикта в журнале нет" in waiting["evidence"]

    append(home, deal_id, "changes_requested", {"reason": "поправь тесты"}, ts=NOW - 700)
    waiting = deals_by_id(plugin_api.collect_now(home, NOW))[deal_id]["waiting"]
    assert waiting["state"] == "wait_slot"
    assert "поправь тесты" in waiting["evidence"]


def test_state_and_repo_filters(home: Path, client: TestClient) -> None:
    """`?repo=` и `?state=` фильтруют один и тот же детерминированный срез."""
    stack = make_deal(home, status="todo", repo="stack", created_at=NOW - 100)
    other = make_deal(home, status="blocked", repo="other", created_at=NOW - 100)

    everything = client.get("/now").json()
    assert everything["count"] == 2
    # Сортировка по таксономии: wait_human раньше wait_slot.
    assert [deal["id"] for deal in everything["deals"]] == [other, stack]

    one_repo = client.get("/now", params={"repo": "other"}).json()
    assert [deal["id"] for deal in one_repo["deals"]] == [other]

    one_state = client.get("/now", params={"state": "wait_slot"}).json()
    assert [deal["id"] for deal in one_state["deals"]] == [stack]
    assert one_state["by_state"]["wait_human"] == 1

    # Неизвестный repo — фильтр, а не ресурс: пустой результат, тот же код, что у
    # `/stats` (одно поведение на обеих ручках, `docs/SPEC.md`).
    unknown = client.get("/now", params={"repo": "nope"})
    assert unknown.status_code == 200
    assert unknown.json()["deals"] == []
    assert unknown.json()["count"] == 0
    assert client.get("/now", params={"state": "nonsense"}).status_code == 400
    assert client.get("/duty/d_00000000/attempts").status_code == 404
    assert client.get("/duty/nonsense/attempts").status_code == 400


def test_unknown_repo_is_a_filter_on_both_handles(home: Path, client: TestClient) -> None:
    """`/now?repo=` и `/stats?repo=` отвечают одинаково: 200 с пустым результатом.

    Репозиторий — свойство дела, а не адрес: опечатка в фильтре даёт пустую
    страницу, а не 404. Расхождение было настоящим: `/stats?repo=<живой>&since=`
    отдавал 404, потому что repo искался уже внутри окна `since`.
    """
    deal = make_deal(home, status="todo", repo="stack", created_at=NOW - 100)

    for path in ("/now", "/stats"):
        response = client.get(path, params={"repo": "nope"})
        assert response.status_code == 200, path
        assert response.json()["errors"] == [], path
    assert client.get("/stats", params={"repo": "nope"}).json()["repos"] == []
    assert client.get("/now", params={"repo": "nope"}).json()["deals"] == []

    # Живой repo вне окна `since` — тот же ответ, а не 404: дело есть, окно пустое.
    windowed = client.get("/stats", params={"repo": "stack", "since": NOW + 1000})
    assert windowed.status_code == 200
    assert windowed.json()["repos"] == []
    assert client.get("/now", params={"repo": "stack"}).json()["deals"][0]["id"] == deal


def test_sorting_is_deterministic_between_polls(home: Path) -> None:
    """Повторный срез на тех же данных даёт тот же порядок и те же возраста."""
    old_a = make_deal(home, status="todo", created_at=NOW - 5000)
    old_z = make_deal(home, status="todo", created_at=NOW - 5000)
    busy = make_deal(home, status="running", created_at=NOW - 100)
    attempt_started(home, busy, pid=os.getpid(), started_at=NOW - 90)

    first = plugin_api.collect_now(home, NOW)
    second = plugin_api.collect_now(home, NOW)

    expected = [busy] + sorted([old_a, old_z])
    assert [deal["id"] for deal in first["deals"]] == expected
    assert [deal["waiting"]["state"] for deal in first["deals"]] == [
        "working",
        "wait_slot",
        "wait_slot",
    ]
    assert first["deals"] == second["deals"]


def test_sources_untouched_by_now(home: Path) -> None:
    """Срез «сейчас» не меняет источники: mtime, размер и состав файлов те же."""
    deal_id = make_deal(home, status="running", created_at=NOW - 100)
    attempt_started(home, deal_id, pid=os.getpid(), started_at=NOW - 60)

    sources = sorted(path for path in home.rglob("*") if path.is_file())
    before = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in sources}
    time.sleep(0.01)
    plugin_api.collect_now(home, NOW)
    after_paths = sorted(path for path in home.rglob("*") if path.is_file())
    after = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in after_paths}

    assert after_paths == sources
    assert after == before


def test_unreadable_store_lands_in_errors_and_route_stays_200(
    home: Path, client: TestClient
) -> None:
    """Битый `duty/duty.db` не валит роут: 200, причина в `errors`, дел нет."""
    path = home / "duty" / "duty.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database at all")

    response = client.get("/now")

    assert response.status_code == 200
    body = response.json()
    assert body["deals"] == []
    sources = [error["source"] for error in body["errors"]]
    assert plugin_api.DEALS_SOURCE in sources
    assert body["repos"]["read"] is False


def test_missing_pool_db_lands_in_errors_and_is_not_silent(
    home: Path, client: TestClient
) -> None:
    """Нет `messaging/pool.db` → 200 + `errors` про пул, а не молчаливое «вопросов нет».

    Иначе `wait_human` тихо не срабатывает: «вопросов нет» и «канал вопросов не
    читается» неотличимы ни в API, ни в UI.
    """
    deal_id = make_deal(home, status="blocked", created_at=NOW - 900)
    (home / "messaging" / "pool.db").unlink()

    response = client.get("/now")

    assert response.status_code == 200
    body = response.json()
    errors = {error["source"]: error["reason"] for error in body["errors"]}
    assert "messaging/pool.db" in errors
    assert "pool.db not found" in errors["messaging/pool.db"]
    # Дело не теряется: роут не валится, срез на месте.
    assert [deal["id"] for deal in body["deals"]] == [deal_id]
    # Состояние остаётся wait_human, но evidence честно говорит, что факт не проверен.
    waiting = body["deals"][0]["waiting"]
    assert waiting["state"] == "wait_human"
    assert "факт wait_human не проверен" in waiting["evidence"]


def test_pool_db_that_is_not_a_database_is_reported(home: Path, client: TestClient) -> None:
    """Файл `messaging/pool.db` есть, но не БД → причина называет пул, а не «вопросов нет»."""
    deal_id = make_deal(home, status="todo", created_at=NOW - 700)
    (home / "messaging" / "pool.db").write_bytes(b"not a database at all")

    questions, err, truncated = plugin_api._pool_questions(home)

    assert questions == {}
    assert err is not None and "pool.db unreadable" in err
    assert truncated is False
    body = client.get("/now").json()
    errors = {error["source"]: error["reason"] for error in body["errors"]}
    assert "messaging/pool.db" in errors
    assert "not a database" in errors["messaging/pool.db"]
    assert [deal["id"] for deal in body["deals"]] == [deal_id]


def test_pool_absent_question_is_checked_on_clean_pool(home: Path) -> None:
    """Пустой, но читаемый пул — это именно «вопросов нет»: ошибки нет."""
    make_deal(home, status="todo", created_at=NOW - 600)

    questions, err, truncated = plugin_api._pool_questions(home)
    result = plugin_api.collect_now(home, NOW)

    assert questions == {} and err is None
    assert truncated is False
    assert result["errors"] == []
    assert "не проверен" not in result["deals"][0]["waiting"]["evidence"]


def test_crash_outcomes_come_from_the_backend_constant(
    home: Path, client: TestClient
) -> None:
    """Набор «падений» — один источник: бэкенд отдаёт его клиенту в `crash_outcomes`."""
    deal_id = make_deal(home, status="ready", created_at=NOW - 100)
    attempt_started(home, deal_id, started_at=NOW - 90)
    attempt_ended(home, deal_id, outcome="crashed", kind="infra", ended_at=NOW - 60)

    assert list(plugin_api.CRASH_OUTCOMES) == ["crashed", "gave_up", "spawn_failed", "timed_out"]
    now_body = client.get("/now").json()
    assert now_body["crash_outcomes"] == list(plugin_api.CRASH_OUTCOMES)
    attempts = client.get(f"/duty/{deal_id}/attempts").json()
    assert attempts["crash_outcomes"] == list(plugin_api.CRASH_OUTCOMES)
    assert attempts["attempts"][0]["outcome"] == "crashed"


def test_running_without_any_live_signal_is_no_signal(home: Path, client: TestClient) -> None:
    """`status=running` без попытки и лизинга — не «работает», а «нет сигнала»."""
    deal_id = make_deal(home, status="running", created_at=NOW - 400)

    body = client.get("/now").json()
    deal = deals_by_id(body)[deal_id]

    assert deal["attempt"] is None
    assert deal["lease"]["state"] == "none"
    assert deal["waiting"]["state"] == "no_signal"
    assert "идёт ли попытка — неизвестно" in deal["waiting"]["evidence"]
    # Таксономия остаётся взаимоисключающей: дело попало ровно в одну корзину.
    assert body["by_state"]["no_signal"] == 1
    assert sum(body["by_state"].values()) == 1


def test_question_pool_limit_is_not_silent(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Пул обрезан лимитом → причина в `errors` и в evidence дел без вопроса."""
    monkeypatch.setattr(plugin_api, "QUESTION_LIMIT", 2)
    deal_id = make_deal(home, status="review", created_at=NOW - 500)
    add_question(home, session_key=f"t_{deal_id}")
    add_question(home, session_key="agent:a")
    add_question(home, session_key="agent:b")

    body = plugin_api.collect_now(home, NOW)

    errors = {error["source"]: error["reason"] for error in body["errors"]}
    assert "questions truncated" in errors["messaging/pool.db"]
    deal = deals_by_id(body)[deal_id]
    # Вопрос этого дела не вошёл в срез — состояние не выдаёт проверку за факт.
    assert deal["waiting"]["state"] == "wait_human"
    assert "живых вопросов в пуле больше 2" in deal["waiting"]["evidence"]
    assert "факт wait_human не проверен" in deal["waiting"]["evidence"]

    # Ровно лимит живых вопросов — это ещё не обрез: помечать нечего.
    monkeypatch.setattr(plugin_api, "QUESTION_LIMIT", 3)
    clean = plugin_api.collect_now(home, NOW)
    assert "messaging/pool.db" not in {error["source"] for error in clean["errors"]}


def test_attempts_timeline_is_ordered_with_durations(home: Path, client: TestClient) -> None:
    """Две попытки из журнала: таймлайн по возрастанию старта, длительности посчитаны."""
    deal_id = make_deal(home, status="ready", created_at=NOW - 5000)
    first = attempt_started(home, deal_id, stage="impl", started_at=NOW - 4000)
    attempt_ended(
        home, deal_id, stage="impl", outcome="crashed", kind="infra", reason="worker died",
        ended_at=NOW - 3400,
    )
    second = attempt_started(home, deal_id, stage="impl", attempt=2, started_at=NOW - 3300)
    attempt_ended(
        home, deal_id, stage="impl", attempt=2, outcome="timed_out", kind="infra",
        reason="max runtime", ended_at=NOW - 3200,
    )
    append(home, deal_id, "commented", {"author": "lead"}, ts=NOW - 3250)

    body = client.get(f"/duty/{deal_id}/attempts").json()

    assert body["deal_id"] == deal_id
    assert body["count"] == 2
    first_attempt, second_attempt = body["attempts"]
    assert (first_attempt["run_id"], second_attempt["run_id"]) == (first, second)
    assert first_attempt["duration_sec"] == 600
    assert second_attempt["duration_sec"] == 100
    assert first_attempt["outcome"] == "crashed"
    assert first_attempt["reason"] == "worker died"
    assert first_attempt["error"] == first_attempt["reason"]
    assert first_attempt["stage"] == "impl"
    assert first_attempt["profile"] == "implementer"


def test_open_attempt_with_dead_lease_is_orphan(home: Path, client: TestClient) -> None:
    """Незакрытая попытка с истёкшим лизингом — осиротевшая: её закроет уборка.

    Срок лизинга живёт по настоящим часам (`/duty/.../attempts` не подставляет
    `now`), поэтому фикстура берёт текущее время, а не фиксированное `NOW`.
    """
    moment = int(time.time())
    deal_id = make_deal(home, status="running", created_at=NOW - 900)
    attempt_started(home, deal_id, pid=absent_pid(), started_at=moment - 800)
    hold_lease(home, deal_id, pid=absent_pid(), expires_at=moment - 100)

    attempt = client.get(f"/duty/{deal_id}/attempts").json()["attempts"][0]

    assert attempt["status"] == "orphan"
    assert attempt["ended_at"] is None
    assert "bin/duty reap" in attempt["reason"]


def test_kanban_cards_are_not_deals(home: Path, client: TestClient) -> None:
    """Карточка kanban — наблюдение, не дело: в срез она не попадает (0.0.6)."""
    board = home / "kanban" / "boards" / "stack" / "kanban.db"
    board.parent.mkdir(parents=True)
    conn = sqlite3.connect(board)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL, "
        "created_at INTEGER NOT NULL);"
        "INSERT INTO tasks (id, title, status, created_at) "
        "VALUES ('t_0000aa', 'карточка', 'blocked', 1);"
    )
    conn.commit()
    conn.close()

    body = client.get("/now").json()

    assert body["deals"] == []
    assert body["count"] == 0
    assert client.get("/deal/t_0000aa").status_code == 400


# ------------------------------------------------- доски выбыли: наблюдение /cards
#
# Карточки kanban живут только в `/cards` (`plugin_api._card_databases`), и выбывшая
# доска источником не считается: ядро kanban убирает закрытые доски в
# `kanban/boards/_archived/` и ставит камень `board.json: archived`. Пробы ниже —
# про `_retired`, а не про адресацию: адрес дела от доски не зависит вовсе.


def make_cards_board(home: Path, name: str, card: str) -> Path:
    """Kanban-БД доски `name` с одной карточкой — так её раскладывает ядро Hermes."""
    database = home / "kanban" / "boards" / name / "kanban.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.executescript(CARD_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, block_kind, consecutive_failures, "
        "created_at) VALUES (?, 'карточка', 'implementer', 'blocked', 'needs_input', 0, 1)",
        (card,),
    )
    conn.commit()
    conn.close()
    return database


def cards_names(client: TestClient) -> list[str]:
    """Имена kanban-БД, которые наблюдение `/cards` реально прочитало."""
    body = client.get("/cards").json()
    return [entry["name"] for entry in body["cards"]]


def test_archived_namespace_is_not_a_live_source(home: Path, client: TestClient) -> None:
    """`boards/_archived/` — namespace, а не доска: в `/cards` он не читается.

    Раньше `_archived/kanban.db` попадал в обнаружение как ещё одна доска и висел
    в ответе пустым наблюдением, хотя это каталог выбывших досок.
    """
    make_cards_board(home, "stack", "t_3e0c0001")
    make_cards_board(home, "_archived/old-board", "t_3e0c0002")
    (home / "kanban" / "boards" / "_archived" / "kanban.db").write_bytes(b"")

    assert cards_names(client) == ["stack"]
    body = client.get("/cards").json()
    assert body["databases"] == 1
    assert body["count"] == 1
    assert {card["id"] for entry in body["cards"] for card in entry["cards"]} == {"t_3e0c0001"}


def test_archived_tombstone_board_is_skipped(home: Path, client: TestClient) -> None:
    """Доска с камнем `board.json: archived` не читается в `/cards`."""
    make_cards_board(home, "stack", "t_3e0d0001")
    legacy = make_cards_board(home, "legacy", "t_3e0d0002")
    (legacy.parent / "board.json").write_text('{"slug": "legacy", "archived": true}')

    assert cards_names(client) == ["stack"]


def test_retired_filter_is_about_retirement_not_readability(
    home: Path, client: TestClient
) -> None:
    """Выбывшая доска скрыта, живая пустая — видна: фильтр про retired, не про чтение."""
    make_cards_board(home, "stack", "t_3e0e0001")
    retired = make_cards_board(home, "_archived/old-board", "t_3e0e0002")
    assert retired.is_file()
    empty = home / "kanban" / "boards" / "empty-board" / "kanban.db"
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")

    body = client.get("/cards").json()
    shown = {entry["name"]: entry for entry in body["cards"]}

    # выбывшая доска со своей карточкой не показана вовсе, хотя её БД читается
    assert "old-board" not in shown
    assert {card["id"] for entry in body["cards"] for card in entry["cards"]} == {"t_3e0e0001"}
    # пустая живая доска остаётся в наблюдении с причиной «empty db», а не исчезает
    assert shown["empty-board"]["read"] is False
    assert shown["empty-board"]["note"] == "empty db"
