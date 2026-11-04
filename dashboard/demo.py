"""Прогон задачи через плагин — демонстрация на песочном корне.

Зачем это здесь. Спека (`~/.hermes/widgets/duty-model.html`) описывает не экран,
а модель труда: дело, этапы, попытки, доказательства, вопросы, журнал. Проверить
модель «на словах» нельзя, поэтому в плагине есть прогон: одна команда

    bin/duty demo

проходит дело от создания до закрытия на **своём** песочном корне и печатает то,
что действительно записалось в журнал и что показывают поверхности. Ни одна
живая БД при этом не открывается на запись: песочница создаётся во временном
каталоге, а `~/.hermes` только читается, чтобы взять DDL схем (пул и доска).

Граница честности. В 0.0.3 есть журнал, вопрос, гейт и владение делом; движка
(этапы, лизинг, спавн `hermes chat -p <role>`, проверка evidence, retry/reap)
нет. Прогон это не прячет: шаги, которые сегодня делает скрипт, помечены
`script`, а то, что появится с движком, — отдельным блоком в конце
(`engine_todo`). Иначе демонстрация врала бы ровно так, как спека запрещает.

Что проверяет сам прогон (и падает, если не сходится):

1. вопрос уводит дело в `wait_human`, ответ возвращает в работу;
2. поздний ответ на закрытое дело его не воскрешает (B3-класс);
3. гейт ребёнка открывается завершением родителя, а не вручную;
4. `/now`, `/deal` и журнал говорят одно и то же про одно дело;
5. `kanban.db` после прогона не изменился ни на байт (инвариант наблюдателя);
6. повторный `ingest` не плодит событий.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

DEMO_BOARD = "duty-demo"
DEMO_TITLE = "тристейт в виджете: разобрать, починить, проверить"

# Схемы песочницы. Живую БД не копируем (там данные), берём только DDL — так
# песочная `questions` не разъедется с настоящей. Если живого файла нет
# (чистая машина, CI), берётся встроенный минимум: те колонки, которые читает
# `ask()`.
POOL_FALLBACK = """
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  project TEXT,
  text TEXT NOT NULL,
  text_full TEXT,
  origin TEXT,
  options_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','sent','answered','expired','pickup_gateway')),
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

KANBAN_FALLBACK = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT,
    created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT,
    branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER,
    tenant TEXT, result TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER, last_failure_error TEXT, max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER, current_run_id INTEGER, block_kind TEXT,
    block_recurrences INTEGER NOT NULL DEFAULT 0, merged_into TEXT
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT,
    step_key TEXT, status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER,
    worker_pid INTEGER, worker_started_at INTEGER, ended_at INTEGER,
    started_at TEXT, outcome TEXT, last_output TEXT, error TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
"""

# Спека дела — тот же пример, что в `examples/demo.yaml`: прогон печатает её
# целиком, чтобы было видно, чего у 0.0.3 ещё нет (этапы и evidence-команды).
DEMO_SPEC: dict[str, Any] = {
    "duty": "sx-widgets-tristate",
    "repo": "~/GolandProjects/sx-widgets-api",
    "base": "origin/master",
    "stages": [
        {"id": "impl", "role": "implementer", "evidence": ["go build ./...", "go test ./widget/..."]},
        {"id": "verify", "role": "tester", "needs": ["impl"]},
        {"id": "review", "role": "reviewer", "needs": ["verify"], "verdict": {"approve": "done", "changes": "impl", "max_rounds": 2}},
    ],
}

ENGINE_TODO = (
    "этапы (stage) и их граф — движок 0.1.0",
    "лизинг и спавн `hermes chat -p <role>` — движок 0.1.0",
    "попытка как процесс: код выхода + result.json — движок 0.1.0",
    "проверка evidence до принятия работы — движок 0.1.0",
    "ретраи, reap, классификация отказа — движок 0.1.0",
)


class DemoError(RuntimeError):
    """Прогон не сошёлся: показывать его как успешный нельзя."""


def _now() -> int:
    return int(time.time())


def _ddl(db: Path, fallback: str) -> str:
    """DDL живой БД (только схема), иначе встроенный минимум."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return fallback
    sql = ";\n".join(str(row[0]) for row in rows if row[0])
    return f"{sql};\n" if sql.strip() else fallback


def _make_db(path: Path, ddl: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_sandbox(home: Path, live: Path) -> dict[str, Any]:
    """Песочный корень Hermes: свой журнал, свой пул, своя доска, свой профиль.

    `live` — живой `~/.hermes`: из него берём только DDL (на чтение).
    """
    if Path(home).resolve() == Path(live).resolve():
        raise DemoError("песочный корень не может совпадать с живым: прогон пишет в свой пул")
    home.mkdir(parents=True, exist_ok=True)
    pool = home / "messaging" / "pool.db"
    _make_db(pool, _ddl(live / "messaging" / "pool.db", POOL_FALLBACK))
    board = home / "kanban" / "boards" / DEMO_BOARD / "kanban.db"
    _make_db(board, _ddl(live / "kanban" / "boards" / "hermes-duty" / "kanban.db", KANBAN_FALLBACK))
    (home / "profiles" / "implementer").mkdir(parents=True, exist_ok=True)
    sqlite3.connect(home / "profiles" / "implementer" / "state.db").close()
    return {"home": home, "pool": pool, "board": board}


def _seed_observed(board: Path, now: int) -> dict[str, Any]:
    """Наблюдаемое kanban-дело: у него есть попытки — их видно только через ingest.

    Своё дело (`d_`) попыток не имеет: попытка — это процесс, а процессов в
    0.0.3 нет. Поэтому вторая половина прогона показывает, откуда берутся
    `attempt_started`/`attempt_ended`: из ранов живой доски, на чтение.
    """
    task_id = "t_a1b2c3d4"
    conn = sqlite3.connect(board)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, created_at, claim_lock,"
            " claim_expires, worker_pid, current_run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "kanban-дело с двумя попытками", "тело", "implementer", "running", now - 900,
             "demo-host:1", now + 600, 4242, 2),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, "created", json.dumps({"status": "ready"}), now - 900),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, "claimed", json.dumps({"run_id": 2}), now - 300),
        )
        conn.executemany(
            "INSERT INTO task_runs (task_id, profile, status, started_at, worker_started_at,"
            " ended_at, outcome) VALUES (?,?,?,?,?,?,?)",
            [
                (task_id, "implementer", "crashed", f"{now - 800}", now - 800, now - 700, "crashed"),
                (task_id, "implementer", "running", f"{now - 300}", now - 300, None, None),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_id": task_id, "path": board}


def run_demo(
    api: Any,
    *,
    home: Optional[Path] = None,
    live: Optional[Path] = None,
    keep: bool = False,
    echo: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Пройти дело по спеке на песочном корне и вернуть отчёт прогона.

    `api` — модуль `dashboard/plugin_api.py` (его же грузит плагин): прогон
    ходит через те же функции, что роуты, а не через второй набор правил.
    """
    say = echo or (lambda _line: None)
    live_root = Path(live) if live else Path.home() / ".hermes"
    sandbox = Path(home) if home else Path(tempfile.mkdtemp(prefix="duty-demo-"))
    sand = build_sandbox(sandbox, live_root)
    observed = _seed_observed(sand["board"], _now())
    board_before = _sha(sand["board"])

    env_before = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(sandbox)
    steps: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    try:
        journal = api.journal
        deals = api.deals
        ask = api.ask
        # Свой store открывает схему целиком (журнал + дела + доски): писать
        # прогон должен тем же соединением, что и мутации, иначе «таблицы нет».
        conn = deals.open_store(sandbox) or journal.open_journal(sandbox)
        assert conn is not None, "store не открылся"

        def step(n: int, what: str, detail: str, *, by: str = "script") -> None:
            steps.append({"n": n, "what": what, "detail": detail, "by": by})
            say(f"{n:>2}. {what:<26} {detail}  [{by}]")

        # 1. Дело заводится явно: карточки нет, статуса нет, есть событие created.
        created = deals.create_deal(conn, board=DEMO_BOARD, title=DEMO_TITLE,
                                    body="по спеке: impl → verify → review", assignee=None,
                                    known_boards=[DEMO_BOARD])
        deal_id = str(created["deal"]["id"])
        step(1, "create  (дело)", f"{deal_id} · status={created['deal']['status']}")

        # 2. Исполнитель берёт дело. В 0.0.3 это запись намерения, не аренда.
        deals.claim_deal(conn, deal_id, owner="implementer")
        step(2, "claim   (взял в работу)", "owner=implementer · status=running")

        # 3. Вопрос человеку — единственным каналом ask(): объект в пуле плюс
        #    событие в журнале, а не «block_kind в чужой таблице».
        asked = ask.ask(conn, {"id": deal_id, "board": DEMO_BOARD, "title": DEMO_TITLE,
                               "home": sandbox},
                        "Режем скоуп до одного виджета или чиним тристейт целиком?",
                        "Полная формулировка: в спеке три этапа; если режем — review не нужен.",
                        options=["режем", "чиним целиком"], critical=True)
        fold = journal.fold(journal.read_events(conn, deal_id))
        step(3, "ask     (вопрос в пул)", f"question #{asked.question_id} · ждёт: {fold.get('state')}/{fold.get('human_kind')}")

        # 4. Ответ приходит из пула (панель, iMessage — неважно), факт пишет sync.
        pool = sqlite3.connect(sand["pool"])
        try:
            pool.execute("UPDATE questions SET status='answered', answer='чиним целиком',"
                         " answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), answered_via='panel'"
                         " WHERE id=?", (asked.question_id,))
            pool.commit()
        finally:
            pool.close()
        synced = ask.sync_answers(conn, sandbox)
        fold = journal.fold(journal.read_events(conn, deal_id))
        step(4, "answer  (ответ получен)", f"+{synced['appended']} событие · дело снова в работе: {fold.get('state') or 'нет ожидания'}")

        # 5. Гейт: ребёнок ждёт родителя и открывается его завершением, не вручную.
        parent = deals.create_deal(conn, board=DEMO_BOARD, title="проверить контракт виджета",
                                   assignee="researcher", known_boards=[DEMO_BOARD])
        parent_id = str(parent["deal"]["id"])
        child = deals.create_deal(conn, board=DEMO_BOARD, title="прогнать тесты после фикса",
                                  parents=[parent_id], known_boards=[DEMO_BOARD])
        child_id = str(child["deal"]["id"])
        gate = journal.fold(journal.read_events(conn, child_id))
        step(5, "gate    (ждёт родителя)", f"{child_id} · {gate.get('state')} · родитель {parent_id}")
        deals.complete_deal(conn, parent_id, summary="контракт проверен, вопросов нет")
        gate = journal.fold(journal.read_events(conn, child_id))
        step(6, "gate    (открылся)", f"{child_id} · {gate.get('state') or 'ожидание снято'}")

        # 6. Комментарий и вложение: факты таймлайна, состояние не меняют.
        deals.add_comment(conn, deal_id, author="max", body="решение принято: чиним целиком")
        deals.attach_file(conn, sandbox, deal_id, filename="plan.md",
                          content=b"# plan\nimpl -> verify -> review\n", content_type="text/markdown")
        step(7, "comment (факт таймлайна)", "состояние ожидания не появилось")

        # 7. Попытки наблюдаемого kanban-дела: единственный источник — ingest.
        before = journal.counts(conn)
        report = journal.ingest(conn, sandbox, api._discover_boards(sandbox))
        after = journal.counts(conn)
        step(8, "ingest  (чужие факты)", f"+{report['appended']} событий · дел {before['deals']}→{after['deals']}")
        again = journal.ingest(conn, sandbox, api._discover_boards(sandbox))
        step(9, "ingest  (повтор)", f"+{again['appended']} событий (идемпотентно)")

        # 8. Поверхности читают журнал, а не колонки.
        now_payload = api.now()
        own_ids = [d["id"] for d in now_payload["deals"]]
        step(10, "surface /now", f"дел в срезе: {len(own_ids)} · наше {deal_id}: {'да' if deal_id in own_ids else 'НЕТ'}")

        # 9. Второй вопрос задаётся ДО закрытия: так и бывает на практике —
        #    спросили, а решение пришло позже. Ответ пока не пришёл.
        late = ask.ask(conn, {"id": deal_id, "board": DEMO_BOARD, "title": DEMO_TITLE, "home": sandbox},
                       "Оставить ли фича-флаг?", "дело закрывают, вопрос задан раньше",
                       critical=False)
        step(11, "ask     (второй вопрос)", f"question #{late.question_id} · дело снова wait_human")

        # 10. Закрытие. После него дело обязано уйти из среза.
        deals.complete_deal(conn, deal_id, summary="фикс влит, evidence-команды зелёные")
        card = api.deal(DEMO_BOARD, deal_id)
        now_after = [d["id"] for d in api.now()["deals"]]
        step(12, "complete", f"terminal={card.get('terminal')} · в /now: {'да' if deal_id in now_after else 'нет'}")

        # 11. Поздний ответ на второй вопрос не воскрешает закрытое дело.
        pool = sqlite3.connect(sand["pool"])
        try:
            pool.execute("UPDATE questions SET status='answered', answer='нет',"
                         " answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), answered_via='panel'"
                         " WHERE id=?", (late.question_id,))
            pool.commit()
        finally:
            pool.close()
        ask.sync_answers(conn, sandbox)
        folded = journal.fold(journal.read_events(conn, deal_id))
        card = api.deal(DEMO_BOARD, deal_id)
        step(13, "late answer", f"active={folded.get('active')} · waiting={'есть' if card.get('waiting') else 'нет'}")

        ledger = [
            {"id": e["id"], "kind": e["kind"], "ts": e["ts"], "detail": _event_line(e)}
            for e in journal.read_events(conn, deal_id)
        ]

        # ---------------------------------------------------------------- проверки
        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        check("вопрос увёл дело в wait_human",
              any(e["kind"] == "question_asked" for e in journal.read_events(conn, deal_id))
              and any(s["what"].startswith("ask") and "wait_human" in s["detail"] for s in steps),
              "question_asked в журнале и состояние wait_human на шаге 3")
        check("ответ вернул дело в работу",
              any(e["kind"] == "question_answered" for e in journal.read_events(conn, deal_id)),
              "question_answered после sync_answers")
        check("гейт открыт завершением родителя",
              any(e["kind"] == "gate_opened" for e in journal.read_events(conn, child_id)),
              "gate_opened у ребёнка")
        check("закрытое дело ушло из /now", deal_id not in now_after, "срез без закрытого дела")
        check("поздний ответ не воскресил дело", folded.get("active") is False and not card.get("waiting"),
              "fold.active=False и waiting отсутствует")
        check("повторный ingest не плодит событий", again["appended"] == 0, f"+{again['appended']}")
        check("kanban.db не изменён", _sha(sand["board"]) == board_before,
              "sha256 доски совпадает с исходным")

        failed = [c["name"] for c in checks if not c["ok"]]
        if failed:
            raise DemoError("прогон не сошёлся: " + ", ".join(failed))

        return {
            "ok": True,
            "home": str(sandbox),
            "board": DEMO_BOARD,
            "deal": deal_id,
            "parent": parent_id,
            "child": child_id,
            "observed": observed["task_id"],
            "spec": DEMO_SPEC,
            "steps": steps,
            "checks": checks,
            "ledger": ledger,
            "now": {"deals": len(now_after), "ids": now_after},
            "pool": {"questions": _questions(sand["pool"])},
            "engine_todo": list(ENGINE_TODO),
            "keep": keep,
        }
    finally:
        if env_before is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = env_before
        if not keep:
            # `--keep` — единственный способ сохранить песочницу: иначе прогон
            # не оставляет мусора ни во временном каталоге, ни в переданном.
            shutil.rmtree(sandbox, ignore_errors=True)


def _questions(pool: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{pool}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, session_key, status, kind, critical, answer FROM questions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "session_key": r[1], "status": r[2], "kind": r[3], "critical": r[4], "answer": r[5]}
        for r in rows
    ]


def _event_line(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    if event["kind"] == "question_asked":
        # Текст вопроса живёт в пуле (одна строка на вопрос), в журнале — только
        # ссылка на неё: иначе формулировка человека и журнал разошлись бы.
        return f"question #{payload.get('question_id')} · critical={payload.get('critical')}"
    if event["kind"] == "question_answered":
        return f"answer via {payload.get('answered_via') or '?'} · len={payload.get('answer_len')}"
    if event["kind"] in ("gate_opened", "gate_closed"):
        return f"{payload.get('from')} → {payload.get('to')} · parent {payload.get('parent')}"
    if event["kind"] == "created":
        return f"title: {str(payload.get('title') or '')[:48]}"
    if event["kind"] == "completed":
        return f"status → {payload.get('status') or 'done'}"
    if payload:
        return ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])
    return ""


def render_demo(report: dict[str, Any]) -> str:
    """Текст прогона для терминала: спека, шаги, журнал, проверки, граница."""
    out: list[str] = []
    out.append("ПРОГОН ЗАДАЧИ ЧЕРЕЗ HERMES-DUTY")
    out.append(f"песочный корень: {report['home']}  (живые БД только читались: DDL)")
    out.append("")
    spec = report["spec"]
    out.append("СПЕКА ДЕЛА (examples/demo.yaml) — движок 0.1.0 её исполнит, 0.0.3 показывает учёт:")
    out.append(f"  duty: {spec['duty']}   repo: {spec['repo']}   base: {spec['base']}")
    for stage in spec["stages"]:
        needs = f" needs={','.join(stage['needs'])}" if stage.get("needs") else ""
        ev = f" evidence={len(stage['evidence'])} команд" if stage.get("evidence") else ""
        out.append(f"  - {stage['id']:<6} role={stage['role']:<12}{needs}{ev}")
    out.append("")
    out.append("ШАГИ")
    for step in report["steps"]:
        out.append(f"{step['n']:>2}. {step['what']:<26} {step['detail']}")
    out.append("")
    out.append(f"ЖУРНАЛ ДЕЛА {report['deal']} (append-only, состояние = fold)")
    for event in report["ledger"]:
        out.append(f"  #{event['id']:<3} {event['kind']:<17} {event['detail']}")
    out.append("")
    out.append(f"ПУЛ ВОПРОСОВ: {len(report['pool']['questions'])} строк")
    for q in report["pool"]["questions"]:
        out.append(
            f"  #{q['id']} {q['session_key']:<18} status={q['status']:<9} kind={q['kind']:<9}"
            f" critical={q['critical']} answer={q['answer']!r}"
        )
    out.append("")
    out.append("ПРОВЕРКИ")
    for check in report["checks"]:
        out.append(f"  [{'OK' if check['ok'] else 'FAIL'}] {check['name']}: {check['detail']}")
    out.append("")
    out.append("ЧЕГО ЕЩЁ НЕТ (движок 0.1.0, вне 0.0.3):")
    for item in report["engine_todo"]:
        out.append(f"  · {item}")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    """`python dashboard/demo.py` — прогон без CLI (для ручной проверки)."""
    import argparse

    parser = argparse.ArgumentParser(prog="duty demo", description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--home", help="песочный корень (по умолчанию временный)")
    parser.add_argument("--live", help="живой корень Hermes, только для DDL")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep", action="store_true", help="не удалять песочницу")
    args = parser.parse_args(argv)
    import importlib.util
    import sys

    here = Path(__file__).resolve().parent
    spec_ = importlib.util.spec_from_file_location("hermes_duty_plugin_api", here / "plugin_api.py")
    assert spec_ is not None and spec_.loader is not None
    module = importlib.util.module_from_spec(spec_)
    sys.modules["hermes_duty_plugin_api"] = module
    spec_.loader.exec_module(module)
    report = run_demo(module, home=Path(args.home) if args.home else None,
                      live=Path(args.live) if args.live else None,
                      keep=args.keep, echo=None if args.json else print)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_demo(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
