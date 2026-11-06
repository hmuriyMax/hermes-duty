"""Контракт «Здоровья» (0.0.6): метрики спеки — по журналу дел, карточки — отдельно.

Экран 4 спеки (`~/.hermes/widgets/duty-model.html`) меряет **дела движка**:
«92% **этапов** с первой попытки · 7м медиана **ожидания человека** · 3 **отказа
контракта** · 1.2M **токенов** за сутки». Ни одного из этих чисел нет в карточке
kanban: у карточки нет этапов, нет отказов контракта и нет токенов. Поэтому
здесь проверяется то, что нельзя увидеть глазами при ревью:

- этап с одним удачным `attempt_ended` идёт в числитель, а этап с отказом и
  повтором — только в знаменатель (повтор убирает «с первой»);
- ожидание человека считается по паре `question_asked` → `question_answered`, а
  не по `attempt_*` (это другая величина — ожидание до первой попытки этапа);
- отказы контракта считаются по классу (`payload.kind`, старые записи —
  `details.class`) и разбиваются по правилу причины;
- пустая база отдаёт `null` и `count: 0`, а не нули: «нет данных» и «ноль» —
  разные факты;
- карточек kanban в `/stats` нет ни одним полем: здоровье отвечает про **дела**,
  а карточки живут отдельным наблюдением `GET /cards`.

Красная проба живёт здесь же: `test_health_counts_deal_stages_not_kanban_tasks`
падает, если источником «Здоровья» подменить `tasks` канбана, — у карточки нет
этапов, и доля «с первой попытки» по ней не считается;
`test_health_cards_and_deals_do_not_mix` падает, если в ответ `/stats` вернуть
ключ `cards`: наблюдение за карточками осталось только в `GET /cards`.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_health", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

journal = plugin_api.journal
deals = plugin_api.deals

NOW = 1_800_000_000
DAY = 24 * 60 * 60

POOL_SCHEMA = """
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  text TEXT NOT NULL,
  text_full TEXT,
  origin TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  kind TEXT NOT NULL DEFAULT 'question',
  answer TEXT
);
"""

# Наблюдение за карточками: только те колонки, которые читает `/cards`.
CARDS_SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  title TEXT,
  body TEXT,
  assignee TEXT,
  status TEXT NOT NULL,
  created_at INTEGER,
  block_kind TEXT,
  consecutive_failures INTEGER DEFAULT 0,
  current_run_id INTEGER,
  started_at INTEGER,
  completed_at INTEGER
);
CREATE TABLE task_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  profile TEXT,
  status TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  outcome TEXT
);
"""


# ------------------------------------------------------------------ фикстуры


def make_pool(home: Path) -> None:
    """Пул вопросов: его отсутствие — отдельный факт в `errors[]`, а не предмет теста."""
    path = home / "messaging" / "pool.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()


def make_cards(home: Path, tasks: dict[str, str], runs: dict[str, int]) -> None:
    """Канбан-карточки: `tasks` — статус по id, `runs` — сколько ранов на исход."""
    path = home / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(CARDS_SCHEMA)
    for card_id, status in tasks.items():
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, created_at)"
            " VALUES (?, 'карточка', '', 'implementer', ?, ?)",
            (card_id, status, NOW - 1000),
        )
    for outcome, count in runs.items():
        for _ in range(count):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, outcome)"
                " VALUES ('t_card', 'implementer', 'done', ?, ?)",
                (NOW - 900, outcome),
            )
    conn.commit()
    conn.close()


def store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(home: Path, *, repo: str | None = "sx-widgets-api", created_at: int = NOW - 1000) -> str:
    conn = store(home)
    try:
        created = deals.create_deal(conn, repo=repo, title="дело", body="", now=created_at)
        return str(created["deal"]["id"])
    finally:
        conn.close()


def append(home: Path, deal_id: str, kind: str, payload: dict, *, ts: int) -> None:
    """Дописать факт журнала так, как его пишет движок."""
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
                    "payload": payload,
                    "idempotency_key": f"test:{deal_id}:{kind}:{ts}:{payload.get('run_id') or payload.get('question_id')}",
                }
            ],
        )
    finally:
        conn.close()


def attempt(
    home: Path,
    deal_id: str,
    *,
    stage: str = "impl",
    number: int = 1,
    started_at: int,
    ended_at: int | None,
    outcome: str = "done",
    kind: str | None = None,
    reason: str = "",
    details: dict | None = None,
) -> None:
    """Попытка этапа: `attempt_started` и, если задан конец, `attempt_ended`."""
    run_id = f"{deal_id}:{stage}#{number}"
    append(
        home,
        deal_id,
        "attempt_started",
        {
            "run_id": run_id,
            "stage": stage,
            "attempt": number,
            "pid": None,
            "lease_expires": None,
            "contract": {"role": "implementer"},
        },
        ts=started_at,
    )
    if ended_at is None:
        return
    append(
        home,
        deal_id,
        "attempt_ended",
        {
            "run_id": run_id,
            "stage": stage,
            "attempt": number,
            "outcome": outcome,
            "kind": kind,
            "reason": reason,
            "details": details or {},
        },
        ts=ended_at,
    )


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> int:
    """Один `now` на прогон: окно здоровья считается от него, а не от часов машины."""
    monkeypatch.setattr(plugin_api, "_now_sec", lambda: NOW)
    return NOW


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def totals_of(client: TestClient, **params) -> dict:
    response = client.get("/stats", params=params)
    assert response.status_code == 200
    return response.json()["totals"]


# --------------------------------------- (а) этап с одним `done` — в числителе


def test_health_single_done_stage_counts_as_first_try(client: TestClient, home: Path) -> None:
    deal_id = make_deal(home)
    attempt(home, deal_id, started_at=NOW - 900, ended_at=NOW - 800)

    totals = totals_of(client)

    assert totals["first_try"] == {"ok": 1, "closed": 1, "share": 1.0}
    # Знаменатель назван отдельно: этап один, и он закрыт.
    assert totals["stages"] == {"total": 1, "closed": 1}


# -------------------- (б) этап с отказом и повтором — в знаменателе, не в числителе


def test_health_retried_stage_is_denominator_only(client: TestClient, home: Path) -> None:
    deal_id = make_deal(home)
    attempt(home, deal_id, started_at=NOW - 900, ended_at=NOW - 850, outcome="failed", kind="infra")
    attempt(home, deal_id, number=2, started_at=NOW - 800, ended_at=NOW - 750)

    totals = totals_of(client)

    # Повтор убирает этап из числителя: «с первой попытки» он не пройден.
    assert totals["first_try"] == {"ok": 0, "closed": 1, "share": 0.0}
    assert totals["stages"] == {"total": 1, "closed": 1}
    assert totals["attempts_by_class"] == {"infra": 1}


# ---------- (в) ожидание человека — по `question_asked` → `question_answered`


def test_health_waiting_human_uses_question_pair_not_attempts(client: TestClient, home: Path) -> None:
    deal_id = make_deal(home, created_at=NOW - 1000)
    # Ожидание до первой попытки этапа — 10 с (created → attempt_started).
    attempt(home, deal_id, started_at=NOW - 990, ended_at=NOW - 950)
    # Человек ждал 600 с: вопрос в 900 с до now, ответ — в 300 с.
    append(home, deal_id, "question_asked", {"question_id": 41, "critical": 0}, ts=NOW - 900)
    append(home, deal_id, "question_answered", {"question_id": 41, "answer_len": 3}, ts=NOW - 300)

    totals = totals_of(client)

    assert totals["waiting_human_sec"] == {"p50": 600, "p90": 600, "p99": 600, "count": 1, "max": 600}
    # Другая величина осталась другой: 10 с — это очередь движка, а не человек.
    assert totals["waiting_sec"] == {"p50": 10, "p90": 10, "p99": 10, "count": 1, "max": 10}


# ------------------------------- (г) отказы контракта — по `details.class`


def test_health_contract_refusals_counted_by_class(client: TestClient, home: Path) -> None:
    deal_id = make_deal(home)
    # Движок пишет класс в `kind` события; старые записи — в `details.class`.
    attempt(
        home,
        deal_id,
        started_at=NOW - 900,
        ended_at=NOW - 850,
        outcome="failed",
        kind="contract",
        reason="result.json:artifacts[0].path: artifact.missing: файла нет",
    )
    attempt(
        home,
        deal_id,
        number=2,
        started_at=NOW - 800,
        ended_at=NOW - 750,
        outcome="failed",
        reason="result.json нет",
        details={"class": "contract"},
    )
    # Не контракт — в разбивку не идёт, хотя тоже отказ.
    attempt(home, deal_id, number=3, started_at=NOW - 700, ended_at=NOW - 650, outcome="failed", kind="infra")

    totals = totals_of(client)

    assert totals["contract_refusals"]["count"] == 2
    assert totals["contract_refusals"]["by_rule"] == {"artifact.missing": 1, "result.json": 1}
    assert totals["attempts_by_class"] == {"contract": 2, "infra": 1}


# ------------------------- (д) пустая база — `null` и `count: 0`, а не нули


def test_health_empty_store_gives_null_not_zeros(client: TestClient, home: Path) -> None:
    totals = totals_of(client)

    assert totals["deals"] == 0
    assert totals["first_try"] == {"ok": 0, "closed": 0, "share": None}
    assert totals["waiting_human_sec"] == {"p50": None, "p90": None, "p99": None, "count": 0, "max": None}
    assert totals["waiting_sec"] == {"p50": None, "p90": None, "p99": None, "count": 0, "max": None}
    assert totals["contract_refusals"] == {"count": 0, "by_rule": {}}
    # Суммы токенов нет вовсе: 0 написал бы «истратили ноль», чего никто не утверждал.
    assert totals["tokens"] == {"total": None, "count": 0, "without": 0}


# ------------------------ токены за окно — сумма `details.usage_tokens`


def test_health_tokens_summed_and_null_when_unwritten(client: TestClient, home: Path) -> None:
    deal_id = make_deal(home)
    attempt(home, deal_id, started_at=NOW - 900, ended_at=NOW - 850, details={"usage_tokens": 1200})
    attempt(
        home,
        deal_id,
        number=2,
        started_at=NOW - 800,
        ended_at=NOW - 750,
        details={"usage_tokens": 300},
    )

    assert totals_of(client)["tokens"] == {"total": 1500, "count": 2, "without": 0}

    other = make_deal(home)
    attempt(home, other, stage="verify", started_at=NOW - 600, ended_at=NOW - 500)
    body = client.get("/stats").json()

    assert body["totals"]["tokens"] == {"total": 1500, "count": 2, "without": 1}


def test_health_tokens_null_with_warning_when_none_written(client: TestClient, home: Path) -> None:
    deal_id = make_deal(home)
    attempt(home, deal_id, started_at=NOW - 900, ended_at=NOW - 850)

    body = client.get("/stats").json()

    assert body["totals"]["tokens"] == {"total": None, "count": 0, "without": 1}
    assert any("usage_tokens" in warning for warning in body["warnings"])


# ---------------------------------------- окно: сутки по умолчанию, `--since` явно


def test_health_window_defaults_to_a_day_and_since_narrows_it(client: TestClient, home: Path) -> None:
    deal_id = make_deal(home, created_at=NOW - 3 * DAY)
    attempt(home, deal_id, started_at=NOW - 3 * DAY + 10, ended_at=NOW - 3 * DAY + 60)
    fresh = make_deal(home, created_at=NOW - 100)
    attempt(home, fresh, started_at=NOW - 90, ended_at=NOW - 80)

    body = client.get("/stats").json()
    windowed = client.get("/stats", params={"since": NOW - DAY}).json()

    assert body["window"]["source"] == "default"
    assert body["window"]["sec"] == DAY
    # Старое дело осталось объектом, но его факт в окно не попал.
    assert body["totals"]["deals"] == 2
    assert body["totals"]["deals_active"] == 1
    assert body["totals"]["first_try"]["closed"] == 1
    assert windowed["window"]["source"] == "explicit"
    assert windowed["totals"]["first_try"]["closed"] == 1


# --------------------- (е) карточки живут в `/cards`, а не в здоровье дел


def test_health_cards_and_deals_do_not_mix(client: TestClient, home: Path) -> None:
    """`/stats` — только про дела: ключа `cards` в ответе нет, карточки — в `/cards`.

    Красная проба: вернуть в ответ `/stats` блок `cards` (счёт карточек канбана) —
    тест падает. Здоровье обязано отвечать про **дела**, а наблюдение за
    карточками осталось одним роутом `GET /cards`.
    """
    deal_id = make_deal(home)
    attempt(home, deal_id, started_at=NOW - 900, ended_at=NOW - 850)
    make_cards(home, {"t_a": "done", "t_b": "archived", "t_c": "running"}, {"completed": 4, "crashed": 2})

    body = client.get("/stats").json()
    totals = body["totals"]

    # Дела: одно дело, один этап, одна попытка — и ни одного числа карточек.
    assert "cards" not in body
    assert totals["deals"] == 1
    assert totals["attempts_total"] == 1
    assert "tasks_total" not in totals and "runs_total" not in totals and "by_status" not in totals
    assert totals["deals_by_status"] == {"ready": 1}
    # Карточки никуда не делись: они в наблюдении, своим ответом и своей подписью.
    # Наблюдение показывает живые карточки, поэтому закрытые (`done`/`archived`) в
    # нём не значатся — их видно только счётом в этом тесте.
    observed = client.get("/cards").json()
    assert observed["kind"] == "observation"
    assert observed["label"] == "карточки kanban (наблюдение, не дела)"
    assert observed["count"] == 1
    assert [card["id"] for entry in observed["cards"] for card in entry["cards"]] == ["t_c"]


# ---------------------------------------------------------- красная проба


def test_health_counts_deal_stages_not_kanban_tasks(client: TestClient, home: Path) -> None:
    """Источник «Здоровья» — журнал дел: карточка не этап и доли «с первой» не даёт.

    Красная проба (карточка FIX-X): подменить источник дел на `tasks` канбана —
    тест падает, потому что у карточки нет этапов, и «1 из 1 с первой попытки»
    превращается в «0 из 0» при трёх карточках. Держит именно объект метрики, а
    не форму ответа.
    """
    deal_id = make_deal(home)
    attempt(home, deal_id, started_at=NOW - 900, ended_at=NOW - 850)
    # Карточки есть, и их больше, чем дел: они не должны попасть ни в одно число дел.
    make_cards(home, {"t_a": "done", "t_b": "done", "t_c": "archived"}, {"completed": 3})

    body = client.get("/stats").json()

    assert body["totals"]["deals"] == 1
    assert body["totals"]["first_try"] == {"ok": 1, "closed": 1, "share": 1.0}
    assert body["totals"]["stages"] == {"total": 1, "closed": 1}
    assert body["totals"]["deals_by_status"] == {"ready": 1}
    # Карточка не этап и не дело: в ответе здоровья её нет ни одним полем.
    assert "cards" not in body
