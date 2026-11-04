"""Уборка осиротевших попыток: попытка без конца закрывается фактом `infra`.

Ни один тест не поднимает процесс и не смотрит в `ps`: живость попытки в движке
определяется по журналу и сроку лизинга (`docs/PROBES.md`), и уборка живёт по
тому же правилу. Проверяется то, что она **делает**: какой факт появляется в
журнале, чего она не трогает и сходятся ли после неё две поверхности.

Правила карточки — по одному тесту на каждое:

1. живой лизинг — не трогать, даже если pid не отвечает;
2. повторный `sweep` не пишет второй `attempt_ended`;
3. запас TTL (`ttl + max(60, ttl/3)`) соблюдён: попытка моложе порога цела;
4. драйвер зовёт уборку перед решением — дело с сиротой трогается с места
   (регрессия ровно на кейс `d_18f1a6e5`);
5. `reaped: true` в payload — и `pid` из `attempt_started`;
6. класс `infra`: политика ретраев применяет `retry.on` как обычно;
7. `bin/duty reap --dry-run` ничего не пишет.

Плюс границы: чужая доска (перенос `attempt_started` из kanban) и чужой store не
задеваются, закрытая попытка не перезакрывается, а лизинг умершей попытки
отпускается — иначе следующая попытка упёрлась бы в строку, которой никто не
держит.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]

# Метка времени всех фактов: неподвижные часы делают порог проверяемым числом, а
# не «примерно через полчаса».
NOW = 1_790_000_000

# TTL лизинга по умолчанию (`runner.DEFAULT_TTL_MIN = 30`): порог уборки —
# 1800 + max(60, 1800/3) = 2400 с.
TTL_DEFAULT = 1800
THRESHOLD_DEFAULT = 2400


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


reaper = load_module("hermes_duty_reaper", "reaper.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
spec_module = load_module("hermes_duty_spec", "spec.py")
transit = load_module("hermes_duty_transit", "transit.py")
runner = load_module("hermes_duty_runner", "runner.py")
driver = load_module("hermes_duty_driver", "driver.py")
cli = load_module("hermes_duty_cli", "cli.py")

# Схема пула imessage-gateway — та, в которую пишет `ask()`: без неё прогон
# драйвера не дошёл бы до `wait_human`, а нам нужен прогон, а не вопрос.
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

# Цепочка из двух этапов: `impl` — тот, на котором попытка умирает, `verify` —
# доказательство того, что дело поехало дальше.
CHAIN = """
duty: probe
stages:
  - id: impl
    role: implementer
  - id: verify
    role: tester
    needs: [impl]
"""

# Та же цепочка, но `impl` не повторяется: так проверяется состояние `failed`
# (`retry.max: 0`), а не «будет попытка 2».
NO_RETRY = """
duty: probe
stages:
  - id: impl
    role: implementer
    retry: {max: 0, on: [infra]}
  - id: verify
    role: tester
    needs: [impl]
"""

# Короткий бюджет этапа: TTL 60 с, порог 60 + max(60, 20) = 120 с. При TTL по
# умолчанию та же попытка была бы цела — это и есть проверка «срок из того же
# места, что у лизинга».
SHORT_BUDGET = """
duty: probe
stages:
  - id: impl
    role: implementer
    budget: {minutes: 1}
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


def make_deal(
    conn: sqlite3.Connection, *, spec_path: Optional[Path] = None, board: str = "duty"
) -> dict[str, Any]:
    """Своё дело в store; спека — в `created.payload.spec`, как у `duty create --spec`."""
    created = deals.create_deal(
        conn,
        board=board,
        title="Дело с осиротевшей попыткой",
        body="",
        known_boards=[board],
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
    """Факт в журнал руками: предыстория, которую уборка обязана прочитать."""
    journal.append(
        conn,
        [
            {
                "ts": ts,
                "recorded_at": NOW,
                "deal_id": str(deal["id"]),
                "board": str(deal["board"]),
                "kind": kind,
                "payload": payload,
                "source": "test",
                "idempotency_key": key,
            }
        ],
    )


def start_attempt(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    *,
    stage: str = "impl",
    attempt: int = 1,
    pid: int = 64511,
    started_at: int = NOW,
    lease_expires: Optional[int] = None,
) -> str:
    """`attempt_started` — начало попытки, за которым не пришёл конец."""
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


def take_lease(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    *,
    stage: str = "impl",
    attempt: int = 1,
    pid: int = 64511,
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


def leases(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT stage, pid, attempt, expires_at FROM leases WHERE deal_id = ?", (str(deal["id"]),)
    ).fetchall()
    return [dict(row) for row in rows]


def events(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    return journal.read_events(conn, str(deal["id"]))


def ends(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Закрытия попыток дела — в порядке журнала."""
    return [event for event in events(conn, deal) if event["kind"] == "attempt_ended"]


# ------------------------------------------------- 1. мёртвая попытка → `infra`


def test_orphan_with_expired_lease_is_closed_as_infra(tmp_path: Path) -> None:
    """Попытка без конца и с истёкшим лизингом закрывается `failed`/`infra`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    rows = reaper.sweep(conn, NOW + 2401)

    assert len(rows) == 1
    row = rows[0]
    assert row["deal"] == deal["id"]
    assert row["board"] == "duty"
    assert row["stage"] == "impl"
    assert row["attempt"] == 1
    assert row["outcome"] == "failed"
    assert row["kind"] == "infra"
    assert row["reaped"] is True
    assert row["written"] == 1
    # 2401 - 1800 = 601 с: срок назван числом, а не словом «давно».
    assert row["reason"] == "процесс попытки не отчитался: лизинг истёк 601с назад"

    closed = ends(conn, deal)
    assert len(closed) == 1
    payload = journal.payload_of(closed[0])
    assert payload["outcome"] == "failed"
    assert payload["kind"] == "infra"
    assert payload["reaped"] is True
    assert payload["run_id"] == "impl#1"
    # Источник факта — движок: «кто закрыл попытку» видно в журнале, а не только
    # в payload.
    assert closed[0]["source"] == "engine"


# --------------------------------------------------------- 1. живой лизинг цел


def test_live_lease_is_not_touched_even_with_dead_pid(tmp_path: Path) -> None:
    """Живой лизинг — не трогать: pid здесь не спрашивается вовсе (правило 1).

    Движок может быть на другой машине или в другом namespace, `ps` ему
    недоступен — поэтому pid в тесте заведомо несуществующий, и это ничего не
    меняет: срок лизинга ещё не вышел.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal, pid=64511)
    take_lease(conn, deal, pid=64511, expires_at=NOW + 5000)

    rows = reaper.sweep(conn, NOW + 3000)

    assert rows == []
    assert ends(conn, deal) == []
    # Лизинг живого этапа уборка не отпускает.
    assert leases(conn, deal)[0]["expires_at"] == NOW + 5000


# ------------------------------------------------------ 2. идемпотентность


def test_second_sweep_does_not_write_second_end(tmp_path: Path) -> None:
    """Повторный проход не пишет второй `attempt_ended` (признак — парность)."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    first = reaper.sweep(conn, NOW + 2401)
    second = reaper.sweep(conn, NOW + 2401)

    assert len(first) == 1
    assert second == []
    assert len(ends(conn, deal)) == 1


def test_closed_attempt_is_not_closed_again(tmp_path: Path) -> None:
    """Закрытая попытка не перезакрывается: её исход — факт, а не заготовка."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    run_id = start_attempt(conn, deal)
    seed(
        conn,
        deal,
        "attempt_ended",
        {"run_id": run_id, "stage": "impl", "attempt": 1, "outcome": "done"},
        key=f"test:{deal['id']}:end:{run_id}",
        ts=NOW + 5,
    )
    # Строка лизинга осталась (движок её не успел отпустить) — и это не повод
    # дописывать конец к уже закрытой попытке.
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    assert reaper.sweep(conn, NOW + 9000) == []

    closed = ends(conn, deal)
    assert len(closed) == 1
    assert journal.payload_of(closed[0])["outcome"] == "done"
    assert "reaped" not in journal.payload_of(closed[0])


# ------------------------------------------------------------ 3. запас TTL


def test_attempt_younger_than_threshold_is_left_alone(tmp_path: Path) -> None:
    """Попытка на секунду моложе порога не закрывается; на пороге — закрывается."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    assert reaper.sweep(conn, NOW + THRESHOLD_DEFAULT - 1) == []
    assert ends(conn, deal) == []

    assert len(reaper.sweep(conn, NOW + THRESHOLD_DEFAULT)) == 1


def test_stage_budget_sets_the_deadline(tmp_path: Path) -> None:
    """Срок берётся из того же места, что у лизинга: `budget.minutes` этапа.

    У этапа с `budget.minutes: 1` TTL 60 с, порог 120 с — и попытка 130-секундной
    давности уже сирота, хотя по TTL по умолчанию (2400 с) была бы цела.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, SHORT_BUDGET)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal, lease_expires=NOW + 60)
    take_lease(conn, deal, expires_at=NOW + 60)

    assert reaper.threshold_sec(60) == 120
    rows = reaper.sweep(conn, NOW + 130)

    assert len(rows) == 1
    assert "лизинг истёк 70с назад" in rows[0]["reason"]


# ------------------------------------------------- 5. payload: движок, не он


def test_reaped_payload_names_the_engine_and_keeps_pid(tmp_path: Path) -> None:
    """`reaped: true` и `pid` из `attempt_started` — и ни слова о падении."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal, pid=64511)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    reaper.sweep(conn, NOW + 2401)

    payload = journal.payload_of(ends(conn, deal)[0])
    assert payload["reaped"] is True
    assert payload["pid"] == 64511
    # Мы знаем только, что отчёта нет: «исполнитель упал» — не наш факт.
    assert payload["reason"].startswith("процесс попытки не отчитался")
    assert "details" not in payload
    assert "result" not in payload


# --------------------------------------------- 6. две поверхности сходятся


def test_surfaces_converge_after_sweep(tmp_path: Path) -> None:
    """После уборки `transit` и `fold` говорят одно: попытки нет, отказ `infra`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    # До уборки — ровно та дыра, из-за которой карточка заведена: срез говорит
    # «умер» (pid не отвечает), переход — «попытка идёт».
    before = journal.fold(events(conn, deal), now=NOW + 2401, pid_probe=lambda pid: False)
    assert before["state"] == "crashed"
    assert transit.next_stage(spec, events(conn, deal), NOW + 2401).what == transit.WAIT_ATTEMPT

    reaper.sweep(conn, NOW + 2401)

    states = transit.stage_states(spec, events(conn, deal))
    assert states["impl"].fail_kind == "infra"
    # `infra` повторяется по умолчанию (`retry.on: [infra]`, `max: 1`): этап не
    # `failed`, а ждёт следующей попытки — политика ретраев W7 применилась как
    # обычно, без особого случая для уборки.
    assert states["impl"].state == transit.PENDING
    assert "будет попытка 2" in states["impl"].reason
    assert states["impl"].failures == (("infra", 1),)

    after = journal.fold(events(conn, deal), now=NOW + 2401, pid_probe=lambda pid: False)
    # Ни «умер», ни «идёт»: живого факта нет, а последнее событие — конец
    # попытки. Срез больше не называет факт, которого нет в журнале.
    assert after["state"] is None
    assert after["reason"] == "no_live_fact"
    assert after["last_kind"] == "attempt_ended"

    decision = transit.next_stage(spec, events(conn, deal), NOW + 2401)
    assert decision.kind == transit.WAIT
    assert decision.what == transit.WAIT_RETRY
    assert decision.retry_at == NOW + 2401 + 30


def test_failed_state_when_policy_has_no_retries(tmp_path: Path) -> None:
    """Без права повтора тот же факт даёт `failed(infra)`, а не ожидание."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, NO_RETRY)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    reaper.sweep(conn, NOW + 2401)

    states = transit.stage_states(spec, events(conn, deal))
    assert states["impl"].state == transit.FAILED
    assert states["impl"].fail_kind == "infra"
    assert "попытки для этого класса исчерпаны" in states["impl"].reason


# ------------------------------------------------------------ 7. `--dry-run`


def test_sweep_dry_run_writes_nothing(tmp_path: Path) -> None:
    """`dry_run` показывает кандидата и не оставляет в журнале ни строки."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)
    before = len(events(conn, deal))

    rows = reaper.sweep(conn, NOW + 2401, dry_run=True)

    assert len(rows) == 1
    assert rows[0]["dry_run"] is True
    assert rows[0]["written"] == 0
    assert len(events(conn, deal)) == before
    assert ends(conn, deal) == []
    # Лизинг сухого прогона тоже не трогается: «показать» — не «сделать».
    assert len(leases(conn, deal)) == 1


def test_cli_reap_dry_run_then_reap(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """`bin/duty reap --dry-run` ничего не пишет; `bin/duty reap` закрывает."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert cli.main(["reap", "--dry-run"]) == 0
    dry = capsys.readouterr().out
    assert "УБОРКА (--dry-run)" in dry
    assert "было бы закрыто" in dry
    assert "impl" in dry and deal["id"] in dry

    check = open_store(home)
    assert ends(check, deal) == []
    check.close()

    # Настоящий проход идёт по тем же часам, что и `--dry-run`, поэтому кандидат
    # тот же: попытка уже старше порога (NOW + 2401 в обоих случаях не нужен —
    # реальное время прогона тоже давно прошло порог относительно NOW).
    assert cli.main(["reap"]) == 0
    real = capsys.readouterr().out
    assert "УБОРКА ·" in real
    assert "закрыто движком" in real

    check = open_store(home)
    closed = ends(check, deal)
    assert len(closed) == 1
    assert journal.payload_of(closed[0])["reaped"] is True
    check.close()


# ------------------------------------- 4. драйвер лечит сироту (кейс d_18f1a6e5)


def test_driver_heals_orphan_and_moves_on(tmp_path: Path) -> None:
    """Регрессия на кейс `d_18f1a6e5`: дело с осиротевшей попыткой едет дальше.

    Драйвер убит снаружи во время `impl`: в журнале `attempt_started` без конца,
    лизинг истёк. До уборки обе поверхности врут каждая по-своему — срез
    «crashed», переход «попытка идёт»; после — дело либо ждёт паузу перед
    повтором, либо поднимает следующую попытку, но **никогда** не стоит вечно.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    # Так это выглядело до уборки.
    assert transit.next_stage(spec, events(conn, deal), NOW + 3000).what == transit.WAIT_ATTEMPT

    # Первый запуск движка: уборка идёт до решения, поэтому «попытка идёт»
    # больше не ответ — это пауза перед повтором после отказа `infra`.
    first = driver.run(
        conn, deal, spec=spec, home=home, once=True, clock=lambda: NOW + 3000, spawn=NoSpawn()
    )
    assert first["outcome"] == "wait"
    assert first["decision"]["what"] == transit.WAIT_RETRY
    assert [row["run_id"] for row in first["reaped"]] == ["impl#1"]
    assert first["steps"] == []

    # Второй запуск после паузы: та же уборка уже нечего делать, а дело едет —
    # поднимается вторая попытка `impl`.
    second = driver.run(
        conn, deal, spec=spec, home=home, once=True, clock=lambda: NOW + 3200, spawn=NoSpawn()
    )
    assert second["reaped"] == []
    assert [(row["stage"], row["attempt"]) for row in second["steps"]] == [("impl", 2)]


class NoSpawn:
    """Спавн, который не должен вызываться: до шага дело не доходит — или он фейк.

    Второй запуск до шага доходит, поэтому спавн возвращает процесс-пустышку с
    готовым отчётом: проверяется уборка, а не поведение исполнителя.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], cwd: Path) -> "FakeProc":
        self.calls.append(list(cmd))
        contract = json.loads(
            Path(cmd[list(cmd).index("--query-file") + 1]).read_text(encoding="utf-8")
        )
        notes = Path(cwd) / "notes.txt"
        notes.write_text("сделано\n", encoding="utf-8")
        import hashlib

        report = {
            "status": "done",
            "summary": "готово",
            "artifacts": [
                {"path": notes.name, "sha256": hashlib.sha256(notes.read_bytes()).hexdigest()}
            ],
            "evidence": [],
        }
        target = Path(str(contract["report_to"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return FakeProc(pid=4200 + len(self.calls))


class FakeProc:
    """Минимум, который читает `runner.follow`: pid, конец, код выхода."""

    def __init__(self, *, pid: int, exit_code: Optional[int] = 0) -> None:
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


# --------------------------------------------------- границы: чужая доска, store


def test_kanban_observed_run_is_not_reaped(tmp_path: Path) -> None:
    """Перенос `attempt_started` из чужой доски уборка не закрывает.

    У наблюдаемого дела нет `stage`: `journal.ingest` пишет факт канаба как есть
    (`run_id` — номер рана доски). Дописывать к нему наш конец значило бы врать
    про доску, которую мы только читаем.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    foreign = {"id": "tabc123456", "board": "hermes-tickets"}
    seed(
        conn,
        foreign,
        "attempt_started",
        {"run_id": 42, "pid": 991},
        key="test:kanban:start:42",
    )
    before = len(events(conn, foreign))

    assert reaper.sweep(conn, NOW + 100_000) == []
    assert len(events(conn, foreign)) == before


def test_sweep_does_not_touch_another_store(tmp_path: Path) -> None:
    """Уборка работает в пределах своего store: соседний `duty.db` цел."""
    mine = make_home(tmp_path, "mine")
    other = make_home(tmp_path, "other")
    spec = write_spec(tmp_path, CHAIN)

    conn = open_store(mine)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal)
    take_lease(conn, deal, expires_at=NOW + TTL_DEFAULT)

    stranger_conn = open_store(other)
    stranger = make_deal(stranger_conn, spec_path=spec.path)
    start_attempt(stranger_conn, stranger)
    take_lease(stranger_conn, stranger, expires_at=NOW + TTL_DEFAULT)

    rows = reaper.sweep(conn, NOW + 2401)

    assert [row["deal"] for row in rows] == [deal["id"]]
    assert ends(stranger_conn, stranger) == []
    assert len(leases(stranger_conn, stranger)) == 1

    # И наоборот: соседний store убирается своим проходом, а не нашим.
    assert [row["deal"] for row in reaper.sweep(stranger_conn, NOW + 2401)] == [stranger["id"]]


# ------------------------------------------------ лизинг умершей попытки отпущен


def test_dead_attempt_lease_is_released(tmp_path: Path) -> None:
    """Строка лизинга умершей попытки удаляется: этап свободен для следующей.

    Строка с истёкшим сроком не блокирует `_take` (условие в SQL — по сроку), но
    оставлять её значит держать в store факт «этап за процессом», которого нет.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_attempt(conn, deal, pid=64511)
    take_lease(conn, deal, pid=64511, expires_at=NOW + TTL_DEFAULT)

    reaper.sweep(conn, NOW + 2401)

    assert leases(conn, deal) == []
    # Этап действительно свободен: следующая попытка берёт лизинг сразу.
    assert runner.lease(conn, deal=deal, stage="impl", attempt=2, pid=777, ttl=60, now=NOW + 2401)


def test_reap_reports_empty_store_instead_of_creating_it(tmp_path: Path) -> None:
    """`reap` по store без файла — «убирать нечего», и файла после него нет."""
    home = tmp_path / "bare"
    home.mkdir()

    report = reaper.reap(home)

    assert report["count"] == 0
    assert report["reaped"] == []
    assert "store ещё нет" in report["note"]
    assert not journal.journal_path(home).exists()
