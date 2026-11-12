"""Тулсет `duty` — четыре инструмента лида (`docs/ENGINE-2.0.md`, разделы 4-5).

Проверяется контракт на том, что видно в журнале, а не на обещаниях docstring:

1. `duty_plan` с этапом без `evidence` (у роли, которой `evidence` нужен)
   отвергается: два факта плана, список проблем лиду, граф дела не растёт;
2. повтор `duty_plan` с тем же `idempotency_key` даёт один `plan_accepted` и
   прежний ответ;
3. `duty_start` на занятом этапе и на исчерпанном `MAX_SLOTS` отказывает, и
   журнал не растёт;
4. `duty_wait` возвращает факты пачкой с курсором, телеметрию не возвращает, а
   пустое окно — `timed_out: true`;
5. `duty_close` закрывает дело итогом лида, живым попыткам отказывает;
6. тулсет виден профилю: `register(ctx)` объявляет ровно те четыре инструмента,
   что записаны в `plugin.yaml`;
7. повтор `duty_start` с тем же ключом не поднимает вторую попытку.

События строятся в форме журнала (`kind`, `payload`, `ts`, `id`): слой инструментов
читает ровно эту форму. Спавн попытки подменяется фейком, который пишет свой
`attempt_started`, — настоящий процесс в тестах не нужен.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import sqlite3
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

NOW = 1_790_000_000

# Ключи фактов теста: уникальны в пределах прогона, иначе `INSERT OR IGNORE`
# склеил бы два разных факта одного вида.
_SEQ = itertools.count()

# Хартия дела: потолок графа и роли, которыми план вправе его растить. Пустой
# `stages` спека 2.0 пока не умеет (`docs/ENGINE-2.0.md`, раздел 3, работа 7),
# поэтому seed-этап в файле есть — плану он и нужен как точка опоры.
SEED = """
duty: тулсет лида
repo: ~/GolandProjects/duty
charter:
  max_stages: 6
  allowed_roles: [implementer, researcher, reviewer]
  allowed_kinds: [agent, shell, gate]
stages:
  - id: impl
    role: implementer
    workspace: none
    evidence:
      - {cmd: "true", must: 0}
"""


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути: пакета нет, грузим так же, как это делает движок."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec_module = load_module("hermes_duty_spec", "spec.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
transit = load_module("hermes_duty_transit", "transit.py")
tools = load_module("hermes_duty_tools", "tools.py")


# ------------------------------------------------------------------ фикстуры


def make_home(tmp_path: Path) -> Path:
    """Корень Hermes: своя `duty/duty.db` и свой пул вопросов."""
    home = tmp_path / "home"
    (home / "messaging").mkdir(parents=True)
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.executescript(
        """
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
    )
    conn.commit()
    conn.close()
    return home


def write_spec(tmp_path: Path, text: str = SEED, name: str = "duty.yaml") -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(conn: sqlite3.Connection, spec: Any) -> dict[str, Any]:
    """Своё дело: строка store плюс путь спеки, записанный в `created`."""
    created = deals.create_deal(
        conn,
        repo="duty",
        title="Тулсет лида",
        body="",
        now=NOW,
        spec_path=str(spec.path),
    )
    deal = deals.deal_row(conn, str(created["deal"]["id"]))
    assert deal is not None
    return deal


def append(conn: sqlite3.Connection, deal: dict[str, Any], *facts: tuple[str, dict[str, Any]]) -> None:
    """Факты в журнал: ключ события уникален, иначе пачка одного вида слипнется."""
    journal.append(
        conn,
        [
            {
                "ts": NOW + index,
                "recorded_at": NOW + index,
                "deal_id": str(deal["id"]),
                "kind": kind,
                "payload": payload,
                "source": "engine",
                "idempotency_key": f"test:{deal['id']}:{kind}:{next(_SEQ)}",
            }
            for index, (kind, payload) in enumerate(facts)
        ],
    )


def kinds(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[str]:
    """Виды фактов дела по порядку — «журнал вырос / не вырос» и «что записано»."""
    return [str(event["kind"]) for event in journal.read_events(conn, str(deal["id"]))]


def row_count(conn: sqlite3.Connection, deal: dict[str, Any]) -> int:
    return len(journal.read_events(conn, str(deal["id"])))


def head(conn: sqlite3.Connection, deal: dict[str, Any]) -> int:
    """Курсор на «всё уже прочитано»: `id` последнего факта дела."""
    events = journal.read_events(conn, str(deal["id"]))
    return int(events[-1]["id"]) if events else 0


def refusal(call: Any, *args: Any, **kwargs: Any) -> str:
    """Причина отказа инструмента: отказ — исключение, ответ готовит обработчик.

    Так же устроен движок: guard clause бросает, а слой ответа (`_run`)
    переводит отказ в `{ok: false}` без трейсбека.
    """
    with pytest.raises(tools.ToolsError) as caught:
        call(*args, **kwargs)
    return str(caught.value)


def started(conn: sqlite3.Connection, deal: dict[str, Any], stage: str, attempt: int = 1, lease_sec: int = 3600) -> None:
    """Живая попытка этапа: `attempt_started` с действующим лизингом."""
    append(
        conn,
        deal,
        (
            "attempt_started",
            {
                "run_id": f"{stage}#{attempt}",
                "stage": stage,
                "attempt": attempt,
                "pid": 999999,
                "lease_expires": int(time.time()) + lease_sec,
            },
        ),
    )


def fake_spawn(written: list[tuple[Any, ...]]) -> Any:
    """Спавн попытки без процесса: фейк сам пишет `attempt_started` с ключом вызова.

    Так проверяется стыковка «лид поднял попытку → факт в журнале», а не работа
    `hermes chat`: настоящий процесс в тестах стоил бы денег и секунд.
    """

    def spawn(home: Path, deal_id: str, stage_id: str, attempt: int, key: str) -> int:
        written.append((deal_id, stage_id, attempt, key))
        conn = open_store(home)
        try:
            deal = deals.deal_row(conn, deal_id)
            assert deal is not None
            append(
                conn,
                deal,
                (
                    "attempt_started",
                    {
                        "run_id": f"{stage_id}#{attempt}",
                        "stage": stage_id,
                        "attempt": attempt,
                        "pid": 4242,
                        "lease_expires": int(time.time()) + 3600,
                        "idem": key,
                    },
                ),
            )
        finally:
            conn.close()
        return 4242

    return spawn


# ------------------------------------------------------- 1. план и его отказ


def test_plan_without_evidence_is_rejected_and_the_graph_stands(tmp_path: Path) -> None:
    """Этап без `evidence` у роли, которой он нужен: отказ, граф дела не вырос.

    `implementer` что-то производит, и план не вправе добавить этап, который
    закроется без доказательств (`docs/ENGINE-2.0.md`, раздел 4). Отказ — два
    факта (`plan_proposed` + `plan_rejected`) и список проблем лиду.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)

    answer = tools.plan(
        str(deal["id"]),
        [{"id": "check", "role": "implementer", "needs": ["impl"], "workspace": "none"}],
        "проверить работу",
        "plan-1",
        home=home,
        now=NOW,
    )

    assert answer["ok"] is True
    assert answer["accepted"] is False
    assert answer["rev"] == 1
    assert any("plan.evidence" in issue for issue in answer["issues"])
    assert "plan.evidence" in answer["text"]
    assert kinds(conn, deal)[-2:] == [transit.PLAN_PROPOSED, transit.PLAN_REJECTED]
    events = journal.read_events(conn, str(deal["id"]))
    assert tuple(stage.id for stage in transit.graph(spec, events)) == ("impl",)
    assert transit.graph(spec, events) == spec.stages


def test_plan_accepted_grows_the_graph(tmp_path: Path) -> None:
    """Принятый план становится графом: этап плана есть в срезе дела."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)

    answer = tools.plan(
        str(deal["id"]),
        [{"id": "research", "role": "researcher", "needs": ["impl"], "workspace": "none"}],
        "скоуп неясен",
        "plan-1",
        home=home,
        now=NOW,
    )

    assert answer["accepted"] is True
    assert answer["stages"] == ["research"]
    assert answer["graph"] == ["impl", "research"]
    events = journal.read_events(conn, str(deal["id"]))
    accepted = [event for event in events if event["kind"] == transit.PLAN_ACCEPTED]
    assert len(accepted) == 1
    assert accepted[0]["payload"]["by"] == "lead"
    assert accepted[0]["payload"]["rev"] == answer["rev"]


def test_plan_repeat_keeps_one_accepted_fact(tmp_path: Path) -> None:
    """Повтор с тем же ключом: один `plan_accepted`, тот же ответ, ноль новых строк."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    stages = [{"id": "research", "role": "researcher", "needs": ["impl"], "workspace": "none"}]

    first = tools.plan(str(deal["id"]), stages, "скоуп", "same-key", home=home, now=NOW)
    after_first = row_count(conn, deal)
    second = tools.plan(str(deal["id"]), stages, "скоуп", "same-key", home=home, now=NOW + 10)

    assert second["accepted"] is True
    assert second["repeated"] is True
    assert second["rev"] == first["rev"] == 1
    assert second["stages"] == first["stages"]
    assert row_count(conn, deal) == after_first
    assert kinds(conn, deal).count(transit.PLAN_ACCEPTED) == 1


def test_plan_repeat_of_a_rejected_plan_gives_the_same_issues(tmp_path: Path) -> None:
    """Повтор отвергнутого плана — тот же список проблем и ни одного нового факта."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    stages = [{"id": "check", "role": "implementer", "needs": ["impl"], "workspace": "none"}]

    first = tools.plan(str(deal["id"]), stages, "", "reject-key", home=home, now=NOW)
    after_first = row_count(conn, deal)
    second = tools.plan(str(deal["id"]), stages, "", "reject-key", home=home, now=NOW + 10)

    assert second["accepted"] is False
    assert second["repeated"] is True
    assert second["issues"] == first["issues"]
    assert row_count(conn, deal) == after_first


# ----------------------------------------------------- 2. запуск и его отказы


def test_start_on_a_busy_stage_refuses_without_writing(tmp_path: Path) -> None:
    """По этапу уже идёт живая попытка: отказ до спавна, журнал не растёт."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    started(conn, deal, "impl")
    before = row_count(conn, deal)
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "impl", "start-1", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "уже идёт" in reason
    assert spawned == []
    assert row_count(conn, deal) == before


def test_start_refuses_past_the_charter_attempt_ceiling(tmp_path: Path) -> None:
    """Серия этапа исчерпана хартией: `duty_start` отказывает, повтор — не работа.

    `charter.max_attempts: 2` объявляет две попытки на этап включая авто-повторы
    (`docs/ENGINE-2.0.md`, раздел 11). Закрытые попытки 1 и 2 — серия исчерпана, и
    третий `duty_start` по тому же этапу поднял бы попытку, которую хартия не
    разрешает: отказ до спавна, журнал не растёт.
    """
    home = make_home(tmp_path)
    spec = write_spec(
        tmp_path,
        """
        duty: потолок попыток
        repo: ~/GolandProjects/duty
        charter:
          max_stages: 6
          max_attempts: 2
          allowed_roles: [implementer]
        stages:
          - id: impl
            role: implementer
            workspace: none
            evidence:
              - {cmd: "true", must: 0}
        """,
    )
    conn = open_store(home)
    deal = make_deal(conn, spec)
    for attempt in (1, 2):
        append(
            conn,
            deal,
            (
                "attempt_started",
                {"run_id": f"impl#{attempt}", "stage": "impl", "attempt": attempt, "pid": attempt},
            ),
            (
                "attempt_ended",
                {"run_id": f"impl#{attempt}", "stage": "impl", "attempt": attempt, "outcome": "done"},
            ),
        )
    before = row_count(conn, deal)
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "impl", "start-3", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "попыток 2 из 2" in reason
    assert "charter.max_attempts" in reason
    assert spawned == []
    assert row_count(conn, deal) == before


def test_start_refuses_when_slots_are_taken(tmp_path: Path) -> None:
    """Живых попыток `MAX_SLOTS`: третий этап не поднимается, журнал не растёт."""
    home = make_home(tmp_path)
    spec = write_spec(
        tmp_path,
        """
        duty: слоты
        repo: ~/GolandProjects/duty
        charter:
          max_stages: 6
          allowed_roles: [implementer, researcher]
        stages:
          - id: impl
            role: implementer
            workspace: none
            evidence:
              - {cmd: "true", must: 0}
          - id: verify
            role: implementer
            workspace: none
            evidence:
              - {cmd: "true", must: 0}
          - id: check
            role: implementer
            workspace: none
            evidence:
              - {cmd: "true", must: 0}
        """,
    )
    conn = open_store(home)
    deal = make_deal(conn, spec)
    started(conn, deal, "impl")
    started(conn, deal, "verify")
    before = row_count(conn, deal)
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "check", "start-3", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert tools.MAX_SLOTS == 2
    assert "живых попыток 2 из 2" in reason
    assert spawned == []
    assert row_count(conn, deal) == before


def test_start_refuses_a_stage_outside_the_plan(tmp_path: Path) -> None:
    """Этапа нет в принятом плане: отказ, ни процесса, ни факта."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    before = row_count(conn, deal)
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-x", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "нет в принятом плане" in reason
    assert spawned == []
    assert row_count(conn, deal) == before


def test_start_refuses_when_the_deal_budget_is_spent(tmp_path: Path) -> None:
    """Потолок дела исчерпан: следующий этап не поднимается, журнал не растёт.

    Потолок считается исчерпанным и при точном равенстве (`budget.refusal`): место
    под новую попытку уже не освободится, поэтому «ровно N из N» — тоже отказ.
    """
    home = make_home(tmp_path)
    spec = write_spec(
        tmp_path,
        """
        duty: бюджет
        repo: ~/GolandProjects/duty
        charter:
          max_stages: 6
          allowed_roles: [implementer]
        budget:
          tokens: 100
        stages:
          - id: impl
            role: implementer
            workspace: none
            evidence:
              - {cmd: "true", must: 0}
        """,
    )
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        ("attempt_started", {"run_id": "impl#0", "stage": "impl", "attempt": 0, "pid": 1}),
        ("attempt_ended", {"run_id": "impl#0", "stage": "impl", "attempt": 0, "outcome": "done", "details": {"usage_tokens": 100}}),
    )
    before = row_count(conn, deal)
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "impl", "start-budget", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "100/100 токенов" in reason
    assert spawned == []
    assert row_count(conn, deal) == before


def test_start_lifts_an_attempt_and_confirms_it_by_fact(tmp_path: Path) -> None:
    """Успешный запуск: процесс поднят, ответ опирается на факт `attempt_started`."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    spawned: list[tuple[Any, ...]] = []

    answer = tools.start(
        str(deal["id"]), "impl", "start-ok", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert answer["started"] is True
    assert answer["confirmed"] is True
    assert answer["run_id"] == "impl#1"
    assert answer["attempt"] == 1
    assert spawned == [(str(deal["id"]), "impl", 1, "start-ok")]
    events = journal.read_events(conn, str(deal["id"]))
    fact = [event for event in events if event["kind"] == "attempt_started"][-1]
    assert fact["payload"]["idem"] == "start-ok"


def test_start_lifts_a_detached_process(tmp_path: Path) -> None:
    """Настоящий спавн: попытку исполняет отдельный процесс и сам пишет свои факты.

    Фейк проверяет стыковку с журналом, а этот тест — то, что фейк подменить не
    может: процесс отвязан (`start_new_session`), сам берёт лизинг, сам
    закрывается фактом `attempt_ended`. Считать деньги на модели здесь нечего,
    поэтому этап — shell-команда (`cmd: "true"`).
    """
    home = make_home(tmp_path)
    spec = write_spec(
        tmp_path,
        """
        duty: живой запуск
        charter:
          max_stages: 6
          allowed_kinds: [agent, shell, gate]
        stages:
          - id: touch
            role: implementer
            kind: shell
            workspace: none
            cmd: "true"
            must: 0
        """,
    )
    conn = open_store(home)
    deal = make_deal(conn, spec)

    answer = tools.start(str(deal["id"]), "touch", "live-1", home=home, ack_sec=30)

    assert answer["confirmed"] is True
    assert answer["run_id"] == "touch#1"
    assert answer["pid"] not in (None, os.getpid())
    for _ in range(60):
        events = journal.read_events(conn, str(deal["id"]))
        if any(event["kind"] == "attempt_ended" for event in events):
            break
        time.sleep(0.25)
    events = journal.read_events(conn, str(deal["id"]))
    assert [event["kind"] for event in events] == ["created", "attempt_started", "attempt_ended"]
    ended = journal.payload_of(events[-1])
    assert ended["run_id"] == "touch#1"
    assert ended["outcome"] == "done"


def test_start_repeat_does_not_lift_a_second_attempt(tmp_path: Path) -> None:
    """Повтор с тем же ключом: тот же `run_id`, второго процесса и факта нет."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    spawned: list[tuple[Any, ...]] = []

    first = tools.start(
        str(deal["id"]), "impl", "same-start", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )
    before = row_count(conn, deal)
    second = tools.start(
        str(deal["id"]), "impl", "same-start", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert first["run_id"] == second["run_id"] == "impl#1"
    assert second["repeated"] is True
    assert len(spawned) == 1
    assert row_count(conn, deal) == before


# ------------------------------------------------------------ 3. ожидание


def test_wait_returns_a_batch_with_the_cursor(tmp_path: Path) -> None:
    """Пачка новых фактов и курсор: второй вызов не пересказывает виденное."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    position = head(conn, deal)
    append(
        conn,
        deal,
        ("attempt_started", {"run_id": "impl#1", "stage": "impl", "attempt": 1, "pid": 1}),
        ("attempt_ended", {"run_id": "impl#1", "stage": "impl", "attempt": 1, "outcome": "done"}),
    )

    answer = tools.wait(str(deal["id"]), position, 0, home=home)

    assert answer["timed_out"] is False
    assert [event["kind"] for event in answer["events"]] == ["attempt_started", "attempt_ended"]
    assert answer["cursor"] == answer["events"][-1]["id"]
    more = tools.wait(str(deal["id"]), answer["cursor"], 0, home=home)
    # `answers` — сколько фактов ответа человека дочитано из пула этим вызовом:
    # ответ на `duty_ask_human` приходит лиду следующим фактом ожидания
    # (`docs/ENGINE-2.0.md`, раздел 6), поэтому счётчик есть в каждом ответе.
    assert more == {
        "ok": True,
        "deal": str(deal["id"]),
        "events": [],
        "cursor": answer["cursor"],
        "timed_out": True,
        "answers": 0,
    }


def test_wait_skips_telemetry_but_moves_the_cursor(tmp_path: Path) -> None:
    """Телеметрия лида не будит, но курсор двигает: журнал не перечитывается вечно."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    position = head(conn, deal)
    append(conn, deal, ("attempt_progress", {"stage": "impl", "attempt": 1, "seq": 1, "phase": "tool_use"}))

    answer = tools.wait(str(deal["id"]), position, 0, home=home)

    assert answer["events"] == []
    assert answer["timed_out"] is True
    assert answer["cursor"] > position


def test_wait_ceiling_is_three_hundred_seconds(tmp_path: Path) -> None:
    """Потолок `timeout_sec` — 300 с: дольше один вызов турн не держит."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    position = head(conn, deal)
    moment = [0.0]
    slept: list[float] = []

    def sleep(sec: float) -> None:
        slept.append(sec)
        moment[0] += sec

    answer = tools.wait(
        str(deal["id"]), position, 10_000, home=home, clock=lambda: moment[0], sleep=sleep
    )

    assert tools.WAIT_CEILING_SEC == 300.0
    assert answer["timed_out"] is True
    assert round(sum(slept), 3) == 300.0  # окно урезано потолком, а не запрошенным часом
    assert answer["cursor"] == position


# -------------------------------------------------------------- 4. закрытие


def test_close_ends_the_deal_with_the_lead_cause(tmp_path: Path) -> None:
    """Закрытие: `completed` с `cause=lead_closed` и итогом лида; повтор — без факта."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        ("attempt_started", {"run_id": "impl#1", "stage": "impl", "attempt": 1, "pid": 1}),
        ("attempt_ended", {"run_id": "impl#1", "stage": "impl", "attempt": 1, "outcome": "done"}),
    )

    answer = tools.close(str(deal["id"]), "сделано и проверено", "approve", "close-1", home=home, now=NOW)

    assert answer["closed"] is True
    assert answer["cause"] == "lead_closed"
    events = journal.read_events(conn, str(deal["id"]))
    closed = journal.closed_fact(events)
    assert closed is not None
    assert closed["payload"]["cause"] == "lead_closed"
    assert closed["payload"]["summary"] == "сделано и проверено"
    before = row_count(conn, deal)
    again = tools.close(str(deal["id"]), "сделано и проверено", "approve", "close-1", home=home, now=NOW + 5)
    assert again["repeated"] is True
    assert row_count(conn, deal) == before


def test_close_refuses_while_attempts_are_alive(tmp_path: Path) -> None:
    """Живые попытки: закрытие — отказ, сначала их гасит `duty_cancel`."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    started(conn, deal, "impl")
    before = row_count(conn, deal)

    reason = refusal(tools.close, str(deal["id"]), "итог", "approve", "close-2", home=home, now=NOW)

    assert "живых попыток" in reason
    assert row_count(conn, deal) == before


def test_close_refuses_a_verdict_that_is_not_approve(tmp_path: Path) -> None:
    """Дело закрывает слово `approve`; провал — не закрытие, а отмена."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    before = row_count(conn, deal)

    reason = refusal(tools.close, str(deal["id"]), "не вышло", "failed", "close-3", home=home, now=NOW)

    assert "duty_cancel_deal" in reason
    assert row_count(conn, deal) == before


def test_mutating_tools_require_an_idempotency_key(tmp_path: Path) -> None:
    """Без ключа мутирующий инструмент отказывает: повтор не отличить от решения."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    before = row_count(conn, deal)

    assert "idempotency_key" in refusal(tools.plan, str(deal["id"]), [], "", "", home=home, now=NOW)
    assert "idempotency_key" in refusal(tools.start, str(deal["id"]), "impl", "", home=home, ack_sec=0)
    assert "idempotency_key" in refusal(tools.close, str(deal["id"]), "итог", "approve", "", home=home, now=NOW)
    assert row_count(conn, deal) == before


# ------------------------------------------------------- 5. инструменты видны


class RecordingContext:
    """Заглушка `PluginContext`: запоминает, что именно регистрирует плагин."""

    def __init__(self) -> None:
        self.tools: list[dict[str, Any]] = []

    def register_tool(self, name: str, **kwargs: Any) -> None:
        self.tools.append({"name": name, **kwargs})


def load_plugin() -> Any:
    """`__init__.py` плагина — так же, как его грузит `hermes plugins validate`."""
    name = "hermes_duty_plugin_under_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]  # noqa: SLF001 - так же делает загрузчик ядра
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_register_declares_the_duty_toolset() -> None:
    """`register(ctx)` объявляет ровно девять инструментов тулсета `duty`."""
    plugin = load_plugin()
    ctx = RecordingContext()

    plugin.register(ctx)

    assert [tool["name"] for tool in ctx.tools] == [
        "duty_plan",
        "duty_start",
        "duty_wait",
        "duty_answer",
        "duty_ask",
        "duty_ask_human",
        "duty_cancel",
        "duty_cancel_deal",
        "duty_close",
    ]
    assert {tool["toolset"] for tool in ctx.tools} == {"duty"}
    for tool in ctx.tools:
        assert callable(tool["handler"])
        assert tool["schema"]["name"] == tool["name"]
        assert tool["schema"]["parameters"]["type"] == "object"
        assert tool["emoji"]


def test_manifest_declares_what_register_registers() -> None:
    """`provides_tools` в `plugin.yaml` и то, что регистрируется, — один список."""
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    plugin = load_plugin()
    ctx = RecordingContext()
    plugin.register(ctx)

    assert [str(name) for name in manifest["provides_tools"]] == [tool["name"] for tool in ctx.tools]


def test_handlers_answer_with_json(tmp_path: Path) -> None:
    """Обработчик инструмента отвечает строкой JSON: так его читает модель."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)

    answer = json.loads(
        tools.handle_wait({"deal": str(deal["id"]), "cursor": head(conn, deal), "timeout_sec": 0}, home=home)
    )

    assert answer["ok"] is True
    assert answer["timed_out"] is True
    refused = json.loads(tools.handle_start({"deal": "t_deadbeef", "stage_id": "impl", "idempotency_key": "x"}))
    assert refused["ok"] is False
    assert "d_<hex>" in refused["error"]
