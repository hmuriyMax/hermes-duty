"""Лента этапов дела (W6): `stages` в `/deal`, этап и попытка в `/now`, read-only.

Карточка W6 требует три вещи, которые нельзя проверить глазами при ревью:

1. `/deal/{board}/{id}` отдаёт блок `stages`, где у каждой попытки есть факты
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
BOARD = "b1"

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


def make_board_home(home: Path) -> None:
    """kanban-доска с нужным слагом: своё дело заводится на существующей доске."""
    path = home / "kanban" / "boards" / BOARD / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(path).close()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    make_board_home(tmp_path)
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
    payload = {"board": BOARD, "title": "дело с лентой"}
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
                        "board": BOARD,
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

    body = client.get(f"/deal/{BOARD}/{deal_id}").json()

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

    body = client.get(f"/deal/{BOARD}/{deal_id}").json()

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

    body = client.get(f"/deal/{BOARD}/{deal_id}").json()

    assert body["stages"] == []
    assert body["spec"] is None
    assert "нет спеки" in body["stages_note"]
    # явный ?spec= даёт граф и делу без записи в журнале
    given = client.get(f"/deal/{BOARD}/{deal_id}", params={"spec": str(spec_file(home))}).json()
    assert [stage["id"] for stage in given["stages"]] == ["impl", "review"]


def test_deal_with_unreadable_spec_names_the_reason(client, home):
    """Спека не читается — лента пуста, но причина названа, а не спрятана."""
    deal_id = new_deal(client, home)

    body = client.get(
        f"/deal/{BOARD}/{deal_id}", params={"spec": str(home / "нет-такого.yaml")}
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

    body = client.get(f"/deal/{BOARD}/{deal_id}").json()
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

    attempt = client.get(f"/deal/{BOARD}/{deal_id}").json()["stages"][0]["attempts"][0]

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
                "INSERT INTO events (ts, recorded_at, deal_id, board, kind, payload, source,"
                " idempotency_key) VALUES (1, 1, 'd_x', 'b', 'created', '{}', 'test', 'k-probe')"
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
    assert client.get(f"/deal/{BOARD}/{deal_id}").status_code == 200
    assert client.get(f"/deal/{BOARD}/{deal_id}", params={"spec": str(spec_file(home))}).status_code == 200

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
