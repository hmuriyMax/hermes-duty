"""Закрывающий этап: дело закрывает лид, а не «все этапы пройдены» (1.0).

Проверяется контракт `docs/ENGINE-1.0.md` (разделы 9 и 10) и `docs/SPEC.md`
(«`close`: дело закрывает лид») там, где его видно без модели:

1. валидатор отказывает `close` не-лиду, команде и этапу без приговора —
   до запуска, а не в момент закрытия;
2. приговор `approve` закрывает дело фактом `completed cause='lead_closed'`,
   и итог дела — `summary` лида из его отчёта, а не текст движка;
3. приговор `changes` возвращает дело на названный этап: оно не закрыто, а
   работа идёт заново;
4. незакрытый `close` при всех остальных закрытых этапах дело не закрывает — ни
   решением `transit`, ни писателем `deals.close_stages_done`;
5. граф с закрывающим этапом не закрывается «все этапы пройдены»: это отказ
   `DealError`, а не тихое закрытие мимо приговора.

Часы нигде не читаются — `now` передаётся аргументом, как и в остальных слоях.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

NOW = 1_791_000_000
DEAL = "d_close"


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
transit = load_module("hermes_duty_transit", "transit.py")
runner = load_module("hermes_duty_runner", "runner.py")
driver = load_module("hermes_duty_driver", "driver.py")
release = load_module("hermes_duty_release", "release.py")

from test_runner import Fake  # noqa: E402


# Спека 1.0: план, работа и закрывающий этап, который возвращает дело на работу.
GOOD = """
duty: дело 1.0
charter:
  max_stages: 6
  max_rounds_replan: 1
stages:
  - id: impl
    role: implementer
    workspace: none
  - id: close
    role: lead
    needs: [impl]
    workspace: none
    verdict: {approve: next, changes: impl, max_rounds: 2}
"""


def write_spec(tmp_path: Path, text: str, name: str = "duty.yaml") -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def issues(tmp_path: Path, text: str, name: str = "broken.yaml") -> list[Any]:
    """Отказы валидатора по спеке: профили подставлены явно, машина не при чём."""
    found = spec_module.validate_spec(write_spec(tmp_path, text, name), profiles=frozenset({"lead", "implementer", "reviewer"}))
    return found


def rules(found: list[Any]) -> set[str]:
    return {issue.rule for issue in found}


def events(*items: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """События журнала в форме `journal.read_events`: id растёт, `ts` один."""
    return [
        {"id": index + 1, "ts": NOW, "deal_id": DEAL, "kind": kind, "payload": payload, "source": "engine"}
        for index, (kind, payload) in enumerate(items)
    ]


def ended(stage: str, attempt: int = 1, summary: str = "") -> dict[str, Any]:
    """`attempt_ended` принятой попытки: в `result` лежит отчёт этапа."""
    return {
        "stage": stage,
        "attempt": attempt,
        "outcome": "done",
        "kind": None,
        "run_id": f"{stage}#{attempt}",
        "reason": "",
        "result": {"status": "done", "summary": summary},
    }


def approve(stage: str, target: str = "next") -> tuple[str, dict[str, Any]]:
    return ("collected", {"stage": stage, "target": target, "reason": "", "attempt": 1})


def changes(stage: str, target: str) -> tuple[str, dict[str, Any]]:
    return ("changes_requested", {"stage": stage, "target": target, "reason": "не принято", "attempt": 1})


# ------------------------------------------------------------------ фикстуры


def make_home(tmp_path: Path) -> Path:
    """Корень Hermes: своя `duty/duty.db` и свой (пустой) пул вопросов."""
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


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(conn: sqlite3.Connection, spec: Any) -> dict[str, Any]:
    """Своё дело: строка store плюс путь спеки, записанный в `created`."""
    created = deals.create_deal(
        conn, repo="duty", title="Дело 1.0", body="", now=NOW, spec_path=str(spec.path)
    )
    deal = deals.deal_row(conn, str(created["deal"]["id"]))
    assert deal is not None
    return deal


def append(conn: sqlite3.Connection, deal: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """Факты журнала так, как их пишет движок: свой ключ идемпотентности на факт."""
    journal.append(
        conn,
        [
            {
                "ts": NOW,
                "recorded_at": NOW,
                "deal_id": deal["id"],
                "kind": item["kind"],
                "payload": item["payload"],
                "source": "engine",
                "idempotency_key": deals.event_key(
                    "engine", str(deal["id"]), str(item["kind"]), dict(item["payload"])
                ),
            }
            for item in items
        ],
    )


# ------------------------------------------------- 1. валидатор: кто закрывает


def test_lead_closes_the_deal(tmp_path: Path) -> None:
    """Годная спека 1.0 проходит: `close` — лид, с приговором и без команды."""
    assert rules(issues(tmp_path, GOOD)) == set()


def test_close_with_a_foreign_role_is_refused(tmp_path: Path) -> None:
    """Закрывает координатор, а не исполнитель: чужая роль — отказ `close.role`."""
    found = issues(tmp_path, GOOD.replace("role: lead", "role: implementer"))

    assert "close.role" in rules(found)
    assert any("координатор" in issue.message for issue in found if issue.rule == "close.role")


def test_close_keeps_the_lead_role_of_old_seeds(tmp_path: Path) -> None:
    """Seed-спеки до переименования (`close` ролью lead) остаются валидными."""
    assert rules(issues(tmp_path, GOOD)) == set()


def test_close_as_a_shell_command_is_refused(tmp_path: Path) -> None:
    """Командой дело не закрывается: у `shell` нет приговора, только код возврата."""
    shell = GOOD.replace(
        "    role: lead\n    needs: [impl]\n    workspace: none\n"
        "    verdict: {approve: next, changes: impl, max_rounds: 2}\n",
        '    kind: shell\n    cmd: "true"\n    needs: [impl]\n    workspace: none\n',
    )
    found = issues(tmp_path, shell)

    assert "close.kind" in rules(found)


def test_close_without_a_verdict_is_refused(tmp_path: Path) -> None:
    """Без приговора этап «просто закончится»: закрывать дело будет нечем."""
    bare = GOOD.replace(
        "    verdict: {approve: next, changes: impl, max_rounds: 2}\n", ""
    )
    found = issues(tmp_path, bare)

    assert "close.verdict" in rules(found)


# --------------------------------- 1а. координатор ведёт дело, но не работает


def test_coordinator_scope_refuses_a_worker_stage(tmp_path: Path) -> None:
    """Этап-исполнитель с ролью координатора — отказ `coordinator.scope`.

    Кейс d_48e01a0e: план объявил `check` ролью координатора, и сверка доки с
    кодом легла в контекст координатора вместо этапа `researcher`.
    """
    worker = GOOD.replace(
        "    workspace: none\n  - id: close\n",
        "    workspace: none\n  - id: audit\n    role: coordinator\n    needs: [impl]\n"
        "    workspace: none\n  - id: close\n",
    )
    found = issues(tmp_path, worker)

    assert "coordinator.scope" in rules(found)
    assert any("координатор" in issue.message for issue in found if issue.rule == "coordinator.scope")


def test_coordinator_scope_allows_entry_and_close(tmp_path: Path) -> None:
    """Вход и `close` — ровно те две позиции, где координаторская роль законна."""
    seed = GOOD.replace("  - id: impl\n    role: implementer\n", "  - id: coordinator\n    role: coordinator\n").replace(
        "    needs: [impl]", "    needs: [coordinator]"
    ).replace("changes: impl", "changes: coordinator").replace("role: lead", "role: coordinator")

    assert rules(issues(tmp_path, seed)) == set()


# ------------------------------------------ 2. approve закрывает дело решением


def test_approve_lets_transit_close_the_graph(tmp_path: Path) -> None:
    """Приговор `approve` — последний факт: граф пройден, `transit` говорит `done`."""
    spec = write_spec(tmp_path, GOOD)
    facts = events(
        ("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "impl#1"}),
        ("attempt_ended", ended("impl", summary="сделано")),
        ("attempt_started", {"stage": "close", "attempt": 1, "run_id": "close#1"}),
        ("attempt_ended", ended("close", summary="итог: сделано, проверено")),
        approve("close"),
    )

    assert transit.next_stage(spec, facts, NOW).kind == transit.DECISION_DONE


# --------------------------------------- 3. незакрытый close держит дело живым


def test_unclosed_close_does_not_close_the_deal(tmp_path: Path) -> None:
    """Все прочие этапы закрыты, а `close` нет — дело не закрыто, а ждёт его."""
    spec = write_spec(tmp_path, GOOD)
    facts = events(
        ("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "impl#1"}),
        ("attempt_ended", ended("impl", summary="сделано")),
    )

    decision = transit.next_stage(spec, facts, NOW)

    assert decision.kind != transit.DECISION_DONE
    assert decision.pair == [("close", 1)]


def test_close_stages_done_refuses_a_graph_with_the_closing_stage(tmp_path: Path) -> None:
    """«Все этапы пройдены» делом не командует: у графа есть лид, и решает он."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, GOOD)
    deal = make_deal(conn, spec)

    try:
        deals.close_stages_done(conn, str(deal["id"]), stages=["impl", "close"], now=NOW)
    except deals.DealError as exc:
        assert "lead_closed" in str(exc)
    else:  # pragma: no cover - отказ обязателен
        raise AssertionError("close_stages_done закрыл дело мимо приговора лида")
    assert deals.deal_row(conn, str(deal["id"]))["status"] != "done"


def test_closure_without_approve_writes_nothing(tmp_path: Path) -> None:
    """`close` закончился попыткой, а приговора нет — прогон дело не закрывает."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, GOOD)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        [
            {"kind": "attempt_started", "payload": {"stage": "impl", "attempt": 1, "run_id": "impl#1"}},
            {"kind": "attempt_ended", "payload": ended("impl", summary="сделано")},
            {"kind": "attempt_started", "payload": {"stage": "close", "attempt": 1, "run_id": "close#1"}},
            {"kind": "attempt_ended", "payload": ended("close", summary="итог")},
        ],
    )

    closed = driver._closure(conn, deal, spec, float(NOW))

    assert closed["written"] is False
    assert "approve" in closed["refused"]
    assert journal.closed_fact(journal.read_events(conn, str(deal["id"]))) is None


def test_plan_added_stage_keeps_the_deal_open(tmp_path: Path) -> None:
    """`done` считается по графу журнала: добавленный планом этап держит дело.

    Файл спеки знает только `plan`, и он пройден. Принятый план добавил `research`,
    и его попытка идёт: «все этапы закрыты» по одному файлу закрыло бы дело поверх
    живой работы (`docs/ENGINE-1.0.md`, раздел 3), а её коммит остался бы
    неопубликованным.
    """
    spec = write_spec(
        tmp_path,
        """
duty: план закрывает дело
stages:
  - id: plan
    role: implementer
    workspace: none
""",
        name="plan-only.yaml",
    )
    facts = events(
        ("attempt_started", {"stage": "plan", "attempt": 1, "run_id": "plan#1"}),
        ("attempt_ended", ended("plan", summary="план")),
        (
            "plan_proposed",
            {
                "rev": 1,
                "stages": [
                    {
                        "id": "research",
                        "role": "implementer",
                        "needs": ["plan"],
                        "workspace": "none",
                        "evidence": [{"cmd": "true", "must": 0}],
                    }
                ],
                "reason": "скоуп",
                "source_stage": "plan",
                "attempt": 1,
            },
        ),
        ("plan_accepted", {"rev": 1, "by": "engine"}),
        ("attempt_started", {"stage": "research", "attempt": 1, "run_id": "research#1"}),
    )

    decision = transit.next_stage(spec, facts, NOW)

    assert decision.kind == transit.WAIT
    assert decision.what == transit.WAIT_ATTEMPT
    assert "research" in decision.reason


# ---------------------------------------- 4. changes возвращает дело в работу


def test_changes_sends_the_deal_back_to_the_named_stage(tmp_path: Path) -> None:
    """`changes: impl` — дело не закрыто, а работа `impl` начата заново."""
    spec = write_spec(tmp_path, GOOD)
    facts = events(
        ("attempt_started", {"stage": "impl", "attempt": 1, "run_id": "impl#1"}),
        ("attempt_ended", ended("impl", summary="сделано")),
        ("attempt_started", {"stage": "close", "attempt": 1, "run_id": "close#1"}),
        ("attempt_ended", ended("close", summary="итог не принят")),
        changes("close", "impl"),
    )

    states = transit.stage_states(spec, facts)
    assert states["impl"].state == "pending"
    assert states["close"].state != "done"
    assert "impl" in states["close"].reason
    decision = transit.next_stage(spec, facts, NOW)
    assert decision.kind == transit.RUN
    assert decision.pair == [("impl", 2)]


# ------------------------------ 5. сквозной прогон: лид отчитывается и закрывает


def test_lead_report_closes_the_deal_with_his_summary(tmp_path: Path) -> None:
    """Отчёт лида с приговором `approve` → `completed cause=lead_closed` и его итог.

    Проверяются три стыка сразу: приговор становится фактом `collected` (пишет
    `runner.report_verdict`), `transit` по нему говорит `done`, а `_closure`
    закрывает дело **словом лида** — `summary` из `result.json` в payload.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, GOOD)
    deal = make_deal(conn, spec)

    done = runner.run_stage(
        conn,
        spec,
        deal,
        "impl",
        1,
        home=home,
        spawn=Fake(result={"status": "done", "summary": "сделано"}, pack=True),
        clock=lambda: NOW,
    )
    assert done["outcome"] == "done", (done.get("kind"), done.get("reason"))

    closed_by_lead = runner.run_stage(
        conn,
        spec,
        deal,
        "close",
        1,
        home=home,
        spawn=Fake(
            result={
                "status": "done",
                "summary": "Работа сделана и проверена: 12 тестов зелёные",
                "verdict": {"action": "approve", "target": "next"},
            },
            pack=True,
        ),
        clock=lambda: NOW,
    )
    assert closed_by_lead["outcome"] == "done", (
        closed_by_lead.get("kind"),
        closed_by_lead.get("reason"),
    )

    fact = journal.closed_fact(journal.read_events(conn, str(deal["id"])))
    assert fact is None, "этап не закрывает дело сам — это делает прогон"
    report = driver._closure(conn, deal, spec, float(NOW))
    assert report["written"] is True
    fact = journal.closed_fact(journal.read_events(conn, str(deal["id"])))
    assert fact is not None
    payload = fact["payload"]
    assert payload["cause"] == deals.CLOSE_CAUSE_LEAD
    assert payload["summary"] == "Работа сделана и проверена: 12 тестов зелёные"
    assert payload["stages"] == ["impl", "close"]
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "done"


def test_repeat_closure_names_the_lead_fact(tmp_path: Path) -> None:
    """Повторный прогон второго `completed` не пишет и называет факт лида."""
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, GOOD)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        [
            {"kind": "attempt_ended", "payload": ended("impl", summary="сделано")},
            {"kind": "attempt_ended", "payload": ended("close", summary="итог дела")},
            {"kind": "collected", "payload": approve("close")[1]},
        ],
    )

    first = driver._closure(conn, deal, spec, float(NOW))
    second = driver._closure(conn, deal, spec, float(NOW))

    assert first["written"] is True and first["cause"] == deals.CLOSE_CAUSE_LEAD
    assert second["already"] is True and second["cause"] == deals.CLOSE_CAUSE_LEAD
    completions = [
        event for event in journal.read_events(conn, str(deal["id"])) if event["kind"] == "completed"
    ]
    assert len(completions) == 1


# --------------------- 6. второй писатель закрытия: проход отпускания


def running(conn: sqlite3.Connection, deal: dict[str, Any]) -> None:
    """Строка `running` — так её ставит движок, взяв лизинг (как в `test_release`)."""
    with conn:
        deals._set_status(conn, deal, "running", NOW)


def test_release_closes_the_deal_with_the_lead_word(tmp_path: Path) -> None:
    """Движок ушёл после `approve` — проход отпускания закрывает дело словом лида.

    Тот же класс, что «дело висит `running`, когда движка нет» (0.0.8): приговор
    лида в журнале есть, а `completed` нет. Второй писатель закрытия обязан
    закрывать **той же причиной и тем же итогом**, что прогон: `cause='lead_closed'`
    поверх графа с `close` — отказ, а не тихое закрытие мимо приговора.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, GOOD)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        [
            {"kind": "attempt_ended", "payload": ended("impl", summary="сделано")},
            {"kind": "attempt_ended", "payload": ended("close", summary="итог лида: сделано и проверено")},
            {"kind": "collected", "payload": approve("close")[1]},
        ],
    )
    running(conn, deal)

    rows = release.sweep(conn, NOW + 300, home=home)

    assert len(rows) == 1, rows
    assert rows[0]["action"] == "closed", rows
    assert rows[0]["cause"] == deals.CLOSE_CAUSE_LEAD
    fact = journal.closed_fact(journal.read_events(conn, str(deal["id"])))
    assert fact is not None
    assert fact["payload"]["cause"] == deals.CLOSE_CAUSE_LEAD
    assert fact["payload"]["summary"] == "итог лида: сделано и проверено"
    assert fact["payload"]["stages"] == ["impl", "close"]
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "done"


def test_release_does_not_close_without_the_verdict(tmp_path: Path) -> None:
    """`close` закончился попыткой без приговора: проход дело не закрывает, а называет отказ.

    `transit` видит «все этапы закрыты», но приговора нет — и закрытие от этого не
    появляется. Отказ виден строкой `error`, а дело остаётся `running`: решает
    человек (`bin/duty close`) или новый приговор лида, а не ушедший движок.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    spec = write_spec(tmp_path, GOOD)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        [
            {"kind": "attempt_ended", "payload": ended("impl", summary="сделано")},
            {"kind": "attempt_ended", "payload": ended("close", summary="итог без приговора")},
        ],
    )
    running(conn, deal)
    decision = transit.next_stage(spec, journal.read_events(conn, str(deal["id"])), NOW + 300)
    assert decision.kind == transit.DECISION_DONE, decision.kind

    rows = release.sweep(conn, NOW + 300, home=home)

    assert len(rows) == 1, rows
    assert "approve" in str(rows[0].get("error") or ""), rows
    assert journal.closed_fact(journal.read_events(conn, str(deal["id"]))) is None
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "running"



def test_coordinator_seed_validates_and_spawns_lead_profile(tmp_path: Path) -> None:
    """Новое имя: вход `coordinator` ролью `coordinator`, close — тот же координатор.

    Решение Макса (11.11): первого лида зовём не plan, а coordinator; роль —
    координатор и в начале дела, и по ходу; close делает тот же координатор.
    Профиля `coordinator` в Hermes нет — спавн мапится на профиль `lead`
    (`spec.spawn_profile`), права не расширяются.
    """
    coordinator_seed = GOOD.replace("id: plan", "id: coordinator").replace("role: lead", "role: coordinator")
    coordinator_seed = coordinator_seed.replace("needs: [plan]", "needs: [coordinator]").replace(
        "changes: plan", "changes: coordinator"
    )
    assert rules(issues(tmp_path, coordinator_seed)) == set()
    # спавн: роль coordinator живёт под профилем lead
    runner_module = load_module("hermes_duty_runner", "runner.py")
    command = runner_module.spawn_command("coordinator", tmp_path / "query.txt")
    assert "lead" in command
    # прежнее имя валидно дальше: журналы дел до переименования
    assert rules(issues(tmp_path, GOOD)) == set()
