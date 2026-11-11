"""Решения лида видны (2.0): лента, состояние, `now`, `why` и `stats` по журналу.

Тест держит карточку T8 (`docs/ENGINE-2.0.md`, раздел 9: «фолд как проекция»), а
не внутренности рендера. Три вещи проверяются по отдельности, как они и устроены:

- **проекция** (`dashboard/leadfeed.py`): лента решений и состояние лида — чистая
  функция от фактов журнала. Живой сессии в тесте нет ни одной: если бы экран
  спрашивал процесс, эти тесты просто не смогли бы собрать ленту;
- **поверхности** (`dashboard/cli.py` + `dashboard/plugin_api.py`): колонка
  состояния в `now`, лента рядом с попытками на экране дела, фраза `why` «стоит на
  этапе X с 14:31, пробуждений нет, лид жив» и строки `stats` — по полям ответов
  роутов, без второго счёта по тем же событиям;
- **отмена отдельно от завершения** (раздел 5): отменённое дело не считается
  сделанным, и `stats` называет два числа, а не одно.

Факты кладутся в настоящий журнал через `journal.append`: виды `lead_*` и
`stage_cancelled` — часть закрытого набора (`journal.EVENT_KINDS`), и тест читает
их той же формой, которую увидит движок после рестарта.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str) -> Any:
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Те же имена, что у плагина: одна копия модуля на процесс.
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
leadfeed = load_module("hermes_duty_leadfeed", "leadfeed.py")
plugin_api = load_module("hermes_duty_plugin_api", "plugin_api.py")
cli = load_module("hermes_duty_cli", "cli.py")

NOW = 1_800_000_000
REPO = "duty"


# ------------------------------------------------------------------ фикстуры


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes: store заводится на первом обращении, пул не нужен."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    conn = deals.open_store(root)
    conn.close()
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def make_deal(home: Path, *, title: str = "дело лида", repo: str | None = REPO) -> dict[str, Any]:
    conn = deals.open_store(home)
    assert conn is not None
    try:
        created = deals.create_deal(conn, repo=repo, title=title, body="", now=NOW - 1000)
        return deals.deal_row(conn, str(created["deal"]["id"]))
    finally:
        conn.close()


def append(home: Path, deal_id: str, facts: list[tuple[str, dict[str, Any], int]]) -> None:
    """Факты в журнал: `(kind, payload, ts)` — по порядку, как их пишет движок."""
    conn = deals.open_store(home)
    assert conn is not None
    try:
        journal.append(
            conn,
            [
                {
                    "ts": ts,
                    "recorded_at": ts,
                    "deal_id": deal_id,
                    "kind": kind,
                    "payload": payload,
                    "source": "test",
                    "idempotency_key": f"{deal_id}:{kind}:{index}",
                }
                for index, (kind, payload, ts) in enumerate(facts)
            ],
        )
    finally:
        conn.close()


def events_of(home: Path, deal_id: str) -> list[dict[str, Any]]:
    conn = deals.open_store(home)
    assert conn is not None
    try:
        return journal.read_events(conn, deal_id)
    finally:
        conn.close()


def fact(index: int, kind: str, ts: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Событие журнала в памяти: `id` задаёт порядок применения, как в БД."""
    return {
        "id": index,
        "ts": ts,
        "recorded_at": ts,
        "deal_id": "d_aaaaaa",
        "kind": kind,
        "payload": payload or {},
        "source": "test",
        "idempotency_key": f"test:{index}",
    }


def plan_stages(*ids: str) -> list[dict[str, Any]]:
    return [{"id": name} for name in ids]


# ------------------------------------------------- проектция: лента и состояние


def test_feed_is_built_from_facts_without_a_live_session() -> None:
    """Лента решений — из фактов журнала: план, перезапуск, добавление, отмена.

    Живой сессии здесь нет ни одной: лента собирается по событиям, которые уже
    записаны. Красная проба: убрать ветку перезапусков — пропадёт
    «этап verify перезапущен после contract ×2», и тест упадёт.
    """
    events = [
        fact(1, "plan_proposed", NOW - 900, {"rev": 1, "stages": plan_stages("research", "impl", "verify", "review"), "reason": "скоуп"}),
        fact(2, "plan_accepted", NOW - 900, {"rev": 1, "by": "lead"}),
        fact(3, "attempt_started", NOW - 800, {"stage": "verify", "attempt": 1, "run_id": "r1"}),
        fact(4, "attempt_ended", NOW - 780, {"stage": "verify", "attempt": 1, "outcome": "failed", "kind": "contract"}),
        fact(5, "attempt_started", NOW - 760, {"stage": "verify", "attempt": 2, "run_id": "r2"}),
        fact(6, "attempt_ended", NOW - 740, {"stage": "verify", "attempt": 2, "outcome": "failed", "kind": "contract"}),
        fact(7, "attempt_started", NOW - 720, {"stage": "verify", "attempt": 3, "run_id": "r3"}),
        fact(8, "plan_proposed", NOW - 700, {"rev": 2, "stages": plan_stages("research", "impl", "verify", "review", "docs"), "reason": "документы"}),
        fact(9, "plan_accepted", NOW - 700, {"rev": 2, "by": "lead"}),
        fact(10, "plan_rejected", NOW - 690, {"rev": 3, "why": "план ревизии 3 отклонён: отказов 1", "issues": ["stage docs: evidence"]}),
        fact(11, "stage_cancelled", NOW - 680, {"stage": "research", "reason": "скоуп вырос", "by": "lead"}),
        fact(12, "question_asked", NOW - 660, {"question_id": 7, "session_key": "duty:lead:d_aaaaaa"}),
        fact(13, "question_answered", NOW - 600, {"question_id": 7, "answer_len": 12}),
    ]

    feed = leadfeed.feed(events)
    texts = [item["text"] for item in feed]

    assert "план создан (4 этапа)" in texts
    assert "этап verify перезапущен после contract ×2" in texts
    assert "план ревизии 2: добавлен этап docs" in texts
    # Отказ плана читается словами факта, без второго счёта отказов рядом.
    assert "план ревизии 3 отклонён: отказов 1" in texts
    assert "этап research отменён: скоуп вырос" in texts
    assert "ответ на вопрос #7 пришёл от человека" in texts
    # Автор назван там, где факт его называет: план принял лид, отмену сделал он же.
    assert {item["author"] for item in feed if item["kind"] == "plan_accepted"} == {"lead"}
    assert next(item for item in feed if item["kind"] == "question_answered")["author"] == "human"
    # У перезапуска автора в факте нет: приписать его лиду значило бы выдумать.
    assert next(item for item in feed if item["text"].startswith("этап verify перезапущен"))["author"] is None


def test_feed_names_the_author_of_every_answer() -> None:
    """Маршрут ответа виден автором: лид сам, роль, человек или срок.

    Одно поле `answer_author` на ленту и на `stats`: две трактовки одного факта
    разошлись бы на первом же вопросе роли.
    """
    events = [
        fact(1, "question_asked", NOW - 500, {"question_id": 7}),
        fact(2, "question_answered", NOW - 400, {"question_id": 7, "by": "lead", "answer": "продолжай с main"}),
        fact(3, "question_asked", NOW - 300, {"text": "какой формат?"}),
        fact(4, "question_answered", NOW - 250, {"by": "researcher", "answer": "markdown", "routing": {"stage": "docs", "attempt": 1}}),
        fact(5, "question_asked", NOW - 200, {"question_id": 9}),
        fact(6, "question_answered", NOW - 150, {"question_id": 9, "answer_len": 3}),
        fact(7, "question_asked", NOW - 120, {"question_id": 11}),
        fact(8, "question_answered", NOW - 100, {"question_id": 11, "answer_len": 0, "answered_via": "expired"}),
    ]

    feed = leadfeed.feed(events)
    authors = [item["author"] for item in feed if item["kind"] == "question_answered"]

    assert authors == ["lead", "role", "human", "expired"]
    lead_answer = next(item for item in feed if item["author"] == "lead")
    assert lead_answer["text"] == "ответ на вопрос #7 дал лид: продолжай с main"
    assert "передан роли researcher" in next(item for item in feed if item["author"] == "role")["text"]
    assert "закрыт по сроку" in next(item for item in feed if item["author"] == "expired")["text"]


def test_status_says_live_absent_and_resume_counter() -> None:
    """«лид: жив / отсутствует / резюм 2 из 6» — по последнему факту и счётчику."""
    started = [fact(1, "lead_started", NOW - 900, {"pid": 1, "max": 6})]
    assert leadfeed.status(started)["text"] == "лид: жив"
    assert leadfeed.status(started)["state"] == leadfeed.STATE_LIVE

    resumed = started + [
        fact(2, "lead_exited", NOW - 800, {"reason": "budget"}),
        fact(3, "lead_resumed", NOW - 700, {"resumes": 1, "max": 6}),
        fact(4, "lead_exited", NOW - 600, {"reason": "turns"}),
        fact(5, "lead_resumed", NOW - 500, {"resumes": 2, "max": 6}),
    ]
    block = leadfeed.status(resumed)
    assert block["text"] == "лид: резюм 2 из 6"
    assert block["resumes"] == 2 and block["cap"] == 6
    assert leadfeed.wake_word(block) == "пробуждений 2 из 6"

    exited = resumed + [fact(6, "lead_exited", NOW - 400, {"reason": "crash"})]
    block = leadfeed.status(exited)
    assert block["text"] == "лид: отсутствует · резюм 2 из 6"
    assert block["state"] == leadfeed.STATE_ABSENT
    assert leadfeed.lead_word(block) == "отсутствует"

    # Дело 1.x: фактов лида нет вовсе — честное «нет фактов», а не выдуманное «жив».
    empty = leadfeed.status([fact(1, "created", NOW - 900, {"title": "старое дело"})])
    assert empty["state"] == leadfeed.STATE_UNKNOWN
    assert empty["text"] == "лид: нет фактов"
    assert leadfeed.wake_word(empty) == "пробуждений нет"


def test_lead_kinds_are_part_of_the_closed_journal_set() -> None:
    """Виды лида и отмена этапа — в закрытом наборе и не меняют состояние дела.

    Новый вид заводится вместе с правилом и тестом: здесь проверяется, что он
    записывается, читается и не воскрешает закрытое дело (`NON_STATE_KINDS`).
    """
    assert {"stage_cancelled", "lead_started", "lead_resumed", "lead_exited"} <= set(journal.EVENT_KINDS)
    assert {"stage_cancelled", "lead_started", "lead_resumed", "lead_exited"} <= set(journal.NON_STATE_KINDS)
    folded = journal.fold(
        [
            fact(1, "created", NOW - 900, {"title": "дело"}),
            fact(2, "completed", NOW - 800, {"cause": "lead_closed", "status": "done"}),
            fact(3, "lead_exited", NOW - 700, {"reason": "budget"}),
        ],
        now=NOW,
    )
    assert folded["active"] is False, "поздний факт лида не воскрешает закрытое дело"


# ------------------------------------------------------ поверхности: now, deal, why


def test_now_state_column_carries_the_lead_line(home: Path, client: TestClient) -> None:
    """`duty now`: колонка состояния получает «лид: …» из журнала, не из процесса."""
    deal = make_deal(home)
    append(
        home,
        str(deal["id"]),
        [
            ("lead_started", {"pid": 11, "max": 6}, NOW - 900),
            ("lead_exited", {"reason": "budget"}, NOW - 800),
            ("lead_resumed", {"resumes": 1, "max": 6}, NOW - 700),
        ],
    )

    body = client.get("/now").json()
    row = next(item for item in body["deals"] if item["id"] == deal["id"])
    printed = cli.render_now({"now": body["now"], "deals": [row]})

    assert row["lead"]["text"] == "лид: резюм 1 из 6"
    assert "лид: резюм 1 из 6" in printed


def test_deal_screen_shows_lead_decisions_next_to_attempts(home: Path, client: TestClient) -> None:
    """Экран дела: лента решений рядом с попытками, с автором каждого ответа."""
    deal = make_deal(home)
    append(
        home,
        str(deal["id"]),
        [
            ("plan_proposed", {"rev": 1, "stages": plan_stages("impl", "verify", "review"), "reason": "скоуп"}, NOW - 900),
            ("plan_accepted", {"rev": 1, "by": "lead"}, NOW - 900),
            ("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "r1"}, NOW - 800),
            ("attempt_ended", {"stage": "impl", "attempt": 1, "outcome": "failed", "kind": "contract"}, NOW - 780),
            ("attempt_started", {"stage": "impl", "attempt": 2, "run_id": "r2"}, NOW - 760),
            ("question_asked", {"question_id": 4, "session_key": f"duty:lead:{deal['id']}"}, NOW - 740),
            ("question_answered", {"question_id": 4, "answer_len": 6}, NOW - 700),
        ],
    )

    body = client.get(f"/deal/{deal['id']}").json()
    printed = cli.render_deal(body, None, None)

    texts = [item["text"] for item in body["decisions"]]
    assert "план создан (3 этапа)" in texts
    assert "этап impl перезапущен после contract" in texts
    assert "РЕШЕНИЯ ЛИДА · 3" in printed
    assert "[lead]  план создан (3 этапа)" in printed
    assert "[human]  ответ на вопрос #4 пришёл от человека" in printed


def test_why_says_where_it_stands_and_wakes(home: Path, client: TestClient) -> None:
    """`duty why`: этап, время начала, число пробуждений и состояние лида — из журнала."""
    deal = make_deal(home)
    started_at = NOW - 300
    append(
        home,
        str(deal["id"]),
        [
            ("lead_started", {"pid": 12, "max": 6}, NOW - 900),
            ("lead_exited", {"reason": "budget"}, NOW - 800),
            ("lead_resumed", {"resumes": 1, "max": 6}, NOW - 700),
            ("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "r1"}, started_at),
        ],
    )

    payload = client.get(f"/deal/{deal['id']}").json()
    printed = cli.render_why(payload)

    assert (
        f"стоит на этапе impl с {cli._fmt_clock(started_at)}, пробуждений 1 из 6, лид жив"
        in printed
    )

    # Дело, до которого лид ещё не дошёл (1.x и свежесозданные): фраза не врёт про
    # «жив», а называет то, что записано, — лида не поднимали.
    lonely = make_deal(home, title="без лида")
    append(home, str(lonely["id"]), [("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "r9"}, started_at)])
    printed = cli.render_why(client.get(f"/deal/{lonely['id']}").json())

    assert "пробуждений нет, лид не поднимался" in printed


# ------------------------------------------------------------------ stats


def test_stats_counts_cancelled_separately_from_closed(home: Path, client: TestClient) -> None:
    """`stats` считает отмену отдельно от завершения: два числа, а не одно «закрыто».

    Отменённое дело не сделано (`docs/ENGINE-2.0.md`, раздел 5), и складывать его
    с закрытым значило бы называть провал работой. Красная проба: считать отмену
    обычным `completed` — оба числа совпадут.
    """
    closed = make_deal(home, title="сделанное")
    cancelled = make_deal(home, title="отменённое")
    append(home, str(closed["id"]), [("completed", {"cause": "lead_closed", "status": "done", "summary": "готово"}, NOW - 500)])
    append(home, str(cancelled["id"]), [("completed", {"cause": "lead_cancelled", "status": "cancelled", "reason": "скоуп умер"}, NOW - 400)])

    body = client.get("/stats").json()
    totals = body["totals"]

    assert totals["deals_cancelled"] == 1
    assert totals["deals_closed"] == 1
    printed = cli.render_stats(body)
    assert "закрыто дел: 1 · отменено: 1 (отдельно от завершённых)" in printed


def test_stats_counts_coordination_share_question_outcomes_and_resume_cap(home: Path, client: TestClient) -> None:
    """`stats`: доля координации в токенах, исходы вопросов, упёршиеся в потолок резюмов.

    Токены лида и токены работы — две суммы разных фактов: доля считается по ним,
    а не по числу вызовов. Потолок резюмов читается из факта (`max`), поэтому
    «упёрлось» — это `resumes >= max`, а не «лид падал хоть раз».
    """
    deal = make_deal(home, title="координация")
    append(
        home,
        str(deal["id"]),
        [
            ("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "r1"}, NOW - 900),
            ("attempt_ended", {"stage": "impl", "attempt": 1, "outcome": "done", "details": {"usage_tokens": 3000}}, NOW - 800),
            ("lead_started", {"pid": 13, "max": 2}, NOW - 700),
            ("lead_exited", {"reason": "budget", "usage_tokens": 1000}, NOW - 650),
            ("lead_resumed", {"resumes": 1, "max": 2}, NOW - 600),
            ("lead_exited", {"reason": "budget", "usage_tokens": 1000}, NOW - 550),
            ("lead_resumed", {"resumes": 2, "max": 2}, NOW - 500),
            ("question_asked", {"question_id": 5}, NOW - 450),
            ("question_answered", {"question_id": 5, "by": "lead", "answer": "делай"}, NOW - 400),
            ("question_asked", {"question_id": 6}, NOW - 350),
        ],
    )

    body = client.get("/stats").json()
    totals = body["totals"]

    assert totals["coordination"]["tokens"] == 2000
    assert totals["coordination"]["share"] == pytest.approx(0.4)  # 2000 / (2000 + 3000)
    assert totals["question_outcomes"]["answered"]["lead"] == 1
    assert totals["question_outcomes"]["waiting"] == 1
    assert totals["lead_resumes"]["deals"] == 1
    printed = cli.render_stats(body)
    assert "координация лида: 2k (доля 40%, фактов лида 2)" in printed
    assert "исходы вопросов: lead 1 · ждут 1" in printed
    assert "дел упёрлось в max_lead_resumes: 1" in printed


def test_stats_says_no_data_when_lead_tokens_are_not_measured(home: Path, client: TestClient) -> None:
    """Ни один факт лида не записал токенов — «нет данных», а не «0 токенов».

    Ноль здесь был бы выдумкой: расход лида может быть просто не измерен, и
    подменять одно другим значит врать про координацию (так же считаются токены
    попыток).
    """
    deal = make_deal(home, title="без телеметрии лида")
    append(home, str(deal["id"]), [("lead_started", {"pid": 14, "max": 6}, NOW - 900)])

    body = client.get("/stats").json()

    assert body["totals"]["coordination"]["tokens"] is None
    assert body["totals"]["coordination"]["share"] is None
    assert "координация лида: нет данных" in cli.render_stats(body)
