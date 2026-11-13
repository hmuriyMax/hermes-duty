"""Слова человека в окне лида: текст ответа пула и тело комментария.

Журнал слов человека не хранит: у `question_answered` есть только `answer_len`,
у `commented` — автор и `len`, а сам текст живёт в `messaging/pool.db` и в таблице
`comments`. Лиду этого текста не хватало: он видел «ответ был, 171 символ» и решал
вслепую (живое дело `d_1c53fbd0`: человек ответил в пул, лид поднялся и не смог
прочитать ответ). Проверяется контракт чтения — что текст доезжает до `duty_wait`
и до строки дайджеста, — а не пересказ фактов словами:

1. `deals.human_text` отдаёт текст ответа по номеру вопроса пула;
2. он же отдаёт тело комментария, а не его длину;
3. длинный текст режется с честной пометкой;
4. `tools._brief` (то, что лид видит в `duty_wait`) несёт `answer`/`body`;
5. строка дайджеста лида (`lead._fact_line`) называет слова человека целиком;
6. текста нет (комментарий без строки, пул без ответа) — пустая строка, а не падение.

Фикстуры — свои маленькие `duty.db`/`pool.db` в `tmp_path` по реальной схеме;
живые источники не читаются и не пишутся.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEAL = "d_0000aa22"
NOW = 1_800_000_000

POOL_SCHEMA = """
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  project TEXT,
  text TEXT NOT NULL,
  text_full TEXT,
  origin TEXT,
  options_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
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


def _load(name: str, filename: str) -> object:
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = _load("hermes_duty_journal_human", "journal.py")
deals = _load("hermes_duty_deals_human", "deals.py")
ask = _load("hermes_duty_ask_human", "ask.py")
tools = _load("hermes_duty_tools_human", "tools.py")
lead = _load("hermes_duty_lead_human", "lead.py")


# ------------------------------------------------------------------ фикстуры


def make_deal(home: Path) -> str:
    """Своё дело в store — строкой, без события: тесты считают свои факты."""
    conn = deals.open_store(home)
    assert conn is not None
    try:
        with conn:
            conn.execute(
                "INSERT INTO deals (id, repo, title, body, assignee, status, origin, created_at, updated_at) "
                "VALUES (?, 'stack', 'дело', 'тело', 'implementer', 'blocked', 'duty', ?, ?)",
                (DEAL, NOW - 3600, NOW - 3600),
            )
    finally:
        conn.close()
    return DEAL


def make_pool(home: Path, *, answer: str | None) -> int:
    """Ответ пула по делу: строка `questions` так, как её пишет гейтвей."""
    path = home / "messaging" / "pool.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(POOL_SCHEMA)
        cur = conn.execute(
            "INSERT INTO questions (session_key, text, origin, status, answer, answered_via) "
            "VALUES (?, 'перенести поверх?', 'duty', ?, ?, 'manual')",
            (f"t_{DEAL}", "answered" if answer else "pending", answer),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    make_deal(path)
    return path


def answered_fact(home: Path, question_id: int, answer_len: int) -> dict:
    """Факт `question_answered` в том виде, в каком его пишет `sync_answers`.

    Пишется через журнал, а не через синхронизацию пула: тесту нужен ровно этот
    факт, а `sync_answers` проверяется своими тестами (`tests/test_ask.py`).
    """
    conn = deals.open_store(home)
    assert conn is not None
    try:
        journal.append(
            conn,
            [
                {
                    "ts": NOW,
                    "recorded_at": NOW,
                    "deal_id": DEAL,
                    "repo": "stack",
                    "kind": "question_answered",
                    "payload": {
                        "question_id": question_id,
                        "answer_len": answer_len,
                        "answered_via": "manual",
                    },
                    "source": "ask",
                    "idempotency_key": f"ask:{question_id}:question_answered",
                }
            ],
        )
        return next(e for e in journal.read_events(conn, DEAL) if e["kind"] == "question_answered")
    finally:
        conn.close()


def events_of(home: Path) -> list[dict]:
    conn = deals.open_store(home)
    assert conn is not None
    try:
        return journal.read_events(conn, DEAL)
    finally:
        conn.close()


# ------------------------------------------------- текст ответа и комментария


def test_human_text_reads_the_pool_answer(home: Path) -> None:
    """Ответ человека читается по номеру вопроса: журнал хранит только длину."""
    question_id = make_pool(home, answer="разбери сам, если никто не занимается")
    event = answered_fact(home, question_id, answer_len=39)
    conn = deals.open_store(home)
    assert conn is not None
    try:
        # В журнале текста нет — и это проверяется, иначе тест прошёл бы даром.
        assert "answer" not in journal.payload_of(event)
        text = deals.human_text(conn, home, DEAL, "question_answered", journal.payload_of(event))
    finally:
        conn.close()

    assert text == "разбери сам, если никто не занимается"


def test_human_text_reads_the_comment_body(home: Path) -> None:
    """Комментарий человека приходит телом: `len` лиду ничего не говорит."""
    conn = deals.open_store(home)
    assert conn is not None
    try:
        with conn:
            deals.add_comment(conn, DEAL, author="max", body="закоммить и повтори", source="cli")
        event = next(e for e in journal.read_events(conn, DEAL) if e["kind"] == "commented")
        payload = journal.payload_of(event)
        assert payload.get("body") is None
        text = deals.human_text(conn, home, DEAL, "commented", payload)
    finally:
        conn.close()

    assert text == "закоммить и повтори"


def test_long_human_text_is_clipped_with_a_mark(home: Path) -> None:
    """Длинный текст режется пометкой: окно лида — не документ, но и не молчание."""
    conn = deals.open_store(home)
    assert conn is not None
    try:
        with conn:
            deals.add_comment(conn, DEAL, author="max", body="очень длинная реплика " * 200, source="cli")
        event = next(e for e in journal.read_events(conn, DEAL) if e["kind"] == "commented")
        text = deals.human_text(conn, home, DEAL, "commented", journal.payload_of(event))
    finally:
        conn.close()

    assert text.endswith("… (обрезано)")
    assert len(text) <= deals.HUMAN_TEXT_LIMIT + len("… (обрезано)")


def test_missing_text_is_silent_not_a_failure(home: Path) -> None:
    """Нет строки комментария или ответа — пустая строка: это «текста нет»."""
    conn = deals.open_store(home)
    assert conn is not None
    try:
        assert deals.human_text(conn, home, DEAL, "commented", {"author": "max", "len": 5}) == ""
        assert deals.human_text(conn, home, DEAL, "question_answered", {"question_id": 999_999}) == ""
        assert deals.human_text(None, None, DEAL, "commented", {"len": 5}) == ""
    finally:
        conn.close()


# ------------------------------------------- то, что видит лид: wait и дайджест


def test_brief_finds_the_comment_by_the_caller_deal_id(home: Path) -> None:
    """Номер дела приходит и аргументом: факт без `deal_id` не должен терять тело.

    Живое дело `d_1c53fbd0`: комментарий вернулся лиду одной длиной — номер дела
    искали только в строке факта, а вызывающий его и так знает.
    """
    conn = deals.open_store(home)
    assert conn is not None
    try:
        with conn:
            deals.add_comment(conn, DEAL, author="hermes", body="подними review", source="cli")
        event = dict(next(e for e in journal.read_events(conn, DEAL) if e["kind"] == "commented"))
        event.pop("deal_id")  # так факт выглядит у читателя, который номера не несёт
        brief = tools._brief(event, home=home, conn=conn, deal_id=DEAL)
    finally:
        conn.close()

    assert brief["payload"]["body"] == "подними review"


def test_wait_brief_carries_the_answer(home: Path) -> None:
    """`duty_wait` отдаёт факт с текстом ответа: иначе лид решает вслепую."""
    question_id = make_pool(home, answer="перенеси поверх")
    event = answered_fact(home, question_id, answer_len=14)
    conn = deals.open_store(home)
    assert conn is not None
    try:
        brief = tools._brief(event, home=home, conn=conn)
    finally:
        conn.close()

    assert brief["payload"]["answer"] == "перенеси поверх"
    assert brief["payload"]["answer_len"] == 14


def test_digest_line_carries_the_comment_body(home: Path) -> None:
    """Строка дайджеста называет реплику человека целиком, а не её длину."""
    conn = deals.open_store(home)
    assert conn is not None
    try:
        with conn:
            deals.add_comment(conn, DEAL, author="max", body="подожди, пока закончат", source="cli")
        event = next(e for e in journal.read_events(conn, DEAL) if e["kind"] == "commented")
        line = lead._fact_line(event, NOW, lead._human_text_of(conn, home))
    finally:
        conn.close()

    assert "подожди, пока закончат" in line
    assert "комментарий max" in line


def test_digest_line_carries_the_answer_text(home: Path) -> None:
    """Ответ в дайджесте — текстом: «(manual, 39 симв.)» без него бессмысленно."""
    question_id = make_pool(home, answer="продолжай, я посмотрю")
    event = answered_fact(home, question_id, answer_len=20)
    conn = deals.open_store(home)
    assert conn is not None
    try:
        line = lead._fact_line(event, NOW, lead._human_text_of(conn, home))
    finally:
        conn.close()

    assert "продолжай, я посмотрю" in line
