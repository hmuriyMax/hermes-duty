"""`ask()` — единственный канал вопросов (контракт `docs/JOURNAL.md`).

Проверяется то, что нельзя увидеть глазами при ревью: живой вопрос по делу
возвращается как есть (второй строки в пуле нет), сбой пула не оставляет в
журнале ни вопроса, ни его тени, ответ доходит до журнала ровно один раз, а
`waiting.human_kind` отличает «есть вопрос» от «человек нужен, вопроса нет».

Отдельно — цикл целиком (B1 ревью): подхваченный **чужой** вопрос не пишется в
журнал вовсе, истёкший TTL закрывает свой вопрос фактом `answered_via='expired'`;
в обоих случаях дело перестаёт «ждать ответа» на вопрос, которого уже нет.

Фикстуры — свои маленькие `kanban.db`/`pool.db` в `tmp_path` по реальной схеме;
живые источники не читаются.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
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
deals = plugin_api.deals

NOW = 1_800_000_000
# Своё дело в store: адресация только `d_<hex>` (0.0.6), доски больше нет.
DEAL = "d_0000aa11"

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


def make_deal(home: Path, *, status: str = "blocked", repo: str = "stack") -> str:
    """Своё дело в store — строкой, без события: тесты считают события журнала.

    Схему доводит открытие store на запись (`deals.open_store`). Факта `created`
    намеренно нет: `ask()` обязан быть **первым** событием дела, и тесты это
    проверяют (`journal_events(home) == []` до вопроса).
    """
    conn = deals.open_store(home)
    assert conn is not None
    try:
        with conn:
            conn.execute(
                "INSERT INTO deals (id, repo, title, body, assignee, status, origin, created_at, updated_at) "
                "VALUES (?, ?, 'дело с вопросом', 'тело дела', 'implementer', ?, 'duty', ?, ?)",
                (DEAL, repo, status, NOW - 3600, NOW - 3600),
            )
    finally:
        conn.close()
    return DEAL


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


def add_foreign_question(
    home: Path, *, status: str = "sent", origin: str = "card", session_key: str = f"t_{DEAL}"
) -> int:
    """Строка пула от чужого продюсера — так выглядит карточный вопрос сканера."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        cur = conn.execute(
            "INSERT INTO questions (session_key, text, origin, status, critical, timeout) "
            "VALUES (?, 'вопрос сканера карточек', ?, ?, 1, NULL)",
            (session_key, origin, status),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def pool_close(home: Path, question_id: int, status: str, *, answer: str | None = None) -> None:
    """Закрыть вопрос пула штатным путём пула: `answered` отвечает, `expired` истекает.

    Скопировано поведение демона: `expire_noncritical` меняет только `status`
    (`answered_via` у истёкшего вопроса остаётся пустым), ответ пишет
    `answered_at`/`answered_via`.
    """
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        if status == "answered":
            conn.execute(
                "UPDATE questions SET status='answered', answer=?, answered_via='manual', "
                "answered_at='2026-11-03T10:00:00.000Z' WHERE id = ?",
                (answer or "ок", question_id),
            )
        else:
            conn.execute("UPDATE questions SET status=? WHERE id = ?", (status, question_id))
        conn.commit()
    finally:
        conn.close()


def journal_events(home: Path) -> list[dict[str, Any]]:
    conn = journal.open_journal(home, create=False)
    if conn is None:  # журнала ещё нет — событий тоже нет
        return []
    try:
        return journal.read_events(conn)
    finally:
        conn.close()


def open_questions(home: Path) -> list[int]:
    """Вопросы журнала без факта закрытия — то, что fold считает «ждёт ответа»."""
    asked: dict[int, None] = {}
    for event in journal_events(home):
        payload = event.get("payload") or {}
        question_id = payload.get("question_id")
        if question_id is None:
            continue
        if event["kind"] == "question_asked":
            asked[int(question_id)] = None
        elif event["kind"] == "question_answered":
            asked.pop(int(question_id), None)
    return list(asked)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes: своё заблокированное дело в store и пустой пул вопросов."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    make_deal(root)
    make_pool(root)
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def deal_facts(home: Path) -> dict[str, Any]:
    """Факты дела для прямого вызова `ask()` (без HTTP)."""
    return {"id": DEAL, "repo": "stack", "home": home}


def ask_once(home: Path, **kwargs: Any) -> ask.AskResult:
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


def waiting_now(client: TestClient) -> dict[str, Any]:
    """`waiting` единственного дела из `/now`."""
    return client.get("/now").json()["deals"][0]["waiting"]


# ------------------------------------------------------- создание вопроса


def test_ask_writes_pool_row_and_journal_event(home: Path) -> None:
    """Вопрос: строка пула по схеме пула + `question_asked` в журнале после вставки."""
    result = ask_once(home, options=["да", "нет"])
    question_id = result.question_id

    assert result.created is True
    assert result.adopted is False

    rows = pool_rows(home)
    assert len(rows) == 1
    row = rows[0]
    assert question_id == row["id"]
    assert row["session_key"] == f"t_{DEAL}"
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
    assert asked["source"] == "ask"
    assert asked["idempotency_key"] == f"ask:{question_id}:question_asked"
    assert asked["payload"] == {
        "question_id": question_id,
        "session_key": f"t_{DEAL}",
        "critical": 0,
        "timeout": ask.ASK_TIMEOUT_HOURS,
    }


def test_second_ask_returns_same_question_without_duplicate(home: Path) -> None:
    """Второй `ask()` по тому же делу: тот же `question_id`, одна строка, одно событие."""
    first = ask_once(home, text="Первый вопрос")
    second = ask_once(home, text="Другой текст — тот же вопрос")

    assert second.question_id == first.question_id
    assert second.created is False
    assert second.adopted is False
    rows = pool_rows(home)
    assert len(rows) == 1
    assert rows[0]["text"] == "Первый вопрос"
    assert [event["kind"] for event in journal_events(home)] == ["question_asked"]


def test_ask_adopts_foreign_live_question_without_journal_fact(home: Path) -> None:
    """Живой вопрос чужого продюсера (`origin='card'`) — id есть, факта в журнале нет.

    Снять `question_asked` по чужому вопросу нечем: ответ на него в наш журнал не
    попадает (`sync_answers` читает только свои), поэтому записанный факт висел бы
    в fold вечно и дело навсегда «ждало ответа» (B1 ревью W2).
    """
    foreign_id = add_foreign_question(home)

    result = ask_once(home)

    assert result.question_id == foreign_id
    assert result.created is False
    assert result.adopted is True
    rows = pool_rows(home)
    assert len(rows) == 1
    assert rows[0]["origin"] == "card"
    # В журнале по делу не появилось ни одного события — и висящего факта тоже.
    assert journal_events(home) == []
    assert open_questions(home) == []


def test_adopted_own_question_takes_ts_from_pool_created_at(home: Path) -> None:
    """`ts` подхваченного своего вопроса — из `created_at` строки пула, а не `now`.

    Иначе evidence говорит «вопрос задан 0 с назад» про вопрос суточной давности, и
    fold сортирует факт не тем временем.
    """
    created = "2026-11-03T09:00:00.000Z"
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.execute(
        "INSERT INTO questions (session_key, text, origin, status, critical, timeout, created_at) "
        "VALUES (?, 'наш вопрос', 'duty', 'sent', 0, 24.0, ?)",
        (f"t_{DEAL}", created),
    )
    conn.commit()
    conn.close()

    result = ask_once(home)

    assert result.adopted is False
    asked = [event for event in journal_events(home) if event["kind"] == "question_asked"]
    assert len(asked) == 1
    assert asked[0]["ts"] == int(datetime.fromisoformat("2026-11-03T09:00:00+00:00").timestamp())
    assert asked[0]["recorded_at"] >= asked[0]["ts"]


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
                    "repo": "stack",
                    "kind": "blocked",
                    "payload": {"block_kind": "needs_input", "reason": "нужно решение"},
                    "source": "ingest",
                    "idempotency_key": f"engine:{DEAL}:blocked:1",
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


def test_insert_failure_writes_no_event_without_fs_rights(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой вставки → факта нет. Права ФС здесь не участвуют: подменён `_insert`.

    `chmod 444` выше держится на том, что ядро проверяет права: под root или в
    контейнере без enforcement тест зелёный при живой записи. Здесь вставка падает
    всегда, и инвариант «нет вставки — нет события» от прав не зависит.
    """

    def boom(*args: Any, **kwargs: Any) -> int:
        raise ask.AskError("pool.db write failed: подмена вставки")

    monkeypatch.setattr(ask, "_insert", boom)

    with pytest.raises(ask.AskError):
        ask_once(home)

    assert journal_events(home) == []
    assert pool_rows(home) == []


def test_zero_question_id_is_refused(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`lastrowid == 0` → `AskError`: ключ `ask:0:*` заглушил бы все будущие факты.

    `UNIQUE(idempotency_key)` у журнала сделал бы `ask:0:question_asked` первым и
    единственным: каждый следующий вопрос молча получал бы `appended: 0`.
    """
    monkeypatch.setattr(ask, "_insert", lambda *args, **kwargs: 0)

    with pytest.raises(ask.AskError):
        ask_once(home)

    assert journal_events(home) == []


def test_journal_failure_after_insert_is_ask_error(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Сбой журнала после успешной вставки → `AskError`, а не 500 без подробностей.

    Строка пула уже создана: вызывающий обязан узнать, что вопрос есть, а факта
    нет (сообщение это говорит), иначе расхождение выглядит как «вопрос не создан».
    Ломается именно запись (`append`), а чтение журнала работает: иначе вызов
    упал бы раньше вставки и тест ничего не доказывал.
    """

    class BrokenJournal:
        """Журнал, который читается, но не пишется: сбой диска на `append`."""

        def __init__(self, module: Any) -> None:
            self._module = module

        def read_events(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return self._module.read_events(*args, **kwargs)

        def append(self, *args: Any, **kwargs: Any) -> int:
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ask, "_journal", lambda: BrokenJournal(journal))
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        with pytest.raises(ask.AskError) as excinfo:
            ask.ask(conn, deal_facts(home), "Вопрос по делу", "полная версия")
    finally:
        conn.close()

    assert "факт журнала не записан" in str(excinfo.value)
    rows = pool_rows(home)
    assert len(rows) == 1
    assert rows[0]["origin"] == "duty"


def test_ask_route_reports_pool_failure_in_errors(client: TestClient, home: Path) -> None:
    """Сбой пула на роуте: 503 и `errors[]`, где назван пул и дело."""
    (home / "messaging" / "pool.db").unlink()

    response = client.post(f"/ask/{DEAL}", json={"text": "Вопрос"})

    assert response.status_code == 503
    body = response.json()
    assert body["question_id"] is None
    assert body["created"] is False
    assert body["adopted"] is False
    assert body["errors"][0]["source"] == ask.QUESTION_SOURCE
    assert DEAL in body["errors"][0]["reason"]
    assert journal_events(home) == []


# ------------------------------------------------------------ маршрут


def test_ask_route_creates_then_dedups(client: TestClient) -> None:
    """Роут: первый вызов создаёт вопрос, второй возвращает тот же (`deduped`)."""
    first = client.post(
        f"/ask/{DEAL}",
        json={"text": "Даёшь ок?", "text_full": "### Вопрос\n\nРазвёрнутая версия", "options": ["ок", "нет"]},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["errors"] == []
    assert body["created"] is True
    assert body["deduped"] is False
    assert body["adopted"] is False
    question_id = body["question_id"]

    second = client.post(f"/ask/{DEAL}", json={"text": "повтор"})
    assert second.status_code == 200
    assert second.json() == {
        "question_id": question_id,
        "created": False,
        "deduped": True,
        "adopted": False,
        "errors": [],
    }


def test_ask_route_marks_adopted_foreign_question(client: TestClient, home: Path) -> None:
    """Роут называет подхват чужого вопроса: `adopted: true`, а не молчаливый dedup."""
    foreign_id = add_foreign_question(home)

    body = client.post(f"/ask/{DEAL}", json={"text": "наш вопрос"}).json()

    assert body == {
        "question_id": foreign_id,
        "created": False,
        "deduped": True,
        "adopted": True,
        "errors": [],
    }
    assert journal_events(home) == []


def test_ask_route_validates_deal_and_text(client: TestClient) -> None:
    """Неизвестное дело — 404, карточка kanban — 400, пустой текст — 400, кривые options — 400."""
    assert client.post("/ask/d_ffffff", json={"text": "?"}).status_code == 404
    assert client.post("/ask/t_dead", json={"text": "?"}).status_code == 400
    assert client.post("/ask/nope", json={"text": "?"}).status_code == 400
    assert client.post(f"/ask/{DEAL}", json={"text": "   "}).status_code == 400
    bad_options = client.post(f"/ask/{DEAL}", json={"text": "?", "options": [{"label": "ок"}]})
    assert bad_options.status_code == 400


def test_ask_route_does_not_duplicate_and_marks_not_cacheable(client: TestClient) -> None:
    """Ответ роута не кэшируется: иначе «вопрос создан» переживёт сам вопрос."""
    response = client.post(f"/ask/{DEAL}", json={"text": "Вопрос"})

    assert response.headers.get("cache-control") == "no-store, no-cache, must-revalidate"


# ----------------------------------------------------------- sync_answers


def test_sync_answers_is_idempotent(home: Path) -> None:
    """Два прохода `sync_answers` → ровно одно событие `question_answered`."""
    question_id = ask_once(home).question_id
    pool_close(home, question_id, "answered")

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
    assert answered[0]["ts"] == int(datetime.fromisoformat("2026-11-03T10:00:00+00:00").timestamp())


def test_sync_answers_closes_expired_own_question(home: Path) -> None:
    """Истёкший TTL своего вопроса закрывает факт: `answered_via='expired'`, `answer_len=0`.

    Пул переводит протухшую строку в `expired`, не заполняя `answered_at` и
    `answered_via`; без явного признака истечение читалось бы как ответ человека.
    """
    question_id = ask_once(home).question_id
    pool_close(home, question_id, "expired")

    conn = journal.open_journal(home)
    assert conn is not None
    try:
        first = ask.sync_answers(conn, home)
        second = ask.sync_answers(conn, home)
    finally:
        conn.close()

    assert first["appended"] == 1
    assert second["appended"] == 0
    answered = [event for event in journal_events(home) if event["kind"] == "question_answered"]
    assert len(answered) == 1
    assert answered[0]["payload"] == {
        "question_id": question_id,
        "answer_len": 0,
        "answered_via": ask.EXPIRED_VIA,
    }
    assert open_questions(home) == []


def test_sync_answers_keeps_pool_reason_for_expired(home: Path) -> None:
    """Если пул сам назвал причину закрытия — она и остаётся в факте.

    `expired` со своим `answered_via` (в imessage-gateway так закрывает вопросы
    сканер карточек, когда условие алерта исчезло) — не истечение по сроку, и
    подменять причину на `expired` значило бы соврать о том, почему вопрос закрыт.
    """
    question_id = ask_once(home).question_id
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.execute("UPDATE questions SET status='expired', answered_via='card' WHERE id = ?", (question_id,))
    conn.commit()
    conn.close()

    conn = journal.open_journal(home)
    assert conn is not None
    try:
        ask.sync_answers(conn, home)
    finally:
        conn.close()

    answered = [event for event in journal_events(home) if event["kind"] == "question_answered"]
    assert [event["payload"]["answered_via"] for event in answered] == ["card"]
    assert open_questions(home) == []


def test_sync_answers_ignores_foreign_questions(home: Path) -> None:
    """Закрытые вопросы чужого продюсера — не наш факт: событий нет.

    И `answered`, и `expired`: чужой вопрос в журнал не попадал (`ask()` его не
    записывает), значит и снимать по нему нечего.
    """
    add_foreign_question(home, status="answered", origin="card", session_key="t_0002")
    add_foreign_question(home, status="expired", origin="card", session_key="t_0003")

    conn = journal.open_journal(home)
    assert conn is not None
    try:
        report = ask.sync_answers(conn, home)
    finally:
        conn.close()

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
    assert report["errors"][0]["source"] == ask.QUESTION_SOURCE


def sync_now(home: Path) -> dict[str, Any]:
    """Обратный ход пула: `ask.sync_answers` под соединением журнала (так зовёт драйвер).

    Роут `POST /ingest` убран вместе с досками (0.0.6): ответы дочитывает драйвер,
    и тесты обязаны проверять тот же шов, а не удалённую ручку.
    """
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        return ask.sync_answers(conn, home)
    finally:
        conn.close()


def test_ingest_pass_syncs_answers(home: Path) -> None:
    """Проход `sync_answers` дочитывает ответ пула ровно один раз."""
    question_id = ask_once(home).question_id
    pool_close(home, question_id, "answered")

    first = sync_now(home)
    second = sync_now(home)

    assert first == {"appended": 1, "errors": []}
    assert second == {"appended": 0, "errors": []}
    assert question_id in {
        event["payload"]["question_id"]
        for event in journal_events(home)
        if event["kind"] == "question_answered"
    }


# ------------------------------- текст вопроса и ответа вместо номеров


def test_exchange_of_returns_question_and_answer_text(home: Path) -> None:
    """Пул отдаёт вопрос и ответ текстом — экран не показывает номер."""
    question_id = ask_once(home).question_id
    pool_close(home, question_id, "answered")

    text = ask.exchange_of(home, question_id)

    assert text["question"]
    assert text["answer"]
    # Одна строка: текст живёт в подсказке маркера, переносы ломают вёрстку.
    assert "\n" not in text["question"] and "\n" not in text["answer"]


def test_exchange_of_is_quiet_without_pool_or_number(home: Path) -> None:
    """Нет пула, нет номера, чужой номер — пустой ответ, а не исключение."""
    question_id = ask_once(home).question_id
    assert ask.exchange_of(home, question_id)["question"]
    assert ask.exchange_of(home, None) == {}
    assert ask.exchange_of(home, "не номер") == {}
    assert ask.exchange_of(home, 999_999) == {}
    (home / "messaging" / "pool.db").unlink()
    assert ask.exchange_of(home, question_id) == {}


def test_exchange_of_clips_long_text_to_one_line(home: Path) -> None:
    """Длинный вопрос обрезается многоточием: подпись, а не документ."""
    long_text = "очень длинный вопрос " * 20
    question_id = ask_once(home, text=long_text).question_id

    text = ask.exchange_of(home, question_id)

    assert len(text["question"]) <= 161
    assert text["question"].endswith("…")



def test_deal_timeline_carries_question_and_answer_text(client: TestClient, home: Path) -> None:
    """Таймлайн дела отдаёт текст вопроса и ответа: экран не рисует номера."""
    question_id = ask_once(home).question_id
    pool_close(home, question_id, "answered")
    # Ответ становится фактом журнала только после дочитывания пула.
    sync_now(home)

    timeline = client.get(f"/deal/{DEAL}").json()["timeline"]
    asked = next(event for event in timeline if event["kind"] == "question_asked")
    answered = next(event for event in timeline if event["kind"] == "question_answered")

    # Вопрос несёт свой текст и номер; ответ — и текст ответа.
    assert asked["question_text"]
    assert asked["question_id"] == question_id
    assert answered["answer_text"]
    # Ответа у момента вопроса ещё не было: история не переписывается.
    assert "answer_text" not in asked



# ----------------------------------------------------- human_kind в /now


def test_human_kind_status_until_a_question_exists(client: TestClient) -> None:
    """Блок без вопроса: `human_kind=status` — человек нужен, вопроса нет."""
    waiting = waiting_now(client)

    assert waiting["state"] == "wait_human"
    assert waiting["human_kind"] == "status"


def test_human_kind_question_after_ask(client: TestClient) -> None:
    """После `ask()` дело ждёт ответа на конкретный вопрос: `human_kind=question`."""
    question_id = client.post(f"/ask/{DEAL}", json={"text": "Даёшь ок?"}).json()["question_id"]

    waiting = waiting_now(client)

    assert waiting["state"] == "wait_human"
    assert waiting["human_kind"] == "question"
    assert f"#{question_id}" in waiting["evidence"]


# --------------------------------------------------- B1: цикл целиком


def test_foreign_question_answer_does_not_leave_deal_waiting_for_answer(
    client: TestClient, home: Path
) -> None:
    """Цикл B1, случай «чужой вопрос»: подхват → ответ в пуле → `sync` → не «ждёт ответа».

    До фикса: `ask()` писал `question_asked` по чужому вопросу, ответ на него в
    журнал не попадал никогда, и fold вечно давал `wait_human` +
    `human_kind='question'` — дело залипало «ждёт ответа» на весь остаток жизни.
    """
    foreign_id = add_foreign_question(home)

    adopted = client.post(f"/ask/{DEAL}", json={"text": "наш вопрос"}).json()
    assert adopted["adopted"] is True and adopted["question_id"] == foreign_id
    # Живой вопрос пула ещё виден поверхности — честно, с пометкой «в журнале нет».
    live = waiting_now(client)
    assert live["state"] == "wait_human"
    assert live["human_kind"] == "question"

    pool_close(home, foreign_id, "answered", answer="да")
    answers = sync_now(home)
    assert answers["appended"] == 0

    # Сначала то, что видит человек: дело больше не «ждёт ответа».
    after = waiting_now(client)
    assert after["human_kind"] == "status"
    assert "вопрос" not in after["evidence"]
    assert open_questions(home) == []


def test_expired_ttl_does_not_leave_deal_waiting_for_answer(
    client: TestClient, home: Path
) -> None:
    """Цикл B1, случай «истёк TTL»: свой вопрос → пул `expired` → `sync` → не «ждёт ответа»."""
    question_id = client.post(f"/ask/{DEAL}", json={"text": "Нужен ответ"}).json()["question_id"]
    assert waiting_now(client)["human_kind"] == "question"

    pool_close(home, question_id, "expired")
    answers = sync_now(home)
    assert answers == {"appended": 1, "errors": []}

    assert open_questions(home) == []
    after = waiting_now(client)
    assert after["state"] == "wait_human"
    assert after["human_kind"] == "status"


def test_answered_own_question_closes_one_fact(client: TestClient, home: Path) -> None:
    """Цикл B1, случай «ответ»: `answered` → ровно один факт, повтор — no-op."""
    question_id = client.post(f"/ask/{DEAL}", json={"text": "Нужен ответ"}).json()["question_id"]
    pool_close(home, question_id, "answered", answer="ок")

    assert sync_now(home) == {"appended": 1, "errors": []}
    assert sync_now(home) == {"appended": 0, "errors": []}

    answered = [event for event in journal_events(home) if event["kind"] == "question_answered"]
    assert [event["payload"]["question_id"] for event in answered] == [question_id]
    assert open_questions(home) == []
    assert waiting_now(client)["human_kind"] == "status"


# --------------------------------------------- запись в пул — только ask.py

REPO_ROOT = MODULE_PATH.parent.parent

# Пишущие операторы над `questions` — в любом регистре и на любом языке репозитория.
POOL_WRITE_RE = re.compile(
    r"\b(?:insert(?:\s+or\s+\w+)?\s+into|replace\s+into|update|delete\s+from)\s+[\"'`]?questions\b",
    re.IGNORECASE,
)

SCAN_SUFFIXES = {".py", ".js", ".mjs", ".sh", ".sql", ".md"}
SCAN_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".worktrees", ".venv"}

# Сборка пути пула в коде (`home / "messaging" / "pool.db"`).
POOL_PATH_RE = re.compile(r"[\"']messaging[\"']\s*/\s*[\"']pool\.db[\"']")


def repo_files() -> list[Path]:
    """Все файлы репозитория, где может жить SQL (без служебных каталогов)."""
    out: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if SCAN_SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
            continue
        out.append(path)
    return out


def test_only_ask_module_writes_the_pool() -> None:
    """Запись в пул — только через `ask.py`; остальной репозиторий его не меняет.

    Проверка по всему репозиторию и регистронезависимо: строчный `insert into
    questions` и писатель в `bin/`/`desktop/` раньше проходили мимо подстрочного
    поиска четырёх литералов в `dashboard/*.py`. Тесты пишут в свои фикстуры — это
    не продовый путь, поэтому `tests/` из проверки исключён явно, а не «по факту».

    Исключён и `dashboard/demo.py`: прогон строит **свой** пул в песочном корне
    (`build_sandbox`), чтобы показать, что ответ пришёл из пула. Живой пул он не
    открывает на запись, и это проверяется отдельно (`tests/test_demo.py`:
    mtime живой `pool.db` после прогона не меняется), а не на слово.
    """
    offenders: list[str] = []
    for path in repo_files():
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] == "tests" or rel in (Path("dashboard") / "ask.py", Path("dashboard") / "demo.py"):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if POOL_WRITE_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == []


def test_pool_is_opened_read_only_outside_ask_module() -> None:
    """Пул вне `ask.py` открывается только `?mode=ro` — второй тест инварианта 2.

    Смотрим на каждое `sqlite3.connect(...)` продового кода в файлах, которые
    вообще знают про пул: если аргумент не `_ro(...)`/`connect_ro(...)` и в вызове
    нет `mode=ro`, это путь записи, и его владельцем может быть только `ask.py`.
    Свой журнал (`duty/duty.db`) сюда не попадает: он не пул и пишется по праву.
    """
    offenders: list[str] = []
    for path in repo_files():
        rel = path.relative_to(REPO_ROOT)
        if path.suffix != ".py" or rel.parts[0] == "tests":
            continue
        if rel in (Path("dashboard") / "ask.py", Path("dashboard") / "demo.py"):
            continue
        source = path.read_text(encoding="utf-8")
        # Проверяются ровно те файлы, которые собирают путь пула: упоминание
        # `pool.db` в прозе (журнал, докстринги) — не путь записи.
        if not POOL_PATH_RE.search(source):
            continue
        for match in re.finditer(r"sqlite3\.connect\(", source):
            call = source[match.start() : match.start() + 400]
            if "mode=ro" in call or "_ro(" in call or "connect_ro(" in call:
                continue
            lineno = source.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{lineno}: {call.splitlines()[0]}")
    assert offenders == []


def test_read_routes_do_not_touch_the_pool(client: TestClient, home: Path) -> None:
    """Поведением: чтение пула не меняет его файл (mtime и размер те же)."""
    db = home / "messaging" / "pool.db"
    before = (db.stat().st_mtime_ns, db.stat().st_size)

    client.get("/now")
    client.get(f"/deal/{DEAL}")
    client.get("/health")
    client.get("/sources")
    client.get(f"/duty/{DEAL}/attempts")

    after = (db.stat().st_mtime_ns, db.stat().st_size)
    assert after == before
    assert pool_rows(home) == []
