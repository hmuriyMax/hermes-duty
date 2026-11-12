"""Пробы на родителе: закрытие дела гасит **настоящий** процесс, а не шим.

Проверка «красное на старом» иначе остаётся статической: если все кейсы идут через
шов `kill=`, а на родителе шва ещё нет, падение приходит `TypeError`-ом — то есть
проверяется форма вызова, а не поведение. Здесь вызовы те же, что в жизни
(`deals.close_deal(...)` и `deals.complete_deal(...)` без `kill=`), а «живая
попытка» — настоящий процесс (`/bin/sleep`), поэтому на родителе оба теста падают
по поведению: процесс жив, а в журнале один `completed` (гашения нет вовсе).

Файл — отдельный от `tests/test_close_attempts.py` намеренно: он должен
запускаться и на родителе (`4cfebde` для `close`, `2bbb5e9` для `complete`), где
соседний файл падал бы на импорте швов (`kill=`).
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

# Фикстуры — у соседнего файла отмены: пакета у `tests/` нет, pytest кладёт его в
# путь, и вторая копия `live_attempt` разошлась бы с первой на первом же поле.
from test_tools_cancel import (
    MOMENT,
    SEED,
    deals,
    journal,
    live_attempt,
    make_deal,
    make_home,
    open_store,
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


def test_close_without_the_seam_extinguishes_a_real_process(tmp_path: Path) -> None:
    """`close` без шва гасит живой процесс и пишет факт отмены **до** `completed`.

    На `4cfebde` (родитель F3) этот тест красный по поведению: `close_deal` писал
    только `completed`, процесс оставался жив, `attempt_ended` не появлялся.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    process = subprocess.Popen(["/bin/sleep", "30"])
    try:
        live_attempt(conn, deal, stage="impl", attempt=1, pid=process.pid)

        deals.close_deal(conn, str(deal["id"]), reason="работа больше не нужна", now=MOMENT)

        assert process.poll() is not None, "живой процесс остался жив"
        ended = ended_payloads(conn, deal)
        assert [payload["outcome"] for payload in ended] == ["cancelled"]
        assert ended[0]["pid"] == process.pid
        assert runner.lease_of(conn, deal, "impl") is None
        assert seen(conn, deal)[-2:] == ["attempt_ended", "completed"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_complete_without_the_seam_extinguishes_a_real_process(tmp_path: Path) -> None:
    """`complete` без шва гасит живой процесс — шестая дверь закрытия, та же рука.

    На `2bbb5e9` (до правки) тест красный по поведению: `complete` писал
    `completed`, а живой процесс продолжал работать отдельно от закрытого дела.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    process = subprocess.Popen(["/bin/sleep", "30"])
    try:
        live_attempt(conn, deal, stage="impl", attempt=1, pid=process.pid)

        deals.complete_deal(conn, str(deal["id"]), summary="готово", now=MOMENT)

        assert process.poll() is not None, "живой процесс остался жив"
        ended = ended_payloads(conn, deal)
        assert [payload["outcome"] for payload in ended] == ["cancelled"]
        assert ended[0]["pid"] == process.pid
        assert runner.lease_of(conn, deal, "impl") is None
        assert seen(conn, deal)[-2:] == ["attempt_ended", "completed"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
