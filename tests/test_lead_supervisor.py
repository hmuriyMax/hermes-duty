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


# --------------------------------- 4. лид завис: тишина в потоке — это `idle`


class SilentProcess:
    """Живой и молчащий процесс сессии: строки кончаются, а процесс — нет.

    Так выглядит зависание (`docs/ENGINE-2.0.md`, раздел 10): pid жив, поток
    молчит. Форма та же, что у `runner.Process`; `stop` запоминает, что заход
    погасил именно он.
    """

    def __init__(self, lines: list[str], *, pid: int = 4242) -> None:
        self._lines = list(lines)
        self._pid = pid
        self.stopped = False
        self._exit: Optional[int] = None

    @property
    def pid(self) -> int:
        return self._pid

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        return self._lines.pop(0) if self._lines else None

    def finished(self) -> bool:
        return False

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit

    def wait(self, timeout: Optional[int] = None) -> Optional[int]:
        return self._exit

    def stop(self, grace: float = 0.0) -> Optional[int]:
        self.stopped = True
        self._exit = -15
        return self._exit

    @property
    def stderr_tail(self) -> list[str]:
        return []


def test_silent_lead_exits_idle_and_the_supervisor_resumes(tmp_path: Path) -> None:
    """Тишина в потоке — `lead_exited(reason=idle)`, дальше штатный резюм.

    §10 различает «лид умер» и «лид завис»: живой процесс, от которого нет ни
    слова. `overrun` видит это только через бюджет захода (30 минут), поэтому у
    захода свой порог тишины (`lead.LEAD_IDLE_TIMEOUT_SEC`): живой `duty_wait`
    возвращается не позже 300 с, и молчание дольше порога — зависание, а не
    ожидание работы.

    Красная проба: без проверки заход держал бы лизинг до `overrun`, и «лид
    завис» было бы неотличимо от «лид работает».
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    conn.close()
    silence = SilentProcess([stream_result("sess-idle")])
    ticks = itertools.count(0, 10)

    report = lead.run(
        home,
        str(deal["id"]),
        run_budget=3600,
        idle_timeout=30,
        now=NOW,
        clock=lambda: NOW,
        monotonic=lambda: next(ticks),
        spawn=lambda cmd, cwd: silence,
    )

    assert report["reason"] == "idle"
    assert silence.stopped is True, "зависший заход гасит обёртка, а не супервизор"
    conn = open_store(home)
    exited = facts_of(conn, deal, lead.LEAD_KIND_EXITED)
    conn.close()
    assert [item["reason"] for item in exited] == ["idle"]
    assert exited[0]["session_id"] == "sess-idle", "резюм поднимается из той же сессии"

    spawned: list[str] = []
    tick = lead.supervise(
        home, str(deal["id"]), now=NOW + 60, probe=dead, spawn=lambda *args: spawned.append(args[1]) or 1
    )

    assert tick["action"] == "started"
    assert tick["mode"] == lead.MODE_RESUME
    assert spawned == [str(deal["id"])]


class WaitingClock:
    """Часы захода: стоят, пока поток отдаёт строки, и идут на пустом опросе.

    Так выглядит ожидание внутри вызова инструмента: строк в потоке нет, а время
    идёт. Счётчик с постоянным шагом здесь не годится — он двигал бы время и на
    чтении строки, и «сколько заход просидел в вызове» стало бы функцией числа
    вызовов часов, а не сценария.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def poll(self, step: float) -> None:
        """Пустой опрос потока: время идёт хотя бы на секунду — шаг виден в тесте."""
        self.now += max(float(step), 1.0)


class ScriptedProcess:
    """Живой процесс, который говорит по расписанию: пары «момент, строка».

    `SilentProcess` молчит вечно, а ожидание внутри блокирующего вызова выглядит
    иначе: `tool_use` пришёл, потом поток молчит до `tool_result`, и всё это время
    процесс жив. `ends=False` — процесс не кончается сам (завис внутри вызова).
    """

    def __init__(
        self,
        clock: WaitingClock,
        script: list[tuple[float, str]],
        *,
        ends: bool = True,
        pid: int = 4242,
    ) -> None:
        self._clock = clock
        self._script = list(script)
        self._ends = ends
        self._pid = pid
        self._exit: Optional[int] = 0
        self.stopped = False

    @property
    def pid(self) -> int:
        return self._pid

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        if self._script and self._clock.now >= self._script[0][0]:
            return self._script.pop(0)[1]
        self._clock.poll(timeout)
        return None

    def finished(self) -> bool:
        return self._ends and not self._script

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit

    def wait(self, timeout: Optional[int] = None) -> Optional[int]:
        return self._exit

    def stop(self, grace: float = 0.0) -> Optional[int]:
        self.stopped = True
        self._exit = -15
        return self._exit

    @property
    def stderr_tail(self) -> list[str]:
        return []


def stream_tool_use(name: str, *, call: str = "call-1") -> str:
    """Строка `tool_use` потока: вызов инструмента начался (`hermes_cli/stream_json.py`)."""
    return json.dumps({"type": "tool_use", "name": name, "tool_call_id": call})


def stream_tool_result(name: str, *, call: str = "call-1") -> str:
    """Строка `tool_result` потока: тот же вызов вернулся — по тому же `tool_call_id`."""
    return json.dumps(
        {"type": "tool_result", "name": name, "tool_call_id": call, "output": "ок", "duration_ms": 1000}
    )


# Сколько «отвечает» роль: дольше порога тишины захода (900 с) — как живая
# под-попытка роли-исполнителя.
ASK_ANSWER_SEC = lead.LEAD_IDLE_TIMEOUT_SEC + 100


def script_of(kind: str) -> list[tuple[float, str]]:
    """Поток одного вызова: начало, а для `ask` — ответ роли и итог захода."""
    if kind == "ask":
        return [
            (0, stream_tool_use("duty_ask")),
            (ASK_ANSWER_SEC, stream_tool_result("duty_ask")),
            (ASK_ANSWER_SEC, stream_result("sess-ask")),
        ]
    return [(0, stream_tool_use("duty_wait"))]


@pytest.mark.parametrize(
    ("kind", "ends", "want_reason", "want_stopped"),
    [("ask", True, "exit", False), ("wait", False, "idle", True)],
    ids=["ask inside the role budget", "wait past its ceiling"],
)
def test_lead_waits_inside_a_blocking_call_instead_of_counting_stream_silence(
    tmp_path: Path, kind: str, ends: bool, want_reason: str, want_stopped: bool
) -> None:
    """Открытый блокирующий вызов — ожидание, а не тишина: у каждого свой потолок.

    Блокер ревью круга 2: тишина мерилась только потоком, а `duty_ask`
    (`dashboard/tools.py` → `runner.ask_role`) синхронно держит лида **всю**
    под-попытку роли — строк в потоке при этом нет. Роль-исполнитель легко
    отвечает дольше `LEAD_IDLE_TIMEOUT_SEC` (900 с), и такой заход гасился
    `idle`: под-попытка рвалась, резюм запускал заход заново, резюмы лида
    тратились на работу, которую никто не бросал.

    Инвариант — «молчание внутри живого вызова не считается тишиной» — проверяется
    с двух сторон:

    - `duty_ask` молчит 1000 с (дольше порога) и всё равно доводит заход до
      штатного выхода: потолок вызова — срок под-попытки роли
      (`runner.ttl_sec`, у seed-этапа 1800 с), а не 900 с порога;
    - `duty_wait` пересидел свой потолок (`tools.WAIT_CEILING_SEC`), и порог
      тишины обязан его видеть, иначе зависание внутри `duty_wait` стало бы
      невидимым до `overrun`.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    conn.close()
    clock = WaitingClock()
    process = ScriptedProcess(clock, script_of(kind), ends=ends)

    report = lead.run(
        home,
        str(deal["id"]),
        run_budget=7200,
        now=NOW,
        clock=lambda: NOW,
        monotonic=clock,
        spawn=lambda cmd, cwd: process,
    )

    assert report["reason"] == want_reason
    assert process.stopped is want_stopped
    if want_reason == "exit":
        assert clock.now > lead.LEAD_IDLE_TIMEOUT_SEC, "роль отвечала дольше порога тишины"
    else:
        assert clock.now < lead.LEAD_IDLE_TIMEOUT_SEC, "вызов гасится по своему потолку, а не по порогу"
    conn = open_store(home)
    exited = facts_of(conn, deal, lead.LEAD_KIND_EXITED)
    conn.close()
    assert [item["reason"] for item in exited] == [want_reason]
    if want_reason == "exit":
        assert exited[0]["session_id"] == "sess-ask", "дело продолжается той же сессией"
    # Потолки — по инструменту: `duty_ask` держит лида дольше порога тишины,
    # `duty_wait` ограничен своим сроком, у прочих вызова своего потолка нет.
    assert lead._call_ceiling("duty_ask", spec) > lead.LEAD_IDLE_TIMEOUT_SEC
    assert lead._call_ceiling("duty_wait", spec) > 0
    assert lead._call_ceiling("read_file", spec) is None, "у прочих вызовов потолка нет"


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


# ----------------------------------- 7. дело, которое ведут руками: чужой драйвер


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

    no_ps = lambda: ""
    spawned: list[str] = []
    tick = lead.supervise(
        home,
        str(deal["id"]),
        now=NOW + 5,
        probe=alive,
        spawn=lambda *args: spawned.append(args[1]) or 1,
        ps=no_ps,
    )
    assert tick["action"] == "alive"
    assert tick["lead"]["mode"] == "runtime"
    assert tick["runtime"]["run_id"] == "implement#1"
    assert spawned == []

    # Процесс попытки мёртв, ручного драйвера нет, хартии у дела нет → не наш заход.
    tick = lead.supervise(
        home,
        str(deal["id"]),
        now=NOW + 10,
        probe=dead,
        spawn=lambda *args: spawned.append(args[1]) or 1,
        ps=no_ps,
    )
    assert tick["action"] == "skipped"
    assert spawned == []


def test_deal_driven_by_hand_is_left_to_its_driver(tmp_path: Path) -> None:
    """Живой цикл `bin/duty run <deal>` в терминале → сторож не поднимает лида.

    Между заходами цикла pid попытки мёртв и лизинга нет: по журналу дело выглядит
    осиротевшим, но драйвер у него есть — человек. Второй драйвер (лид 2.0) дал бы
    двойную работу по делу.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    deal_id = str(deal["id"])
    append(conn, deal, ("attempt_started", {"run_id": "implement#1", "stage": "implement", "attempt": 1, "pid": 4242}))
    conn.close()

    ps = lambda: (
        f"   1 /sbin/launchd\n15283 /bin/zsh -lic cd repo && for i in 1 2; do bin/duty run {deal_id}; sleep 40; done\n"
    )
    spawned: list[str] = []
    tick = lead.supervise(
        home,
        deal_id,
        now=NOW + 30,
        probe=dead,
        spawn=lambda *args: spawned.append(args[1]) or 1,
        ps=ps,
    )
    assert tick["action"] == "skipped"
    assert tick["manual_pid"] == 15283
    assert spawned == []


# ------------------------------- 9. два корня захода и поток сессии файлом


def test_child_env_separates_the_deal_home_from_the_session_root(tmp_path: Path) -> None:
    """`child_env` называет корень сессии и корень дела по отдельности.

    `hermes chat -p lead` ищет профиль **внутри** экспортированного
    `HERMES_HOME` (`hermes_cli.profiles.profile_root_for_env_home`), поэтому
    `HERMES_HOME` обязан быть корнем Hermes, а не корнем профиля и не корнем
    дела. Инструменты `duty` читают store дела из `$HERMES_DUTY_HOME`
    (`dashboard/tools.py`, `hermes_home`), иначе профильная сессия искала бы дело
    в профиле (приёмка 2.0, дело d_eafce375: «дела нет в store» на каждом
    вызове).
    """
    cases = [
        ("root", tmp_path / "home", tmp_path / "home"),
        ("profile", tmp_path / "home" / "profiles" / "lead", tmp_path / "home"),
        ("nested root", tmp_path / "tasks" / "accept", tmp_path / "tasks" / "accept"),
    ]
    for name, home, root in cases:
        env = lead.child_env(home)
        assert env[lead.HOME_ENV] == str(root), name
        assert env[lead.DUTY_HOME_ENV] == str(home), name


def test_env_names_are_the_same_in_the_tools_and_the_supervisor(tmp_path: Path) -> None:
    """Имена переменных — один контракт двух модулей, а не два похожих слова.

    Супервизор пишет `child_env`, инструменты читают `hermes_home`: разъехавшееся
    имя не сломало бы ни один из них по отдельности, и дело молча искалось бы не
    там. Тест сверяет имена, а не поведение — за поведение отвечают соседние.

    Модуль грузится под каноническим именем (`sys.modules`), как и везде: вторая
    копия `tools.py` означала бы вторую копию схемы и правил бюджета.
    """
    module = sys.modules.get("hermes_duty_tools")
    if module is None:
        path = ROOT / "dashboard" / "tools.py"
        spec = importlib.util.spec_from_file_location("hermes_duty_tools", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["hermes_duty_tools"] = module
        spec.loader.exec_module(module)
    assert (module.HOME_ENV, module.DUTY_HOME_ENV) == (lead.HOME_ENV, lead.DUTY_HOME_ENV)
    # И инструменты читают корень дела первым: тот же корень, что назвал
    # супервизор, — это и есть store дела, а не профиль сессии.
    home = tmp_path / "home"
    os.environ[lead.DUTY_HOME_ENV] = str(home)
    try:
        assert module.hermes_home() == home
    finally:
        os.environ.pop(lead.DUTY_HOME_ENV, None)


def test_session_stream_is_written_and_named_in_the_fact(tmp_path: Path, monkeypatch) -> None:
    """Заход пишет поток сессии файлом и называет его в `lead_started`.

    Ключи модели лида собирал `runner.spawn_process` (единственный шов спавна), и
    до приёмки 2.0 вывод сессии выбрасывался: «покажи, что сказал лид» было
    нечем выполнить. Теперь настоящий спавн получает каталог лога и окружение, а
    журнал — путь (`docs/ENGINE-2.0.md`, раздел 9).
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    conn.close()

    seen: dict[str, Any] = {}

    def fake_process(cmd: list[str], cwd: Path, env: Any = None, logs: Any = None) -> Any:
        seen.update({"cmd": list(cmd), "cwd": Path(cwd), "env": env, "logs": Path(logs)})
        return FakeProcess([stream_result("sess-log")])

    monkeypatch.setattr(lead.runner, "spawn_process", fake_process)
    report = lead.run(
        home,
        str(deal["id"]),
        now=NOW,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )

    assert report["started"] is True
    expected = lead.stream_dir(home, str(deal["id"]), lead.MODE_FIRST, NOW)
    assert seen["logs"] == expected
    assert report["session_log"] == str(expected)
    assert seen["env"][lead.DUTY_HOME_ENV] == str(home)
    assert seen["env"][lead.HOME_ENV] == str(lead.hermes_root(home))

    conn = open_store(home)
    started = facts_of(conn, deal, lead.LEAD_KIND_STARTED)
    conn.close()
    assert started[0]["session_log"] == str(expected)


def test_real_spawn_gives_the_child_both_homes_and_keeps_its_stream(tmp_path: Path, monkeypatch) -> None:
    """Настоящий спавн: ребёнок видит оба корня, поток ложится файлом.

    Единственный тест, где заход идёт через `runner.Process`, а не через фейк:
    модель заменена скриптом (`tests/fixtures/fake-hermes.sh`), который говорит
    те же две строки, что живая сессия в `--format stream-json`. Так проверяется
    стык, который фейк обходит: переменные доезжают до ребёнка, поток пишется в
    `<home>/duty/lead/…stream/stdout.log`, а `session_id` и расход берутся из
    `result` — то есть путь из факта `lead_started.session_log` ведёт к словам
    лида, а не в пустоту.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    conn.close()

    fake = ROOT / "tests" / "fixtures" / "fake-hermes.sh"
    assert fake.exists() and os.access(fake, os.X_OK)
    monkeypatch.setattr(lead.runner, "HERMES_BIN", str(fake))

    report = lead.run(home, str(deal["id"]), now=NOW, clock=lambda: NOW, monotonic=lambda: 0.0)

    assert report["started"] is True
    assert report["session_id"] == "sess-fake"
    assert report["usage_tokens"] == 7
    assert report["reason"] == "exit"
    stream = Path(report["session_log"])
    text = (stream / "stdout.log").read_text(encoding="utf-8")
    assert f'"duty_home":"{home}"' in text
    assert f'"hermes_home":"{lead.hermes_root(home)}"' in text

    conn = open_store(home)
    started = facts_of(conn, deal, lead.LEAD_KIND_STARTED)
    exited = facts_of(conn, deal, lead.LEAD_KIND_EXITED)
    conn.close()
    assert started[0]["session_log"] == str(stream)
    assert exited[0]["session_id"] == "sess-fake"
    assert exited[0]["usage_tokens"] == 7
