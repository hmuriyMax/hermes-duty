"""Hermes Duty — backend desktop-плагина «Дела».

С 0.0.3 состояние дела считается не по колонкам kanban, а по **своему журналу
событий** (`dashboard/journal.py`, контракт `docs/JOURNAL.md`):
`waiting = fold(events)`. Журнал живёт в `$HERMES_HOME/duty/duty.db` и принадлежит
плагину; чужие БД (`kanban.db`, `state.db`, `pool.db`) по-прежнему открываются
только `?mode=ro`, и срез в них ничего не пишет.

Единственное место, где плагин пишет в свой журнал, — проход ingest, и только по
явному требованию: `POST /ingest` или `GET /now?ingest=1`. Проход ingest читает
kanban на чтение, сравнивает с журналом и дописывает недостающие события;
повторный проход — no-op. Без этого флага ни один читающий роут не создаёт и не
меняет файлов.

Вопросы человеку идут одним каналом — `dashboard/ask.py`: `POST /ask/{board}/{task_id}`
пишет вопрос в пул `messaging/pool.db` (`origin='duty'`, `kind='decision'`) и факт
`question_asked` в журнал. Остальной код пул только читает (`mode=ro`).

Маршруты монтируются как `/api/plugins/hermes-duty/<...>`.

Источники (поиск от корня Hermes, без хардкода путей):

- `kanban.db` в корне — живая БД доски `default` (остальные доски лежат в
  `kanban/boards/<slug>/kanban.db`);
- `messaging/pool.db` — пул вопросов (для таксономии ожиданий, тоже только чтение);
- `profiles/<role>/state.db` — сессии, из которых берётся «последнее действие»;
- `duty/duty.db` — свой журнал (читается всеми, пишется только ingest).

Ядро kanban и его диспетчер не импортируются: P0 не должен зависеть от кода,
который мы собираемся заменить.

Экран «сейчас» (`/now`) и таймлайн попыток (`/duty/{board}/{task_id}/attempts`)
считаются здесь же: состояние ожидания дела выводится fold'ом по событиям
журнала, а дело, которого в журнале ещё нет, — по набору фактов kanban (лизинг,
pid, heartbeat, граф зависимостей, пул вопросов, исход последнего рана) с честной
пометкой в `waiting.evidence`, откуда состояние взято.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

# `dashboard/journal.py` рядом с этим файлом. Плагин грузит `plugin_api.py` как
# отдельный модуль (`spec_from_file_location`), пакета у него нет, поэтому
# относительный импорт невозможен: соседний файл берём по пути и кладём в
# `sys.modules` под стабильным именем — второй загрузки в процессе не будет.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
# `dashboard/ask.py` — единственный канал вопросов: пишет в пул и в журнал.
ASK_MODULE_NAME = "hermes_duty_ask"
# `dashboard/deals.py` — свой store дел и мутации (`docs/OWNERSHIP.md`).
DEALS_MODULE_NAME = "hermes_duty_deals"
# `dashboard/stagefeed.py` — лента этапов и попыток дела (`GET /deal`, `GET /now`).
STAGEFEED_MODULE_NAME = "hermes_duty_stagefeed"


def _load_journal() -> Any:
    """Загрузить `dashboard/journal.py` (журнал событий и fold по контракту)."""
    loaded = sys.modules.get(JOURNAL_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "journal.py"
    spec = importlib.util.spec_from_file_location(JOURNAL_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"journal module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[JOURNAL_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


journal = _load_journal()


def _load_ask() -> Any:
    """Загрузить `dashboard/ask.py` — единственный канал вопросов (`ask()`)."""
    loaded = sys.modules.get(ASK_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "ask.py"
    spec = importlib.util.spec_from_file_location(ASK_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"ask module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[ASK_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


ask = _load_ask()


def _load_deals() -> Any:
    """Загрузить `dashboard/deals.py` — свой store дел и мутации по контракту."""
    loaded = sys.modules.get(DEALS_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "deals.py"
    spec = importlib.util.spec_from_file_location(DEALS_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"deals module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[DEALS_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


deals = _load_deals()


def _load_stagefeed() -> Any:
    """Загрузить `dashboard/stagefeed.py` — лента этапов и попыток дела."""
    loaded = sys.modules.get(STAGEFEED_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "stagefeed.py"
    spec = importlib.util.spec_from_file_location(STAGEFEED_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"stagefeed module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[STAGEFEED_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


stagefeed = _load_stagefeed()


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

# id дела в обоих пространствах: kanban-карточка — `t_<hex>`, своё дело —
# `d_<hex>` (`docs/OWNERSHIP.md`). Один паттерн на файл: по нему и `session_key`
# вопроса пула, и упоминание в его тексте, и поиск дела в индексе `/now`.
# Префикс — не украшение: он и отличает владение от наблюдения.
DEAL_ID_RE = re.compile(r"[td]_[0-9a-f]{6,}")

# Корневая БД: доска `default` лежит не в `kanban/boards/`, а прямо в корне Hermes.
DEFAULT_BOARD = "default"

# Каталог выбывших досок: ядро убирает туда закрытые доски (`_archived/<slug>/`)
# и ставит рядом камень `board.json` с `"archived": true`. Это не источник среза.
ARCHIVED_DIR = "_archived"


def hermes_home() -> Path:
    """Корень Hermes — та же точка, что у роутов.

    CLI зовёт её отсюда, а не ищет `HERMES_HOME` сам: иначе «где живёт store» и
    «где живут источники» разъехались бы между терминалом и экраном.
    """
    return _hermes_home()


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


def _open_journal_ro(home: Path) -> Optional[sqlite3.Connection]:
    """Свой журнал **на чтение**: `?mode=ro` и без создания файла.

    Отсутствие журнала — не ошибка, а «журнала ещё нет»: `None`. Чтение через
    обычное соединение (`sqlite3.connect(path)`) запрещено: журнал в WAL, и
    закрытие такого соединения вправе сделать checkpoint — то есть чтение
    меняло бы файл, хотя ни одного события не записано (`?mode=ro`, P0-контракт).
    """
    path = journal.journal_path(home)
    if not path.is_file():
        return None
    return journal.connect_ro(path, settled_only=True)


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
    """Живая проверка: плагин смонтирован и источники читаются на чтение.

    `journal` — состояние своего журнала: `events` (строк), `deals` (дел с
    событиями) и `drift.empty_journal` — сколько активных дел kanban журнал ещё
    не подобрал. Это число, а не «всё хорошо»: пока оно больше нуля, состояние
    этих дел считается по kanban, и расхождение видно счётчиком. `/health`
    журнал не создаёт и не пишет: нет файла — значит событий 0, а в расхождении
    все активные дела, и это правда.
    """
    home = _hermes_home()
    boards = _discover_boards(home)
    readable = [b for b in boards if _table_exists(Path(b["path"]), "tasks")]
    # Доски, которые не попали в `readable`, не считают свой drift ни в одну
    # сторону: без имени это выглядело бы как «расхождений нет». Причина (включая
    # `note` источника) идёт в `journal.note`, а не в тишину.
    skipped = [
        f"{b['slug']}: {b.get('note') or 'no tasks table'}" for b in boards if b not in readable
    ]
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
        "journal": _journal_health(home, readable, skipped),
        "mode": "read-only",
    }


def _journal_health(
    home: Path, boards: list[dict[str, Any]], skipped: Optional[list[str]] = None
) -> dict[str, Any]:
    """`{events, deals, drift: {empty_journal}}` — сколько журнал знает и чего не догнал.

    `drift.empty_journal` — активные дела kanban без единого события в журнале:
    именно им состояние считается по kanban (правило 7 контракта), и пока ingest
    их не подобрал, число больше нуля.

    Непрочитанное не выдаётся за «расхождений нет»: доска, которую не удалось
    прочитать (или у которой нет `tasks`), в `active` не попадает, поэтому её
    дела не считаются ни в одну сторону — и это названо в `note`. Поле `note`
    появляется только тогда, когда сказать есть что; при полностью читаемых
    источниках ответ — ровно три ключа контракта.
    """
    active: set[str] = set()
    unread: list[str] = list(skipped or [])
    for entry in boards:
        rows, err = _query_checked(
            Path(entry["path"]),
            "SELECT id FROM tasks WHERE status NOT IN (?, ?)",
            TERMINAL_STATUSES,
        )
        if err:
            unread.append(f"{entry.get('slug')}: {err}")
            continue
        active.update(str(row.get("id")) for row in rows)

    notes = [f"boards unreadable: {'; '.join(unread)}"] if unread else []
    totals = {"events": 0, "deals": 0}
    known: set[str] = set()
    try:
        conn = _open_journal_ro(home)
    except Exception as exc:  # noqa: BLE001 - свой журнал не открылся
        conn = None
        notes.append(f"journal unreadable: {type(exc).__name__}: {exc}")
    if conn is not None:
        try:
            totals = journal.counts(conn)
            known = journal.deal_ids(conn)
        except Exception as exc:  # noqa: BLE001 - битый журнал
            notes.append(f"journal unreadable: {type(exc).__name__}: {exc}")
        finally:
            conn.close()

    answer: dict[str, Any] = {
        "events": totals["events"],
        "deals": totals["deals"],
        "drift": {"empty_journal": len(active - known)},
    }
    if notes:
        answer["note"] = "; ".join(notes)
    return answer


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
# Набор один на весь бэкенд: fold читает его из журнала, а не держит второй копии —
# разъехавшийся набор закрывал бы дела не там, где их закрывает состояние.
TERMINAL_STATUSES = journal.TERMINAL_STATUSES

# Порядок таксономии — тоже один: fold выбирает победителя по нему, поверхность
# по нему же сортирует и группирует. Вторая копия здесь разошлась бы с журналом.
WAITING_PRIORITY = journal.WAITING_PRIORITY

# Ран считается живым, только если heartbeat не старше этого порога.
HEARTBEAT_FRESH_SEC = 300

# Outcome'ы рана, означающие падение попытки, а не работу. Единственный источник
# этого набора: он уходит клиенту в `crash_outcomes`, чтобы UI не держал копию.
CRASH_OUTCOMES = ("crashed", "gave_up", "spawn_failed", "timed_out")

# Статусы дела, в которых оно ждёт незавершённого родителя в графе зависимостей.
GATE_STATUSES = ("todo", "ready")

# Слова, по которым причина блока читается как «нужен доступ или права» —
# нужны только если причина блока пришла без typed-колонки `block_kind`.
CAPABILITY_WORDS = journal.CAPABILITY_WORDS

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

# Вид ожидания «человеку осталось выбрать решение» (`waiting.human_kind`):
# ответ на вопрос запаркованного дела получен, и `continue|stop` пишется командой
# (`duty decide`). Третий вид рядом с `question` (ждёт ответа) и `status` (ждёт
# решения по статусу) — их не подменяет, а называет другой факт.
DECISION_KIND = "decision"

# Сколько символов ответа человека попадает в строку доказательства: решение
# принимается по ответу, но таблица `duty now` не переносит строк.
DECISION_ANSWER_LIMIT = 80

# Сколько задач берёт экран «Здоровье» с доски. Это не срез «сейчас»: тут важна
# доля, а не мгновенная картина, поэтому лимит больше. Сработавший лимит виден
# как `truncated` доски и назван в `warnings[]` итога — молча урезать выборку
# нельзя, иначе и доля, и распределение ожидания врут.
STATS_TASK_LIMIT = 5000

# Исходы рана, которые считаются успехом попытки. Отдельно от статуса: у рана
# бывает `status='awaiting_merge'` с `outcome='completed'` — этап пройден.
SUCCESS_OUTCOMES = ("completed", "done", "succeeded")

# Сколько тела дела отдаём на экран «Дело» (тело карточки — спецификация, в
# ответе она нужна целиком, но не бесконечно).
DEAL_BODY_LIMIT = 20000


def _now_sec() -> int:
    """Текущее время в unix-секундах — как все `*_at` в источниках."""
    return int(time.time())


def _age_sec(at: Optional[int], now: int) -> Optional[int]:
    """Возраст метки времени в секундах; None, если метки нет."""
    if at is None:
        return None
    return max(0, int(now) - int(at))


def _pid_alive(pid: Optional[int]) -> Optional[bool]:
    """Честная проверка живости pid: None — pid неизвестен, иначе ответ ядра.

    Реализация одна на бэкенд — в журнале (`journal.pid_alive`): fold проверяет
    тот же pid по тем же правилам, и вторая копия `os.kill` разошлась бы с ним в
    трактовке `EPERM`.
    """
    return journal.pid_alive(pid)


def _pid_text(pid: Optional[int], pid_alive: Optional[bool]) -> str:
    """Текст про pid ровно по тому, что известно: жив / состояние неизвестно / не записан.

    Одна формулировка на все ветки: иначе копии в `crashed` и `working` разъезжаются
    и одна из них называет «живым» то, чего не проверяла. `pid_alive=False` сюда не
    доходит: обе ветки отсекают мёртвый pid раньше — это отдельный факт `dead_lease`.
    """
    return journal.pid_text(pid, pid_alive)


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
    """Живые вопросы пула (`ask.LIVE_STATUSES`), разложенные по id дела.

    Набор живых статусов берётся у `ask.py`, а не пишется здесь второй раз: `sent`
    и `pending` — один и тот же живой вопрос (второй ещё не ушёл в iMessage), и
    разъехавшийся набор показывал бы `status='pending'` там, где вопрос уже есть.

    Схема пула `card_id` не знает: у вопроса с `origin='card'` id карточки лежит
    в `session_key` (проверено на живых данных — там ровно `t_xxxxxxxx`), а у
    остальных связь видна только по упоминанию `t_<id>` в тексте. Пути полей
    поэтому такие: `session_key` → затем `text`/`text_full`.

    `session_key` разворачивается в id дела **одной функцией** (`ask.deal_id_of`),
    а не как есть: у своего дела ключ пула — `t_d_<hex>`, и подставить его сырым
    значило бы не найти вопрос своего дела ни в `/now`, ни в `_pool_questions`
    (находка координатора 03.11). Второй элемент — причина сбоя: недоступный
    пул это не «вопросов нет», иначе `wait_human` молча не срабатывает и ответ
    врёт про «никто не ждёт». Третий — признак того, что живых вопросов больше
    лимита: обрезанный пул тоже нельзя выдавать за полный. Проверяется лишней
    строкой (`LIMIT + 1`), а не равенством длины лимиту, — иначе полный пул
    ровно на `QUESTION_LIMIT` вопросов ложной пометки не избежит.
    """
    db = home / "messaging" / "pool.db"
    if not db.is_file():
        return {}, f"pool.db not found: {db}", False
    marks = ", ".join("?" * len(ask.LIVE_STATUSES))
    rows, err = _query_checked(
        db,
        f"SELECT * FROM questions WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
        (*ask.LIVE_STATUSES, QUESTION_LIMIT + 1),
    )
    if err:
        return {}, f"pool.db unreadable: {db} ({err})", False
    truncated = len(rows) > QUESTION_LIMIT
    rows = rows[:QUESTION_LIMIT]
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        holders: set[str] = set()
        deal_id = ask.deal_id_of(str(row.get("session_key") or ""))
        if deal_id:
            holders.add(deal_id)
        blob = f"{row.get('text') or ''} {row.get('text_full') or ''}"
        holders.update(DEAL_ID_RE.findall(blob))
        for task_id in holders:
            index.setdefault(task_id, row)
    return index, None, truncated


def _pool_context(
    home: Path,
) -> tuple[dict[str, dict[str, Any]], Optional[str], list[dict[str, str]]]:
    """Пул вопросов: индекс по делу, готовая заметка о неполноте и ошибки ответа.

    Пул нужен двум роутам (`/now` и `/deal/...`), и заметка о неполноте обязана
    звучать одинаково: иначе один экран говорит «вопросов нет», а другой честно
    предупреждает, что факт `wait_human` не проверен.
    """
    questions, pool_err, pool_truncated = _pool_questions(home)
    notes: list[str] = []
    errors: list[dict[str, str]] = []
    if pool_err:
        notes.append(f"пул вопросов недоступен ({pool_err})")
        errors.append({"board": "messaging/pool.db", "reason": pool_err})
    if pool_truncated:
        reason = f"questions truncated: more than {QUESTION_LIMIT} live questions"
        notes.append(f"живых вопросов в пуле больше {QUESTION_LIMIT} (старые не вошли)")
        errors.append({"board": "messaging/pool.db", "reason": reason})
    pool_note = f"{'; '.join(notes)} — факт wait_human не проверен" if notes else None
    return questions, pool_note, errors


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


# --------------------------------------------------- журнал событий (0.0.3)
#
# Состояние дела считается fold'ом по событиям своего журнала (контракт
# `docs/JOURNAL.md`). Kanban-факты (`_waiting` выше) остаются нужны в двух
# случаях: журнал по делу пуст (правило 7 контракта) и журнал есть, но живого
# факта в нём нет — например, у дела было только `created`. В обоих случаях
# состояние считается по kanban, а `waiting.evidence` начинается с того, откуда
# оно взято: «нет данных в журнале» нельзя показывать как «всё спокойно».

# Имя источника в `errors[]`/`evidence`, когда свой журнал не читается. Одна копия
# в `journal.py`: иначе путь к журналу разошёлся бы между модулями.
JOURNAL_SOURCE = journal.JOURNAL_SOURCE


def _journal_context(
    home: Path, boards: list[dict[str, Any]], *, ingest: bool = False
) -> tuple[dict[str, list[dict[str, Any]]], Optional[str], list[dict[str, str]]]:
    """События журнала по делам; при `ingest` — сначала проход ingest.

    Читающие роуты журнал не пишут и файла не создают (`create=False`): иначе
    «открыл экран» и «включил журнал» стали бы одним действием, а срез отвечал бы
    за запись. Недоступный журнал не валит ответ: причина уходит в `errors`, а
    состояние дел считается по kanban с пометкой в `evidence`.

    Без `ingest` соединение открывается строго на чтение (`?mode=ro`): журнал
    живёт в WAL, и обычное соединение при закрытии вправе сделать checkpoint —
    тогда «посмотрел срез» меняло бы файл, хотя ни одного события не записано.
    """
    try:
        conn = journal.open_journal(home, create=ingest) if ingest else _open_journal_ro(home)
    except Exception as exc:  # noqa: BLE001 - свой журнал не открылся
        reason = f"{type(exc).__name__}: {exc}"
        return {}, f"journal: недоступен ({reason}) — состояние из kanban", [
            {"board": JOURNAL_SOURCE, "reason": reason}
        ]
    if conn is None:  # журнала ещё нет — это пустой журнал, а не ошибка
        return {}, None, []
    errors: list[dict[str, str]] = []
    try:
        if ingest:
            report = journal.ingest(conn, home, boards)
            errors.extend(report.get("errors") or [])
            # В том же проходе журнал узнаёт ответы пула: вопрос, на который уже
            # ответили, обязан перестать быть живым фактом ожидания.
            answers = ask.sync_answers(conn, home)
            for error in answers.get("errors") or []:
                if error not in errors:
                    errors.append(error)
        by_deal: dict[str, list[dict[str, Any]]] = {}
        for event in journal.read_events(conn):
            by_deal.setdefault(str(event["deal_id"]), []).append(event)
        return by_deal, None, errors
    except Exception as exc:  # noqa: BLE001 - битый/занятый журнал
        reason = f"{type(exc).__name__}: {exc}"
        return {}, f"journal: недоступен ({reason}) — состояние из kanban", [
            {"board": JOURNAL_SOURCE, "reason": reason}
        ]
    finally:
        conn.close()


def _deal_waiting(
    folded: dict[str, Any],
    kanban_waiting: dict[str, str],
    question: Optional[dict[str, Any]],
    journal_note: Optional[str],
    *,
    home: Optional[Path] = None,
    deal_id: str = "",
) -> dict[str, Any]:
    """`waiting` дела: журнал первым, kanban — только с честной пометкой, откуда факт.

    `human_kind` отделяет два разных «нужен человек»: `question` — ждёт ответа на
    вопрос, `status` — ждёт решения по статусу (блок, разбор, ревью, мердж), а
    `decision` — ответ по запаркованному делу уже получен, и человеку осталось
    выбрать `continue|stop` (`duty decide`). Это закрывает п.5 заметки 0.0.2, где
    оба первых случая назывались «ждёт ответа».
    """
    if folded["state"] is not None:
        return _journal_waiting(folded, question, home=home, deal_id=deal_id)
    if journal_note:
        head = journal_note
    elif folded["reason"] == "empty":
        head = "journal: empty — состояние из kanban"
    else:
        head = (
            f"journal: нет живых фактов (последнее событие: {folded['last_kind'] or '—'}) "
            f"— состояние из kanban"
        )
    state = kanban_waiting["state"]
    return {
        "state": state,
        "evidence": f"{head}; {kanban_waiting['evidence']}",
        "human_kind": "question" if question is not None else ("status" if state == "wait_human" else None),
    }


def _journal_waiting(
    folded: dict[str, Any],
    question: Optional[dict[str, Any]],
    *,
    home: Optional[Path] = None,
    deal_id: str = "",
) -> dict[str, Any]:
    """Состояние из журнала плюс живой вопрос пула, которого журнал ещё не знает.

    `ask()` станет единственным каналом вопросов и будет писать `question_asked`
    в журнал (W2). Пока журнал вопросов не знает, живой вопрос пула — факт, а не
    догадка, и он важнее прочих состояний (приоритет 1 таксономии); в `evidence`
    при этом сказано, что факт из пула, а не из журнала.

    Запаркованное дело с полученным ответом (`wait_human/decision`) получает
    строку решения: что именно человек ответил и какой командой это записать.
    Текст ответа читается из пула (`ask.answer_of`, только чтение) — в журнале
    его нет, и без него строка «ждёт решения» не отвечает на вопрос «чего не
    хватает».
    """
    waiting = _pool_waiting(folded, question)
    if waiting.get("human_kind") != DECISION_KIND:
        return waiting
    parked = folded.get("parked") or {}
    answer = None if home is None else ask.answer_of(home, parked.get("question_id"))
    return {
        **waiting,
        "evidence": f"{waiting.get('evidence') or ''}; "
        + _decision_line(deal_id, parked.get("question_id"), answer),
    }


def _pool_waiting(folded: dict[str, Any], question: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Состояние журнала и живой вопрос пула в одном ответе (до строки решения)."""
    state = folded["state"]
    human_kind = folded["human_kind"]
    if question is None or human_kind == "question":
        return {"state": state, "evidence": folded["evidence"], "human_kind": human_kind}
    pool_fact = (
        f"пул: вопрос #{question.get('id')} status='{question.get('status')}' "
        f"origin='{question.get('origin')}' — в журнале вопроса нет"
    )
    if state != "wait_human":
        return {
            "state": "wait_human",
            "evidence": f"{pool_fact}; ещё: {state}: {folded['evidence']}",
            "human_kind": "question",
        }
    return {"state": state, "evidence": f"{folded['evidence']}; ещё: {pool_fact}", "human_kind": "question"}


def _decision_line(deal_id: str, question_id: Any, answer: Optional[str]) -> str:
    """Строка ожидания запаркованного дела с ответом: что ответили и что сделать.

    Движок не угадывает решение по словам ответа (`docs/RUNBOOK.md`): текст
    остаётся текстом, а решение приходит командой. Поэтому здесь названо и то и
    другое — ответ как факт и команда как единственный способ его записать.
    """
    head = "" if not answer else answer[:DECISION_ANSWER_LIMIT] + (
        "…" if len(answer) > DECISION_ANSWER_LIMIT else ""
    )
    said = f"ответ #{question_id or '—'} получен: {head}" if head else f"ответ #{question_id or '—'} получен"
    return f"{said}; нужно решение: bin/duty decide {deal_id or '<id>'} continue|stop"


def _board_deals(
    entry: dict[str, Any],
    now: int,
    questions: dict[str, dict[str, Any]],
    pool_note: Optional[str],
    journal_events: dict[str, list[dict[str, Any]]],
    journal_note: Optional[str] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    """Активные дела одной доски с лизингом, попыткой, последним действием и ожиданием.

    Провал чтения самой `tasks` роняет этот вызов: доска тогда попадает в
    `errors` ответа (исключение не перехватывается здесь — его тип и текст важны
    для причины). Провал вспомогательных таблиц (`task_runs`, `task_events`,
    `task_links`) деградирует факты и уходит в `warnings` доски, а не в ошибку.

    Дело, у которого последнее state-changing событие журнала — `completed`
    (правило 2 контракта), в срез не попадает: журнал говорит, что дело закрыто.
    Расхождение с kanban при этом не молчит — дело называется в `warnings` доски.
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
    closed: list[str] = []
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
        question = questions.get(task_id)
        folded = journal.fold(journal_events.get(task_id, []), now=now)
        if not folded["active"]:
            # Правило 2: журнал говорит, что дело закрыто. В срез оно не идёт, но и
            # не исчезает молча — расхождение с kanban названо в warnings доски.
            closed.append(
                f"дело {task_id}: в журнале completed, а kanban status={task.get('status')} — "
                f"в срез не попало (правило 2 fold)"
            )
            continue
        waiting = _deal_waiting(
            folded,
            _waiting(
                task,
                attempt,
                lease,
                last_run,
                parents_of.get(task_id, []),
                None if slot_holder == task_id else slot_holder,
                question,
                block_reasons.get(task_id, ""),
                pool_note,
            ),
            question,
            journal_note,
            home=home,
            deal_id=task_id,
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
                # Этап и номер попытки — из журнала (`dashboard/stagefeed.py`):
                # «что сейчас» видно без открытия дела. Вложенный объект, потому
                # что поле `attempt` в этой же форме уже занято фактом рана
                # kanban — одно имя не может значить две вещи.
                "stage": stagefeed.current(journal_events.get(task_id, []), now),
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
        "warnings": warnings + closed,
        "truncated": len(tasks) >= DEAL_LIMIT,
    }


# ------------------------------------------------------- свои дела (0.0.3, W5)
#
# Владение делом (`docs/OWNERSHIP.md`): свой store (`dashboard/deals.py`) живёт
# рядом с журналом, в той же `duty/duty.db`. Срез «сейчас» показывает свои дела
# наравне с kanban-делами — те же поля и та же сортировка, отличается только
# префиксом `d_`; `/deal` отдаёт одну форму для обоих, чтобы экран не знал,
# откуда дело. Читающие пути store не создают (`create=False`): смотр экрана не
# должен заводить таблицы как побочный эффект.

# Имя источника в `errors[]` для своих дел — путь журнала: store живёт в том же
# файле, и вторая строка «duty.db (дела)» только путала бы.
DEALS_SOURCE = journal.JOURNAL_SOURCE

# Остаточная ветка для своего дела: живого факта в журнале нет, а kanban тут ни
# при чём — своего дела в kanban нет, и «состояние из kanban» было бы выдумкой.
# Поэтому состояние называет `deals.status`, а текст говорит **только то, что
# подтверждено**: у `todo`/`ready` нет ни одного шага движка, у `running` — ни
# одного живого факта журнала. Приписывать движку прошлую версию («спавна в 0.0.3
# нет», «попыток нет») нельзя: он спавнит попытки и берёт лизинг (`runner.py`), и
# та же ложь рядом уже исправлена — `OWN_NO_ATTEMPTS`.
OWN_WAITING_FALLBACK = {
    "todo": (
        "wait_slot",
        "status=todo без живых фактов: шага движка ещё не было (bin/duty run)",
    ),
    "ready": (
        "wait_slot",
        "status=ready без живых фактов: шага движка ещё не было (bin/duty run)",
    ),
    "running": (
        "no_signal",
        "status=running без живого факта журнала: идёт ли попытка — неизвестно",
    ),
    "blocked": ("wait_human", "status=blocked: нужен человек"),
    "review": ("wait_human", "status=review: ждёт вердикта ревьюера"),
}


def _timeline(events: list[dict[str, Any]], now: int) -> list[dict[str, Any]]:
    """Таймлайн дела по событиям журнала: что произошло, когда и с чем.

    Одна форма для своих и наблюдаемых дел: экран рисует её одинаково. Выжимка
    payload'а берётся тем же `_payload_summary`, что у последнего действия, —
    второго форматтера в ответе быть не должно.
    """
    return [
        {
            "kind": event.get("kind"),
            "at": event.get("ts"),
            "recorded_at": event.get("recorded_at"),
            "age_sec": _age_sec(event.get("ts"), now),
            "source": event.get("source"),
            "summary": _payload_summary(json.dumps(event.get("payload") or {}, ensure_ascii=False)),
        }
        for event in events
    ]


def _own_waiting(
    folded: dict[str, Any],
    question: Optional[dict[str, Any]],
    status: str,
    journal_note: Optional[str],
    pool_note: Optional[str],
    *,
    home: Optional[Path] = None,
    deal_id: str = "",
) -> dict[str, Any]:
    """`waiting` своего дела: журнал первым, остальное — по `deals.status`.

    Kanban-ветки здесь нет и быть не может: своего дела в kanban не существует, и
    подставить туда «состояние из kanban» значило бы выдумать факт. Пометка в
    `evidence` говорит, откуда взято состояние, — как и у наблюдаемых дел.
    """
    if folded["state"] is not None:
        return _journal_waiting(folded, question, home=home, deal_id=deal_id)
    if journal_note:
        head = journal_note
    else:
        head = (
            f"journal: нет живых фактов (последнее событие: {folded['last_kind'] or '—'}) "
            "— состояние из status своего дела"
        )
    state, evidence = OWN_WAITING_FALLBACK.get(
        status, ("wait_slot", f"status={status or '—'}: нет живых фактов")
    )
    human_kind = "question" if question is not None else ("status" if state == "wait_human" else None)
    if pool_note and state == "wait_human" and question is None:
        evidence = f"{evidence}; {pool_note}"
    return {"state": state, "evidence": f"{head}; {evidence}", "human_kind": human_kind}


def _own_attempt_facts(
    events: list[dict[str, Any]],
    leases: dict[tuple[str, int], int],
    now: int,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Попытка и лизинг своего дела: `(попытка, лизинг)` — факты, а не константы.

    Попытка — последняя незакрытая в журнале (`attempt_started` без
    `attempt_ended`): у своего дела нет записи рана kanban, и называть живую
    попытку «попытки нет» значит противоречить собственному таймлайну
    (`GET /duty/{board}/{id}/attempts`). Все попытки закрыты — идущей нет, и это
    тоже факт журнала.

    Живость доказывается теми же двумя источниками, что у `stagefeed._alive`:
    `pid` из `attempt_started` (проверка сигналом, `journal.pid_alive`) и срок
    лизинга — строка store у пары `(этап, попытка)`, а если строки нет — срок из
    самого события. `run_status` и `heartbeat_age_sec` пусты: журнал heartbeat'а
    не пишет (лизинг продлевается в store), рана kanban у своего дела нет.

    Лизинг — три ответа, как у незакрытой попытки: `live`/`expired` по сроку,
    `unknown` — попытка идёт, а срока нет ни в store, ни в событии («мёртв» здесь
    был бы догадкой), `none` — ни строки store, ни идущей попытки. `claim_lock` у
    своего дела не бывает: этап держит pid, и он назван в попытке.
    """
    open_rows = [row for row in stagefeed.attempts(events, now, leases=leases) if row["finished_at"] is None]
    row = open_rows[-1] if open_rows else None
    expires = row["lease_expires"] if row is not None else None
    if row is None and leases:
        # Строка лизинга без попытки — тоже факт store (лизинг взяли, а
        # `attempt_started` не успел лечь): молчать о ней значило бы «лизинга нет».
        expires = max(leases.values())
    if expires is None:
        lease: dict[str, Any] = {
            "claim_lock": None,
            "expires_at": None,
            "expires_in_sec": None,
            "state": "unknown" if row is not None else "none",
        }
    else:
        lease = {
            "claim_lock": None,
            "expires_at": int(expires),
            "expires_in_sec": int(expires) - int(now),
            "state": "expired" if int(expires) <= int(now) else "live",
        }
    if row is None:
        return None, lease
    return (
        {
            "run_id": row["run_id"],
            "source": "journal",
            "start": row["started_at"],
            "age_sec": _age_sec(row["started_at"], now),
            "pid": row["pid"],
            "pid_alive": _pid_alive(row["pid"]),
            "heartbeat_age_sec": None,
            "run_status": None,
        },
        lease,
    )


def _own_shaped(
    row: dict[str, Any],
    events: list[dict[str, Any]],
    now: int,
    question: Optional[dict[str, Any]],
    journal_note: Optional[str],
    pool_note: Optional[str],
    home: Optional[Path] = None,
    leases: Optional[dict[tuple[str, int], int]] = None,
) -> dict[str, Any]:
    """Дело своего store в той же форме, что дело kanban в `/now`.

    Пустые факты названы пустыми, а не выброшены: `last_run`/`priority`/
    `workspace_kind` у своего дела нет вовсе, и это видно полем, а не отсутствием
    ключа — иначе экран не отличил бы «нечего показывать» от «не посчитали».
    Попытка и лизинг — наоборот, читаются из журнала и store (`_own_attempt_facts`):
    движок спавнит попытки и держит лизинг, и постоянные `null`/`none` на этом
    месте врали бы про идущую работу.
    """
    last = events[-1] if events else None
    folded = journal.fold(events, now=now)
    attempt, lease = _own_attempt_facts(events, leases or {}, now)
    return {
        "board": str(row["board"]),
        "id": str(row["id"]),
        "title": row.get("title"),
        "assignee": row.get("assignee"),
        "status": str(row["status"]),
        "priority": None,
        "consecutive_failures": 0,
        "created_at": row.get("created_at"),
        "workspace_kind": None,
        "attempt": attempt,
        "lease": lease,
        "last_action": {
            "kind": None if last is None else last.get("kind"),
            "at": None if last is None else last.get("ts"),
            "age_sec": None if last is None else _age_sec(last.get("ts"), now),
            "summary": None
            if last is None
            else _payload_summary(json.dumps(last.get("payload") or {}, ensure_ascii=False)),
        },
        "last_run": None,
        # Этап и номер попытки — из журнала, как и у наблюдаемых дел: экран не
        # знает, откуда дело, и форма одна (`dashboard/stagefeed.py`).
        "stage": stagefeed.current(events, now),
        "waiting": _own_waiting(
            folded, question, str(row["status"]), journal_note, pool_note,
            home=home, deal_id=str(row["id"]),
        ),
        "_age_sec": _age_sec(row.get("created_at"), now),
    }


def _own_leases(conn: sqlite3.Connection, task_id: str) -> dict[tuple[str, int], int]:
    """Сроки лизингов дела из store: `{(этап, попытка): expires_at}`.

    Лизинг продлевается heartbeat'ом (`runner.follow`), поэтому строка store —
    актуальный срок этапа, а `attempt_started.lease_expires` — только начальный.
    Пара `(этап, попытка)` не даёт чужому сроку (лизинг уже следующей попытки
    того же этапа) оживить старую (`stagefeed.attempts`).
    """
    return {
        (str(row["stage"]), int(row["attempt"])): int(row["expires_at"])
        for row in conn.execute(
            "SELECT stage, attempt, expires_at FROM leases WHERE deal_id = ?", (task_id,)
        )
    }


def _own_deals(
    home: Path,
    now: int,
    questions: dict[str, dict[str, Any]],
    pool_note: Optional[str],
    journal_events: dict[str, list[dict[str, Any]]],
    journal_note: Optional[str],
    board: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Активные свои дела для среза «сейчас»: `(дела, ошибки)`.

    Отсутствие store — не ошибка, а «своих дел ещё нет» (`create=False` файла не
    создаёт). Нечитаемый store или журнал — строка в `errors[]`: пустой список без
    причины выглядел бы как «своих дел нет», а это разные факты.
    """
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 - свой store не открылся
        return [], [{"board": DEALS_SOURCE, "reason": f"{type(exc).__name__}: {exc}"}]
    if conn is None:
        return [], []
    try:
        if not deals.has_store(conn):
            return [], []
        out: list[dict[str, Any]] = []
        for row in deals.deal_rows(conn, board=board, open_only=True):
            events = journal_events.get(str(row["id"]), [])
            folded = journal.fold(events, now=now)
            if not folded["active"]:
                continue  # закрытое дело в срез не идёт (правило 2 fold)
            out.append(
                _own_shaped(
                    row,
                    events,
                    now,
                    questions.get(str(row["id"])),
                    journal_note,
                    pool_note,
                    home,
                    _own_leases(conn, str(row["id"])),
                )
            )
        return out, []
    except Exception as exc:  # noqa: BLE001 - битый store/журнал
        return [], [{"board": DEALS_SOURCE, "reason": f"store: {type(exc).__name__}: {exc}"}]
    finally:
        conn.close()


def _closed_block(fact: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Факт закрытия дела полями ответа: причина, время, этапы, статус.

    `fact` — ответ `journal.closed_fact` (`{"at", "payload"}`). `None` — дело не
    закрывалось фактом журнала, и подставлять сюда `status='done'` нельзя:
    строка store и факт журнала — разные вещи, а экран обязан назвать именно
    записанную причину.
    """
    if not fact:
        return None
    payload = fact.get("payload") or {}
    return {
        "at": int(fact.get("at") or 0),
        "cause": str(payload.get("cause") or ""),
        "stages": [str(stage) for stage in payload.get("stages") or ()],
        "status": str(payload.get("status") or "done"),
    }


def _deal_response(
    *,
    now: int,
    board: str,
    deal_id: str,
    row: dict[str, Any],
    shaped: Optional[dict[str, Any]],
    comments: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    stages: Optional[list[dict[str, Any]]] = None,
    stages_note: str = "",
    spec: Optional[str] = None,
    errors: Optional[list[dict[str, str]]] = None,
    closed: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Ответ «одно дело» — одна форма и для kanban-дела, и для своего.

    Экран не должен знать, откуда дело: ключи одинаковые, различаются только
    значения. У наблюдаемого дела нет наших комментариев и вложений — там пустые
    списки, а не отсутствие полей, иначе клиент держал бы две ветки разбора.

    `stages` — лента этапов (`dashboard/stagefeed.py`): состояние графа и попытки
    из журнала. Пустой список без причины выглядел бы как «этапов нет», поэтому
    `stages_note` называет, почему лента пуста (нет спеки, спека не читается),
    а `spec` — какой граф прочитан.

    `external_ref` — внешнее имя дела (`jira:RP-14080`), по которому его находят
    повторно. У наблюдаемого kanban-дела его нет: имя ставит тот, кто заводит
    дело в store (`docs/OWNERSHIP.md`, «Внешнее имя дела»).

    `closed` — факт закрытия (`journal.closed_fact`): причина и время, `null` —
    дело не закрывалось фактом журнала. Один ответ на вопрос «почему закрыто» для
    `deal`, `why` и `run`: состояния ожидания у закрытого дела нет, и вместо него
    называется записанный факт, а не пересказ статуса.
    """
    body_text = str(row.get("body") or "")
    status = str(row.get("status") or "")
    return {
        "now": now,
        "board": board,
        "id": deal_id,
        "title": row.get("title"),
        "status": status,
        "assignee": row.get("assignee"),
        "external_ref": row.get("external_ref"),
        "terminal": status in TERMINAL_STATUSES,
        "closed": _closed_block(closed),
        "created_at": row.get("created_at"),
        "body": body_text[:DEAL_BODY_LIMIT],
        "body_truncated": len(body_text) > DEAL_BODY_LIMIT,
        "deal": shaped,
        "comments": comments,
        "attachments": attachments,
        "timeline": timeline,
        "spec": spec,
        "stages": list(stages or []),
        "stages_note": stages_note,
        "errors": list(errors or []),
    }


def _stages_block(
    events: list[dict[str, Any]], now: int, spec_override: Optional[str] = None
) -> dict[str, Any]:
    """Лента этапов: граф читается по спеке, названной в журнале или в `?spec=`.

    Приоритет у явного `?spec=`: это просьба смотреть конкретный граф (дело
    завели раньше, чем журнал узнал спеку). Иначе — `created.payload.spec`, то
    есть та спека, по которой дело создавалось. Ни одного процесса: граф — файл,
    состояние — журнал (`dashboard/stagefeed.py`).
    """
    path = spec_override or stagefeed.spec_of(events)
    return stagefeed.snapshot(events, now, spec_path=path)



def _own_response(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    now: int,
    *,
    questions: Optional[dict[str, dict[str, Any]]] = None,
    pool_note: Optional[str] = None,
    journal_note: Optional[str] = None,
    spec: Optional[str] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    """Полный ответ по делу своего store (та же форма, что `/deal`)."""
    deal_id = str(row["id"])
    events = journal.read_events(conn, deal_id)
    folded = journal.fold(events, now=now)
    shaped = (
        _own_shaped(
            row,
            events,
            now,
            (questions or {}).get(deal_id),
            journal_note,
            pool_note,
            home,
            _own_leases(conn, deal_id),
        )
        if folded["active"]
        else None
    )
    feed = _stages_block(events, now, spec)
    return _deal_response(
        now=now,
        board=str(row["board"]),
        deal_id=deal_id,
        row=row,
        shaped=shaped,
        comments=deals.comments_of(conn, deal_id),
        attachments=deals.attachments_of(conn, deal_id),
        timeline=_timeline(events, now),
        stages=feed["stages"],
        stages_note=feed["note"],
        spec=feed["spec"],
        closed=journal.closed_fact(events),
    )


def collect_now(
    home: Path,
    now: int,
    board: Optional[str] = None,
    state: Optional[str] = None,
    ingest: bool = False,
) -> dict[str, Any]:
    """Срез «сейчас» по всем доскам: активные дела, отсортированные по таксономии.

    Сортировка детерминирована: `waiting.state` по `WAITING_PRIORITY`, затем
    возраст текущей попытки по убыванию, затем `id` — иначе UI дёргается между
    опросами. Доска, у которой не читается `tasks`, пропускается и попадает в
    `errors`; ответ при этом остаётся 200.

    `waiting` считается fold'ом по журналу (правило 7 контракта — исключение:
    дело без событий считается по kanban, и это сказано в `evidence`). `ingest`
    запускает проход в свой журнал до сбора среза; без него срез ничего не пишет.
    """
    boards = _discover_boards(home)
    scope = [entry for entry in boards if board is None or entry["slug"] == board]
    questions, pool_note, pool_errors = _pool_context(home)
    errors: list[dict[str, str]] = list(pool_errors)
    journal_events, journal_note, journal_errors = _journal_context(home, boards, ingest=ingest)
    errors.extend(journal_errors)
    # Свои дела (`docs/OWNERSHIP.md`) идут в тот же срез: экран отличает их только
    # префиксом `d_`. Ошибка своего store не валит ответ — она в `errors[]`, как и
    # ошибка доски: «своих дел нет» и «store не прочитался» — разные факты.
    own_deals, own_errors = _own_deals(
        home, now, questions, pool_note, journal_events, journal_note, board
    )
    errors.extend(own_errors)

    boards_out: list[dict[str, Any]] = []
    # Имя `deals` занято модулем своего store (`deals.store_path`), поэтому список
    # дел среза называется `items`: иначе внутри функции модуль был бы затенён и
    # `deals.store_path(...)` означал бы «метод списка».
    items: list[dict[str, Any]] = []
    for entry in scope:
        try:
            collected = _board_deals(entry, now, questions, pool_note, journal_events, journal_note, home)
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
            boards_out.append(unread)
            continue
        boards_out.append(
            {
                "slug": collected["slug"],
                "path": collected["path"],
                "read": True,
                "deals": len(collected["deals"]),
                "truncated": collected["truncated"],
                "warnings": collected["warnings"],
            }
        )
        items.extend(collected["deals"])

    # Свои дела вливаются в тот же список и в те же счётчики досок: иначе `/now`
    # показывал бы два разных «сколько дел» — по срезу и по доскам.
    items.extend(own_deals)
    own_by_board: dict[str, int] = {}
    for own in own_deals:
        own_by_board[str(own["board"])] = own_by_board.get(str(own["board"]), 0) + 1
    for slug, count in sorted(own_by_board.items()):
        for entry in boards_out:
            if entry["slug"] == slug:
                entry["deals"] = int(entry.get("deals") or 0) + count
                entry["own_deals"] = count
                break
        else:
            # Доска есть только в своём store: kanban-доски с таким слагом нет.
            # Это не «непрочитанная доска», поэтому `read: true` и путь store.
            boards_out.append(
                {
                    "slug": slug,
                    "path": str(deals.store_path(home)),
                    "read": True,
                    "deals": count,
                    "own_deals": count,
                    "note": "только свои дела: kanban-доски с таким слагом нет",
                }
            )

    order = {name: i for i, name in enumerate(WAITING_PRIORITY)}
    items.sort(
        key=lambda deal: (
            order.get(deal["waiting"]["state"], len(order)),
            -(deal["_age_sec"] or 0),
            deal["id"],
        )
    )
    by_state = {name: 0 for name in WAITING_PRIORITY}
    for deal in items:
        by_state[deal["waiting"]["state"]] = by_state.get(deal["waiting"]["state"], 0) + 1
    if state is not None:
        items = [deal for deal in items if deal["waiting"]["state"] == state]
    for deal in items:
        deal.pop("_age_sec", None)
    return {
        "now": now,
        "count": len(items),
        "deals": items,
        "by_state": by_state,
        "crash_outcomes": list(CRASH_OUTCOMES),
        "boards": boards_out,
        "errors": errors,
    }


# --------------------------------------------------- «дело» и «здоровье» (0.0.2)
#
# Экран «Дело» — одна карточка: чем дело занято сейчас (тот же факт-набор, что в
# `/now`) плюс её собственная спецификация. Экран «Здоровье» — агрегат по ранам:
# доля этапов, пройденных с первой попытки, распределение ожидания до первой
# попытки (p50/p90/p99, а не одно число) и таксономия исходов. Ни один из них
# ничего не пишет и не пересчитывает статусы.

PERCENTILES = (50, 90, 99)


def _waiting_distribution(values: list[int]) -> dict[str, Any]:
    """Распределение ожидания до первой попытки: p50, p90, p99, `count`, `max`.

    Метод — ближайший ранг (nearest rank), без интерполяции: перцентиль `p` —
    это значение на позиции `ceil(p / 100 * n)` в возрастающем ряду (нумерация
    с 1). На наборе 1..10 это даёт p50 = 5 и p90 = 9 (у интерполяции было бы
    5.5 и 9.1). Ряд сортируется один раз, все три перцентиля берутся из него по
    индексу — второго прохода по данным нет, поэтому p99 отдаётся всегда.

    Пустая выборка — `null` во всех перцентилях и `count: 0`: «нет данных» и
    «ждали 0 секунд» — разные вещи, нули здесь были бы выдумкой.
    """
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return {"p50": None, "p90": None, "p99": None, "count": 0, "max": None}
    shaped: dict[str, Any] = {}
    for pct in PERCENTILES:
        # ceil(pct * count / 100) целочисленно, без float на границах
        rank = -(-pct * count // 100)
        shaped[f"p{pct}"] = int(ordered[min(rank, count) - 1])
    shaped["count"] = count
    shaped["max"] = int(ordered[-1])
    return shaped


def _stats_board(entry: dict[str, Any], *, since: Optional[int] = None) -> dict[str, Any]:
    """Метрики одной доски: первая попытка, ожидание до неё, исходы ранов.

    Провал чтения `tasks` роняет вызов (доска уходит в `errors`), провал
    `task_runs` — только в `warnings`: доля попыток тогда не считается, и это
    видно как `first_try.with_runs = 0`, а не как «всё с первой попытки».

    `since` — нижняя граница окна по `tasks.created_at` (unix-секунды). Фильтр
    живёт здесь, а не в CLI: распределение и доля считаются по тому же проходу,
    иначе терминал показывал бы числа, которых не считает ни один роут. Задачи
    без `created_at` в окно не попадают — их нечем датировать, — и это названо
    в `warnings`, а не спрятано.
    """
    slug = str(entry["slug"])
    path = Path(entry["path"])
    if entry.get("note") == "empty db":
        raise RuntimeError(f"empty db ({path}): 0 bytes")
    warnings: list[str] = []
    with closing(_connect_ro(path)) as conn:
        # Лимит проверяется лишней строкой: ровно `STATS_TASK_LIMIT` задач не
        # должны помечаться обрезанными.
        tasks, err = _fetch(
            conn,
            "SELECT id, status, created_at FROM tasks "
            "WHERE (? IS NULL OR (created_at IS NOT NULL AND created_at >= ?)) "
            "ORDER BY created_at, id LIMIT ?",
            (since, since, STATS_TASK_LIMIT + 1),
        )
        if err:
            raise RuntimeError(err)
        if since is not None:
            rows, err = _fetch(conn, "SELECT COUNT(*) AS n FROM tasks WHERE created_at IS NULL")
            if err:
                warnings.append(f"tasks(created_at): {err}")
            elif rows and int(rows[0].get("n") or 0):
                warnings.append(
                    f"задач без created_at: {int(rows[0]['n'])} — в окно since не вошли, датировать нечем"
                )
        truncated = len(tasks) > STATS_TASK_LIMIT
        tasks = tasks[:STATS_TASK_LIMIT]
        task_ids = [str(task.get("id")) for task in tasks]
        runs_by_task: dict[str, list[dict[str, Any]]] = {}
        if task_ids:
            marks = ", ".join("?" * len(task_ids))
            rows, err = _fetch(
                conn,
                f"SELECT task_id, status, outcome, started_at FROM task_runs "
                f"WHERE task_id IN ({marks}) ORDER BY started_at, id",
                tuple(task_ids),
            )
            if err:
                warnings.append(f"task_runs: {err}")
            for row in rows:
                runs_by_task.setdefault(str(row.get("task_id")), []).append(row)

    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    waits: list[int] = []
    first_try_ok = 0
    with_runs = 0
    runs_total = 0
    for task in tasks:
        status = str(task.get("status") or "—")
        by_status[status] = by_status.get(status, 0) + 1
        runs = runs_by_task.get(str(task.get("id")), [])
        if not runs:
            continue
        with_runs += 1
        runs_total += len(runs)
        for run in runs:
            outcome = run.get("outcome")
            key = str(outcome) if outcome else f"без исхода ({run.get('status') or '—'})"
            by_outcome[key] = by_outcome.get(key, 0) + 1
        first = runs[0]
        if str(first.get("outcome") or "") in SUCCESS_OUTCOMES:
            first_try_ok += 1
        created = task.get("created_at")
        started = first.get("started_at")
        if created is not None and started is not None:
            waits.append(max(0, int(started) - int(created)))

    return {
        "slug": slug,
        "path": str(path),
        "read": True,
        "truncated": truncated,
        "warnings": warnings,
        "tasks_total": len(tasks),
        "tasks_by_status": by_status,
        "runs_total": runs_total,
        "runs_by_outcome": by_outcome,
        "first_try": {
            "ok": first_try_ok,
            "with_runs": with_runs,
            "share": (first_try_ok / with_runs) if with_runs else None,
        },
        "waiting_sec": _waiting_distribution(waits),
        "_waits": waits,
    }


def collect_stats(
    home: Path,
    now: int,
    board: Optional[str] = None,
    since: Optional[int] = None,
) -> dict[str, Any]:
    """Здоровье живых досок: агрегат по ранам плюс итог по прочитанным.

    Распределение ожидания считается по объединённой выборке, а не как среднее
    перцентилей досок: перцентиль перцентилей не равен перцентилю объединённой
    выборки. Доска, у которой не читается `tasks`, попадает в `errors` и в итог
    не входит — ответ остаётся 200.

    `board` и `since` — те же фильтры, что у `collect_now`: CLI не фильтрует
    готовый агрегат (перцентиль подмножества не считается из перцентилей целого),
    а просит ту же функцию посчитать по нужному окну. `boards[]` и `totals`
    поэтому всегда согласованы между собой.

    Обрезанный срез доски (`truncated`) остаётся флагом этой доски, но дополнительно
    называется в `warnings[]` верхнего уровня: распределение по неполной выборке
    нельзя выдавать за полное, а `totals.waiting_sec` читают в отрыве от `boards[]`.
    """
    boards: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    warnings: list[str] = []
    waits: list[int] = []
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    tasks_total = 0
    runs_total = 0
    first_try_ok = 0
    with_runs = 0
    scope = [entry for entry in _discover_boards(home) if board is None or entry["slug"] == board]
    for entry in scope:
        try:
            collected = _stats_board(entry, since=since)
        except Exception as exc:  # noqa: BLE001 - ошибка доски не валит ответ
            reason = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
            errors.append({"board": str(entry["slug"]), "reason": reason})
            unread: dict[str, Any] = {
                "slug": entry["slug"],
                "path": str(entry["path"]),
                "read": False,
                "tasks_total": 0,
            }
            if entry.get("note"):
                unread["note"] = entry["note"]
            boards.append(unread)
            continue
        waits.extend(collected.pop("_waits"))
        tasks_total += collected["tasks_total"]
        runs_total += collected["runs_total"]
        first_try_ok += collected["first_try"]["ok"]
        with_runs += collected["first_try"]["with_runs"]
        for key, count in collected["tasks_by_status"].items():
            by_status[key] = by_status.get(key, 0) + count
        for key, count in collected["runs_by_outcome"].items():
            by_outcome[key] = by_outcome.get(key, 0) + count
        if collected["truncated"]:
            warnings.append(
                f"доска {collected['slug']}: срез обрезан лимитом {STATS_TASK_LIMIT} задач — "
                f"распределение ожидания и доля первой попытки считаются по неполной выборке"
            )
        boards.append(collected)

    return {
        "now": now,
        "boards": boards,
        "errors": errors,
        "warnings": warnings,
        "totals": {
            "tasks_total": tasks_total,
            "tasks_by_status": by_status,
            "runs_total": runs_total,
            "runs_by_outcome": by_outcome,
            "first_try": {
                "ok": first_try_ok,
                "with_runs": with_runs,
                "share": (first_try_ok / with_runs) if with_runs else None,
            },
            "waiting_sec": _waiting_distribution(waits),
        },
        "success_outcomes": list(SUCCESS_OUTCOMES),
        "crash_outcomes": list(CRASH_OUTCOMES),
    }


@router.get("/stats")
def stats(board: Optional[str] = None, since: Optional[int] = None) -> dict[str, Any]:
    """Здоровье: доля с первой попытки, распределение ожидания (p50/p90/p99), исходы.

    `?board=<slug>` — только одна доска (404, если её нет);
    `?since=<unix>` — только дела, созданные не раньше этой секунды. Оба фильтра
    считает `collect_stats`, поэтому CLI и роут отдают одну и ту же форму и одни
    и те же числа; без параметров ответ прежний.
    """
    home = _hermes_home()
    if board is not None:
        _board_entry(home, board)
    return collect_stats(home, _now_sec(), board=board, since=since)


@router.get("/deal/{board}/{task_id}")
def deal(board: str, task_id: str, spec: Optional[str] = None) -> dict[str, Any]:
    """Одно дело: спецификация плюс тот же факт-набор, что в `/now`.

    Дело ищется по префиксу id: `d_<hex>` — своё (store), `t_<hex>` — наблюдаемое
    (kanban). Форма ответа одна на оба: экран не должен знать, откуда дело, иначе
    пришлось бы держать две ветки разбора одного и того же экрана.

    `deal` — `null` для закрытого дела (`done`/`archived`): состояний ожидания у
    него нет, и придумывать их нельзя. Таймлайн попыток живёт отдельно —
    `GET /duty/{board}/{task_id}/attempts`. Лента этапов (`stages`) есть и у
    закрытого дела: что было — факт, он не перестаёт быть им после закрытия.

    `?spec=<абсолютный путь>` — граф для ленты этапов, если в журнале спека не
    записана. Файл читается `load_spec()` (`yaml.safe_load`), не исполняется;
    процессов роут не открывает ни одного.
    """
    home = _hermes_home()
    if deals.is_own(task_id):
        return _own_deal(home, board, task_id, spec=spec)
    entry = _board_entry(home, board)
    rows, err = _query_checked(Path(entry["path"]), "SELECT * FROM tasks WHERE id = ?", (task_id,))
    if err:
        raise HTTPException(
            status_code=503, headers=NO_STORE_HEADERS, detail=f"board '{board}' unreadable: {err}"
        )
    if not rows:
        raise HTTPException(
            status_code=404,
            headers=NO_STORE_HEADERS,
            detail=f"task '{task_id}' not found on board '{board}'",
        )
    task = rows[0]
    now = _now_sec()
    journal_events, journal_note, _ = _journal_context(home, [])
    events = journal_events.get(task_id, [])
    shaped: Optional[dict[str, Any]] = None
    if str(task.get("status") or "") not in TERMINAL_STATUSES:
        questions, pool_note, _ = _pool_context(home)
        collected = _board_deals(entry, now, questions, pool_note, journal_events, journal_note, home)
        shaped = next((item for item in collected["deals"] if item["id"] == task_id), None)
    feed = _stages_block(events, now, spec)
    return _deal_response(
        now=now,
        board=board,
        deal_id=task_id,
        row=task,
        shaped=shaped,
        # Комментарии и вложения у наблюдаемого дела — не наши: store их не ведёт,
        # и пустой список здесь означает «duty их не держит», а не «их нет».
        comments=[],
        attachments=[],
        timeline=_timeline(events, now),
        stages=feed["stages"],
        stages_note=feed["note"],
        spec=feed["spec"],
        closed=journal.closed_fact(events),
    )


def _resolve_deal(home: Path, board: str, deal_id: str) -> dict[str, Any]:
    """Дело по id для роутов: своё (store) или kanban — по префиксу.

    Одно место, где решается «где искать дело»: `d_` живёт в store, `t_` — на
    доске kanban, и один id не ищется в двух правдах. Возвращает ровно те факты,
    которых хватает вопросу и таймлайну (`id`, `board`, `status`, `title`, `own`);
    404/503 бросаются здесь же, чтобы у всех роутов была одна формулировка отказа.
    """
    if deals.is_own(deal_id):
        try:
            conn = deals.open_store(home, create=False, readonly=True)
        except Exception as exc:  # noqa: BLE001 - свой store не открылся
            raise HTTPException(
                status_code=503,
                headers=NO_STORE_HEADERS,
                detail=f"store unreadable: {type(exc).__name__}: {exc}",
            ) from exc
        if conn is None:
            raise HTTPException(
                status_code=404,
                headers=NO_STORE_HEADERS,
                detail=f"deal '{deal_id}' not found: своего store ещё нет",
            )
        try:
            row = deals.deal_row(conn, deal_id) if deals.has_store(conn) else None
        finally:
            conn.close()
        if row is None or str(row["board"]) != board:
            raise HTTPException(
                status_code=404,
                headers=NO_STORE_HEADERS,
                detail=f"deal '{deal_id}' not found on board '{board}'",
            )
        return {
            "id": deal_id,
            "board": board,
            "status": str(row["status"]),
            "title": row.get("title"),
            "own": True,
        }
    entry = _board_entry(home, board)
    rows, err = _query_checked(
        Path(entry["path"]), "SELECT id, title, status FROM tasks WHERE id = ?", (deal_id,)
    )
    if err:
        raise HTTPException(
            status_code=503, headers=NO_STORE_HEADERS, detail=f"board '{board}' unreadable: {err}"
        )
    if not rows:
        raise HTTPException(
            status_code=404,
            headers=NO_STORE_HEADERS,
            detail=f"task '{deal_id}' not found on board '{board}'",
        )
    return {
        "id": deal_id,
        "board": board,
        "status": str(rows[0].get("status") or ""),
        "title": rows[0].get("title"),
        "own": False,
    }


def _own_deal(home: Path, board: str, deal_id: str, *, spec: Optional[str] = None) -> dict[str, Any]:
    """Своё дело по id: строка store плюс журнал; 404, если его нет или не та доска."""
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 - свой store не открылся
        raise HTTPException(
            status_code=503,
            headers=NO_STORE_HEADERS,
            detail=f"store unreadable: {type(exc).__name__}: {exc}",
        ) from exc
    if conn is None:
        raise HTTPException(
            status_code=404,
            headers=NO_STORE_HEADERS,
            detail=f"deal '{deal_id}' not found: своего store ещё нет",
        )
    try:
        row = deals.deal_row(conn, deal_id) if deals.has_store(conn) else None
        if row is None or str(row["board"]) != board:
            raise HTTPException(
                status_code=404,
                headers=NO_STORE_HEADERS,
                detail=f"deal '{deal_id}' not found on board '{board}'",
            )
        questions, pool_note, _ = _pool_context(home)
        _, journal_note, _ = _journal_context(home, [])
        return _own_response(
            conn,
            row,
            _now_sec(),
            questions=questions,
            pool_note=pool_note,
            journal_note=journal_note,
            spec=spec,
            home=home,
        )
    finally:
        conn.close()


# ------------------------------------------------- мутации своих дел (W5)
#
# Роуты и CLI ходят в **одни** функции (`deals_*` ниже), а те — в
# `dashboard/deals.py`: второго набора правил быть не должно (`docs/OWNERSHIP.md`,
# «CLI и API ходят в один модуль (как W3)»). Ошибка входа — `errors[]` с причиной
# и 400/404, а не 500: битый вход это ответ, а не падение.


def _known_boards(home: Path) -> list[str]:
    """Слаги kanban-досок: своя доска должна быть здесь или уже в store.

    Так опечатка в слаге не заводит тихую доску-призрак: `POST /deals` с чужим
    слагом отвечает ошибкой, а не создаёт новое пространство имён.
    """
    return [str(entry["slug"]) for entry in _discover_boards(home)]


def _store_for_write(home: Path) -> sqlite3.Connection:
    """Соединение store для мутации: 503, если файл не открывается."""
    try:
        conn = deals.open_store(home)
    except Exception as exc:  # noqa: BLE001 - свой store не открылся
        raise HTTPException(
            status_code=503,
            headers=NO_STORE_HEADERS,
            detail=f"store unreadable: {type(exc).__name__}: {exc}",
        ) from exc
    if conn is None:  # pragma: no cover - create=True всегда даёт соединение
        raise HTTPException(
            status_code=503, headers=NO_STORE_HEADERS, detail="store unreadable: no connection"
        )
    return conn


def _error_body(board: Optional[str], deal_id: Optional[str], reason: str) -> dict[str, Any]:
    """Тело ошибки мутации — та же форма, что у успеха, но `deal: null`.

    Клиент не должен разбирать второй тип тела ради отказа: ключи те же, причина
    в `errors[]` (как у остальных ошибок 0.0.2), дело не притворяется живым.
    """
    return {
        "now": _now_sec(),
        "board": board,
        "id": deal_id,
        "title": None,
        "status": None,
        "assignee": None,
        "terminal": False,
        "closed": None,
        "created_at": None,
        "body": "",
        "body_truncated": False,
        "deal": None,
        "comments": [],
        "attachments": [],
        "timeline": [],
        "errors": [{"board": board or DEALS_SOURCE, "reason": reason}],
    }


def _deal_error(exc: RuntimeError, board: Optional[str], deal_id: Optional[str]) -> JSONResponse:
    """Отвергнутая мутация: 404 для «дела нет», 400 для «вход не тот», не 500.

    Тип в подписи — `RuntimeError` (`deals.DealError` — его наследник): модуль
    своего store загружается по пути, и как аннотация он не тип для проверяльщика,
    а поведение от этого не меняется.
    """
    status = 404 if isinstance(exc, deals.DealNotFound) else 400
    return JSONResponse(
        status_code=status,
        headers=NO_STORE_HEADERS,
        content=_error_body(board, deal_id, str(exc)),
    )


def _mutation(conn: sqlite3.Connection, home: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Ответ мутации: та же форма, что `/deal`, плюс блок `mutation`.

    `mutation.events` — сколько строк журнала легло этим вызовом, `idempotent` —
    что вызов повторил уже сделанное (0 новых событий, store не тронут). Оба
    факта в ответе, потому что «сделано» и «уже было» — разные ответы, и CLI не
    должен угадывать их по коду возврата.

    У `create` рядом стоят `created` и, когда дело нашлось по внешнему имени,
    `existing: true` с причиной в `reason`: «нового дела нет, вернули живое» —
    это ответ, а не ошибка, и скрипт обязан отличить его от создания.
    """
    questions, pool_note, _ = _pool_context(home)
    _, journal_note, _ = _journal_context(home, [])
    body = _own_response(
        conn,
        result["deal"],
        _now_sec(),
        questions=questions,
        pool_note=pool_note,
        journal_note=journal_note,
        home=home,
    )
    body["mutation"] = {"events": result["events"], "idempotent": result["idempotent"]}
    if "attachment" in result:
        body["attachment"] = result["attachment"]
    if "created" in result:
        body["created"] = bool(result["created"])
        if not result["created"]:
            body["existing"] = True
            body["reason"] = str(result.get("reason") or "")
    return body


def deals_create(home: Path, payload: dict[str, Any], *, source: str = "api") -> dict[str, Any]:
    """`POST /deals` и `duty create`: одно правило на оба входа."""
    parents = payload.get("parents") or []
    if not isinstance(parents, list) or not all(isinstance(item, str) for item in parents):
        raise deals.DealError("create: 'parents' — список id своих дел (d_<hex>)")
    assignee = payload.get("assignee")
    if assignee is not None and not isinstance(assignee, str):
        raise deals.DealError("create: 'assignee' — строка (профиль-роль) или null")
    body = payload.get("body")
    if body is not None and not isinstance(body, str):
        raise deals.DealError("create: 'body' — строка")
    spec = payload.get("spec")
    if spec is not None and not isinstance(spec, str):
        raise deals.DealError("create: 'spec' — абсолютный путь к спеке (duty.yaml) или null")
    external_ref = payload.get("external_ref")
    if external_ref is not None and not isinstance(external_ref, str):
        raise deals.DealError("create: 'external_ref' — строка (например jira:RP-14080) или null")
    dedup_window = payload.get("dedup_window")
    if dedup_window is not None and (not isinstance(dedup_window, int) or isinstance(dedup_window, bool)):
        raise deals.DealError("create: 'dedup_window' — целое число секунд или null")
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.create_deal(
                conn,
                board=str(payload.get("board") or ""),
                title=str(payload.get("title") or ""),
                body=str(body or ""),
                assignee=assignee,
                parents=parents,
                known_boards=_known_boards(home),
                source=source,
                spec_path=spec,
                external_ref=external_ref,
                dedup_window=dedup_window,
            ),
        )
    finally:
        conn.close()


def deals_patch(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`PATCH /deals/{id}`: title/body/assignee и `status='archived'`."""
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.patch_deal(
                conn,
                deal_id,
                title=payload.get("title"),
                body=payload.get("body"),
                assignee=payload.get("assignee"),
                status=payload.get("status"),
                source=source,
            ),
        )
    finally:
        conn.close()


def deals_comment(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/comment`: комментарий человека (автор по умолчанию `max`)."""
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.add_comment(
                conn,
                deal_id,
                author=str(payload.get("author") or "max"),
                body=str(payload.get("body") or ""),
                source=source,
            ),
        )
    finally:
        conn.close()


def deals_block(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/block`: блокировка с типизированной причиной."""
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.set_blocked(
                conn,
                deal_id,
                kind=str(payload.get("kind") or "needs_input"),
                reason=str(payload.get("reason") or ""),
                author=str(payload.get("author") or "max"),
                source=source,
            ),
        )
    finally:
        conn.close()


def deals_unblock(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/unblock`: снять блок и вернуть дело в очередь."""
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.set_unblocked(
                conn, deal_id, author=str(payload.get("author") or "max"), source=source
            ),
        )
    finally:
        conn.close()


def deals_claim(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/claim`: взять дело в работу (аренды в 0.0.3 нет)."""
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.claim_deal(conn, deal_id, owner=str(payload.get("owner") or ""), source=source),
        )
    finally:
        conn.close()


def deals_complete(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/complete`: закрыть дело или сдать в ревью (`review: true`)."""
    review = payload.get("review")
    if review is not None and not isinstance(review, bool):
        raise deals.DealError("complete: 'review' — true/false")
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.complete_deal(
                conn,
                deal_id,
                summary=str(payload.get("summary") or ""),
                review=bool(review),
                reviewer=payload.get("reviewer"),
                source=source,
            ),
        )
    finally:
        conn.close()


def deals_review(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/review`: сдать дело в ревью."""
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.request_review(
                conn,
                deal_id,
                reviewer=payload.get("reviewer"),
                summary=str(payload.get("summary") or ""),
                source=source,
            ),
        )
    finally:
        conn.close()


def deals_changes(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/changes`: вернуть дело на доработку с причиной."""
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.request_changes(
                conn,
                deal_id,
                reason=str(payload.get("reason") or ""),
                reviewer=payload.get("reviewer"),
                source=source,
            ),
        )
    finally:
        conn.close()


def deals_decide(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/decide`: одно решение человека — `continue` или `stop`.

    `stop` без `reason` закрывает дело словами самого человека: ответ пула — его
    текст, и подставлять вместо него «решено остановить» значило бы переписать
    ответ. Пул только читается (`ask.answer_of`); решения по тексту ответа никто
    не выводит — оно приходит выбранным словом (`docs/RUNBOOK.md`).
    """
    conn = _store_for_write(home)
    try:
        decision = str(payload.get("decision") or "")
        reason = str(payload.get("reason") or "")
        if decision.strip().lower() == deals.DECISION_STOP and not reason.strip():
            reason = _parked_answer(home, conn, deal_id) or ""
        return _mutation(
            conn,
            home,
            deals.decide_deal(
                conn,
                deal_id,
                decision=decision,
                reason=reason,
                author=str(payload.get("author") or "max"),
                source=source,
            ),
        )
    finally:
        conn.close()


def deals_close(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`POST /deals/{id}/close`: закрыть дело решением человека (`completed`).

    Одна транзакция: событие `completed` с `cause='closed_by_human'`, причиной
    человека и состоянием ожидания до закрытия — и строка дела.
    """
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.close_deal(
                conn, deal_id, reason=str(payload.get("reason") or ""), source=source
            ),
        )
    finally:
        conn.close()


def _parked_answer(home: Path, conn: sqlite3.Connection, deal_id: str) -> Optional[str]:
    """Текст ответа человека на вопрос открытой парковки — только чтение пула.

    `decide stop` без причины закрывает дело словами человека, которые он уже
    сказал в ответе: второй раз спрашивать нечего. Нет парковки, нет вопроса или
    пул не прочитался — `None`, и `close` честно запишет пустую причину.
    """
    parked = journal.parked_of(journal.read_events(conn, deal_id)) or {}
    question_id = parked.get("question_id")
    if question_id is None:
        return None
    return ask.answer_of(home, question_id)


def deals_attach(
    home: Path,
    deal_id: str,
    *,
    filename: str,
    content: bytes,
    content_type: Optional[str] = None,
    source: str = "api",
) -> dict[str, Any]:
    """`POST /deals/{id}/attach`: метаданные вложения плюс факт `attached`."""
    conn = _store_for_write(home)
    try:
        return _mutation(
            conn,
            home,
            deals.attach_file(
                conn,
                home,
                deal_id,
                filename=filename,
                content=content,
                content_type=content_type,
                source=source,
            ),
        )
    finally:
        conn.close()


@router.post("/deals")
def create_deal_route(payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals {board, title, body?, assignee?, parents?[], spec?, external_ref?}` → дело.

    Отвечает той же формой, что `/deal/{board}/{id}`, плюс блок `mutation`. Ошибка
    входа (нет доски, нет родителя, пустой title) — 400 с `errors[]`: контракт
    прямо запрещает 500 на битом входе.

    `external_ref` — внешнее имя дела: если живое дело с этим именем на этой доске
    уже есть, ответ несёт `created: false` и `existing: true` с причиной в
    `reason`, а второе дело не заводится (`docs/OWNERSHIP.md`).
    """
    try:
        return deals_create(_hermes_home(), payload)
    except deals.DealError as exc:
        return _deal_error(exc, payload.get("board"), None)


@router.patch("/deals/{deal_id}")
def patch_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`PATCH /deals/{id} {title?, body?, assignee?, status?}` → дело."""
    try:
        return deals_patch(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/comment")
def comment_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/comment {author, body}` → дело с комментарием в таймлайне."""
    try:
        return deals_comment(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/block")
def block_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/block {kind, reason}` → дело в `blocked`."""
    try:
        return deals_block(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/unblock")
def unblock_deal_route(deal_id: str, payload: dict[str, Any] = Body(default={})) -> Any:
    """`POST /deals/{id}/unblock {}` → дело снова в очереди."""
    try:
        return deals_unblock(_hermes_home(), deal_id, payload or {})
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/decide")
def decide_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/decide {decision: continue|stop, reason?}` → факт решения.

    `continue` — тот же факт, что `unblock`; `stop` — `completed` с
    `cause='stopped_by_human'`. Оба идемпотентны: повтор не пишет второй факт.
    """
    try:
        return deals_decide(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/close")
def close_deal_route(deal_id: str, payload: dict[str, Any] = Body(default={})) -> Any:
    """`POST /deals/{id}/close {reason}` → дело закрыто решением человека."""
    try:
        return deals_close(_hermes_home(), deal_id, payload or {})
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/claim")
def claim_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/claim {owner}` → дело `running` с названным владельцем."""
    try:
        return deals_claim(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/complete")
def complete_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/complete {summary, review?}` → дело закрыто или в ревью."""
    try:
        return deals_complete(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/review")
def review_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/review {reviewer?, summary?}` → `status='review'`."""
    try:
        return deals_review(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/changes")
def changes_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/changes {reason, reviewer?}` → возврат на доработку."""
    try:
        return deals_changes(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


@router.post("/deals/{deal_id}/attach")
def attach_deal_route(deal_id: str, file: UploadFile = File(...)) -> Any:
    """`POST /deals/{id}/attach` (multipart `file`) → дело плюс `attachment`.

    Файл читается целиком: предел вложения (`deals.ATTACH_LIMIT_BYTES`) назван
    числом, и превышение — 400 с причиной, а не тихо урезанный файл.
    """
    try:
        content = file.file.read()
        return deals_attach(
            _hermes_home(),
            deal_id,
            filename=file.filename or "",
            content=content,
            content_type=file.content_type,
        )
    except deals.DealError as exc:
        return _deal_error(exc, None, deal_id)


def _board_entry(home: Path, slug: str) -> dict[str, Any]:
    """Запись доски по слагу или HTTP 404."""
    match = [b for b in _discover_boards(home) if b["slug"] == slug]
    if not match:
        raise HTTPException(status_code=404, headers=NO_STORE_HEADERS, detail=f"board '{slug}' not found")
    return match[0]


@router.get("/now")
def now(board: Optional[str] = None, state: Optional[str] = None, ingest: bool = False) -> dict[str, Any]:
    """Активные дела всех досок: лизинг, попытка, последнее действие, ожидание.

    `?board=<slug>` — только одна доска (404, если её нет);
    `?state=<waiting.state>` — только дела в этом состоянии ожидания;
    `?ingest=1` — сначала прогнать ingest в свой журнал (тот же проход, что
    `POST /ingest`, только без отчёта). Без флага срез ничего не пишет: журнал
    открывается на чтение и файла не создаёт.
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
    return collect_now(home, _now_sec(), board=board, state=state, ingest=ingest)


@router.post("/ingest")
def ingest_now() -> dict[str, Any]:
    """Проход ingest по требованию: дописать в свой журнал недостающие события.

    Единственный роут (вместе с `POST /ask`), который пишет, и пишет он только в
    `duty/duty.db`. Источники (`kanban.db`, `state.db`, `pool.db`) открываются
    `mode=ro` — инвариант «в kanban не пишем» проверяется тестом по `mtime`.

    Повторный вызов — no-op: `appended: 0` (`INSERT OR IGNORE` по
    `idempotency_key`). Ответ — отчёт прохода: сколько строк добавлено, по каким
    доскам, что не прочиталось (`errors[]`). Тот же проход доступен как
    `GET /now?ingest=1`.

    Там же дочитываются ответы пула (`ask.sync_answers`, блок `answers`):
    вопрос, на который уже ответили, перестаёт быть живым фактом ожидания.
    """
    home = _hermes_home()
    try:
        conn = journal.open_journal(home)
    except Exception as exc:  # noqa: BLE001 - свой журнал не открылся
        raise HTTPException(
            status_code=503,
            headers=NO_STORE_HEADERS,
            detail=f"journal unreadable: {type(exc).__name__}: {exc}",
        ) from exc
    try:
        report = journal.ingest(conn, home, _discover_boards(home))
        report["answers"] = ask.sync_answers(conn, home)
        return report
    finally:
        conn.close()


@router.post("/ask/{board}/{task_id}")
def ask_deal(board: str, task_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """Явный канал вопроса по делу: пул `messaging/pool.db` плюс событие журнала.

    Кто и когда зовёт: человек или координатор (из UI/CLI), когда дело стоит в
    `blocked`/`needs_input` **без** живого вопроса — тогда `/now` честно говорит
    `waiting.human_kind="status"`, а этот роут превращает «нужен человек» в
    конкретный вопрос со вариантами ответа. Автоматически ingest вопросов не
    создаёт: пул решений не должен наполняться проходами по доске.

    Тело: `{"text": "...", "text_full": "...", "options": ["..."], "critical": true}`
    (`text_full`, `options` и `critical` необязательны; `critical` по умолчанию
    решается по журналу — дело ждёт человека дольше `ASK_CRITICAL_SEC`).

    Ответ: `{"question_id": N, "created": bool, "deduped": bool, "adopted": bool, "errors": []}`.
    Живой вопрос по делу возвращается как есть (`deduped: true`), второй строки в
    пуле нет. `adopted: true` — подхвачен живой вопрос **чужого** продюсера
    (`origin != 'duty'`): id возвращён, но `question_asked` по нему не пишется,
    потому что снять этот факт нечем (ответ на чужой вопрос журналу не виден).
    Сбой пула — 503 с `errors[]`, где назван пул и дело: событие в журнал при
    этом не пишется (`question_asked` только после успешной вставки).
    """
    home = _hermes_home()
    _resolve_deal(home, board, task_id)

    text = " ".join(str(payload.get("text") or "").split())
    if not text:
        raise HTTPException(
            status_code=400, headers=NO_STORE_HEADERS, detail="ask: поле 'text' обязательно"
        )
    options = payload.get("options")
    if options is not None and (
        not isinstance(options, list) or not all(isinstance(option, str) for option in options)
    ):
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail="ask: 'options' — список простых строк (не объектов)",
        )
    critical = payload.get("critical")
    if critical is not None and not isinstance(critical, bool):
        raise HTTPException(
            status_code=400, headers=NO_STORE_HEADERS, detail="ask: 'critical' — true/false"
        )

    deal = {"id": task_id, "board": board, "home": home}
    try:
        conn = journal.open_journal(home)
    except Exception as exc:  # noqa: BLE001 - свой журнал не открылся
        raise HTTPException(
            status_code=503,
            headers=NO_STORE_HEADERS,
            detail=f"journal unreadable: {type(exc).__name__}: {exc}",
        ) from exc
    try:
        result = ask.ask(
            conn,
            deal,
            text,
            payload.get("text_full"),
            options=options,
            critical=critical,
        )
    except ask.AskError as exc:
        return JSONResponse(
            status_code=503,
            headers=NO_STORE_HEADERS,
            content={
                "question_id": None,
                "created": False,
                "deduped": False,
                "adopted": False,
                "errors": [{"board": ask.QUESTION_SOURCE, "reason": f"ask {task_id}: {exc}"}],
            },
        )
    finally:
        conn.close()
    return {
        "question_id": result.question_id,
        "created": result.created,
        "deduped": not result.created,
        "adopted": result.adopted,
        "errors": [],
    }


@router.get("/duty/{board}/{task_id}/attempts")
def duty_attempts(board: str, task_id: str) -> dict[str, Any]:
    """Таймлайн попыток дела: все попытки по возрастанию старта плюс их события.

    У своего дела (`d_…`) попытки строятся по журналу: в 0.0.4 движок спавнит
    процесс и пишет `attempt_started`/`attempt_progress`/`attempt_ended`, а
    живость незакрытой попытки берётся из лизинга — без `ps` и `/proc`
    (`docs/PROBES.md`). Форма у обоих пространств одна: этап, номер, начало,
    конец, длительность, исход, класс отказа, причина, `base`, `commit`, `pid`,
    `reaped`, число событий прогресса — чтобы консоль и страница рисовали одну
    форму, а не две.

    Пустой список — законный ответ: дело существует, и «попыток не было» — это
    факт о нём, а не отсутствие дела. `note` в этом случае называет настоящую
    причину; у наблюдаемого kanban-дела `note` пуст, потому что его попытки
    читаются из `task_runs` доски.
    """
    home = _hermes_home()
    if deals.is_own(task_id):
        return _own_attempts(home, board, task_id)
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
        # Пометка о пустом ответе — в одной форме у обоих пространств: у
        # kanban-дела попытки читаются из `task_runs`, и объяснять нечего.
        "note": "",
    }


# Причина пустого таймлайна своего дела: попыток правда не было. Прежняя
# формулировка («спавна в 0.0.3 нет») после W11/W12 стала ложью — движок спавнит
# попытки и пишет их в журнал, — и врать про отсутствие механизма нельзя.
OWN_NO_ATTEMPTS = "попыток не было: в журнале дела нет ни одного attempt_started"

# Подсказка к осиротевшей попытке: её закроет уборка, а не человек руками.
ORPHAN_HINT = "попытку закроет уборка: bin/duty reap"


def _own_attempt(row: dict[str, Any], now: int) -> dict[str, Any]:
    """Попытка своего дела в форме таймлайна: те же имена, что у kanban-дела.

    Источник — журнал и лизинг (`stagefeed.attempts`): ни `ps`, ни `/proc`.
    Незакрытая попытка называется честно: лизинг жив — `running` (а
    `duration_sec` у неё — возраст, как у незакрытого рана kanban), лизинг истёк —
    `orphan` с подсказкой про `bin/duty reap`. Срока лизинга в журнале нет —
    `unknown`: «жив» и «мёртв» здесь были бы догадкой.

    `start`/`end` — те же два факта, что `started_at`/`ended_at`: консоль и веб
    читают одно имя (`desktop/model.js`), спека и `stagefeed` — другое, а
    переименовывать одно ради другого значило бы держать две формы таймлайна.
    `error` — то же слово, что `reason`: отдельного поля ошибки у своей попытки
    нет, а страница печатает причину отказа именно оттуда.
    """
    ended_at = row["finished_at"]
    duration = None
    if row["started_at"] is not None:
        stop = int(ended_at if ended_at is not None else now)
        duration = max(0, stop - int(row["started_at"]))
    status = "unknown"
    reason = row["reason"]
    if ended_at is not None:
        status = "completed" if row["outcome"] in SUCCESS_OUTCOMES else "failed"
    elif row["live"]:
        status = "running"
    elif row["dead"]:
        status = "orphan"
        expired = int(now) - int(row["lease_expires"] or now)
        reason = f"лизинг истёк {max(0, expired)}с назад — {ORPHAN_HINT}"
    else:
        reason = "срок лизинга в журнале не записан: жив ли процесс — неизвестно"
    return {
        "run_id": row["run_id"],
        "profile": row["role"],
        "stage": row["stage"],
        "attempt": row["n"],
        "status": status,
        "start": row["started_at"],
        "end": ended_at,
        "started_at": row["started_at"],
        "ended_at": ended_at,
        "duration_sec": duration,
        "outcome": row["outcome"],
        "kind": row["kind"],
        "reason": reason,
        "error": reason,
        "summary": "",
        "base": row["base"],
        "commit": row["commit"],
        "pid": row["pid"],
        "reaped": row["reaped"],
        "progress": row["progress"],
        # Событий доски у своей попытки нет: шаги процесса считаны числом
        # (`progress`), а не подставлены чужими по виду строками.
        "events": [],
    }


def _own_attempts(home: Path, board: str, task_id: str) -> dict[str, Any]:
    """Таймлайн попыток своего дела: журнал плюс таблица лизингов, без `ps`."""
    resolved = _resolve_deal(home, board, task_id)
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 - свой store не открылся
        raise HTTPException(
            status_code=503,
            headers=NO_STORE_HEADERS,
            detail=f"store unreadable: {type(exc).__name__}: {exc}",
        ) from exc
    try:
        events = journal.read_events(conn, task_id)
        # Срок из store важнее срока из `attempt_started`: лизинг продлевается
        # heartbeat'ом (`_own_leases`).
        leases = _own_leases(conn, task_id)
    finally:
        conn.close()
    now = _now_sec()
    attempts = [_own_attempt(row, now) for row in stagefeed.attempts(events, now, leases=leases)]
    return {
        "board": board,
        "task_id": task_id,
        "title": resolved.get("title"),
        "count": len(attempts),
        "crash_outcomes": list(CRASH_OUTCOMES),
        "attempts": attempts,
        "note": "" if attempts else OWN_NO_ATTEMPTS,
    }


# ------------------------------------------------------ веб-версия (0.0.2)
#
# Страница, открываемая в любом браузере: `http://127.0.0.1:<port>/web/hermes-duty/`.
# Живёт НЕ под `/api` (там гейт токена), токен вшивается в HTML, а данные
# страница берёт с `/api/plugins/hermes-duty/*` заголовком `X-Hermes-Session-Token`
# — ровно как веб-версия `hermes-tickets`.
#
# Шейпер данных общий с десктопом: страница импортирует `desktop/model.js`,
# который отдаётся этим же роутом. Второй копии таксономии и форматирования в
# вебе нет — иначе бейджи и доказательства разъезжались бы между поверхностями.

_WEB_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}

# Страница обновляется правкой файлов в репозитории: без запрета кэша браузер
# держит старый app.js и правки «не доезжают» до вкладки.
_WEB_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

_WEB_404 = (
    '<!doctype html><html lang="ru"><body style="font-family:system-ui;padding:40px">'
    "Hermes Duty: страница не найдена в сборке плагина (dashboard/web/ отсутствует)."
    "</body></html>"
)


def _register_web_version() -> None:
    """Смонтировать `/web/hermes-duty/*` и общий шейпер `/web/hermes-duty/model.js`.

    Плагин монтируется in-process (`isolation` не задан), поэтому
    `hermes_cli.web_server.app` доступен отсюда. В host-изоляции роут не
    смонтируется — веб-версии не будет, API-роуты продолжат работать.
    """
    try:
        from fastapi.responses import FileResponse, HTMLResponse
        from hermes_cli.web_server import _SESSION_TOKEN, app as _app
    except Exception:  # noqa: BLE001 - не in-process: веб-версия просто не монтируется
        return

    web_dir = Path(__file__).resolve().parent / "web"
    if not (web_dir / "index.html").is_file():
        return  # веб-фронта нет в сборке — не мешаем API
    model_file = Path(__file__).resolve().parent.parent / "desktop" / "model.js"
    gated = bool(getattr(_app.state, "auth_required", False))
    token_js = "" if gated else f"window.__HERMES_SESSION_TOKEN__={json.dumps(_SESSION_TOKEN)};"

    async def _web_index():  # noqa: ANN202 - сигнатура FastAPI-обработчика
        try:
            html = (web_dir / "index.html").read_text(encoding="utf-8")
        except OSError:
            return HTMLResponse(_WEB_404, status_code=404, headers=_WEB_NO_CACHE)
        html = html.replace(
            "<!--__HERMES_TOKEN__-->", f"<script>{token_js}</script>" if token_js else ""
        )
        return HTMLResponse(html, headers=_WEB_NO_CACHE)

    async def _web_model():  # noqa: ANN202 - шейпер общий с десктопом
        if not model_file.is_file():
            return HTMLResponse(_WEB_404, status_code=404, headers=_WEB_NO_CACHE)
        return FileResponse(
            str(model_file), media_type="text/javascript; charset=utf-8", headers=_WEB_NO_CACHE
        )

    async def _web_static(path: str):  # noqa: ANN202 - статика рядом с index.html
        if not path or ".." in path:
            path = "index.html"
        resolved = os.path.realpath(os.path.join(web_dir, path))
        if not resolved.startswith(os.path.realpath(web_dir) + os.sep) or not os.path.isfile(resolved):
            return HTMLResponse(_WEB_404, status_code=404, headers=_WEB_NO_CACHE)
        return FileResponse(
            resolved,
            media_type=_WEB_MIME.get(os.path.splitext(resolved)[1].lower(), "application/octet-stream"),
            headers=_WEB_NO_CACHE,
        )

    _app.add_api_route("/web/hermes-duty", _web_index, methods=["GET"], include_in_schema=False)
    _app.add_api_route("/web/hermes-duty/", _web_index, methods=["GET"], include_in_schema=False)
    _app.add_api_route("/web/hermes-duty/model.js", _web_model, methods=["GET"], include_in_schema=False)
    _app.add_api_route("/web/hermes-duty/{path:path}", _web_static, methods=["GET"], include_in_schema=False)


_register_web_version()
