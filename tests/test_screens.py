"""Контракт экранов 0.0.6: `/deal/{deal_id}`, `/stats` и группировка по `repo`.

Дело адресуется своим id `d_<hex>` (0.0.6: доск нет), а группируется по `repo`
строки store. Экраны «Дело» и «Здоровье» считаются по тем же read-only
источникам, что и `/now`, поэтому фикстуры здесь свои, маленькие: `tmp_path` +
`HERMES_HOME`, живые БД не читаются.

Проверяется не рендер, а то, что нельзя проверить глазами при ревью:
распределение ожидания считается методом ближайшего ранга по объединённой
выборке (а не средним перцентилей репозиториев), «с первой попытки» — по этапам
журнала, нечитаемый store не роняет ответ и не попадает в итог, а завершённое
дело не получает придуманного состояния ожидания. Карточка kanban делом не
бывает: её id отвечает отказом, а не пустой карточкой.
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
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_screens", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

journal = plugin_api.journal
deals = plugin_api.deals

NOW = 1_800_000_000

# Пул вопросов: его отсутствие — отдельный факт в `errors[]`, а эти тесты
# проверяют ответ без деградации источников.
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


# ------------------------------------------------------------------ фикстуры


def make_pool(home: Path) -> None:
    path = home / "messaging" / "pool.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()


def store(home: Path) -> sqlite3.Connection:
    """Соединение своего store: схему и журнал доводит открытие на запись."""
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(
    home: Path,
    *,
    repo: str | None = "sx-widgets-api",
    title: str = "дело",
    body: str = "",
    status: str = "ready",
    created_at: int = NOW - 1000,
) -> str:
    """Своё дело в store: строка плюс `created` — как их пишет мутация."""
    conn = store(home)
    try:
        created = deals.create_deal(
            conn, repo=repo, title=title, body=body, now=created_at
        )
        deal_id = str(created["deal"]["id"])
        if status != str(created["deal"]["status"]):
            with conn:
                conn.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))
    finally:
        conn.close()
    return deal_id


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
                    "idempotency_key": f"test:{deal_id}:{kind}:{ts}",
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
) -> None:
    """Попытка этапа: `attempt_started` и, если она закрыта, `attempt_ended`."""
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
            "kind": None,
            "reason": "",
            "base": None,
            "commit": None,
        },
        ts=ended_at,
    )


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


# ------------------------------------------------------- «дело»: одна карточка


def test_deal_returns_active_facts_and_body(home: Path) -> None:
    deal_id = make_deal(home, body="спека дела", status="blocked")
    conn = store(home)
    try:
        deals.set_blocked(
            conn,
            deal_id,
            kind="needs_input",
            reason="нужно решение",
            author="engine",
            source="engine",
            now=NOW - 200,
        )
    finally:
        conn.close()

    body = plugin_api.deal(deal_id)

    assert body["repo"] == "sx-widgets-api"
    assert body["id"] == deal_id
    assert body["terminal"] is False
    assert body["body"] == "спека дела"
    assert body["body_truncated"] is False
    # факты — тот же набор, что в /now: состояние ожидания посчитано бэкендом
    assert body["deal"] is not None
    assert body["deal"]["waiting"]["state"] == "wait_human"
    assert body["deal"]["waiting"]["evidence"]
    assert body["deal"]["last_action"]["kind"] == "blocked"


def test_deal_of_finished_deal_has_no_invented_state(home: Path) -> None:
    deal_id = make_deal(home, title="закрытое дело")
    conn = store(home)
    try:
        deals.close_deal(conn, deal_id, reason="не нужно", now=NOW - 100)
    finally:
        conn.close()

    body = plugin_api.deal(deal_id)

    assert body["terminal"] is True
    assert body["deal"] is None  # состояния ожидания у завершённого дела нет
    assert body["closed"]["cause"] == deals.CLOSE_CAUSE_HUMAN


def test_deal_unknown_id_and_card_id(home: Path) -> None:
    """Дело адресуется своим id: незнакомый — 404, карточка kanban — отказ 400."""
    make_deal(home)

    with pytest.raises(Exception) as missing:
        plugin_api.deal("d_ffffffff")
    assert getattr(missing.value, "status_code", None) == 404

    # `t_<hex>` — карточка, а не дело: доски, которая их связывала, больше нет.
    with pytest.raises(Exception) as card:
        plugin_api.deal("t_aaa001")
    assert getattr(card.value, "status_code", None) == 400


def test_deal_body_is_capped(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin_api, "DEAL_BODY_LIMIT", 10)
    deal_id = make_deal(home, body="0123456789XYZ")

    body = plugin_api.deal(deal_id)

    assert body["body"] == "0123456789"
    assert body["body_truncated"] is True


# ------------------------------------------- «здоровье»: группировка по repo


def test_stats_first_try_and_wait_distribution(client: TestClient, home: Path) -> None:
    # Три дела одного репозитория: у двух этап закрыт с первой попытки, у
    # третьего первая попытка упала. Ожидание до первой попытки 100/300/800 с:
    # ближайший ранг даёт p50 — 2-й из 3 (300), p90 и p99 — 3-й (800).
    first = make_deal(home, created_at=NOW - 1000)
    second = make_deal(home, created_at=NOW - 1000)
    third = make_deal(home, created_at=NOW - 1000)
    attempt(home, first, started_at=NOW - 900, ended_at=NOW - 850)
    attempt(home, second, started_at=NOW - 700, ended_at=NOW - 650)
    attempt(home, third, started_at=NOW - 200, ended_at=NOW - 150, outcome="failed")
    attempt(home, third, number=2, started_at=NOW - 100, ended_at=NOW - 50)

    stats = client.get("/stats").json()
    totals = stats["totals"]

    assert totals["deals"] == 3
    assert totals["attempts_total"] == 4
    assert totals["first_try"] == {"ok": 2, "closed": 3, "share": pytest.approx(2 / 3)}
    assert totals["waiting_sec"] == {"p50": 300, "p90": 800, "p99": 800, "count": 3, "max": 800}
    assert totals["attempts_by_outcome"] == {"done": 3, "failed": 1}
    assert "done" in stats["success_outcomes"]
    assert "crashed" in stats["crash_outcomes"]
    # Группировка названа репозиторием дела, а не доской.
    assert [group["repo"] for group in stats["repos"]] == ["sx-widgets-api"]
    assert stats["without_repo"]["deals"] == 0
    assert stats["errors"] == []


def test_stats_wait_percentiles_nearest_rank(client: TestClient, home: Path) -> None:
    # Ожидания 1..10 с: ближайший ранг даёт p50 = 5 (позиция 5 из 10) и p90 = 9
    # (позиция 9), p99 — позиция 10, то есть максимум; интерполяция дала бы 5.5 и
    # 9.1, а медиана чётного ряда — 5.5. Тест фиксирует именно метод.
    for index in range(1, 11):
        deal_id = make_deal(home, created_at=NOW - 1000 + index)
        attempt(home, deal_id, started_at=NOW - 1000 + index * 2, ended_at=NOW - 1000 + index * 2 + 1)

    totals = client.get("/stats").json()["totals"]

    # ожидание дела i — ровно i секунд (created_at = NOW - 1000 + i, started_at = +2i)
    assert totals["waiting_sec"] == {"p50": 5, "p90": 9, "p99": 10, "count": 10, "max": 10}


def test_stats_wait_single_sample_has_all_percentiles(client: TestClient, home: Path) -> None:
    # Один элемент: любой перцентиль — это он сам, а не None и не 0.
    deal_id = make_deal(home, created_at=NOW - 107)
    attempt(home, deal_id, started_at=NOW - 100, ended_at=NOW - 90)

    totals = client.get("/stats").json()["totals"]

    assert totals["waiting_sec"] == {"p50": 7, "p90": 7, "p99": 7, "count": 1, "max": 7}


def test_stats_wait_is_over_pooled_sample(client: TestClient, home: Path) -> None:
    # Репозиторий a: ожидания 0 и 0. Репозиторий b: 1000 с. Перцентиль по каждой
    # группе отдельно дал бы 0 и 1000; объединённая выборка 0,0,1000 — p50 = 0,
    # p90 = 1000. Итог считается по объединённой выборке, а не средним групп.
    for _ in range(2):
        deal_id = make_deal(home, repo="a", created_at=NOW - 100)
        attempt(home, deal_id, started_at=NOW - 100, ended_at=NOW - 90)
    late = make_deal(home, repo="b", created_at=NOW - 1100)
    attempt(home, late, started_at=NOW - 100, ended_at=NOW - 90)

    stats = client.get("/stats").json()
    totals = stats["totals"]

    assert totals["waiting_sec"] == {"p50": 0, "p90": 1000, "p99": 1000, "count": 3, "max": 1000}
    assert totals["first_try"]["closed"] == 3
    assert [group["repo"] for group in stats["repos"]] == ["a", "b"]


def test_stats_reports_unreadable_store_without_failing(client: TestClient, home: Path) -> None:
    """Битый store не роняет ответ: причина в `errors[]`, итог честно пустой."""
    (home / "duty").mkdir(parents=True, exist_ok=True)
    (home / "duty" / "duty.db").write_bytes(b"not a database")

    stats = client.get("/stats").json()

    assert any(error["source"] == "duty/duty.db" for error in stats["errors"])
    # нечитаемый источник не попадает в итог, но ответ остаётся 200
    assert stats["totals"]["deals"] == 0
    assert stats["repos"] == []


def test_stats_without_attempts_reports_null_share_and_empty_distribution(
    client: TestClient, home: Path
) -> None:
    make_deal(home, status="todo")

    totals = client.get("/stats").json()["totals"]

    assert totals["first_try"] == {"ok": 0, "closed": 0, "share": None}
    # «нет данных» — не «ждали 0 секунд»: перцентили null, count 0
    assert totals["waiting_sec"] == {"p50": None, "p90": None, "p99": None, "count": 0, "max": None}


def test_stats_without_repo_is_its_own_group(client: TestClient, home: Path) -> None:
    """«Репозиторий не назван» — законная группа, а не пропущенная строка."""
    with_repo = make_deal(home, repo="sx-widgets-api")
    without = make_deal(home, repo=None)
    attempt(home, with_repo, started_at=NOW - 900, ended_at=NOW - 850)
    attempt(home, without, started_at=NOW - 900, ended_at=NOW - 850)

    stats = client.get("/stats").json()

    assert [group["repo"] for group in stats["repos"]] == ["sx-widgets-api"]
    assert stats["without_repo"]["repo"] == ""
    assert stats["without_repo"]["deals"] == 1
    # Итог объединяет обе группы: 2 дела, обе попытки.
    assert stats["totals"]["deals"] == 2
    assert stats["totals"]["attempts_total"] == 2


def test_stats_repo_distribution_is_not_averaged_across_repos(
    client: TestClient, home: Path
) -> None:
    # Перцентили группы считаются по её собственной выборке — не подмешивая итог.
    a = make_deal(home, repo="a", created_at=NOW - 100)
    attempt(home, a, started_at=NOW - 40, ended_at=NOW - 30)
    b = make_deal(home, repo="b", created_at=NOW - 100)
    attempt(home, b, started_at=NOW - 100, ended_at=NOW - 90)

    stats = client.get("/stats").json()
    per_repo = {group["repo"]: group["waiting_sec"] for group in stats["repos"]}

    assert per_repo["a"] == {"p50": 60, "p90": 60, "p99": 60, "count": 1, "max": 60}
    assert per_repo["b"] == {"p50": 0, "p90": 0, "p99": 0, "count": 1, "max": 0}
    assert stats["totals"]["waiting_sec"] == {"p50": 0, "p90": 60, "p99": 60, "count": 2, "max": 60}


def test_stats_has_no_board_shaped_leftovers(client: TestClient, home: Path) -> None:
    """Доск нет: ни `boards[]`, ни предупреждений «срез доски обрезан».

    Старая форма отвечала списком досок с `read`/`truncated` и предупреждением
    верхнего уровня про обрезанный срез доски. В 0.0.6 источник один — store
    дел, — поэтому ни ключа, ни предупреждения быть не должно: они вернули бы доску
    чёрным ходом.
    """
    make_deal(home)

    stats = client.get("/stats").json()

    assert "boards" not in stats
    assert stats["warnings"] == []
    assert "truncated" not in stats["totals"]
    # Источник назван: группировка по repo, а не по доске.
    assert set(stats) == {
        "now",
        "window",
        "repos",
        "without_repo",
        "totals",
        "errors",
        "warnings",
        "success_outcomes",
        "crash_outcomes",
    }
