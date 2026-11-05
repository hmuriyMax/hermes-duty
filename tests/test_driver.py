"""Драйвер дела: цикл «решение → шаг → перечитать журнал» без живой модели.

Ни один тест не поднимает `hermes chat`: спавн подменён фейком (`Fake`), который
пишет отчёт и пакет так же, как это сделал бы исполнитель. Проверяется то, что
драйвер **делает**: какие этапы он запускает и в каком порядке, каким кодом
выходит и что остаётся в журнале и в таблице лизингов.

Правила карточки проверяются по одному:

1. один шаг — один факт: прогон, остановленный после `impl`, при повторном запуске
   идёт в `verify`, а не в `impl` заново;
2. `wait` — выход, а не занятая петля: код 3 и названное ожидание (вопрос,
   `retry_at`, чужой лизинг);
3. `--once` — ровно один шаг;
4. предел шагов — код 4, дело не тронуто;
5. процессы не остаются сиротами: SIGINT гасит ребёнка, лизинг отпускается;
6. человеческий вывод — строка на шаг, `--json` — машинная форма;
7. ответы пула дочитываются сами: `ask.sync_answers` идёт после `reaper.sweep` и
   до решения, поэтому `bin/duty run --once` забирает ответ без ручного синка
   (регрессия ровно на кейс `d_a2a26b55`), а `errors[]` синка не глотаются.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import sqlite3
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Метка времени всех фактов: неподвижные часы делают прогон воспроизводимым, а
# `retry_at` (пауза политики) — проверяемым числом, а не «примерно через полминуты».
NOW = 1_790_000_000


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


driver = load_module("hermes_duty_driver", "driver.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
spec_module = load_module("hermes_duty_spec", "spec.py")
transit = load_module("hermes_duty_transit", "transit.py")
runner = load_module("hermes_duty_runner", "runner.py")
cli = load_module("hermes_duty_cli", "cli.py")

# Схема пула imessage-gateway — та, в которую пишет `ask()`: без неё вопрос
# человеку записать некуда, а драйвер обязан доходить до `wait_human`.
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

# Три этапа подряд: ровно та цепочка, которую карточка называет гейтом P2.
# Дерево выключено явно (`workspace: none`): драйвер проверяется без git, а
# умолчание этапа с `needs` — `worktree`, которому нужны `repo`/`base`.
CHAIN = """
duty: probe
stages:
  - id: impl
    role: implementer
  - id: verify
    role: tester
    needs: [impl]
    workspace: none
  - id: review
    role: reviewer
    needs: [verify]
    workspace: none
"""


# ------------------------------------------------------------------ фикстуры


def make_home(tmp_path: Path) -> Path:
    """Корень Hermes: своя `duty/duty.db` (создаст store) и свой пул вопросов."""
    home = tmp_path / "home"
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


def write_spec(tmp_path: Path, text: str) -> Any:
    """Спека на диске и загруженная: путь абсолютный — так её и читает движок."""
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def make_deal(conn: sqlite3.Connection, *, spec_path: Optional[Path] = None) -> dict[str, Any]:
    """Своё дело в store; спека — в `created.payload.spec`, как у `duty create --spec`."""
    created = deals.create_deal(
        conn,
        board="duty",
        title="Довести дело по спеке",
        body="",
        known_boards=["duty"],
        spec_path=str(spec_path) if spec_path is not None else None,
        now=NOW,
    )
    return deals.deal_row(conn, str(created["deal"]["id"]))


def events(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    return journal.read_events(conn, str(deal["id"]))


def kinds(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[str]:
    return [str(event["kind"]) for event in events(conn, deal)]


def seed(conn: sqlite3.Connection, deal: dict[str, Any], kind: str, payload: dict[str, Any], *, key: str, ts: int = NOW) -> None:
    """Факт в журнал руками: предыстория, которую драйвер обязан прочитать.

    `ts` — время факта: предыстория бывает старше прогона, и тогда пауза политики
    повтора (`retry_at`) уже прошла — иначе тест проверял бы ожидание паузы вместо
    того правила, ради которого факт положен.
    """
    journal.append(
        conn,
        [
            {
                "ts": ts,
                "recorded_at": ts,
                "deal_id": str(deal["id"]),
                "board": str(deal["board"]),
                "kind": kind,
                "payload": payload,
                "source": "test",
                "idempotency_key": key,
            }
        ],
    )


def leases(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Строки лизинга дела: после прогона их быть не должно ни в одном исходе."""
    rows = conn.execute(
        "SELECT stage, pid, attempt, expires_at FROM leases WHERE deal_id = ?", (str(deal["id"]),)
    ).fetchall()
    return [dict(row) for row in rows]


def answer_in_pool(
    home: Path,
    question_id: int,
    *,
    status: str = "answered",
    answer: str = "режем скоуп",
    session_key: Optional[str] = None,
) -> None:
    """Закрыть вопрос пула так, как это делает сам пул (`msgq resolve`/срок).

    Пишет в свою фикстуру, а не в живой пул: ответ человека приходит от демона
    моста, и тест изображает ровно его эффект — `answered` с `answered_at` и
    `answered_via`, либо `expired` без ответа. `session_key` перекрывается для
    случая «пул закрыл, а ключ не разобран»: это `errors[]` синка, не ответ.
    """
    db = home / "messaging" / "pool.db"
    pool = sqlite3.connect(db)
    try:
        if status == "answered":
            # `answered_at` — настоящий момент ответа: `question_asked` движок
            # пишет живыми часами, и время ответа из прошлого переупорядочило бы
            # журнал (ответ раньше вопроса), и fold снял бы нечего.
            answered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            pool.execute(
                "UPDATE questions SET status='answered', answer=?, answered_via='manual', "
                "answered_at=? WHERE id = ?",
                (answer, answered_at, int(question_id)),
            )
        else:
            pool.execute("UPDATE questions SET status=? WHERE id = ?", (status, int(question_id)))
        if session_key is not None:
            pool.execute("UPDATE questions SET session_key = ? WHERE id = ?", (session_key, int(question_id)))
        pool.commit()
    finally:
        pool.close()


def asked_question_id(conn: sqlite3.Connection, deal: dict[str, Any]) -> int:
    """Номер вопроса пула по последнему `question_asked` журнала дела."""
    for event in reversed(events(conn, deal)):
        if event["kind"] == "question_asked":
            return int(journal.payload_of(event)["question_id"])
    raise AssertionError("в журнале нет question_asked: вопроса не было")


class Proc:
    """Один фейковый процесс попытки: тот минимум, что читает `runner.follow`."""

    def __init__(self, *, pid: int, exit_code: Optional[int], hang: bool = False, hook: Any = None) -> None:
        self.pid = pid
        self.exit_code = exit_code
        self.hang = hang
        self.stopped = 0
        self.stderr_text = ""
        self._hook = hook

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        # Долгожитель сам просит прерывания: так тест SIGINT детерминирован, а не
        # «через полсекунды, если успело».
        if self._hook is not None:
            hook, self._hook = self._hook, None
            hook()
        return None

    def finished(self) -> bool:
        return not self.hang or bool(self.stopped)

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.exit_code

    def stop(self, grace: float = 10) -> Optional[int]:
        self.stopped += 1
        if self.exit_code is None:
            self.exit_code = -15
        return self.exit_code


class Fake:
    """Фейковый спавн: по плану этапа пишет отчёт (с пакетом) или висит.

    План — `{этап: ok|fail|ask|hang}`; `ok` кладёт в рабочий каталог артефакт и
    пишет отчёт с его настоящим `sha256`, поэтому evidence-гейт проходит на тех же
    правилах, что и с живым исполнителем, а не на подставленном «совпало».
    """

    def __init__(self, plan: Optional[dict[str, str]] = None, *, interrupt: bool = False) -> None:
        self.plan = dict(plan or {})
        self.interrupt = interrupt
        self.calls: list[dict[str, Any]] = []
        self.processes: list[Proc] = []

    def __call__(self, cmd: list[str], cwd: Path) -> Proc:
        contract = json.loads(Path(cmd[list(cmd).index("--query-file") + 1]).read_text(encoding="utf-8"))
        stage = str(contract["stage"])
        attempt = int(contract["attempt"])
        action = self.plan.get(stage, "ok")
        self.calls.append({"stage": stage, "attempt": attempt, "action": action, "cwd": Path(cwd)})
        pid = 4200 + len(self.processes)
        if action == "hang":
            hook = (lambda: os.kill(os.getpid(), signal.SIGINT)) if self.interrupt else None
            process = Proc(pid=pid, exit_code=None, hang=True, hook=hook)
        elif action == "fail":
            # Процесс упал сам и не отчитался: класс `infra` (источник — код
            # выхода), а не `contract`: отказ по отчёту — другой класс и другая
            # политика, и путать их в тесте нельзя.
            process = Proc(pid=pid, exit_code=1)
        elif action == "ask":
            process = Proc(pid=pid, exit_code=0)
            self._report(
                contract,
                {
                    "status": "blocked",
                    "summary": "нужно решение",
                    "question": {"text": "режем скоуп?", "options": ["режем", "чиним"]},
                },
            )
        else:
            process = Proc(pid=pid, exit_code=0)
            self._report(contract, self._packed(Path(cwd)))
        self.processes.append(process)
        return process

    @property
    def stopped(self) -> int:
        return sum(process.stopped for process in self.processes)

    @staticmethod
    def _report(contract: dict[str, Any], report: dict[str, Any]) -> None:
        target = Path(str(contract["report_to"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _packed(cwd: Path) -> dict[str, Any]:
        notes = cwd / "notes.txt"
        notes.write_text("сделано\n", encoding="utf-8")
        return {
            "status": "done",
            "summary": "готово",
            "artifacts": [{"path": notes.name, "sha256": hashlib.sha256(notes.read_bytes()).hexdigest()}],
            "evidence": [],
        }


def run(conn: sqlite3.Connection, spec: Any, deal: dict[str, Any], home: Path, fake: Fake, **kwargs: Any) -> dict[str, Any]:
    """Прогон с подменённым спавном и неподвижными часами."""
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("spawn", fake)
    return driver.run(conn, deal, spec=spec, home=home, **kwargs)


def deal_state(conn: sqlite3.Connection, deal: dict[str, Any], spec: Any) -> str:
    """Состояние дела словами журнала — то, что увидит консоль после прогона."""
    return str(journal.fold(journal.read_events(conn, str(deal["id"]))).get("state"))


# -------------------------------------------------------------- линейная цепочка


def test_linear_chain_impl_verify_review_done(tmp_path: Path) -> None:
    """Линейная цепочка проходит целиком: impl → verify → review → done, код 0."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert report["outcome"] == "done"
    assert report["exit_code"] == 0
    assert [(call["stage"], call["attempt"]) for call in fake.calls] == [
        ("impl", 1),
        ("verify", 1),
        ("review", 1),
    ]
    assert [row["stage"] for row in report["steps"]] == ["impl", "verify", "review"]
    assert [row["outcome"] for row in report["steps"]] == ["done", "done", "done"]
    assert kinds(conn, deal).count("attempt_started") == 3
    assert kinds(conn, deal).count("attempt_ended") == 3
    assert report["decision"]["kind"] == "done"


def test_step_line_says_stage_attempt_and_outcome(tmp_path: Path) -> None:
    """Человеческая строка шага: этап, номер попытки, исход и длительность."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    seen: list[dict[str, Any]] = []

    report = run(conn, spec, deal, home, Fake(), on_step=seen.append)

    assert len(seen) == 3, "on_step зовётся на каждый шаг, не на прогон целиком"
    line = cli.render_run_step(seen[0])
    assert line == "impl · попытка 1 · ok (0.0s)", line
    assert report["steps"][0]["duration_sec"] == 0.0
    # Итог — одной строкой, как обещает карточка.
    assert cli.render_run_final(report).startswith("итог: дело закрыто")


def test_failed_step_names_class_and_reason(tmp_path: Path) -> None:
    """Строка упавшего шага: класс отказа и причина, а не слово «ошибка»."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)

    report = run(conn, spec, deal, home, Fake({"impl": "fail"}))
    row = report["steps"][0]

    assert row["outcome"] == "failed"
    assert row["kind"] == "infra"
    line = cli.render_run_step(row)
    assert line.startswith("impl · попытка 1 · infra: "), line


# ---------------------------------------------------- правило 1: журнал = истина


def test_rerun_after_impl_continues_with_verify(tmp_path: Path) -> None:
    """Прогон, остановленный после `impl`, при повторном запуске идёт в `verify`.

    Драйвер не помнит состояния: второй процесс спрашивает журнал и получает
    следующее место в графе. Попытки `impl` заново не появляется.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake()

    first = run(conn, spec, deal, home, fake, once=True)
    assert first["outcome"] == "step"
    assert [call["stage"] for call in fake.calls] == ["impl"]

    second = run(conn, spec, deal, home, fake, once=True)
    assert second["steps"][0]["stage"] == "verify"
    assert [call["stage"] for call in fake.calls] == ["impl", "verify"]

    third = run(conn, spec, deal, home, fake, once=True)
    assert third["steps"][0]["stage"] == "review"

    # Четвёртый прогон делать нечего: журнал говорит «закрыто».
    fourth = run(conn, spec, deal, home, fake)
    assert fourth["outcome"] == "done"
    assert [call["stage"] for call in fake.calls] == ["impl", "verify", "review"]
    starts = [event for event in events(conn, deal) if event["kind"] == "attempt_started"]
    assert [event["payload"]["stage"] for event in starts] == ["impl", "verify", "review"]


# --------------------------------------------------------- правило 3: `--once`


def test_once_makes_exactly_one_step(tmp_path: Path) -> None:
    """`--once` делает ровно один шаг и выходит: код 0, в журнале одна попытка."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake()

    report = run(conn, spec, deal, home, fake, once=True)

    assert report["outcome"] == "step"
    assert report["exit_code"] == 0
    assert len(fake.calls) == 1
    assert kinds(conn, deal).count("attempt_ended") == 1
    assert report["decision"]["kind"] == "run", "решение после шага названо, а не прогнано"
    assert cli.render_run_final(report).startswith("итог: сделан один шаг")


# ------------------------------------------------------- правило 4: предел шагов


def test_max_steps_two_on_long_chain_exits_4(tmp_path: Path) -> None:
    """Предел шагов: третий шаг не запускается, код 4, дело не тронуто дальше."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake()

    report = run(conn, spec, deal, home, fake, max_steps=2)

    assert report["outcome"] == "limit"
    assert report["exit_code"] == 4
    assert [call["stage"] for call in fake.calls] == ["impl", "verify"]
    assert kinds(conn, deal).count("attempt_ended") == 2, "review не запускался"
    assert report["decision"]["kind"] == "run", "решение осталось тем же — продолжают с него"
    assert cli.render_run_final(report).startswith("итог: предел шагов")


# ------------------------------------------------ правило 2: `wait` — это выход


def test_wait_question_exits_3_and_names_question(tmp_path: Path) -> None:
    """Вопрос человеку: прогон выходит с кодом 3 и называет номер вопроса."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"impl": "ask"})

    report = run(conn, spec, deal, home, fake)

    assert report["outcome"] == "wait"
    assert report["exit_code"] == 3
    assert report["decision"]["kind"] == "wait"
    assert report["decision"]["what"] == "question"
    question_id = report["steps"][0]["question_id"]
    assert question_id, "вопрос задан и его номер попал в отчёт"
    assert f"#{question_id}" in report["note"]
    assert f"#{question_id}" in cli.render_run_final(report)
    assert deal_state(conn, deal, spec) == "wait_human"
    assert len(fake.calls) == 1, "второй попытки до ответа нет"


def test_wait_retry_exits_3_and_names_retry_at(tmp_path: Path) -> None:
    """Пауза политики: код 3, `retry_at` назван, повтор «сейчас же» не выдаётся."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            retry: {max: 2, on: [infra]}
        """,
    )
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"impl": "fail"})

    report = run(conn, spec, deal, home, fake)

    assert report["outcome"] == "wait"
    assert report["exit_code"] == 3
    assert report["decision"]["what"] == "retry"
    assert report["decision"]["retry_at"] == NOW + 30, "пауза политики от времени отказа"
    assert str(NOW + 30) in cli.render_run_final(report)
    assert len(fake.calls) == 1, "до retry_at повтор не запускается"


def test_busy_lease_exits_3_instead_of_spinning(tmp_path: Path) -> None:
    """Чужой живой лизинг: попытка не поднимается, прогон уступает и выходит (код 3)."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    assert runner.lease(conn, deal=deal, stage="impl", attempt=1, pid=9999, ttl=600, now=NOW)
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert report["outcome"] == "wait"
    assert report["exit_code"] == 3
    assert fake.calls == [], "чужой этап не спавнится"
    assert "9999" in report["note"]
    assert report["steps"][0]["started"] is False


def test_park_exits_2_and_deal_stays_wait_human(tmp_path: Path) -> None:
    """Припаркованный этап (вопрос закрыт сроком): код 2, дело остаётся `wait_human`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    seed(conn, deal, "question_asked", {"stage": "impl", "attempt": 1, "question_id": 77}, key="q-asked")
    # Блокировка — тот же факт, что пишет движок, задавая вопрос: без неё дело
    # выглядело бы «ничего не ждёт», хотя вопрос задан и срок вышел.
    seed(
        conn,
        deal,
        "blocked",
        {"block_kind": "needs_input", "reason": "попытка impl#1: вопрос #77 адресован human — без ответа"},
        key="q-blocked",
    )
    seed(
        conn,
        deal,
        "question_answered",
        {"stage": "impl", "question_id": 77, "answered_via": "expired", "answer_len": 0},
        key="q-expired",
    )
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert report["outcome"] == "park"
    assert report["exit_code"] == 2
    assert report["decision"]["question_id"] == "77"
    assert fake.calls == [], "парковку уже записали, повторять нечего"
    assert deal_state(conn, deal, spec) == "wait_human"


def test_failure_without_park_exits_1(tmp_path: Path) -> None:
    """Отказ, который не паркуется: код 1 и класс отказа в решении."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    seed(
        conn,
        deal,
        "attempt_ended",
        {"stage": "impl", "attempt": 1, "outcome": "failed", "kind": "capability"},
        key="cap-1",
    )
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert report["outcome"] == "failed"
    assert report["exit_code"] == 1
    assert report["decision"]["fail_kind"] == "capability"
    assert fake.calls == []


# --------------------------------------- правило 5: дети и лизинг при выходе


def test_sigint_terminates_child_and_exits_130(tmp_path: Path) -> None:
    """SIGINT: ребёнок получает terminate, лизинг отпущен, код 130."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"impl": "hang"}, interrupt=True)

    report = run(conn, spec, deal, home, fake)

    assert report["outcome"] == "interrupted"
    assert report["exit_code"] == 130
    assert fake.stopped == 1, "долгожитель получил terminate, а не остался сиротой"
    assert leases(conn, deal) == []
    assert cli.render_run_final(report).startswith("итог: прервано")


def test_leases_released_after_done(tmp_path: Path) -> None:
    """После закрытого дела лизингов нет: следующий прогон не увидит чужой."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)

    run(conn, spec, deal, home, Fake())

    assert leases(conn, deal) == []


def test_leases_released_after_wait(tmp_path: Path) -> None:
    """После ожидания ответа лизингов тоже нет: дело стоит на человеке, не на процессе."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)

    run(conn, spec, deal, home, Fake({"impl": "ask"}))

    assert leases(conn, deal) == []


def test_leases_released_after_limit(tmp_path: Path) -> None:
    """Предел шагов не оставляет занятого этапа: продолжение возможно сразу."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)

    run(conn, spec, deal, home, Fake(), max_steps=2)

    assert leases(conn, deal) == []


# --------------------------------------------------- правило 6: `--json`-схема


def test_json_report_schema(tmp_path: Path) -> None:
    """Машинная форма: дело, спека, шаги и финальное решение — одной схемой."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)

    report = run(conn, spec, deal, home, Fake())
    # Отчёт обязан переживать `json.dumps` без посторонних объектов.
    restored = json.loads(json.dumps(report, ensure_ascii=False))

    assert set(restored) == {
        "deal",
        "board",
        "spec",
        "steps",
        "reaped",
        "answers",
        "answers_errors",
        "outcome",
        "note",
        "decision",
        "closed",
        "parked",
        "exit_code",
    }
    assert restored["deal"] == deal["id"]
    assert restored["spec"] == str(spec.path)
    # Уборки в чистом прогоне нет: список пуст, а не отсутствует — ключ есть
    # всегда, иначе читатель `--json` не отличил бы «нечего убирать» от
    # «поле забыли».
    assert restored["reaped"] == []
    # Ответов пула в чистом прогоне не было — но ключи есть: «нечего дочитывать»
    # и «синк не звали» читатель `--json` обязан различать.
    assert restored["answers"] == {"appended": 0}
    assert restored["answers_errors"] == []
    # Закрытие — не строка вывода, а факт: в отчёте он назван той же формой, что
    # читает консоль, и `--json` не отличается от человеческой строки.
    assert set(restored["closed"]) == {
        "written",
        "already",
        "cause",
        "at",
        "stages",
        "status",
        "refused",
    }
    assert restored["closed"]["written"] is True
    assert restored["closed"]["cause"] == "all_stages_done"
    assert set(restored["steps"][0]) == {
        "n",
        "stage",
        "attempt",
        "started",
        "outcome",
        "kind",
        "reason",
        "question_id",
        "stage_kind",
        "refused",
        "base",
        "commit",
        "duration_sec",
    }
    assert set(restored["decision"]) == {
        "kind",
        "stages",
        "attempts",
        "what",
        "fail_kind",
        "question_id",
        "reason",
        "retry_at",
    }
    assert isinstance(restored["decision"]["stages"], list)


# ------------------------------------------------------------- вход `drive`


def test_drive_takes_spec_from_deal_journal(tmp_path: Path) -> None:
    """`drive` без `--spec` берёт путь из журнала дела и доводит его до конца."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    conn.close()
    fake = Fake()

    report = driver.drive(home, deal["id"], spawn=fake, clock=lambda: NOW)

    assert report["spec"] == str(spec.path)
    assert report["outcome"] == "done"
    assert [call["stage"] for call in fake.calls] == ["impl", "verify", "review"]


def test_drive_unknown_deal_is_not_found(tmp_path: Path) -> None:
    """Неизвестное дело — «дела нет» (код 1 у CLI), а не пустой прогон."""
    home = make_home(tmp_path)
    open_store(home).close()

    with pytest.raises(deals.DealNotFound):
        driver.drive(home, "d_abcdef12", spawn=Fake(), clock=lambda: NOW)


def test_drive_without_spec_names_the_reason(tmp_path: Path) -> None:
    """Дело без спеки: `DriverError` с подсказкой, до первого шага (код 2 у CLI)."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    conn.close()

    with pytest.raises(driver.DriverError) as caught:
        driver.drive(home, deal["id"], spawn=Fake(), clock=lambda: NOW)
    assert "нет спеки" in str(caught.value)


# ------------------------------------- спека проверяется до первого процесса
#
# `docs/SPEC.md`: каждое правило спеки — отказ, случающийся **раньше первого
# процесса**. До FIX-4 `drive()` читал спеку через `load_spec()` и отдавал её
# движку как есть: цикл в графе давал вечный `wait("gate")` вместо отказа.

# Та же тройка отказов, которую проверяет `bin/duty spec check` (`tests/test_spec.py`).
BAD_SPECS = {
    "cycle": """
        duty: probe
        stages:
          - id: impl
            role: implementer
            needs: [review]
            workspace: none
          - id: review
            role: reviewer
            needs: [impl]
            workspace: none
        """,
    "verdict.changes": """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: review
            role: reviewer
            needs: [impl]
            workspace: none
            verdict: {approve: next, changes: publish, max_rounds: 2}
          - id: publish
            role: publisher
            needs: [review]
            workspace: none
        """,
    "review-gate": """
        duty: probe
        stages:
          - id: impl
            role: implementer
          - id: publish
            role: publisher
            needs: [impl]
            workspace: none
        """,
}


@pytest.mark.parametrize("rule", sorted(BAD_SPECS))
def test_drive_refuses_invalid_spec_before_the_first_process(tmp_path: Path, rule: str) -> None:
    """Негодная спека — отказ `drive()`, а не первый шаг: в журнале нет попытки.

    Отказ называется тем же `file:line: правило: текст`, что у `spec check`, — за
    это отвечает сверка ниже; здесь важно, что прогон не начался вовсе.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, BAD_SPECS[rule])
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake()

    with pytest.raises(driver.DriverError) as caught:
        driver.drive(home, deal["id"], spawn=fake, clock=lambda: NOW)

    assert caught.value.code == driver.EXIT_FAILED, "код 1 — как у `spec check`"
    assert f"{spec.path}:" in str(caught.value)
    assert rule in str(caught.value)
    assert fake.calls == [], "процесс не спавнился"
    assert "attempt_started" not in kinds(conn, deal)
    assert leases(conn, deal) == []
    conn.close()


@pytest.mark.parametrize("rule", sorted(BAD_SPECS))
def test_drive_refusal_is_the_same_as_spec_check(tmp_path: Path, rule: str) -> None:
    """Строки отказа и код — те же, что у `bin/duty spec check` на том же файле."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, BAD_SPECS[rule])
    deal = make_deal(conn, spec_path=spec.path)
    conn.close()

    with pytest.raises(driver.DriverError) as caught:
        driver.drive(home, deal["id"], spawn=Fake(), clock=lambda: NOW)

    payload = cli.spec_report(str(spec.path))
    assert cli.spec_exit_code(payload) == caught.value.code
    rendered = cli.render_spec_check(payload)
    for line in str(caught.value).splitlines():
        assert line in rendered, line


def test_drive_unreadable_spec_exits_2_like_spec_check(tmp_path: Path) -> None:
    """Нечитаемая спека — код 2 (как `spec check`), а не список отказов."""
    home = make_home(tmp_path)
    conn = open_store(home)
    path = tmp_path / "duty.yaml"
    path.write_text("duty: probe\nstages: [\n", encoding="utf-8")
    deal = make_deal(conn, spec_path=path)
    conn.close()

    with pytest.raises(driver.DriverError) as caught:
        driver.drive(home, deal["id"], spawn=Fake(), clock=lambda: NOW)

    assert caught.value.code is None, "общий код 2 — как у нечитаемой спеки в `spec check`"
    assert f"{path}:" in str(caught.value) and "yaml" in str(caught.value)
    assert cli.spec_exit_code(cli.spec_report(str(path))) == 2


# ------------------------------------------------------------------ CLI `run`


def _cli_run(monkeypatch: pytest.MonkeyPatch, home: Path, args: list[str]) -> int:
    """`bin/duty run` без процесса: подмена спавна живёт в тестах драйвера."""
    monkeypatch.setenv("HERMES_HOME", str(home))
    return cli.main(args)


def test_cli_run_closes_ready_deal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Закрытое дело: `duty run` говорит «уже закрыто» и выходит с кодом 0 (без спавна).

    Дело закрыто до прогона — второй `completed` не пишется, и что прогон ничего
    не сделал, названо словом «уже», а не приписано себе (W21).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    seed(conn, deal, "completed", {"summary": "влито"}, key="done-1")

    code = _cli_run(monkeypatch, home, ["run", deal["id"]])

    assert code == 0
    out = capsys.readouterr().out
    assert "итог: дело уже закрыто" in out
    assert "completed" in out and "(шагов: 0)" in out
    assert kinds(conn, deal).count("completed") == 1


def test_cli_run_parked_deal_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Припаркованное дело: код 2 и номер вопроса в итоге."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    seed(conn, deal, "question_asked", {"stage": "impl", "attempt": 1, "question_id": 91}, key="q91-asked")
    seed(
        conn,
        deal,
        "question_answered",
        {"stage": "impl", "question_id": 91, "answered_via": "expired", "answer_len": 0},
        key="q91-expired",
    )

    code = _cli_run(monkeypatch, home, ["run", deal["id"]])

    assert code == 2
    out = capsys.readouterr().out
    assert "#91" in out
    assert "припарковано" in out


def test_cli_run_refuses_invalid_spec_with_file_line_and_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Негодная спека: `run` отказывает кодом 1 и называет `file:line: правило`.

    Тот же код и та же строка, что у `bin/duty spec check`: отказ один на оба
    входа, иначе скрипт, проверяющий спеку заранее, и прогон говорили бы разное.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, BAD_SPECS["cycle"])
    deal = make_deal(conn, spec_path=spec.path)
    conn.close()

    code = _cli_run(monkeypatch, home, ["run", deal["id"]])

    captured = capsys.readouterr()
    assert code == cli.spec_exit_code(cli.spec_report(str(spec.path))) == 1
    assert "cycle" in captured.err
    assert f"{spec.path}:" in captured.err
    assert captured.out == "", "до первого шага дела нет — и строк шагов тоже"


def test_cli_run_json_refusal_is_a_line_in_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`run --json` на негодной спеке: код 1 и строка отказа в `errors[]`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, BAD_SPECS["review-gate"])
    deal = make_deal(conn, spec_path=spec.path)
    conn.close()

    code = _cli_run(monkeypatch, home, ["run", deal["id"], "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["deal"] == deal["id"]
    assert payload["exit_code"] == 1
    assert payload["errors"], payload
    assert all(line.startswith(f"{spec.path}:") for line in payload["errors"]), payload["errors"]
    assert any("review-gate" in line for line in payload["errors"])


def test_cli_run_json_prints_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`--json` печатает отчёт целиком: без строк шагов и без лишнего текста."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    seed(conn, deal, "completed", {"summary": "влито"}, key="done-2")

    code = _cli_run(monkeypatch, home, ["run", deal["id"], "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "done"
    assert payload["deal"] == deal["id"]
    assert payload["steps"] == []


# ------------------------------------ правило 7: движок сам дочитывает ответы пула


def start_orphan(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    *,
    stage: str = "impl",
    attempt: int = 1,
    ttl: int = 1800,
) -> None:
    """Попытка без конца и живой лизинг, который истечёт: сырьё для `reaper.sweep`.

    Уборка судит по журналу и сроку лизинга (`docs/PROBES.md`), поэтому сирота
    собирается теми же двумя фактами, что пишет движок: `attempt_started` и
    строка `leases`. Процесса нет вовсе — и это ровно случай «прогон убили».
    """
    seed(
        conn,
        deal,
        "attempt_started",
        {
            "run_id": f"{stage}#{attempt}",
            "stage": stage,
            "attempt": attempt,
            "pid": 64511,
            "lease_expires": NOW + ttl,
            "workdir": f"/tmp/{deal['id']}-{stage}-{attempt}",
        },
        key=f"orphan:{deal['id']}:{stage}#{attempt}",
    )
    with conn:
        conn.execute(
            "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(deal_id, stage) DO UPDATE SET pid = excluded.pid, expires_at = excluded.expires_at",
            (str(deal["id"]), stage, int(attempt), 64511, NOW, NOW + ttl),
        )


def test_once_picks_up_pool_answer_without_manual_sync(tmp_path: Path) -> None:
    """Регрессия `d_a2a26b55`: ответ в пуле забирает сам `--once`, без ручного синка.

    До W15 движок ответов пула не читал вовсе: человек отвечал, `run --once`
    снова говорил «ждём вопрос #N», и дело стояло в `wait_human` навсегда.
    Здесь между двумя прогонами нет ни одного вызова `sync_answers` — только
    строка пула, закрытая штатным путём пула.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"impl": "ask"})

    # Живые часы: `question_asked` и `question_answered` пишутся настоящим
    # временем пула, а журнал применяет факты по `ts` — с неподвижными часами
    # ответ из будущего переупорядочил бы попытки и снял бы не тот факт.
    first = run(conn, spec, deal, home, fake, once=True, clock=time.time)
    assert first["outcome"] == "step", "--once делает шаг и выходит"
    assert first["decision"]["what"] == "question"
    question_id = first["steps"][0]["question_id"]
    assert question_id, "вопрос задан и его номер попал в отчёт"
    assert deal_state(conn, deal, spec) == "wait_human"
    assert "question_answered" not in kinds(conn, deal)

    answer_in_pool(home, question_id)
    fake.plan["impl"] = "ok"

    second = run(conn, spec, deal, home, fake, once=True, clock=time.time)

    assert second["answers"]["appended"] == 1, "движок дочитал ответ пула сам"
    assert second["answers_errors"] == []
    assert kinds(conn, deal).count("question_answered") == 1
    # Ответ снимает и блок, который держал вопрос: иначе срез показывал бы
    # wait_human, хотя движок уже работает.
    assert "unblocked" in kinds(conn, deal)
    assert journal.fold(events(conn, deal))["state"] != "wait_human", "ответ сдвинул дело с человека"
    assert transit.deal_state(spec, events(conn, deal)) == "pending", "этап снова в работе"
    # Ответ — повод для новой попытки того же этапа: impl#2 пошёл в работу.
    assert [(call["stage"], call["attempt"]) for call in fake.calls] == [("impl", 1), ("impl", 2)]
    assert "ответов пула дочитано: 1" in cli.render_run_final(second)


def test_cli_run_once_and_deal_show_pool_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bin/duty run --once` → ответ в пуле → `bin/duty run --once` → `bin/duty deal`.

    Ровно транскрипт приёмки: оба прогона идут через CLI (`HERMES_HOME` — своя
    песочница), спавн подменён фейком, ручного `sync_answers` нет ни одного.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"impl": "ask"})
    monkeypatch.setattr(driver.runner, "spawn_process", fake)

    assert _cli_run(monkeypatch, home, ["run", deal["id"], "--once"]) == 0
    assert "wait(question)" in capsys.readouterr().out
    question_id = asked_question_id(conn, deal)

    answer_in_pool(home, question_id)
    fake.plan["impl"] = "ok"

    assert _cli_run(monkeypatch, home, ["run", deal["id"], "--once"]) == 0
    out = capsys.readouterr().out
    assert "ответов пула дочитано: 1" in out
    assert "question_answered" in kinds(conn, deal)

    assert _cli_run(monkeypatch, home, ["deal", "duty", deal["id"]]) == 0
    card = capsys.readouterr().out
    assert "состояние ожидания: wait_human" not in card, card
    assert "impl#2" in card, "карточка называет попытку, которую поднял ответ"
    assert "ответа нет" not in card, "отвеченный вопрос больше не выдаётся за ожидание"


def test_pool_answer_closes_orphan_and_order_is_sweep_sync_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Порядок шага: `reaper.sweep` → `ask.sync_answers` → решение по журналу.

    На одном решении сходятся оба факта — осиротевшая попытка и ответ пула.
    Проверяется и порядок вызовов (иначе ответ читался бы уже после решения и
    терялся бы на один шаг), и что оба факта попали в журнал до приговора.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    start_orphan(conn, deal)
    # Вопрос дела, закрытый в пуле: journal его ещё не знает — это и есть работа синка.
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    with pool:
        pool.execute(
            "INSERT INTO questions (session_key, project, text, origin, status, answer,"
            " answered_at, answered_via, kind) "
            "VALUES (?, 'duty', 'режем скоуп?', 'duty', 'answered', 'режем',"
            " '2026-11-03T10:00:00.000Z', 'manual', 'decision')",
            (f"t_{deal['id']}",),
        )
    pool.close()

    order: list[str] = []
    real_sweep, real_sync, real_next = driver.reaper.sweep, driver.ask.sync_answers, driver.transit.next_stage

    def sweep(*args: Any, **kwargs: Any) -> Any:
        order.append("sweep")
        return real_sweep(*args, **kwargs)

    def sync(*args: Any, **kwargs: Any) -> Any:
        order.append("sync")
        return real_sync(*args, **kwargs)

    def next_stage(*args: Any, **kwargs: Any) -> Any:
        order.append("decision")
        return real_next(*args, **kwargs)

    monkeypatch.setattr(driver.reaper, "sweep", sweep)
    monkeypatch.setattr(driver.ask, "sync_answers", sync)
    monkeypatch.setattr(driver.transit, "next_stage", next_stage)

    report = run(conn, spec, deal, home, Fake(), clock=lambda: NOW + 2401)

    assert order[:3] == ["sweep", "sync", "decision"], order
    assert [row["stage"] for row in report["reaped"]] == ["impl"], "сирота закрыта до решения"
    assert report["answers"]["appended"] == 1
    # Оба факта — в журнале, и приговор считается уже по ним: попытка impl упала
    # как infra, поэтому дело остановлено, а не «ждёт вопрос #—».
    assert kinds(conn, deal).count("question_answered") == 1
    assert report["outcome"] in ("failed", "wait"), report["outcome"]


def test_answer_errors_go_to_output_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пул закрыл вопрос, а `session_key` не разобран — это `errors[]`, а не тишина.

    «Вопрос закрыт, но не разобран» не должно выглядеть как «ответа нет»: строка
    уходит в вывод шага и в `--json` (`answers_errors`), а решение остаётся
    прежним — факта в журнале нет.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"impl": "ask"})
    monkeypatch.setattr(driver.runner, "spawn_process", fake)

    assert _cli_run(monkeypatch, home, ["run", deal["id"], "--once"]) == 0
    question_id = asked_question_id(conn, deal)
    capsys.readouterr()
    answer_in_pool(home, question_id, session_key="zzz-чужой-ключ")

    seen: list[dict[str, Any]] = []
    report = run(conn, spec, deal, home, fake, once=True, on_answer_error=seen.append)

    assert len(seen) == 1 and seen[0] == report["answers_errors"][0]
    assert "не разобран" in str(seen[0]["reason"])
    assert report["answers"] == {"appended": 0}, "факта нет — дочитывать нечего"
    assert deal_state(conn, deal, spec) == "wait_human", "решение осталось прежним"
    assert "question_answered" not in kinds(conn, deal)

    assert _cli_run(monkeypatch, home, ["run", deal["id"], "--once"]) == 3
    out = capsys.readouterr().out
    assert "! ответы пула:" in out and "не разобран" in out

    assert _cli_run(monkeypatch, home, ["run", deal["id"], "--once", "--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["answers_errors"] and "не разобран" in payload["answers_errors"][0]["reason"]


def test_pool_without_answer_keeps_wait_human_and_writes_nothing(tmp_path: Path) -> None:
    """Живой вопрос (ответа нет) — поведение как раньше: `wait_human`, без записи."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"impl": "ask"})

    first = run(conn, spec, deal, home, fake, once=True)
    question_id = first["steps"][0]["question_id"]
    before = kinds(conn, deal)

    second = run(conn, spec, deal, home, fake, once=True)

    assert second["outcome"] == "wait"
    assert second["exit_code"] == 3
    assert second["answers"] == {"appended": 0}
    assert kinds(conn, deal) == before, "синк без закрытого вопроса журнал не трогает"
    assert deal_state(conn, deal, spec) == "wait_human"
    assert f"#{question_id}" in second["note"]
    assert len(fake.calls) == 1, "второй попытки до ответа нет"


def test_expired_question_is_closed_by_sync_and_keeps_parking(tmp_path: Path) -> None:
    """Истёкший вопрос закрывает синк, и попытку это не поднимает: парковка — парковка."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"impl": "ask"})

    first = run(conn, spec, deal, home, fake, once=True)
    question_id = first["steps"][0]["question_id"]
    # Пул перевёл строку в `expired` своим чистящим циклом: ответа нет, но и
    # вопроса, которого ждут, тоже нет.
    answer_in_pool(home, question_id, status="expired")

    second = run(conn, spec, deal, home, fake, once=True)

    assert second["answers"]["appended"] == 1
    assert second["outcome"] == "park", "срок вышел — этап припаркован, а не поднят"
    assert second["exit_code"] == 2
    assert second["decision"]["question_id"] == str(question_id)
    assert len(fake.calls) == 1, "истёкший вопрос не повод для новой попытки"
    answered = [event for event in events(conn, deal) if event["kind"] == "question_answered"]
    assert len(answered) == 1
    assert journal.payload_of(answered[0])["answered_via"] == "expired"
    assert journal.payload_of(answered[0])["answer_len"] == 0
    assert "unblocked" not in kinds(conn, deal), "парковку движок не снимает"
    assert deal_state(conn, deal, spec) == "wait_human", "парковка остаётся парковкой"


def test_answer_arriving_mid_run_is_picked_up_before_next_step(tmp_path: Path) -> None:
    """Ответ, пришедший по ходу прогона, читается перед следующим шагом.

    Синк стоит в начале **каждого** шага, а не только первого: иначе ответ,
    данный между `impl` и `verify`, ждал бы внешнего вызова, и прогон упирался бы
    в `wait_human` на уже отвеченном вопросе.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    fake = Fake({"verify": "ask"})
    answered: list[int] = []

    def close_after_first_step(row: dict[str, Any]) -> None:
        # Человек отвечает ровно между шагами: пул закрыт, журнал ещё нет.
        if row["stage"] != "verify" or answered:
            return
        answered.append(int(row["question_id"]))
        answer_in_pool(home, int(row["question_id"]))
        fake.plan["verify"] = "ok"

    report = run(conn, spec, deal, home, fake, on_step=close_after_first_step, clock=time.time)

    assert answered, "вопрос по verify был задан"
    assert report["outcome"] == "done"
    assert [call["stage"] for call in fake.calls] == ["impl", "verify", "verify", "review"]
    assert report["answers"]["appended"] == 1
    assert "unblocked" in kinds(conn, deal), "ответ снял блок, который держал вопрос"
    assert journal.fold(events(conn, deal))["state"] != "wait_human"
    assert transit.deal_state(spec, events(conn, deal)) == "done"


def test_manual_block_after_answer_is_not_lifted_by_the_step(tmp_path: Path) -> None:
    """Чужой `needs_input` (человек заблокировал уже после ответа) движок не снимает.

    Движок закрывает ровно тот блок, который держал вопрос: если ответ пришёл
    раньше, а блок поставил человек, снимать его — решение человека, а не шага.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    seed(conn, deal, "question_asked", {"question_id": 5, "session_key": f"t_{deal['id']}"}, key="q5-asked")
    seed(conn, deal, "question_answered", {"question_id": 5, "answer_len": 4, "answered_via": "manual"}, key="q5-done")
    deals.set_blocked(
        conn, str(deal["id"]), kind="needs_input", reason="человек снял дело с очереди", author="max", now=NOW
    )

    report = run(conn, spec, deal, home, Fake(), clock=time.time)

    assert report["steps"], "шаг всё равно идёт: блок не парковка"
    assert "unblocked" not in kinds(conn, deal), "блок человека движок не снимает"
    assert journal.fold(events(conn, deal))["state"] == "wait_human"
    # W21: «все этапы пройдены» не даёт движку права закрыть дело, которое держит
    # человек. Отказ не глотается: прогон говорит, что дело не закрыто, и почему.
    assert report["closed"]["written"] is False
    assert "заблокировано" in report["closed"]["refused"]
    assert "completed" not in kinds(conn, deal)
    line = cli.render_run_final(report)
    assert line.startswith("итог: все этапы закрыты, но дело не закрыто"), line


# ---------------------------- W21: закрытие — факт журнала, а не строка вывода
#
# Живой прогон `d_6e764042` (04.11 19:16) прошёл всю цепочку и напечатал «итог:
# дело закрыто», а в журнале `completed` не было: дело осталось `status='running'`
# и выпало из `/now` как «no_signal». Движок назвал результат, которого в системе
# не было. Здесь проверяется обратное: закрытие пишется тем же путём, что
# `bin/duty close`, второй раз не пишется, а три поверхности говорят одно.

# Метка закрытия словами — та же, что печатает консоль (`cli._fmt_stamp`).
STAMP = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW))

# Факт закрытия одной фразой: ровно он обязан появиться на всех трёх поверхностях.
CLOSURE_FACT = "completed cause=all_stages_done, этапы: impl, verify, review"


def seed_attempt(
    conn: sqlite3.Connection, deal: dict[str, Any], stage: str, attempt: int
) -> None:
    """Попытка этапа в журнале: `attempt_started` и успешный `attempt_ended`.

    Ровно те два факта, которые пишет `runner`, и ничего больше: так выглядит
    журнал дела, у которого все этапы прошли, а закрытия никто не записал.
    """
    run_id = f"{deal['id']}:{stage}#{attempt}"
    seed(
        conn,
        deal,
        "attempt_started",
        {
            "run_id": run_id,
            "stage": stage,
            "attempt": attempt,
            "pid": 4300 + attempt,
            "lease_expires": NOW - 100,
            "contract": {"role": stage},
        },
        key=f"start-{stage}-{attempt}",
    )
    seed(
        conn,
        deal,
        "attempt_ended",
        {
            "run_id": run_id,
            "stage": stage,
            "attempt": attempt,
            "outcome": "done",
            "kind": None,
            "reason": "",
            "base": None,
            "commit": None,
        },
        key=f"end-{stage}-{attempt}",
    )


def test_all_stages_done_writes_one_completed_and_closes_the_deal(tmp_path: Path) -> None:
    """Все этапы закрыты → один `completed` с причиной и строка `status=done`."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)

    report = run(conn, spec, deal, home, Fake())

    completed = [event for event in events(conn, deal) if event["kind"] == "completed"]
    assert len(completed) == 1, "закрытие — один факт, а не абзац"
    payload = journal.payload_of(completed[0])
    assert payload == {"cause": "all_stages_done", "stages": ["impl", "verify", "review"]}
    assert completed[0]["source"] == "engine", "закрыл движок, и это видно в источнике"
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "done"
    assert report["closed"]["written"] is True
    assert report["closed"]["cause"] == "all_stages_done"
    assert report["closed"]["at"] == NOW
    # Журнал и строка говорят одно: дело закрыто и в активные не возвращается.
    assert journal.fold(events(conn, deal))["active"] is False
    assert deals.deal_rows(conn, open_only=True) == []
    # Печать итога — по записанному факту, а не вместо него.
    line = cli.render_run_final(report)
    assert line.startswith("итог: дело закрыто"), line
    assert CLOSURE_FACT in line and STAMP in line


def test_repeated_run_on_closed_deal_writes_no_second_completed(tmp_path: Path) -> None:
    """Повторный `run` на закрытом деле: второго `completed` нет, строка не тронута."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)

    run(conn, spec, deal, home, Fake())
    before = deals.deal_row(conn, str(deal["id"]))
    fake = Fake()
    second = run(conn, spec, deal, home, fake)

    assert fake.calls == [], "закрытое дело не поднимает ни одной попытки"
    assert kinds(conn, deal).count("completed") == 1
    assert deals.deal_row(conn, str(deal["id"]))["updated_at"] == before["updated_at"]
    assert second["closed"]["already"] is True
    assert second["closed"]["written"] is False
    assert second["closed"]["at"] == NOW, "время закрытия — из журнала, а не из часов"
    line = cli.render_run_final(second)
    assert line.startswith("итог: дело уже закрыто"), line
    assert CLOSURE_FACT in line and STAMP in line


def test_regression_d_6e764042_all_stages_done_without_completed(tmp_path: Path) -> None:
    """Регрессия живого прогона `d_6e764042`: три попытки ok, `completed` нет.

    Дело в работе (`status='running'`), журнал говорит «все этапы пройдены», а
    закрытия в нём нет. Прогон обязан дописать `completed` и закрыть дело — а не
    бодро отрапортовать «закрыто» в stdout.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    for stage in ("impl", "verify", "review"):
        seed_attempt(conn, deal, stage, 1)
    deals.claim_deal(conn, str(deal["id"]), owner="duty", source="engine", now=NOW)
    assert kinds(conn, deal).count("attempt_ended") == 3
    assert "completed" not in kinds(conn, deal)
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "running"
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert fake.calls == [], "этапы уже пройдены: новых попыток нет"
    assert report["outcome"] == "done"
    assert report["exit_code"] == 0
    assert kinds(conn, deal).count("completed") == 1, "недостающий факт дописан"
    assert report["closed"]["written"] is True
    assert report["closed"]["stages"] == ["impl", "verify", "review"]
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "done"
    assert journal.fold(events(conn, deal))["active"] is False


def test_deal_why_and_run_name_the_same_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`deal`, `why` и `run` на закрытом деле говорят одно: чем и когда закрыто."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    report = run(conn, spec, deal, home, Fake())
    monkeypatch.setenv("HERMES_HOME", str(home))

    payload = cli.api().deal(str(deal["board"]), str(deal["id"]))
    deal_out = cli.render_deal(payload, None, None)
    why_out = cli.render_why(payload)
    run_out = cli.render_run_final(report)

    assert payload["closed"]["cause"] == "all_stages_done"
    for out in (deal_out, why_out, run_out):
        assert CLOSURE_FACT in out, out
        assert STAMP in out, out
    assert "состояние ожидания: закрыто" in deal_out
    assert "состояние ожидания: закрыто" in why_out
    # Закрытое дело не показывается в «сейчас»: факт журнала и срез согласны.
    assert str(deal["id"]) not in [item["id"] for item in cli.api().now()["deals"]]




def test_run_does_not_revive_a_deal_closed_by_human(tmp_path: Path) -> None:
    """Дело, закрытое человеком, прогон не поднимает — даже с `stage` в payload.

    Человеческое закрытие (`decide stop`, `close`) называет этап, на котором
    стояло дело, и `transit` прочитал бы такой `completed` как закрытие **этапа**,
    а не дела. Поэтому прогон спрашивает `journal.closed_fact` до шага: иначе
    первая же попытка воскресила бы закрытое дело — та же подмена, что и «закрыто»
    без факта, только наоборот.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    seed_attempt(conn, deal, "impl", 1)
    seed(
        conn,
        deal,
        "blocked",
        {
            "block_kind": "parked",
            "reason": "исчерпано",
            "stage": "verify",
            "kind": "infra",
            "attempts": 3,
            "limit": 2,
        },
        key="park-1",
    )
    deals.close_deal(
        conn,
        str(deal["id"]),
        reason="дело больше не нужно",
        cause=deals.CLOSE_CAUSE_STOP,
        source="cli",
        now=NOW,
    )
    closed_payload = journal.payload_of(
        [event for event in events(conn, deal) if event["kind"] == "completed"][-1]
    )
    assert closed_payload["stage"] == "verify", "этап назван — так пишет decide stop"
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert fake.calls == [], "закрытое дело попыток не поднимает"
    assert report["outcome"] == "done"
    assert report["exit_code"] == 0
    assert report["closed"]["already"] is True
    assert report["closed"]["written"] is False
    assert report["closed"]["cause"] == "stopped_by_human"
    assert kinds(conn, deal).count("completed") == 1, "второй факт не пишется"
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "done"
    assert cli.render_run_final(report).startswith("итог: дело уже закрыто")


# ------------------------------------------------------------------ гейт
#
# Гейт в цепочке — обычный этап дела: `impl → gate → check`. Драйвер его не
# исполняет (процесса нет), а доводит до вопроса человеку и останавливается;
# ответ пула доезжает журналом, и следующий прогон судит его по `allow`.

GATE_CHAIN = """
duty: probe
stages:
  - id: impl
    role: implementer
  - id: gate
    kind: gate
    ask: {q: "Выкатываем?", options: [да, нет]}
    needs: [impl]
    budget: {minutes: 5}
  - id: check
    role: tester
    needs: [gate]
    workspace: none
"""


def gate_home(tmp_path: Path) -> tuple[Path, sqlite3.Connection, Any, dict[str, Any]]:
    """Своё дело на цепочке с гейтом: дом, store, спека и строка дела."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, GATE_CHAIN)
    deal = make_deal(conn, spec_path=spec.path)
    return home, conn, spec, deal


def test_gate_chain_stops_at_the_question(tmp_path: Path) -> None:
    """Прогон доводит дело до вопроса гейта и выходит ожиданием, а не ошибкой.

    Проверяется записанное: шаг `impl` сделан процессом, у гейта процесса нет
    (ни одного `attempt_started`), вопрос задан, дело ждёт человека.
    """
    home, conn, spec, deal = gate_home(tmp_path)
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert report["decision"]["kind"] == "wait"
    assert report["outcome"] == "wait"
    assert [call["stage"] for call in fake.calls] == ["impl"]
    assert asked_question_id(conn, deal) > 0
    staged = {str(payload["stage"]) for payload in process_stages(conn, deal)}
    assert staged == {"impl"}  # гейт попытки-процесса не начинал
    assert deal_state(conn, deal, spec) == "wait_human"


def test_gate_answer_in_allow_lets_the_chain_finish(tmp_path: Path) -> None:
    """Ответ «да» доезжает пулом: гейт закрыт, `check` выполнен, дело закрыто.

    Перезапуска драйвера не требуется — ответ читает тот же прогон
    (`ask.sync_answers` до решения), и это ровно приёмка карточки.
    """
    home, conn, spec, deal = gate_home(tmp_path)
    run(conn, spec, deal, home, Fake())
    answer_in_pool(home, asked_question_id(conn, deal), answer="да")

    report = run(conn, spec, deal, home, Fake())

    assert report["outcome"] == "done"
    assert report["closed"] is not None and report["closed"]["written"] is True
    ended = [event["payload"] for event in events(conn, deal) if str(event["kind"]) == "attempt_ended"]
    gate_ended = next(payload for payload in ended if str(payload.get("stage")) == "gate")
    assert gate_ended["outcome"] == "done"
    assert gate_ended["details"]["answer"] == "да"
    assert transit.deal_state(spec, events(conn, deal)) == "done"


def test_gate_answer_outside_allow_stops_the_deal(tmp_path: Path) -> None:
    """Ответ «нет» при `allow: [да]` — отказ контракта: дело дальше не идёт, `check` не запущен.

    Это и приёмка «ответ вне allow — отказ», и красная проба: без сверки с `allow`
    гейт закрылся бы успехом, и `check` выполнился бы.
    """
    home, conn, spec, deal = gate_home(tmp_path)
    narrow = write_spec(tmp_path, GATE_CHAIN.replace("    budget: {minutes: 5}", "    budget: {minutes: 5}\n    allow: [да]"))
    run(conn, narrow, deal, home, Fake())
    answer_in_pool(home, asked_question_id(conn, deal), answer="нет")
    fake = Fake()

    report = run(conn, narrow, deal, home, fake)

    assert report["outcome"] == "failed"
    assert fake.calls == []  # `check` не запускался: решение человека — не продолжать
    gate_ended = next(
        event["payload"]
        for event in events(conn, deal)
        if str(event["kind"]) == "attempt_ended" and str(event["payload"].get("stage")) == "gate"
    )
    assert gate_ended["kind"] == runner.FAIL_CONTRACT
    # Цитата ответа — в факте, а не в пересказе: по журналу видно, **что** ответил человек.
    assert "«нет»" in gate_ended["reason"] and "вне allow" in gate_ended["reason"]
    assert gate_ended["details"]["answer"] == "нет"
    assert transit.deal_state(narrow, events(conn, deal)) == "failed"


def test_gate_expired_question_parks_the_deal_with_the_gate_reason(tmp_path: Path) -> None:
    """Человек не ответил за срок — дело запарковано с причиной гейта, а не «0/0 unknown».

    Срок объявлен спекой (`budget.minutes: 5`), и причина называет его же: число в
    журнале обязано совпадать с числом, по которому вопрос ушёл в пул.
    """
    home, conn, spec, deal = gate_home(tmp_path)
    run(conn, spec, deal, home, Fake())
    answer_in_pool(home, asked_question_id(conn, deal), status="expired")

    report = run(conn, spec, deal, home, Fake())

    assert report["parked"] is not None
    assert report["parked"]["written"] is True
    evidence = str(journal.fold(events(conn, deal), now=NOW)["evidence"])
    assert "гейт: человек не ответил за 5 мин" in evidence
    assert "0/0" not in evidence


def process_stages(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    """Этапы, у которых была попытка-процесс: только они попадают в `attempt_started`."""
    return [
        dict(event["payload"])
        for event in events(conn, deal)
        if str(event["kind"]) == "attempt_started"
    ]


# --------------------------------------- потолок дела целиком (0.0.5, карточка S3)


BUDGET_STOP = """
duty: budget-stop
budget:
  tokens: 500
  minutes: 60

stages:
  - id: impl
    role: implementer
    retry: {max: 3, on: [infra]}
  - id: verify
    role: tester
    needs: [impl]
    workspace: none
"""


def spent_attempt(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    *,
    run_id: str,
    stage: str,
    tokens: int,
    seconds: int,
    kind: str = "infra",
) -> None:
    """Первая попытка уже была: начало, провал и телеметрия токенов на конце.

    Ровно та форма, которую пишет `runner`, включая `details.usage_tokens` —
    расход считается по журналу (`dashboard/budget.py`), поэтому предыстория
    должна выглядеть как настоящий факт, а не как «поставим счётчик в память».
    """
    started = NOW - 600
    seed(
        conn,
        deal,
        "attempt_started",
        {"run_id": run_id, "stage": stage, "attempt": 1, "pid": 4300, "contract": {"role": stage}},
        key=f"start:{run_id}",
        ts=started,
    )
    seed(
        conn,
        deal,
        "attempt_ended",
        {
            "run_id": run_id,
            "stage": stage,
            "attempt": 1,
            "outcome": "failed",
            "kind": kind,
            "reason": "процесс упал",
            "duration_sec": seconds,
            "details": {"usage_tokens": tokens},
        },
        key=f"end:{run_id}",
        ts=started + seconds,
    )


def test_second_attempt_does_not_start_when_deal_budget_is_spent(tmp_path: Path) -> None:
    """Исчерпанный потолок дела останавливает ретрай — попытка не поднимается вовсе.

    Красная проба карточки S3. Первая попытка провалилась классом `infra`, политика
    разрешает повтор — и до 0.0.5 он бы поднялся: потолок был только у этапа, а
    сумма по делу не смотрел никто. Проверяется то, что видно снаружи: спавна нет
    (`fake.calls` пуст), отказ лежит фактом журнала с числами, дело остановлено
    классом `capability`, и те же числа называет строка прогона.

    Уберите сверку в `runner.run_stage` — тест падает на первом же утверждении:
    вторая попытка стартует и жжёт бюджет, которого уже нет.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, BUDGET_STOP)
    deal = make_deal(conn, spec_path=spec.path)
    spent_attempt(conn, deal, run_id=f"{deal['id']}:impl#1", stage="impl", tokens=900, seconds=60)
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert fake.calls == [], "вторая попытка не стартует при исчерпанном бюджете"
    assert leases(conn, deal) == [], "лизинг не берётся: этапа за процессом нет"
    assert report["outcome"] == "failed"
    assert report["exit_code"] == 1
    assert report["steps"][0]["started"] is False
    assert report["steps"][0]["refused"] is True
    assert report["steps"][0]["kind"] == "capability"
    assert "бюджет дела исчерпан: 900/500 токенов" in report["note"]
    assert "900/500" in cli.render_run_final(report), "причина с числами видна человеку"
    assert report["decision"]["kind"] == "failed"
    assert report["decision"]["fail_kind"] == "capability"

    # Факт отказа — в журнале, а не только в выводе прогона: по нему живут `why`
    # и `deal`, и он же переживает рестарт движка.
    refused = [
        dict(event["payload"])
        for event in events(conn, deal)
        if str(event["kind"]) == "attempt_ended" and dict(event["payload"]).get("details", {}).get("refused")
    ]
    assert len(refused) == 1
    assert refused[0]["kind"] == "capability"
    assert refused[0]["reason"] == "бюджет дела исчерпан: 900/500 токенов"
    assert refused[0]["details"]["budget"] == {"kind": "tokens", "used": 900, "limit": 500}
    assert refused[0]["attempt"] == 2, "отказ относится ко второй попытке"

    row = deals.deal_row(conn, str(deal["id"]))
    assert str(row["status"]) == "blocked"
    folded = journal.fold(events(conn, deal), now=NOW)
    assert folded["state"] == "capability"
    assert "block_kind=capability" in str(folded["evidence"])
    assert "900/500 токенов" in str(folded["evidence"])
    assert deal_state(conn, deal, spec) == "capability"


def test_refused_shell_attempt_names_deal_minutes(tmp_path: Path) -> None:
    """Минуты дела — тоже потолок: shell-этап не стартует, причина называет минуты."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(
        tmp_path,
        """
        duty: minutes-stop
        repo: .
        budget:
          minutes: 5
        stages:
          - id: check
            kind: shell
            cmd: "true"
            retry: {max: 3, on: [infra]}
        """,
    )
    deal = make_deal(conn, spec_path=spec.path)
    spent_attempt(conn, deal, run_id=f"{deal['id']}:check#1", stage="check", tokens=0, seconds=301)
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert fake.calls == [], "shell-этап тоже не стартует при исчерпанных минутах"
    assert report["outcome"] == "failed"
    assert "бюджет дела исчерпан: 5/5 минут" in report["note"]
    assert report["steps"][0]["kind"] == "capability"


def test_attempt_starts_while_deal_has_room(tmp_path: Path) -> None:
    """Потолок не тратится на здоровые прогоны: место есть — попытка идёт."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, BUDGET_STOP)
    deal = make_deal(conn, spec_path=spec.path)
    spent_attempt(conn, deal, run_id=f"{deal['id']}:impl#1", stage="impl", tokens=100, seconds=60)
    fake = Fake()

    report = run(conn, spec, deal, home, fake)

    assert [(call["stage"], call["attempt"]) for call in fake.calls] == [("impl", 2), ("verify", 1)], (
        "место есть — повтор разрешён, и дело идёт дальше по графу"
    )
    assert report["outcome"] == "done"
