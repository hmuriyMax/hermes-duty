"""Hermes Duty — backend desktop-плагина «Дела».

P0: только чтение. Плагин открывает живые источники в режиме `?mode=ro` и
показывает, что в них есть, — он ничего не пишет ни в kanban, ни в pool, ни в
state.db, и не пересчитывает статусы карточек.

Маршруты монтируются как `/api/plugins/hermes-duty/<...>`.

Источники (поиск от корня Hermes, без хардкода путей):

- `kanban.db` в корне — живая БД доски `default` (остальные доски лежат в
  `kanban/boards/<slug>/kanban.db`);
- `messaging/pool.db` — пул вопросов (для таксономии ожиданий, тоже только чтение);
- `profiles/<role>/state.db` — сессии, из которых берётся «последнее действие».

Ядро kanban и его диспетчер не импортируются: P0 не должен зависеть от кода,
который мы собираемся заменить.

Экран «сейчас» (`/now`) и таймлайн попыток (`/duty/{board}/{task_id}/attempts`)
считаются здесь же, по тем же read-only источникам: состояние ожидания дела
выводится из набора фактов (лизинг, pid, heartbeat, граф зависимостей, пул
вопросов, исход последнего рана), а не из одной колонки `status`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.routing import APIRoute


# Один набор заголовков на все ответы: и 200, и ошибки. Кэш промежуточного
# слоя держит и `404`, поэтому «доска недоступна» из прошлого тела иначе
# переживает починку кода.
NO_STORE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate"}


class _NoStoreRoute(APIRoute):
    """Ответы плагина не кэшируются нигде.

    Снимок «сейчас» живёт секунды, а `4xx/5xx` и тела без заголовков кэширования
    промежуточные слои (LAN-прокси, кэш клиента) вправе переиспользовать — тогда
    на экране висит картина, снятая до починки, и выглядит свежей.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapper(request):
            response = await handler(request)
            response.headers.update(NO_STORE_HEADERS)
            return response

        return wrapper


router = APIRouter(route_class=_NoStoreRoute)

# Busy-timeout соединения: сколько ждать чужую запись, а не дедлайн запроса.
# Плагин читает живые БД, которые пишет ядро, поэтому ждать дольше смысла нет —
# защита от медленного скана лежит на клиенте (`REQUEST_TIMEOUT_MS` в UI).
SQL_BUSY_TIMEOUT_SEC = 2.0

# id карточки kanban — один паттерн на файл (`t_` + hex, например `t_21f41d1c`).
# Используется и для `session_key` вопроса пула, и для упоминания в его тексте.
TASK_ID_RE = re.compile(r"t_[0-9a-f]{6,}")

# Корневая БД: доска `default` лежит не в `kanban/boards/`, а прямо в корне Hermes.
DEFAULT_BOARD = "default"

# Каталог выбывших досок: ядро убирает туда закрытые доски (`_archived/<slug>/`)
# и ставит рядом камень `board.json` с `"archived": true`. Это не источник среза.
ARCHIVED_DIR = "_archived"


def _plugin_version() -> str:
    """Номер версии — один источник (plugin.yaml), а не четвёртая копия в коде."""
    meta = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        for line in meta.read_text().splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return "0.0.0"


VERSION = _plugin_version()


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _ro(path: Path) -> str:
    """URI для открытия БД строго на чтение (не создаёт файл, не берёт write-lock)."""
    return path.resolve().as_uri() + "?mode=ro"


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Read-only соединение со снимком: `BEGIN` фиксирует одно состояние БД на срез."""
    conn = sqlite3.connect(_ro(path), uri=True, timeout=SQL_BUSY_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    return conn


def _fetch(
    conn: sqlite3.Connection, sql: str, params: tuple = ()
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Один запрос на уже открытом соединении: `(строки, причина сбоя)` без исключений.

    Отдельно от `_query_checked`, потому что срез доски делает несколько запросов
    по одному соединению, а не открывает новое на каждый.
    """
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()], None
    except Exception as exc:  # noqa: BLE001 - битый файл/нет таблицы/нет прав
        return [], f"{type(exc).__name__}: {exc}"


def _query_checked(
    path: Path, sql: str, params: tuple = ()
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Один read-only запрос: `(строки, причина сбоя)`, без исключений наружу.

    Причина нужна экрану «сейчас»: доска, которая не читается, попадает в
    `errors` ответа, а не роняет роут.
    """
    try:
        with closing(_connect_ro(path)) as conn:
            return _fetch(conn, sql, params)
    except Exception as exc:  # noqa: BLE001 - файл не открылся
        return [], f"{type(exc).__name__}: {exc}"


def _query(path: Path, sql: str) -> list[dict[str, Any]]:
    """Один read-only запрос; на любой сбой возвращает пустой список, а не падает."""
    return _query_checked(path, sql)[0]


def _table_exists(path: Path, table: str) -> bool:
    rows = _query(path, "SELECT name FROM sqlite_master WHERE type='table'")
    return any(str(r.get("name")) == table for r in rows)


def _archived_reason(board_dir: Path) -> Optional[str]:
    """Почему доска выбыла, или None, если она живая.

    Ядро kanban убирает закрытые доски в `kanban/boards/_archived/` и ставит в
    каталог доски могильный камень `board.json` с `"archived": true`; открывать
    такую доску ядро отказывается (`_refuse_dead_board_resurrection`). Для среза
    это не источник, а история: читать её значит показывать архив как живое дело,
    а не читать — значит держать вечный `errors[]` на каждом ответе.
    """
    if board_dir.name == ARCHIVED_DIR or board_dir.parent.name == ARCHIVED_DIR:
        return f"{ARCHIVED_DIR}: выбывшие доски, не живой источник"
    tombstone = board_dir / "board.json"
    if not tombstone.is_file():
        return None
    try:
        raw = json.loads(tombstone.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict) and raw.get("archived"):
        return "board.json: archived"
    return None


def _board_paths(home: Path) -> list[tuple[str, Path]]:
    """Все каталоги-кандидаты в доски: корень Hermes + `kanban/boards/<slug>`."""
    candidates: list[tuple[str, Path]] = []
    root = home / "kanban.db"
    if root.is_file():
        candidates.append((DEFAULT_BOARD, root))
    boards_dir = home / "kanban" / "boards"
    if boards_dir.is_dir():
        for child in sorted(boards_dir.iterdir()):
            db = child / "kanban.db"
            if child.is_dir() and db.is_file():
                candidates.append((child.name, db))
            # Сам каталог `_archived/` — это namespace, а не доска: настоящие
            # выбывшие доски лежат на уровень глубже (`_archived/<slug>/`).
            # Без этого спуска архив был бы невидим целиком.
            if child.is_dir() and child.name == ARCHIVED_DIR:
                for retired in sorted(child.iterdir()):
                    retired_db = retired / "kanban.db"
                    if retired.is_dir() and retired_db.is_file():
                        candidates.append((retired.name, retired_db))
    return candidates


def _discover_archived(home: Path) -> list[dict[str, Any]]:
    """Выбывшие доски: видны в `/sources`, но не читаются в срез.

    Молча выкинуть архив нельзя — тогда «доски не видно» станет неотличимо от
    «доски нет». Поэтому он перечислен отдельным списком с причиной.
    """
    found: list[dict[str, Any]] = []
    for slug, path in _board_paths(home):
        if slug == DEFAULT_BOARD:
            continue
        reason = _archived_reason(path.parent)
        if reason is None:
            continue
        found.append({"slug": slug, "path": str(path), "reason": reason})
    return found


def _discover_boards(home: Path) -> list[dict[str, Any]]:
    """Все живые kanban-БД: корневая (доска default) + boards/<slug>/kanban.db.

    0-байтная БД не выпадает из перечня: она помечается `note = "empty db"` и
    видна как доска, которую не прочитали. Иначе такой файл молча исчезает и из
    `boards[]`, и из `errors[]`, и срез выглядит полным, хотя доска потеряна.

    Выбывшие доски (`_archived/`, камень `board.json: archived`) сюда не входят:
    они не источник среза, их место — `/sources.archived`.
    """
    found: list[dict[str, Any]] = []
    for slug, path in _board_paths(home):
        if slug != DEFAULT_BOARD and _archived_reason(path.parent) is not None:
            continue
        entry: dict[str, Any] = {"slug": slug, "path": str(path)}
        if path.stat().st_size == 0:
            entry["note"] = "empty db"
        found.append(entry)
    return found


def _board_summary(entry: dict[str, Any]) -> dict[str, Any]:
    path = Path(entry["path"])
    out: dict[str, Any] = {
        "slug": entry["slug"],
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "mtime": int(path.stat().st_mtime),
        "readable": False,
        "tasks": 0,
        "blocked": 0,
        "running": 0,
    }
    if entry.get("note"):
        out["note"] = entry["note"]
        return out
    if not _table_exists(path, "tasks"):
        # Пустая/служебная БД — не ошибка, честно помечаем.
        out["note"] = "no tasks table"
        return out
    rows = _query(
        path,
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked, "
        "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running FROM tasks",
    )
    if rows:
        out["readable"] = True
        out["tasks"] = int(rows[0].get("n") or 0)
        out["blocked"] = int(rows[0].get("blocked") or 0)
        out["running"] = int(rows[0].get("running") or 0)
    return out


def _pool_summary(home: Path) -> Optional[dict[str, Any]]:
    db = home / "messaging" / "pool.db"
    if not db.is_file():
        return None
    rows = _query(db, "SELECT origin, status, COUNT(*) AS n FROM questions GROUP BY origin, status")
    return {
        "path": str(db),
        "by_origin_status": rows,
        "total": sum(int(r.get("n") or 0) for r in rows),
    }


@router.get("/health")
def health() -> dict[str, Any]:
    """Живая проверка: плагин смонтирован и источники читаются на чтение."""
    home = _hermes_home()
    boards = _discover_boards(home)
    readable = [b for b in boards if _table_exists(Path(b["path"]), "tasks")]
    pool = _pool_summary(home)
    return {
        "ok": True,
        "version": VERSION,
        "hermes_home": str(home),
        "kanban": {
            "databases": len(boards),
            "readable": len(readable),
            "task_rows": sum(
                int((_query(Path(b["path"]), "SELECT COUNT(*) AS n FROM tasks") or [{"n": 0}])[0].get("n") or 0)
                for b in readable
            ),
        },
        "pool": {"present": pool is not None, "total": (pool or {}).get("total", 0)},
        "mode": "read-only",
    }


@router.get("/sources")
def sources() -> dict[str, Any]:
    """Что плагин видит: доски, пул, профили. Ничего не меняет."""
    home = _hermes_home()
    profiles_dir = home / "profiles"
    profiles = sorted(
        p.name for p in profiles_dir.iterdir() if p.is_dir() and (p / "state.db").is_file()
    ) if profiles_dir.is_dir() else []
    return {
        "version": VERSION,
        "hermes_home": str(home),
        "boards": [_board_summary(b) for b in _discover_boards(home)],
        "archived": _discover_archived(home),
        "pool": _pool_summary(home),
        "profiles": profiles,
    }


@router.get("/board/{slug}/tasks")
def board_tasks(slug: str) -> dict[str, Any]:
    """Сырой срез карточек доски для будущего экрана «сейчас» (read-only)."""
    home = _hermes_home()
    match = [b for b in _discover_boards(home) if b["slug"] == slug]
    if not match:
        raise HTTPException(status_code=404, headers=NO_STORE_HEADERS, detail=f"board '{slug}' not found")
    path = Path(match[0]["path"])
    rows = _query(
        path,
        "SELECT id, title, assignee, status, block_kind, consecutive_failures, "
        "worker_pid, current_run_id, created_at, started_at, completed_at "
        "FROM tasks WHERE status NOT IN ('done','archived') ORDER BY created_at DESC LIMIT 200",
    )
    return {"slug": slug, "path": str(path), "count": len(rows), "tasks": rows}


# ------------------------------------------------------------- «сейчас» (P0b)
#
# Таксономия ожиданий: семь взаимоисключающих состояний ожидания плюс
# `working` и `no_signal`. Состояние вычисляется по набору фактов (лизинг, pid,
# heartbeat, граф зависимостей, пул вопросов, исход последнего рана), и каждый
# сработавший факт попадает в `waiting.evidence` текстом — из одной колонки
# ничего не выводится.
#
# Порядок приоритета при совпадении фактов — ровно этот кортеж, первое
# совпадение побеждает:
#
#     wait_human > capability > dead_lease > crashed > working > no_signal
#       > wait_slot > wait_gate
#
# `no_signal` — дело со `status=running`, у которого нет ни рана, ни pid, ни
# heartbeat: живого сигнала нет, поэтому оно не выдаётся за работающее.
#
# Он же задаёт порядок сортировки ответа `/now`. Статус, для которого не
# сработал ни один факт, доразбирается оглавлением `WAITING_FALLBACK` (кто
# должен действовать), чтобы у активного дела состояние было всегда.

# Терминальные статусы: такие карточки в `/now` не попадают — это не «дела сейчас».
TERMINAL_STATUSES = ("done", "archived")

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

# Ран считается живым, только если heartbeat не старше этого порога.
HEARTBEAT_FRESH_SEC = 300

# Outcome'ы рана, означающие падение попытки, а не работу. Единственный источник
# этого набора: он уходит клиенту в `crash_outcomes`, чтобы UI не держал копию.
CRASH_OUTCOMES = ("crashed", "gave_up", "spawn_failed", "timed_out")

# Статусы дела, в которых оно ждёт незавершённого родителя в графе зависимостей.
GATE_STATUSES = ("todo", "ready")

# Слова, по которым причина блока читается как «нужен доступ или права» —
# нужны только если причина блока пришла без typed-колонки `block_kind`.
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

# Остаточная ветка: статус, для которого ни один факт не сработал. Ключ — что
# записано в `tasks.status`, значение — состояние и объяснение, кто должен
# действовать. `running` в оглавлении есть, но ведёт в `no_signal`: живого
# сигнала у такого дела нет, и «работает» про него сказать нельзя.
WAITING_FALLBACK = {
    "triage": ("wait_human", "status=triage: ждёт разбора (спецификатор или человек)"),
    "scheduled": ("wait_gate", "status=scheduled: ждёт времени пробуждения"),
    "todo": ("wait_slot", "status=todo без открытых родителей: очередь диспетчера, слотов нет"),
    "ready": ("wait_slot", "status=ready без открытых родителей: очередь диспетчера, слотов нет"),
    "blocked": ("wait_human", "status=blocked: нужен человек"),
    "review": ("wait_human", "status=review: ждёт вердикта ревьюера"),
    "awaiting_merge": ("wait_human", "status=awaiting_merge: ждёт мерджа координатором"),
    "running": ("no_signal", "status=running без живого сигнала: рана, pid и heartbeat нет"),
}

# Сколько активных дел берём с доски за один срез и сколько живых вопросов пула.
# Оба лимита не молчат: сработавший лимит попадает в `truncated`/`errors`.
DEAL_LIMIT = 500
QUESTION_LIMIT = 200

# Длина текстовой выжимки payload'а события в ответе.
SUMMARY_LIMIT = 240


def _now_sec() -> int:
    """Текущее время в unix-секундах — как все `*_at` в источниках."""
    return int(time.time())


def _age_sec(at: Optional[int], now: int) -> Optional[int]:
    """Возраст метки времени в секундах; None, если метки нет."""
    if at is None:
        return None
    return max(0, int(now) - int(at))


def _pid_alive(pid: Optional[int]) -> Optional[bool]:
    """Честная проверка живости pid: None — pid неизвестен, иначе ответ ядра."""
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


def _pid_text(pid: Optional[int], pid_alive: Optional[bool]) -> str:
    """Текст про pid ровно по тому, что известно: жив / состояние неизвестно / не записан.

    Одна формулировка на все ветки: иначе копии в `crashed` и `working` разъезжаются
    и одна из них называет «живым» то, чего не проверяла. `pid_alive=False` сюда не
    доходит: обе ветки отсекают мёртвый pid раньше — это отдельный факт `dead_lease`.
    """
    if not pid:
        return "pid не записан"
    if pid_alive is True:
        return f"pid {pid} жив"
    return f"состояние pid {pid} неизвестно"


def _payload_summary(payload: Optional[str]) -> Optional[str]:
    """Короткая выжимка payload'а события: сначала известные ключи, потом JSON целиком."""
    if not payload:
        return None
    text = payload
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("reason", "summary", "note", "message", "status", "kind", "last_output"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        else:
            text = json.dumps(data, ensure_ascii=False)
    text = " ".join(text.split())
    if not text:
        return None
    return text[:SUMMARY_LIMIT] + ("…" if len(text) > SUMMARY_LIMIT else "")


def _pool_questions(home: Path) -> tuple[dict[str, dict[str, Any]], Optional[str], bool]:
    """Живые вопросы пула (`status='sent'`), разложенные по id дела.

    Схема пула `card_id` не знает: у вопроса с `origin='card'` id карточки лежит
    в `session_key` (проверено на живых данных — там ровно `t_xxxxxxxx`), а у
    остальных связь видна только по упоминанию `t_<id>` в тексте. Пути полей
    поэтому такие: `session_key` → затем `text`/`text_full`.

    Второй элемент — причина сбоя: недоступный пул это не «вопросов нет», иначе
    `wait_human` молча не срабатывает и ответ врёт про «никто не ждёт». Третий —
    признак того, что живых вопросов больше лимита: обрезанный пул тоже нельзя
    выдавать за полный. Проверяется лишней строкой (`LIMIT + 1`), а не
    равенством длины лимиту, — иначе полный пул ровно на `QUESTION_LIMIT`
    вопросов ложной пометки не избежит.
    """
    db = home / "messaging" / "pool.db"
    if not db.is_file():
        return {}, f"pool.db not found: {db}", False
    rows, err = _query_checked(
        db,
        "SELECT * FROM questions WHERE status = 'sent' ORDER BY id DESC LIMIT ?",
        (QUESTION_LIMIT + 1,),
    )
    if err:
        return {}, f"pool.db unreadable: {db} ({err})", False
    truncated = len(rows) > QUESTION_LIMIT
    rows = rows[:QUESTION_LIMIT]
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        holders: set[str] = set()
        key = str(row.get("session_key") or "")
        if TASK_ID_RE.fullmatch(key):
            holders.add(key)
        blob = f"{row.get('text') or ''} {row.get('text_full') or ''}"
        holders.update(TASK_ID_RE.findall(blob))
        for task_id in holders:
            index.setdefault(task_id, row)
    return index, None, truncated


def _lease(task: dict[str, Any], run: Optional[dict[str, Any]], now: int) -> dict[str, Any]:
    """Лизинг дела: `claim_lock`/`claim_expires` карточки, а если их нет — текущего рана."""
    lock = task.get("claim_lock")
    expires = task.get("claim_expires")
    if expires is None and run is not None:
        lock = lock or run.get("claim_lock")
        expires = run.get("claim_expires")
    if expires is None:
        state = "none" if not lock else "unknown"
        return {"claim_lock": lock, "expires_at": None, "expires_in_sec": None, "state": state}
    state = "expired" if int(expires) <= now else "live"
    return {
        "claim_lock": lock,
        "expires_at": int(expires),
        "expires_in_sec": int(expires) - now,
        "state": state,
    }


def _attempt(task: dict[str, Any], run: Optional[dict[str, Any]], now: int) -> Optional[dict[str, Any]]:
    """Текущая попытка дела: живой ран (или ран, на который указывает `current_run_id`).

    Пид, старт и heartbeat берутся из записи рана, а карточка — только запасной
    источник для старых БД, где `task_runs` скудный. Добор из карточки виден в
    поле `source`: `run`, если все три факта из рана, иначе `task` — тогда часть
    фактов попытки на самом деле карточные, и это не скрыто.
    """
    if run is None:
        return None
    pid = run.get("worker_pid")
    start = run.get("started_at")
    heartbeat = run.get("last_heartbeat_at")
    source = "run"
    if pid is None:
        pid = task.get("worker_pid")
        source = "task"
    if start is None:
        start = task.get("started_at")
        source = "task"
    if heartbeat is None:
        heartbeat = task.get("last_heartbeat_at")
        source = "task"
    return {
        "run_id": run.get("id"),
        "source": source,
        "start": None if start is None else int(start),
        "age_sec": _age_sec(start, now),
        "pid": None if not pid else int(pid),
        "pid_alive": _pid_alive(pid),
        "heartbeat_age_sec": _age_sec(heartbeat, now),
        "run_status": run.get("status"),
    }


def _waiting(
    task: dict[str, Any],
    attempt: Optional[dict[str, Any]],
    lease: dict[str, Any],
    last_run: Optional[dict[str, Any]],
    parents: list[tuple[str, str]],
    slot_holder: Optional[str],
    question: Optional[dict[str, Any]],
    block_reason: str,
    pool_note: Optional[str],
) -> dict[str, str]:
    """Состояние ожидания дела по набору фактов: `{"state", "evidence"}`.

    Каждое правило, которое сработало, кладёт свой факт в `facts`; состояние —
    первое по `WAITING_PRIORITY`, а все сработавшие факты уходят в `evidence`.
    """
    status = str(task.get("status") or "")
    facts: dict[str, str] = {}
    # Факты одного рана: статус, pid и heartbeat берутся из одной попытки
    # (`attempt`), иначе старый `running` склеивается со стартовавшим новым раном
    # и `dead_lease` не срабатывает. `last_run` нужен только для исхода последнего
    # рана — и в тексте факта он назван явно.
    attempt = attempt or {}
    run_status = attempt.get("run_status")
    run_id = attempt.get("run_id")
    pid_alive = attempt.get("pid_alive")
    heartbeat_age = attempt.get("heartbeat_age_sec")
    pid = attempt.get("pid")
    last_status = (last_run or {}).get("status")

    # 1. Живой вопрос человеку в пуле.
    if question is not None:
        facts["wait_human"] = (
            f"в пуле вопрос #{question.get('id')} status='{question.get('status')}' "
            f"origin='{question.get('origin')}'"
        )

    # 2. Блок, который просит доступ или права.
    block_kind = str(task.get("block_kind") or "")
    if status == "blocked" and (
        block_kind == "capability" or any(word in block_reason.lower() for word in CAPABILITY_WORDS)
    ):
        facts["capability"] = (
            f"status=blocked, block_kind={block_kind or '—'}: {(block_reason or 'причина не записана')[:160]}"
        )

    # 3. Протухший лизинг. Мёртвый pid при уже завершённом ране — не dead_lease:
    #    это либо crashed, либо нормальное завершение (проверяется ниже).
    hung_run = run_status == "running" and pid_alive is False
    last_running = last_run is None or last_status == "running"
    expired_claim = lease["state"] == "expired" and last_running
    if hung_run:
        facts["dead_lease"] = (
            f"ран {run_id} в статусе running, а pid {pid} мёртв "
            f"(лизинг {lease['state']})"
        )
    elif expired_claim:
        facts["dead_lease"] = (
            f"лизинг истёк {-lease['expires_in_sec']}с назад ({lease['claim_lock'] or '—'}), "
            f"ран {last_status or '—'}"
        )

    # 4. Падение: счётчик отказов, кривой исход последнего рана или зависший ран
    #    (pid жив, а heartbeat молчит дольше порога — диспетчер такое снимает).
    crash_bits: list[str] = []
    failures = int(task.get("consecutive_failures") or 0)
    if failures > 0:
        crash_bits.append(f"consecutive_failures={failures}")
    last_outcome = (last_run or {}).get("outcome")
    if last_outcome in CRASH_OUTCOMES:
        crash_bits.append(f"последний ран {(last_run or {}).get('id')}: outcome={last_outcome}")
    stale_heartbeat = (heartbeat_age or 0) > HEARTBEAT_FRESH_SEC
    if run_status == "running" and pid_alive is not False and stale_heartbeat:
        crash_bits.append(
            f"ран {run_id} running, {_pid_text(pid, pid_alive)}, heartbeat {heartbeat_age}с назад"
        )
    if crash_bits:
        facts["crashed"] = "; ".join(crash_bits)

    # 5. Живая работа: ран идёт, pid не мёртв, heartbeat свежий. Факт про pid
    #    называет ровно то, что известно: жив / состояние неизвестно (`os.kill`
    #    вернул прочий OSError) / не записан — формулировка одна на обе ветки.
    fresh_heartbeat = heartbeat_age is not None and heartbeat_age <= HEARTBEAT_FRESH_SEC
    if run_status == "running" and pid_alive is not False and fresh_heartbeat:
        facts["working"] = (
            f"ран {run_id} running, {_pid_text(pid, pid_alive)}, heartbeat {heartbeat_age}с назад"
        )

    # 6. Очередь на занятый профиль.
    if status == "ready" and slot_holder:
        facts["wait_slot"] = (
            f"status=ready, профиль {task.get('assignee') or '—'} занят делом {slot_holder} (running)"
        )

    # 7. Гейт по незавершённому родителю.
    if status in GATE_STATUSES and parents:
        gate_list = ", ".join(f"{pid_} ({st})" for pid_, st in parents)
        facts["wait_gate"] = f"незавершённые родители: {gate_list}"

    state = next((name for name in WAITING_PRIORITY if name in facts), None)
    if state is None:
        state, reason = WAITING_FALLBACK.get(status, ("wait_slot", f"status={status or '—'}: нет живых фактов"))
        facts[state] = reason

    winner = facts[state]
    others = [f"{name}: {text}" for name, text in facts.items() if name != state]
    order = {name: i for i, name in enumerate(WAITING_PRIORITY)}
    others.sort(key=lambda line: order.get(line.split(":", 1)[0], len(order)))
    evidence = winner if not others else f"{winner}; ещё: " + "; ".join(others)
    if pool_note and state == "wait_human" and question is None:
        evidence = f"{evidence}; {pool_note}"
    return {"state": state, "evidence": evidence}


def _board_deals(
    entry: dict[str, Any], now: int, questions: dict[str, dict[str, Any]], pool_note: Optional[str]
) -> dict[str, Any]:
    """Активные дела одной доски с лизингом, попыткой, последним действием и ожиданием.

    Провал чтения самой `tasks` роняет этот вызов: доска тогда попадает в
    `errors` ответа (исключение не перехватывается здесь — его тип и текст важны
    для причины). Провал вспомогательных таблиц (`task_runs`, `task_events`,
    `task_links`) деградирует факты и уходит в `warnings` доски, а не в ошибку.
    """
    slug = str(entry["slug"])
    path = Path(entry["path"])
    if entry.get("note") == "empty db":
        # 0-байтный файл sqlite откроет как пустую БД и скажет «no such table»;
        # настоящую причину называем сами, чтобы она была видна в `errors`.
        raise RuntimeError(f"empty db ({path}): 0 bytes")
    warnings: list[str] = []
    # Одно соединение на доску: все запросы среза идут по одному снимку (`BEGIN`),
    # а не открывают новое соединение на каждый запрос.
    with closing(_connect_ro(path)) as conn:
        tasks, err = _fetch(
            conn,
            "SELECT * FROM tasks WHERE status NOT IN (?, ?) ORDER BY created_at, id LIMIT ?",
            (TERMINAL_STATUSES[0], TERMINAL_STATUSES[1], DEAL_LIMIT),
        )
        if err:
            raise RuntimeError(err)
        deal_ids = [str(row.get("id")) for row in tasks]
        marks = ", ".join("?" * len(deal_ids))

        runs_by_task: dict[str, list[dict[str, Any]]] = {}
        parents_of: dict[str, list[tuple[str, str]]] = {}
        suspenders: dict[str, dict[str, Any]] = {}
        block_reasons: dict[str, str] = {}
        if deal_ids:
            rows, err = _fetch(
                conn,
                f"SELECT * FROM task_runs WHERE task_id IN ({marks}) ORDER BY started_at, id",
                tuple(deal_ids),
            )
            if err:
                warnings.append(f"task_runs: {err}")
            for row in rows:
                runs_by_task.setdefault(str(row.get("task_id")), []).append(row)

            # Незавершённый родитель — ровно то, чем гейтит ядро: родитель не в
            # терминальном статусе (Edge-таблица `task_links`, колонки parent/child).
            # Связи ограничены делами среза: без `child_id IN (...)` join тянет всю доску.
            rows, err = _fetch(
                conn,
                f"SELECT l.child_id, p.id AS parent_id, p.status AS parent_status "
                f"FROM task_links l JOIN tasks p ON p.id = l.parent_id "
                f"WHERE l.child_id IN ({marks}) AND p.status NOT IN (?, ?) "
                f"ORDER BY l.child_id, p.id",
                tuple(deal_ids) + TERMINAL_STATUSES,
            )
            if err:
                warnings.append(f"task_links: {err}")
            for row in rows:
                parents_of.setdefault(str(row.get("child_id")), []).append(
                    (str(row.get("parent_id")), str(row.get("parent_status")))
                )

            # Последнее действие: самое свежее событие, а heartbeat — только если
            # других событий у дела нет (иначе экран показывает шум).
            latest_action_sql = (
                f"SELECT * FROM task_events WHERE id IN (SELECT MAX(id) FROM task_events "
                f"WHERE task_id IN ({marks}) AND kind != 'heartbeat' GROUP BY task_id)"
            )
            latest_any_sql = (
                f"SELECT * FROM task_events WHERE id IN (SELECT MAX(id) FROM task_events "
                f"WHERE task_id IN ({marks}) GROUP BY task_id)"
            )
            for sql in (latest_action_sql, latest_any_sql):
                rows, err = _fetch(conn, sql, tuple(deal_ids))
                if err:
                    warnings.append(f"task_events: {err}")
                    break
                for row in rows:
                    suspenders.setdefault(str(row.get("task_id")), row)

            rows, err = _fetch(
                conn,
                f"SELECT * FROM task_events WHERE id IN (SELECT MAX(id) FROM task_events "
                f"WHERE task_id IN ({marks}) AND kind = 'blocked' GROUP BY task_id)",
                tuple(deal_ids),
            )
            if err:
                warnings.append(f"task_events(blocked): {err}")
            for row in rows:
                reason = _payload_summary(row.get("payload")) or ""
                block_reasons[str(row.get("task_id"))] = reason

    slots: dict[str, str] = {}
    for row in tasks:
        if str(row.get("status")) == "running":
            slots.setdefault(str(row.get("assignee") or ""), str(row.get("id")))

    deals: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("id"))
        runs = runs_by_task.get(task_id, [])
        current_run_id = task.get("current_run_id")
        run = next((r for r in reversed(runs) if r.get("status") == "running"), None)
        if run is None and current_run_id is not None:
            run = next((r for r in runs if r.get("id") == current_run_id), None)
        last_run = runs[-1] if runs else None
        attempt = _attempt(task, run, now)
        lease = _lease(task, run, now)
        event = suspenders.get(task_id)
        assignee = task.get("assignee")
        slot_holder = slots.get(str(assignee or ""))
        waiting = _waiting(
            task,
            attempt,
            lease,
            last_run,
            parents_of.get(task_id, []),
            None if slot_holder == task_id else slot_holder,
            questions.get(task_id),
            block_reasons.get(task_id, ""),
            pool_note,
        )
        deals.append(
            {
                "board": slug,
                "id": task_id,
                "title": task.get("title"),
                "assignee": assignee,
                "status": task.get("status"),
                "priority": task.get("priority"),
                "consecutive_failures": int(task.get("consecutive_failures") or 0),
                "created_at": task.get("created_at"),
                "workspace_kind": task.get("workspace_kind"),
                "attempt": attempt,
                "lease": lease,
                "last_action": {
                    "kind": None if event is None else event.get("kind"),
                    "at": None if event is None else event.get("created_at"),
                    "age_sec": None if event is None else _age_sec(event.get("created_at"), now),
                    "summary": None if event is None else _payload_summary(event.get("payload")),
                },
                "last_run": None
                if last_run is None
                else {
                    "run_id": last_run.get("id"),
                    "status": last_run.get("status"),
                    "outcome": last_run.get("outcome"),
                    "started_at": last_run.get("started_at"),
                    "ended_at": last_run.get("ended_at"),
                },
                "waiting": waiting,
                "_age_sec": (attempt or {}).get("age_sec")
                if attempt is not None
                else _age_sec(task.get("created_at"), now),
            }
        )

    return {
        "slug": slug,
        "path": str(path),
        "deals": deals,
        "warnings": warnings,
        "truncated": len(tasks) >= DEAL_LIMIT,
    }


def collect_now(
    home: Path, now: int, board: Optional[str] = None, state: Optional[str] = None
) -> dict[str, Any]:
    """Срез «сейчас» по всем доскам: активные дела, отсортированные по таксономии.

    Сортировка детерминирована: `waiting.state` по `WAITING_PRIORITY`, затем
    возраст текущей попытки по убыванию, затем `id` — иначе UI дёргается между
    опросами. Доска, у которой не читается `tasks`, пропускается и попадает в
    `errors`; ответ при этом остаётся 200.
    """
    scope = [b for b in _discover_boards(home) if board is None or b["slug"] == board]
    questions, pool_err, pool_truncated = _pool_questions(home)
    errors: list[dict[str, str]] = []
    # Заметки о неполноте пула: недоступен или обрезан лимитом. Обе дописываются
    # в evidence дел, которым вопрос не нашёлся, — иначе `wait_human` выглядит
    # проверенным, хотя пул мог не отдать нужный вопрос.
    notes: list[str] = []
    if pool_err:
        notes.append(f"пул вопросов недоступен ({pool_err})")
        errors.append({"board": "messaging/pool.db", "reason": pool_err})
    if pool_truncated:
        reason = f"questions truncated: more than {QUESTION_LIMIT} live questions"
        notes.append(f"живых вопросов в пуле больше {QUESTION_LIMIT} (старые не вошли)")
        errors.append({"board": "messaging/pool.db", "reason": reason})
    pool_note = f"{'; '.join(notes)} — факт wait_human не проверен" if notes else None

    boards: list[dict[str, Any]] = []
    deals: list[dict[str, Any]] = []
    for entry in scope:
        try:
            collected = _board_deals(entry, now, questions, pool_note)
        except Exception as exc:  # noqa: BLE001 - любая ошибка доски не валит срез
            # Тип исключения важен: `ValueError` из чужой схемы (`int('soon')` по
            # `claim_expires`) иначе не отличить от сбоя БД. RuntimeError несёт
            # готовую причину, у остальных префиксуем тип.
            reason = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
            errors.append({"board": str(entry["slug"]), "reason": reason})
            unread: dict[str, Any] = {
                "slug": entry["slug"],
                "path": str(entry["path"]),
                "read": False,
                "deals": 0,
            }
            if entry.get("note"):
                unread["note"] = entry["note"]
            boards.append(unread)
            continue
        boards.append(
            {
                "slug": collected["slug"],
                "path": collected["path"],
                "read": True,
                "deals": len(collected["deals"]),
                "truncated": collected["truncated"],
                "warnings": collected["warnings"],
            }
        )
        deals.extend(collected["deals"])

    order = {name: i for i, name in enumerate(WAITING_PRIORITY)}
    deals.sort(
        key=lambda deal: (
            order.get(deal["waiting"]["state"], len(order)),
            -(deal["_age_sec"] or 0),
            deal["id"],
        )
    )
    by_state = {name: 0 for name in WAITING_PRIORITY}
    for deal in deals:
        by_state[deal["waiting"]["state"]] = by_state.get(deal["waiting"]["state"], 0) + 1
    if state is not None:
        deals = [deal for deal in deals if deal["waiting"]["state"] == state]
    for deal in deals:
        deal.pop("_age_sec", None)
    return {
        "now": now,
        "count": len(deals),
        "deals": deals,
        "by_state": by_state,
        "crash_outcomes": list(CRASH_OUTCOMES),
        "boards": boards,
        "errors": errors,
    }


def _board_entry(home: Path, slug: str) -> dict[str, Any]:
    """Запись доски по слагу или HTTP 404."""
    match = [b for b in _discover_boards(home) if b["slug"] == slug]
    if not match:
        raise HTTPException(status_code=404, headers=NO_STORE_HEADERS, detail=f"board '{slug}' not found")
    return match[0]


@router.get("/now")
def now(board: Optional[str] = None, state: Optional[str] = None) -> dict[str, Any]:
    """Активные дела всех досок: лизинг, попытка, последнее действие, ожидание.

    `?board=<slug>` — только одна доска (404, если её нет);
    `?state=<waiting.state>` — только дела в этом состоянии ожидания.
    """
    if state is not None and state not in WAITING_PRIORITY:
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=f"unknown state '{state}', expected one of {list(WAITING_PRIORITY)}",
        )
    home = _hermes_home()
    if board is not None:
        _board_entry(home, board)
    return collect_now(home, _now_sec(), board=board, state=state)


@router.get("/duty/{board}/{task_id}/attempts")
def duty_attempts(board: str, task_id: str) -> dict[str, Any]:
    """Таймлайн попыток дела: все раны по возрастанию старта плюс их события."""
    home = _hermes_home()
    path = Path(_board_entry(home, board)["path"])
    tasks, err = _query_checked(path, "SELECT id, title FROM tasks WHERE id = ?", (task_id,))
    if err:
        raise HTTPException(status_code=503, headers=NO_STORE_HEADERS, detail=f"board '{board}' unreadable: {err}")
    if not tasks:
        raise HTTPException(status_code=404, headers=NO_STORE_HEADERS, detail=f"task '{task_id}' not found on board '{board}'")

    now = _now_sec()
    runs, err = _query_checked(
        path, "SELECT * FROM task_runs WHERE task_id = ? ORDER BY started_at, id", (task_id,)
    )
    if err:
        raise HTTPException(status_code=503, headers=NO_STORE_HEADERS, detail=f"task_runs unreadable: {err}")
    events_by_run: dict[int, list[dict[str, Any]]] = {}
    rows, err = _query_checked(
        path,
        "SELECT run_id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND run_id IS NOT NULL ORDER BY created_at, id",
        (task_id,),
    )
    if err:
        rows = []
    for row in rows:
        events_by_run.setdefault(int(row.get("run_id") or 0), []).append(
            {
                "kind": row.get("kind"),
                "at": row.get("created_at"),
                "summary": _payload_summary(row.get("payload")),
            }
        )

    attempts: list[dict[str, Any]] = []
    for run in runs:
        start = run.get("started_at")
        end = run.get("ended_at")
        duration = None
        if start is not None:
            duration = int(end if end is not None else now) - int(start)
        attempts.append(
            {
                "run_id": run.get("id"),
                "profile": run.get("profile"),
                "status": run.get("status"),
                "start": start,
                "end": end,
                "duration_sec": duration,
                "outcome": run.get("outcome"),
                "error": run.get("error"),
                "summary": run.get("summary"),
                "events": events_by_run.get(int(run.get("id") or 0), []),
            }
        )
    return {
        "board": board,
        "task_id": task_id,
        "title": tasks[0].get("title"),
        "count": len(attempts),
        "crash_outcomes": list(CRASH_OUTCOMES),
        "attempts": attempts,
    }
