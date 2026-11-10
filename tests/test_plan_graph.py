"""Граф дела растёт из журнала (1.0, тонкий срез): план, хартия, гейт плана.

Проверяется контракт `docs/ENGINE-1.0.md` (раздел 8) на том, что видно в
журнале, а не на обещаниях в docstring:

1. старый журнал без фактов плана даёт ровно seed-граф — поведение 0.0.8 не
   меняется ни на факт;
2. `plan_proposed` граф не меняет, `plan_accepted` — меняет;
3. `plan_rejected` возвращает этап плана в работу с причиной и `issues[]`, а
   сверх `charter.max_rounds_replan` — `failed(contract)`;
4. предложение сверх `max_stages` отклоняется, дело не растёт;
5. `publish` планом не добавляется, этап без `evidence`, который что-то
   производит, тоже;
6. гейт плана: предложение больше порога — вопрос человеку (`ask()`), до ответа
   `wait_human`; «принять» пишет `plan_accepted` с `by: human`;
7. реплей: `stage_states` на том же журнале даёт тот же срез целиком;
8. `close_stages_done` закрывает дело по графу из журнала.

События строятся в форме журнала (`kind`, `payload`, `ts`, `id`): слой перехода
читает ровно эту форму. Часы нигде не читаются — `now` передаётся аргументом.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

NOW = 1_790_000_000
DEAL = "d_plan"


def clock_later(seconds: int) -> Any:
    """Часы шага движка: чуть позже фактов пула.

    `ask()` и `ask.sync_answers` пишут настоящие часы, поэтому «решение движка»
    обязано быть позже вопроса и ответа — иначе fold прочитал бы решение до
    вопроса, которого ещё не было.
    """
    return lambda: float(int(time.time()) + seconds)


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
transit = load_module("hermes_duty_transit", "transit.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
runner = load_module("hermes_duty_runner", "runner.py")
driver = load_module("hermes_duty_driver", "driver.py")
stagefeed = load_module("hermes_duty_stagefeed", "stagefeed.py")
ask_module = load_module("hermes_duty_ask", "ask.py")
cli = load_module("hermes_duty_cli", "cli.py")

# Фейковый спавн исполнителя берётся у тестов движка: он пишет `result.json` по
# контракту, который движок положил на диск, — то есть проверяется настоящая
# стыковка «контракт → отчёт», а не подставленный словарь.
from test_runner import Fake  # noqa: E402

# Хартия по умолчанию: потолок графа 6, один реплан, гейт плана после четырёх
# этапов. Числа маленькие, чтобы порог проверялся двумя этапами плана.
SEED = """
duty: план дела
charter:
  max_stages: 6
  max_rounds_replan: 1
  plan_gate: {min_stages: 4}
stages:
  - id: plan
    role: implementer
    workspace: none
  - id: close
    role: lead
    needs: [plan]
    workspace: none
    verdict: {approve: next, changes: plan, max_rounds: 1}
"""

# Спека 0.0.8: без `charter` и без этапа плана — граф задан файлом.
OLD = """
duty: старое дело
stages:
  - id: impl
    role: implementer
  - id: check
    role: implementer
    needs: [impl]
"""


def write_spec(tmp_path: Path, text: str, name: str = "duty.yaml") -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def seed_spec(tmp_path: Path) -> Any:
    return write_spec(tmp_path, SEED)


def plan_stage(stage_id: str, *, needs: tuple[str, ...] = ("plan",), evidence: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    """Этап предложения плана: работа, которую можно проверить (evidence есть)."""
    return {
        "id": stage_id,
        "role": "implementer",
        "needs": list(needs),
        "workspace": "none",
        "evidence": [dict(item) for item in (evidence or ({"cmd": "true", "must": 0},))],
    }


def proposal(*stages: dict[str, Any]) -> list[dict[str, Any]]:
    """Предложение по цепочке: каждый этап после предыдущего, первый — после plan."""
    out: list[dict[str, Any]] = []
    previous = "plan"
    for stage in stages:
        item = dict(stage)
        item["needs"] = list(item.get("needs") or [previous])
        out.append(item)
        previous = str(item["id"])
    return out


def events(*items: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """События журнала в форме `journal.read_events`: id растёт, `ts` один."""
    return [
        {"id": index + 1, "ts": NOW, "deal_id": DEAL, "kind": kind, "payload": payload, "source": "engine"}
        for index, (kind, payload) in enumerate(items)
    ]


def ended(stage: str, attempt: int = 1, outcome: str = "done", kind: str | None = None) -> dict[str, Any]:
    return {
        "stage": stage,
        "attempt": attempt,
        "outcome": outcome,
        "kind": kind,
        "run_id": f"{stage}#{attempt}",
        "reason": "",
    }


def accepted(rev: int, by: str = "engine") -> tuple[str, dict[str, Any]]:
    return ("plan_accepted", {"rev": rev, "by": by})


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


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(conn: sqlite3.Connection, home: Path, spec: Any) -> dict[str, Any]:
    """Своё дело: строка store плюс путь спеки, записанный в `created`."""
    created = deals.create_deal(
        conn,
        repo="duty",
        title="План дела",
        body="",
        now=NOW,
        spec_path=str(spec.path),
    )
    deal = deals.deal_row(conn, str(created["deal"]["id"]))
    assert deal is not None
    return deal


def answer_pool(home: Path, question_id: Any, text: str | None, status: str = "answered") -> None:
    """Человек ответил (или вопрос истёк) — так вопрос закрывает пул, не duty.

    `answered_at` — позже вопроса: пул кладёт его в `question_answered.ts`, и
    ответ, помеченный раньше вопроса, fold прочитал бы до самого вопроса.
    """
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(time.time()) + 60)) + ".000Z"
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        conn.execute(
            "UPDATE questions SET status = ?, answer = ?, answered_at = ?, answered_via = ? WHERE id = ?",
            (status, text, iso, None, int(question_id)),
        )
        conn.commit()
    finally:
        conn.close()


def pool_rows(home: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM questions ORDER BY id")]
    finally:
        conn.close()


# ------------------------------------------------- 1. журнал без фактов плана


def test_journal_without_plan_keeps_the_seed_graph(tmp_path: Path) -> None:
    """Журнал 0.0.8: граф — ровно `spec.stages`, срез тот же, что был до 1.0.

    Это гейт «старые дела работают без правок файла»: ни одного нового этапа, ни
    одного нового состояния — `graph` возвращает seed, а срез и решение идут по
    нему.
    """
    spec = write_spec(tmp_path, OLD)
    journal_events = events(
        ("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "impl#1"}),
        ("attempt_ended", ended("impl")),
    )

    assert transit.graph(spec, journal_events) == spec.stages
    states = transit.stage_states(spec, journal_events)
    assert tuple(states) == ("impl", "check")
    assert states["impl"].state == "done"
    assert transit.next_stage(spec, journal_events, NOW).pair == [("check", 1)]


# ------------------------------------------------------ 2. предложение и решение


def test_plan_proposed_does_not_grow_the_graph(tmp_path: Path) -> None:
    """`plan_proposed` — факт о предложении: граф остаётся seed-ом, дело ждёт плана.

    Иначе `close` (у него `needs: [plan]`) закрыл бы дело по графу, которого ещё
    не приняли: предложение — не план.
    """
    spec = seed_spec(tmp_path)
    stages = proposal(plan_stage("research"), plan_stage("impl"))
    journal_events = events(
        ("attempt_started", {"stage": "plan", "attempt": 1, "run_id": "plan#1"}),
        ("attempt_ended", ended("plan")),
        ("plan_proposed", {"rev": 1, "stages": stages, "reason": "скоуп", "source_stage": "plan", "attempt": 1}),
    )

    assert tuple(stage.id for stage in transit.graph(spec, journal_events)) == ("plan", "close")
    states = transit.stage_states(spec, journal_events)
    assert tuple(states) == ("plan", "close")
    decision = transit.next_stage(spec, journal_events, NOW)
    assert (decision.kind, decision.what) == ("wait", "plan")
    assert "research" in decision.reason


def test_plan_accepted_grows_the_graph(tmp_path: Path) -> None:
    """`plan_accepted` — граф: этапы предложения появляются в срезе и запускаются."""
    spec = seed_spec(tmp_path)
    stages = proposal(plan_stage("research"), plan_stage("impl"))
    journal_events = events(
        ("attempt_started", {"stage": "plan", "attempt": 1, "run_id": "plan#1"}),
        ("attempt_ended", ended("plan")),
        ("plan_proposed", {"rev": 1, "stages": stages, "reason": "скоуп", "source_stage": "plan", "attempt": 1}),
        accepted(1),
    )

    assert tuple(stage.id for stage in transit.graph(spec, journal_events)) == ("plan", "close", "research", "impl")
    states = transit.stage_states(spec, journal_events)
    assert tuple(states) == ("plan", "close", "research", "impl")
    assert states["plan"].state == "done"
    decision = transit.next_stage(spec, journal_events, NOW)
    assert decision.pair == [("research", 1)]


def test_replay_gives_the_same_slice(tmp_path: Path) -> None:
    """Реплей: тот же журнал в обратном порядке — тот же срез целиком.

    Сравнивается полный `dict[str, StageState]`, а не отдельные поля: разошедшийся
    раунд или причина — такой же дефект реплея, как пропавший этап.
    """
    spec = seed_spec(tmp_path)
    stages = proposal(plan_stage("research"), plan_stage("impl"))
    journal_events = events(
        ("attempt_started", {"stage": "plan", "attempt": 1, "run_id": "plan#1"}),
        ("attempt_ended", ended("plan")),
        ("plan_proposed", {"rev": 1, "stages": stages, "reason": "скоуп", "source_stage": "plan", "attempt": 1}),
        accepted(1),
        ("attempt_started", {"stage": "research", "attempt": 1, "run_id": "research#1"}),
        ("attempt_ended", ended("research")),
    )

    forward = transit.stage_states(spec, journal_events)
    backward = transit.stage_states(spec, list(reversed(journal_events)))
    shuffled = transit.stage_states(spec, [journal_events[3], journal_events[0], journal_events[5], journal_events[1], journal_events[4], journal_events[2]])

    assert forward == backward == shuffled
    assert forward["research"].state == "done"
    assert transit.graph(spec, journal_events) == transit.graph(spec, list(reversed(journal_events)))


# --------------------------------------------------------- 3. отказ и реплан


def test_plan_rejected_reopens_the_plan_stage(tmp_path: Path) -> None:
    """`plan_rejected` возвращает этап плана в работу: причина, отказы, раунд."""
    spec = seed_spec(tmp_path)
    journal_events = events(
        ("attempt_started", {"stage": "plan", "attempt": 1, "run_id": "plan#1"}),
        ("attempt_ended", ended("plan")),
        ("plan_proposed", {"rev": 1, "stages": [plan_stage("research")], "reason": "", "source_stage": "plan", "attempt": 1}),
        ("plan_rejected", {"rev": 1, "why": "план ревизии 1 отклонён", "issues": ["plan.evidence: этап research что-то производит, а evidence пуст"]}),
    )

    states = transit.stage_states(spec, journal_events)
    assert states["plan"].state == "pending"
    assert states["plan"].rounds == 1
    assert "plan.evidence" in states["plan"].reason
    decision = transit.next_stage(spec, journal_events, NOW)
    assert decision.pair == [("plan", 2)]


def test_replan_over_the_limit_fails_with_contract(tmp_path: Path) -> None:
    """Сверх `charter.max_rounds_replan` — `failed(contract)`, а не новый раунд."""
    spec = seed_spec(tmp_path)  # max_rounds_replan: 1
    journal_events = events(
        ("attempt_started", {"stage": "plan", "attempt": 1, "run_id": "plan#1"}),
        ("attempt_ended", ended("plan")),
        ("plan_proposed", {"rev": 1, "stages": [plan_stage("research")], "reason": "", "source_stage": "plan", "attempt": 1}),
        ("plan_rejected", {"rev": 1, "why": "первый отказ", "issues": []}),
        ("attempt_started", {"stage": "plan", "attempt": 2, "run_id": "plan#2"}),
        ("attempt_ended", ended("plan", 2)),
        ("plan_proposed", {"rev": 2, "stages": [plan_stage("research")], "reason": "", "source_stage": "plan", "attempt": 2}),
        ("plan_rejected", {"rev": 2, "why": "второй отказ", "issues": []}),
    )

    states = transit.stage_states(spec, journal_events)
    assert states["plan"].state == "failed"
    assert states["plan"].fail_kind == "contract"
    decision = transit.next_stage(spec, journal_events, NOW)
    assert (decision.kind, decision.fail_kind) == ("failed", "contract")


# ------------------------------------------------- 4–5. правила хартии в отчёте


def report(tmp_path: Path, *, stages: Any, seed: Any = None, **kwargs: Any) -> tuple[list[dict[str, Any]], str]:
    """`runner.report_plan` от имени этапа плана: факты и причина отказа."""
    spec = seed or seed_spec(tmp_path)
    stage = spec.stage("plan")
    assert stage is not None
    result: dict[str, Any] = {} if stages is None else {"plan": {"stages": stages, "reason": "скоуп"}}
    return runner.report_plan({"id": DEAL}, spec, stage, result, 1, events=list(kwargs.get("events") or ()), now=NOW)


def test_plan_over_max_stages_is_rejected(tmp_path: Path) -> None:
    """Потолок `max_stages` держит граф: предложение сверх него отклоняется.

    seed (2) плюс три этапа — пять; хартия держит шесть. Два этапа сверх потолка
    в отдельной спеке проверяются тем же правилом, что и `spec check`.
    """
    tight = write_spec(
        tmp_path,
        SEED.replace("max_stages: 6", "max_stages: 3"),
        name="tight.yaml",
    )
    stages = proposal(plan_stage("research"), plan_stage("impl"))
    facts, why = report(tmp_path, stages=stages, seed=tight)

    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_rejected"]
    assert "charter.max_stages" in " ".join(facts[1]["payload"]["issues"])
    assert tuple(stage.id for stage in transit.graph(tight, facts)) == ("plan", "close")


def test_plan_cannot_add_publish(tmp_path: Path) -> None:
    """Публикацию план не добавляет: она объявляется в seed-спеке и требует ревью."""
    stage = plan_stage("publish")
    stage["role"] = "publisher"
    facts, why = report(tmp_path, stages=proposal(stage))

    assert why == ""
    assert facts[-1]["kind"] == "plan_rejected"
    assert "plan.publish" in " ".join(facts[-1]["payload"]["issues"])


def test_plan_stage_without_evidence_is_rejected(tmp_path: Path) -> None:
    """Этап, который что-то производит, без `evidence` планом не добавляется."""
    bare = {"id": "research", "role": "implementer", "needs": ["plan"]}
    facts, why = report(tmp_path, stages=[bare])

    assert why == ""
    assert facts[-1]["kind"] == "plan_rejected"
    assert "plan.evidence" in " ".join(facts[-1]["payload"]["issues"])


def test_plan_stage_reporting_without_a_plan_is_contract(tmp_path: Path) -> None:
    """Этап плана без `plan` в отчёте — отказ `contract`, а не тихое «готово»."""
    facts, why = report(tmp_path, stages=None)

    assert facts == []
    assert "plan: {stages, reason}" in why


def test_engine_accepts_plan_below_the_gate(tmp_path: Path) -> None:
    """Порог не пройден — движок принимает план сам и граф растёт одним фактом."""
    spec = write_spec(tmp_path, SEED.replace("min_stages: 4", "min_stages: 0"))
    facts, why = report(tmp_path, stages=proposal(plan_stage("research")), seed=spec)

    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed", "plan_accepted"]
    assert facts[1]["payload"]["by"] == "engine"
    assert "research" in tuple(stage.id for stage in transit.graph(spec, facts))


# ------------------------------------------------------------------ 6. гейт плана


def test_plan_gate_asks_human_and_accepts_by_human(tmp_path: Path) -> None:
    """План больше порога: вопрос человеку, `wait_human`, «принять» → граф.

    Порог `plan_gate.min_stages` считается по всему графу: два этапа плана поверх
    двух seed-этапов — четыре, то есть ровно больше трёх.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED.replace("min_stages: 4", "min_stages: 3"), name="gate.yaml")
    conn = open_store(home)
    deal = make_deal(conn, home, spec)
    stages = proposal(plan_stage("research"), plan_stage("impl"))
    facts, why = report(tmp_path, stages=stages, seed=spec)
    assert why == ""
    assert [fact["kind"] for fact in facts] == ["plan_proposed"]
    assert facts[0]["payload"]["rev"] == 1
    facts[0]["deal_id"] = deal["id"]
    for fact in facts:
        fact["idempotency_key"] = deals.event_key("engine", deal["id"], fact["kind"], {"rev": 1, "stage": "plan"})
    journal.append(conn, facts)

    events_in_journal = journal.read_events(conn, str(deal["id"]))
    decision = transit.next_stage(spec, events_in_journal, NOW)
    assert (decision.kind, decision.what) == ("wait", "plan")
    assert transit.deal_state(spec, events_in_journal) == "wait_gate"

    first = runner.plan_gate(conn, spec, deal, events=events_in_journal, home=home, clock=lambda: NOW)
    assert first["asked"] is True
    question_id = first["question_id"]
    assert question_id
    rows = pool_rows(home)
    assert len(rows) == 1
    assert "принять" in str(rows[0]["options_json"])
    assert rows[0]["session_key"] == ask_module.session_key(str(deal["id"]))

    events_in_journal = journal.read_events(conn, str(deal["id"]))
    assert transit.next_stage(spec, events_in_journal, NOW).what == "plan"
    assert transit.deal_state(spec, events_in_journal) == "wait_human"

    # Строка шага прогона: гейт плана читается как ожидание ответа, а не как
    # «попытка не поднята».
    step = driver._plan_step(conn, deal, spec, home, float(NOW))
    assert step is not None
    assert step["plan"]["asked"] is True and step["plan"]["rev"] == 1
    assert "ждёт ответа" in cli.render_run_step(step)

    # Второй вызов вопроса не удваивает: ждёт того же ответа.
    again = runner.plan_gate(conn, spec, deal, events=events_in_journal, home=home, clock=lambda: NOW)
    assert (again["asked"], again["question_id"], again["answered"]) == (False, question_id, False)
    assert len(pool_rows(home)) == 1

    answer_pool(home, question_id, "принять")
    assert ask_module.sync_answers(conn, home)["appended"] == 1
    events_in_journal = journal.read_events(conn, str(deal["id"]))
    accepted_row = runner.plan_gate(
        conn, spec, deal, events=events_in_journal, home=home, clock=clock_later(300)
    )
    assert (accepted_row["answered"], accepted_row["accepted"], accepted_row["rejected"]) == (True, True, False)

    events_in_journal = journal.read_events(conn, str(deal["id"]))
    accepted_facts = [event for event in events_in_journal if event["kind"] == "plan_accepted"]
    assert len(accepted_facts) == 1
    assert journal.payload_of(accepted_facts[0])["by"] == "human"
    assert "research" in tuple(stage.id for stage in transit.graph(spec, events_in_journal))
    assert transit.next_stage(spec, events_in_journal, NOW).pair == [("research", 1)]


def test_plan_gate_rejects_on_another_answer(tmp_path: Path) -> None:
    """«Урезать» — отказ с причиной: этап плана возвращается в работу, граф прежний."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED.replace("min_stages: 4", "min_stages: 3"), name="gate.yaml")
    conn = open_store(home)
    deal = make_deal(conn, home, spec)
    stages = proposal(plan_stage("research"), plan_stage("impl"))
    facts, _why = report(tmp_path, stages=stages, seed=spec)
    facts[0]["deal_id"] = deal["id"]
    facts[0]["idempotency_key"] = deals.event_key("engine", deal["id"], "plan_proposed", {"rev": 1})
    journal.append(conn, facts)

    events_in_journal = journal.read_events(conn, str(deal["id"]))
    question_id = runner.plan_gate(conn, spec, deal, events=events_in_journal, home=home, clock=lambda: NOW)["question_id"]
    answer_pool(home, question_id, "урезать")
    ask_module.sync_answers(conn, home)
    events_in_journal = journal.read_events(conn, str(deal["id"]))
    row = runner.plan_gate(conn, spec, deal, events=events_in_journal, home=home, clock=clock_later(300))

    assert (row["answered"], row["accepted"], row["rejected"]) == (True, False, True)
    events_in_journal = journal.read_events(conn, str(deal["id"]))
    rejected = [event for event in events_in_journal if event["kind"] == "plan_rejected"]
    assert len(rejected) == 1
    assert "урезать" in journal.payload_of(rejected[0])["why"]
    states = transit.stage_states(spec, events_in_journal)
    assert states["plan"].state == "pending", states["plan"]
    assert states["plan"].rounds == 1
    assert tuple(stage.id for stage in transit.graph(spec, events_in_journal)) == ("plan", "close")


# ------------------------------------------------------- 8. закрытие по графу


def test_close_uses_the_journal_graph(tmp_path: Path) -> None:
    """`close_stages_done` получает граф журнала: добавленный этап в списке есть."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED.replace("min_stages: 4", "min_stages: 0"), name="close.yaml")
    conn = open_store(home)
    deal = make_deal(conn, home, spec)
    stages = proposal(plan_stage("research"))
    facts, _why = report(tmp_path, stages=stages, seed=spec)
    for fact in facts:
        fact["deal_id"] = deal["id"]
        fact["idempotency_key"] = deals.event_key("engine", deal["id"], fact["kind"], {"rev": 1})
    journal.append(conn, facts)
    # Все этапы графа, включая добавленный планом, закрыты своими фактами, и
    # закрывающий этап вынес приговор — иначе дело не закрывается (1.0).
    journal.append(
        conn,
        [
            {
                "ts": NOW,
                "recorded_at": NOW,
                "deal_id": deal["id"],
                "kind": "attempt_ended",
                "payload": ended(stage_id),
                "source": "engine",
                "idempotency_key": deals.event_key("engine", deal["id"], "attempt_ended", {"stage": stage_id}),
            }
            for stage_id in ("plan", "close", "research")
        ]
        + [
            {
                "ts": NOW,
                "recorded_at": NOW,
                "deal_id": deal["id"],
                "kind": "collected",
                "payload": {"stage": "close", "target": "next", "reason": "итог дела", "attempt": 1},
                "source": "engine",
                "idempotency_key": deals.event_key("engine", deal["id"], "collected", {"stage": "close"}),
            }
        ],
    )

    events_in_journal = journal.read_events(conn, str(deal["id"]))
    assert transit.next_stage(spec, events_in_journal, NOW).kind == "done"

    closed = driver._closure(conn, deal, spec, float(NOW))
    assert closed["written"] is True
    assert "research" in closed["stages"]
    payload = journal.closed_fact(journal.read_events(conn, str(deal["id"])))["payload"]
    assert "research" in payload["stages"]


# ------------------------------------------------------- срез для поверхностей


def test_snapshot_shows_the_plan_stage_without_the_file(tmp_path: Path) -> None:
    """Экран видит этап, которого нет в файле: срез строится по графу журнала."""
    spec = write_spec(tmp_path, SEED.replace("min_stages: 4", "min_stages: 0"), name="screen.yaml")
    stages = proposal(plan_stage("research"))
    facts, _why = report(tmp_path, stages=stages, seed=spec)
    journal_events = [
        {"id": 1, "ts": NOW, "deal_id": DEAL, "kind": "created", "payload": {"spec": str(spec.path)}, "source": "cli"},
        *[
            {
                "id": index + 2,
                "ts": NOW,
                "deal_id": DEAL,
                "kind": fact["kind"],
                "payload": fact["payload"],
                "source": "engine",
            }
            for index, fact in enumerate(facts)
        ],
    ]

    snapshot = stagefeed.snapshot(journal_events, NOW, spec_path=str(spec.path))
    assert snapshot["note"] == ""
    assert [row["id"] for row in snapshot["stages"]] == ["plan", "close", "research"]
    rows = {row["id"]: row for row in snapshot["stages"]}
    assert rows["research"]["role"] == "implementer"
    assert rows["research"]["state"] in ("pending", "wait_gate")
    assert "research" not in (spec.path.read_text(encoding="utf-8"))


def test_plan_stage_grows_the_graph_through_the_engine(tmp_path: Path) -> None:
    """Сквозной прогон: план-этап отчитывается, движок пишет граф, этап плана идёт.

    Проверяются три стыка, которые по отдельности ничего не доказывают: отчёт
    этапа превращается в `plan_proposed`+`plan_accepted` той же транзакцией,
    добавленный этап движок находит в графе журнала (файл его не знает), а
    закрытие получает граф журнала.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, SEED.replace("min_stages: 4", "min_stages: 0"), name="e2e.yaml")
    deal = make_deal(conn, home, spec)
    stages = proposal(plan_stage("research"))

    planned = runner.run_stage(
        conn,
        spec,
        deal,
        "plan",
        1,
        home=home,
        spawn=Fake(result={"status": "done", "plan": {"stages": stages, "reason": "скоуп"}, "summary": "план"}, pack=True),
        clock=lambda: NOW,
    )
    assert planned["outcome"] == "done", (planned.get("kind"), planned.get("reason"))
    kinds = [str(event["kind"]) for event in journal.read_events(conn, str(deal["id"]))]
    assert "plan_proposed" in kinds and "plan_accepted" in kinds

    done = runner.run_stage(
        conn,
        spec,
        deal,
        "research",
        1,
        home=home,
        spawn=Fake(result={"status": "done", "summary": "исследовано"}, pack=True),
        clock=lambda: NOW,
    )
    assert done["outcome"] == "done", (done.get("kind"), done.get("reason"))

    events_in_journal = journal.read_events(conn, str(deal["id"]))
    states = transit.stage_states(spec, events_in_journal)
    assert states["research"].state == "done"
    # `close` ждал план и то, что план добавил: работа research закрыла его ребро.
    assert states["close"].state == "pending"
    assert transit.next_stage(spec, events_in_journal, NOW).pair == [("close", 1)]

    finished = runner.run_stage(
        conn,
        spec,
        deal,
        "close",
        1,
        home=home,
        spawn=Fake(
            result={
                "status": "done",
                "summary": "закрыто",
                "verdict": {"action": "approve", "target": "next"},
            },
            pack=True,
        ),
        clock=lambda: NOW,
    )
    assert finished["outcome"] == "done", (finished.get("kind"), finished.get("reason"))

    closed = driver._closure(conn, deal, spec, float(NOW))
    assert closed["written"] is True
    assert closed["cause"] == deals.CLOSE_CAUSE_LEAD
    assert closed["stages"] == ["plan", "close", "research"]


def test_a_rejected_plan_is_not_erased_by_the_attempt_that_proposed_it(tmp_path: Path) -> None:
    """Отказ плана переживает конец собственной попытки — и этап идёт на реплан.

    Живой кейс 10.11 (`d_1cae95b3`): этап плана предложил граф с двумя shell-этапами
    без `cmd`, валидатор отказал (`plan_rejected`, два `issues`), и **в тот же
    пакет** лёг `attempt_ended(done)` — процесс-то закончился успешно. Канонический
    порядок журнала ставил факты плана раньше конца попытки, поэтому `done` ложился
    поверх `pending`: дело закрылось «все этапы пройдены» через 39 секунд, ни один
    предложенный этап не запустился, а хартия разрешала ещё два реплана.

    Здесь проверяется следствие, а не порядок сам по себе: после отказа этап плана
    снова `pending` с раундом 1, и следующий шаг движка — попытка `plan#2`.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, SEED.replace("min_stages: 4", "min_stages: 0").replace("max_rounds_replan: 1", "max_rounds_replan: 2"), name="replan.yaml")
    deal = make_deal(conn, home, spec)
    # Ровно то, что вернул живой этап: shell-этап без команды — отказ `kind.cmd`.
    broken = [{"id": "check", "kind": "shell", "needs": ["plan"]}]
    planned = runner.run_stage(
        conn,
        spec,
        deal,
        "plan",
        1,
        home=home,
        spawn=Fake(result={"status": "done", "plan": {"stages": broken, "reason": "проверка"}, "summary": "план"}, pack=True),
        clock=lambda: NOW,
    )
    assert planned["outcome"] == "done", (planned.get("kind"), planned.get("reason"))

    events_in_journal = journal.read_events(conn, str(deal["id"]))
    kinds = [str(event["kind"]) for event in events_in_journal]
    assert kinds.index("plan_rejected") > kinds.index("attempt_ended"), kinds
    states = transit.stage_states(spec, events_in_journal)
    assert states["plan"].state == "pending", states["plan"]
    assert states["plan"].rounds == 1
    nxt = transit.next_stage(spec, events_in_journal, NOW)
    assert nxt.pair == [("plan", 2)], nxt.pair
    # Граф не вырос: предложение не стало планом ни одним этапом.
    assert tuple(stage.id for stage in transit.graph(spec, events_in_journal)) == ("plan", "close")


def test_driver_waits_for_the_plan_gate(tmp_path: Path) -> None:
    """Цикл движка на открытом гейте плана: вопрос, ожидание, дело не тронуто.

    Гейт плана обрабатывается тем же циклом, что и вопрос этапа: шаг ставит
    вопрос (или видит уже поставленный) и выходит ожиданием — процессов не
    поднимается, а дело остаётся `wait_human`.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED.replace("min_stages: 4", "min_stages: 3"), name="wait.yaml")
    conn = open_store(home)
    deal = make_deal(conn, home, spec)
    facts, _why = report(tmp_path, stages=proposal(plan_stage("research"), plan_stage("impl")), seed=spec)
    facts[0]["deal_id"] = deal["id"]
    facts[0]["idempotency_key"] = deals.event_key("engine", deal["id"], "plan_proposed", {"rev": 1})
    journal.append(conn, facts)

    run = driver.run(conn, deal, spec=spec, home=home, max_steps=3, clock=lambda: NOW)
    assert run["outcome"] == "wait"
    assert [step["plan"]["rev"] for step in run["steps"]] == [1]
    assert run["steps"][0]["plan"]["asked"] is True
    assert "ждёт ответа" in cli.render_run_step(run["steps"][0])
    assert len(pool_rows(home)) == 1
    assert transit.deal_state(spec, journal.read_events(conn, str(deal["id"]))) == "wait_human"


def test_plan_events_are_journal_facts(tmp_path: Path) -> None:
    """Виды плана — закрытый набор журнала, и предложение состояние не меняет."""
    assert {"plan_proposed", "plan_accepted", "plan_rejected", "plan_gate"} <= set(journal.EVENT_KINDS)
    assert "plan_proposed" in journal.NON_STATE_KINDS
    assert "plan_accepted" not in journal.NON_STATE_KINDS
    assert "plan_rejected" not in journal.NON_STATE_KINDS
    assert "plan_gate" not in journal.NON_STATE_KINDS
    order = journal.KIND_ORDER
    # Предложение — часть суда над попыткой, а не её пролог: применяется после
    # `attempt_ended`, иначе отказ плана тонет в `done` той же попытки.
    assert order.index("plan_proposed") > order.index("attempt_ended")
    assert order.index("plan_proposed") < order.index("plan_accepted")
