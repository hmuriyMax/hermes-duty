"""Свой журнал дел: события, fold по ним и ingest (контракт `docs/JOURNAL.md`).

До 0.0.3 состояние дела выводилось из изменяемых колонок kanban (`status`,
`claim_lock`, `claim_expires`, `task_runs`) — то есть из того же места, которое
отвечает и за факт, и за политику. Здесь состояние считается из событий:
`state(deal) = fold(events)`.

Границы модуля по контракту:

- журнал — собственность плагина: `$HERMES_HOME/duty/duty.db`, режим WAL;
- чужие БД (`kanban.db`, `state.db`, `pool.db`) открываются только на чтение
  (`connect_ro` — единственное место, где этот модуль их касается), а пишем мы
  исключительно в свой журнал;
- `fold` — чистая функция: на вход список событий, на выход состояние ожидания.
  Ни БД, ни файлов она не открывает. Внешние факты, без которых правила 4–5
  контракта не сформулировать, — часы и живость pid: это не чтение источников;
- ingest ничего не «додумывает»: нет строки в источнике — нет события.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional

# Каталог и файл журнала рядом с остальными БД Hermes: `duty/` — наш namespace.
JOURNAL_SUBDIR = "duty"
JOURNAL_FILE = "duty.db"

# Сколько ждать чужую запись, а не сколько живёт запрос (как в `plugin_api`).
BUSY_TIMEOUT_MS = 2000

# Лизинг, за которым не началась попытка, считается протухшим через этот порог
# (`docs/JOURNAL.md`, fold правило 4).
CLAIM_STALE_SEC = 900

# Восемь взаимоисключающих состояний ожидания (`docs/DESIGN.md`) — и порядок
# приоритета при совпадении фактов. Одна таблица на весь бэкенд: журнал и
# поверхность берут порядок отсюда, второй копии нет.
WAITING_PRIORITY = (
    "wait_human",
    "capability",
    "dead_lease",
    "crashed",
    "working",
    "no_signal",
    "wait_slot",
    "wait_gate",
)

# Терминальные статусы карточки: такие дела не «дела сейчас».
TERMINAL_STATUSES = ("done", "archived")

# Слова, по которым причина блока читается как «нужен доступ или права».
CAPABILITY_WORDS = (
    "доступ",
    "права",
    "прав.",
    "разрешени",
    "permission",
    "access",
    "credential",
    "token",
    "forbidden",
    "403",
)

# Закрытый набор `kind` (контракт): новый вид события заводится вместе с
# правилом fold и тестом, а не «по пути».
EVENT_KINDS = (
    "created",
    "claimed",
    "lease_lost",
    "attempt_started",
    "attempt_ended",
    "blocked",
    "unblocked",
    "completed",
    "question_asked",
    "question_answered",
)

# Виды kanban-событий, которые переносятся в журнал один-к-одному.
INGEST_EVENT_KINDS = ("created", "claimed", "blocked", "unblocked", "completed", "archived")

# Порядок применения фактов с одинаковым `ts`: внутри одной секунды сначала
# «дело появилось», потом лизинг, попытка, блок, её снятие, конец попытки и
# завершение дела. Без этого при равном `ts` порядок `id` решался бы порядком
# таблиц-источников, и fold мог прочитать завершение раньше его начала.
KIND_ORDER = (
    "created",
    "claimed",
    "attempt_started",
    "blocked",
    "unblocked",
    "question_asked",
    "question_answered",
    "attempt_ended",
    "lease_lost",
    "completed",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              INTEGER NOT NULL,
  recorded_at     INTEGER NOT NULL,
  deal_id         TEXT    NOT NULL,
  board           TEXT    NOT NULL,
  kind            TEXT    NOT NULL,
  payload         TEXT    NOT NULL DEFAULT '{}',
  source          TEXT    NOT NULL,
  idempotency_key TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS events_idem ON events(idempotency_key);
CREATE INDEX IF NOT EXISTS events_deal ON events(deal_id, id);
"""

# Сколько строк источника берём в один запрос `IN (...)`: предел переменных
# SQLite (999) не должен превращать проход ingest в ошибку на большой доске.
IN_CHUNK = 400

# Длина текста причины блока в evidence.
REASON_LIMIT = 200


def _now_sec() -> int:
    """Текущее время в unix-секундах — как все `*_at` в источниках."""
    return int(time.time())


def journal_path(home: Path) -> Path:
    """Путь журнала в корне Hermes: `<home>/duty/duty.db`."""
    return Path(home) / JOURNAL_SUBDIR / JOURNAL_FILE


def connect_ro(path: Path, timeout: float = BUSY_TIMEOUT_MS / 1000) -> sqlite3.Connection:
    """Read-only соединение со снимком: `?mode=ro` + `BEGIN`.

    Единственный способ, которым duty открывает чужую БД: `mode=ro` не создаёт
    файл и не берёт write-lock, а `BEGIN` фиксирует одно состояние на срез.
    """
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    return conn


def open_journal(home: Path, *, create: bool = True) -> Optional[sqlite3.Connection]:
    """Соединение с журналом: `create=True` создаёт файл и схему при первом запуске.

    Отсутствие файла — не ошибка, а «журнала ещё нет»: с `create=True` это
    превращается в пустой журнал, с `create=False` — в `None`. Читающим роутам
    нужен второй режим: GET не должен создавать файл как побочный эффект, иначе
    «посмотрел /now» и «включил журнал» стали бы одним действием.
    """
    path = journal_path(home)
    if not create and not path.is_file():
        return None
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not create:
        return conn
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    return conn


def append(conn: sqlite3.Connection, events: Iterable[dict[str, Any]]) -> int:
    """Дописать события в журнал; вернуть число реально записанных строк.

    `INSERT OR IGNORE` по уникальному `idempotency_key`: повторный проход с теми
    же фактами даёт 0, и это видно числом, а не на глаз. Пачка пишется одной
    транзакцией и в порядке `(ts, KIND_ORDER)` — потому что порядок применения в
    fold это порядок `id`, и он обязан совпадать с порядком фактов.
    """
    ordered = sorted(events, key=lambda ev: (int(ev["ts"]), _kind_rank(str(ev["kind"]))))
    if not ordered:
        return 0
    rows = [
        (
            int(ev["ts"]),
            int(ev["recorded_at"]),
            str(ev["deal_id"]),
            str(ev["board"]),
            str(ev["kind"]),
            json.dumps(ev.get("payload") or {}, ensure_ascii=False),
            str(ev.get("source") or "ingest"),
            str(ev["idempotency_key"]),
        )
        for ev in ordered
    ]
    before = conn.total_changes
    with conn:  # одна транзакция на пачку
        conn.executemany(
            "INSERT OR IGNORE INTO events "
            "(ts, recorded_at, deal_id, board, kind, payload, source, idempotency_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    return conn.total_changes - before


def read_events(
    conn: sqlite3.Connection, deal_id: Optional[str] = None, since_id: int = 0
) -> list[dict[str, Any]]:
    """События журнала по возрастанию `id`; `deal_id`/`since_id` — необязательный отбор.

    `payload` разворачивается в объект: битый JSON — не повод ронять срез, но и
    не повод молчать, поэтому неразобранное поле отдаётся как есть в `payload_raw`.
    """
    sql = (
        "SELECT id, ts, recorded_at, deal_id, board, kind, payload, source, idempotency_key "
        "FROM events WHERE id > ?"
    )
    params: list[Any] = [int(since_id)]
    if deal_id is not None:
        sql += " AND deal_id = ?"
        params.append(str(deal_id))
    sql += " ORDER BY id"
    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, tuple(params)):
        item = dict(row)
        item["payload"] = _payload(item)
        out.append(item)
    return out


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Сколько в журнале: событий и дел (для `/health`)."""
    row = conn.execute("SELECT COUNT(*) AS events, COUNT(DISTINCT deal_id) AS deals FROM events").fetchone()
    return {"events": int(row["events"] or 0), "deals": int(row["deals"] or 0)}


def deal_ids(conn: sqlite3.Connection) -> set[str]:
    """Все дела, о которых в журнале есть хотя бы одно событие."""
    return {str(row[0]) for row in conn.execute("SELECT DISTINCT deal_id FROM events")}


# ------------------------------------------------------------------ fold


def fold(events: Iterable[dict[str, Any]], *, now: Optional[int] = None) -> dict[str, Any]:
    """Состояние ожидания дела по его событиям — правила 1–7 контракта.

    Чистая функция: ни БД, ни файлов. События применяются в порядке `id`
    (правило 1). Ответ:

    - `active: False` — последнее событие `completed` (правило 2): дело не
      активно, в `/now` не попадает; `state` тогда `None`.
    - `state` из восьми состояний — сработал живой факт (правила 3–6, 8):
      блок, вопрос человеку, лизинг без попытки, попытка без завершения,
      снимок `created` с незакрытыми родителями;
    - `state: None` с `reason: "empty"` — событий нет вовсе (правило 7), а с
      `reason: "no_live_fact"` — события есть, но живого факта в них нет
      (например, только `created`). В обоих случаях состояние считает
      поверхность по kanban и говорит это в `evidence`: «нет данных» нельзя
      выдавать за «всё спокойно».

    `now` подставляется тестами; по умолчанию берутся часы.
    """
    ordered = sorted(events, key=lambda ev: int(ev.get("id") or 0))
    if not ordered:
        return _verdict(None, None, None, "empty", None)
    last_kind = str(ordered[-1].get("kind") or "")
    now_sec = _now_sec() if now is None else int(now)

    if last_kind == "completed":
        status = _payload(ordered[-1]).get("status") or "done"
        return _verdict(False, None, f"журнал: последнее событие completed (status={status})", "closed", last_kind)

    facts: dict[str, list[str]] = {}
    question = _open_question(ordered)

    # 6. Вопрос человеку: `question_asked` без ответа. Это и есть «ждёт ответа» —
    #    блокировка без вопроса даёт тот же wait_human, но с human_kind=status.
    if question is not None:
        payload = _payload(question[1])
        facts.setdefault("wait_human", []).append(
            f"вопрос #{payload.get('question_id') or '—'} задан {_ago(question[1].get('ts'), now_sec)}, "
            f"ответа нет (session_key {payload.get('session_key') or '—'})"
        )

    # 3. Блок без последующего `unblocked`. Доступ/права — отдельное состояние
    #    `capability`, как в 0.0.2: это не «человек решает», а «человек даёт доступ».
    block = _open_block(ordered)
    if block is not None:
        payload = _payload(block[1])
        block_kind = str(payload.get("block_kind") or "")
        reason = str(payload.get("reason") or "")
        text = f"blocked: block_kind={block_kind or '—'}, причина: {_short(reason) or 'не записана'}"
        if block_kind == "capability" or any(word in reason.lower() for word in CAPABILITY_WORDS):
            facts.setdefault("capability", []).append(text)
        else:
            facts.setdefault("wait_human", []).append(text)

    # 4. Лизинг, за которым не началась попытка: дело «взяли» и не пришли.
    claim = _last_of_kind(ordered, "claimed")
    if claim is not None:
        claim_index, claim_event = claim
        started = any(str(ev.get("kind")) == "attempt_started" for ev in ordered[claim_index + 1 :])
        claim_ts = int(claim_event.get("ts") or 0)
        if not started and now_sec - claim_ts > CLAIM_STALE_SEC:
            payload = _payload(claim_event)
            facts.setdefault("dead_lease", []).append(
                f"лизинг {payload.get('claim_lock') or '—'} взят {_ago(claim_ts, now_sec)}, "
                f"attempt_started за ним нет (expires {payload.get('claim_expires') or '—'})"
            )

    # 5. Попытка без завершения: живость pid — единственный сигнал, который в
    #    закрытом наборе событий есть. Сердцебиение в журнал не пишется, и это
    #    сказано в evidence, а не подразумевается.
    attempt = _open_attempt(ordered)
    if attempt is not None:
        payload = _payload(attempt[1])
        pid = payload.get("pid")
        alive = pid_alive(pid)
        start = f"ран {payload.get('run_id') or '—'} начат {_ago(attempt[1].get('ts'), now_sec)}"
        if alive is False:
            facts.setdefault("crashed", []).append(f"{start}, attempt_ended нет, {pid_text(pid, alive)}")
        elif alive is True:
            facts.setdefault("working", []).append(
                f"{start}, {pid_text(pid, alive)}; heartbeat в журнале не пишется — свежесть не проверена"
            )
        else:
            facts.setdefault("no_signal", []).append(
                f"{start}, attempt_ended нет, {pid_text(pid, alive)} — живого сигнала нет"
            )

    # 8. Гейт: от дела остался только снимок `created`, и в нём есть родители.
    #    Закрытие родителей видно в их собственных событиях, а не в этих, поэтому
    #    факт назван снимком, а не сегодняшним состоянием графа.
    if last_kind == "created":
        parents = _payload(ordered[-1]).get("parents") or []
        if parents:
            facts.setdefault("wait_gate", []).append(
                "родители на момент created: "
                + ", ".join(str(parent) for parent in parents)
                + " (закрытие в журнале по делу не видно)"
            )

    state = next((name for name in WAITING_PRIORITY if name in facts), None)
    if state is None:
        return _verdict(None, None, None, "no_live_fact", last_kind)

    human_kind = None
    if state == "wait_human":
        human_kind = "question" if question is not None else "status"
    return _verdict(True, state, _evidence(facts, state), "live_fact", last_kind, human_kind)


def _verdict(
    active: Optional[bool],
    state: Optional[str],
    evidence: Optional[str],
    reason: str,
    last_kind: Optional[str],
    human_kind: Optional[str] = None,
) -> dict[str, Any]:
    """Ответ fold одной формы: состояние, его доказательство и причина, почему его нет."""
    return {
        "active": True if active is None else bool(active),
        "state": state,
        "human_kind": human_kind,
        "evidence": evidence,
        "reason": reason,
        "last_kind": last_kind,
    }


def _evidence(facts: dict[str, list[str]], state: str) -> str:
    """Победитель первым, остальные сработавшие факты — через `; ещё:` по приоритету."""
    order = {name: i for i, name in enumerate(WAITING_PRIORITY)}
    others = [f"{name}: {'; '.join(texts)}" for name, texts in facts.items() if name != state]
    others.sort(key=lambda line: order.get(line.split(":", 1)[0], len(order)))
    winner = "; ".join(facts[state])
    return winner if not others else f"{winner}; ещё: " + "; ".join(others)


def _open_question(ordered: list[dict[str, Any]]) -> Optional[tuple[int, dict[str, Any]]]:
    """Последний вопрос без ответа: `question_answered` снимает вопрос по `question_id`."""
    open_questions: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, event in enumerate(ordered):
        kind = str(event.get("kind") or "")
        if kind not in ("question_asked", "question_answered"):
            continue
        question_id = str(_payload(event).get("question_id") or "")
        if kind == "question_asked":
            open_questions[question_id or f"#{index}"] = (index, event)
        elif question_id:
            open_questions.pop(question_id, None)
    if not open_questions:
        return None
    return max(open_questions.values(), key=lambda item: item[0])


def _open_block(ordered: list[dict[str, Any]]) -> Optional[tuple[int, dict[str, Any]]]:
    """Последний `blocked` без последующего `unblocked`."""
    found: Optional[tuple[int, dict[str, Any]]] = None
    for index, event in enumerate(ordered):
        kind = str(event.get("kind") or "")
        if kind == "blocked":
            found = (index, event)
        elif kind == "unblocked":
            found = None
    return found


def _open_attempt(ordered: list[dict[str, Any]]) -> Optional[tuple[int, dict[str, Any]]]:
    """Последний `attempt_started`, чей ран ещё не закрыт `attempt_ended`."""
    started: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, event in enumerate(ordered):
        kind = str(event.get("kind") or "")
        if kind not in ("attempt_started", "attempt_ended"):
            continue
        run_id = str(_payload(event).get("run_id") or "")
        if kind == "attempt_started":
            started[run_id or f"#{index}"] = (index, event)
        elif run_id:
            started.pop(run_id, None)
    if not started:
        return None
    return max(started.values(), key=lambda item: item[0])


def _last_of_kind(ordered: list[dict[str, Any]], kind: str) -> Optional[tuple[int, dict[str, Any]]]:
    """Индекс и событие последнего факта нужного вида."""
    for index in range(len(ordered) - 1, -1, -1):
        if str(ordered[index].get("kind") or "") == kind:
            return index, ordered[index]
    return None


def _kind_rank(kind: str) -> int:
    """Место вида события в каноническом порядке применения (`KIND_ORDER`)."""
    return KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


# ------------------------------------------------------- живость pid, тексты


def pid_alive(pid: Any) -> Optional[bool]:
    """Честная проверка живости pid: None — неизвестно, иначе ответ ядра."""
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Процесс есть, но он не наш: значит живой.
        return True
    except OSError:
        return None
    return True


def pid_text(pid: Any, alive: Optional[bool]) -> str:
    """Текст про pid ровно по тому, что известно: жив / мёртв / состояние неизвестно / не записан."""
    if not pid:
        return "pid не записан"
    if alive is True:
        return f"pid {pid} жив"
    if alive is False:
        return f"pid {pid} мёртв"
    return f"состояние pid {pid} неизвестно"


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    """`payload` события объектом: уже разобранный, JSON-строка или мусор → `{}`."""
    raw = event.get("payload")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ago(ts: Any, now: int) -> str:
    """Возраст метки времени словами: `3ч назад`; без метки — `время не записано`."""
    if ts is None:
        return "время не записано"
    seconds = max(0, int(now) - int(ts))
    if seconds < 60:
        return f"{seconds}с назад"
    if seconds < 3600:
        return f"{seconds // 60}мин назад"
    if seconds < 86400:
        return f"{seconds // 3600}ч назад"
    return f"{seconds // 86400}д назад"


def _short(text: str) -> str:
    """Причина блока одной строкой: пробелы схлопнуты, хвост обрезан."""
    collapsed = " ".join(str(text).split())
    return collapsed[:REASON_LIMIT] + ("…" if len(collapsed) > REASON_LIMIT else "")


# ---------------------------------------------------------------- ingest


def ingest(conn: sqlite3.Connection, home: Path, boards: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Дописать в журнал недостающие события по фактам источников.

    `boards` — живой список `({"slug", "path"}, ...)` от поверхности
    (`plugin_api._discover_boards`): правило «какая БД источник, а какая архив»
    живёт в одном месте, дублировать его здесь нельзя. Источники открываются
    только `connect_ro`, пишем исключительно в свой журнал.

    Один факт — одна строка источника:

    - `task_events` → `created`/`claimed`/`blocked`/`unblocked`/`completed`;
    - `task_runs` → `attempt_started`/`attempt_ended` (ран открылся/закрылся);
    - `lease_lost` события в kanban не имеет и выводится сравнением журнала с
      текущим лизингом: лизинг был, попытки за ним не было, а лизинга больше нет.

    Историю задним числом не мигрируем (`docs/JOURNAL.md`: журнал начинается с
    момента включения), поэтому в проход попадают активные дела и дела, которые
    в журнале уже есть. Повторный проход — no-op: ключи те же.
    """
    now = _now_sec()
    known = _known_deals_by_board(conn)
    report: dict[str, Any] = {
        "appended": 0,
        "boards": [],
        "errors": [],
        "warnings": [],
    }
    leases: list[dict[str, Any]] = []
    for entry in boards:
        slug = str(entry["slug"])
        try:
            events, board_leases, warnings = _board_pass(entry, known.get(slug, set()), now)
        except Exception as exc:  # noqa: BLE001 - недоступная доска не валит проход
            reason = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
            report["errors"].append({"board": slug, "reason": reason})
            report["boards"].append({"slug": slug, "path": entry.get("path"), "read": False, "deals": 0, "appended": 0})
            continue
        written = append(conn, events)
        report["warnings"].extend(warnings)
        report["boards"].append(
            {
                "slug": slug,
                "path": entry.get("path"),
                "read": True,
                "deals": len(board_leases),
                "appended": written,
            }
        )
        report["appended"] += written
        leases.extend(board_leases)

    lost, error = _lease_lost_events(conn, leases, now)
    if error:
        report["errors"].append({"board": JOURNAL_SUBDIR, "reason": error})
    written = append(conn, lost)
    report["appended"] += written
    for lost_event in lost:
        for board in report["boards"]:
            if board["slug"] == lost_event["board"]:
                board["appended"] += 1
    report.update(counts(conn))
    return report


def _known_deals_by_board(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Дела, о которых журнал уже знает, по доскам: их тоже держим в проходе."""
    known: dict[str, set[str]] = {}
    for row in conn.execute("SELECT DISTINCT board, deal_id FROM events"):
        known.setdefault(str(row["board"]), set()).add(str(row["deal_id"]))
    return known


def _board_pass(
    entry: dict[str, Any], known: set[str], now: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Один проход по доске: (новые события, факты для lease_lost, предупреждения).

    Чтение — одним read-only снимком доски. Провал вспомогательной таблицы
    (`task_events`, `task_runs`, `task_links`) не валит проход: факты этой
    таблицы просто не переносятся, а причина уходит в `warnings`.
    """
    slug = str(entry["slug"])
    path = Path(entry["path"])
    warnings: list[str] = []
    if entry.get("note") == "empty db":
        raise RuntimeError(f"empty db ({path}): 0 bytes")
    with closing(connect_ro(path)) as src:
        if not _table_exists(src, "tasks"):
            raise RuntimeError(f"{path}: no tasks table")
        active = _rows(
            src,
            "SELECT id FROM tasks WHERE status NOT IN (?, ?) ORDER BY created_at, id",
            TERMINAL_STATUSES,
        )
        wanted = {str(row["id"]) for row in active} | set(known)
        if not wanted:
            return [], [], warnings
        tasks = _by_id(
            _rows_in(src, "SELECT id, title, assignee, status, created_at, claim_lock, claim_expires FROM tasks", "id", wanted)
        )
        events = _rows_in(
            src,
            "SELECT id, task_id, run_id, kind, payload, created_at FROM task_events",
            "task_id",
            wanted,
            extra_sql="AND kind IN ({})".format(", ".join("?" * len(INGEST_EVENT_KINDS))),
            extra_params=INGEST_EVENT_KINDS,
            order_by="ORDER BY id",
            warning=("task_events", warnings),
        )
        runs = _rows_in(
            src,
            "SELECT id, task_id, status, outcome, error, started_at, ended_at, worker_pid FROM task_runs",
            "task_id",
            wanted,
            order_by="ORDER BY id",
            warning=("task_runs", warnings),
        )
        links = _rows_in(
            src,
            "SELECT child_id, parent_id FROM task_links",
            "child_id",
            wanted,
            order_by="ORDER BY child_id, parent_id",
            warning=("task_links", warnings),
        )

    parents: dict[str, list[str]] = {}
    for row in links:
        parents.setdefault(str(row["child_id"]), []).append(str(row["parent_id"]))

    out: list[dict[str, Any]] = []
    block_kind_by_deal: dict[str, Any] = {}
    created_deals: set[str] = set()
    for row in events:
        deal_id = str(row["task_id"])
        task = tasks.get(deal_id, {})
        kind = str(row["kind"])
        payload = _payload(row)
        ts = int(row["created_at"] or now)
        if kind == "created":
            created_deals.add(deal_id)
            event_payload = {
                "title": task.get("title"),
                "assignee": payload.get("assignee", task.get("assignee")),
                "status": payload.get("status", task.get("status")),
                "parents": payload.get("parents") or parents.get(deal_id, []),
            }
            # Ключ один на оба источника факта (`task_events` и строка `tasks`):
            # «дело впервые увидено» — один факт, и второй его копии быть не должно.
            out.append(_event(slug, deal_id, "created", ts, event_payload, "task", now))
        elif kind == "claimed":
            out.append(
                _event(
                    slug,
                    deal_id,
                    "claimed",
                    ts,
                    {"claim_lock": payload.get("lock"), "claim_expires": payload.get("expires"), "run_id": payload.get("run_id")},
                    row["id"],
                    now,
                )
            )
        elif kind == "blocked":
            block_kind_by_deal[deal_id] = payload.get("kind")
            out.append(
                _event(slug, deal_id, "blocked", ts, {"block_kind": payload.get("kind"), "reason": payload.get("reason")}, row["id"], now)
            )
        elif kind == "unblocked":
            out.append(_event(slug, deal_id, "unblocked", ts, {"previous_kind": block_kind_by_deal.get(deal_id)}, row["id"], now))
        elif kind == "archived":
            out.append(_event(slug, deal_id, "completed", ts, {"status": "archived"}, row["id"], now))
        elif _completed_status(payload, task) is not None:
            # `completed` в kanban — это «этап сдан»: карточка могла уйти в
            # `review`/`awaiting_merge`. Завершением дела событие считается
            # только по терминальному статусу, иначе fold закрыл бы живое дело.
            out.append(
                _event(slug, deal_id, "completed", ts, {"status": _completed_status(payload, task)}, row["id"], now)
            )

    for row in runs:
        deal_id = str(row["task_id"])
        run_id = row["id"]
        started_at = int(row["started_at"] or now)
        out.append(_event(slug, deal_id, "attempt_started", started_at, {"run_id": run_id, "pid": row["worker_pid"]}, f"run:{run_id}", now))
        if row["ended_at"] is not None:
            out.append(
                _event(
                    slug,
                    deal_id,
                    "attempt_ended",
                    int(row["ended_at"]),
                    {"run_id": run_id, "outcome": row["outcome"], "error": row["error"]},
                    f"run:{run_id}",
                    now,
                )
            )

    # `created` без kanban-события: строка `tasks` — тоже строка источника, и без
    # неё журнал не знал бы о деле вовсе. Ключ общий на доску и дело: у факта
    # «дела ещё не было» один источник — сама карточка.
    for deal_id, task in tasks.items():
        if deal_id in created_deals:
            continue
        out.append(
            _event(
                slug,
                deal_id,
                "created",
                int(task.get("created_at") or now),
                {
                    "title": task.get("title"),
                    "assignee": task.get("assignee"),
                    "status": task.get("status"),
                    "parents": parents.get(deal_id, []),
                },
                "task",
                now,
            )
        )

    leases = [
        {"board": slug, "deal_id": deal_id, "claim_lock": task.get("claim_lock")}
        for deal_id, task in tasks.items()
    ]
    return out, leases, warnings


def _completed_status(payload: dict[str, Any], task: dict[str, Any]) -> Optional[str]:
    """Терминальный статус факта `completed`, или None если факт не про завершение.

    Старые kanban-события не писали `status` в payload: тогда источником статуса
    остаётся сама карточка (`tasks.status`) — это факт источника, а не догадка.
    """
    status = payload.get("status")
    if status is not None:
        return str(status) if str(status) in TERMINAL_STATUSES else None
    current = task.get("status")
    return str(current) if str(current) in TERMINAL_STATUSES else None


def _lease_lost_events(
    conn: sqlite3.Connection, leases: list[dict[str, Any]], now: int
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Лизинги, которые исчезли без попытки: (события, причина сбоя чтения журнала).

    События для этого факта в kanban нет — он выводится сравнением: журнал
    последним помнит `claimed`, за ним не было `attempt_started`, а лизинга в
    источнике уже нет. Ключ идемпотентности — хеш кортежа `lock|expires`, как в
    контракте для фактов из колонок.
    """
    if not leases:
        return [], None
    try:
        by_deal: dict[str, list[dict[str, Any]]] = {}
        for event in read_events(conn):
            by_deal.setdefault(str(event["deal_id"]), []).append(event)
    except Exception as exc:  # noqa: BLE001 - свой журнал не прочитался
        return [], f"journal unreadable: {type(exc).__name__}: {exc}"
    out: list[dict[str, Any]] = []
    for lease in leases:
        events = by_deal.get(lease["deal_id"]) or []
        claim = _last_of_kind(events, "claimed")
        if claim is None:
            continue
        claim_index, claim_event = claim
        payload = _payload(claim_event)
        if str(payload.get("claim_lock") or "") == str(lease["claim_lock"] or ""):
            continue  # лизинг на месте
        if any(str(event.get("kind")) == "attempt_started" for event in events[claim_index + 1 :]):
            continue  # за лизингом была попытка: лизинг кончился работой, а не потерей
        if str(events[-1].get("kind") or "") == "completed":
            continue  # дело закрыто: лизинг снят завершением
        stable = hashlib.sha1(
            f"{payload.get('claim_lock')}|{payload.get('claim_expires')}".encode()
        ).hexdigest()
        out.append(
            _event(
                str(lease["board"]),
                str(lease["deal_id"]),
                "lease_lost",
                now,
                {"previous_expires": payload.get("claim_expires")},
                stable,
                now,
            )
        )
    return out, None


def _event(
    board: str, deal_id: str, kind: str, ts: int, payload: dict[str, Any], stable: Any, now: int
) -> dict[str, Any]:
    """Событие журнала по контракту: ключ идемпотентности — факт источника, не время."""
    return {
        "ts": int(ts),
        "recorded_at": int(now),
        "deal_id": str(deal_id),
        "board": str(board),
        "kind": str(kind),
        "payload": payload,
        "source": "ingest",
        "idempotency_key": f"ingest:{board}:{deal_id}:{kind}:{stable}",
    }


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Строки запроса как словари."""
    return [dict(row) for row in conn.execute(sql, params)]


def _rows_in(
    conn: sqlite3.Connection,
    select_sql: str,
    key: str,
    values: set[str],
    *,
    extra_sql: str = "",
    extra_params: tuple = (),
    order_by: str = "",
    warning: tuple[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """`SELECT ... WHERE key IN (...)` пачками: предел переменных SQLite не должен валить проход.

    Провал такого запроса не «нет фактов», поэтому причина уходит в `warnings`
    доски (`warning` = (имя таблицы, список предупреждений)), а проход едет дальше.
    """
    gathered: list[dict[str, Any]] = []
    ordered = sorted(values)
    for start in range(0, len(ordered), IN_CHUNK):
        chunk = ordered[start : start + IN_CHUNK]
        sql = f"{select_sql} WHERE {key} IN ({', '.join('?' * len(chunk))}) {extra_sql} {order_by}"
        try:
            gathered.extend(dict(row) for row in conn.execute(sql, tuple(chunk) + extra_params))
        except Exception as exc:  # noqa: BLE001 - таблицы нет/схема чужая
            if warning is not None:
                warning[1].append(f"{warning[0]}: {type(exc).__name__}: {exc}")
            return gathered
    return gathered


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Строки `tasks` по id дела."""
    return {str(row.get("id")): row for row in rows}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Есть ли таблица в открытой БД (для понятной причины вместо `no such table`)."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchall()
    return bool(rows)
