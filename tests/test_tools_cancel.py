"""Отмена этапа и дела (`docs/ENGINE-2.0.md`, раздел 5).

Проверяется то, что видно в журнале и в состоянии store, а не в docstring:

1. `duty_cancel` по `pending`-этапу пишет **только** `stage_cancelled`: гасить
   нечего, процесс не поднимается, но и запускать этап больше нельзя
   (`duty_start` отказывает — иначе «лид отказался» значило бы только «попытка
   погашена»);
2. отменённая попытка не воскресает `reaper`-ом: пара `attempt_started` ↔
   `attempt_ended` в журнале есть, и уборка не пишет второй конец;
3. слот освобождается сразу: после отмены одной из двух живых попыток
   `duty_start` третьего этапа проходит — идти за слотом к истечению лизинга лиду
   не нужно;
4. при несовпадении pid попытка **не** гасится и не помечается отменённой:
   сигнал по переиспользованному чужому pid убил бы чужую работу, поэтому
   инструмент отказывает, а живой процесс остаётся жив;
5. `duty_cancel_deal` гасит все живые попытки, ставит статус `cancelled` (а не
   `done`), и `duty now`/`stats` не смешивают отменённое с завершённым.

Гашение процессов подменяется фейком (`kill`), кроме одного теста: там процесс
настоящий (`/bin/sleep`), и проверяется, что сигнала ему не было.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from test_runner import (  # noqa: E402 - пакета нет, pytest кладёт `tests/` в путь
    NOW,
    deals,
    journal,
    make_home,
    open_store,
    runner,
    spec_module,
    transit,
    write_spec,
)

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str) -> Any:
    """Модуль плагина под стабильным именем: вторая загрузка дала бы вторую копию."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = ROOT / "dashboard" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tools = load("hermes_duty_tools", "tools.py")
reaper = load("hermes_duty_reaper", "reaper.py")
api = load("hermes_duty_plugin_api_cancel", "plugin_api.py")

# Три этапа без `needs`: отмена проверяется на слотах и лизингах, а не на графе.
# `evidence` объявлен у всех, кто что-то производит, — иначе спека не загрузится.
SEED = """
duty: отмена этапа и дела
repo: ~/GolandProjects/duty
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
  - id: review
    role: reviewer
    workspace: none
"""

# Момент решений: тот же кадр времени, что у фактов и лизингов (`test_runner.NOW`) —
# гард принадлежности pid сверяет `attempt_started.ts` с `taken_at` лизинга.
MOMENT = NOW + 10


# ------------------------------------------------------------------ фикстуры


def make_deal(conn: sqlite3.Connection, spec: Any) -> dict[str, Any]:
    """Своё дело: строка store плюс путь спеки, записанный в `created`."""
    created = deals.create_deal(
        conn,
        repo="duty",
        title="Отмена",
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
                "idempotency_key": f"test:{deal['id']}:{kind}:{index}:{payload.get('run_id') or ''}",
            }
            for index, (kind, payload) in enumerate(facts)
        ],
    )


def live_attempt(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    *,
    stage: str = "impl",
    attempt: int = 1,
    pid: int = 111,
    lease_pid: int | None = None,
    ttl: int = 3600,
) -> None:
    """Живая попытка этапа: факт `attempt_started`, лизинг с её pid и сроком.

    `lease_pid` — для случая «лизинг называет другой процесс»: так выглядит
    переиспользованный pid, и гард обязан это увидеть.
    """
    append(
        conn,
        deal,
        (
            "attempt_started",
            {
                "run_id": f"{stage}#{attempt}",
                "stage": stage,
                "attempt": attempt,
                "pid": pid,
                "lease_expires": NOW + ttl,
            },
        ),
    )
    assert runner.lease(
        conn,
        deal=deal,
        stage=stage,
        attempt=attempt,
        pid=pid if lease_pid is None else lease_pid,
        ttl=ttl,
        now=NOW,
    )


def seen(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[str]:
    """Виды фактов дела по порядку — «журнал вырос / не вырос» и «что записано»."""
    return [str(event["kind"]) for event in journal.read_events(conn, str(deal["id"]))]


def ended_payloads(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Payload'ы всех `attempt_ended` дела — исход и класс каждой попытки."""
    return [
        journal.payload_of(event)
        for event in journal.read_events(conn, str(deal["id"]))
        if str(event["kind"]) == "attempt_ended"
    ]


def recording_kill(pids: list[int], answer: bool = True) -> Any:
    """Фейк гашения: помнит, какие pid попросили погасить, и отвечает `answer`."""

    def kill(pid: int) -> bool:
        pids.append(int(pid))
        return answer

    return kill


def fake_spawn(written: list[tuple[Any, ...]]) -> Any:
    """Спавн попытки без процесса: фейк сам пишет `attempt_started` с ключом вызова."""

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
                        "lease_expires": NOW + 3600,
                        "idem": key,
                    },
                ),
            )
        finally:
            conn.close()
        return 4242

    return spawn


def refusal(call: Any, *args: Any, **kwargs: Any) -> str:
    """Причина отказа инструмента: отказ — исключение, ответ готовит обработчик."""
    with pytest.raises(tools.ToolsError) as caught:
        call(*args, **kwargs)
    return str(caught.value)


# ---------------------------------------------- 1. pending: только один факт


def test_pending_stage_is_cancelled_by_one_fact(tmp_path: Path) -> None:
    """Этап без попытки: `stage_cancelled`, ни сигнала, ни `attempt_ended`."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    killed: list[int] = []

    answer = tools.cancel(
        str(deal["id"]),
        "impl",
        "скоуп оказался другим",
        "cancel-pending",
        home=home,
        clock=lambda: MOMENT,
        kill=recording_kill(killed),
    )

    assert answer["ok"] is True
    assert answer["cancelled"] is True
    assert answer["attempt"] == 0
    assert answer["stopped"] is False
    assert killed == []  # гасить было нечего
    assert seen(conn, deal)[-1] == transit.STAGE_CANCELLED
    assert "attempt_ended" not in seen(conn, deal)
    fact = journal.payload_of(journal.read_events(conn, str(deal["id"]))[-1])
    assert fact["reason"] == "скоуп оказался другим"
    assert fact["by"] == "lead"
    # Срез этапа: отменённый этап терминален и называет себя словами лида.
    state = transit.stage_states(spec, journal.read_events(conn, str(deal["id"])))["impl"]
    assert state.state == transit.FAILED
    assert "отменён" in state.reason


def test_cancelled_stage_cannot_be_started_again(tmp_path: Path) -> None:
    """Отмену не обходит `duty_start`: попытки по отменённому этапу не поднимаются."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    tools.cancel(str(deal["id"]), "impl", "не нужен", "cancel-1", home=home, clock=lambda: MOMENT)
    written: list[tuple[Any, ...]] = []
    before = len(seen(conn, deal))

    why = refusal(
        tools.start,
        str(deal["id"]),
        "impl",
        "start-after-cancel",
        home=home,
        spawn=fake_spawn(written),
        clock=lambda: MOMENT,
    )

    assert "отменён" in why
    assert written == []  # второго процесса нет
    assert len(seen(conn, deal)) == before  # журнал не вырос


def test_cancel_repeat_writes_nothing_and_touches_no_process(tmp_path: Path) -> None:
    """Повтор с тем же ключом: прежний ответ, ноль фактов, ноль сигналов."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    killed: list[int] = []
    first = tools.cancel(
        str(deal["id"]), "impl", "не нужен", "same", home=home, clock=lambda: MOMENT, kill=recording_kill(killed)
    )
    after_first = len(seen(conn, deal))

    second = tools.cancel(
        str(deal["id"]), "impl", "не нужен", "same", home=home, clock=lambda: MOMENT + 5, kill=recording_kill(killed)
    )

    assert second["repeated"] is True
    assert second["stage"] == first["stage"]
    assert killed == []
    assert len(seen(conn, deal)) == after_first


def test_new_plan_revision_does_not_revive_a_cancelled_stage(tmp_path: Path) -> None:
    """Ревизия плана отмену не снимает: срез и решение говорят одно и то же.

    Перестановка того же id — единственная запись ревизии под занятым id, и она
    не делает отменённый этап идущим. Вернуть работу можно только новым этапом.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    tools.cancel(str(deal["id"]), "impl", "не нужен", "cancel-1", home=home, clock=lambda: MOMENT)

    plan = tools.plan(str(deal["id"]), [{"id": "impl", "needs": ["verify"]}], "перестановка", "p1", home=home, now=MOMENT + 5)

    assert plan["accepted"] is True
    # Срез: этап остался отменённым и после принятия ревизии.
    state = transit.stage_states(spec, journal.read_events(conn, str(deal["id"])))["impl"]
    assert state.state == transit.FAILED
    assert "отменён" in state.reason
    # Решение: попытку по нему по-прежнему не поднять.
    written: list[tuple[Any, ...]] = []
    why = refusal(
        tools.start, str(deal["id"]), "impl", "s1", home=home, spawn=fake_spawn(written), clock=lambda: MOMENT
    )
    assert "отменён" in why
    assert written == []

    # А новый этап той же ревизии стартует: работа возвращается новым id.
    assert tools.plan(
        str(deal["id"]),
        [{"id": "impl2", "role": "implementer", "workspace": "none", "evidence": [{"cmd": "true", "must": 0}]}],
        "новый этап",
        "p2",
        home=home,
        now=MOMENT + 6,
    )["accepted"] is True
    started = tools.start(
        str(deal["id"]), "impl2", "s2", home=home, spawn=fake_spawn(written), clock=lambda: MOMENT + 10
    )
    assert started["ok"] is True
    assert written and written[0][1] == "impl2"


# ------------------------------- 2-3. отменённая попытка: reaper и слот


def test_cancelled_attempt_is_not_reaped_and_frees_the_slot(tmp_path: Path) -> None:
    """Отмена гасит попытку окончательно: `reaper` её не трогает, слот свободен."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    killed: list[int] = []

    answer = tools.cancel(
        str(deal["id"]),
        "impl",
        "план изменился",
        "cancel-live",
        home=home,
        clock=lambda: MOMENT,
        kill=recording_kill(killed),
    )

    assert killed == [111]
    assert answer["attempt"] == 1
    assert answer["stopped"] is True
    ended = ended_payloads(conn, deal)
    assert [payload["outcome"] for payload in ended] == ["cancelled"]
    assert "kind" not in ended[0]  # класса отказа у отмены нет
    assert ended[0]["cancelled"] is True
    assert runner.lease_of(conn, deal, "impl") is None  # лизинг отпущен
    # Уборка далеко за порогом: пары в журнале достаточно, второго конца нет.
    assert reaper.sweep(conn, NOW + 10**6) == []
    assert [payload["outcome"] for payload in ended_payloads(conn, deal)] == ["cancelled"]
    # Слот свободен: другой этап поднимается сразу, без истечения лизинга.
    written: list[tuple[Any, ...]] = []
    started = tools.start(
        str(deal["id"]),
        "verify",
        "start-after-cancel",
        home=home,
        spawn=fake_spawn(written),
        clock=lambda: MOMENT,
    )
    assert started["ok"] is True
    assert written and written[0][1] == "verify"


def test_slot_limit_is_freed_by_cancellation(tmp_path: Path) -> None:
    """Две живые попытки — третья не влезает; после отмены одной влезает."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    live_attempt(conn, deal, stage="verify", attempt=1, pid=222)
    written: list[tuple[Any, ...]] = []
    full = refusal(
        tools.start,
        str(deal["id"]),
        "review",
        "start-full",
        home=home,
        spawn=fake_spawn(written),
        clock=lambda: MOMENT,
    )
    assert "живых попыток 2 из 2" in full
    assert written == []

    tools.cancel(
        str(deal["id"]), "impl", "этап отпал", "cancel-impl", home=home, clock=lambda: MOMENT, kill=recording_kill([])
    )

    started = tools.start(
        str(deal["id"]),
        "review",
        "start-after-free",
        home=home,
        spawn=fake_spawn(written),
        clock=lambda: MOMENT,
    )
    assert started["ok"] is True
    assert written[0][1] == "review"


# ---------------------------------------- 4. несовпадение pid: не гасим, не метим


def test_foreign_pid_is_not_killed_and_the_attempt_stays_alive(tmp_path: Path) -> None:
    """Лизинг называет другой pid — сигнала нет, попытка не отменена.

    Процесс в тесте живой (`/bin/sleep`), и гашение настоящее: если бы гард
    пропустил чужой pid, тест увидел бы мёртвый процесс, а не только отказ.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        live_attempt(conn, deal, stage="impl", attempt=1, pid=process.pid, lease_pid=process.pid + 1)
        before = seen(conn, deal)

        why = refusal(
            tools.cancel,
            str(deal["id"]),
            "impl",
            "чужой pid",
            "cancel-foreign",
            home=home,
            clock=lambda: MOMENT,
        )

        assert "лизинг" in why
        assert process.poll() is None  # чужой процесс не погашен
        assert seen(conn, deal) == before  # ни один факт не записан
        assert "stage_cancelled" not in seen(conn, deal)
        assert runner.lease_of(conn, deal, "impl") is not None  # лизинг на месте
    finally:
        process.kill()
        process.wait()


def test_closed_attempt_is_not_signalled_even_with_a_stale_lease(tmp_path: Path) -> None:
    """Пара в журнале уже закрыта — pid свободен, сигнала быть не должно."""
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
    ended_before = len(ended_payloads(conn, deal))

    answer = tools.cancel(
        str(deal["id"]),
        "impl",
        "этап не нужен",
        "cancel-closed",
        home=home,
        clock=lambda: MOMENT,
        kill=recording_kill(killed),
    )

    # Живой попытки нет: гасить нечего и некого, факт отмены — единственный новый.
    assert answer["stopped"] is False
    assert killed == []
    assert len(ended_payloads(conn, deal)) == ended_before
    assert seen(conn, deal)[-1] == transit.STAGE_CANCELLED


def test_stop_pid_really_kills_a_live_attempt_process(tmp_path: Path) -> None:
    """Гашение по pid: живой процесс действительно кончается, мёртвый — «погашен».

    Фейк `kill` в остальных тестах проверяет порядок «гард → сигнал → факт», но не
    сам сигнал. Здесь процесс настоящий, в своей группе — ровно так его поднимает
    `tools._spawn_attempt` (`start_new_session=True`), и группа гасится целиком.
    """
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        assert runner.stop_pid(process.pid, grace=5.0) is True
        assert process.wait(timeout=5) is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    # Уже мёртвый pid — не ошибка: гасить нечего, и «погашено» здесь правда.
    assert runner.stop_pid(process.pid, grace=1.0) is True


# --------------------------------------- 5. отмена дела: все попытки и статус


def test_cancel_deal_kills_every_live_attempt_and_sets_cancelled(tmp_path: Path) -> None:
    """`duty_cancel_deal`: гасит все живые попытки, дело — `cancelled`, не `done`."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    live_attempt(conn, deal, stage="verify", attempt=1, pid=222)
    killed: list[int] = []

    answer = tools.cancel_deal(
        str(deal["id"]),
        "человек отказался от задачи",
        "cancel-deal",
        home=home,
        clock=lambda: MOMENT,
        kill=recording_kill(killed),
    )

    assert answer["ok"] is True
    assert answer["status"] == "cancelled"
    assert answer["cause"] == "lead_cancelled"
    assert sorted(killed) == [111, 222]
    assert {row["stage"] for row in answer["attempts"]} == {"impl", "verify"}
    ended = ended_payloads(conn, deal)
    assert [payload["outcome"] for payload in ended] == ["cancelled", "cancelled"]
    assert all(payload.get("cancelled") for payload in ended)
    assert runner.lease_of(conn, deal, "impl") is None
    assert runner.lease_of(conn, deal, "verify") is None
    # Строка store и факт закрытия: статус `cancelled`, причина `lead_cancelled`.
    row = deals.deal_row(conn, str(deal["id"]))
    assert row is not None and row["status"] == "cancelled"
    closed = journal.closed_fact(journal.read_events(conn, str(deal["id"])))
    assert closed is not None
    assert closed["payload"]["cause"] == "lead_cancelled"
    assert closed["payload"]["status"] == "cancelled"
    # `completed` — вид факта закрытия; отмена закрывает дело, а не этап.
    assert seen(conn, deal)[-1] == "completed"


def test_cancelled_deal_leaves_now_and_stats(tmp_path: Path) -> None:
    """`duty now` его не показывает, `stats` держит отдельно от завершённого."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    tools.cancel_deal(
        str(deal["id"]),
        "передумали",
        "cancel-deal",
        home=home,
        clock=lambda: MOMENT,
        kill=recording_kill([]),
    )

    # «Дела сейчас» — открытые строки store: отменённое дело закрыто.
    open_ids = [str(row["id"]) for row in deals.deal_rows(conn, open_only=True)]
    assert str(deal["id"]) not in open_ids
    # Срез состояния: дело закрыто, состояния ожидания у него нет.
    events = journal.read_events(conn, str(deal["id"]))
    folded = journal.fold(events, now=MOMENT)
    assert folded["active"] is False
    assert folded["state"] is None
    assert "status=cancelled" in str(folded["evidence"])
    # Попытки: исход `cancelled` — отдельно от `done`, а не «закрыто».
    outcomes = [row["outcome"] for row in api.stagefeed.attempts(events, MOMENT)]
    assert outcomes == ["cancelled"]

    payload = api.collect_stats(home, MOMENT, since=NOW - 100)
    totals = payload["totals"]
    assert totals["deals_by_status"].get("cancelled") == 1
    assert totals["deals_by_status"].get("done") is None
    assert totals["attempts_by_outcome"].get("cancelled") == 1
    assert totals["attempts_by_outcome"].get("done") is None
    # Отменённый этап не считается «закрытым»: он не был доведён до конца.
    assert totals["stages"]["closed"] == 0
    assert totals["first_try"]["share"] is None


def test_cancel_deal_refuses_when_an_attempt_cannot_be_proven(tmp_path: Path) -> None:
    """Одна недоказуемая попытка — отказ целиком: наполовину отменённого не бывает."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    live_attempt(conn, deal, stage="impl", attempt=1, pid=111)
    live_attempt(conn, deal, stage="verify", attempt=1, pid=222, lease_pid=333)
    killed: list[int] = []
    before = seen(conn, deal)
    status_before = str(deals.deal_row(conn, str(deal["id"]))["status"])

    why = refusal(
        tools.cancel_deal,
        str(deal["id"]),
        "передумали",
        "cancel-deal",
        home=home,
        clock=lambda: MOMENT,
        kill=recording_kill(killed),
    )

    assert "verify#1" in why
    assert killed == []  # первая попытка тоже не погашена
    assert seen(conn, deal) == before
    row = deals.deal_row(conn, str(deal["id"]))
    assert row is not None and row["status"] == status_before


def test_cancelled_parent_does_not_hold_its_child(tmp_path: Path) -> None:
    """Отменённый родитель закрыт для гейта: ребёнок не висит в `todo` вечно.

    `cancelled` входит в `deals.CLOSED_STATUSES` тем же правилом, что `archived`:
    родитель терминален, и держать из-за него ребёнка в `todo` значило бы ждать
    работы, которой уже не будет. В `stats` отменённое при этом не считается
    завершённым — это разные вопросы, и наборы у них разные.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    parent = make_deal(conn, spec)
    child = deals.create_deal(conn, repo="duty", title="ребёнок", parents=[str(parent["id"])], now=NOW)
    assert child["deal"]["status"] == "todo"  # гейт держит, пока родитель открыт
    assert deals.gate_open(conn, str(child["deal"]["id"])) is False

    tools.cancel_deal(str(parent["id"]), "передумали", "cancel-parent", home=home, clock=lambda: MOMENT)

    assert deals.gate_open(conn, str(child["deal"]["id"])) is True


# ------------------------------------ 6. один вид, два вопроса: дело и этап


def test_cancelled_stage_is_not_a_quiet_kind() -> None:
    """Отмена тихая для **дела**, но решающая для **этапа**.

    Один журнальный вид отвечает на два разных вопроса: закрытое дело он не
    воскрешает (`NON_STATE_KINDS`), а для среза этапов это единственный факт
    отменённого `pending`-этапа — значит, из «тихих» видов этапа он исключён.
    Правило закреплено тестом: без него отмена молча выпадала из среза, и
    отменённый этап читался как «ждёт запуска» (`1089 passed` только после
    разведения наборов).
    """
    assert transit.STAGE_CANCELLED in journal.NON_STATE_KINDS
    assert transit.STAGE_CANCELLED in transit.STAGE_KINDS
    assert transit.STAGE_CANCELLED not in transit.QUIET_KINDS
