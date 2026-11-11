"""Супервизор лида: подъём, резюм после падения, парковка по резюмам.

Контракт — `docs/ENGINE-2.0.md`, разделы 2 и 10. Проверяется то, что видно в
журнале и в командной строке захода, а не обещания docstring:

1. нет сессии → заход собирает команду **первого** запуска с брифом
   (`-p lead -s project-delivery --query-file <бриф> --format stream-json`);
   бюджет захода держит сам супервизор: `hermes chat` через живой гейтвей
   отказывает `--run-budget`;
2. `kill` процесса лида посреди дела → следующий тик поднимает **резюм**
   (`--resume <session_id>`) с дайджестом фактов после курсора: курсор не теряется,
   второй попытки той же работы не появляется;
3. исчерпан `max_lead_resumes` → дело паркуется вопросом человеку, а не крутится
   новым заходом;
4. двойной запуск гасится лизингом лида в журнале, а мёртвому заходу сторож
   дописывает недостающий `lead_exited`.

Живой процесс `hermes chat` в тестах не поднимается: спавн подменяется фейком,
который отдаёт заданный поток и заданный код выхода. Настоящая модель стоила бы
денег и секунд, а проверяется стыковка «решение сторожа → факт журнала → команда».
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import sqlite3
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

NOW = 1_790_000_000
# Ключи фактов теста: уникальны в пределах прогона, иначе `INSERT OR IGNORE`
# склеил бы два разных факта одного вида.
_SEQ = itertools.count()

SEED = """
duty: супервизор лида
repo: ~/GolandProjects/duty
budget: {tokens: 3000000, minutes: 480}
charter:
  max_stages: 6
  allowed_roles: [implementer, researcher, reviewer]
  allowed_kinds: [agent, shell, gate]
stages:
  - id: impl
    role: implementer
    workspace: none
    evidence:
      - {cmd: "true", must: 0}
"""


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
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
lead = load_module("hermes_duty_lead", "lead.py")


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


def write_spec(tmp_path: Path, text: str = SEED) -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(conn: sqlite3.Connection, spec: Any, *, body: str = "сделай дело") -> dict[str, Any]:
    """Своё дело: строка store плюс путь спеки, записанный в `created`."""
    created = deals.create_deal(
        conn,
        repo="duty",
        title="Супервизор лида",
        body=body,
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
                "source": "test",
                "idempotency_key": f"test:{deal['id']}:{kind}:{next(_SEQ)}",
            }
            for index, (kind, payload) in enumerate(facts)
        ],
    )


def kinds(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[str]:
    """Виды фактов дела по порядку — «журнал вырос / не вырос» и «что записано»."""
    return [str(event["kind"]) for event in journal.read_events(conn, str(deal["id"]))]


def facts_of(conn: sqlite3.Connection, deal: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """Payload'ы фактов одного вида — то, чем проверяется записанное."""
    return [
        journal.payload_of(event)
        for event in journal.read_events(conn, str(deal["id"]))
        if str(event["kind"]) == kind
    ]


def head(conn: sqlite3.Connection, deal: dict[str, Any]) -> int:
    """Курсор «всё прочитано»: `id` последнего факта дела."""
    return lead.head(journal.read_events(conn, str(deal["id"])))


class FakeProcess:
    """Процесс сессии без модели: отдаёт заданный поток и заданный код выхода.

    Форма — та же, что у `runner.Process`: `pid`, `next_line`, `finished`,
    `exit_code`, `wait`, `stop`, `stderr_tail`. `kill` процесса лида — это
    `exit_code = -9`: обёртка видит смерть дочернего процесса ровно так же, как в
    жизни, и пишет `lead_exited`.
    """

    def __init__(self, lines: list[str], *, exit_code: Optional[int] = 0, pid: int = 4242) -> None:
        self._lines = list(lines)
        self._exit = exit_code
        self._pid = pid
        self._stderr: list[str] = []
        self.stopped = False

    @property
    def pid(self) -> int:
        return self._pid

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        return self._lines.pop(0) if self._lines else None

    def finished(self) -> bool:
        return not self._lines

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self._exit

    def stop(self, grace: float = 0.0) -> Optional[int]:
        self.stopped = True
        self._exit = -15
        return self._exit

    @property
    def stderr_tail(self) -> list[str]:
        """Та же форма, что у `runner.Process`: хвост stderr свойством."""
        return self._stderr


def stream_result(session_id: str, tokens: int = 1000) -> str:
    """Строка `result` потока: из неё обёртка берёт id сессии и расход."""
    return json.dumps({"type": "result", "session_id": session_id, "tokens": {"total": tokens}})


def fake_spawn(lines: list[str], *, exit_code: Optional[int] = 0, pid: int = 4242, seen: Optional[dict[str, Any]] = None) -> Any:
    """Спавн сессии фейком: запоминает команду, поднимает `FakeProcess`."""

    def spawn(cmd: list[str], cwd: Path) -> FakeProcess:
        if seen is not None:
            seen["cmd"] = list(cmd)
            seen["cwd"] = Path(cwd)
        return FakeProcess(lines, exit_code=exit_code, pid=pid)

    return spawn


def dead(pid: Any) -> bool:
    """Проба живости pid для тестов: «процесса нет»."""
    return False


def alive(pid: Any) -> bool:
    """Проба живости pid для тестов: «процесс есть»."""
    return True


# ------------------------------------------- 1. первый заход: команда с брифом


def test_first_start_builds_brief_command_and_records_facts(tmp_path: Path) -> None:
    """Нет сессии → команда первого запуска с брифом, `lead_started`/`lead_exited`.

    `docs/ENGINE-2.0.md`, раздел 2: первый заход поднимается скиллом
    `project-delivery` и брифом; бюджет захода (1800 с) держит супервизор по
    часам — флага в команде нет, потому что живой гейтвей его отвергает. Факт
    начала несёт pid процесса и курсор — то, на чём потом стоит резюм.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    before = head(conn, deal)
    conn.close()

    seen: dict[str, Any] = {}
    report = lead.run(
        home,
        str(deal["id"]),
        now=NOW,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        spawn=fake_spawn([stream_result("sess-1")], seen=seen),
    )

    assert report["started"] is True
    assert report["mode"] == lead.MODE_FIRST
    cmd = seen["cmd"]
    assert cmd[0].endswith("hermes")
    assert cmd[1:6] == ["chat", "-p", "lead", "-s", "project-delivery"]
    assert "--resume" not in cmd
    assert cmd[cmd.index("--format") + 1] == "stream-json"
    brief_path = Path(cmd[cmd.index("--query-file") + 1])
    brief = brief_path.read_text(encoding="utf-8")
    assert "--run-budget" not in cmd
    assert "бюджет этого захода: 1800 с" in brief
    # Бриф — про это дело, с путём к контракту движка и к спеке дела.
    assert "Супервизор лида" in brief
    assert str(lead.ENGINE_DOC) in brief
    assert str(spec.path) in brief
    assert "duty_plan" in brief

    conn = open_store(home)
    started = facts_of(conn, deal, lead.LEAD_KIND_STARTED)
    assert len(started) == 1
    assert started[0]["mode"] == lead.MODE_FIRST
    assert started[0]["pid"] == 4242
    assert started[0]["cursor"] == before
    assert started[0]["session_id"] is None
    exited = facts_of(conn, deal, lead.LEAD_KIND_EXITED)
    assert len(exited) == 1
    assert exited[0]["reason"] == "exit"
    assert exited[0]["exit_code"] == 0
    assert exited[0]["session_id"] == "sess-1"
    # Лизинг отпущен: заход кончился, строка не держит следующего.
    assert lead.lease_of(conn, str(deal["id"])) is None
    conn.close()


# --------------------------------- 2. kill посреди дела → резюм с дайджестом


def test_kill_mid_deal_resumes_with_digest_and_keeps_cursor(tmp_path: Path) -> None:
    """`kill` лида посреди дела → следующий тик поднимает резюм, работа не дублируется.

    Проверяются три вещи из приёмки (`docs/ENGINE-2.0.md`, раздел 10):

    - следующий тик собирает команду `--resume <session_id>` (сессия та же);
    - дайджест начинается с курсора сессии: факты до него не повторяются, факты
      после — названы;
    - второй попытки той же работы не появляется: журнал не растёт
      `attempt_started`, лид получает факты, а не новый старт.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    # Попытка, которую лид уже поднял и которая закончилась до его смерти.
    append(
        conn,
        deal,
        (
            "attempt_started",
            {"run_id": "impl#1", "stage": "impl", "attempt": 1, "pid": 111, "lease_expires": NOW + 60},
        ),
        (
            "attempt_ended",
            {
                "run_id": "impl#1",
                "stage": "impl",
                "attempt": 1,
                "outcome": "done",
                "kind": None,
                "reason": "",
                "details": {"workdir": str(tmp_path / "work" / "impl-1"), "usage_tokens": 5000},
            },
        ),
    )
    cursor_before = head(conn, deal)
    conn.close()

    # Первый заход: сессия успела назвать себя и была убита (SIGKILL → код -9).
    first = lead.run(
        home,
        str(deal["id"]),
        now=NOW,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        spawn=fake_spawn([stream_result("sess-killed")], exit_code=-9),
    )
    assert first["started"] is True
    assert first["reason"] == "crashed"

    # Пока лида не было, пришёл новый факт — он и должен попасть в дайджест.
    conn = open_store(home)
    append(
        conn,
        deal,
        (
            "question_asked",
            {
                "question_id": 77,
                "session_key": f"t_{deal['id']}",
                "text": "продолжать ли с Х?",
                "routing": {"target": "lead", "reason": "worker asked"},
            },
        ),
    )
    attempts_before = kinds(conn, deal).count("attempt_started")

    # Тик сторожа: лида нет → резюм.
    spawned: list[tuple[str, int]] = []

    def supervisor_spawn(home_path: Path, deal_id: str, run_budget: int) -> int:
        spawned.append((deal_id, run_budget))
        return 5151

    tick = lead.supervise(
        home, str(deal["id"]), now=NOW + 10, probe=dead, spawn=supervisor_spawn
    )
    assert tick["action"] == "started"
    assert tick["mode"] == lead.MODE_RESUME
    assert tick["session_id"] == "sess-killed"
    assert spawned == [(str(deal["id"]), lead.DEFAULT_RUN_BUDGET_SEC)]

    # Сам заход резюма: команда с --resume и дайджест от курсора сессии.
    seen: dict[str, Any] = {}
    second = lead.run(
        home,
        str(deal["id"]),
        now=NOW + 20,
        clock=lambda: NOW + 20,
        monotonic=lambda: 0.0,
        spawn=fake_spawn([stream_result("sess-killed", tokens=700)], seen=seen),
    )
    assert second["mode"] == lead.MODE_RESUME
    cmd = seen["cmd"]
    assert cmd[cmd.index("--resume") + 1] == "sess-killed"
    # Скилл на резюме не повторяется: сессия его уже видела.
    assert "-s" not in cmd
    digest_path = Path(cmd[cmd.index("--query-file") + 1])
    digest = digest_path.read_text(encoding="utf-8")
    assert "sess-killed" in digest
    # Курсор не потерян: дайджест говорит, с какого места он собран, и содержит
    # новый вопрос — но не историю до курсора.
    assert f"после курсора сессии ({cursor_before})" in digest
    assert "продолжать ли с Х?" in digest
    assert "дело заведено" not in digest

    started = facts_of(conn, deal, lead.LEAD_KIND_STARTED)
    assert [row["mode"] for row in started] == [lead.MODE_FIRST, lead.MODE_RESUME]
    assert started[1]["resumed_from"] == cursor_before
    assert started[1]["resumes"] == 1
    # Работа не продублирована: новых попыток журнал не получил.
    assert kinds(conn, deal).count("attempt_started") == attempts_before
    state = lead.state(journal.read_events(conn, str(deal["id"])), now=NOW + 30, probe=dead)
    assert state["resumes"] == 1
    assert state["live"] is False
    conn.close()


# ------------------------------------- 3. резюмы исчерпаны → парковка вопросом


def test_exhausted_resumes_park_the_deal_with_a_question(tmp_path: Path) -> None:
    """Исчерпан `max_lead_resumes` → дело паркуется с вопросом, а не крутится.

    `docs/ENGINE-2.0.md`, раздел 10: «лид исчерпал резюмы → дело паркуется,
    вопрос человеку». Парковка — существующий `blocked` с
    `block_kind='parked'`, вопрос — в пуле (`ask.ask`); повторный тик не
    спрашивает второй раз.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        *[
            (
                lead.LEAD_KIND_STARTED,
                {
                    "mode": lead.MODE_RESUME,
                    "pid": 900 + index,
                    "cursor": index,
                    "run_budget": 1800,
                    "lease_expires": NOW + 60,
                },
            )
            for index in range(lead.DEFAULT_MAX_RESUMES)
        ],
        (
            lead.LEAD_KIND_EXITED,
            {"mode": lead.MODE_RESUME, "pid": 905, "reason": "crashed", "exit_code": -9},
        ),
    )
    conn.close()

    spawned: list[str] = []
    tick = lead.supervise(
        home,
        str(deal["id"]),
        now=NOW + 60,
        probe=dead,
        spawn=lambda *args: spawned.append(args[1]) or 1,
    )

    assert tick["action"] == "parked"
    assert tick["limit"] == lead.DEFAULT_MAX_RESUMES
    assert tick["question"]["resumes"] == lead.DEFAULT_MAX_RESUMES
    assert spawned == []  # заход не поднят: крутить нечего

    conn = open_store(home)
    blocked = facts_of(conn, deal, "blocked")
    assert len(blocked) == 1
    assert blocked[0]["block_kind"] == "parked"
    assert blocked[0]["limit"] == lead.DEFAULT_MAX_RESUMES
    assert blocked[0]["question_id"] == tick["question"]["question_id"]
    assert "Лид падает" in blocked[0]["reason"]
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "blocked"
    folded = journal.fold(journal.read_events(conn, str(deal["id"])), now=NOW + 60, pid_probe=dead)
    assert folded["state"] == "wait_human"
    assert folded["parked"]["question_id"] == tick["question"]["question_id"]
    # Вопрос лежит в пуле с вариантами, которыми человек и решает.
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    row = pool.execute("SELECT status, options_json FROM questions WHERE id = ?", (tick["question"]["question_id"],)).fetchone()
    pool.close()
    assert row[0] == "pending"
    assert json.loads(row[1]) == list(lead.failures.PARK_OPTIONS)

    # Повторный тик: дело уже ждёт человека — сторожа оно больше не будит, второй
    # парковки и второго вопроса нет.
    again = lead.supervise(home, str(deal["id"]), now=NOW + 120, probe=dead, spawn=lambda *args: 1)
    assert again["action"] == "skipped"
    assert "человека" in again["reason"]
    assert len(facts_of(conn, deal, "blocked")) == 1
    # Парковка узнаётся по факту, а не только по статусу: тот же сторож, увидев
    # запаркованное дело в работе, второй раз не спрашивает.
    parked = lead._existing_park(journal.read_events(conn, str(deal["id"])), lead.DEFAULT_MAX_RESUMES)
    assert parked is not None
    assert parked["question_id"] == tick["question"]["question_id"]
    conn.close()

    pool = sqlite3.connect(home / "messaging" / "pool.db")
    count = pool.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    pool.close()
    assert count == 1


# ------------------------------- 4. двойной запуск гасится лизингом, мёртвый заход закрывается


def test_live_lead_is_not_raised_twice(tmp_path: Path) -> None:
    """Живой лид → тик ничего не поднимает; живой лизинг не пускает второй заход.

    Два условия одного правила (`docs/ENGINE-2.0.md`, раздел 10): «двойной запуск
    гасится лизингом лида в журнале». Живость решает pid процесса сессии, а
    лизинг — взаимное исключение до спавна.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        (
            lead.LEAD_KIND_STARTED,
            {
                "mode": lead.MODE_FIRST,
                "pid": 777,
                "cursor": 0,
                "run_budget": 1800,
                "lease_expires": NOW + 1800,
            },
        ),
    )
    # Лизинг лида держит живой процесс: pid этого теста действительно существует,
    # иначе заход вправе забрать лизинг у заведомо мёртвого держателя.
    with conn:
        taken = lead._take_lease(
            conn, str(deal["id"]), attempt=1, pid=os.getpid(), ttl=1800, now=NOW
        )
    assert taken is True
    conn.close()

    spawned: list[str] = []
    tick = lead.supervise(
        home, str(deal["id"]), now=NOW + 5, probe=alive, spawn=lambda *args: spawned.append(args[1]) or 1
    )
    assert tick["action"] == "alive"
    assert tick["lead"]["pid"] == 777
    assert spawned == []

    # Заход поверх занятого лизинга не поднимается и модель не спавнит.
    seen: dict[str, Any] = {}
    blocked_run = lead.run(
        home,
        str(deal["id"]),
        now=NOW + 5,
        clock=lambda: NOW + 5,
        monotonic=lambda: 0.0,
        spawn=fake_spawn([stream_result("sess-should-not-exist")], seen=seen),
    )
    assert blocked_run["started"] is False
    assert "лизинг" in blocked_run["reason"]
    assert seen == {}

    conn = open_store(home)
    assert facts_of(conn, deal, lead.LEAD_KIND_STARTED) == [
        {
            "mode": lead.MODE_FIRST,
            "pid": 777,
            "cursor": 0,
            "run_budget": 1800,
            "lease_expires": NOW + 1800,
        }
    ]
    conn.close()


def test_dead_lead_gets_the_missing_exit_fact(tmp_path: Path) -> None:
    """Мёртвому заходу сторож дописывает `lead_exited` — «умер» ≠ «отсутствует».

    Обёртку могли убить снаружи: тогда факта о конце нет, и журнал молчит о том,
    что заход кончился. Второй такой случай не должен добавлять второй факт.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        (
            lead.LEAD_KIND_STARTED,
            {
                "mode": lead.MODE_RESUME,
                "pid": 888,
                "session_id": "sess-orphan",
                "cursor": 3,
                "run_budget": 1800,
                "lease_expires": NOW + 1800,
            },
        ),
    )
    conn.close()

    tick = lead.supervise(
        home, str(deal["id"]), now=NOW + 40, probe=dead, spawn=lambda *args: 4242
    )
    assert tick["action"] == "started"
    assert tick["recovered_exit"] == 1

    conn = open_store(home)
    exited = facts_of(conn, deal, lead.LEAD_KIND_EXITED)
    assert len(exited) == 1
    assert exited[0]["reason"] == "process_gone"
    assert exited[0]["recovered"] is True
    assert exited[0]["session_id"] == "sess-orphan"
    # Закрытый факт не делает дело живым: следующий тик видит «лида нет».
    state = lead.state(journal.read_events(conn, str(deal["id"])), now=NOW + 50, probe=dead)
    assert state["open"] is False
    assert state["live"] is False
    conn.close()


# ------------------------------------------------- 5. дело не для лида


def test_deal_waiting_for_human_is_not_led(tmp_path: Path) -> None:
    """Заблокированное дело лида не поднимает: ответ человека проводит диспетчер."""
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    deals.set_blocked(conn, str(deal["id"]), kind="needs_input", reason="ответ человека", now=NOW)
    conn.close()

    spawned: list[str] = []
    tick = lead.supervise(
        home, str(deal["id"]), now=NOW + 10, probe=dead, spawn=lambda *args: spawned.append(args[1]) or 1
    )
    assert tick["action"] == "skipped"
    assert "человека" in tick["reason"]
    assert spawned == []


# ------------------------------------- 7. дело старого движка: жив его рантайм


def test_live_legacy_runtime_is_not_raised_twice(tmp_path: Path) -> None:
    """Дело старого движка сторож не трогает ни с живым, ни с мёртвым pid.

    Признак дела 0.1.x — отсутствие хартии (`created.payload.spec`): работу такого
    дела ведёт `bin/duty run`. Живой pid открытой попытки — «лид жив»; мёртвый pid
    между заходами — повод **не** поднимать лида 2.0, а оставить дело старому
    рантайму: два движка на одном деле дали бы двойную работу.
    """
    home = make_home(tmp_path)
    write_spec(tmp_path)  # спека 1.x живёт в самом деле — хартии у него нет
    conn = open_store(home)
    created = deals.create_deal(conn, repo="duty", title="Старое дело", body="b", now=NOW)
    deal = deals.deal_row(conn, str(created["deal"]["id"]))
    assert deal is not None
    append(
        conn,
        deal,
        (
            "attempt_started",
            {"run_id": "implement#1", "stage": "implement", "attempt": 1, "pid": 4242},
        ),
    )
    conn.close()

    spawned: list[str] = []
    tick = lead.supervise(
        home,
        str(deal["id"]),
        now=NOW + 5,
        probe=alive,
        spawn=lambda *args: spawned.append(args[1]) or 1,
    )
    assert tick["action"] == "alive"
    assert tick["lead"]["mode"] == "runtime"
    assert tick["runtime"]["run_id"] == "implement#1"
    assert spawned == []

    # Тот же журнал, но процесс попытки уже мёртв → не поднимаем лида 2.0.
    tick = lead.supervise(
        home,
        str(deal["id"]),
        now=NOW + 10,
        probe=dead,
        spawn=lambda *args: spawned.append(args[1]) or 1,
    )
    assert tick["action"] == "skipped"
    assert "старого движка" in tick["reason"]
    assert spawned == []
