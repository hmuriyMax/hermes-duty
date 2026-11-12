"""Классы повтора — политика движка, а не лида (`docs/ENGINE-2.0.md`, раздел 7).

Решение принято 11.11 по истории переходов «после отказа того же этапа»: `infra`
дал 9 done / 9 failed, `contract` — 8 done / 4 failed, `capability` — 0 done /
2 failed. Отсюда два правила, и оба проверяются здесь:

1. `infra` и `contract` движок повторяет **сам** в пределах `retry.max` этапа, а
   в бриф повторной попытки кладёт причину прошлого отказа;
2. `capability` не повторяется никогда, даже если спека объявила
   `retry.on: [capability]` с `max: 3`: `retry.on` набор сужает, но не расширяет.

Ни один тест не поднимает модель: `spawn` подменяется фейком, который задаёт код
выхода и поток процесса. Проверяется то, что движок **записал** — факты журнала,
файл контракта повторной попытки и тот единственный факт, который уходит лиду
после исчерпания повторов.
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

# Метка времени всех фактов теста: одинаковый `ts` не мешает — порядок задаёт id.
NOW = 1_790_000_000

# Схема пула вопросов: парковка после исчерпания повторов пишет в него вопрос.
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


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути — под тем именем, под которым его видит весь код."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = load_module("hermes_duty_runner", "runner.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
spec_module = load_module("hermes_duty_spec", "spec.py")
failures = load_module("hermes_duty_failures", "failures.py")
tools = load_module("hermes_duty_tools", "tools.py")


# ------------------------------------------------------------------ фикстуры


def make_home(tmp_path: Path) -> Path:
    """Корень Hermes: своя `duty/duty.db` и свой пул вопросов."""
    home = tmp_path / "home"
    (home / "messaging").mkdir(parents=True)
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()
    return home


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(conn: sqlite3.Connection) -> dict[str, Any]:
    created = deals.create_deal(conn, repo="duty", title="Починить шапку", body="", now=NOW)
    return deals.deal_row(conn, str(created["deal"]["id"]))


def write_spec(tmp_path: Path, text: str) -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def retry_spec(tmp_path: Path, retry: str, *, budget: str = "") -> Any:
    """Спека одного этапа без дерева с объявленной политикой повторов."""
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: impl
            role: implementer
            workspace: none
            retry: {retry}
        {textwrap.indent(textwrap.dedent(budget), "    ") if budget else ""}
        """,
    )


def lead_spec(tmp_path: Path, retry: str) -> Any:
    """Спека с этапом плана: переосмысление уходит лиду, а не человеку.

    Исчерпание повторов при живом этапе плана — факт лиду (`plan_rethink`), и
    проверять «один факт с числом повторов» надо на этой форме: без этапа плана
    движок спрашивает человека, и это другой путь.
    """
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: plan
            role: lead
            workspace: none
          - id: impl
            role: implementer
            workspace: none
            retry: {retry}
        """,
    )


class Fake:
    """Фейковый спавн попытки: код выхода и поток заданы, `result.json` не пишется.

    Отчёт не пишется намеренно: класс отказа тогда выводится движком по коду
    выхода (`infra`) или по его отсутствию при нуле (`contract`), а не берётся из
    текста. Строки потока нужны бюджетному случаю: `usage` больше бюджета этапа
    даёт `capability` — стену, которую повторять нельзя.
    """

    def __init__(self, *, exit_code: int = 0, lines: Optional[list[dict[str, Any]]] = None, pid: int = 4242) -> None:
        self.exit_code = exit_code
        self.pid = pid
        self.template = [json.dumps(line, ensure_ascii=False) + "\n" for line in (lines or [])]
        self.queue: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.stopped = 0
        self.stderr_text = ""

    # --- то, что делает спавн попытки
    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "Fake":
        query = Path(cmd[list(cmd).index("--query-file") + 1])
        contract = json.loads(query.read_text(encoding="utf-8"))
        self.calls.append({"contract": contract, "cwd": Path(cwd)})
        self.queue = list(self.template)
        return self

    # --- то, что нужно `runner.follow`
    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        return self.queue.pop(0) if self.queue else None

    def finished(self) -> bool:
        return True

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.exit_code

    def stop(self, grace: float = 10) -> Optional[int]:
        self.stopped += 1
        return self.exit_code


def events(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    return journal.read_events(conn, str(deal["id"]))


def kinds(conn: sqlite3.Connection, deal: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item for item in events(conn, deal) if str(item.get("kind") or "") == kind]


def attempts_started(conn: sqlite3.Connection, deal: dict[str, Any], stage: str = "impl") -> list[dict[str, Any]]:
    return [
        journal.payload_of(item)
        for item in kinds(conn, deal, "attempt_started")
        if str(journal.payload_of(item).get("stage") or "") == stage
    ]


# --------------------------------------------------- повторы infra и contract


def test_infra_is_retried_by_engine_with_reason_in_brief(tmp_path: Path) -> None:
    """`infra` повторяется сам: вторая попытка, причина первого отказа в брифе.

    Повтор — отдельная попытка со своим фактом (`retried_from` называет, чей
    отказ повторяют) и с `previous_failures` в контракте: без причины повтор вслепую
    повторил бы и ошибку (`docs/ENGINE-2.0.md`, раздел 7).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 1, on: [infra, contract]}")
    fake = Fake(exit_code=1)

    report = runner.run_attempts(conn, spec, deal, "impl", 1, home=home, spawn=fake, clock=lambda: NOW)

    assert report["outcome"] == failures.OUTCOME_FAILED
    assert (report["kind"], report["attempt"], report["retries"]) == ("infra", 2, 1)

    started = attempts_started(conn, deal)
    assert [row["attempt"] for row in started] == [1, 2]
    assert "retried_from" not in started[0]
    assert started[1]["retried_from"] == 1, "повтор движка называет, чей отказ повторяет"

    assert len(fake.calls) == 2
    previous = fake.calls[1]["contract"]["previous_failures"]
    assert [item["kind"] for item in previous] == ["infra"]
    assert "код выхода 1" in previous[0]["reason"], "причина первого отказа — в брифе второй попытки"


def test_contract_is_retried_by_engine_with_reason_in_brief(tmp_path: Path) -> None:
    """`contract` повторяется так же: отчёт не пришёл при нулевом выходе — это не стена."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 1, on: [infra, contract]}")
    fake = Fake(exit_code=0)

    report = runner.run_attempts(conn, spec, deal, "impl", 1, home=home, spawn=fake, clock=lambda: NOW)

    assert (report["kind"], report["attempt"], report["retries"]) == ("contract", 2, 1)
    previous = fake.calls[1]["contract"]["previous_failures"]
    assert [item["kind"] for item in previous] == ["contract"]
    assert previous[0]["reason"], "у контрактного отказа причина тоже есть"


def test_charter_max_attempts_caps_the_series(tmp_path: Path) -> None:
    """Серия упирается в потолок хартии, а не в `retry.max` этапа.

    `charter.max_attempts: 2` — две попытки на этап **включая** авто-повторы
    (`docs/ENGINE-2.0.md`, разделы 3 и 11), хотя политика этапа разрешает пять.
    Красная проба: без проверки в `run_attempts` движок сделал бы шесть попыток, и
    «3 попыток на этап» из хартии остались бы обещанием.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = write_spec(
        tmp_path,
        """
        duty: потолок хартии
        charter:
          max_attempts: 2
        stages:
          - id: impl
            role: implementer
            workspace: none
            retry: {max: 5, on: [infra]}
        """,
    )
    fake = Fake(exit_code=1)

    report = runner.run_attempts(conn, spec, deal, "impl", 1, home=home, spawn=fake, clock=lambda: NOW)

    assert (report["attempt"], report["retries"]) == (2, 1)
    assert report["attempts_ceiling"] == 2
    assert report["retry"] is False, "повтор за потолком хартии не поднимается"
    assert [row["attempt"] for row in attempts_started(conn, deal)] == [1, 2]


def test_retry_limit_bounds_the_series(tmp_path: Path) -> None:
    """`max: 2` — три попытки всего, дальше политика отказывает в повторе."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 2, on: [infra]}")
    fake = Fake(exit_code=1)

    report = runner.run_attempts(conn, spec, deal, "impl", 1, home=home, spawn=fake, clock=lambda: NOW)

    assert (report["attempt"], report["retries"]) == (3, 2)
    assert [row["attempt"] for row in attempts_started(conn, deal)] == [1, 2, 3]


# ------------------------------------------------------- capability не повторяется


def test_capability_is_never_retried_even_when_declared(tmp_path: Path) -> None:
    """`retry.on: [capability]` с `max: 3` повторов не включает: стена не рассосётся.

    Спека называет класс, который движок не повторяет никогда, — набор сужается
    до пустого, и попытка одна. Иначе движок вечно бился бы в отсутствующий
    доступ (0 done / 2 failed по истории переходов).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(
        tmp_path,
        "{max: 3, on: [capability]}",
        budget="""budget: {tokens: 10}""",
    )
    # Расход больше бюджета этапа и отчёта нет: `classify` называет это `capability`.
    fake = Fake(exit_code=0, lines=[{"type": "result", "tokens": {"total": 5000}}])

    report = runner.run_attempts(conn, spec, deal, "impl", 1, home=home, spawn=fake, clock=lambda: NOW)

    assert report["kind"] == "capability"
    assert report["retry"] is False
    assert len(fake.calls) == 1, "второй попытки быть не должно"
    started = attempts_started(conn, deal)
    assert [row["attempt"] for row in started] == [1]
    assert "retried_from" not in started[0]

    stage = next(item for item in spec.stages if item.id == "impl")
    policy = failures.policy(stage)
    assert policy.on == (), "объявленный capability сузился до пустого набора"
    assert policy.allows("capability") is False


def test_capability_stops_the_series_of_retries(tmp_path: Path) -> None:
    """Серия повторов обрывается на `capability`: следующий отказ — стена, не флейк.

    `retry.on: [infra, contract]` разрешает повторы по этим классам, но класс
    отказа берётся из факта: упёрлись в бюджет — повтора нет, даже если лимит
    `infra` ещё не выбран.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(
        tmp_path,
        "{max: 3, on: [infra, contract]}",
        budget="""budget: {tokens: 10}""",
    )
    fake = Fake(exit_code=0, lines=[{"type": "result", "tokens": {"total": 5000}}])

    report = runner.run_attempts(conn, spec, deal, "impl", 1, home=home, spawn=fake, clock=lambda: NOW)

    assert (report["kind"], report["attempt"]) == ("capability", 1)
    assert len(fake.calls) == 1


# ------------------------------------------------ `retry.on` сужает, не расширяет


def test_retry_on_narrows_the_engine_set(tmp_path: Path) -> None:
    """Эффективная политика — пересечение объявленного набора с политикой движка.

    Выбор зафиксирован: недозволенный класс **сужается** (а не отвергается
    спекой) — старые спеки остаются валидными, а движок всё равно не повторяет
    то, что повторять нельзя. Спека говорит об этом предупреждением.
    """
    narrow = failures.policy(next(item for item in retry_spec(tmp_path, "{max: 2, on: [contract]}").stages))
    assert (narrow.max, narrow.on) == (2, ("contract",))
    assert narrow.allows("infra") is False and narrow.allows("contract") is True

    mixed = failures.policy(next(item for item in retry_spec(tmp_path, "{max: 3, on: [infra, capability]}").stages))
    assert mixed.on == ("infra",), "capability выброшен, infra остался"
    assert mixed.allows("capability") is False

    nothing = failures.policy(next(item for item in retry_spec(tmp_path, "{max: 3, on: [capability]}").stages))
    assert (nothing.max, nothing.on) == (3, ()), "лимит остался, повторять нечего — и это не ошибка спеки"


def test_spec_warns_about_narrowed_class_without_rejecting(tmp_path: Path) -> None:
    """Спека объясняет сужение предупреждением, но спека остаётся валидной."""
    spec = retry_spec(tmp_path, "{max: 3, on: [capability]}")

    issues = spec_module.validate_spec(spec, profiles=frozenset({"implementer"}))

    assert [issue.rule for issue in issues] == ["retry.on"]
    assert issues[0].severity == spec_module.WARNING
    assert "capability" in issues[0].message and "infra, contract" in issues[0].message
    assert spec_module.errors(issues) == []


# ------------------------------------------- исчерпание: один факт лиду с числом


def test_exhausted_retries_go_to_lead_as_one_fact_with_count(tmp_path: Path) -> None:
    """Исчерпал повторы — лиду один факт: класс, число повторов, последняя причина.

    Лид решает по серии, а не по одной попытке: «2 автоматических повтора, оба
    `contract`» — это флейк, и он не должен выглядеть как провал работы. Факт
    один (`plan_rethink`), а не по одному на попытку, и человек при этом не
    спрашивается.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path, "{max: 1, on: [infra]}")
    fake = Fake(exit_code=1)

    report = runner.run_attempts(conn, spec, deal, "impl", 1, home=home, spawn=fake, clock=lambda: NOW)

    assert (report["attempt"], report["retries"]) == (2, 1)
    rethink = kinds(conn, deal, "plan_rethink")
    assert len(rethink) == 1, "лиду уходит один факт, а не по одному на попытку"
    payload = journal.payload_of(rethink[0])
    assert payload["failed_stage"] == "impl"
    assert payload["kind"] == "infra"
    assert payload["retries"] == {"count": 1, "kinds": {"infra": 1}}
    assert "код выхода 1" in payload["why"], "последняя причина — в том же факте"
    assert kinds(conn, deal, "blocked") == [], "человека не спрашивают: решает лид"
    # Дело возвращено лиду, а не брошено в `running`: диспетчер поднимает
    # рантаймы только `ready`/`todo`/`wait_slot`, и `running` без лизинга значил
    # бы «серия повторов закончилась ничем» (кейс d_c8551c18).
    assert deals.deal_row(conn, deal["id"])["status"] == "ready"


# --------------------------------------------------- связка с процессом 2.0


def test_attempt_process_leads_the_retry_series(tmp_path: Path, monkeypatch: Any) -> None:
    """Процесс попытки (2.0) идёт в `run_attempts`, а не в одну попытку.

    `duty_start` поднимает процесс, и серию повторов ведёт **он**: без этой
    связки авто-повторы существовали бы только в тестах, а лид по-прежнему
    поднимал бы каждый повтор вручную (`docs/ENGINE-2.0.md`, раздел 7).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = retry_spec(tmp_path, "{max: 2, on: [infra]}")
    created = deals.create_deal(
        conn, repo="duty", title="Починить шапку", body="", now=NOW, spec_path=str(spec.path)
    )
    deal = deals.deal_row(conn, str(created["deal"]["id"]))
    seen: dict[str, Any] = {}

    def fake_run_attempts(conn_: Any, spec_: Any, deal_: Any, stage_id: str, attempt: int, **kwargs: Any) -> dict[str, Any]:
        seen.update({"stage": stage_id, "attempt": attempt, **kwargs})
        return {"started": True}

    monkeypatch.setattr(tools.runner, "run_attempts", fake_run_attempts)

    code = tools._attempt_main([str(deal["id"]), "impl", "1", "ключ-лида", str(home)])

    assert code == 0
    assert (seen["stage"], seen["attempt"]) == ("impl", 1)
    assert seen["idem"] == "ключ-лида", "ключ лида доезжает до первой попытки серии"
    assert seen["answer"] is None

