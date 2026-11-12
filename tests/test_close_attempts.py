"""Закрытие дела гасит живые попытки (`docs/ENGINE-2.0.md`, раздел 5).

Живой случай RP-14338: человек убил pid 8397 руками и закрыл дело `d_ff4c3c3f`.
Через 3.5 минуты попытка дописала «процесс умер», а осиротевшая работа продолжила
жить отдельно от закрытого дела: `duty_cancel_deal` гасить живые попытки умел, а
`close` — нет, он писал только `completed`.

Здесь проверяется то, что видно в журнале и в store, а не в docstring:

1. `close` гасит живую попытку **до** `completed` — тем же механизмом, что отмена
   (`attempt_ended(outcome='cancelled')`, лизинг отпущен), поэтому закрытое дело
   больше не получает `attempt_failed` после своего закрытия;
2. чужой pid (лизинг называет другой процесс) не трогается: сигнала нет, отказ,
   ни одного факта — сигнал по переиспользованному pid убил бы чужую работу;
3. мёртвая попытка (уже закрытая фактом) и попытка без лизинга не трогаются:
   гасить нечего и не по чему;
4. повтор `close` идемпотентен: процессов не трогает, второго `completed` и
   второго `attempt_ended` не пишет;
5. `close_stages_done` (дело, закрытое пройденным графом) ведёт себя так же.

Гашение процессов подменяется фейком (`kill`): настоящий процесс под нож теста не
попадает, а порядок «гард → сигнал → факт» проверяется как есть.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

# Фикстуры — у соседнего файла отмены: пакета у `tests/` нет, pytest кладёт его в
# путь, и вторая копия `live_attempt` разошлась бы с первой на первом же поле.
from test_tools_cancel import (
    MOMENT,
    SEED,
    append,
    deals,
    journal,
    live_attempt,
    make_deal,
    make_home,
    open_store,
    recording_kill,
    runner,
    seen,
    write_spec,
)


def ended_payloads(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Payload'ы всех `attempt_ended` дела — исход и след каждой попытки."""
    return [
        journal.payload_of(event)
        for event in journal.read_events(conn, str(deal["id"]))
        if str(event["kind"]) == "attempt_ended"
    ]


def close(
    conn: sqlite3.Connection, deal: dict[str, Any], *, killed: list[int], reason: str = "работа больше не нужна"
) -> dict[str, Any]:
    """`close` решением человека: тот же кадр времени, что у лизингов и фактов."""
    return deals.close_deal(
        conn,
        str(deal["id"]),
        reason=reason,
        now=MOMENT,
        kill=recording_kill(killed),
    )


# ------------------------------------ 1. close гасит попытку до completed


def test_close_extinguishes_a_live_attempt_before_completed(tmp_path: Path) -> None:
    """`close`: сигнал живому pid, факт отмены попытки, и только потом `completed`."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    killed: list[int] = []

    applied = close(conn, deal, killed=killed)

    # `events` считает факты закрытия (`completed`); `attempt_ended` пишет гашение
    # попытки — своим вызовом, до него.
    assert applied["events"] == 1
    assert killed == [111]  # процесс погашен
    ended = ended_payloads(conn, deal)
    assert len(ended) == 1
    assert ended[0]["outcome"] == "cancelled"
    assert ended[0]["cancelled"] is True
    assert ended[0]["pid"] == 111
    assert ended[0]["reason"] == "попытка погашена: дело закрывается (closed_by_human)"
    assert runner.lease_of(conn, deal, "impl") is None  # лизинг отпущен
    # Порядок — контракт: попытка гаснет до закрытия, иначе закрытое дело получает
    # `attempt_failed` уже после своего `completed`.
    kinds = seen(conn, deal)
    assert kinds[-2:] == ["attempt_ended", "completed"]
    row = deals.deal_row(conn, str(deal["id"]))
    assert row is not None and row["status"] == "done"


# -------------------------------------- 2. чужой pid: отказ, ни одного факта


def test_close_does_not_signal_a_foreign_pid(tmp_path: Path) -> None:
    """Лизинг называет другой pid — отказ целиком: ни сигнала, ни фактов, ни статуса.

    Переиспользованный pid — это чужая работа: сигнал по нему убил бы её молча, а
    закрытие записало бы «попытка погашена» о живой чужой работе.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111, lease_pid=333)
    killed: list[int] = []
    before = seen(conn, deal)
    status_before = str(deals.deal_row(conn, str(deal["id"]))["status"])

    with pytest.raises(deals.DealError) as caught:
        close(conn, deal, killed=killed)

    assert "impl#1" in str(caught.value)
    assert killed == []
    assert seen(conn, deal) == before
    row = deals.deal_row(conn, str(deal["id"]))
    assert row is not None and row["status"] == status_before
    assert runner.lease_of(conn, deal, "impl") is not None  # лизинг не тронут


# --------------------------- 3. мёртвая попытка и попытка без лизинга: не трогаем


def test_close_leaves_an_ended_attempt_alone(tmp_path: Path) -> None:
    """Попытка уже закрыта фактом — сигнала нет, второго `attempt_ended` тоже."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    append(
        conn,
        deal,
        ("attempt_ended", {"run_id": "impl#1", "stage": "impl", "attempt": 1, "outcome": "failed", "kind": "infra"}),
    )
    killed: list[int] = []

    applied = close(conn, deal, killed=killed)

    assert applied["events"] == 1  # только `completed`
    assert killed == []  # мёртвый pid не трогается
    assert [payload["outcome"] for payload in ended_payloads(conn, deal)] == ["failed"]
    assert seen(conn, deal)[-1] == "completed"


def test_close_skips_an_attempt_no_lease_leads(tmp_path: Path) -> None:
    """Живая по журналу попытка без строки лизинга закрытию не мешает.

    Процесса за ней нет (лизинг — единственный признак «кто-то работает»), и
    сигнала без pid не бывает: записать «погашена» о работе, которую никто не
    ведёт, значило бы соврать в журнале. Осиротевшую попытку закрывает уборка
    своим фактом, а дело закрывается решением человека.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        ("attempt_started", {"run_id": "impl#1", "stage": "impl", "attempt": 1, "pid": 111, "lease_expires": MOMENT + 3600}),
    )
    killed: list[int] = []

    applied = close(conn, deal, killed=killed)

    assert applied["events"] == 1
    assert killed == []
    assert ended_payloads(conn, deal) == []
    assert seen(conn, deal)[-1] == "completed"


# ------------------------------------------------ 4. повтор close идемпотентен


def test_repeated_close_touches_no_process_and_writes_nothing(tmp_path: Path) -> None:
    """Повтор `close` с той же причиной: `events: 0`, процессов не трогает."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    killed: list[int] = []
    close(conn, deal, killed=killed)
    before = seen(conn, deal)

    again = close(conn, deal, killed=killed)

    assert again["events"] == 0 and again["idempotent"] is True
    assert killed == [111]  # второй раз никого не гасили
    assert seen(conn, deal) == before  # ни второго `completed`, ни второго `attempt_ended`
    assert seen(conn, deal).count("completed") == 1


# ------------------------------- 5. close_stages_done: тот же порядок и механизм


def test_close_stages_done_extinguishes_live_attempts(tmp_path: Path) -> None:
    """Дело, закрытое пройденным графом, живых попыток не держит."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    live_attempt(conn, deal, stage="verify", attempt=1, pid=222)
    killed: list[int] = []

    applied = deals.close_stages_done(
        conn,
        str(deal["id"]),
        stages=["impl", "verify"],
        now=MOMENT,
        kill=recording_kill(killed),
    )

    assert applied["events"] == 1  # `completed`; два `attempt_ended` пишет гашение
    assert sorted(killed) == [111, 222]
    ended = ended_payloads(conn, deal)
    assert [payload["outcome"] for payload in ended] == ["cancelled", "cancelled"]
    assert all(payload["cancelled"] for payload in ended)
    assert runner.lease_of(conn, deal, "impl") is None
    assert runner.lease_of(conn, deal, "verify") is None
    assert seen(conn, deal)[-1] == "completed"
    assert "attempt_ended" not in seen(conn, deal)[seen(conn, deal).index("completed") :]
