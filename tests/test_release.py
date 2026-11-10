"""Дело не висит `running`, когда движка уже нет: проход отпускания.

Ни один тест не поднимает процесс: отпускание — проход по store и журналу, и
проверяется то, что он **делает**: какой факт появляется, что он ставит строке,
чего он не трогает и сходятся ли после него поверхности.

Фикстура главного класса — живой журнал `d_eb4ad2bb` (07.11): `created` →
`attempt_started` → `attempt_ended failed/infra` (таймаут попытки) → ничего.
Строка при этом `running`, решения продолжать — в журнале, а писателя нет.

Правила карточки 0.0.8 — по одному тесту на каждое:

1. фикстура `d_eb4ad2bb` отпускается один раз: `released`, строка `ready`,
   повтор ничего не пишет и не двигает `updated_at`;
2. дело, которое держит человек (`blocked`, `review`, `archived`), не трогается;
3. дело, взятое человеком (`claimed`), не отпускается: это его намерение;
4. отказ движка без начала попытки (`details.refused`) блокируется классом
   отказа, а не паркуется и не отпускается;
5. живая попытка и живой лизинг не трогаются — иначе фикс закрывал бы работу,
   которая ещё идёт;
6. свежий факт ждёт паузу `RELEASE_GRACE_SEC`;
7. исчерпанные попытки паркуются (`runner.park_exhausted`), а не отпускаются;
8. все этапы пройдены — дело закрывается `completed`, а не отпускается;
9. дело в очереди (`ready`) проход не трогает: отпускать нечего;
10. `bin/duty reap` закрывает осиротевшую попытку и отпускает дело; `--dry-run`
    не пишет;
11. прогон отпускает своё дело и на `--once`, и на пределе шагов, и продолжение
    после отпускания не блокируется;
12. две поверхности называют отпускание: `/now` — `wait_slot`, `duty deal` —
    строку с причиной.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]

# Метка времени всех фактов: неподвижные часы делают идемпотентность и паузу
# проверяемыми числами, а не «примерно через две минуты».
NOW = 1_790_000_000

# Пауза отпускания (`release.RELEASE_GRACE_SEC`): два порога `reaper.GRACE_MIN_SEC`.
GRACE = 120

# TTL лизинга по умолчанию (`runner.DEFAULT_TTL_MIN = 30`).
TTL_DEFAULT = 1800


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути — под тем же именем, что и у остального кода."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release = load_module("hermes_duty_release", "release.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
spec_module = load_module("hermes_duty_spec", "spec.py")
transit = load_module("hermes_duty_transit", "transit.py")
runner = load_module("hermes_duty_runner", "runner.py")
reaper = load_module("hermes_duty_reaper", "reaper.py")
driver = load_module("hermes_duty_driver", "driver.py")
cli = load_module("hermes_duty_cli", "cli.py")

# Схема пула imessage-gateway — та, в которую пишет `ask()`: парковка без неё
# не записалась бы, а нам нужен факт парковки, а не вопрос.
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

# Цепочка из двух этапов: `impl` — тот, на котором попытка закончилась, `verify` —
# доказательство того, что отпущенное дело поехало дальше. Дерево выключено явно:
# отпускание проверяется без git.
CHAIN = """
duty: probe
stages:
  - id: impl
    role: implementer
  - id: verify
    role: tester
    needs: [impl]
    workspace: none
"""

# Та же цепочка, но `infra` не повторяется: так проверяется класс (б) — попытки
# исчерпаны, парковки в журнале нет.
NO_RETRY = """
duty: probe
stages:
  - id: impl
    role: implementer
    retry: {max: 0, on: [infra]}
  - id: verify
    role: tester
    needs: [impl]
    workspace: none
"""


# Спека с этапом плана: лид растит граф из журнала, в файле этапов плана нет.
PLAN_CHAIN = """
duty: probe
stages:
  - id: plan
    role: lead
    workspace: none
  - id: close
    role: lead
    needs: [plan]
    workspace: none
"""


# ------------------------------------------------------------------ фикстуры


def make_home(tmp_path: Path, name: str = "home") -> Path:
    """Корень Hermes: своя `duty/duty.db` (создаст store) и свой пул вопросов."""
    home = tmp_path / name
    (home / "messaging").mkdir(parents=True)
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    pool.executescript(POOL_SCHEMA)
    pool.commit()
    pool.close()
    return home


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def write_spec(tmp_path: Path, text: str, name: str = "duty.yaml") -> Any:
    """Спека на диске и загруженная: путь абсолютный — так её и читает движок."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def make_deal(conn: sqlite3.Connection, *, spec_path: Optional[Path] = None) -> dict[str, Any]:
    """Своё дело в store; спека — в `created.payload.spec`, как у `duty create --spec`."""
    created = deals.create_deal(
        conn,
        repo="duty",
        title="Дело, брошенное движком",
        body="",
        spec_path=str(spec_path) if spec_path is not None else None,
        now=NOW,
    )
    return deals.deal_row(conn, str(created["deal"]["id"]))


def seed(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    kind: str,
    payload: dict[str, Any],
    *,
    key: str,
    ts: int = NOW,
) -> None:
    """Факт в журнал руками: предыстория, которую проход обязан прочитать."""
    journal.append(
        conn,
        [
            {
                "ts": ts,
                "recorded_at": ts,
                "deal_id": str(deal["id"]),
                "kind": kind,
                "payload": payload,
                "source": "test",
                "idempotency_key": key,
            }
        ],
    )


def mark_running(conn: sqlite3.Connection, deal: dict[str, Any], now: int = NOW) -> None:
    """Строка `running` — так её ставит движок, беря лизинг (`deals._set_status`).

    Событие `attempt_started` строку не двигает: её двигает сам движок перед
    спавном. Фикстура обязана выглядеть ровно так, как живой store после ушедшего
    прогона, иначе проверялось бы «проход по чужим строкам».
    """
    with conn:
        deals._set_status(conn, deal, "running", now)


def start_attempt(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    *,
    stage: str = "impl",
    attempt: int = 1,
    pid: Optional[int] = 61524,
    started_at: int = NOW,
    lease_expires: Optional[int] = None,
) -> str:
    """`attempt_started` — начало попытки."""
    run_id = f"{stage}#{attempt}"
    seed(
        conn,
        deal,
        "attempt_started",
        {
            "run_id": run_id,
            "stage": stage,
            "attempt": attempt,
            "pid": pid,
            "lease_expires": started_at + TTL_DEFAULT if lease_expires is None else lease_expires,
            "workdir": f"/tmp/{deal['id']}-{stage}-{attempt}",
        },
        key=f"test:{deal['id']}:start:{run_id}",
        ts=started_at,
    )
    return run_id


def end_attempt(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    *,
    stage: str = "impl",
    attempt: int = 1,
    outcome: str = "failed",
    kind: Optional[str] = "infra",
    reason: str = "",
    details: Optional[dict[str, Any]] = None,
    ended_at: int = NOW,
) -> None:
    """`attempt_ended` — конец попытки; ровно та форма, которую пишет `runner`."""
    payload: dict[str, Any] = {
        "run_id": f"{stage}#{attempt}",
        "stage": stage,
        "attempt": attempt,
        "outcome": outcome,
        "kind": kind,
        "reason": reason,
    }
    if details is not None:
        payload["details"] = details
    seed(
        conn,
        deal,
        "attempt_ended",
        payload,
        key=f"test:{deal['id']}:end:{stage}#{attempt}",
        ts=ended_at,
    )


def take_lease(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    *,
    stage: str = "impl",
    attempt: int = 1,
    pid: int = os.getpid(),
    expires_at: int = NOW + TTL_DEFAULT,
) -> None:
    """Строка лизинга — та же таблица, что ведёт движок (`runner.renew` её двигает)."""
    with conn:
        conn.execute(
            "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(deal_id, stage) DO UPDATE SET attempt = excluded.attempt, "
            "pid = excluded.pid, taken_at = excluded.taken_at, expires_at = excluded.expires_at",
            (str(deal["id"]), stage, int(attempt), int(pid), NOW, int(expires_at)),
        )


def events(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    return journal.read_events(conn, str(deal["id"]))


def kinds(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[str]:
    return [str(event["kind"]) for event in events(conn, deal)]


def released_facts(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Факты отпускания дела — в порядке журнала."""
    return [event for event in events(conn, deal) if str(event["kind"]) == "released"]


def row_of(conn: sqlite3.Connection, deal: dict[str, Any]) -> dict[str, Any]:
    row = deals.deal_row(conn, str(deal["id"]))
    assert row is not None
    return row


def orphan_fixture(
    conn: sqlite3.Connection, spec: Any
) -> dict[str, Any]:
    """Журнал `d_eb4ad2bb`: создание, попытка, её таймаут — и больше ничего.

    Ровно те три факта, что остались от живого дела 07.11, и та же причина: попытка
    не уложилась в бюджет времени. Строка `running`: её поставил движок, начиная
    попытку, и вернуть её стало некому.
    """
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    end_attempt(
        conn,
        deal,
        reason="таймаут попытки: процесс не уложился в бюджет времени (5400 с)",
        details={"timed_out": True, "exit_code": -15},
    )
    mark_running(conn, deal)
    return deal


# --------------------------------- 1. фикстура `d_eb4ad2bb`: один `released`


def test_fixture_d_eb4ad2bb_is_released_once(tmp_path: Path) -> None:
    """Дело отпущено один раз: факт, строка `ready`, повтор — `events: 0`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = orphan_fixture(conn, spec)

    # До фикса: решение в журнале есть, а писателя нет.
    assert row_of(conn, deal)["status"] == "running"
    assert journal.fold(events(conn, deal))["reason"] == "no_live_fact"
    assert transit.next_stage(spec, events(conn, deal), NOW + 300).kind == transit.RUN

    rows = release.sweep(conn, NOW + 300, home=home)

    assert len(rows) == 1
    row = rows[0]
    assert row["deal"] == deal["id"]
    assert row["action"] == "released"
    assert row["from"] == "running"
    assert row["to"] == "ready"
    assert row["decision"] == "run"
    assert row["written"] == 1

    facts = released_facts(conn, deal)
    assert len(facts) == 1
    payload = journal.payload_of(facts[0])
    assert payload["run_id"] == "impl#1"
    assert payload["stage"] == "impl"
    assert payload["attempt"] == 1
    assert payload["from"] == "running"
    assert payload["to"] == "ready"
    assert payload["decision"] == "run"
    assert payload["wait"] is None
    # Сколько журнал молчал до отпускания — числом, а не словом «давно».
    assert payload["idle_sec"] == 300
    assert payload["reason"] == (
        "движок отпустил дело: попытка impl#1 закончилась failed/infra, "
        "продолжения не было"
    )
    # Источник — движок: отпустил он, и это видно в журнале, а не только в строке.
    assert facts[0]["source"] == "engine"

    store = row_of(conn, deal)
    assert store["status"] == "ready"
    assert store["updated_at"] == NOW + 300
    # Дело не закрыто: отпускание вернуло его в очередь, а не объявило сделанным.
    assert journal.fold(events(conn, deal))["active"] is True
    assert journal.fold(events(conn, deal))["reason"] == "no_live_fact"

    # Ключ события — от последнего state-changing факта до отпускания, а не от
    # решения `transit`: иначе между двумя проходами (`wait(retry)` → `run`) тот же
    # отпуск стал бы вторым фактом.
    ended = next(event for event in events(conn, deal) if str(event["kind"]) == "attempt_ended")
    assert facts[0]["idempotency_key"] == deals.event_key(
        "engine", str(deal["id"]), "released", {"after": int(ended["id"])}
    )

    # Повтор: строка уже `ready` — проход её не видит.
    assert release.sweep(conn, NOW + 400, home=home) == []
    assert len(released_facts(conn, deal)) == 1
    assert row_of(conn, deal)["updated_at"] == NOW + 300

    # Худший случай — строку вернул в `running` чужой писатель: второй факт не
    # ляжет, потому что ключ тот же, и `updated_at` не сдвинется.
    mark_running(conn, deal, now=NOW + 500)
    again = release.sweep(conn, NOW + 600, home=home)
    assert len(again) == 1 and again[0]["written"] == 0
    assert len(released_facts(conn, deal)) == 1
    assert row_of(conn, deal)["updated_at"] == NOW + 500


def test_release_reads_plan_stages_the_file_does_not_know(tmp_path: Path) -> None:
    """Этап из принятого плана (в файле спеки его нет) — не «не найден в спеке».

    Кейс d_c8551c18: attempt_ended по этапу `review` из плана ревизии 1; проход
    отпускания искал этап только в файле, отвечал «этап не найден в спеке дела» —
    и дело навсегда оставалось `running` без живого факта. Этап ищется и в графе
    журнала: движение по факту, а не по догадке.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, PLAN_CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    # План принят: в графе появился этап `review`, в файле его нет.
    seed(conn, deal, "attempt_started", {"stage": "plan", "attempt": 1, "run_id": "plan#1", "pid": 1}, key="t:plan:start")
    seed(conn, deal, "attempt_ended", {"stage": "plan", "attempt": 1, "outcome": "done", "kind": None, "reason": "", "run_id": "plan#1"}, key="t:plan:end")
    seed(conn, deal, "plan_proposed", {"rev": 1, "stages": [{"id": "review", "role": "tester", "needs": ["plan"], "workspace": "none", "evidence": [{"cmd": "true", "must": 0}]}], "reason": "", "source_stage": "plan", "attempt": 1}, key="t:propose")
    seed(conn, deal, "plan_accepted", {"rev": 1, "by": "engine"}, key="t:accept")
    end_attempt(conn, deal, stage="review", attempt=1, outcome="failed", kind="contract", reason="evidence.missing")
    mark_running(conn, deal)
    assert transit.next_stage(spec, events(conn, deal), NOW + 300).kind == transit.DECISION_FAILED

    rows = release.sweep(conn, NOW + 300, home=home)

    assert len(rows) == 1
    row = rows[0]
    assert row.get("error") is None
    assert row["action"] == "rethink"
    assert row["written"] == 1
    rethinks = [event for event in events(conn, deal) if str(event["kind"]) == "plan_rethink"]
    assert len(rethinks) == 1
    assert journal.payload_of(rethinks[0])["failed_stage"] == "review"
    # Дело сразу в очереди: `running` без лизинга диспетчер не поднимает, а
    # второй проход отпуска не существует в живом цикле — `sweep` зовёт только
    # рантайм своего дела. Отпускание и запись факта — один проход.
    assert row_of(conn, deal)["status"] == "ready"
    second = release.sweep(conn, NOW + 600, home=home)
    assert second == [], "дело уже в очереди — второму проходу нечего отпускать"


# ------------------------------- 2. дело, которое держит человек, не трогается

def test_release_does_not_touch_deal_held_by_human(tmp_path: Path) -> None:
    """`blocked`, `review`, `archived` — ни факта, ни строки: решает человек."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)

    held: dict[str, dict[str, Any]] = {}
    for name in ("blocked", "review", "archived"):
        deal = orphan_fixture(conn, spec)
        if name == "blocked":
            deals.set_blocked(conn, str(deal["id"]), kind="needs_input", reason="нужен ввод", now=NOW + 10)
        elif name == "review":
            deals.request_review(conn, str(deal["id"]), reviewer="max", now=NOW + 10)
        else:
            deals.patch_deal(conn, str(deal["id"]), status="archived", now=NOW + 10)
        held[name] = deal
        assert row_of(conn, deal)["status"] == name

    assert release.sweep(conn, NOW + 300, home=home) == []

    for name, deal in held.items():
        assert released_facts(conn, deal) == [], name
        assert row_of(conn, deal)["status"] == name
        # Строку не двигали и после: `updated_at` остался моментом решения человека.
        assert row_of(conn, deal)["updated_at"] == NOW + 10


# ------------------------------- 3. дело, взятое человеком (`claimed`)


def test_claimed_deal_is_not_released(tmp_path: Path) -> None:
    """`claimed` — факт человека: отпускание стёрло бы намерение «я в работе»."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    deals.claim_deal(conn, str(deal["id"]), owner="max", source="cli", now=NOW)
    assert row_of(conn, deal)["status"] == "running"
    # Решение журнала при этом «продолжать» — соблазн отпустить есть.
    assert transit.next_stage(spec, events(conn, deal), NOW + 300).kind == transit.RUN

    assert release.sweep(conn, NOW + 300, home=home) == []

    assert released_facts(conn, deal) == []
    assert row_of(conn, deal)["status"] == "running"


# ------------------- 4. отказ движка без начала попытки: блок, а не парковка


def test_refused_attempt_is_blocked_not_parked(tmp_path: Path) -> None:
    """Класс (е): отказ без `attempt_started` блокируется классом отказа.

    Парковка здесь была бы враньём: «продолжать? continue|stop» спрашивают, когда
    продолжать есть куда, а при исчерпанном потолке ответ `continue` сжёг бы ещё
    столько же. `released` тоже не пишется: дело не отпущено продолжать.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    end_attempt(conn, deal, kind="infra", reason="процесс упал: код 1")
    # Отказ второй попытки: событие есть, начала попытки нет.
    end_attempt(
        conn,
        deal,
        attempt=2,
        outcome="failed",
        kind="capability",
        reason="бюджет дела исчерпан: 900/500 токенов",
        details={"refused": True, "budget": {"kind": "tokens", "used": 900, "limit": 500}},
    )
    mark_running(conn, deal)
    assert transit.next_stage(spec, events(conn, deal), NOW + 300).kind == transit.DECISION_FAILED

    rows = release.sweep(conn, NOW + 300, home=home)

    assert len(rows) == 1
    assert rows[0]["action"] == "blocked"
    assert rows[0]["to"] == "blocked"
    assert rows[0]["written"] == 1

    assert row_of(conn, deal)["status"] == "blocked"
    assert released_facts(conn, deal) == []
    blocks = [journal.payload_of(event) for event in events(conn, deal) if str(event["kind"]) == "blocked"]
    assert len(blocks) == 1
    assert blocks[0]["block_kind"] == "capability"
    assert blocks[0]["reason"] == "бюджет дела исчерпан: 900/500 токенов"
    assert blocks[0]["budget"] == {"kind": "tokens", "used": 900, "limit": 500}
    assert journal.parked_of(events(conn, deal)) is None, "парковки нет: продолжать нечего"
    assert journal.fold(events(conn, deal), now=NOW + 300)["state"] == "capability"

    # Повтор ничего не пишет: строка уже `blocked`.
    assert release.sweep(conn, NOW + 400, home=home) == []


# --------------------------- 5. живое дело не трогается: попытка и лизинг


def test_live_attempt_and_live_lease_are_not_touched(tmp_path: Path) -> None:
    """Открытая попытка — предмет `reaper`, живой лизинг — «этап ведёт движок»."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)

    # Открытая попытка: pid жив, конца нет. Пауза не спасает — факт старый.
    attempt_deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, attempt_deal, pid=os.getpid())
    take_lease(conn, attempt_deal, pid=os.getpid(), expires_at=NOW + 5000)
    mark_running(conn, attempt_deal)
    assert release.sweep(conn, NOW + 3000, home=home) == []
    assert reaper.sweep(conn, NOW + 3000) == [], "живая попытка — не осиротевшая"
    assert released_facts(conn, attempt_deal) == []
    assert row_of(conn, attempt_deal)["status"] == "running"

    # Лизинг без попытки (шаг между взятием лизинга и спавном): журнал молчит, но
    # этап за движком — трогать его отпусканием значит закрывать идущую работу.
    leased_deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, leased_deal, pid=os.getpid())
    end_attempt(conn, leased_deal, outcome="done", kind=None, reason="ok")
    take_lease(conn, leased_deal, pid=os.getpid(), expires_at=NOW + 5000)
    mark_running(conn, leased_deal)
    assert release.sweep(conn, NOW + 3000, home=home) == []
    assert released_facts(conn, leased_deal) == []
    assert row_of(conn, leased_deal)["status"] == "running"


# ------------------------------------- 6. свежий факт ждёт паузу


def test_fresh_fact_waits_for_grace(tmp_path: Path) -> None:
    """`attempt_ended` 10 с назад — не «движок ушёл»: проход ждёт паузы."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = orphan_fixture(conn, spec)

    assert release.sweep(conn, NOW + GRACE - 1, home=home) == []
    assert released_facts(conn, deal) == []

    # Свой прогон уходит сам — ему пауза не нужна.
    rows = release.sweep(conn, NOW + GRACE - 1, home=home, grace_sec=0)
    assert len(rows) == 1 and rows[0]["written"] == 1
    assert row_of(conn, deal)["status"] == "ready"


def test_grace_is_two_reaper_thresholds() -> None:
    """Пауза названа числом и взята у уборки: два `reaper.GRACE_MIN_SEC`."""
    assert GRACE == 2 * reaper.GRACE_MIN_SEC == release.RELEASE_GRACE_SEC


# ------------------------ 7. исчерпанные попытки паркуются, а не отпускаются


def test_exhausted_attempts_are_parked_not_released(tmp_path: Path) -> None:
    """Класс (б): `failed/capability` без парковки → `blocked(parked)` и вопрос."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, NO_RETRY)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    end_attempt(conn, deal, kind="capability", reason="нет доступа к репозиторию")
    mark_running(conn, deal)
    decision = transit.next_stage(spec, events(conn, deal), NOW + 300)
    assert decision.kind == transit.DECISION_FAILED

    rows = release.sweep(conn, NOW + 300, home=home)

    assert len(rows) == 1
    assert rows[0]["action"] == "parked"
    assert rows[0]["to"] == "blocked"
    assert row_of(conn, deal)["status"] == "blocked"
    assert released_facts(conn, deal) == []
    parked = journal.parked_of(events(conn, deal))
    assert parked is not None
    assert parked["kind"] == "capability"
    assert parked["stage"] == "impl"
    assert parked["question_id"], "парковка спрашивает человека continue|stop"

    # Повтор: строка `blocked` — проход её больше не видит.
    assert release.sweep(conn, NOW + 400, home=home) == []
    assert len([event for event in events(conn, deal) if str(event["kind"]) == "blocked"]) == 1


# ------------------------------- 8. все этапы пройдены — закрытие, не отпуск


def test_all_stages_done_closes_the_deal(tmp_path: Path) -> None:
    """Класс (д): журнал говорит `done` — дело закрывается `completed`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    for stage in ("impl", "verify"):
        start_attempt(conn, deal, stage=stage)
        end_attempt(conn, deal, stage=stage, outcome="done", kind=None, reason="ok")
    mark_running(conn, deal)
    assert transit.next_stage(spec, events(conn, deal), NOW + 300).kind == transit.DECISION_DONE

    rows = release.sweep(conn, NOW + 300, home=home)

    assert len(rows) == 1
    assert rows[0]["action"] == "closed"
    assert rows[0]["to"] == "done"
    assert row_of(conn, deal)["status"] == "done"
    assert released_facts(conn, deal) == []
    completed = [journal.payload_of(event) for event in events(conn, deal) if str(event["kind"]) == "completed"]
    assert len(completed) == 1
    assert completed[0]["cause"] == deals.CLOSE_CAUSE_ALL_STAGES
    assert completed[0]["stages"] == ["impl", "verify"]

    # Повтор: дело закрыто фактом — второго `completed` нет.
    assert release.sweep(conn, NOW + 400, home=home) == []
    assert kinds(conn, deal).count("completed") == 1


# --------------------------------- 9. дело в очереди проход не трогает


def test_ready_deal_is_left_alone(tmp_path: Path) -> None:
    """Класс (в): строка `ready` — отпускать нечего, решения ждёт прогон."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    assert row_of(conn, deal)["status"] == "ready"

    assert release.sweep(conn, NOW + 300, home=home) == []

    assert kinds(conn, deal) == ["created"]
    assert row_of(conn, deal)["status"] == "ready"


# --------------------- 10. `bin/duty reap`: уборка, затем отпускание


def test_reap_closes_orphan_attempt_then_releases_deal(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """`reap` закрывает осиротевшую попытку и отпускает дело; `--dry-run` не пишет."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal, started_at=NOW)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)
    mark_running(conn, deal)
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert cli.main(["reap", "--dry-run"]) == 0
    dry = capsys.readouterr().out
    assert "УБОРКА (--dry-run)" in dry
    check = open_store(home)
    assert released_facts(check, deal) == [], "--dry-run ничего не пишет"
    assert row_of(check, deal)["status"] == "running"
    check.close()

    assert cli.main(["reap"]) == 0
    real = capsys.readouterr().out
    assert "закрыто движком" in real, real
    assert "отпущено дел: 1" in real, real
    assert "отпущено движком" in real, real

    check = open_store(home)
    # Уборка шла первой: попытка закрыта `reaped`, и только потом отпущено дело.
    closed = [event for event in events(check, deal) if str(event["kind"]) == "attempt_ended"]
    assert len(closed) == 1 and journal.payload_of(closed[0])["reaped"] is True
    assert len(released_facts(check, deal)) == 1
    assert row_of(check, deal)["status"] == "ready"
    # Поведение самой уборки не изменилось: она пишет один факт и не трогает дело.
    assert reaper.sweep(check, NOW + 9000) == []
    check.close()


# ------------------- 11. прогон отпускает своё дело и не блокирует продолжение


class FakeProc:
    """Процесс попытки: тот минимум, что читает `runner.follow`."""

    def __init__(self, *, pid: int, exit_code: Optional[int]) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.stderr_text = ""

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        return None

    def finished(self) -> bool:
        return True

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.exit_code

    def stop(self, grace: float = 10) -> Optional[int]:
        return self.exit_code


class Fake:
    """Фейковый спавн: пишет отчёт с настоящим `sha256` артефакта."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], cwd: Path) -> FakeProc:
        contract = json.loads(Path(cmd[list(cmd).index("--query-file") + 1]).read_text(encoding="utf-8"))
        self.calls.append({"stage": str(contract["stage"]), "attempt": int(contract["attempt"])})
        notes = Path(cwd) / "notes.txt"
        notes.write_text("сделано\n", encoding="utf-8")
        target = Path(str(contract["report_to"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "status": "done",
                    "summary": "готово",
                    "artifacts": [
                        {"path": notes.name, "sha256": hashlib.sha256(notes.read_bytes()).hexdigest()}
                    ],
                    "evidence": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return FakeProc(pid=4200 + len(self.calls), exit_code=0)


def test_run_releases_its_own_deal_on_exit(tmp_path: Path) -> None:
    """`--once` и предел шагов оставляют строку `ready`: движок ушёл, дело живо."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = orphan_fixture(conn, spec)

    first = driver.run(conn, deal, spec=spec, home=home, once=True, clock=lambda: NOW + 300, spawn=Fake())

    assert first["outcome"] == "step"
    # Два отпуска, и оба — факты: первый перед шагом (брошенное дело), второй на
    # выходе прогона (своя попытка закончилась, продолжения `--once` не делает).
    assert first["released"] == 2
    assert first["release_errors"] == []
    assert {str(event["source"]) for event in released_facts(conn, deal)} == {"engine"}
    # Отпускание идёт до первого шага: решение по журналу читается уже по полному
    # журналу, и продолжение не блокируется.
    order = [str(event["kind"]) for event in events(conn, deal)]
    assert order.index("released") < order.index("attempt_started", order.index("released"))
    assert row_of(conn, deal)["status"] == "ready"

    # Предел шагов: попыток не поднято, но дело всё равно отпущено.
    second = orphan_fixture(conn, spec)
    limit = driver.run(conn, second, spec=spec, home=home, max_steps=0, clock=lambda: NOW + 300, spawn=Fake())
    assert limit["outcome"] == "limit"
    assert limit["released"] == 1
    assert row_of(conn, second)["status"] == "ready"


def test_released_deal_goes_on_to_completion(tmp_path: Path) -> None:
    """Отпущенное дело продолжается следующим прогоном и доходит до `completed`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = orphan_fixture(conn, spec)
    fake = Fake()

    report = driver.run(conn, deal, spec=spec, home=home, clock=lambda: NOW + 300, spawn=fake)

    assert report["outcome"] == "done"
    assert report["released"] == 1
    calls = [(call["stage"], call["attempt"]) for call in fake.calls]
    assert calls == [("impl", 2), ("verify", 1)]
    assert row_of(conn, deal)["status"] == "done"
    assert kinds(conn, deal).count("completed") == 1
    assert len(released_facts(conn, deal)) == 1, "второй отпуск не пишется"


# ------------------------- 12. поверхности называют отпускание


def test_now_and_deal_name_the_release(tmp_path: Path, monkeypatch: Any) -> None:
    """`/now` — `wait_slot` вместо `no_signal`, `duty deal` — строка с причиной."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = orphan_fixture(conn, spec)
    release.sweep(conn, NOW + 300, home=home)
    monkeypatch.setenv("HERMES_HOME", str(home))

    payload = cli.api().now()
    item = next(entry for entry in payload["deals"] if entry["id"] == deal["id"])
    assert item["waiting"]["state"] == "wait_slot"
    assert "идущей попытки нет" in item["waiting"]["evidence"]
    assert item["last_action"]["kind"] == "released"
    assert "движок отпустил дело" in item["last_action"]["summary"]

    deal_payload = cli.api().deal(str(deal["id"]))
    out = cli.render_deal(deal_payload, None, None)
    assert "последнее действие: released" in out, out
    assert "движок отпустил дело" in out, out
    # `fold` и `transit` не менялись: отпускание не сдвинуло решение графа.
    assert transit.next_stage(spec, events(conn, deal), NOW + 300).kind == transit.RUN
    assert transit.deal_state(spec, events(conn, deal)) == transit.PENDING
