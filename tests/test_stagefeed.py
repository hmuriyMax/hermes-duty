"""Лента этапов дела (W6): `stages` в `/deal`, этап и попытка в `/now`, read-only.

Карточка W6 требует три вещи, которые нельзя проверить глазами при ревью:

1. `/deal/{id}` отдаёт блок `stages`, где у каждой попытки есть факты
   процесса (pid, начало, конец, код выхода, исход, класс, токены, инструменты,
   причина), а не строка статуса;
2. `/now` показывает, на каком этапе дело сейчас, без открытия дела;
3. ни одна читающая ручка не пишет в журнал — `?mode=ro` проверяется по
   mtime/sha файла до и после запроса, а не по обещанию в docstring.

Четвёртое — «мёртвый процесс виден без выдуманного статуса»: попытка без
`attempt_ended` с истёкшим лизингом остаётся `state=running` (так записано), но
получает `live: false` и `dead: true`. Это и есть ответ на «не выдумывать
статус»: состояние графа и живость процесса — разные факты.

Отдельный тест сторожит источник: в `dashboard/stagefeed.py` не должно появиться
ни `subprocess`, ни `ps`/`lsof`/`/proc`, ни `os.kill` — лента читает журнал.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_stages", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

journal = sys.modules[plugin_api.JOURNAL_MODULE_NAME]
stagefeed = plugin_api.stagefeed

NOW = int(time.time()) - 10  # часы теста — почти настоящие: лизинг «в прошлом» обязан истечь
# Группировка дела — `repo` (0.0.6: доск нет); адрес дела — его `d_<hex>`.
REPO = "sx-widgets-api"

# Спека графа: два этапа, у первого объявлена команда и ретрай по `contract` —
# ровно тот случай, который карточка называет фикстурой (одна попытка упала
# отказом контракта, вторая идёт).
SPEC = """
duty: лента этапов
stages:
  - id: impl
    role: implementer
    evidence:
      - {cmd: "go build ./...", must: 0}
    retry: {max: 1, on: [contract]}
  - id: review
    role: reviewer
    needs: [impl]
    verdict: {approve: next, changes: impl, max_rounds: 1}
"""


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Корень Hermes: дело заводится своим store, доска для этого не нужна."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client(home):
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def spec_file(home: Path) -> Path:
    path = home / "duty.yaml"
    path.write_text(SPEC, encoding="utf-8")
    return path


def new_deal(client: TestClient, home: Path, *, spec: bool = True) -> str:
    payload = {"repo": REPO, "title": "дело с лентой"}
    if spec:
        payload["spec"] = str(spec_file(home))
    response = client.post("/deals", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def append(client: TestClient, home: Path, deal_id: str, events: list[dict]) -> None:
    """События попыток в журнал — так их пишет движок (`dashboard/runner.py`)."""
    conn = journal.open_journal(home)
    try:
        for index, event in enumerate(events):
            journal.append(
                conn,
                [
                    {
                        "ts": event["ts"],
                        "recorded_at": event["ts"],
                        "deal_id": deal_id,
                        "kind": event["kind"],
                        "payload": event["payload"],
                        "source": event.get("source", "engine"),
                        "idempotency_key": event.get("key", f"{deal_id}:{event['kind']}:{index}"),
                    }
                ],
            )
    finally:
        conn.close()


def two_attempts(deal_id: str) -> list[dict]:
    """Две попытки первого этапа: первая упала отказом контракта, вторая идёт."""
    return [
        {
            "ts": NOW - 400,
            "kind": "attempt_started",
            "payload": {
                "run_id": f"{deal_id}:impl#1",
                "stage": "impl",
                "attempt": 1,
                "pid": 20658,
                "lease_expires": NOW - 100,
                "contract": {"role": "implementer"},
            },
        },
        {
            "ts": NOW - 100,
            "kind": "attempt_ended",
            "payload": {
                "run_id": f"{deal_id}:impl#1",
                "stage": "impl",
                "attempt": 1,
                "outcome": "failed",
                "kind": "contract",
                "reason": "нет evidence по go build ./...",
                "details": {
                    "exit_code": 0,
                    "usage_tokens": 15234,
                    "tools": 17,
                    "verdict": {"ok": False, "kind": "contract", "details": ["evidence.missing"]},
                },
                "result": {
                    "status": "done",
                    "summary": "готово",
                    "evidence": [{"cmd": "go  build ./...", "exit": 1, "started_at": NOW - 200}],
                },
            },
        },
        {
            "ts": NOW - 50,
            "kind": "attempt_started",
            "payload": {
                "run_id": f"{deal_id}:impl#2",
                "stage": "impl",
                "attempt": 2,
                "pid": 20999,
                "lease_expires": NOW + 900,
                "contract": {"role": "implementer"},
            },
        },
    ]


def test_deal_returns_stages_with_attempts(client, home):
    """`/deal` отдаёт ленту: этапы графа, у попытки — факты процесса."""
    deal_id = new_deal(client, home)
    append(client, home, deal_id, two_attempts(deal_id))

    body = client.get(f"/deal/{deal_id}").json()

    assert body["spec"] == str(home / "duty.yaml")
    assert body["stages_note"] == ""
    stages = {stage["id"]: stage for stage in body["stages"]}
    assert list(stages) == ["impl", "review"]  # порядок объявления графа, не журнала

    impl = stages["impl"]
    assert impl["state"] == "running"
    assert impl["attempt"] == 2
    assert [attempt["n"] for attempt in impl["attempts"]] == [1, 2]

    first, second = impl["attempts"]
    # попытка — процесс: время, код выхода, исход, класс, токены, инструменты
    assert first["pid"] == 20658
    assert first["started_at"] == NOW - 400
    assert first["finished_at"] == NOW - 100
    assert first["duration_sec"] == 300
    assert first["exit"] == 0
    assert first["outcome"] == "failed"
    assert first["kind"] == "contract"
    assert first["tokens"] == 15234
    assert first["tools"] == 17
    assert first["reason"] == "нет evidence по go build ./..."
    assert first["live"] is False
    assert first["lease"] == "ended"
    # что делала: запись объявленной команды из отчёта попытки
    assert first["evidence"] == [{"cmd": "go build ./...", "exit": 1, "started_at": NOW - 200}]

    assert second["pid"] == 20999
    assert second["finished_at"] is None
    assert second["duration_sec"] is None  # идущая попытка не мерит себя
    assert second["live"] is True

    # evidence этапа — объявленные команды против отчёта: код не совпал с must
    assert impl["evidence"] == [
        {"cmd": "go build ./...", "must": 0, "got": 1, "ok": False, "at": NOW - 200}
    ]

    review = stages["review"]
    assert review["state"] == "wait_gate"
    assert review["attempt"] == 0
    assert review["attempts"] == []
    assert review["waits"]["kind"] == "gate"


def test_deal_without_attempts_keeps_the_graph(client, home):
    """Дело без попыток: этапы видны, попыток ноль — граф не зависит от журнала."""
    deal_id = new_deal(client, home)

    body = client.get(f"/deal/{deal_id}").json()

    assert [stage["id"] for stage in body["stages"]] == ["impl", "review"]
    assert body["stages"][0]["state"] == "pending"
    assert body["stages"][0]["attempts"] == []
    # объявленная команда без записи — «не доказано», а не «провалено»
    assert body["stages"][0]["evidence"] == [
        {"cmd": "go build ./...", "must": 0, "got": None, "ok": False, "at": None}
    ]


def test_deal_without_spec_names_the_reason(client, home):
    """Пустая лента называет причину: «нет спеки» и «этапов нет» — разные факты."""
    deal_id = new_deal(client, home, spec=False)

    body = client.get(f"/deal/{deal_id}").json()

    assert body["stages"] == []
    assert body["spec"] is None
    assert "нет спеки" in body["stages_note"]
    # явный ?spec= даёт граф и делу без записи в журнале
    given = client.get(f"/deal/{deal_id}", params={"spec": str(spec_file(home))}).json()
    assert [stage["id"] for stage in given["stages"]] == ["impl", "review"]


def test_deal_with_unreadable_spec_names_the_reason(client, home):
    """Спека не читается — лента пуста, но причина названа, а не спрятана."""
    deal_id = new_deal(client, home)

    body = client.get(
        f"/deal/{deal_id}", params={"spec": str(home / "нет-такого.yaml")}
    ).json()

    assert body["stages"] == []
    assert "не читается" in body["stages_note"]


def test_dead_attempt_is_visible_without_invented_state(client, home):
    """Попытка без завершения и с истёкшим лизингом: `live: false`, `dead: true`.

    Состояние этапа при этом остаётся тем, что записано в журнале (`running`):
    мёртвость процесса — отдельный факт, а не выдуманный статус.
    """
    deal_id = new_deal(client, home)
    append(
        client,
        home,
        deal_id,
        [
            {
                "ts": NOW - 500,
                "kind": "attempt_started",
                "payload": {
                    "run_id": f"{deal_id}:impl#1",
                    "stage": "impl",
                    "attempt": 1,
                    "pid": 20123,
                    "lease_expires": NOW - 60,
                    "contract": {"role": "implementer"},
                },
            }
        ],
    )

    body = client.get(f"/deal/{deal_id}").json()
    impl = body["stages"][0]

    assert impl["state"] == "running"
    attempt = impl["attempts"][0]
    assert attempt["finished_at"] is None
    assert attempt["live"] is False
    assert attempt["dead"] is True
    assert attempt["lease"] == "dead"


def test_attempt_without_lease_is_unknown_not_dead(client, home):
    """Срок лизинга не записан — живость неизвестна, а не «мёртв»."""
    deal_id = new_deal(client, home)
    append(
        client,
        home,
        deal_id,
        [
            {
                "ts": NOW - 500,
                "kind": "attempt_started",
                "payload": {"run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1},
            }
        ],
    )

    attempt = client.get(f"/deal/{deal_id}").json()["stages"][0]["attempts"][0]

    assert attempt["live"] is None
    assert attempt["dead"] is None
    assert attempt["lease"] == "unknown"


def test_now_shows_stage_and_attempt(client, home):
    """`/now`: «что сейчас» видно без открытия дела — этап и номер попытки."""
    deal_id = new_deal(client, home)
    append(client, home, deal_id, two_attempts(deal_id))

    payload = client.get("/now").json()
    row = next(deal for deal in payload["deals"] if deal["id"] == deal_id)

    assert row["stage"] == {
        "id": "impl",
        "role": "implementer",
        "attempt": 2,
        "state": "running",
        "live": True,
        "since": NOW - 50,
    }


def test_now_stage_is_null_without_attempts(client, home):
    """Дело без попыток: поля `stage` нет значения — граф без спеки не выдумывается."""
    deal_id = new_deal(client, home)

    row = next(deal for deal in client.get("/now").json()["deals"] if deal["id"] == deal_id)

    assert row["stage"] is None


def fingerprint(path: Path) -> tuple:
    """Слепок файла: размер, mtime и sha256 — «не изменился» значит все три."""
    data = path.read_bytes()
    stat = path.stat()
    return (stat.st_size, stat.st_mtime_ns, hashlib.sha256(data).hexdigest())


def files_of(home: Path) -> dict[str, tuple]:
    """Все файлы журнала: сам `duty.db` плюс WAL-спутники, если они есть."""
    return {
        str(path.name): fingerprint(path)
        for path in sorted((home / "duty").glob("duty.db*"))
        if path.is_file()
    }


def event_rows(home: Path) -> int:
    """Число строк журнала: чтение не должно добавлять ни одной."""
    conn = journal.connect_ro(journal.journal_path(home))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    finally:
        conn.close()


def test_connect_ro_settled_only_creates_no_side_files(tmp_path):
    """`settled_only`: журнал дописан — чтение не заводит рядом `-shm`/`-wal`.

    «Показал экран» не должно оставлять в каталоге журнала новых файлов: иначе
    read-only ручка меняет каталог, хоть и не меняет журнал.
    """
    path = tmp_path / "j.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (v TEXT)")
    writer.commit()
    writer.close()

    conn = journal.connect_ro(path, settled_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        conn.close()

    assert not (tmp_path / "j.db-wal").exists()
    assert not (tmp_path / "j.db-shm").exists()


def test_connect_ro_settled_only_still_reads_a_live_wal(tmp_path):
    """Есть `-wal` — читаем обычным `?mode=ro`: иначе срез отстал бы от журнала.

    Это обратная сторона «не создавать файлов»: незачекпойнченные события лежат
    в `-wal`, и неизменяемое чтение их не увидело бы. Поэтому `immutable=1`
    включается только когда `-wal` рядом нет.
    """
    path = tmp_path / "j.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (v TEXT)")
    writer.commit()
    writer.execute("INSERT INTO t VALUES ('в журнале')")
    writer.commit()
    assert (tmp_path / "j.db-wal").exists()

    conn = journal.connect_ro(path, settled_only=True)
    try:
        assert [row[0] for row in conn.execute("SELECT v FROM t")] == ["в журнале"]
    finally:
        conn.close()
        writer.close()


def test_read_connection_refuses_writes(client, home):
    """Read-only соединение ручек не даёт себя записать, а не «обещает».

    Файл журнала после просмотра экрана одинаков — но одинаков он и у открытого
    на запись соединения, которое просто ничего не записало. Поэтому проверяется
    сама возможность: попытка `INSERT` обязана упасть `readonly`.
    """
    deal_id = new_deal(client, home)
    append(client, home, deal_id, two_attempts(deal_id))

    conn = plugin_api._open_journal_ro(home)
    assert conn is not None
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
            conn.execute(
                "INSERT INTO events (ts, recorded_at, deal_id, kind, payload, source,"
                " idempotency_key) VALUES (1, 1, 'd_x', 'created', '{}', 'test', 'k-probe')"
            )
    finally:
        conn.close()


def test_reading_routes_do_not_write_journal(client, home):
    """`/deal` и `/now` не меняют журнал: ни байта в `duty.db`, ни одной строки.

    Сравнивается всё: файл журнала (размер, mtime, sha256), число строк и набор
    файлов. Журнал дописан и закрыт, `-wal` нет — значит читающая ручка обязана
    не заводить рядом с ним ни одного нового файла (поэтому чтение идёт
    `settled_only`: `immutable=1` вместо WAL-спутников).
    """
    deal_id = new_deal(client, home)
    append(client, home, deal_id, two_attempts(deal_id))
    rows_before = event_rows(home)
    before = files_of(home)
    assert before and "duty.db" in before, "журнал должен существовать после записи событий"

    assert client.get("/now").status_code == 200
    assert client.get(f"/deal/{deal_id}").status_code == 200
    assert client.get(f"/deal/{deal_id}", params={"spec": str(spec_file(home))}).status_code == 200

    after = files_of(home)
    assert after["duty.db"] == before["duty.db"], "чтение изменило файл журнала"
    assert event_rows(home) == rows_before, "чтение дописало события в журнал"
    assert set(after) == set(before), f"чтение создало файлы журнала: {sorted(set(after) - set(before))}"


def test_reading_routes_do_not_create_journal(home):
    """Пока журнала нет, чтение не заводит его: «посмотрел» ≠ «включил store»."""
    app = FastAPI()
    app.include_router(plugin_api.router)
    client = TestClient(app)

    assert client.get("/now").status_code == 200
    assert not (home / "duty").exists() or not list((home / "duty").glob("duty.db*"))


def test_stagefeed_reads_journal_only(monkeypatch, home):
    """Лента не смотрит на процессы: даже если проба pid недоступна — ответ есть.

    `journal.pid_alive` — шов старого блока `waiting` (`os.kill`). Лента этапов
    обязана считать всё без него: её источник — срок лизинга из журнала.
    """
    deal_id = "d_deadbeef"
    spec = stagefeed.spec_module.load_spec(spec_file(home))
    events = [
        {
            "id": 1,
            "ts": NOW - 500,
            "kind": "attempt_started",
            "payload": {
                "run_id": f"{deal_id}:impl#1",
                "stage": "impl",
                "attempt": 1,
                "pid": 20123,
                "lease_expires": NOW - 60,
                "contract": {"role": "implementer"},
            },
        }
    ]

    def boom(_pid):  # pragma: no cover - вызов здесь был бы провалом теста
        raise AssertionError("лента этапов не имеет права проверять pid процесса")

    monkeypatch.setattr(journal, "pid_alive", boom)

    rows = stagefeed.stages(spec, events, NOW)

    assert rows[0]["attempts"][0]["dead"] is True


def test_shell_attempt_is_named_by_kind_not_by_role(home):
    """Попытка `kind: shell` видна с кодом возврата и без выдуманного профиля.

    У этапа-команды роли нет: `role` пуст, а исполнителя называет
    `stage_kind: shell` из `attempt_started`. Код возврата и длительность — те же
    поля `exit`/`duration_sec`, что у агента, поэтому консоль печатает shell-попытку
    не отдельной веткой, а тем же таймлайном.
    """
    events = [
        {
            "ts": NOW - 30,
            "kind": "attempt_started",
            "payload": {
                "run_id": "check#1",
                "stage": "check",
                "attempt": 1,
                "pid": 70100,
                "lease_expires": NOW + 600,
                "stage_kind": "shell",
                "shell_cmd": "pytest tests/ -q",
            },
        },
        {
            "ts": NOW - 25,
            "kind": "attempt_ended",
            "payload": {
                "run_id": "check#1",
                "stage": "check",
                "attempt": 1,
                "outcome": "failed",
                "kind": "contract",
                "reason": "код 1, ждали 0",
                "details": {"exit_code": 1, "duration_sec": 5.0, "stdout_tail": [], "stderr_tail": []},
            },
        },
    ]
    rows = stagefeed.attempts(events, NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row["stage_kind"] == "shell"
    assert row["role"] == ""
    assert (row["exit"], row["kind"], row["outcome"]) == (1, "contract", "failed")
    assert row["duration_sec"] == 5


def _progress(stage: str, attempt: int, seq: int, phase: str, name: str, ts: int, ms: int | None = None) -> dict:
    """Событие телеметрии шага — в той форме, что пишет `runner._progress_event`."""
    payload = {"stage": stage, "attempt": attempt, "seq": seq, "phase": phase, "name": name, "ts": ts}
    if ms is not None:
        payload["duration_ms"] = ms
    return {"ts": ts, "kind": "attempt_progress", "payload": payload}


def _attempt(stage: str, attempt: int, started: int, ended: int | None, outcome: str = "done") -> list[dict]:
    events = [
        {
            "ts": started,
            "kind": "attempt_started",
            "payload": {
                "run_id": f"{stage}#{attempt}",
                "stage": stage,
                "attempt": attempt,
                "pid": 4242,
                "lease_expires": started + 600,
                "contract": {"role": "implementer"},
            },
        }
    ]
    if ended is not None:
        events.append(
            {
                "ts": ended,
                "kind": "attempt_ended",
                "payload": {
                    "run_id": f"{stage}#{attempt}",
                    "stage": stage,
                    "attempt": attempt,
                    "outcome": outcome,
                    "details": {"exit_code": 0},
                },
            }
        )
    return events


def test_calls_pair_start_with_result_and_keep_duration():
    """Вызов — пара `tool_use` ↔ `tool_result`: начало считается назад от конца.

    Длительность есть только у результата, поэтому начало вызова выводится из
    неё. Время старта без этой арифметики поехало бы вправо на всю длительность,
    и таймлайн показывал бы вызовы там, где они закончились.
    """
    events = _attempt("impl", 1, NOW - 100, NOW - 10)
    events += [
        _progress("impl", 1, 1, "tool_use", "read_file", NOW - 90),
        _progress("impl", 1, 2, "tool_result", "read_file", NOW - 88, ms=1900),
        _progress("impl", 1, 3, "tool_use", "search_files", NOW - 60),
        _progress("impl", 1, 4, "tool_result", "search_files", NOW - 49, ms=11000),
    ]
    row = stagefeed.attempts(events, NOW)[0]
    assert [(call["name"], call["at"], call["ms"]) for call in row["calls"]] == [
        ("read_file", NOW - 89.9, 1900),
        ("search_files", NOW - 60, 11000),
    ]
    assert row["calls_omitted"] == 0


def test_says_are_kept_apart_from_calls():
    """Сказанное — не вызов: своя полка, свой потолок, свой смысл.

    Раннер сводит дельты текста в один факт на фразу (`phase: say`). Если свалить
    их в `calls`, поверхность нарисует несуществующие инструменты с пустым именем,
    а счётчик вызовов начнёт врать. Поэтому рядом: `says` — что агент говорил,
    `calls` — что он трогал.
    """
    events = _attempt("impl", 1, NOW - 100, NOW - 10)
    events += [
        _progress("impl", 1, 1, "say", "", NOW - 95, ms=4000),
        _progress("impl", 1, 2, "tool_use", "read_file", NOW - 90),
        _progress("impl", 1, 3, "tool_result", "read_file", NOW - 88, ms=1900),
    ]
    events[2]["payload"]["preview"] = "Сначала посмотрю файл."
    row = stagefeed.attempts(events, NOW)[0]
    assert [call["name"] for call in row["calls"]] == ["read_file"]
    assert row["says"] == [{"seq": 1, "at": NOW - 99, "ms": 4000, "text": "Сначала посмотрю файл."}]
    assert row["says_omitted"] == 0


def test_say_without_preview_is_not_a_fact():
    """Пустое сказанное — не сказанное: молчание не притворяется фразой."""
    events = _attempt("impl", 1, NOW - 100, NOW - 10)
    events += [_progress("impl", 1, 1, "say", "", NOW - 95, ms=1000)]
    row = stagefeed.attempts(events, NOW)[0]
    assert row["says"] == []


def test_call_without_result_keeps_its_start_and_null_duration():
    """Вызов, чей результат не пришёл (попытка оборвалась) — `ms: null`, но виден.

    Спрятать его значило бы показать меньше работы, чем было; придумать срок —
    показать больше. Поэтому он остаётся с началом из `tool_use` и без числа.
    """
    events = _attempt("impl", 1, NOW - 100, NOW - 10)
    events += [
        _progress("impl", 1, 1, "tool_use", "terminal", NOW - 70),
        _progress("impl", 1, 2, "tool_use", "read_file", NOW - 40),
        _progress("impl", 1, 3, "tool_result", "read_file", NOW - 39, ms=500),
    ]
    row = stagefeed.attempts(events, NOW)[0]
    calls = {call["name"]: call for call in row["calls"]}
    assert calls["terminal"]["ms"] is None
    assert calls["terminal"]["at"] == NOW - 70
    assert calls["read_file"]["ms"] == 500


def test_repeated_tool_name_pairs_in_order():
    """Два вызова одного инструмента подряд склеиваются по порядку, а не крест-накрест."""
    events = _attempt("impl", 1, NOW - 100, NOW - 10)
    events += [
        _progress("impl", 1, 1, "tool_use", "read_file", NOW - 90),
        _progress("impl", 1, 2, "tool_result", "read_file", NOW - 89, ms=300),
        _progress("impl", 1, 3, "tool_use", "read_file", NOW - 80),
        _progress("impl", 1, 4, "tool_result", "read_file", NOW - 70, ms=9000),
    ]
    calls = stagefeed.attempts(events, NOW)[0]["calls"]
    assert [call["ms"] for call in calls] == [300, 9000]
    assert calls[0]["at"] == NOW - 89.3
    assert calls[1]["at"] == NOW - 79


def test_calls_are_capped_and_the_remainder_is_named():
    """Потолок вызовов: лишние не молча выкидываются, а считаются в `calls_omitted`."""
    events = _attempt("impl", 1, NOW - 5000, NOW - 10)
    budget = stagefeed.MAX_CALLS + 7
    for index in range(budget):
        seq = index * 2 + 1
        moment = NOW - 4000 + index
        events.append(_progress("impl", 1, seq, "tool_use", "read_file", moment))
        events.append(_progress("impl", 1, seq + 1, "tool_result", "read_file", moment + 1, ms=10))
    row = stagefeed.attempts(events, NOW)[0]
    assert len(row["calls"]) == stagefeed.MAX_CALLS
    assert row["calls_omitted"] == 7


def test_attempt_without_telemetry_has_empty_calls():
    """Нет событий прогресса — пустой список, а не выдуманная строка."""
    row = stagefeed.attempts(_attempt("impl", 1, NOW - 100, NOW - 10), NOW)[0]
    assert row["calls"] == []
    assert row["calls_omitted"] == 0


def test_stagefeed_has_no_process_lookups():
    """Источник ленты — журнал: ни процессов, ни их проб.

    Проверяется импорт, а не текст: `ps`/`/proc`/`lsof` встречаются в docstring
    как объяснение того, чего модуль *не* делает, и запрещать слова было бы
    запретом писать о правиле. Запрещены именно возможности.
    """
    source = (ROOT / "dashboard" / "stagefeed.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"subprocess", "os", "shutil", "psutil", "signal"}), (
        f"stagefeed.py: доступ к процессам через {sorted(imported)}"
    )
    assert "os.kill" not in source
    assert "popen" not in source


# ------------------------------------------------------------------ гейт
#
# Гейт (`kind: gate`) в ленте — обычный этап: `wait_human` с номером вопроса.
# Отличается только парковка: у неё нет класса отказа и счётчика попыток, и
# нулевые поля вместо отсутствующих были бы утверждением о несуществующих
# попытках.

GATE_SPEC = """
duty: гейт
stages:
  - id: gate
    kind: gate
    ask: {q: "Выкатываем?", options: [да, нет]}
  - id: check
    kind: shell
    cmd: "true"
    needs: [gate]
    workspace: none
"""


def test_gate_question_is_wait_human_with_question_id(home):
    """Ожидание гейта в ленте: `wait_human`, вид ожидания `question`, номер вопроса есть."""
    path = home / "gate.yaml"
    path.write_text(GATE_SPEC, encoding="utf-8")
    spec = stagefeed.spec_module.load_spec(path)
    events = [
        {
            "ts": NOW - 30,
            "kind": "question_asked",
            "payload": {
                "question_id": 284,
                "session_key": "t_0001",
                "options": ["да", "нет"],
                "routing": {"stage": "gate", "attempt": 1},
            },
        }
    ]

    rows = {row["id"]: row for row in stagefeed.stages(spec, events, NOW)}

    assert rows["gate"]["state"] == "wait_human"
    assert rows["gate"]["waits"]["kind"] == "question"
    assert rows["gate"]["waits"]["question_id"] == 284
    assert rows["gate"]["attempts"] == []  # попытки-процесса у гейта не было
    assert rows["check"]["state"] == "wait_gate"


def test_gate_park_waits_without_class_and_counter():
    """Парковка гейта: вид `parked` и флаг `gate`, а полей класса и счётчика нет вовсе."""
    waits = stagefeed._waits(
        "parked",
        "284",
        "этап 'gate': гейт: человек не ответил за 5 мин",
        {"gate": True, "reason": "гейт: человек не ответил за 5 мин"},
    )

    assert waits["kind"] == "parked"
    assert waits["gate"] is True
    assert waits["question_id"] == 284
    assert "человек не ответил за 5 мин" in waits["reason"]
    assert "class" not in waits and "attempts" not in waits and "limit" not in waits


def test_now_gate_question_shows_options_from_the_pool(home):
    """`/now` на ждущем гейте: `wait_human` с вопросом и вариантами ответа из пула.

    Варианты — факт пула (`options_json`), а не догадка из журнала: человек
    выбирает ответ, и «вопрос #284 без ответа» без вариантов заставляло бы
    открывать панель, чтобы вспомнить, из чего выбирать.
    """
    path = home / "gate.yaml"
    path.write_text(GATE_SPEC, encoding="utf-8")
    app = FastAPI()
    app.include_router(plugin_api.router)
    client = TestClient(app)
    created = client.post("/deals", json={"repo": REPO, "title": "гейт", "spec": str(path)})
    assert created.status_code == 200, created.text
    deal_id = created.json()["id"]
    (home / "messaging").mkdir(parents=True, exist_ok=True)
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        pool.executescript(
            """
            CREATE TABLE questions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_key TEXT NOT NULL, project TEXT, text TEXT NOT NULL, text_full TEXT,
              origin TEXT, options_json TEXT, status TEXT NOT NULL DEFAULT 'pending',
              sender_rowid INTEGER, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
              sent_at TEXT, answered_at TEXT, answer TEXT, timeout REAL,
              critical INTEGER NOT NULL DEFAULT 0, notified_at TEXT,
              reminder_every REAL NOT NULL DEFAULT 4.0, kind TEXT NOT NULL DEFAULT 'question',
              link TEXT, answered_via TEXT, external_notified_at TEXT, external_ref TEXT
            );
            """
        )
        pool.execute(
            "INSERT INTO questions (id, session_key, text, origin, options_json, status) "
            "VALUES (284, ?, 'Выкатываем?', 'duty', ?, 'pending')",
            (f"t_{deal_id}", '["да", "нет"]'),
        )
        pool.commit()
    finally:
        pool.close()

    conn = journal.open_journal(home)
    try:
        journal.append(
            conn,
            [
                {
                    "ts": NOW - 30,
                    "recorded_at": NOW - 30,
                    "deal_id": deal_id,
                    "kind": "question_asked",
                    "payload": {
                        "question_id": 284,
                        "session_key": f"t_{deal_id}",
                        "routing": {"stage": "gate"},
                    },
                    "source": "ask",
                    "idempotency_key": "ask:284:question_asked",
                }
            ],
        )
    finally:
        conn.close()

    row = next(deal for deal in client.get("/now").json()["deals"] if deal["id"] == deal_id)
    waiting = row["waiting"]

    assert waiting["state"] == "wait_human"
    assert waiting["human_kind"] == "question"
    assert "вопрос #284" in waiting["evidence"]
    assert "варианты: да, нет" in waiting["evidence"]
    assert "/imsg" in waiting["evidence"]


def test_чужая_фаза_не_съедает_открытый_вызов():
    """Фаза вне {tool_use, tool_result} вызовов не описывает.

    `attempt_progress` пишет и `thinking`/`message`. Раньше любая такая запись
    считалась результатом: она закрывала открытый вызов (или делала вызов с
    пустым именем), и настоящий результат уже не находил пары.
    """
    calls, omitted = stagefeed._calls(
        [
            {"seq": 1, "phase": "tool_use", "name": "read_file", "ts": NOW},
            {"seq": 2, "phase": "thinking", "ts": NOW + 1},
            {"seq": 3, "phase": "tool_result", "name": "read_file", "ts": NOW + 2, "duration_ms": 1900},
        ]
    )
    assert omitted == 0
    # Начало считается назад от конца: (NOW + 2) − 1.9 s. Секунды журнала целые,
    # поэтому и результат дробный — это факт о точности журнала, не о тесте.
    assert calls == [{"seq": 1, "name": "read_file", "at": NOW + 0.1, "ms": 1900}]


def test_результат_с_чужим_именем_не_забирает_чужой_вызов():
    """Рассинхрон имён — сирота, а не повод закрыть другой вызов.

    Пара ищется по имени; если имени нет среди открытых, прежний `index = 0`
    приписывал `duration_ms` другому инструменту, и настоящий вызов исчезал.
    """
    calls, _ = stagefeed._calls(
        [
            {"seq": 1, "phase": "tool_use", "name": "read_file", "ts": NOW},
            {"seq": 2, "phase": "tool_result", "name": "terminal", "ts": NOW + 1, "duration_ms": 1000},
            {"seq": 3, "phase": "tool_result", "name": "read_file", "ts": NOW + 3, "duration_ms": 3000},
        ]
    )
    by_name = {call["name"]: call for call in calls}
    # Оба инструмента на месте, и длительности у каждого свои.
    assert by_name["read_file"]["ms"] == 3000
    assert by_name["terminal"]["ms"] == 1000
    assert by_name["terminal"]["at"] == NOW
