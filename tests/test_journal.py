"""Журнал событий, fold и ingest (контракт `docs/JOURNAL.md`).

Проверяется то, что нельзя увидеть глазами при ревью: схема совпадает с
контрактом, повторный ingest ничего не дописывает, fold не открывает ни одной БД,
состояние дела считается по событиям, а дело без событий честно помечается
«journal: empty». `question_asked`/`question_answered` пишет `ask()`
(`dashboard/ask.py`, свои тесты — у него), поэтому здесь они проверяются через
fold и `append`: набор `kind` на них не расширяется.

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
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_journal", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

journal = plugin_api.journal

NOW = 1_800_000_000

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


# ------------------------------------------------------------------ фикстуры


def board_db(home: Path, slug: str = "stack") -> Path:
    """Путь БД доски (`default` — корневой kanban.db, как в живом Hermes)."""
    if slug == "default":
        return home / "kanban.db"
    return home / "kanban" / "boards" / slug / "kanban.db"


def make_board(home: Path, slug: str = "stack") -> Path:
    path = board_db(home, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(KANBAN_SCHEMA)
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


def add_task(
    conn: sqlite3.Connection,
    task_id: str = "t_0001",
    *,
    status: str = "ready",
    title: str = "дело",
    created_at: int = NOW - 3600,
    claim_lock: str | None = None,
    claim_expires: int | None = None,
    block_kind: str | None = None,
    worker_pid: int | None = None,
    current_run_id: int | None = None,
    consecutive_failures: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, created_at, claim_lock, claim_expires,"
        " block_kind, worker_pid, current_run_id, consecutive_failures)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            title,
            "тело дела",
            "implementer",
            status,
            created_at,
            claim_lock,
            claim_expires,
            block_kind,
            worker_pid,
            current_run_id,
            consecutive_failures,
        ),
    )


def add_run(
    conn: sqlite3.Connection,
    task_id: str = "t_0001",
    *,
    status: str = "running",
    started_at: int = NOW - 600,
    ended_at: int | None = None,
    outcome: str | None = None,
    worker_pid: int | None = None,
    error: str | None = None,
    claim_lock: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, worker_pid, error, claim_lock)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, "implementer", status, started_at, ended_at, outcome, worker_pid, error, claim_lock),
    )
    return int(cur.lastrowid or 0)


def add_kanban_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    task_id: str = "t_0001",
    payload: dict | None = None,
    run_id: int | None = None,
    created_at: int = NOW - 120,
) -> int:
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
        (task_id, run_id, kind, json.dumps(payload or {}, ensure_ascii=False), created_at),
    )
    return int(cur.lastrowid or 0)


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
    """Корень Hermes: роуты и ingest читают его через `HERMES_HOME`."""
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


def events(*pairs: tuple[str, dict | None], **kwargs: Any) -> list[dict[str, Any]]:
    """События журнала для fold: `("blocked", {"reason": …})` и т.п., id по порядку."""
    start = int(kwargs.get("start", 1))
    ts = int(kwargs.get("ts", NOW - 100))
    out: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs):
        kind, payload = pair
        out.append(
            {
                "id": start + index,
                "ts": ts + index,
                "recorded_at": ts + index,
                "deal_id": kwargs.get("deal_id", "t_0001"),
                "board": "stack",
                "kind": kind,
                "payload": payload or {},
                "source": "cli",
                "idempotency_key": f"cli:t_0001:{kind}:{index}",
            }
        )
    return out


# ---------------------------------------------------------------------- схема


def test_schema_matches_contract(home: Path) -> None:
    """Колонки, оба индекса и режимы — ровно как в `docs/JOURNAL.md`."""
    conn = journal.open_journal(home)
    assert conn is not None
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(events)")}
    assert list(columns) == [
        "id",
        "ts",
        "recorded_at",
        "deal_id",
        "board",
        "kind",
        "payload",
        "source",
        "idempotency_key",
    ]
    assert columns["id"]["pk"] == 1
    assert columns["payload"]["dflt_value"] == "'{}'"
    for name in ("ts", "recorded_at", "deal_id", "board", "kind", "source", "idempotency_key"):
        assert columns[name]["notnull"] == 1, name

    indexes = {row["name"]: row for row in conn.execute("PRAGMA index_list(events)")}
    assert indexes["events_idem"]["unique"] == 1
    assert indexes["events_deal"]["unique"] == 0
    idem = [row["name"] for row in conn.execute("PRAGMA index_info(events_idem)")]
    assert idem == ["idempotency_key"]
    deal = [row["name"] for row in conn.execute("PRAGMA index_info(events_deal)")]
    assert deal == ["deal_id", "id"]

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 2000
    conn.close()
    assert (home / "duty" / "duty.db").is_file()


def test_every_kind_has_a_row_in_the_contract() -> None:
    """Ни один `kind` не живёт в коде без строки в контракте (п.5 ревью W5).

    Таблица «Набор событий» — единственное место, где вид назван словами вместе с
    правилом fold. Тест держит соответствие в обе стороны: вид без строки —
    расхождение кода и контракта, строка без вида — обещание, которого нет.
    """
    doc = (Path(__file__).resolve().parents[1] / "docs" / "JOURNAL.md").read_text(encoding="utf-8")
    start = doc.index("## Набор событий")
    section = doc[start : doc.index("## ", start + len("## Набор событий"))]
    documented = {
        match.group(1) for match in re.finditer(r"^\|\s*`([a-z_]+)`\s*\|", section, flags=re.MULTILINE)
    }
    documented.discard("kind")  # заголовок таблицы

    assert documented == set(journal.EVENT_KINDS)


def test_missing_journal_is_empty_not_an_error(home: Path) -> None:
    """Отсутствие файла — «журнала ещё нет»: пустой журнал, без исключения."""
    assert not (home / "duty" / "duty.db").exists()

    conn = journal.open_journal(home)

    assert conn is not None
    assert journal.read_events(conn) == []
    assert journal.counts(conn) == {"events": 0, "deals": 0}
    conn.close()


def test_open_without_create_leaves_no_file(home: Path) -> None:
    """Читающий режим файла не создаёт: `None` — и каталога `duty/` тоже нет."""
    assert journal.open_journal(home, create=False) is None
    assert not (home / "duty").exists()


def test_append_ignores_duplicates_by_key(home: Path) -> None:
    """`INSERT OR IGNORE` по `idempotency_key`: второй раз — 0 записанных строк."""
    conn = journal.open_journal(home)
    batch = events(("created", {"title": "x"}), ("claimed", {"claim_lock": "L", "claim_expires": NOW}))

    assert journal.append(conn, batch) == 2
    assert journal.append(conn, batch) == 0
    assert journal.counts(conn) == {"events": 2, "deals": 1}
    conn.close()


def test_append_rejects_kind_outside_the_closed_set(home: Path) -> None:
    """Набор `kind` закрыт кодом: `append` отвергает вид вне `EVENT_KINDS` целиком.

    Без этой проверки «набор закрыт» держался дисциплиной: посторонний `kind` лёг
    бы в журнал, и fold встретил бы факт, для которого нет правила.
    """
    conn = journal.open_journal(home)
    batch = events(("created", None), ("heartbeat", {"pid": 1}))

    with pytest.raises(ValueError) as excinfo:
        journal.append(conn, batch)

    assert "heartbeat" in str(excinfo.value)
    # Пачка отвергается до записи: «почти записанной» половины в журнале не остаётся.
    assert journal.counts(conn) == {"events": 0, "deals": 0}
    conn.close()


def test_read_events_is_ordered_and_filtered(home: Path) -> None:
    """События идут по возрастанию `id`; `deal_id` и `since_id` — необязательный отбор."""
    conn = journal.open_journal(home)
    journal.append(
        conn,
        events(("created", None), deal_id="t_a")
        + events(("created", None), deal_id="t_b", start=10)
        + events(("blocked", {"reason": "r"}), deal_id="t_a", start=20),
    )

    all_ids = [event["id"] for event in journal.read_events(conn)]
    assert all_ids == sorted(all_ids)
    assert [event["deal_id"] for event in journal.read_events(conn, deal_id="t_a")] == ["t_a", "t_a"]
    assert journal.read_events(conn, since_id=all_ids[0])[0]["id"] == all_ids[1]
    # payload разворачивается в объект: у события без payload это `{}`, а не строка
    assert journal.read_events(conn)[0]["payload"] == {}
    conn.close()


# ---------------------------------------------------------------------- fold


def test_fold_of_empty_journal_does_not_claim_all_is_well() -> None:
    """Пустой журнал — не состояние: `state=None`, причина `empty` (правило 7)."""
    folded = journal.fold([])

    assert folded["active"] is True
    assert folded["state"] is None
    assert folded["reason"] == "empty"


def test_fold_does_not_open_any_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """fold — чистая функция: ни одной БД она не открывает (инвариант 4)."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("fold не имеет права открывать БД")

    monkeypatch.setattr(sqlite3, "connect", boom)
    folded = journal.fold(
        events(("created", None), ("blocked", {"block_kind": "needs_input", "reason": "нужно решение"})),
        now=NOW,
    )

    assert folded["state"] == "wait_human"


def test_fold_applies_events_in_id_order() -> None:
    """Правило 1: порядок — по `id`, а не по порядку в списке.

    По `id` сначала блок, потом его снятие → блокировки нет. Если бы fold брал
    порядок списка, тот же набор дал бы `wait_human` — тест фиксирует разницу.
    """
    batch = events(("blocked", {"reason": "нужно решение"}), ("unblocked", None))
    by_list_order = [batch[1], batch[0]]

    assert journal.fold(by_list_order)["reason"] == "no_live_fact"
    assert journal.fold(by_list_order) == journal.fold(batch)
    # А вот порядок по `id` в обратную сторону — это уже блок без снятия.
    assert journal.fold([batch[0]])["state"] == "wait_human"


def test_fold_last_completed_makes_deal_inactive() -> None:
    """Правило 2: последнее событие `completed` — дело не активное."""
    folded = journal.fold(events(("created", None), ("completed", {"status": "done"})))

    assert folded["active"] is False
    assert folded["state"] is None
    assert "status=done" in folded["evidence"]


def test_fold_closed_deal_is_not_reopened_by_non_state_kinds() -> None:
    """Правила 2 и 12: комментарий, вложение и правка закрытость не снимают.

    Закрытость считается по последнему state-changing событию, а не по последней
    строке журнала. Иначе `commented` поверх `completed` возвращал дело в срез с
    `active: True` и состоянием ожидания — «воскресшее» дело на экране.
    """
    folded = journal.fold(
        events(
            ("created", {"status": "ready"}),
            ("completed", {"status": "done"}),
            ("commented", {"author": "max", "len": 6}),
            ("attached", {"filename": "note.txt"}),
            ("patched", {"fields": ["title"]}),
        ),
        now=NOW,
    )

    assert folded["active"] is False
    assert folded["state"] is None
    assert folded["reason"] == "closed"
    assert "status=done" in folded["evidence"]
    # `last_kind` остаётся честной последней строкой журнала: разница между ней и
    # решением о закрытости и есть правило 2.
    assert folded["last_kind"] == "patched"
    # Набор «не открывающих живое ожидание» видов назван в одном месте и ровно этими
    # четырьмя: ответ на вопрос — закрывающий факт, а не переход.
    assert journal.NON_STATE_KINDS == ("commented", "attached", "patched", "question_answered")


def test_state_changing_kind_after_completed_reopens_honestly() -> None:
    """Обратная сторона правила 2: настоящий переход после `completed` снова активен.

    Блокировка закрытого дела — state-changing факт, и fold обязан его показать,
    иначе правило превратилось бы в «после `completed` журнал больше не читают».
    """
    folded = journal.fold(
        events(
            ("created", {"status": "ready"}),
            ("completed", {"status": "done"}),
            ("blocked", {"block_kind": "needs_input", "reason": "вернулось"}),
        ),
        now=NOW,
    )

    assert folded["active"] is True
    assert folded["state"] == "wait_human"


def test_late_question_answered_does_not_reopen_closed_deal() -> None:
    """Правила 2 и 12: поздний ответ на вопрос закрытое дело не воскрешает.

    Штатный хвост: вопрос задан, дело завершили, ответ пришёл позже
    (`sync_answers` пишет `question_answered` по закрытому делу). Ответ —
    закрывающий факт, и живое ожидание он открывать не может: иначе fold уходил в
    `no_live_fact` с `active: True`, и дело снова попадало в срез с состоянием
    ожидания, хотя `terminal: true`.
    """
    folded = journal.fold(
        events(
            ("created", {"status": "ready"}),
            ("question_asked", {"question_id": 42, "session_key": "t_0001"}),
            ("completed", {"status": "done"}),
            ("question_answered", {"question_id": 42, "answer_len": 3, "answered_via": "pool"}),
        ),
        now=NOW,
    )

    assert folded["active"] is False
    assert folded["state"] is None
    assert folded["reason"] == "closed"
    assert "status=done" in folded["evidence"]
    # `last_kind` по-прежнему честная последняя строка журнала.
    assert folded["last_kind"] == "question_answered"


def test_created_snapshot_gate_survives_a_comment() -> None:
    """Правило 8 считается по state-changing событию: комментарий гейт не отменяет."""
    folded = journal.fold(
        events(
            ("created", {"status": "todo", "parents": ["d_aabbccdd"]}),
            ("commented", {"author": "max", "len": 6}),
        ),
        now=NOW,
    )

    assert folded["state"] == "wait_gate"
    assert "d_aabbccdd" in folded["evidence"]


def test_fold_blocked_without_unblocked_is_wait_human_status() -> None:
    """Правило 3: блок без снятия — wait_human, и это «жду решения», а не «жду ответа»."""
    folded = journal.fold(
        events(("created", None), ("blocked", {"block_kind": "needs_input", "reason": "нужно решение"}))
    )

    assert folded["state"] == "wait_human"
    assert folded["human_kind"] == "status"
    assert "нужно решение" in folded["evidence"]


def test_fold_capability_block_is_capability_not_wait_human() -> None:
    """Доступ/права — отдельное состояние `capability` (таксономия 0.0.2)."""
    folded = journal.fold(
        events(("blocked", {"block_kind": "capability", "reason": "role-access: read path denied"}))
    )

    assert folded["state"] == "capability"
    assert folded["human_kind"] is None
    assert "role-access" in folded["evidence"]


def test_fold_capability_words_without_typed_kind() -> None:
    """Причина без typed `block_kind`, но со словом про доступ — всё равно capability."""
    folded = journal.fold(events(("blocked", {"reason": "нужен доступ к secrets"})), now=NOW)

    assert folded["state"] == "capability"


def test_fold_question_without_answer_is_wait_human_question() -> None:
    """Правило 6: `question_asked` без ответа — «ждёт ответа», `human_kind=question`."""
    folded = journal.fold(
        events(
            ("created", None),
            ("question_asked", {"question_id": 42, "session_key": "t_0001", "critical": 1, "timeout": 300}),
        ),
        now=NOW,
    )

    assert folded["state"] == "wait_human"
    assert folded["human_kind"] == "question"
    assert "#42" in folded["evidence"]


def test_fold_answered_question_is_not_waiting() -> None:
    """Ответ на вопрос снимает ожидание: тот же вопрос больше не «ждёт человек»."""
    folded = journal.fold(
        events(
            ("question_asked", {"question_id": 42}),
            ("question_answered", {"question_id": 42, "answer_len": 3, "answered_via": "pool"}),
        ),
        now=NOW,
    )

    assert folded["reason"] == "no_live_fact"


def test_fold_claim_with_expired_lease_is_dead_lease() -> None:
    """Правило 4: срок лизинга записан и прошёл, а попытки за лизингом не было.

    Возраст `claimed` здесь меньше прежнего порога: «протух» — это факт о сроке,
    и называет его срок из события, а не время с момента записи.
    """
    folded = journal.fold(
        events(("created", None), ("claimed", {"claim_lock": "L", "claim_expires": NOW - 100}), ts=NOW - 200),
        now=NOW,
    )

    assert folded["state"] == "dead_lease"
    assert "claim_lock" not in folded["evidence"] and "L" in folded["evidence"]


def test_fold_claim_without_expiry_is_not_dead_lease() -> None:
    """Правило 4: у `claimed` без срока нет свойства «протух».

    Своё дело пишет `claim_expires: NULL` (`claim`, `dashboard/deals.py`): аренды в
    0.0.3 нет, и срока у факта не записано. Возраст такого `claimed` ничего не
    доказывает — «протухший лизинг» это факт о сроке, а не о возрасте строки.
    """
    folded = journal.fold(
        events(("created", None), ("claimed", {"claim_lock": "L", "claim_expires": None}), ts=NOW - 3600),
        now=NOW,
    )

    assert folded["state"] != "dead_lease"
    assert folded["reason"] == "no_live_fact"


def test_fold_claim_with_unexpired_lease_is_not_dead_lease() -> None:
    """Правило 4: пока записанный срок не прошёл, старый `claimed` не «протух»."""
    folded = journal.fold(
        events(("created", None), ("claimed", {"claim_lock": "L", "claim_expires": NOW + 900}), ts=NOW - 3600),
        now=NOW,
    )

    assert folded["state"] != "dead_lease"


def test_fold_fresh_claim_is_not_dead_lease() -> None:
    """Только что взятый лизинг — не «протухший»: срок ещё не прошёл."""
    folded = journal.fold(
        events(("created", None), ("claimed", {"claim_lock": "L", "claim_expires": NOW + 900}), ts=NOW - 10),
        now=NOW,
    )

    assert folded["state"] is None
    assert folded["reason"] == "no_live_fact"


def test_fold_open_attempt_with_dead_pid_is_crashed() -> None:
    """Правило 5: попытка не закрыта, pid мёртв — `crashed`."""
    pid = absent_pid()
    folded = journal.fold(
        events(("attempt_started", {"run_id": 7, "pid": pid}), ts=NOW - 600), now=NOW
    )

    assert folded["state"] == "crashed"
    assert f"pid {pid} мёртв" in folded["evidence"]


def test_fold_open_attempt_with_live_pid_is_working() -> None:
    """Правило 5: pid жив — `working`, и текст не обещает проверки heartbeat."""
    pid = os.getpid()
    folded = journal.fold(events(("attempt_started", {"run_id": 7, "pid": pid}), ts=NOW - 60), now=NOW)

    assert folded["state"] == "working"
    assert f"pid {pid} жив" in folded["evidence"]
    assert "heartbeat в журнале не пишется" in folded["evidence"]


def test_fold_open_attempt_without_pid_is_no_signal() -> None:
    """Правило 5: pid не читается — `no_signal`, а не «работает»."""
    for payload in ({"run_id": 7}, {"run_id": 7, "pid": None}):
        folded = journal.fold(events(("attempt_started", payload), ts=NOW - 60), now=NOW)

        assert folded["state"] == "no_signal"
        assert "pid не записан" in folded["evidence"]


def test_fold_closed_attempt_is_not_a_live_fact() -> None:
    """`attempt_ended` закрывает попытку: открытой попытки больше нет."""
    pid = os.getpid()
    folded = journal.fold(
        events(
            ("attempt_started", {"run_id": 7, "pid": pid}),
            ("attempt_ended", {"run_id": 7, "outcome": "completed", "error": None}),
        ),
        now=NOW,
    )

    assert folded["reason"] == "no_live_fact"


def test_fold_pid_liveness_is_injectable() -> None:
    """Живость pid — шов, а не скрытая зависимость: тест задаёт ответ сам.

    Пункт 7 ревью: `fold` звал `os.kill`, то есть результат зависел от живого
    состояния процессов машины. Теперь ответ подставляется (`pid_probe`), и один и
    тот же набор событий даёт `working`/`crashed`, не спрашивая ядро.
    """
    batch = events(("attempt_started", {"run_id": 7, "pid": 999_999}), ts=NOW - 60)

    assert journal.fold(batch, now=NOW, pid_probe=lambda pid: True)["state"] == "working"
    assert journal.fold(batch, now=NOW, pid_probe=lambda pid: False)["state"] == "crashed"
    assert journal.fold(batch, now=NOW, pid_probe=lambda pid: None)["state"] == "no_signal"
    # Без шва поведение прежнее: мёртвый pid — `crashed`.
    assert journal.fold(batch, now=NOW)["state"] == "crashed"


def test_created_with_parents_is_wait_gate() -> None:
    """Правило 8 таксономии: от дела остался снимок `created` с родителями — гейт."""
    folded = journal.fold(events(("created", {"status": "todo", "parents": ["t_parent"]})), now=NOW)

    assert folded["state"] == "wait_gate"
    assert "t_parent" in folded["evidence"]
    # Факт назван снимком, а не сегодняшним состоянием графа.
    assert "на момент created" in folded["evidence"]


def test_fold_priority_wait_human_beats_crashed() -> None:
    """Порядок таксономии один: вопрос человеку важнее упавшей попытки."""
    pid = absent_pid()
    folded = journal.fold(
        events(
            ("attempt_started", {"run_id": 7, "pid": pid}),
            ("blocked", {"block_kind": "needs_input", "reason": "нужно решение"}),
        ),
        now=NOW,
    )

    assert folded["state"] == "wait_human"
    assert "ещё: crashed:" in folded["evidence"]


def test_ingest_expired_kanban_lease_still_folds_to_dead_lease(home: Path) -> None:
    """Правило 4 для чужого факта: истёкший срок kanban-лизинга — по-прежнему `dead_lease`.

    `claimed` от ingest несёт срок из payload источника, срок прошёл, попытки за
    лизингом не было — факт называется. Изменение правила касается только
    `claimed` без срока, а не проверки срока у чужих фактов.
    """
    board = make_board(home)
    conn = sqlite3.connect(board)
    try:
        add_task(conn, "t_expired", status="running", claim_lock="L", claim_expires=NOW - 100)
        add_kanban_event(
            conn,
            "claimed",
            task_id="t_expired",
            payload={"lock": "L", "expires": NOW - 100},
            created_at=NOW - 200,
        )
        conn.commit()
    finally:
        conn.close()

    ingest(board, home)

    conn = journal.open_journal(home, create=False)
    assert conn is not None
    try:
        folded = journal.fold(journal.read_events(conn, "t_expired"), now=NOW)
    finally:
        conn.close()

    assert folded["state"] == "dead_lease"
    assert "L" in folded["evidence"]


# -------------------------------------------------------------------- ingest


def ingest(board: Path, home: Path) -> dict[str, Any]:
    """Прогнать ingest по одной фикстурной доске."""
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        return journal.ingest(conn, home, plugin_api._discover_boards(home))
    finally:
        conn.close()


def journal_kinds(home: Path) -> list[str]:
    conn = journal.open_journal(home, create=False)
    assert conn is not None
    try:
        return [event["kind"] for event in journal.read_events(conn)]
    finally:
        conn.close()


def test_ingest_writes_facts_of_every_ingested_kind(home: Path) -> None:
    """Каждый вид из таблицы контракта, у которого есть строка источника, — в журнале."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_full", status="running", claim_lock="L1", claim_expires=NOW + 600)
    run_id = add_run(conn, "t_full", worker_pid=os.getpid(), claim_lock="L1")
    add_kanban_event(conn, "created", task_id="t_full", payload={"assignee": "implementer", "status": "todo", "parents": []}, created_at=NOW - 900)
    add_kanban_event(conn, "claimed", task_id="t_full", payload={"lock": "L1", "expires": NOW + 600, "run_id": run_id}, run_id=run_id, created_at=NOW - 800)
    add_kanban_event(conn, "blocked", task_id="t_full", payload={"kind": "needs_input", "reason": "нужно решение"}, created_at=NOW - 700)
    add_kanban_event(conn, "unblocked", task_id="t_full", created_at=NOW - 650)
    add_kanban_event(conn, "completed", task_id="t_full", payload={"status": "awaiting_merge"}, run_id=run_id, created_at=NOW - 600)
    conn.commit()
    conn.close()

    report = ingest(board, home)

    assert report["errors"] == []
    assert report["appended"] > 0
    kinds = journal_kinds(home)
    # `completed` со статусом awaiting_merge — не завершение дела: fold закрыл бы живое.
    assert set(kinds) == {"created", "claimed", "blocked", "unblocked", "attempt_started"}
    assert "completed" not in kinds


def test_ingest_payload_holds_source_facts_only(home: Path) -> None:
    """payload — сырые факты источника, без вычисленных состояний."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_pay", status="ready", title="дело про факты")
    add_kanban_event(conn, "created", task_id="t_pay", payload={"assignee": "implementer", "status": "ready", "parents": ["t_parent"]}, created_at=NOW - 900)
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_parent', 't_pay')")
    conn.commit()
    conn.close()

    ingest(board, home)

    conn = journal.open_journal(home)
    created = journal.read_events(conn, deal_id="t_pay")[0]
    conn.close()
    assert created["kind"] == "created"
    assert created["payload"] == {
        "title": "дело про факты",
        "assignee": "implementer",
        "status": "ready",
        "parents": ["t_parent"],
    }
    assert created["source"] == "ingest"
    assert created["idempotency_key"].startswith("ingest:stack:t_pay:created:")


def test_second_ingest_is_a_noop_and_fold_is_the_same(home: Path) -> None:
    """Повторный проход: 0 новых строк, и `fold` даёт тот же результат (инвариант 3)."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_twice", status="blocked", block_kind="needs_input")
    add_kanban_event(conn, "created", task_id="t_twice", payload={"status": "blocked"}, created_at=NOW - 900)
    add_kanban_event(conn, "blocked", task_id="t_twice", payload={"kind": "needs_input", "reason": "нужно решение"}, created_at=NOW - 800)
    conn.commit()
    conn.close()

    first = ingest(board, home)
    conn = journal.open_journal(home, create=False)
    assert conn is not None
    before = journal.read_events(conn)
    conn.close()
    second = ingest(board, home)
    conn = journal.open_journal(home, create=False)
    assert conn is not None
    after = journal.read_events(conn)
    conn.close()

    assert first["appended"] == len(before) > 0
    assert second["appended"] == 0
    assert after == before
    assert journal.fold(before) == journal.fold(after)
    assert journal.fold(after)["state"] == "wait_human"


def test_ingest_does_not_touch_foreign_databases(home: Path) -> None:
    """mtime `kanban.db`/`state.db` не меняется за весь сценарий (инвариант 1)."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_ro", status="running", worker_pid=os.getpid())
    run_id = add_run(conn, "t_ro", worker_pid=os.getpid())
    add_kanban_event(conn, "created", task_id="t_ro", payload={"status": "running"}, created_at=NOW - 900)
    add_kanban_event(conn, "claimed", task_id="t_ro", payload={"lock": "L", "expires": NOW + 600, "run_id": run_id}, run_id=run_id, created_at=NOW - 800)
    conn.commit()
    conn.close()
    state_db = home / "profiles" / "implementer" / "state.db"
    state_db.parent.mkdir(parents=True)
    sqlite3.connect(state_db).close()

    before = {path: path.stat().st_mtime_ns for path in (board, state_db)}
    ingest(board, home)
    ingest(board, home)
    after = {path: path.stat().st_mtime_ns for path in (board, state_db)}

    assert after == before


def test_ingest_skips_history_of_untouched_terminal_deals(home: Path) -> None:
    """Историю задним числом не мигрируем: закрытое дело, которого журнал не знает, не пишется."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_old", status="done")
    add_kanban_event(conn, "created", task_id="t_old", payload={"status": "ready"}, created_at=NOW - 9000)
    add_kanban_event(conn, "completed", task_id="t_old", payload={"status": "done"}, created_at=NOW - 8000)
    add_task(conn, "t_live", status="ready")
    conn.commit()
    conn.close()

    ingest(board, home)

    conn = journal.open_journal(home, create=False)
    assert conn is not None
    deals = journal.deal_ids(conn)
    conn.close()
    assert deals == {"t_live"}


def test_ingest_writes_lease_lost_for_claim_without_attempt(home: Path) -> None:
    """Лизинг исчез без попытки: в kanban события для этого нет, факт выводится сравнением."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_lost", status="ready", claim_lock=None, claim_expires=None)
    add_kanban_event(conn, "created", task_id="t_lost", payload={"status": "ready"}, created_at=NOW - 900)
    add_kanban_event(conn, "claimed", task_id="t_lost", payload={"lock": "L-lost", "expires": NOW - 500, "run_id": 1}, run_id=1, created_at=NOW - 800)
    conn.commit()
    conn.close()

    ingest(board, home)

    conn = journal.open_journal(home)
    rows = [event for event in journal.read_events(conn, deal_id="t_lost") if event["kind"] == "lease_lost"]
    conn.close()
    assert len(rows) == 1
    assert rows[0]["payload"] == {"previous_expires": NOW - 500}


def test_ingest_does_not_write_lease_lost_for_closed_deal_with_tail(home: Path) -> None:
    """Хвост после `completed` не делает закрытое дело кандидатом на `lease_lost`.

    `lease_lost` в kanban события не имеет: он выводится сравнением журнала с
    источником (лизинг был, попытки за ним не было, лизинга больше нет). Признак
    «дело закрыто» читался по последней строке журнала (`events[-1]`), и у дела с
    хвостом (`commented`, `question_answered`) он был ложен — в закрытое дело
    дописывался `lease_lost`, а он state-changing, то есть снова воскрешал дело.
    Теперь закрытость считается общим предикатом fold (правило 2).
    """
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_tail", status="running", claim_lock="L-tail", claim_expires=NOW + 600)
    add_kanban_event(conn, "created", task_id="t_tail", payload={"status": "running"}, created_at=NOW - 900)
    add_kanban_event(
        conn,
        "claimed",
        task_id="t_tail",
        payload={"lock": "L-tail", "expires": NOW + 600, "run_id": 1},
        run_id=1,
        created_at=NOW - 800,
    )
    conn.commit()
    conn.close()

    ingest(board, home)  # журнал знает дело, пока оно ещё активно

    conn = sqlite3.connect(board)
    conn.execute("UPDATE tasks SET status='done', claim_lock=NULL, claim_expires=NULL WHERE id='t_tail'")
    add_kanban_event(conn, "completed", task_id="t_tail", payload={"status": "done"}, created_at=NOW - 100)
    conn.commit()
    conn.close()

    ingest(board, home)  # закрытие легло в журнал; хвоста ещё нет
    jconn = journal.open_journal(home)
    journal.append(
        jconn, events(("commented", {"author": "max", "len": 6}), deal_id="t_tail", ts=NOW - 50)
    )
    jconn.close()

    first = ingest(board, home)
    second = ingest(board, home)

    kinds = journal_kinds(home)
    assert "completed" in kinds
    # Хвост — последняя строка журнала, и решение о закрытости от него не зависит.
    assert kinds[-1] == "commented"
    assert "lease_lost" not in kinds
    # Идемпотентность сохранена: дописывать нечего ни на первом проходе с хвостом,
    # ни на повторном.
    assert first["appended"] == 0
    assert second["appended"] == 0


def test_ingest_keeps_live_lease_untouched(home: Path) -> None:
    """Лизинг на месте — он не потерян, `lease_lost` не пишется."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_held", status="ready", claim_lock="L-hold", claim_expires=NOW + 600)
    add_kanban_event(conn, "created", task_id="t_held", payload={"status": "ready"}, created_at=NOW - 900)
    add_kanban_event(conn, "claimed", task_id="t_held", payload={"lock": "L-hold", "expires": NOW + 600, "run_id": 1}, run_id=1, created_at=NOW - 800)
    conn.commit()
    conn.close()

    ingest(board, home)

    assert "lease_lost" not in journal_kinds(home)


def test_ingest_completed_without_status_uses_task_status(home: Path) -> None:
    """Старые kanban-события не писали `status`: терминальность берём у карточки."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_old_done", status="awaiting_merge")
    add_kanban_event(conn, "created", task_id="t_old_done", payload={"status": "ready"}, created_at=NOW - 900)
    add_kanban_event(conn, "completed", task_id="t_old_done", payload={}, created_at=NOW - 800)
    conn.commit()
    conn.close()

    ingest(board, home)

    # дело активно, а `completed` без статуса завершением не считается
    assert "completed" not in journal_kinds(home)


def test_ingest_completed_of_done_card_closes_deal(home: Path, client: TestClient) -> None:
    """B1, ветка «статус из карточки»: дело сдали → в журнале `completed`.

    Дело закрывается ровно в одном месте — ingest через `_completed_status()`.
    До этого теста про `completed` были только отрицательные проверки («событие
    НЕ появилось»), поэтому `_completed_status()`, всегда возвращающий `None`,
    оставлял весь набор зелёным, а журнал перестал бы закрывать дела вовсе.

    Дело сначала живёт на доске (иначе это история, которую ingest намеренно не
    мигрирует), потом карточка уходит в `done`, а kanban-событие `completed`
    приходит без `status` — статус берётся из карточки.
    """
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_done", status="running")
    add_kanban_event(conn, "created", task_id="t_done", payload={"status": "running"}, created_at=NOW - 900)
    conn.commit()
    ingest(board, home)
    assert "completed" not in journal_kinds(home)

    conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't_done'")
    add_kanban_event(conn, "completed", task_id="t_done", payload={}, created_at=NOW - 800)
    conn.commit()
    conn.close()

    client.post("/ingest")

    kinds = journal_kinds(home)
    assert "completed" in kinds
    conn = journal.open_journal(home, create=False)
    assert conn is not None
    rows = journal.read_events(conn, deal_id="t_done")
    conn.close()
    completed = [event for event in rows if event["kind"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["payload"] == {"status": "done"}
    assert journal.fold(rows, now=NOW)["active"] is False
    assert client.get("/now").json()["count"] == 0


def test_ingest_completed_by_payload_status_takes_deal_out_of_now(home: Path, client: TestClient) -> None:
    """B1, ветка «статус из payload»: `completed` события закрывает дело и уводит его из `/now`.

    Карточка при этом ещё активна (`awaiting_merge`), поэтому срез обязан не
    просто выкинуть дело, а назвать расхождение в `boards[].warnings`: «дело
    ушло» и «дело пропало» — разные вещи.
    """
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_stage", status="awaiting_merge")
    add_kanban_event(conn, "created", task_id="t_stage", payload={"status": "ready"}, created_at=NOW - 900)
    add_kanban_event(conn, "completed", task_id="t_stage", payload={"status": "done"}, created_at=NOW - 800)
    conn.commit()
    conn.close()

    before = client.get("/now").json()
    client.post("/ingest")
    after = client.get("/now").json()

    assert [deal["id"] for deal in before["deals"]] == ["t_stage"]
    assert before["boards"][0]["warnings"] == []
    assert after["count"] == 0
    assert any(
        "t_stage" in warning and "правило 2" in warning for warning in after["boards"][0]["warnings"]
    )


def test_ingest_writes_attempt_ended_for_closed_run(home: Path) -> None:
    """B2: у закрытого рана (`ended_at`) появляется `attempt_ended`, и попытка не живёт.

    До этого теста ни один `add_run(...)` не задавал `ended_at`, поэтому потеря
    записи `attempt_ended` в ingest не роняла ничего: дело с закрытым раном
    навсегда осталось бы в `working`/`no_signal` — тихая ложь в `/now`.
    """
    board = make_board(home)
    pid = os.getpid()
    conn = sqlite3.connect(board)
    add_task(conn, "t_ran", status="running", worker_pid=pid)
    run_id = add_run(conn, "t_ran", started_at=NOW - 600, ended_at=NOW - 100, outcome="completed", worker_pid=pid)
    add_kanban_event(conn, "created", task_id="t_ran", payload={"status": "running"}, created_at=NOW - 900)
    conn.commit()
    conn.close()

    ingest(board, home)

    conn = journal.open_journal(home, create=False)
    assert conn is not None
    rows = journal.read_events(conn, deal_id="t_ran")
    conn.close()
    ended = [event for event in rows if event["kind"] == "attempt_ended"]
    assert len(ended) == 1
    assert ended[0]["payload"] == {"run_id": run_id, "outcome": "completed", "error": None}
    assert ended[0]["ts"] == NOW - 100
    # Тот же набор без `attempt_ended` даёт живой факт — на этом различии тест и стоит.
    still_open = journal.fold([event for event in rows if event["kind"] != "attempt_ended"], now=NOW)
    assert still_open["state"] == "working"
    folded = journal.fold(rows, now=NOW)
    assert folded["state"] is None
    assert folded["reason"] == "no_live_fact"
    assert folded["last_kind"] == "attempt_ended"


def test_ingest_reports_unreadable_board(home: Path) -> None:
    """Ошибка источника — факт отчёта, а не падение прохода."""
    broken = board_db(home, "broken")
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"")
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_ok", status="ready")
    conn.commit()
    conn.close()

    report = ingest(board, home)

    assert report["appended"] > 0
    assert any(error["board"] == "broken" for error in report["errors"])
    assert {item["slug"]: item["read"] for item in report["boards"]}["broken"] is False


# -------------------------------------------------------------------- роуты


def test_now_falls_back_to_kanban_and_says_so(home: Path, client: TestClient) -> None:
    """Пустой журнал: состояние из kanban, и evidence это называет (правило 7)."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_blocked", status="blocked", block_kind="needs_input")
    conn.commit()
    conn.close()

    deal = client.get("/now").json()["deals"][0]

    assert deal["waiting"]["state"] == "wait_human"
    assert deal["waiting"]["evidence"].startswith("journal: empty — состояние из kanban")
    assert "status=blocked" in deal["waiting"]["evidence"]
    assert deal["waiting"]["human_kind"] == "status"


def test_now_counts_state_by_journal_after_ingest(home: Path, client: TestClient) -> None:
    """После ingest состояние считается по журналу, и пометки «из kanban» больше нет."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    pid = os.getpid()
    add_task(conn, "t_run", status="running", worker_pid=pid, claim_lock="L", claim_expires=NOW + 600)
    run_id = add_run(conn, "t_run", worker_pid=pid, claim_lock="L")
    add_kanban_event(conn, "created", task_id="t_run", payload={"status": "running"}, created_at=NOW - 900)
    add_kanban_event(conn, "claimed", task_id="t_run", payload={"lock": "L", "expires": NOW + 600, "run_id": run_id}, run_id=run_id, created_at=NOW - 800)
    conn.commit()
    conn.close()

    before = client.get("/now").json()
    client.post("/ingest")
    after = client.get("/now").json()

    assert before["deals"][0]["waiting"]["evidence"].startswith("journal: empty")
    assert after["deals"][0]["waiting"]["state"] == "working"
    assert "journal:" not in after["deals"][0]["waiting"]["evidence"]
    assert "attempt_started" in journal_kinds(home)


def test_health_drift_falls_after_ingest_and_repeat_is_noop(home: Path, client: TestClient) -> None:
    """`drift.empty_journal` — число: было N, после ingest 0, повторный проход count не меняет."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_a", status="ready")
    add_task(conn, "t_b", status="todo")
    add_kanban_event(conn, "created", task_id="t_a", payload={"status": "ready"}, created_at=NOW - 900)
    conn.commit()
    conn.close()

    before = client.get("/health").json()["journal"]
    first = client.post("/ingest").json()
    after = client.get("/health").json()["journal"]
    second = client.post("/ingest").json()
    again = client.get("/health").json()["journal"]

    assert before == {"events": 0, "deals": 0, "drift": {"empty_journal": 2}}
    assert first["appended"] > 0
    assert after["drift"]["empty_journal"] == 0
    assert after["events"] == first["events"] > 0
    assert second["appended"] == 0
    assert again == after


def test_now_with_ingest_flag_writes_only_then(home: Path, client: TestClient) -> None:
    """`?ingest=1` — единственный способ, которым срез пишет; без флага журнал не меняется."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_flag", status="ready")
    add_kanban_event(conn, "created", task_id="t_flag", payload={"status": "ready"}, created_at=NOW - 900)
    conn.commit()
    conn.close()

    client.get("/now")
    assert journal_count(home) == 0
    first = client.get("/now", params={"ingest": 1}).json()
    written = journal_count(home)
    client.get("/now")
    assert journal_count(home) == written > 0
    client.get("/now", params={"ingest": 1})
    assert journal_count(home) == written
    assert first["count"] == 1


def test_now_waiting_shape_is_pinned(home: Path, client: TestClient) -> None:
    """Форма `waiting` — контракт для UI: три ключа, и `human_kind` среди них.

    Пропажа `human_kind` вернула бы 0.0.2, где «ждёт ответа» и «ждёт решения по
    статусу» были одним словом; пропажа любого ключа ломает шейпер экрана.
    """
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_shape", status="blocked", block_kind="needs_input")
    conn.commit()
    conn.close()

    waiting = client.get("/now").json()["deals"][0]["waiting"]

    assert list(waiting) == ["state", "evidence", "human_kind"]
    assert waiting["human_kind"] == "status"


def test_question_in_journal_reports_answer_expected(home: Path, client: TestClient) -> None:
    """П.5 заметки 0.0.2: вопрос в журнале — `wait_human` с `human_kind=question`.

    Пара к предыдущему тесту: два разных «нужен человек» различимы по ответу
    `/now`, а не только внутри fold.
    """
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_ask", status="blocked")
    conn.commit()
    conn.close()
    jconn = journal.open_journal(home)
    journal.append(
        jconn,
        events(
            ("created", {"status": "blocked"}),
            ("question_asked", {"question_id": 7, "session_key": "t_ask", "critical": 1}),
            deal_id="t_ask",
        ),
    )
    jconn.close()

    waiting = client.get("/now").json()["deals"][0]["waiting"]

    assert waiting["state"] == "wait_human"
    assert waiting["human_kind"] == "question"
    assert "#7" in waiting["evidence"]


def test_repeat_pass_counts_no_rows_for_lease_lost(home: Path) -> None:
    """Счётчик доски считает вставленные строки, а не кандидатов `lease_lost`.

    Кандидат на `lease_lost` на повторном проходе тот же (условие вывода от записи
    не меняется), `INSERT OR IGNORE` его отбрасывает. До правки `appended` доски
    прибавлял единицу за кандидата, а итог оставался нулём: сумма по доскам
    расходилась с `appended` ответа — число врало тихо.
    """
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_lost2", status="ready")
    add_kanban_event(conn, "created", task_id="t_lost2", payload={"status": "ready"}, created_at=NOW - 900)
    add_kanban_event(conn, "claimed", task_id="t_lost2", payload={"lock": "L2", "expires": NOW - 500}, run_id=1, created_at=NOW - 800)
    conn.commit()
    conn.close()

    first = ingest(board, home)
    second = ingest(board, home)

    assert "lease_lost" in journal_kinds(home)
    # created + claimed + lease_lost — и ровно столько же насчитала доска
    assert first["appended"] == 3
    assert first["appended"] == sum(item["appended"] for item in first["boards"])
    assert second["appended"] == 0
    assert [item["appended"] for item in second["boards"]] == [0]


def test_now_deal_shape_is_pinned(home: Path, client: TestClient) -> None:
    """Форма `/now` и `/now.deals[]` запинена тестом: правка поля роняет тест.

    Пункт 6 ревью: пин был только на `waiting`, а сама форма дела проверялась
    чтением кода — при следующей правке поле уехало бы молча. `_age_sec` здесь
    намеренно отсутствует: это служебный ключ сортировки, наружу он не идёт.
    """
    board = make_board(home)
    pid = os.getpid()
    conn = sqlite3.connect(board)
    add_task(conn, "t_shape", status="running", worker_pid=pid, claim_lock="L", claim_expires=NOW + 600)
    add_run(conn, "t_shape", worker_pid=pid, claim_lock="L")
    add_kanban_event(conn, "created", task_id="t_shape", payload={"status": "running"}, created_at=NOW - 900)
    conn.commit()
    conn.close()

    body = client.get("/now").json()

    assert list(body) == ["now", "count", "deals", "by_state", "crash_outcomes", "boards", "errors"]
    assert list(body["by_state"]) == list(journal.WAITING_PRIORITY)
    assert body["crash_outcomes"] == list(plugin_api.CRASH_OUTCOMES)
    assert list(body["boards"][0]) == ["slug", "path", "read", "deals", "truncated", "warnings"]
    deal = body["deals"][0]
    assert list(deal) == [
        "board",
        "id",
        "title",
        "assignee",
        "status",
        "priority",
        "consecutive_failures",
        "created_at",
        "workspace_kind",
        "attempt",
        "lease",
        "last_action",
        "last_run",
        "waiting",
    ]
    assert list(deal["attempt"]) == [
        "run_id",
        "source",
        "start",
        "age_sec",
        "pid",
        "pid_alive",
        "heartbeat_age_sec",
        "run_status",
    ]
    assert list(deal["lease"]) == ["claim_lock", "expires_at", "expires_in_sec", "state"]
    assert list(deal["last_action"]) == ["kind", "at", "age_sec", "summary"]
    assert list(deal["last_run"]) == ["run_id", "status", "outcome", "started_at", "ended_at"]
    assert list(deal["waiting"]) == ["state", "evidence", "human_kind"]


def test_health_names_boards_it_could_not_read(home: Path, client: TestClient) -> None:
    """Нечитаемая доска не «выпадает из drift молча»: она названа в `journal.note`.

    Пункт 9 ревью: причина сбоя `_query_checked` выбрасывалась, доска не попадала в
    `active`, и `drift.empty_journal` показывал 0 — «тихое всё хорошо». Теперь у
    ответа появляется `note` с именем доски и причиной, а не только число.
    """
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_ok", status="ready")
    conn.commit()
    conn.close()
    empty = board_db(home, "broken")
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")

    journal_health = client.get("/health").json()["journal"]

    assert journal_health["drift"]["empty_journal"] == 1
    assert "broken" in journal_health["note"]


def test_health_has_no_note_when_every_board_reads(home: Path, client: TestClient) -> None:
    """Читаемые источники — ровно три ключа контракта: `note` не появляется зря."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_ok", status="ready")
    conn.commit()
    conn.close()

    journal_health = client.get("/health").json()["journal"]

    assert list(journal_health) == ["events", "deals", "drift"]
    assert journal_health["drift"]["empty_journal"] == 1


def test_unreadable_journal_names_itself_as_a_path(home: Path, client: TestClient) -> None:
    """Недоступный журнал назван путём `duty/duty.db`, как остальные источники.

    Не слагом доски: «доска duty» читалась бы как ещё одна доска kanban, а дело
    тут — свой файл. Имя берётся из одной константы `journal.JOURNAL_SOURCE`.
    """
    journal_path = home / "duty" / "duty.db"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_bytes(b"not a database")  # журнал есть, но не читается
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_ok", status="ready")
    conn.commit()
    conn.close()

    body = client.get("/now").json()

    sources = [error["board"] for error in body["errors"]]
    assert journal.JOURNAL_SOURCE in sources
    assert "duty" not in sources


def journal_count(home: Path) -> int:
    """Сколько событий в журнале (0, если файла ещё нет)."""
    conn = journal.open_journal(home, create=False)
    if conn is None:
        return 0
    try:
        return journal.counts(conn)["events"]
    finally:
        conn.close()


def test_now_excludes_deal_that_journal_closed(home: Path, client: TestClient) -> None:
    """Правило 2: журнал говорит completed, kanban ещё активна — дело не в срезе, но назван."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_drift", status="awaiting_merge")
    conn.commit()
    conn.close()
    jconn = journal.open_journal(home)
    journal.append(
        jconn,
        events(("created", {"status": "todo"}), ("completed", {"status": "done"}), deal_id="t_drift"),
    )
    jconn.close()

    body = client.get("/now").json()

    assert body["count"] == 0
    assert any("t_drift" in warning and "правило 2" in warning for warning in body["boards"][0]["warnings"])


def test_pool_question_wins_over_journal_state_and_is_named(home: Path, client: TestClient) -> None:
    """Живой вопрос пула — факт: он важнее состояния журнала и назван в evidence."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    pid = os.getpid()
    add_task(conn, "t_0000aa", status="running", worker_pid=pid)
    run_id = add_run(conn, "t_0000aa", worker_pid=pid)
    add_kanban_event(conn, "created", task_id="t_0000aa", payload={"status": "running"}, created_at=NOW - 900)
    conn.commit()
    conn.close()
    jconn = journal.open_journal(home)
    journal.append(
        jconn,
        events(("attempt_started", {"run_id": run_id, "pid": pid}), deal_id="t_0000aa"),
    )
    jconn.close()
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    pool.execute(
        "INSERT INTO questions (session_key, text, origin, status) VALUES ('t_0000aa', 'вопрос по делу', 'card', 'sent')"
    )
    pool.commit()
    pool.close()

    deal = client.get("/now").json()["deals"][0]

    assert deal["waiting"]["state"] == "wait_human"
    assert deal["waiting"]["human_kind"] == "question"
    assert "в журнале вопроса нет" in deal["waiting"]["evidence"]


def test_deal_screen_uses_the_same_journal_state(home: Path, client: TestClient) -> None:
    """Экран дела показывает тот же факт-набор, что `/now`: второй правды нет."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_one", status="blocked", block_kind="needs_input")
    conn.commit()
    conn.close()

    body = client.get("/deal/stack/t_one").json()

    assert body["deal"]["waiting"]["state"] == "wait_human"
    assert body["deal"]["waiting"]["evidence"].startswith("journal: empty — состояние из kanban")
