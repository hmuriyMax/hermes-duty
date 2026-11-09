"""Контракт экрана «Завершённые» (0.0.7): список закрытых дел и его числа.

До 0.0.7 завершённую работу не было видно нигде: `/now` отвечает только про
активные дела, списка нет, и открыть закрытое дело можно было, лишь зная его
`d_<hex>`. Здесь проверяется то, что нельзя проверить глазами при ревью:

- в список попадают только терминальные дела (`done`/`archived`), свежие сверху;
- строка называет **записанный** факт закрытия (`finished_at`, `cause`,
  `stages`), а не пересказ статуса: у дела, закрытого архивацией, причины нет
  вовсе, и пустая строка честнее придуманной;
- числа строки (`attempts`, `questions`, `duration_sec`) — те же, что у
  `GET /deal/{deal_id}` и `GET /duty/{deal_id}/attempts`: карточка дела и строка
  списка об одном деле не спорят;
- пустой список, пустой фильтр и нечитаемый store — разные факты (`note`);
- `?status=running` — отказ: активные дела показывает `/now`.

Красная проба — `test_finished_row_numbers_match_the_deal_card`: подменить в
строке любое из чисел на «своё» (длительность по `created_at → finished_at`
вместо оси журнала, попытки по числу событий вместо `stagefeed.attempts`,
вопросы разностью вместо пар) — тест падает.
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
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_finished", MODULE_PATH)
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
    created_at: int = NOW - 1000,
) -> str:
    """Своё дело в store: строка плюс `created` — как их пишет мутация."""
    conn = store(home)
    try:
        created = deals.create_deal(conn, repo=repo, title=title, body="", now=created_at)
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
                    "idempotency_key": f"finished:{deal_id}:{kind}:{ts}",
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


def close_stages_done(home: Path, deal_id: str, *, stages: list[str], ts: int) -> None:
    """Закрытие движком: `completed` с причиной и пройденным графом."""
    conn = store(home)
    try:
        deals.close_stages_done(conn, deal_id, stages=stages, now=ts)
    finally:
        conn.close()


def archive(home: Path, deal_id: str, *, ts: int) -> None:
    """Архивация: тот же `completed`, но `status=archived` и без причины."""
    conn = store(home)
    try:
        deals.patch_deal(conn, deal_id, status="archived", source="cli", now=ts)
    finally:
        conn.close()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(plugin_api, "_now_sec", lambda: NOW)
    make_pool(root)
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def rows_of(client: TestClient, params: dict | None = None) -> list[dict]:
    response = client.get("/deals", params=params or {})
    assert response.status_code == 200, response.text
    return response.json()["deals"]


# ---------------------------------------- состав списка: только закрытые дела


def test_finished_lists_closed_deals_newest_first(client: TestClient, home: Path) -> None:
    older = make_deal(home, title="старое", created_at=NOW - 3000)
    attempt(home, older, started_at=NOW - 2900, ended_at=NOW - 2800)
    close_stages_done(home, older, stages=["impl"], ts=NOW - 2000)

    newer = make_deal(home, title="свежее", created_at=NOW - 1500)
    attempt(home, newer, started_at=NOW - 1400, ended_at=NOW - 1300)
    close_stages_done(home, newer, stages=["impl"], ts=NOW - 900)

    archived = make_deal(home, title="архив", repo="other-repo", created_at=NOW - 1200)
    archive(home, archived, ts=NOW - 600)

    # Активное дело в список не попадает: `/now` — про «сейчас», этот экран — про
    # сделанное, и вторая правда об активном здесь не заводится.
    make_deal(home, title="живое", created_at=NOW - 500)

    body = client.get("/deals").json()

    assert body["statuses"] == ["done", "archived"]
    assert body["count"] == 3
    assert body["total"] == 3
    assert body["truncated"] is False
    assert body["note"] == ""
    assert body["errors"] == []
    # Свежие сверху — по времени закрытия, а не по созданию.
    assert [row["id"] for row in body["deals"]] == [archived, newer, older]
    assert [row["status"] for row in body["deals"]] == ["archived", "done", "done"]
    assert body["deals"][0]["repo"] == "other-repo"


def test_finished_archived_deal_has_no_invented_cause(client: TestClient, home: Path) -> None:
    """У архивного дела причины нет: пустая строка честнее придуманной."""
    deal_id = make_deal(home)
    archive(home, deal_id, ts=NOW - 100)

    row = rows_of(client)[0]

    assert row["status"] == "archived"
    assert row["finished_at"] == NOW - 100
    assert row["cause"] == ""
    assert row["stages"] == []
    # Архивация пишется тем же `completed`: время закрытия — факт журнала.
    assert row["attempts"] == 0
    assert row["questions"] == {"asked": 0, "answered": 0, "waiting": 0}


def test_finished_deal_without_close_fact_does_not_pretend(client: TestClient, home: Path) -> None:
    """Строка store `done` без `completed` в журнале: время и причина — не записаны."""
    deal_id = make_deal(home, created_at=NOW - 400)
    conn = store(home)
    try:
        with conn:
            conn.execute("UPDATE deals SET status = 'done' WHERE id = ?", (deal_id,))
    finally:
        conn.close()

    row = rows_of(client)[0]

    assert row["status"] == "done"
    assert row["finished_at"] is None
    assert row["cause"] == ""
    assert row["stages"] == []


# ------------------------------------------------------ числа строки и карточки


def test_finished_row_numbers_match_the_deal_card(client: TestClient, home: Path) -> None:
    """Числа строки — те же, что у `/deal` и `/duty/{id}/attempts`.

    Дело: создано в `NOW-1000`, две попытки (`NOW-900…-800` и `NOW-700…-200`), два
    вопроса человеку (на первый ответили), публикация в `NOW-500`, закрытие
    движком в `NOW-300`. Вторая попытка закрылась **после** закрытия дела — так
    бывает, когда человек закрывает дело, а попытка ещё доживает; поэтому ось
    журнала (`NOW-1000 … NOW-200` = 800 с) и `created_at → finished_at` (700 с) —
    разные числа. Строка обязана называть первое: ровно его рисует шкала времени
    карточки дела (`tests/model.test.mjs`, «факты Завершённых» — тот же набор).
    """
    deal_id = make_deal(home, created_at=NOW - 1000)
    attempt(home, deal_id, started_at=NOW - 900, ended_at=NOW - 800)
    attempt(home, deal_id, stage="verify", started_at=NOW - 700, ended_at=NOW - 200)
    append(home, deal_id, "question_asked", {"question_id": 7}, ts=NOW - 650)
    append(home, deal_id, "question_answered", {"question_id": 7, "answer_len": 2}, ts=NOW - 640)
    append(home, deal_id, "question_asked", {"question_id": 8}, ts=NOW - 630)
    append(
        home,
        deal_id,
        "published",
        {"stage": "publish", "attempt": 1, "sha": "b" * 40, "branch": "release/v0.0.7"},
        ts=NOW - 500,
    )
    close_stages_done(home, deal_id, stages=["impl", "verify"], ts=NOW - 300)

    row = rows_of(client)[0]
    detail = plugin_api.deal(deal_id)
    timeline = plugin_api.duty_attempts(deal_id)

    # Попытки — число строк таймлайна, а не число событий.
    assert row["attempts"] == timeline["count"] == 2
    # Закрытие и его причина — поля карточки дела, а не пересказ статуса.
    assert row["finished_at"] == detail["closed"]["at"] == NOW - 300
    assert row["cause"] == detail["closed"]["cause"] == deals.CLOSE_CAUSE_ALL_STAGES
    assert row["stages"] == detail["closed"]["stages"] == ["impl", "verify"]
    # Публикация — тот же факт журнала.
    assert row["published"] == detail["published"]
    assert row["published"]["sha"] == "b" * 40
    # Вопросы — пары `question_asked` → `question_answered` из таймлайна карточки.
    kinds = [event["kind"] for event in detail["timeline"]]
    assert row["questions"] == {
        "asked": kinds.count("question_asked"),
        "answered": kinds.count("question_answered"),
        "waiting": 1,
    }
    # Длительность — ось журнала (первое событие … последний факт, включая конец
    # попытки), ровно как у шкалы карточки дела. `created_at → finished_at` дало бы
    # 700 и разошлось бы с карточкой того же дела.
    assert row["duration_sec"] == 800
    assert row["duration_sec"] != row["finished_at"] - row["created_at"]


# --------------------------------------------------- фильтры: repo, limit, status


def test_finished_repo_filter_and_limit(client: TestClient, home: Path) -> None:
    first = make_deal(home, repo="repo-a", title="a1", created_at=NOW - 1000)
    second = make_deal(home, repo="repo-a", title="a2", created_at=NOW - 900)
    third = make_deal(home, repo="repo-b", title="b1", created_at=NOW - 800)
    orphan = make_deal(home, repo=None, title="без репозитория", created_at=NOW - 700)
    for index, deal_id in enumerate((first, second, third, orphan), start=1):
        close_stages_done(home, deal_id, stages=["impl"], ts=NOW - 100 + index)

    only_a = client.get("/deals", params={"repo": "repo-a"}).json()
    only_orphan = client.get("/deals", params={"repo": ""}).json()
    unknown = client.get("/deals", params={"repo": "nope"}).json()
    limited = client.get("/deals", params={"limit": 2}).json()

    assert [row["title"] for row in only_a["deals"]] == ["a2", "a1"]
    assert only_a["total"] == 2
    # `repo=""` — ровно дела без репозитория, как у `/now` и `/stats`.
    assert [row["id"] for row in only_orphan["deals"]] == [orphan]
    # Неизвестный репозиторий — фильтр, а не ресурс: 200 и пусто, с причиной.
    assert unknown["deals"] == []
    assert "nope" in unknown["note"] and "завершённых дел нет" in unknown["note"]
    # Лимит режет список, но `total` называет, сколько дел подошло целиком.
    assert limited["limit"] == 2
    assert limited["count"] == 2
    assert limited["total"] == 4
    assert limited["truncated"] is True
    assert [row["title"] for row in limited["deals"]] == ["без репозитория", "b1"]


def test_finished_cursor_pagination_walks_backwards_without_repeats(
    client: TestClient, home: Path
) -> None:
    """Курсор — пара «факт закрытия + id»: страницы не повторяют строк и не теряют их."""
    ids = []
    for index in range(5):
        deal_id = make_deal(home, repo="repo-a", title=f"c{index}", created_at=NOW - 1000)
        # Два дела закрыты одно и то же мгновение: ключ обязан различать их по id,
        # иначе страница повторит строку или перескочит через неё.
        close_stages_done(home, deal_id, stages=["impl"], ts=NOW - 500 + (index // 2))
        ids.append(deal_id)

    first = client.get("/deals", params={"limit": 2}).json()
    assert first["cursor"] == ""
    assert first["next_cursor"]
    assert first["truncated"] is True

    second = client.get("/deals", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    third = client.get("/deals", params={"limit": 2, "cursor": second["next_cursor"]}).json()

    walked = [row["id"] for page in (first, second, third) for row in page["deals"]]
    # Все дела ровно по одному разу, свежие сверху — тот же порядок, что у полного списка.
    assert sorted(walked) == sorted(ids)
    assert len(walked) == len(set(walked)) == 5
    assert walked == [row["id"] for row in client.get("/deals", params={"limit": 50}).json()["deals"]]
    # Последняя страница сама говорит, что она последняя.
    assert third["next_cursor"] is None
    assert third["truncated"] is False
    # `total` — сколько дел подошло под фильтр целиком, а не размер страницы.
    assert [page["total"] for page in (first, second, third)] == [5, 5, 5]

    # Кривой курсор — отказ с форматом, а не пустая страница.
    broken = client.get("/deals", params={"cursor": "nonsense"})
    assert broken.status_code == 400
    assert "finished_at" in broken.json()["detail"]


def test_finished_status_filter_narrows_and_refuses_open_status(
    client: TestClient, home: Path
) -> None:
    done = make_deal(home, title="закрытое", created_at=NOW - 1000)
    archived = make_deal(home, title="архив", created_at=NOW - 900)
    close_stages_done(home, done, stages=["impl"], ts=NOW - 500)
    archive(home, archived, ts=NOW - 400)

    only_done = client.get("/deals", params={"status": "done"}).json()
    only_archived = client.get("/deals", params={"status": "archived"}).json()

    assert only_done["statuses"] == ["done"]
    assert [row["id"] for row in only_done["deals"]] == [done]
    assert [row["id"] for row in only_archived["deals"]] == [archived]

    # Активные дела показывает `/now`: `?status=running` — отказ словами, а не
    # пустой список («таких дел нет» было бы враньём).
    running = client.get("/deals", params={"status": "running"})
    assert running.status_code == 400
    assert "/now" in running.json()["detail"]
    empty = client.get("/deals", params={"status": " "})
    assert empty.status_code == 400

    # `?board=` — тот же отказ, что у `/now` и `/stats`: доск нет.
    board = client.get("/deals", params={"board": "stack"})
    assert board.status_code == 400
    assert "repo" in board.json()["detail"]

    # Лимит вне диапазона — отказ, а не тихое урезание до умолчания.
    assert client.get("/deals", params={"limit": 0}).status_code == 400
    assert client.get("/deals", params={"limit": 10_000}).status_code == 400


# ------------------------------------------------------------ пустота и деградация


def test_finished_empty_store_says_no_deals_in_words(client: TestClient) -> None:
    body = client.get("/deals").json()

    assert body["count"] == 0
    assert body["total"] == 0
    assert body["deals"] == []
    assert body["errors"] == []
    # Пусто — словами: экран печатает эту строку, а не пустой квадрат.
    assert body["note"] == "завершённых дел нет: в store ни одного дела в статусе done, archived"


def test_finished_unreadable_store_is_an_error_not_an_empty_list(home: Path) -> None:
    """Битый store: не «дел нет», а причина в `errors[]` и в `note`."""
    duty_dir = home / "duty"
    duty_dir.mkdir(parents=True)
    (duty_dir / "duty.db").write_bytes(b"not a sqlite database")

    body = plugin_api.collect_deals(home, NOW)

    assert body["deals"] == []
    assert body["errors"], "нечитаемый store обязан назвать себя"
    assert body["note"].startswith("завершённые дела не прочитаны")


def test_finished_does_not_create_the_store(tmp_path: Path) -> None:
    """Чтение списка файла не создаёт: `create=False`, `?mode=ro`."""
    root = tmp_path / "empty-home"
    root.mkdir()

    body = plugin_api.collect_deals(root, NOW)

    assert body["deals"] == []
    assert not (root / "duty" / "duty.db").exists()
