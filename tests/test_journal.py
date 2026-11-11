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
deals = plugin_api.deals

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
                "repo": "stack",
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
    # Колонки `board` в журнале больше нет (0.0.6): дело адресуется своим `deal_id`.
    assert list(columns) == [
        "id",
        "ts",
        "recorded_at",
        "deal_id",
        "kind",
        "payload",
        "source",
        "idempotency_key",
    ]
    assert columns["id"]["pk"] == 1
    assert columns["payload"]["dflt_value"] == "'{}'"
    for name in ("ts", "recorded_at", "deal_id", "kind", "source", "idempotency_key"):
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
    # десятью: ответ на вопрос — закрывающий факт, телеметрия попытки — не факт о деле,
    # публикация — факт о дереве (этап закрыт своим `attempt_ended`), предложение
    # плана — факт о графе, а не о состоянии (в граф оно не входит), продолжение в
      # сохранённом дереве — факт о дереве попытки (о её начале говорит `attempt_started`),
      # жизнь сессии лида — факт о лиде, а не о деле (`docs/ENGINE-2.0.md`, раздел 10).
      # Отмена этапа и факты лида (2.0) — тоже не состояние **дела**: отменённый этап
      # закрыт своим `attempt_ended`, а `lead_*` говорят, где лид, а не что делает работа.
    assert journal.NON_STATE_KINDS == (
        "commented",
        "attached",
        "patched",
        "question_answered",
        "attempt_progress",
        "tree_reused",
        "published",
        "plan_proposed",
        "stage_cancelled",
        "lead_started",
        "lead_resumed",
        "lead_exited",
    )


def test_attempt_progress_keeps_state_and_closed_deal(tmp_path: Path) -> None:
    """Правило 14: телеметрия копится счётчиком и состояния не меняет.

    Прогресс до и после `attempt_ended` не двигает `state`; поздний прогресс на
    закрытом деле его не воскрешает; мусорный payload сверку не роняет.
    """
    folded = journal.fold(
        events(
            ("created", {"title": "дело"}),
            ("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "impl#1", "pid": 99}),
            ("attempt_progress", {"stage": "impl", "attempt": 1, "phase": "tool_use", "name": "read_file"}),
            (
                "attempt_progress",
                {"stage": "impl", "attempt": 1, "phase": "tool_result", "name": "read_file", "duration_ms": 7},
            ),
            ("attempt_ended", {"stage": "impl", "attempt": 1, "run_id": "impl#1", "outcome": "done"}),
            ("attempt_progress", {"stage": "impl", "attempt": 1, "phase": "tool_use", "name": "terminal"}),
        ),
        now=NOW,
        pid_probe=lambda pid: False,
    )

    # Последний state-changing факт — `attempt_ended`, поэтому «попытка идёт» не
    # поднимается; прогресс лишь дописывает счётчик.
    assert folded["active"] is True
    assert folded["state"] is None
    assert folded["reason"] == "no_live_fact"
    assert folded["progress"] == {"tools": 2, "last_tool": "terminal", "last_at": NOW - 100 + 5}

    closed = journal.fold(
        events(
            ("created", {"status": "ready"}),
            ("completed", {"status": "done"}),
            ("attempt_progress", {"phase": "tool_use", "name": "terminal"}),
        ),
        now=NOW,
    )
    assert closed["active"] is False
    assert closed["state"] is None
    assert closed["reason"] == "closed"
    assert closed["progress"]["tools"] == 1

    garbage = journal.fold(
        [
            {"id": 1, "ts": NOW, "kind": "created", "deal_id": "t_1", "payload": {"title": "дело"}},
            {"id": 2, "ts": NOW, "kind": "attempt_progress", "deal_id": "t_1", "payload": "не JSON"},
            {"id": 3, "ts": NOW, "kind": "attempt_progress", "deal_id": "t_1", "payload": {"phase": 42}},
        ],
        now=NOW,
    )
    assert garbage["progress"] is None
    assert garbage["reason"] == "no_live_fact"


def test_attempt_progress_is_the_only_new_kind(home: Path) -> None:
    """Расширение набора — одно слово: `attempt_progress` пишется, `tool_used` — нет."""
    conn = journal.open_journal(home)
    assert journal.append(conn, events(("attempt_progress", {"phase": "tool_use", "name": "read_file"}))) == 1
    with pytest.raises(ValueError) as excinfo:
        journal.append(conn, [dict(events(("attempt_progress", None))[0], kind="tool_used")])
    assert "tool_used" in str(excinfo.value)
    # Порядок применения: прогресс раньше конца попытки при том же `ts`.
    assert journal.KIND_ORDER.index("attempt_progress") < journal.KIND_ORDER.index("attempt_ended")
    conn.close()


def test_plan_facts_are_a_closed_set_and_ordered_before_stage_facts(home: Path) -> None:
    """Четыре события плана: закрытый набор, `plan_proposed` не меняет состояние.

    План дела растёт из журнала (`docs/ENGINE-1.0.md`), поэтому виды плана — часть
    того же закрытого набора, что и остальные: предложение пишется фактом, а
    принятие — вторым, и граф читается по ним, а не по файлу спеки. Предложение
    состояния не меняет (правило 12): этап плана закрыт своим `attempt_ended`.
    """
    conn = journal.open_journal(home)
    batch = events(
        ("created", {"title": "дело"}),
        ("plan_proposed", {"rev": 1, "stages": [{"id": "research"}], "reason": "скоуп", "source_stage": "plan"}),
    )
    assert journal.append(conn, batch) == 2
    folded = journal.fold(journal.read_events(conn), now=NOW)
    assert "plan_proposed" in journal.NON_STATE_KINDS
    assert folded["last_kind"] == "plan_proposed"
    # Последний state-changing факт — `created`: предложение его не перебивает.
    assert folded["reason"] == "no_live_fact"
    conn.close()

    for kind in ("plan_proposed", "plan_accepted", "plan_rejected", "plan_gate"):
        assert kind in journal.EVENT_KINDS
    # Решение по плану — суд над отчётом попытки, поэтому применяется после её
    # конца. Обратный порядок (план раньше `attempt_ended`) стирал отказ плана
    # фактом `done` той же попытки: дело d_1cae95b3 закрылось «все этапы
    # пройдены», не запустив ни одного предложенного этапа.
    for kind in ("plan_proposed", "plan_accepted", "plan_rejected", "plan_gate"):
        assert journal.KIND_ORDER.index(kind) > journal.KIND_ORDER.index("attempt_ended")


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


def test_fold_says_answer_is_received_when_pool_has_it(tmp_path=None) -> None:
    """Шов `answer_of`: ответ в пуле — не «ответа нет», а «ответ получен, решение не проведено».

    Дело d_2fc749fb: человек ответил через пул, а журнала `question_answered` ещё
    нет (его пишет рантайм при перезапуске) — доказательство говорило «ответа нет»
    и врало против системы записи. Журнал остаётся чистым: текст ответа приходит
    швом, а не чтением БД внутри fold.
    """
    folded = journal.fold(
        events(
            ("created", None),
            ("question_asked", {"question_id": 461, "session_key": "t_d_2fc749fb", "critical": 1, "timeout": 300}),
        ),
        now=NOW,
        answer_of=lambda number: {461: "continue"}.get(int(number)),
    )

    assert folded["state"] == "wait_human"
    assert folded["human_kind"] == "question"
    assert "ответ получен: continue" in folded["evidence"]
    assert "ответа нет" not in folded["evidence"]
    assert "решение не проведено" in folded["evidence"]


def test_fold_without_answer_probe_stays_journal_only() -> None:
    """Шов не подставлен — формулировка прежняя: fold не обязан знать про пул."""
    folded = journal.fold(
        events(
            ("created", None),
            ("question_asked", {"question_id": 42, "session_key": "t_0001", "critical": 1, "timeout": 300}),
        ),
        now=NOW,
    )
    assert "ответа нет" in folded["evidence"]


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


def test_expired_lease_term_still_folds_to_dead_lease() -> None:
    """Правило 4: `claimed` со сроком из payload, срок прошёл, попытки не было — `dead_lease`.

    Изменение правила касается только `claimed` без срока, а не проверки срока:
    факт лизинга с истёкшим сроком назван независимо от того, кто его записал.
    """
    folded = journal.fold(
        events(("claimed", {"claim_lock": "L", "claim_expires": NOW - 100, "source": "engine"})),
        now=NOW,
    )

    assert folded["state"] == "dead_lease"
    assert "L" in folded["evidence"]


# ------------------------------------------------- своё дело в store (0.0.6)
#
# Доск больше нет: `/now` показывает только дела своего store (`dashboard/deals.py`),
# а состояние берётся fold'ом журнала. Карточки kanban — наблюдение (`/cards`), в
# срез дел они не входят. Поэтому фикстура «дело в срезе» — строка store плюс его
# события, а не карточка доски.


def store(home: Path) -> sqlite3.Connection:
    """Соединение своего store: схему (и журнал рядом) доводит открытие на запись."""
    conn = deals.open_store(home)
    assert conn is not None
    conn.row_factory = sqlite3.Row
    return conn


def make_deal(
    home: Path, *, title: str = "дело", status: str = "ready", repo: str | None = "stack"
) -> str:
    """Своё дело в store: строка плюс `created` — как их пишет мутация."""
    conn = store(home)
    try:
        result = deals.create_deal(conn, repo=repo, title=title, now=NOW - 900)
        deal_id = str(result["deal"]["id"])
        if status != str(result["deal"]["status"]):
            # Статус жизни ставится командой, а не созданием: в тесте — прямо в строку.
            with conn:
                conn.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))
    finally:
        conn.close()
    return deal_id


def append_facts(
    home: Path, deal_id: str, *pairs: tuple[str, dict | None], ts: int = NOW - 100
) -> None:
    """Дописать факты журнала дела так, как их пишет движок."""
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        journal.append(conn, events(*pairs, deal_id=deal_id, ts=ts))
    finally:
        conn.close()


def journal_kinds(home: Path) -> list[str]:
    conn = journal.open_journal(home, create=False)
    assert conn is not None
    try:
        return [event["kind"] for event in journal.read_events(conn)]
    finally:
        conn.close()


# -------------------------------------------------------------------- роуты


def test_now_reads_deals_from_store_not_from_kanban(home: Path, client: TestClient) -> None:
    """`/now` показывает дела store, а карточка kanban делом не считается (0.0.6)."""
    board = make_board(home)
    conn = sqlite3.connect(board)
    add_task(conn, "t_blocked", status="blocked", block_kind="needs_input")
    conn.commit()
    conn.close()

    assert client.get("/now").json()["deals"] == []

    deal_id = make_deal(home)

    listed = client.get("/now").json()["deals"]
    assert [deal["id"] for deal in listed] == [deal_id]
    assert listed[0]["repo"] == "stack"


def test_now_counts_state_by_journal(home: Path, client: TestClient) -> None:
    """Состояние дела считается по журналу: `attempt_started` даёт `working`."""
    deal_id = make_deal(home, status="running")
    append_facts(
        home,
        deal_id,
        ("attempt_started", {"run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1, "pid": os.getpid(), "lease_expires": NOW + 600}),
    )

    assert journal_kinds(home) == ["created", "attempt_started"]
    deal = client.get("/now").json()["deals"][0]

    assert deal["waiting"]["state"] == "working"
    assert "journal:" not in deal["waiting"]["evidence"]
    assert deal["attempt"]["pid"] == os.getpid()
    assert deal["attempt"]["source"] == "journal"


def test_health_counts_journal_only_and_is_idempotent(home: Path, client: TestClient) -> None:
    """`/health.journal` — числа журнала: два чтения подряд дают одно и то же."""
    deal_id = make_deal(home)

    first = client.get("/health").json()["journal"]
    second = client.get("/health").json()["journal"]

    assert first == {"events": 1, "deals": 1}
    assert second == first
    assert deal_id in journal.deal_ids(journal.open_journal(home, create=False))


def test_now_does_not_write_the_journal(home: Path, client: TestClient) -> None:
    """Срез только читает: ни одного события и ни байта новых файлов."""
    deal_id = make_deal(home)
    written = journal_count(home)

    client.get("/now")
    client.get(f"/deal/{deal_id}")
    client.get(f"/duty/{deal_id}/attempts")

    assert journal_count(home) == written


def test_now_waiting_shape_is_pinned(home: Path, client: TestClient) -> None:
    """Форма `waiting` — контракт для UI: три ключа, и `human_kind` среди них.

    Пропажа `human_kind` вернула бы 0.0.2, где «ждёт ответа» и «ждёт решения по
    статусу» были одним словом; пропажа любого ключа ломает шейпер экрана.
    """
    make_deal(home, status="blocked")

    waiting = client.get("/now").json()["deals"][0]["waiting"]

    assert list(waiting) == ["state", "evidence", "human_kind"]
    assert waiting["human_kind"] == "status"


def test_question_in_journal_reports_answer_expected(home: Path, client: TestClient) -> None:
    """П.5 заметки 0.0.2: вопрос в журнале — `wait_human` с `human_kind=question`.

    Пара к предыдущему тесту: два разных «нужен человек» различимы по ответу
    `/now`, а не только внутри fold.
    """
    deal_id = make_deal(home, status="blocked")
    append_facts(
        home,
        deal_id,
        ("question_asked", {"question_id": 7, "session_key": f"t_{deal_id}", "critical": 1}),
    )

    waiting = client.get("/now").json()["deals"][0]["waiting"]

    assert waiting["state"] == "wait_human"
    assert waiting["human_kind"] == "question"
    assert "#7" in waiting["evidence"]


def test_now_deal_shape_is_pinned(home: Path, client: TestClient) -> None:
    """Форма `/now` и `/now.deals[]` запинена тестом: правка поля роняет тест.

    Пункт 6 ревью: пин был только на `waiting`, а сама форма дела проверялась
    чтением кода — при следующей правке поле уехало бы молча. `_age_sec` здесь
    намеренно отсутствует: это служебный ключ сортировки, наружу он не идёт.
    Доски (`boards[]`) в ответе больше нет (0.0.6): группировка — `repos`.
    """
    deal_id = make_deal(home, status="running")
    append_facts(
        home,
        deal_id,
        ("attempt_started", {"run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1, "pid": os.getpid(), "lease_expires": NOW + 600}),
    )

    body = client.get("/now").json()

    assert list(body) == ["now", "count", "deals", "repos", "by_state", "crash_outcomes", "errors"]
    assert list(body["by_state"]) == list(journal.WAITING_PRIORITY)
    assert body["crash_outcomes"] == list(plugin_api.CRASH_OUTCOMES)
    assert list(body["repos"]) == ["list", "without_repo", "read"]
    assert body["repos"]["list"] == [{"repo": "stack", "deals": 1}]
    deal = body["deals"][0]
    assert list(deal) == [
        "repo",
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
        # W6: этап графа и номер попытки из журнала — «что сейчас» без открытия
        # дела. Вложенный объект, потому что имя `attempt` в этой форме занято
        # фактом попытки.
        "stage",
        # 0.0.5: расход против верхнего `budget` спеки (`dashboard/budget.py`);
        # `null` — потолка на дело нет.
        "budget",
        # 0.0.5: публикация по журналу (`dashboard/publish.py`); `null` — не
        # публиковали.
        "published",
        # 2.0: состояние лида по журналу (`dashboard/leadfeed.py`): жив,
        # отсутствует или поднят резюмом N из M.
        "lead",
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
    assert deal["last_run"] is None
    assert list(deal["waiting"]) == ["state", "evidence", "human_kind"]


def test_health_without_journal_does_not_create_it(home: Path, client: TestClient) -> None:
    """`/health` файлов не создаёт: пустой журнал — это 0 событий, а не новый файл."""
    journal_health = client.get("/health").json()["journal"]

    assert journal_health == {"events": 0, "deals": 0}
    assert not (home / "duty" / "duty.db").exists()


def test_health_note_names_unreadable_journal(home: Path, client: TestClient) -> None:
    """Нечитаемый журнал назван в `note` с причиной, а не выдан за пустой."""
    path = home / "duty" / "duty.db"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database")

    health = client.get("/health").json()

    assert health["journal"] == {"events": 0, "deals": 0}
    assert "journal unreadable" in health["note"]


def test_unreadable_journal_names_itself_as_a_path(home: Path, client: TestClient) -> None:
    """Недоступный журнал назван в `errors[]` именем источника, а не падением роута.

    Не слагом доски: «доска duty» читалась бы как ещё одна доска kanban, а дело
    тут — свой файл. Имя берётся из одной константы `journal.JOURNAL_SOURCE`.
    """
    journal_path = home / "duty" / "duty.db"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_bytes(b"not a database")

    body = client.get("/now").json()

    sources = [error["source"] for error in body["errors"]]
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
    """Правило 2: журнал говорит `completed` — дело не в срезе, хотя строка активна."""
    deal_id = make_deal(home, status="awaiting_merge")
    append_facts(home, deal_id, ("completed", {"status": "done"}))

    body = client.get("/now").json()

    assert body["count"] == 0
    assert deal_id not in {deal["id"] for deal in body["deals"]}


def test_pool_question_wins_over_journal_state_and_is_named(home: Path, client: TestClient) -> None:
    """Живой вопрос пула — факт: он важнее состояния журнала и назван в evidence."""
    deal_id = make_deal(home, status="running")
    append_facts(
        home,
        deal_id,
        ("attempt_started", {"run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1, "pid": os.getpid()}),
    )
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    pool.execute(
        "INSERT INTO questions (session_key, text, origin, status) VALUES (?, 'вопрос по делу', 'duty', 'sent')",
        (f"t_{deal_id}",),
    )
    pool.commit()
    pool.close()

    deal = client.get("/now").json()["deals"][0]

    assert deal["waiting"]["state"] == "wait_human"
    assert deal["waiting"]["human_kind"] == "question"
    assert "в журнале вопроса нет" in deal["waiting"]["evidence"]


def test_deal_screen_uses_the_same_journal_state(home: Path, client: TestClient) -> None:
    """Экран дела показывает тот же факт-набор, что `/now`: второй правды нет."""
    deal_id = make_deal(home, status="blocked")

    body = client.get(f"/deal/{deal_id}").json()

    assert body["deal"]["waiting"] == client.get("/now").json()["deals"][0]["waiting"]
    assert body["deal"]["waiting"]["state"] == "wait_human"


def test_kanban_card_is_not_a_deal_for_the_routes(home: Path, client: TestClient) -> None:
    """`t_…` в маршрутах дел — отказ словами: карточки смотри в `/cards`."""
    make_board(home)

    for path in ("/deal/t_0000aa", "/duty/t_0000aa/attempts"):
        response = client.get(path)
        assert response.status_code == 400, (path, response.text)
        assert "карточка kanban" in response.json()["detail"]


# ------------------------------------------------------- парковка гейта
#
# Гейт (`kind: gate`) паркуется, когда человек не ответил за срок: класса отказа
# и счётчика попыток у него нет, поэтому «парковка 0/0 unknown» было бы выдумкой.


def test_fold_gate_park_is_wait_human_without_counter() -> None:
    """Парковка гейта — `wait_human` с причиной гейта и без счётчика попыток."""
    folded = journal.fold(
        events(
            (
                "blocked",
                {
                    "block_kind": "parked",
                    "reason": "гейт: человек не ответил за 5 мин",
                    "gate": True,
                    "stage": "gate",
                    "question_id": 284,
                },
            ),
        ),
        now=NOW,
    )

    assert folded["state"] == "wait_human"
    assert folded["human_kind"] == "status"
    evidence = folded["evidence"]
    assert "гейт" in evidence and "человек не ответил за 5 мин" in evidence
    assert "вопрос #284" in evidence
    # Ни класса, ни счётчика: у гейта не было ни попыток, ни ошибки процесса.
    assert "0/0" not in evidence
    assert "unknown" not in evidence
    assert "парковка" not in evidence


def test_fold_gate_park_with_answer_asks_for_the_decision() -> None:
    """Ответ на вопрос парковки получен — ждём решения, а не ответа."""
    folded = journal.fold(
        events(
            (
                "blocked",
                {
                    "block_kind": "parked",
                    "reason": "гейт: человек не ответил за 5 мин",
                    "gate": True,
                    "stage": "gate",
                    "question_id": 284,
                },
            ),
            ("question_answered", {"question_id": 284, "answer_len": 2, "answered_via": "pool"}),
        ),
        now=NOW,
    )

    assert folded["state"] == "wait_human"
    assert "ждёт решения человека" not in folded["evidence"]
