"""Адрес вопроса — по объявлению, а не по догадке (W5).

Вопрос — «требование входа»: без ответа дальше работать нельзя. Поэтому у него
есть адрес, и адрес выбирает спека детерминированно (`duty-model.html`, «Вопросы:
кто отвечает и почему»): фактовые вопросы — ролям, властные — только человеку,
цепочка конечна (`max_hops`), неизвестный адресат уходит человеку с
`routing_failed`.

Проверяется не «функция что-то вернула», а **какой адрес и почему**: вид,
значение, причина (`Target.reason`). Причина — не украшение: она пишется в
`question_asked.payload.routing`, и по ней через месяц видно, почему вопрос ушёл
этой роли.

Две части:

1. резолвер (`dashboard/routing.py`) — чистая функция: спека и разметка вопроса
   на входе, адрес на выходе. Ни модели, ни БД, ни процессов, ни часов;
2. цикл целиком на настоящем журнале и настоящем пуле `tmp_path`:
   `ask()` с адресом → `sync_answers()` → `transit.next_stage()` поднял новую
   попытку; истёкший вопрос попытку **не** поднимает.

Адрес в пул не пишется: пул — канал человека, а этап и попытка — свойство
журнала (`docs/JOURNAL.md`, `ask()`).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]

NOW = 1_700_000_000
DEAL = "d_beef01"
BOARD = "stack"


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
routing = load_module("hermes_duty_routing", "routing.py")
journal = load_module("hermes_duty_journal", "journal.py")
transit = load_module("hermes_duty_transit", "transit.py")
ask = load_module("hermes_duty_ask", "ask.py")


# ------------------------------------------------------------ объявления спеки


def stage(
    role: str = "implementer",
    *,
    allowed: tuple[str, ...] = ("tester",),
    to_human: str = "decisions_only",
    max_hops: int = 2,
    risky: bool = False,
) -> Any:
    """Этап спеки с объявленным адресом — ровно те поля, которые читает резолвер."""
    declaration = spec_module.Ask(
        allowed=allowed,
        to_human=to_human,
        max_hops=max_hops,
        risky=risky,
    )
    return spec_module.Stage(id="impl", role=role, ask=declaration)


def fact(**extra: Any) -> dict[str, Any]:
    """Разметка фактового вопроса из `result.json` — класс назван явно."""
    return {"kind": "fact", "text": "в каком каталоге лежат миграции?", **extra}


# ---------------------------------------------------------------- адрес: роль


def test_declared_role_resolves_without_a_model() -> None:
    """Объявленный адрес: фактовый вопрос → роль из `allowed` (правило 1)."""
    target = routing.route(stage(allowed=("tester", "researcher")), fact(), 0)

    assert target.kind == routing.ROLE
    assert target.value == "tester"
    assert target.reason == routing.REASON_DECLARED
    assert target.address == "role:tester"
    assert target.to_human is False


def test_declared_order_is_the_spec_order() -> None:
    """Порядок адресатов — объявления в спеке, не алфавит и не догадка.

    `researcher` раньше по алфавиту, но спека назвала первым `tester`: список
    читается как предпочтение, и переставлять его по своему вкусу нельзя.
    """
    target = routing.route(stage(allowed=("tester", "researcher")), fact(), 0)

    assert target.value == "tester"


def test_role_does_not_answer_its_own_question() -> None:
    """Роль не отвечает на собственный вопрос: своё имя выкидывается (правило 4)."""
    target = routing.route(
        stage(role="implementer", allowed=("implementer", "researcher")), fact(), 0
    )

    assert target.kind == routing.ROLE
    assert target.value == "researcher"


def test_only_self_allowed_is_routing_failed() -> None:
    """Кроме себя адресатов нет — адрес потерян, а не «человек по цепочке»."""
    target = routing.route(stage(role="implementer", allowed=("implementer",)), fact(), 0)

    assert target.kind == routing.FAILED
    assert target.reason == routing.REASON_ONLY_SELF
    assert target.to_human is True


def test_unknown_role_is_routing_failed() -> None:
    """Объявленной роли не существует — вопрос уходит человеку с `routing_failed`."""
    target = routing.route(
        stage(allowed=("ghost",)), fact(), 0, known_roles=("tester", "researcher")
    )

    assert target.kind == routing.FAILED
    assert target.reason == routing.REASON_UNKNOWN_ROLE


def test_known_roles_absent_means_no_filter() -> None:
    """Пустой `known_roles` — «списка нет», а не «ролей нет»: роль не отбрасывается.

    Иначе резолвер молча превращал бы любой вызов без списка профилей в человека,
    и объявленный адрес перестал бы действовать.
    """
    target = routing.route(stage(allowed=("ghost",)), fact(), 0, known_roles=())

    assert target.kind == routing.ROLE
    assert target.value == "ghost"


def test_empty_allowed_goes_to_human_with_routing_failed() -> None:
    """`allowed` пуст → человек с `routing_failed` и причиной `allowed_empty` (правило 2)."""
    target = routing.route(stage(allowed=()), fact(), 0)

    assert target.kind == routing.FAILED
    assert target.reason == routing.REASON_ALLOWED_EMPTY
    assert target.as_payload() == {"target": "human_routing_failed", "reason": "allowed_empty"}


def test_stage_without_ask_is_routing_failed_not_exhausted_chain() -> None:
    """Этап без `ask` адреса не объявил: причина — пустой список, не «цепочка».

    Обе причины ведут к человеку, но называют разные факты: «адрес не назван» и
    «передачи кончились» — не одно и то же, и в журнале это должно читаться верно.
    """
    target = routing.route(spec_module.Stage(id="impl", role="implementer"), fact(), 0)

    assert target.kind == routing.FAILED
    assert target.reason == routing.REASON_ALLOWED_EMPTY


# ------------------------------------------------------------- адрес: человек


def test_risky_declaration_wins_over_allowed_roles() -> None:
    """`risky: true` в спеке — человек, даже когда роли объявлены (правило 3)."""
    target = routing.route(stage(allowed=("tester",), risky=True), fact(), 0)

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_RISKY
    assert target.to_human is True


def test_risky_question_marking_wins_over_allowed_roles() -> None:
    """Властный вопрос (разметка `risky`) — человек, даже при заполненном `allowed`."""
    target = routing.route(stage(allowed=("tester",)), fact(risky=True), 0)

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_RISKY


def test_risky_wins_over_declared_default() -> None:
    """Таймаут и «человеку не спрашивать» не превращают властный вопрос в решение.

    Публикация с объявленным дефолтом `не публикуем` не идёт по дефолту: иначе
    молчание человека решало бы за него.
    """
    declared = stage(allowed=(), to_human="never", max_hops=1, risky=True)

    target = routing.route(declared, fact(default="не публикуем"), 0)

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_RISKY


def test_authority_marked_question_goes_to_human() -> None:
    """Явная разметка «властный» — человек, роли рядом не рассматриваются (правило 3)."""
    target = routing.route(stage(allowed=("tester",)), {"kind": "Authority"}, 0)

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_AUTHORITY


def test_unmarked_question_goes_to_human() -> None:
    """Вопрос без разметки считается властным (безопасная сторона, правило 5)."""
    target = routing.route(stage(allowed=("tester",)), "в каком каталоге миграции?", 0)

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_UNMARKED


def test_question_without_kind_but_with_options_is_human() -> None:
    """`options` без класса — не разметка: класс назван, или вопрос идёт человеку."""
    target = routing.route(
        stage(allowed=("tester",)),
        {"text": "Публикуем diff?", "options": ["пушить", "стоп"]},
        0,
    )

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_UNMARKED


# --------------------------------------------------------- адрес: дефолт спеки


def test_declared_default_when_human_is_never_asked() -> None:
    """`to_human: never` + объявленный дефолт → `spec_default:<значение>` (правило 1)."""
    target = routing.route(
        stage(allowed=(), to_human="never", max_hops=1), fact(default="берём B"), 0
    )

    assert target.kind == routing.DEFAULT
    assert target.value == "берём B"
    assert target.address == "spec_default:берём B"
    assert target.reason == routing.REASON_DEFAULT
    assert target.to_human is False


def test_default_is_not_used_when_human_is_asked_for_decisions() -> None:
    """`decisions_only` не разрешает решать за человека: дефолт не адрес."""
    target = routing.route(
        stage(allowed=("tester",), to_human="decisions_only"), fact(default="берём B"), 0
    )

    assert target.kind == routing.ROLE
    assert target.value == "tester"


# -------------------------------------------------------------- адрес: цепочка


def test_max_hops_exhausted_sends_the_question_to_human() -> None:
    """Передачи исчерпаны — цепочка конечна, дальше решает человек (правило 4)."""
    target = routing.route(stage(allowed=("tester",), max_hops=1), fact(), 1)

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_HOPS


def test_max_hops_zero_means_human_at_once() -> None:
    """`max_hops: 0` (значение по умолчанию) — по цепочке не передавать вовсе."""
    target = routing.route(stage(allowed=("tester",), max_hops=0), fact(), 0)

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_HOPS


def test_hops_counter_survives_json_text() -> None:
    """Номер хопа приходит и строкой (из JSON): `"1"` — это исчерпанные `max_hops: 1`."""
    target = routing.route(stage(allowed=("tester",), max_hops=1), fact(), "1")

    assert target.kind == routing.HUMAN
    assert target.reason == routing.REASON_HOPS


# ------------------------------------------------------------- тотальность


def test_garbage_question_and_stage_keep_the_resolver_total() -> None:
    """Мусор вместо вопроса или объявления — человек, а не исключение (правило 8)."""
    for bad_question in (None, 42, ["fact"], {"kind": ["fact"]}):
        target = routing.route(stage(allowed=("tester",)), bad_question, 0)
        assert target.kind == routing.HUMAN

    no_stage = routing.route(None, fact(), 0)
    assert no_stage.kind == routing.FAILED

    junk_stage = routing.route(object(), fact(), 0)
    assert junk_stage.kind == routing.FAILED


def test_resolver_needs_no_model_no_network_no_clock() -> None:
    """Адресация — арифметика по объявлениям: ни модели, ни сети, ни часов, ни БД.

    Проверяется по импортам модуля, а не по обещанию в докстринге: появление
    `subprocess`/`socket`/`time` здесь и было бы «координатором», которого спека
    не разрешает.
    """
    source = (ROOT / "dashboard" / "routing.py").read_text(encoding="utf-8")
    forbidden = ("import subprocess", "import socket", "import urllib", "import http", "import time")
    for word in forbidden:
        assert word not in source, f"адрес не должен зависеть от {word!r}"
    assert "from datetime" not in source


# ------------------------------------------------------- адрес в журнале и в пуле

POOL_SCHEMA = """
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  project TEXT,
  text TEXT NOT NULL,
  text_full TEXT,
  origin TEXT,
  options_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','sent','answered','expired','pickup_gateway')),
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

CYCLE_SPEC = """
duty: probe
stages:
  - id: impl
    role: implementer
    ask: {allowed: [researcher], to_human: decisions_only, max_hops: 1}
  - id: verify
    role: tester
    needs: [impl]
"""


def make_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes: пустой пул вопросов и пустой журнал — по реальным схемам."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    pool = root / "messaging" / "pool.db"
    pool.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(pool)
    conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()
    return root


def write_spec(tmp_path: Path) -> Any:
    """Спека настоящим загрузчиком: резолвер должен видеть те же объекты, что движок."""
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(CYCLE_SPEC), encoding="utf-8")
    return spec_module.load_spec(path)


def start_attempt(conn: sqlite3.Connection, stage_id: str = "impl", attempt: int = 1) -> None:
    """Попытка этапа началась — как её пишет движок (`payload.stage`)."""
    journal.append(
        conn,
        [
            {
                "ts": NOW - 60,
                "recorded_at": NOW - 60,
                "deal_id": DEAL,
                "board": BOARD,
                "kind": "attempt_started",
                "payload": {
                    "stage": stage_id,
                    "attempt": attempt,
                    "run_id": f"{stage_id}:{attempt}",
                    "pid": 4242,
                },
                "source": "engine",
                "idempotency_key": f"start:{stage_id}:{attempt}",
            }
        ],
    )


def end_attempt(conn: sqlite3.Connection, stage_id: str = "impl", attempt: int = 1) -> None:
    """Попытка закончилась вопросом: так пишет движок — `attempt_ended`, затем вопрос.

    Порядок важен: `question_asked` адресуется попытке, а не наоборот. Незакрытая
    попытка означала бы «работа идёт» и `wait(attempt)`, а не `wait(question)`.
    """
    journal.append(
        conn,
        [
            {
                "ts": NOW - 50,
                "recorded_at": NOW - 50,
                "deal_id": DEAL,
                "board": BOARD,
                "kind": "attempt_ended",
                "payload": {
                    "stage": stage_id,
                    "attempt": attempt,
                    "outcome": "done",
                    "kind": None,
                    "exit": 0,
                },
                "source": "engine",
                "idempotency_key": f"end:{stage_id}:{attempt}",
            }
        ],
    )


def later(events: list[dict[str, Any]]) -> int:
    """`now` для перехода — после последнего факта журнала, а не раньше него.

    Часы в тесте не подменяются: `ask()` пишет настоящий `ts`, и брать «сейчас»
    меньшим, чем уже записанный факт, значило бы проверять переход на перевёрнутом
    времени.
    """
    return max(int(event["ts"]) for event in events) + 1


def ask_with_address(conn: sqlite3.Connection, home: Path, spec: Any, stage_id: str = "impl") -> Any:
    """Вопрос через единственный канал — с адресом этапа и попытки в журнале."""
    target = routing.route(spec.stage(stage_id), fact(), 0)
    return ask.ask(
        conn,
        {"id": DEAL, "board": BOARD, "home": home},
        "в каком каталоге миграции?",
        "полная версия вопроса",
        routing={**target.as_payload(), "stage": stage_id, "attempt": 1},
    )


def close_question(home: Path, question_id: int, status: str) -> None:
    """Пул закрывает вопрос штатным путём: ответом (`answered`) или сроком (`expired`).

    `answered_at` — текущее время пула: факт ответа обязан быть позже факта
    вопроса, иначе переход применил бы его раньше (`ts` — это порядок фактов).
    """
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        if status == "answered":
            conn.execute(
                "UPDATE questions SET status='answered', answer='в db/migrations', "
                "answered_via='manual', answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ?",
                (question_id,),
            )
        else:
            conn.execute("UPDATE questions SET status='expired' WHERE id = ?", (question_id,))
        conn.commit()
    finally:
        conn.close()


def test_routing_failed_reaches_the_journal_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`routing_failed` попадает в событие: адрес и причина видны в журнале, не только в ответе.

    Человек, которому ушёл вопрос, должен через месяц прочитать, почему вопроса
    не задали роли: `payload.routing` — единственное место, где это сказано.
    """
    home = make_home(tmp_path, monkeypatch)
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        lost = routing.route(stage(allowed=()), fact(), 0)
        asked = ask.ask(
            conn,
            {"id": DEAL, "board": BOARD, "home": home},
            "в каком каталоге миграции?",
            None,
            routing={**lost.as_payload(), "stage": "impl", "attempt": 1},
        )
        events = journal.read_events(conn, DEAL)
    finally:
        conn.close()

    asked_event = [event for event in events if event["kind"] == "question_asked"]
    assert len(asked_event) == 1
    payload = asked_event[0]["payload"]
    assert payload["question_id"] == asked.question_id
    assert payload["routing"] == {
        "target": "human_routing_failed",
        "reason": "allowed_empty",
        "stage": "impl",
        "attempt": 1,
    }


def test_answer_in_the_pool_raises_the_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Цикл целиком: вопрос → ответ в пуле → новая попытка того же этапа.

    Это то, ради чего адрес вообще нужен: вопрос без `stage` (так пишет `ask()`)
    переводит этап в `wait_human`, а ответ поднимает **новую** попытку того же
    этапа — следующий этап графа не запускается, пока работа не сделана.
    """
    home = make_home(tmp_path, monkeypatch)
    spec = write_spec(tmp_path)
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        start_attempt(conn)
        end_attempt(conn)
        asked = ask_with_address(conn, home, spec)

        events = journal.read_events(conn, DEAL)
        waiting = transit.next_stage(spec, events, later(events))
        assert waiting.kind == transit.WAIT
        assert waiting.what == "question"
        assert transit.stage_states(spec, events)["impl"].state == "wait_human"

        close_question(home, asked.question_id, "answered")
        report = ask.sync_answers(conn, home)
        assert report["appended"] == 1

        events = journal.read_events(conn, DEAL)
        decision = transit.next_stage(spec, events, later(events))
    finally:
        conn.close()

    assert decision.kind == transit.RUN
    assert decision.pair == [("impl", 2)], "ответ — новый вход, а не та же попытка"


def test_expired_question_does_not_raise_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Истёкший вопрос попытку не поднимает: закрытие по сроку — не ответ.

    `answer_len=0` для `expired` (`docs/JOURNAL.md`): пул не заполняет `answer` у
    протухшей строки, и выдавать длину остатка за длину ответа значило бы врать.
    Работы не сделано — этап `parked` (`park` — вопрос закрыт без ответа), а не
    `wait_human`: ждать больше нечего.
    """
    home = make_home(tmp_path, monkeypatch)
    spec = write_spec(tmp_path)
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        start_attempt(conn)
        end_attempt(conn)
        asked = ask_with_address(conn, home, spec)

        close_question(home, asked.question_id, "expired")
        ask.sync_answers(conn, home)
        events = journal.read_events(conn, DEAL)

        answered = [event for event in events if event["kind"] == "question_answered"]
        decision = transit.next_stage(spec, events, later(events))
    finally:
        conn.close()

    assert len(answered) == 1
    assert answered[0]["payload"]["answer_len"] == 0
    assert answered[0]["payload"]["answered_via"] == "expired"
    assert decision.kind == transit.PARK
    assert decision.pair == [], "истёкший вопрос — не ответ: новой попытки нет"
    assert transit.stage_states(spec, events)["impl"].state == "parked"


def test_question_address_does_not_create_a_second_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Адрес живёт в журнале, а не в пуле: в пул по-прежнему пишет только `ask()`.

    В строке пула адреса нет — у пула своя схема, и второй канал («роль отвечает
    прямо в пуле») не заводится: ответ роли — это новая попытка, а не строка пула.
    """
    home = make_home(tmp_path, monkeypatch)
    spec = write_spec(tmp_path)
    conn = journal.open_journal(home)
    assert conn is not None
    try:
        asked = ask_with_address(conn, home, spec)
    finally:
        conn.close()

    pool = sqlite3.connect(home / "messaging" / "pool.db")
    pool.row_factory = sqlite3.Row
    try:
        row = dict(pool.execute("SELECT * FROM questions").fetchone())
    finally:
        pool.close()

    assert row["id"] == asked.question_id
    assert row["origin"] == "duty"
    assert "routing" not in json.dumps(row, ensure_ascii=False)
    assert row["text"] == "в каком каталоге миграции?"
