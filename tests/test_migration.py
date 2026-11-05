"""Миграция базы 0.0.5 в схему 0.0.6: доски уходят, факты остаются.

Проверяется самая рискованная ветка плагина — пересборка `deals` и `events`
(create new → copy → drop → rename). Фикстура строит базу 0.0.5 по-настоящему:
таблица `boards`, `deals.board NOT NULL` с индексом `deals_board`, `events.board
NOT NULL` и немного данных (три дела с разными исходами, события, комментарий).

Что обязано уцелеть: `id`, `payload` (кроме добавленного `$.board`),
`idempotency_key`, порядок `id` и `fold` каждого дела. Что обязано уйти: таблица
`boards`, колонки `board` и индекс `deals_board`. Что обязано появиться:
значение доски в `payload.$.board` у каждого старого события.

Второй `open_store` — no-op: миграция идемпотентна по проверке схемы. Остаток
прерванного запуска (`deals_v6`/`events_v6` без данных) снимается сам: повторный
запуск лечит базу, ручного `DROP` не нужно.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_migration", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

journal = plugin_api.journal
deals = plugin_api.deals

NOW = 1_800_000_000

# Схема 0.0.5 дословно: доска — сущность, дело принадлежит доске, событие тоже.
LEGACY_SCHEMA = """
CREATE TABLE boards (
  slug       TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE deals (
  id         TEXT PRIMARY KEY,
  board      TEXT NOT NULL REFERENCES boards(slug),
  title      TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  assignee   TEXT,
  status     TEXT NOT NULL,
  origin     TEXT NOT NULL DEFAULT 'duty',
  external_ref TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE deal_links (
  parent_id TEXT NOT NULL REFERENCES deals(id),
  child_id  TEXT NOT NULL REFERENCES deals(id),
  PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE comments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id    TEXT NOT NULL REFERENCES deals(id),
  author     TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE attachments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id      TEXT NOT NULL REFERENCES deals(id),
  filename     TEXT NOT NULL,
  path         TEXT NOT NULL,
  content_type TEXT,
  size_bytes   INTEGER NOT NULL,
  created_at   INTEGER NOT NULL
);

CREATE TABLE leases (
  deal_id    TEXT NOT NULL REFERENCES deals(id),
  stage      TEXT NOT NULL,
  attempt    INTEGER NOT NULL,
  pid        INTEGER NOT NULL,
  taken_at   INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (deal_id, stage)
);
CREATE INDEX deals_board ON deals(board, status);
CREATE INDEX comments_deal ON comments(deal_id, id);
CREATE INDEX attachments_deal ON attachments(deal_id, id);

CREATE TABLE events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              INTEGER NOT NULL,
  recorded_at     INTEGER NOT NULL,
  deal_id         TEXT    NOT NULL,
  board           TEXT    NOT NULL,
  kind            TEXT    NOT NULL,
  payload         TEXT    NOT NULL DEFAULT '{}',
  source          TEXT    NOT NULL,
  idempotency_key TEXT    NOT NULL
);
CREATE UNIQUE INDEX events_idem ON events(idempotency_key);
CREATE INDEX events_deal ON events(deal_id, id);
"""

# Три дела: заблокированное, закрытое и сданное в ревью — по ним видно, что fold
# читает разные ветки правил и что миграция их не двигает.
LEGACY_DEALS = (
    ("d_0ba5e001", "stack", "заблокированное", "blocked", "jira:RP-1", NOW - 9000),
    ("d_0ba5e002", "stack", "закрытое", "done", None, NOW - 8000),
    ("d_0ba5e003", "hermes-duty", "на ревью", "review", "jira:RP-2#2", NOW - 7000),
)

LEGACY_EVENTS = (
    ("d_0ba5e001", "stack", "created", {"spec": None, "parents": [], "external_ref": "jira:RP-1"}, NOW - 9000),
    ("d_0ba5e001", "stack", "claimed", {"owner": "implementer"}, NOW - 8900),
    (
        "d_0ba5e001",
        "stack",
        "blocked",
        {"block_kind": "needs_input", "reason": "нужен выбор", "author": "max"},
        NOW - 8800,
    ),
    ("d_0ba5e002", "stack", "created", {"spec": None, "parents": [], "external_ref": None}, NOW - 8000),
    (
        "d_0ba5e002",
        "stack",
        "attempt_started",
        {"run_id": "d_0ba5e002:impl#1", "stage": "impl", "attempt": 1, "pid": None},
        NOW - 7900,
    ),
    (
        "d_0ba5e002",
        "stack",
        "attempt_ended",
        {
            "run_id": "d_0ba5e002:impl#1",
            "stage": "impl",
            "attempt": 1,
            "outcome": "done",
            "kind": None,
            "reason": "",
            "base": None,
            "commit": "abc123",
        },
        NOW - 7800,
    ),
    ("d_0ba5e002", "stack", "completed", {"summary": "сделано"}, NOW - 7700),
    (
        "d_0ba5e003",
        "hermes-duty",
        "created",
        {"spec": None, "parents": [], "external_ref": "jira:RP-2#2"},
        NOW - 7000,
    ),
    ("d_0ba5e003", "hermes-duty", "review_requested", {"reviewer": "reviewer", "summary": "сдано"}, NOW - 6900),
)


def _event_key(deal_id: str, kind: str, ts: int) -> str:
    """Ключ идемпотентности старого события: своё значение, лишь бы уникальное."""
    return f"cli:{deal_id}:{kind}:{ts}"


def make_legacy(home: Path) -> Path:
    """База 0.0.5 в `home/duty/duty.db` — как её оставляла версия до 0.0.6."""
    path = home / "duty" / "duty.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO boards (slug, name, created_at) VALUES ('stack', 'Стек', ?)",
            (NOW - 10_000,),
        )
        conn.execute(
            "INSERT INTO boards (slug, name, created_at) VALUES ('hermes-duty', 'Duty', ?)",
            (NOW - 10_000,),
        )
        for deal_id, board, title, status, external_ref, created_at in LEGACY_DEALS:
            conn.execute(
                "INSERT INTO deals (id, board, title, body, assignee, status, origin, "
                "external_ref, created_at, updated_at) "
                "VALUES (?, ?, ?, '', 'implementer', ?, 'duty', ?, ?, ?)",
                (deal_id, board, title, status, external_ref, created_at, created_at + 100),
            )
        conn.execute(
            "INSERT INTO comments (deal_id, author, body, created_at) VALUES (?, 'max', 'итог', ?)",
            ("d_0ba5e001", NOW - 8700),
        )
        for deal_id, board, kind, payload, ts in LEGACY_EVENTS:
            conn.execute(
                "INSERT INTO events (ts, recorded_at, deal_id, board, kind, payload, source, "
                "idempotency_key) VALUES (?, ?, ?, ?, ?, ?, 'cli', ?)",
                (
                    ts,
                    ts,
                    deal_id,
                    board,
                    kind,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    _event_key(deal_id, kind, ts),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def raw(path: Path) -> sqlite3.Connection:
    """Соединение без схемы плагина: смотрим базу как файл, а не как store."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row["name"]) for row in rows}


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def legacy_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """События старой базы: доска отдельной колонкой, payload разобран."""
    out: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM events ORDER BY id"):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        out.append(item)
    return out


def folds(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """`fold` каждого дела — то, что миграция обязана сохранить без изменений."""
    by_deal: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_deal.setdefault(str(event["deal_id"]), []).append(event)
    return {
        deal_id: journal.fold(ordered, now=NOW) for deal_id, ordered in by_deal.items()
    }


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Строки store и журнала как есть — сравнивать «до» и «после» дословно."""
    rows = [dict(row) for row in conn.execute("SELECT * FROM deals ORDER BY id")]
    events = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]
    return {"deals": rows, "events": events}


def no_rebuild_leftovers(conn: sqlite3.Connection) -> None:
    """Служебных таблиц пересборки не осталось — иначе следующее открытие упадёт."""
    assert "deals_v6" not in tables(conn)
    assert "events_v6" not in tables(conn)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes с базой 0.0.5; плагин читает его через `HERMES_HOME`."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    make_legacy(root)
    return root


def test_005_base_is_migrated_to_store_without_boards(home: Path) -> None:
    """Пересборка: доски уходят, `id`/`payload`/`idempotency_key` и `fold` целы."""
    path = deals.store_path(home)
    before = raw(path)
    try:
        old_events = legacy_events(before)
        old_folds = folds(old_events)
        old_deals = [dict(row) for row in before.execute("SELECT * FROM deals ORDER BY id")]
        assert "boards" in tables(before) and "board" in columns(before, "events")
        assert "board" in columns(before, "deals")
    finally:
        before.close()

    conn = deals.open_store(home)  # открытие на запись и есть миграция
    assert conn is not None
    try:
        after = snapshot(conn)
        assert "boards" not in tables(conn)
        assert "board" not in columns(conn, "deals")
        assert "board" not in columns(conn, "events")
        assert "deals_board" not in {
            row[0] for row in conn.execute("select name from sqlite_master where type='index'")
        }
        no_rebuild_leftovers(conn)

        # дело адресуется id, а группировка — repo: у старых дел он не выдуман
        assert [row["id"] for row in after["deals"]] == [row["id"] for row in old_deals]
        assert {row["repo"] for row in after["deals"]} == {None}
        assert [row["title"] for row in after["deals"]] == [row["title"] for row in old_deals]
        assert [row["external_ref"] for row in after["deals"]] == [
            row["external_ref"] for row in old_deals
        ]

        # события: тот же порядок id, те же ключи идемпотентности, payload — плюс board
        assert [row["id"] for row in after["events"]] == [row["id"] for row in old_events]
        assert [row["idempotency_key"] for row in after["events"]] == [
            row["idempotency_key"] for row in old_events
        ]
        assert [row["kind"] for row in after["events"]] == [row["kind"] for row in old_events]
        assert [row["ts"] for row in after["events"]] == [row["ts"] for row in old_events]
        for old, new in zip(old_events, after["events"]):
            kept = json.loads(new["payload"])
            assert kept.pop("board") == old["board"]  # доска переехала в payload
            assert kept == old["payload"]  # остальное не тронуто

        # значение доски не NULL ровно там, где была колонка, и совпадает с ней
        nulls = conn.execute(
            "SELECT COUNT(*) FROM events WHERE json_extract(payload, '$.board') IS NULL"
        ).fetchone()[0]
        assert nulls == 0
        assert after["events"] and len(after["events"]) == len(old_events)

        # fold трёх дел одинаков до и после: пересборка не сдвинула факты
        assert folds(journal.read_events(conn)) == old_folds
    finally:
        conn.close()


def test_second_open_store_is_a_no_op(home: Path) -> None:
    """Повторное открытие на запись ничего не меняет: идемпотентность по проверке."""
    conn = deals.open_store(home)
    assert conn is not None
    try:
        first = snapshot(conn)
        first_schema = {
            str(row["name"]): str(row["sql"])
            for row in conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name")
        }
    finally:
        conn.close()

    again = deals.open_store(home)
    assert again is not None
    try:
        assert snapshot(again) == first
        assert {
            str(row["name"]): str(row["sql"])
            for row in again.execute("SELECT name, sql FROM sqlite_master ORDER BY name")
        } == first_schema
        no_rebuild_leftovers(again)
    finally:
        again.close()


def test_failed_rebuild_is_self_healing(home: Path) -> None:
    """Мусорная `deals_v6`/`events_v6` от прошлого запуска не мешает миграции.

    Падение после `CREATE TABLE *_v6` оставляло пустые служебные таблицы, и
    следующее открытие на запись падало на «table deals_v6 already exists».
    Теперь `DROP TABLE IF EXISTS` перед `CREATE` лечит базу сам.
    """
    path = deals.store_path(home)
    dirty = raw(path)
    try:
        dirty.execute("CREATE TABLE deals_v6 (id TEXT PRIMARY KEY)")
        dirty.execute("CREATE TABLE events_v6 (id INTEGER PRIMARY KEY)")
        dirty.commit()
    finally:
        dirty.close()

    conn = deals.open_store(home)
    assert conn is not None
    try:
        no_rebuild_leftovers(conn)
        assert "boards" not in tables(conn)
        assert "board" not in columns(conn, "deals")
        assert "board" not in columns(conn, "events")
        # данные целы: мусор от сбоя лежал рядом, а не вместо строк
        assert conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0] == len(LEGACY_DEALS)
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == len(LEGACY_EVENTS)
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE json_extract(payload, '$.board') IS NULL"
        ).fetchone()[0] == 0
        survived = snapshot(conn)
    finally:
        conn.close()

    again = deals.open_store(home)
    assert again is not None
    try:
        assert snapshot(again) == survived
        no_rebuild_leftovers(again)
    finally:
        again.close()


def test_payload_board_key_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Уже записанный `payload.$.board` остаётся как был, колонка его не перетирает."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    path = make_legacy(root)
    own = raw(path)
    try:
        own.execute(
            "UPDATE events SET payload = ? WHERE deal_id = 'd_0ba5e001' AND kind = 'claimed'",
            (json.dumps({"owner": "implementer", "board": "из-payload"}),),
        )
        own.commit()
    finally:
        own.close()

    conn = deals.open_store(root)
    assert conn is not None
    try:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM events WHERE deal_id = 'd_0ba5e001' AND kind = 'claimed'"
            ).fetchone()[0]
        )
        assert payload == {"owner": "implementer", "board": "из-payload"}
    finally:
        conn.close()


def test_broken_payload_keeps_board_and_raw_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Неразбираемый payload не выбрасывается: доска в `$.board`, текст в `payload_raw`."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    path = make_legacy(root)
    broken = raw(path)
    try:
        broken.execute(
            "UPDATE events SET payload = 'не json' "
            "WHERE deal_id = 'd_0ba5e001' AND kind = 'claimed'"
        )
        broken.commit()
    finally:
        broken.close()

    conn = deals.open_store(root)
    assert conn is not None
    try:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM events WHERE deal_id = 'd_0ba5e001' AND kind = 'claimed'"
            ).fetchone()[0]
        )
        assert payload == {"board": "stack", "payload_raw": "не json"}
    finally:
        conn.close()
