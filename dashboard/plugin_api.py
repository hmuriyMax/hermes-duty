"""Hermes Duty — backend desktop-плагина «Дела».

С 0.0.3 состояние дела считается не по колонкам kanban, а по **своему журналу
событий** (`dashboard/journal.py`, контракт `docs/JOURNAL.md`):
`waiting = fold(events)`. Журнал живёт в `$HERMES_HOME/duty/duty.db` и принадлежит
плагину; чужие БД (`kanban.db`, `state.db`, `pool.db`) открываются только
`?mode=ro`, и ни один роут в них не пишет.

С 0.0.6 **доск нет**: дело адресуется своим `d_<hex>`, а группируется по `repo`
строки дела (репозиторий из спеки). Маршруты поэтому односегментные —
`/deal/{deal_id}`, `/duty/{deal_id}/attempts`, — а фильтр группировки называется
`?repo=`. Подбора событий из kanban (`ingest`) больше нет: это был последний мост
к канбану, и вместе с досками он стал не нужен.

Карточки kanban остались **только наблюдением** — `GET /cards`: их статусы и
лизинг, своим ответом и своим маршрутом. На список дел, статистику и id они не
влияют: дела живут в store (`dashboard/deals.py`), и другого источника у них нет.

Вопросы человеку идут одним каналом — `dashboard/ask.py`: `POST /ask/{deal_id}`
пишет вопрос в пул `messaging/pool.db` (`origin='duty'`, `kind='decision'`) и факт
`question_asked` в журнал. Остальной код пул только читает (`mode=ro`).

Маршруты монтируются как `/api/plugins/hermes-duty/<...>`.

Источники (поиск от корня Hermes, без хардкода путей):

- `duty/duty.db` — свой store дел и журнал (единственный источник дел и `/stats`);
- `messaging/pool.db` — пул вопросов (для таксономии ожиданий, только чтение);
- `profiles/<role>/state.db` — сессии, из которых берётся «последнее действие»;
- `kanban.db` (+ `kanban/boards/<slug>/kanban.db`) — только для `/cards`.

Ядро kanban и его диспетчер не импортируются: P0 не должен зависеть от кода,
который мы собираемся заменить.

Экран «сейчас» (`/now`) и таймлайн попыток (`/duty/{deal_id}/attempts`) считаются
здесь же: состояние ожидания дела выводится fold'ом по событиям журнала, а факты
попытки и лизинга — из журнала и таблицы `leases` своего store.
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
# `dashboard/spec.py` — чтение спеки дела (`load_spec`): из неё берётся `repo`.
SPEC_MODULE_NAME = "hermes_duty_spec"
# `dashboard/stagefeed.py` — лента этапов и попыток дела (`GET /deal`, `GET /now`).
STAGEFEED_MODULE_NAME = "hermes_duty_stagefeed"
# `dashboard/budget.py` — расход дела по журналу против верхнего `budget` спеки.
BUDGET_MODULE_NAME = "hermes_duty_budget"
# `dashboard/publish.py` — факт публикации дела по журналу (`published`).
PUBLISH_MODULE_NAME = "hermes_duty_publish"
FAILURES_MODULE_NAME = "hermes_duty_failures"


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


def _load_spec() -> Any:
    """Загрузить `dashboard/spec.py` — чтение спеки дела (граф этапов и `repo`).

    Отдельной загрузкой по пути, как и остальные модули рядом: плагин грузит
    `plugin_api.py` как файл, и обычный `import spec` попал бы не в тот пакет.
    Нужен здесь ради одного факта — `repo` из спеки при создании дела; граф
    этапов читает `stagefeed.py` сам.
    """
    loaded = sys.modules.get(SPEC_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "spec.py"
    spec = importlib.util.spec_from_file_location(SPEC_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"spec module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[SPEC_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


spec_mod = _load_spec()


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


def _load_budget() -> Any:
    """Загрузить `dashboard/budget.py` — расход дела и потолок `budget` спеки."""
    loaded = sys.modules.get(BUDGET_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "budget.py"
    spec = importlib.util.spec_from_file_location(BUDGET_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"budget module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[BUDGET_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


budget = _load_budget()


def _load_failures() -> Any:
    """Загрузить `dashboard/failures.py` — словарь классов отказа и их имена."""
    loaded = sys.modules.get(FAILURES_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "failures.py"
    spec = importlib.util.spec_from_file_location(FAILURES_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"failures module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[FAILURES_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


failures = _load_failures()


def _load_publish() -> Any:
    """Загрузить `dashboard/publish.py` — факт публикации дела по журналу."""
    loaded = sys.modules.get(PUBLISH_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "publish.py"
    spec = importlib.util.spec_from_file_location(PUBLISH_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"publish module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PUBLISH_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish()


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


def _plugin_version() -> str:
    """Номер версии — один источник (plugin.yaml), а не четвёртая копия в коде."""
    meta = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        for line in meta.read_text().splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return "0.0.0"


VERSION = _plugin_version()


def _hermes_home() -> Path:
    """Корень Hermes: `$HERMES_HOME`, иначе `~/.hermes`.

    Одна точка на все роуты и на CLI (`hermes_home` ниже): иначе «где живёт
    store» и «где живут источники» разъехались бы между терминалом и экраном.
    """
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def hermes_home() -> Path:
    """Корень Hermes — та же точка, что у роутов (её зовёт CLI)."""
    return _hermes_home()


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

    Отдельно от `_query_checked`, потому что наблюдение делает несколько запросов
    по одному соединению, а не открывает новое на каждый.
    """
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()], None
    except Exception as exc:  # noqa: BLE001 - битый файл/нет таблицы/нет прав
        return [], f"{type(exc).__name__}: {exc}"


def _query_checked(
    path: Path, sql: str, params: tuple = ()
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Один read-only запрос: `(строки, причина сбоя)`, без исключений наружу."""
    try:
        with closing(_connect_ro(path)) as conn:
            return _fetch(conn, sql, params)
    except Exception as exc:  # noqa: BLE001 - файл не открылся
        return [], f"{type(exc).__name__}: {exc}"


def _query(path: Path, sql: str) -> list[dict[str, Any]]:
    """Один read-only запрос; на любой сбой возвращает пустой список, а не падает."""
    return _query_checked(path, sql)[0]


# Карточки kanban (наблюдение, не дела). Корневая БД лежит не в `kanban/boards/<slug>/`,
# а прямо в корне Hermes; остальные — по каталогам-слагам. Список источников
# нужен только наблюдению `/cards`: ни список дел, ни статистика, ни id из него
# больше не собираются (0.0.6: доск в duty нет).
CARD_DB_NAME = "kanban.db"
CARDS_DIR = ("kanban", "boards")
CARDS_ARCHIVED = "_archived"
# Имя корневой kanban-БД в наблюдении: «доска default» — раскладка Hermes, а не
# наша сущность, и в ответе она подписана файлом, а не слагом.
CARDS_ROOT_NAME = "default"
# Подпись наблюдения — одна на все поверхности: карточки не дела.
CARDS_LABEL = "карточки kanban (наблюдение, не дела)"
# Сколько карточек отдаёт наблюдение: предел назван числом, а не «как получится».
CARD_LIMIT = 200


def _pool_summary(home: Path) -> Optional[dict[str, Any]]:
    """Пул вопросов: что в нём лежит, по происхождению и статусу (только чтение)."""
    db = home / "messaging" / "pool.db"
    if not db.is_file():
        return None
    rows = _query(db, "SELECT origin, status, COUNT(*) AS n FROM questions GROUP BY origin, status")
    return {
        "path": str(db),
        "by_origin_status": rows,
        "total": sum(int(r.get("n") or 0) for r in rows),
    }


def _retired(board_dir: Path) -> bool:
    """Выбывший каталог доски: архив или камень `board.json: archived`.

    Ядро kanban убирает закрытые доски в `kanban/boards/_archived/` и ставит в
    каталог могильный камень `board.json` с `"archived": true`. Наблюдение архив
    не показывает: его карточки — история, а не «что сейчас».
    """
    if board_dir.name == CARDS_ARCHIVED or board_dir.parent.name == CARDS_ARCHIVED:
        return True
    tombstone = board_dir / "board.json"
    if not tombstone.is_file():
        return False
    try:
        raw = json.loads(tombstone.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(raw, dict) and bool(raw.get("archived"))


def _card_databases(home: Path) -> list[tuple[str, Path]]:
    """Живые kanban-БД для наблюдения: `(имя, путь)`; выбывшие не читаются."""
    found: list[tuple[str, Path]] = []
    root = home / CARD_DB_NAME
    if root.is_file():
        found.append((CARDS_ROOT_NAME, root))
    boards_dir = home.joinpath(*CARDS_DIR)
    if not boards_dir.is_dir():
        return found
    for child in sorted(boards_dir.iterdir()):
        database = child / CARD_DB_NAME
        if not child.is_dir() or not database.is_file() or _retired(child):
            continue
        found.append((child.name, database))
    return found


def _card_row(task: dict[str, Any], run: Optional[dict[str, Any]], now: int) -> dict[str, Any]:
    """Карточка наблюдения: статус, лизинг и последний ран — без состояний дел.

    Наблюдение не выводит `waiting` и не считает таксономию: состояние ожидания —
    свойство дела (`fold` журнала), а у карточки его нет. Поэтому здесь только
    факты строки kanban, названные своими именами.
    """
    expires = run.get("lease_expires") if run is not None else None
    lease: Optional[dict[str, Any]] = None
    if expires is not None:
        lease = {
            "holder": run.get("profile") or task.get("assignee"),
            "expires_at": int(expires),
            "expired": int(expires) <= int(now),
            "sec_left": int(expires) - int(now),
        }
    return {
        "id": str(task.get("id")),
        "title": task.get("title"),
        "status": task.get("status"),
        "assignee": task.get("assignee"),
        "block_kind": task.get("block_kind"),
        "consecutive_failures": int(task.get("consecutive_failures") or 0),
        "run_id": None if run is None else run.get("id"),
        "run_status": None if run is None else run.get("status"),
        "lease": lease,
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
    }


def _cards_of(name: str, database: Path, now: int) -> dict[str, Any]:
    """Карточки одной kanban-БД: активные плюс лизинг их ранов."""
    out: dict[str, Any] = {"name": name, "path": str(database), "read": True, "cards": []}
    if database.stat().st_size == 0:
        out.update({"read": False, "note": "empty db"})
        return out
    with closing(_connect_ro(database)) as conn:
        tasks, err = _fetch(
            conn,
            "SELECT id, title, assignee, status, block_kind, consecutive_failures, "
            "current_run_id, created_at, started_at, completed_at FROM tasks "
            "WHERE status NOT IN (?, ?) ORDER BY created_at, id LIMIT ?",
            (TERMINAL_STATUSES[0], TERMINAL_STATUSES[1], CARD_LIMIT),
        )
        if err:
            out.update({"read": False, "note": err})
            return out
        cards = [str(task.get("id")) for task in tasks]
        runs: dict[str, dict[str, Any]] = {}
        if cards:
            marks = ", ".join("?" * len(cards))
            rows, err = _fetch(
                conn,
                f"SELECT * FROM task_runs WHERE task_id IN ({marks}) ORDER BY started_at, id",
                tuple(cards),
            )
            if err:
                out["warnings"] = [f"task_runs: {err}"]
            for row in rows:
                task_id = str(row.get("task_id"))
                # Лизинг — у идущего рана; иначе самый свежий, чтобы наблюдение
                # показало, чем карточка кончилась в последний раз.
                if row.get("status") == "running" or task_id not in runs:
                    runs[task_id] = row
    out["cards"] = [_card_row(task, runs.get(str(task.get("id"))), now) for task in tasks]
    out["truncated"] = len(tasks) >= CARD_LIMIT
    return out


@router.get("/cards")
def cards() -> dict[str, Any]:
    """Наблюдение за карточками kanban: статусы, лизинг, последний ран.

    Отдельный ответ и отдельный маршрут — потому что это **не дела**. Ни `/now`,
    ни `/stats`, ни адресация по id сюда не ходят: с 0.0.6 доск в duty нет, а
    карточка kanban не может быть адресом дела (`d_<hex>`).
    """
    home = _hermes_home()
    now = _now_sec()
    found = [_cards_of(name, database, now) for name, database in _card_databases(home)]
    return {
        "now": now,
        "kind": "observation",
        "label": CARDS_LABEL,
        "databases": len(found),
        "count": sum(len(entry.get("cards") or []) for entry in found),
        "cards": found,
    }


@router.get("/health")
def health() -> dict[str, Any]:
    """Живая проверка: плагин смонтирован, store и пул читаются на чтение.

    `journal` — состояние своего журнала: `events` (строк) и `deals` (дел с
    событиями). Расхождения «канбан против журнала» здесь больше нет (0.0.6):
    карточки kanban — отдельное наблюдение (`/cards`), и считать их делами
    значило бы вернуть доски через чёрный ход. `/health` файлов не создаёт и не
    пишет: нет журнала — значит событий 0, и это правда.
    """
    home = _hermes_home()
    journal_block: dict[str, Any] = {"events": 0, "deals": 0}
    notes: list[str] = []
    try:
        conn = _open_journal_ro(home)
    except Exception as exc:  # noqa: BLE001 - свой журнал не открылся
        conn = None
        notes.append(f"journal unreadable: {type(exc).__name__}: {exc}")
    if conn is not None:
        try:
            journal_block.update(journal.counts(conn))
        except Exception as exc:  # noqa: BLE001 - битый журнал
            notes.append(f"journal unreadable: {type(exc).__name__}: {exc}")
        finally:
            conn.close()
    answer: dict[str, Any] = {
        "ok": True,
        "version": VERSION,
        "hermes_home": str(home),
        "store": journal.journal_path(home).is_file(),
        "pool": {"present": (home / "messaging" / "pool.db").is_file()},
        "journal": journal_block,
        "mode": "read-only",
    }
    if notes:
        answer["note"] = "; ".join(notes)
    return answer


@router.get("/sources")
def sources() -> dict[str, Any]:
    """Что плагин видит: store дел, репозитории дел, пул, профили. Ничего не меняет."""
    home = _hermes_home()
    profiles_dir = home / "profiles"
    profiles = (
        sorted(
            p.name for p in profiles_dir.iterdir() if p.is_dir() and (p / "state.db").is_file()
        )
        if profiles_dir.is_dir()
        else []
    )
    return {
        "version": VERSION,
        "hermes_home": str(home),
        "store": {
            "path": str(journal.journal_path(home)),
            "present": journal.journal_path(home).is_file(),
        },
        "repos": _repos_of(home),
        "pool": _pool_summary(home),
        "profiles": profiles,
        # Наблюдение за карточками kanban — отдельный источник, и он назван здесь
        # так, чтобы его нельзя было спутать с делами.
        "cards": {
            "label": CARDS_LABEL,
            "databases": len(_card_databases(home)),
        },
    }


def _repos_of(home: Path) -> dict[str, Any]:
    """Репозитории дел: уникальные непустые `repo` строк store плюс дела без repo.

    Один источник на `/now`, `/stats` и `bin/duty` (через ответ роута): второй
    копии этой группировки быть не должно, иначе терминал и экран показывали бы
    разные числа.
    """
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception:  # noqa: BLE001 - store не читается: скажет `/now` в errors
        return {"list": [], "without_repo": 0, "read": False}
    if conn is None:
        return {"list": [], "without_repo": 0, "read": True}
    try:
        # `has_store` и выборка — часть чтения: битый файл падает и здесь, а не
        # только на открытии, и группировка не имеет права ронять весь срез.
        if not deals.has_store(conn):
            return {"list": [], "without_repo": 0, "read": True}
        rows = deals.deal_rows(conn, open_only=False)
    except Exception:  # noqa: BLE001 - битый store
        return {"list": [], "without_repo": 0, "read": False}
    finally:
        conn.close()
    counts: dict[str, int] = {}
    without = 0
    for row in rows:
        repo = row.get("repo")
        if not repo:
            without += 1
            continue
        counts[str(repo)] = counts.get(str(repo), 0) + 1
    return {
        "list": [{"repo": repo, "deals": counts[repo]} for repo in sorted(counts)],
        "without_repo": without,
        "read": True,
    }



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

# Исходы попытки, которые считаются успехом. Отдельно от статуса попытки: этап
# считается пройденным по исходу, а не по тому, закрылась ли попытка.
SUCCESS_OUTCOMES = ("completed", "done", "succeeded")

# Сколько тела дела отдаём на экран «Дело» (тело карточки — спецификация, в
# ответе она нужна целиком, но не бесконечно).
DEAL_BODY_LIMIT = 20000
# Текст спеки для экрана дела. 20 000 — тот же потолок, что у `body`: спека это
# файл репозитория, и страница не обязана пересказывать его целиком.
SPEC_TEXT_LIMIT = 20000


def _spec_text(path: Optional[str]) -> tuple[str, bool]:
    """Текст спеки как есть: `(текст, обрезан)`.

    У дел из `bin/duty create` свободного `body` в store нет — описание живёт
    комментариями в шапке `duty.yaml`. Без файла экран показывал «тело дела
    пустое» при живой спеке, то есть называл спецификацией другое поле.
    Отсутствующий или нечитаемый файл — пустая строка, а не ошибка: страница
    дела остаётся страницей дела, а причина уже видна в `stages_note`.
    """
    if not path:
        return "", False
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return "", False
    return text[:SPEC_TEXT_LIMIT], len(text) > SPEC_TEXT_LIMIT


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
        errors.append({"source": POOL_SOURCE, "reason": pool_err})
    if pool_truncated:
        reason = f"questions truncated: more than {QUESTION_LIMIT} live questions"
        notes.append(f"живых вопросов в пуле больше {QUESTION_LIMIT} (старые не вошли)")
        errors.append({"source": POOL_SOURCE, "reason": reason})
    pool_note = f"{'; '.join(notes)} — факт wait_human не проверен" if notes else None
    return questions, pool_note, errors


# --------------------------------------------------- журнал событий (0.0.3)
#
# Состояние дела считается fold'ом по событиям своего журнала (контракт
# `docs/JOURNAL.md`). Канбан-фактов здесь больше нет (0.0.6): карточка kanban —
# наблюдение (`/cards`), а не дело, поэтому «журнал пуст» и «живого факта нет» —
# это два состояния самого дела (`no_signal`), и `waiting.evidence` говорит, что
# факта журнала нет: «нет данных» нельзя показывать как «всё спокойно».

# Имя источника в `errors[]`/`evidence`, когда свой журнал не читается. Одна копия
# в `journal.py`: иначе путь к журналу разошёлся бы между модулями.
JOURNAL_SOURCE = journal.JOURNAL_SOURCE


def _journal_context(
    home: Path,
) -> tuple[dict[str, list[dict[str, Any]]], Optional[str], list[dict[str, str]]]:
    """События журнала по делам: `(по делам, пометка, ошибки)`.

    Читающие роуты журнал не пишут и файла не создают (`create=False`): иначе
    «открыл экран» и «включил журнал» стали бы одним действием, а срез отвечал бы
    за запись. Недоступный журнал не валит ответ: причина уходит в `errors`, а
    состояния дел тогда нет — и это сказано в пометке, а не выдано за «дел нет».

    Соединение открывается строго на чтение (`?mode=ro`): журнал живёт в WAL, и
    обычное соединение при закрытии вправе сделать checkpoint — тогда «посмотрел
    срез» меняло бы файл, хотя ни одного события не записано.
    """
    try:
        conn = _open_journal_ro(home)
    except Exception as exc:  # noqa: BLE001 - свой журнал не открылся
        reason = f"{type(exc).__name__}: {exc}"
        return {}, f"journal: недоступен ({reason})", [
            {"source": JOURNAL_SOURCE, "reason": reason}
        ]
    if conn is None:  # журнала ещё нет — это пустой журнал, а не ошибка
        return {}, None, []
    try:
        by_deal: dict[str, list[dict[str, Any]]] = {}
        for event in journal.read_events(conn):
            by_deal.setdefault(str(event["deal_id"]), []).append(event)
        return by_deal, None, []
    except Exception as exc:  # noqa: BLE001 - битый/занятый журнал
        reason = f"{type(exc).__name__}: {exc}"
        return {}, f"journal: недоступен ({reason})", [
            {"source": JOURNAL_SOURCE, "reason": reason}
        ]
    finally:
        conn.close()


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
    """Состояние журнала и живой вопрос пула в одном ответе (до строки решения).

    У вопроса, который журнал уже знает (`human_kind='question'`), рядом
    называются **варианты ответа из пула**: гейт спрашивает человека выбором, и
    «вопрос #N без ответа» без вариантов заставляло бы открывать панель `/imsg`,
    чтобы узнать, из чего выбирать. Текст вариантов — факт пула, поэтому он
    берётся из строки пула, а не из журнала.
    """
    state = folded["state"]
    human_kind = folded["human_kind"]
    if question is None:
        return {"state": state, "evidence": folded["evidence"], "human_kind": human_kind}
    if human_kind == "question":
        return {
            "state": state,
            "evidence": f"{folded['evidence']}; {_question_fact(question)}",
            "human_kind": human_kind,
        }
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


def _question_fact(question: dict[str, Any]) -> str:
    """Живой вопрос пула словами: номер, варианты ответа и где отвечать.

    Варианты — из `options_json` пула (`/imsg` показывает их кнопками). Вопрос без
    вариантов — свободный ответ, и это сказано словами, а не пустым списком.
    """
    options = _question_options(question)
    choices = f"варианты: {', '.join(options)}" if options else "вариантов нет — ответ свободным текстом"
    return f"пул: вопрос #{question.get('id')} (status='{question.get('status')}'), {choices}; ответ — пул /imsg"


def _question_options(question: dict[str, Any]) -> list[str]:
    """Варианты ответа из строки пула: `options_json` — список строк, мусор — пусто."""
    raw = question.get("options_json")
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, (str, int, float))]


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
# Пул вопросов — отдельный источник: его сбой не делает дела нечитаемыми, и
# `errors[]` называет именно тот файл, который не открылся.
POOL_SOURCE = "messaging/pool.db"

# Остаточная ветка для своего дела: живого факта в журнале нет, а kanban тут ни
# при чём — своего дела в kanban нет, и «состояние из kanban» было бы выдумкой.
# Поэтому состояние называет `deals.status`, а текст говорит **только то, что
# подтверждено**: у `todo`/`ready` живого факта в журнале нет, у `running` — нет
# живого сигнала о попытке. Приписывать движку прошлую версию («спавна в 0.0.3
# нет», «попыток нет») нельзя: он спавнит попытки и берёт лизинг (`runner.py`), и
# та же ложь рядом уже исправлена — `OWN_NO_ATTEMPTS`.
#
# Вторая половина «только подтверждённого» — история попыток: фраза «шага движка
# ещё не было» верна лишь тогда, когда попыток в журнале нет вовсе. У дела с
# попытками (запарковано и разблокировано) таймлайн ниже показывает те самые
# шаги, и та же фраза читалась бы как «движок не начинал». Поэтому хвост про шаг
# выбирает `_own_waiting_fallback` — по числу попыток из журнала, а не словарь.
OWN_WAITING_FALLBACK = {
    "todo": ("wait_slot", "status=todo без живых фактов"),
    "ready": ("wait_slot", "status=ready без живых фактов"),
    "running": (
        "no_signal",
        "status=running без живого факта журнала: идёт ли попытка — неизвестно",
    ),
    "blocked": ("wait_human", "status=blocked: нужен человек"),
    "review": ("wait_human", "status=review: ждёт вердикта ревьюера"),
}


def _own_waiting_fallback(status: str, attempts: int) -> tuple[str, str]:
    """Состояние и доказательство своего дела, когда живого факта в журнале нет.

    Один текст на три поверхности: `/now` (строка и подсказка «Сейчас»),
    `bin/duty deal` и `why` печатают `waiting.evidence` дословно, поэтому фраза
    живёт здесь, а не в каждой из них.

    Про шаг движка говорит факт журнала, а не постоянная формулировка: попыток
    `0` — «шага ещё не было», попытки есть — «идущей попытки нет, следующего шага
    не было». Второе не противоречит таймлайну тех же попыток, первое — не
    выдумка для свежего дела.
    """
    state, head = OWN_WAITING_FALLBACK.get(
        status, ("wait_slot", f"status={status or '—'}: нет живых фактов")
    )
    if state != "wait_slot":
        return state, head
    if attempts:
        return state, (
            f"{head}: идущей попытки нет (попыток в журнале: {attempts}, все закрыты), "
            "следующего шага движка не было (bin/duty run)"
        )
    return state, f"{head}: шага движка ещё не было (bin/duty run)"


def _timeline(
    events: list[dict[str, Any]], now: int, home: Optional[Path] = None
) -> list[dict[str, Any]]:
    """Таймлайн дела по событиям журнала: что произошло, когда и с чем.

    Одна форма для своих и наблюдаемых дел: экран рисует её одинаково. Выжимка
    payload'а берётся тем же `_payload_summary`, что у последнего действия, —
    второго форматтера в ответе быть не должно.

    Вопросу и ответу добавляется их текст из пула (`ask.exchange_of`): журнал
    хранит только номер вопроса, а человеку на экране нужен сам вопрос, а не
    `question_id`. Пул читается только на чтение и только когда он есть: без
    `home` (наблюдаемое дело) факты остаются номерами, и это видно в подписи.
    """
    rows = []
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload") or {}
        row = {
            "kind": kind,
            "at": event.get("ts"),
            "recorded_at": event.get("recorded_at"),
            "age_sec": _age_sec(event.get("ts"), now),
            "source": event.get("source"),
            "summary": _payload_summary(json.dumps(payload, ensure_ascii=False)),
        }
        if kind == "question_asked":
            row["question_id"] = payload.get("question_id")
            row["routing"] = (payload.get("routing") or {}).get("target") if isinstance(payload.get("routing"), dict) else None
        elif kind == "question_answered":
            row["question_id"] = payload.get("question_id")
            row["answered_via"] = payload.get("answered_via")
        elif kind == "completed":
            row["cause"] = payload.get("cause")
            row["stages"] = payload.get("stages")
        if home is not None and kind in ("question_asked", "question_answered"):
            text = ask.exchange_of(home, payload.get("question_id"))
            if text.get("question"):
                row["question_text"] = text["question"]
            # Ответ показывается только там, где он был фактом: у `question_asked`
            # ответа ещё не было, и подставлять текущий текст пула значило бы
            # переписать историю.
            if kind == "question_answered" and text.get("answer"):
                row["answer_text"] = text["answer"]
        rows.append(row)
    return rows


def _own_waiting(
    folded: dict[str, Any],
    question: Optional[dict[str, Any]],
    status: str,
    journal_note: Optional[str],
    pool_note: Optional[str],
    *,
    home: Optional[Path] = None,
    deal_id: str = "",
    attempts: int = 0,
) -> dict[str, Any]:
    """`waiting` своего дела: журнал первым, остальное — по `deals.status`.

    Kanban-ветки здесь нет и быть не может: своего дела в kanban не существует, и
    подставить туда «состояние из kanban» значило бы выдумать факт. Пометка в
    `evidence` говорит, откуда взято состояние, — как и у наблюдаемых дел.

    `attempts` — сколько попыток записано в журнале (`_own_attempt_facts`): без
    этого числа остаточная ветка говорила бы «шага движка ещё не было» и над
    таймлайном тех самых попыток.
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
    state, evidence = _own_waiting_fallback(status, attempts)
    human_kind = "question" if question is not None else ("status" if state == "wait_human" else None)
    if pool_note and state == "wait_human" and question is None:
        evidence = f"{evidence}; {pool_note}"
    return {"state": state, "evidence": f"{head}; {evidence}", "human_kind": human_kind}


def _own_attempt_facts(
    events: list[dict[str, Any]],
    leases: dict[tuple[str, int], int],
    now: int,
) -> tuple[Optional[dict[str, Any]], dict[str, Any], int]:
    """Факты попыток своего дела: `(идущая попытка, лизинг, попыток в журнале)`.

    Попытка — последняя незакрытая в журнале (`attempt_started` без
    `attempt_ended`): у своего дела нет записи рана kanban, и называть живую
    попытку «попытки нет» значит противоречить собственному таймлайну
    (`GET /duty/{deal_id}/attempts`). Все попытки закрыты — идущей нет, и это
    тоже факт журнала, поэтому рядом едет их число: без него поверхность не
    отличила бы «движок ещё не начинал» от «попытки были и закрыты».

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
    rows = stagefeed.attempts(events, now, leases=leases)
    open_rows = [row for row in rows if row["finished_at"] is None]
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
        return None, lease, len(rows)
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
        len(rows),
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
    """Дело store в той же форме, в какой его читают поверхности.

    Пустые факты названы пустыми, а не выброшены: `last_run`/`priority`/
    `workspace_kind` у дела нет вовсе, и это видно полем, а не отсутствием
    ключа — иначе экран не отличил бы «нечего показывать» от «не посчитали».
    Попытка и лизинг — наоборот, читаются из журнала и store (`_own_attempt_facts`):
    движок спавнит попытки и держит лизинг, и постоянные `null`/`none` на этом
    месте врали бы про идущую работу.
    """
    last = events[-1] if events else None
    folded = journal.fold(events, now=now)
    attempt, lease, attempts = _own_attempt_facts(events, leases or {}, now)
    return {
        "repo": row.get("repo"),
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
        # Расход дела против верхнего `budget` спеки (`dashboard/budget.py`):
        # `null` — потолка на дело нет, и поверхность не выдумывает нули.
        "budget": budget.describe_events(events),
        # Публикация — тот же факт журнала, что у `/deal` (`dashboard/publish.py`).
        "published": publish.published_of(events),
        "waiting": _own_waiting(
            folded, question, str(row["status"]), journal_note, pool_note,
            home=home, deal_id=str(row["id"]), attempts=attempts,
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
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Активные дела для среза «сейчас»: `(дела, ошибки)`.

    Отсутствие store — не ошибка, а «дел ещё нет» (`create=False` файла не
    создаёт). Нечитаемый store или журнал — строка в `errors[]`: пустой список без
    причины выглядел бы как «дел нет», а это разные факты.
    """
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 - свой store не открылся
        return [], [{"source": DEALS_SOURCE, "reason": f"{type(exc).__name__}: {exc}"}]
    if conn is None:
        return [], []
    try:
        if not deals.has_store(conn):
            return [], []
        out: list[dict[str, Any]] = []
        for row in deals.deal_rows(conn, open_only=True):
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
        return [], [{"source": DEALS_SOURCE, "reason": f"store: {type(exc).__name__}: {exc}"}]
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
    budget_block: Optional[dict[str, Any]] = None,
    published_block: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Ответ «одно дело»: одна форма для всех поверхностей.

    Экран не должен собирать дело из разных ответов: ключи одинаковые у
    `GET /deal/{deal_id}`, у мутации и у `bin/duty deal` — различаются значения.
    Дела бывают только свои (0.0.6): наблюдаемых kanban-дел в этой форме нет,
    карточки живут своим ответом `/cards` и сюда не подмешиваются.

    `repo` — группировка дела (репозиторий из спеки), а не адрес: адрес дела —
    его `id`. `null` здесь значит «репозиторий не назван», и это законное
    состояние, а не «не посчитали».

    `stages` — лента этапов (`dashboard/stagefeed.py`): состояние графа и попытки
    из журнала. Пустой список без причины выглядел бы как «этапов нет», поэтому
    `stages_note` называет, почему лента пуста (нет спеки, спека не читается),
    а `spec` — какой граф прочитан.

    `closed` — факт закрытия (`journal.closed_fact`): причина и время, `null` —
    дело не закрывалось фактом журнала. Один ответ на вопрос «почему закрыто» для
    `deal`, `why` и `run`: состояния ожидания у закрытого дела нет, и вместо него
    называется записанный факт, а не пересказ статуса.

    `budget_block` — расход дела против верхнего `budget` спеки
    (`dashboard/budget.py`): у закрытого дела состояния ожидания нет, а расход
    остаётся фактом, поэтому блок живёт здесь, а не в `deal`-срезе. `null` —
    потолка на дело нет.
    """
    body_text = str(row.get("body") or "")
    status = str(row.get("status") or "")
    spec_text, spec_text_truncated = _spec_text(spec)
    return {
        "now": now,
        "repo": row.get("repo"),
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
        "spec_text": spec_text,
        "spec_text_truncated": spec_text_truncated,
        "stages": list(stages or []),
        "stages_note": stages_note,
        "budget": budget_block,
        # Публикация (`dashboard/publish.py`): `{sha, branch, mr_url, …}` или
        # `null`. Это факт журнала, записанный движком после `git ls-remote`, а не
        # заявка исполнителя: «не публиковали» и «публикация не подтверждена» —
        # разные факты, и второй живёт в причине отказа попытки.
        "published": published_block,
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
        deal_id=deal_id,
        row=row,
        shaped=shaped,
        comments=deals.comments_of(conn, deal_id),
        attachments=deals.attachments_of(conn, deal_id),
        timeline=_timeline(events, now, home),
        stages=feed["stages"],
        stages_note=feed["note"],
        spec=feed["spec"],
        closed=journal.closed_fact(events),
        budget_block=budget.describe_events(events),
        published_block=publish.published_of(events),
    )


def collect_now(
    home: Path,
    now: int,
    repo: Optional[str] = None,
    state: Optional[str] = None,
) -> dict[str, Any]:
    """Срез «сейчас»: активные свои дела, отсортированные по таксономии.

    Источник один — store дел (`duty/duty.db`). Карточек kanban в срезе нет
    (0.0.6): доска была тем, чем карточка попадала в дела, и без неё это были бы
    дела без адреса. Наблюдение за карточками живёт отдельно — `GET /cards`.

    Сортировка детерминирована: `waiting.state` по `WAITING_PRIORITY`, затем
    возраст текущей попытки по убыванию, затем `id` — иначе UI дёргается между
    опросами.

    `repo` — фильтр группировки (`None` — все дела, включая дела без репозитория);
    `repo=""` — ровно дела **без** репозитория. Пустая строка как значение фильтра
    не выдумка: «без репозитория» — законная группа, и отличить её от «фильтра нет»
    иначе нечем.

    `waiting` считается fold'ом по журналу. Ошибка store не валит ответ — она в
    `errors[]`: «дел нет» и «store не прочитался» — разные факты.
    """
    questions, pool_note, pool_errors = _pool_context(home)
    errors: list[dict[str, str]] = list(pool_errors)
    journal_events, journal_note, journal_errors = _journal_context(home)
    errors.extend(journal_errors)
    items, own_errors = _own_deals(
        home, now, questions, pool_note, journal_events, journal_note
    )
    errors.extend(own_errors)
    if repo is not None:
        items = [deal for deal in items if (deal.get("repo") or "") == repo]

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
        # Группировка вместо списка досок: уникальные непустые `repo` дел плюс
        # число дел без репозитория. Считается по всему store, а не по срезу, —
        # это состав списка, а не его текущая страница.
        "repos": _repos_of(home),
        "by_state": by_state,
        "crash_outcomes": list(CRASH_OUTCOMES),
        "errors": errors,
    }


# ------------------------------------------------- завершённые дела (0.0.7)
#
# Экран «Завершённые» отвечает на вопрос «что уже сделано». Это не второй срез
# «сейчас»: список закрытых дел (`done`/`archived`) без состояний ожидания.
# Источник тот же, что у карточки дела, — store плюс журнал, и числа строки
# считаются теми же функциями (`stagefeed.attempts`, `journal.closed_fact`,
# `publish.published_of`), иначе строка списка спорила бы с `bin/duty deal <id>`.

# Статусы списка — ровно терминальные (`done`, `archived`): активные дела живут в
# `/now`, и второй список активных дел дал бы две правды о «сейчас».
FINISHED_STATUSES = tuple(TERMINAL_STATUSES)

# Сколько строк отдаём по умолчанию и сколько разрешаем попросить. Список — не
# выгрузка всей истории: `total` называет, сколько закрытых дел есть на самом
# деле, а `truncated` — что показаны не все.
DEFAULT_DEALS_LIMIT = 50
MAX_DEALS_LIMIT = 500


def _finished_questions(events: list[dict[str, Any]]) -> dict[str, int]:
    """Вопросы человеку по журналу: `asked`, `answered` и `waiting`.

    Пары считаются тем же порядком, что рисует шкала дела (`desktop/model.js`,
    `shapeDealTime`): `question_answered` закрывает первый неотвеченный
    `question_asked`. Поэтому `waiting` — остаток пар, а не разность чисел:
    ответ на чужой вопрос (или ответ без вопроса) не должен показывать «ждёт»
    на закрытом деле.
    """
    asked = 0
    answered = 0
    pending = 0
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == "question_asked":
            asked += 1
            pending += 1
        elif kind == "question_answered":
            answered += 1
            if pending:
                pending -= 1
    return {"asked": asked, "answered": answered, "waiting": pending}


def _finished_span(events: list[dict[str, Any]], attempts: list[dict[str, Any]]) -> Optional[int]:
    """Длительность дела — та же ось, что у шкалы времени в карточке дела.

    Начало — минимум из времён событий журнала и стартов попыток, конец —
    максимум из них же и концов попыток (у закрытой попытки без `finished_at`
    конец считается по `duration_sec`). Это ровно `shapeDealTime`
    (`desktop/model.js`): другая формула — «`created_at` → `finished_at`» — дала
    бы в строке списка число, которого на карточке того же дела нет.

    `None` — в журнале нет ни одного времени: рисовать нечего, и ноль здесь был
    бы выдумкой.
    """
    stamps = [int(event.get("ts") or 0) for event in events if int(event.get("ts") or 0) > 0]
    for attempt in attempts:
        if attempt["started_at"] is not None:
            stamps.append(int(attempt["started_at"]))
    if not stamps:
        return None
    start = min(stamps)
    end = max(stamps)
    for attempt in attempts:
        if attempt["started_at"] is None:
            continue
        stop = attempt["finished_at"]
        if stop is None:
            stop = int(attempt["started_at"]) + int(attempt["duration_sec"] or 0)
        end = max(end, int(stop))
    return max(0, end - start)


def _finished_row(
    row: dict[str, Any],
    events: list[dict[str, Any]],
    now: int,
    leases: dict[tuple[str, int], int],
) -> dict[str, Any]:
    """Строка «Завершённых» из строки store и журнала.

    `finished_at`/`cause`/`stages` — записанный факт закрытия
    (`journal.closed_fact`), а не пересказ статуса: у дела, закрытого архивацией,
    причины нет вовсе, и пустая строка здесь честнее придуманной. `attempts`,
    `questions` и `duration_sec` считаются по тем же событиям, что отдают
    `GET /deal/{deal_id}` и `GET /duty/{deal_id}/attempts`, поэтому строка и
    карточка одного дела называют одни числа.
    """
    closed = journal.closed_fact(events)
    payload = (closed or {}).get("payload") or {}
    attempts = stagefeed.attempts(events, now, leases=leases)
    return {
        "id": str(row["id"]),
        "repo": row.get("repo"),
        "title": row.get("title"),
        "assignee": row.get("assignee"),
        "status": str(row["status"]),
        "created_at": row.get("created_at"),
        "finished_at": None if closed is None else int(closed.get("at") or 0),
        "duration_sec": _finished_span(events, attempts),
        "cause": str(payload.get("cause") or ""),
        "stages": [str(stage) for stage in payload.get("stages") or ()],
        "attempts": len(attempts),
        "questions": _finished_questions(events),
        "published": publish.published_of(events),
    }


def _finished_note(
    rows: list[dict[str, Any]],
    statuses: tuple[str, ...],
    repo: Optional[str],
    errors: list[dict[str, str]],
) -> str:
    """Почему список пуст — словами, а не пустым экраном.

    Пустой ответ законен (закрытых дел может не быть), но причин у него три
    разных, и «нет дел» вместо каждой было бы враньём: нечитаемый store,
    пустой фильтр по репозиторию и store без закрытых дел — разные факты.
    Бэкенд называет их строкой `note`, а страница её печатает как есть.
    """
    if rows:
        return ""
    if errors:
        return f"завершённые дела не прочитаны: {errors[0]['reason']}"
    where = "store" if repo is None else ("репозитории " + repo if repo else "группе без репозитория")
    return f"завершённых дел нет: в {where} ни одного дела в статусе {', '.join(statuses)}"


def _deals_cursor_key(row: dict[str, Any]) -> tuple[int, str]:
    """Ключ порядка строки списка: `(-finished_at, id)`.

    Знак минус — потому что список идёт свежими вверх, а ключ должен расти в том
    же направлении, что и порядок: тогда «следующая страница» — это `key >
    ключ последней строки`. `finished_at` может быть `null` (факта закрытия в
    журнале нет): такие строки уезжают в конец списка, и ключ у них — `0`.
    """
    return (-int(row.get("finished_at") or 0), str(row.get("id") or ""))


def _parse_deals_cursor(cursor: str) -> tuple[int, str]:
    """`<finished_at>:<id>` → ключ порядка; кривой курсор — отказ.

    Курсор приходит от поверхности, а не от человека, но отказ с форматом честнее
    пустого списка: «страница пуста» и «курсор сломан» — разные факты.
    """
    text = str(cursor or "").strip()
    at, sep, deal_id = text.partition(":")
    if not sep or not deal_id.strip() or not re.fullmatch(r"-?\d+", at.strip()):
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=(
                f"?cursor={cursor!r}: ожидалось `<finished_at>:<id>` "
                "(ключ последней строки страницы из `next_cursor`)"
            ),
        )
    return (-int(at.strip()), deal_id.strip())


def collect_deals(
    home: Path,
    now: int,
    *,
    statuses: tuple[str, ...] = FINISHED_STATUSES,
    repo: Optional[str] = None,
    limit: int = DEFAULT_DEALS_LIMIT,
    cursor: Optional[str] = None,
) -> dict[str, Any]:
    """Список завершённых дел: закрытые и архивные, свежие сверху.

    `repo` — фильтр группировки (`None` — все репозитории, `""` — ровно дела
    **без** репозитория), как у `/now` и `/stats`. `limit` режет список после
    сортировки, а `total` называет, сколько дел подошло под фильтр целиком:
    иначе «50 строк» читалось бы как «50 закрытых дел». `cursor` — «строго
    старше названного ключа»: следующая страница без смещения, поэтому новое
    закрытие между запросами не сдвигает окно и не повторяет строку.

    Ошибка store не валит ответ — она в `errors[]`, а причина пустоты — в
    `note`: «дел нет» и «store не прочитался» разные факты.
    """
    errors: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []
    total = 0
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 - свой store не открылся
        conn = None
        errors.append({"source": DEALS_SOURCE, "reason": f"{type(exc).__name__}: {exc}"})
    if conn is not None:
        try:
            if deals.has_store(conn):
                for row in deals.deal_rows(conn, open_only=False):
                    if str(row["status"]) not in statuses:
                        continue
                    if repo is not None and (row.get("repo") or "") != repo:
                        continue
                    events = journal.read_events(conn, str(row["id"]))
                    rows.append(
                        _finished_row(row, events, now, _own_leases(conn, str(row["id"])))
                    )
        except Exception as exc:  # noqa: BLE001 - битый store/журнал
            errors.append({"source": DEALS_SOURCE, "reason": f"store: {type(exc).__name__}: {exc}"})
        finally:
            conn.close()
    # Свежие сверху: закрытие — то, чем дело попадает в список. Дело без факта
    # закрытия (`finished_at: null`) уходит в конец, а не притворяется свежим.
    rows.sort(key=_deals_cursor_key)
    total = len(rows)
    # Сколько закрытых дел у каждого репозитория — до отсечения страницей: по
    # этому числу страница показывает «5 из 23» и листает группы отдельно, а не
    # общим потоком, в котором один проект вытесняет другой.
    by_repo: dict[str, int] = {}
    for row in rows:
        key = str(row.get("repo") or "")
        by_repo[key] = by_repo.get(key, 0) + 1
    repos = [
        {"repo": key, "total": count}
        for key, count in sorted(by_repo.items(), key=lambda item: (item[0] == "", item[0]))
    ]
    after = _parse_deals_cursor(cursor) if cursor else None
    if after is not None:
        # Строго после курсора: страница не повторяет строку, на которой
        # остановилась, даже если в списке есть дела с тем же мгновением закрытия.
        rows = [row for row in rows if _deals_cursor_key(row) > after]
    shown = rows[:limit]
    next_cursor = None
    if shown and len(rows) > len(shown):
        last = shown[-1]
        next_cursor = f"{int(last.get('finished_at') or 0)}:{last.get('id')}"
    return {
        "now": now,
        "statuses": list(statuses),
        "count": len(shown),
        "total": total,
        "limit": limit,
        "cursor": cursor or "",
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
        "repos": repos,
        "deals": shown,
        "note": _finished_note(shown, statuses, repo, errors),
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

# Окно здоровья по умолчанию — сутки: спека (экран 4 «здоровье») меряет «за
# сутки», и число без окна («за всё время») с ней не сравнимо. `?since=`/`--since`
# называет границу явно и переопределяет эту.
HEALTH_WINDOW_SEC = 24 * 60 * 60

# Класс отказа «контракт» (`dashboard/failures.py`): движок кладёт его в `kind`
# события `attempt_ended`, старые записи — в `details.class`. Одно слово на плагин:
# переписанное строкой, оно разошлось бы с политикой ретраев.
CONTRACT_CLASS = "contract"

# Код правила в тексте причины отказа: `artifact.missing`, `commit.foreign`,
# `result.json`. Правило — последний точечный код причины, а не путь перед ним.
RULE_CODE_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+\b")


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


def _payload_dict(event: dict[str, Any]) -> dict[str, Any]:
    """`payload` события объектом: у факта без payload полей нет, а не «пусто-мусор»."""
    payload = journal.payload_of(event)
    return payload if isinstance(payload, dict) else {}


def _int(value: Any) -> int:
    """Целое из payload: `True` числом не считается, мусор — нулём."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _maybe_int(value: Any) -> Optional[int]:
    """Целое или `None`: токены отсутствуют честно, а не нулём."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _details_of(payload: dict[str, Any]) -> dict[str, Any]:
    """`details` попытки объектом: детали пишет движок, а не вызывающий."""
    details = payload.get("details")
    return details if isinstance(details, dict) else {}


def _window_events(events: list[dict[str, Any]], since: int) -> list[dict[str, Any]]:
    """Факты окна: `ts` не раньше границы. Окно — про факты, а не про дела."""
    return [event for event in events if _int(event.get("ts")) >= int(since)]


def _refusal_rule(payload: dict[str, Any]) -> str:
    """Правило отказа контракта: `details.rule`, иначе код причины, иначе первая фраза.

    Класс отказа (`contract`) говорит «чем кончилось», а экран «Здоровье» считает
    ещё и **чем** — разбивкой по правилу. Движок пишет правило текстом причины
    (`result.json:artifacts[0].path: artifact.missing: …`), и правилом там служит
    последний точечный код, а не путь перед ним; у причины без кода (`код 1,
    ждали 0`) правило — её первая фраза.
    """
    rule = str(_details_of(payload).get("rule") or "").strip()
    if rule:
        return rule
    reason = str(payload.get("reason") or "").strip()
    codes = RULE_CODE_RE.findall(reason)
    if codes:
        return codes[-1]
    return re.split(r"[:,]", reason, maxsplit=1)[0].strip() or "без правила"


def _refusal_class(payload: dict[str, Any]) -> str:
    """Класс отказа попытки: `kind` события (так пишет движок), иначе `details.class`."""
    return str(payload.get("kind") or _details_of(payload).get("class") or "").strip()


def _human_waits(events: list[dict[str, Any]]) -> list[int]:
    """Ожидание человека: `question_answered.ts − question_asked.ts` по `question_id`.

    Это **другая** величина, чем ожидание до первой попытки этапа: там ждёт
    очередь движка, здесь — человек. Пара ищется по id вопроса, а не по порядку
    событий (ответ приходит отдельным фактом и может лечь позже любого другого).
    Вопрос без ответа в выборку не идёт: он либо ещё ждёт, либо закрыт таймаутом,
    и приписывать ему срок нельзя.
    """
    asked: dict[str, int] = {}
    answered: dict[str, int] = {}
    for event in sorted(events, key=lambda item: _int(item.get("id"))):
        kind = str(event.get("kind") or "")
        if kind not in ("question_asked", "question_answered"):
            continue
        payload = _payload_dict(event)
        question_id = payload.get("question_id")
        if question_id is None:
            continue
        key = str(question_id)
        if kind == "question_asked":
            asked.setdefault(key, _int(event.get("ts")))
        else:
            answered.setdefault(key, _int(event.get("ts")))
    return [max(0, answered[key] - asked[key]) for key in asked if key in answered]


def _repo_metrics(
    name: str,
    rows: list[dict[str, Any]],
    journal_events: dict[str, list[dict[str, Any]]],
    now: int,
    leases: dict[str, dict[tuple[str, int], int]],
    since: int,
) -> dict[str, Any]:
    """Здоровье одной группы-репозитория: по журналу её дел, не по карточкам.

    Метрики — про **дела движка**, и считаются ровно по тем фактам, которые он
    пишет сам: этап (`deal_id` + `stage`), закрытый с первой попытки; исходы и
    классы попыток; ожидание человека (`question_asked` → `question_answered`);
    ожидание до первой попытки этапа; отказы контракта с разбивкой по правилу;
    токены за окно. Ни одно число здесь не выводится из `kanban.db`: карточки —
    наблюдение (`/cards`, отдельный блок ответа `cards`), и их «раны» не дела.

    `since` — граница окна по `ts` фактов: `attempt_*`, `question_*` и близкие
    метрики считаются только по фактам окна, иначе «за сутки» и «за всё время»
    смешались бы в одно число.

    `first_try.share` — `null` при нулевом знаменателе, а не «100%» и не «0%»:
    доля без случаев не считается. `waiting_sec` и `waiting_human_sec` —
    распределения: «ждали 0 секунд» и «ждать было нечему» разные факты, поэтому
    пустая выборка даёт `null`, а не нули. `tokens.total` — `null`, если ни одна
    закрытая попытка окна не записала `usage_tokens`: ноль был бы выдумкой.
    """
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    by_class: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    waits: list[int] = []
    human_waits: list[int] = []
    attempts_total = 0
    first_ok = 0
    closed = 0
    stages_total = 0
    deals_active = 0
    ended_total = 0
    tokens_total = 0
    tokens_count = 0
    for row in rows:
        status = str(row.get("status") or "—")
        by_status[status] = by_status.get(status, 0) + 1
        deal_id = str(row["id"])
        events = _window_events(journal_events.get(deal_id, []), since)
        if events:
            deals_active += 1
        human_waits.extend(_human_waits(events))
        for event in events:
            if str(event.get("kind") or "") != "attempt_ended":
                continue
            payload = _payload_dict(event)
            ended_total += 1
            refusal = _refusal_class(payload)
            if refusal:
                by_class[refusal] = by_class.get(refusal, 0) + 1
                if refusal == CONTRACT_CLASS:
                    rule = _refusal_rule(payload)
                    by_rule[rule] = by_rule.get(rule, 0) + 1
            tokens = _maybe_int(_details_of(payload).get("usage_tokens"))
            if tokens is not None:
                tokens_total += tokens
                tokens_count += 1
        attempts = stagefeed.attempts(events, now, leases=leases.get(deal_id, {}))
        attempts_total += len(attempts)
        stages: dict[str, list[dict[str, Any]]] = {}
        for attempt in attempts:
            stages.setdefault(str(attempt.get("stage") or ""), []).append(attempt)
            outcome = attempt.get("outcome")
            key = str(outcome) if outcome else f"без исхода ({attempt.get('status') or '—'})"
            by_outcome[key] = by_outcome.get(key, 0) + 1
        stages_total += len(stages)
        for stage_attempts in stages.values():
            finished = [item for item in stage_attempts if item.get("finished_at") is not None]
            if not finished:
                continue
            closed += 1
            # «С первой попытки» — этап закрыт с первой же попытки и успешно.
            if len(stage_attempts) == 1 and str(finished[0].get("outcome") or "") in SUCCESS_OUTCOMES:
                first_ok += 1
        created = row.get("created_at")
        started = next(
            (
                int(attempt["started_at"])
                for attempt in attempts
                if attempt.get("started_at") is not None
            ),
            None,
        )
        if created is not None and started is not None:
            waits.append(max(0, started - int(created)))
    return {
        "repo": name,
        "deals": len(rows),
        "deals_active": deals_active,
        "deals_by_status": by_status,
        "attempts_total": attempts_total,
        "attempts_by_outcome": by_outcome,
        "attempts_by_class": by_class,
        # Доля этапов с первой попытки — та самая метрика спеки; имя `first_try`
        # оставлено как было: его читают поверхности (`dashboard/web/app.js`,
        # `desktop/plugin.js`) и оно не врёт. `stages` называет знаменатель.
        "first_try": {
            "ok": first_ok,
            "closed": closed,
            "share": (first_ok / closed) if closed else None,
        },
        "stages": {"total": stages_total, "closed": closed},
        "waiting_sec": _waiting_distribution(waits),
        "waiting_human_sec": _waiting_distribution(human_waits),
        "contract_refusals": {"count": sum(by_rule.values()), "by_rule": by_rule},
        "tokens": {
            "total": tokens_total if tokens_count else None,
            "count": tokens_count,
            "without": ended_total - tokens_count,
        },
        "_waits": waits,
        "_human_waits": human_waits,
    }


def _pooled(metric: str, groups: list[dict[str, Any]]) -> dict[str, Any]:
    """Распределение по объединённой выборке групп: перцентиль групп — не перцентиль целого."""
    values: list[int] = []
    for group in groups:
        values.extend(group.pop(metric, []))
    return _waiting_distribution(values)


def collect_stats(
    home: Path,
    now: int,
    repo: Optional[str] = None,
    since: Optional[int] = None,
) -> dict[str, Any]:
    """Здоровье дел по репозиториям: этапы, ожидание человека, отказы, токены.

    Источник один — store дел и его журнал (`duty/duty.db`). Доски были тем,
    чем считалась статистика до 0.0.6, — и вместе с ними ушёл чужой объект:
    `runs_by_outcome` по `task_runs` kanban описывал карточки, а не дела. Группа
    теперь одна — `repo` строки дела; дела без репозитория идут отдельной группой
    (`without_repo`), и это не «ошибка данных», а законное состояние.

    Окно по умолчанию — сутки (`HEALTH_WINDOW_SEC`), как в спеке экрана 4:
    «за сутки» и «за всё время» — разные числа, и молча отдавать второе под
    первым нельзя. `since` называет границу явно; тогда же по `created_at`
    сужается и список дел (так было и до окна по умолчанию), а без явной границы
    сужаются только факты: дело живёт дольше окна, и его `deals` — объект, а не
    факт.

    Распределения считаются по объединённой выборке группы, а не как среднее:
    перцентиль перцентилей не равен перцентилю объединённой выборки. Нечитаемый
    store или журнал попадает в `errors`, и ответ остаётся 200.

    Карточек kanban здесь нет ни одним полем: у карточки нет этапов, отказов
    контракта и токенов, а здоровье обязано отвечать про **дела**. Наблюдение за
    карточками живёт отдельным ответом `GET /cards`.
    """
    errors: list[dict[str, str]] = []
    since_value = int(since) if since is not None else int(now) - HEALTH_WINDOW_SEC
    journal_events, _, journal_errors = _journal_context(home)
    errors.extend(journal_errors)
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 - store не открылся
        conn = None
        errors.append({"source": DEALS_SOURCE, "reason": f"{type(exc).__name__}: {exc}"})
    rows: list[dict[str, Any]] = []
    leases: dict[str, dict[tuple[str, int], int]] = {}
    if conn is not None:
        try:
            if deals.has_store(conn):
                rows = deals.deal_rows(conn, open_only=False)
                if since is not None:
                    rows = [row for row in rows if int(row.get("created_at") or 0) >= int(since)]
                leases = {str(row["id"]): _own_leases(conn, str(row["id"])) for row in rows}
        except Exception as exc:  # noqa: BLE001 - битый store/журнал
            errors.append({"source": DEALS_SOURCE, "reason": f"store: {type(exc).__name__}: {exc}"})
        finally:
            conn.close()

    grouped: dict[str, list[dict[str, Any]]] = {}
    without: list[dict[str, Any]] = []
    for row in rows:
        name = row.get("repo")
        if name:
            grouped.setdefault(str(name), []).append(row)
        else:
            without.append(row)

    groups = [
        _repo_metrics(name, grouped[name], journal_events, now, leases, since_value)
        for name in sorted(grouped)
    ]
    empty = _repo_metrics("", [], journal_events, now, leases, since_value)
    without_group = _repo_metrics("", without, journal_events, now, leases, since_value)
    if repo is not None:
        # Неизвестный `repo` — это фильтр, а не ресурс: пустой результат, а не 404.
        # Опечатка в фильтре видна пустым ответом; отказом она выглядела бы как
        # «такого репозитория нет», хотя репозиторий — свойство дела, а не адрес.
        groups = [group for group in groups if group["repo"] == repo]
        # `repo=""` — ровно дела без репозитория: их группа остаётся, а группы
        # названных репозиториев — пусты.
        if repo:
            without_group = empty
    scoped = groups + [without_group]
    totals = _repo_metrics("", rows, journal_events, now, leases, since_value)
    totals["waiting_sec"] = _pooled("_waits", scoped)
    totals["waiting_human_sec"] = _pooled("_human_waits", scoped)
    totals.pop("_waits", None)
    totals.pop("_human_waits", None)
    warnings: list[str] = []
    if totals["tokens"]["count"] == 0 and totals["tokens"]["without"]:
        warnings.append(
            "токены: ни одна закрытая попытка окна не записала details.usage_tokens — "
            "сумма не считается (null, а не 0)"
        )
    return {
        "now": now,
        "window": {
            "since": since_value,
            "sec": max(0, int(now) - since_value),
            "source": "explicit" if since is not None else "default",
        },
        "repos": groups,
        "without_repo": without_group,
        "totals": totals,
        "errors": errors,
        "warnings": warnings,
        "success_outcomes": list(SUCCESS_OUTCOMES),
        "crash_outcomes": list(CRASH_OUTCOMES),
    }


@router.get("/stats")
def stats(
    repo: Optional[str] = None,
    since: Optional[int] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Здоровье дел по репозиториям: этапы, ожидание человека, отказы, токены.

    `?repo=<путь>` — только один репозиторий; `?repo=` (пустая строка) — только
    дела **без** репозитория; `?since=<unix>` — граница окна (по умолчанию
    сутки): факты считаются с неё, а список дел при явной границе сужается по
    `created_at`, как и раньше. Фильтры считает `collect_stats`, поэтому CLI и
    роут отдают одну и ту же форму и одни и те же числа. Неизвестный `repo` — это
    фильтр, а не ресурс: ответ 200 с пустым результатом, как у `/now` (одно
    поведение на обеих ручках — `docs/SPEC.md`). `?board=` — отказ (400) с
    подсказкой: доск нет, фильтр называется `repo`.

    Ответ описывает только **дела**: карточек kanban в нём нет ни одним полем —
    наблюдение за ними живёт отдельным роутом `GET /cards`.
    """
    _refuse_board_filter(board)
    return collect_stats(_hermes_home(), _now_sec(), repo=repo, since=since)


@router.get("/deal/{deal_id}")
def deal(deal_id: str, spec: Optional[str] = None) -> dict[str, Any]:
    """Одно дело: спецификация плюс тот же факт-набор, что в `/now`.

    Адрес дела — только его id `d_<hex>` (0.0.6). Карточка kanban (`t_<hex>`)
    делом не бывает: доски, которая связывала их, больше нет, и попытка открыть
    дело по карте — отказ словами, а не пустой экран.

    `deal` — `null` для закрытого дела (`done`/`archived`): состояний ожидания у
    него нет, и придумывать их нельзя. Таймлайн попыток живёт отдельно —
    `GET /duty/{deal_id}/attempts`. Лента этапов (`stages`) есть и у закрытого
    дела: что было — факт, он не перестаёт быть им после закрытия.

    `?spec=<абсолютный путь>` — граф для ленты этапов, если в журнале спека не
    записана. Файл читается `load_spec()` (`yaml.safe_load`), не исполняется;
    процессов роут не открывает ни одного.
    """
    home = _hermes_home()
    if not deals.is_own(deal_id) and deals.is_card(deal_id):
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=(
                f"'{deal_id}' — карточка kanban, а не дело: доск больше нет, дело "
                "адресуется своим id d_<hex>; карточки смотри в /cards"
            ),
        )
    return _own_deal(home, deal_id, spec=spec)


def _resolve_deal(home: Path, deal_id: str) -> dict[str, Any]:
    """Дело по id для роутов: строка store или 404 словами.

    Одно место, где решается «это дело?»: `d_<hex>` живёт в store, а `t_<hex>`
    делом не является вовсе (0.0.6). Возвращает ровно те факты, которых хватает
    вопросу и таймлайну (`id`, `repo`, `status`, `title`); 404/503 бросаются
    здесь же, чтобы у всех роутов была одна формулировка отказа.
    """
    if deals.is_card(deal_id):
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=(
                f"'{deal_id}' — карточка kanban, а не дело: дело адресуется своим id "
                "d_<hex>; карточки смотри в /cards"
            ),
        )
    if not deals.is_own(deal_id):
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=f"id {deal_id!r} не дело: свои дела адресуются как d_<hex>",
        )
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
    if row is None:
        raise HTTPException(
            status_code=404,
            headers=NO_STORE_HEADERS,
            detail=f"deal '{deal_id}' not found",
        )
    return {
        "id": deal_id,
        "repo": row.get("repo"),
        "status": str(row["status"]),
        "title": row.get("title"),
    }


def _own_deal(home: Path, deal_id: str, *, spec: Optional[str] = None) -> dict[str, Any]:
    """Своё дело по id: строка store плюс журнал; 404, если его нет."""
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
        if row is None:
            raise HTTPException(
                status_code=404,
                headers=NO_STORE_HEADERS,
                detail=f"deal '{deal_id}' not found",
            )
        questions, pool_note, _ = _pool_context(home)
        _, journal_note, _ = _journal_context(home)
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


def _error_body(deal_id: Optional[str], reason: str) -> dict[str, Any]:
    """Тело ошибки мутации — та же форма, что у успеха, но `deal: null`.

    Клиент не должен разбирать второй тип тела ради отказа: ключи те же, причина
    в `errors[]` (как у остальных ошибок), дело не притворяется живым. `repo`
    здесь `null`: отказанная мутация не назвала репозиторий — объявлять его в
    ответе значило бы утверждать то, чего в store не появилось.
    """
    return {
        "now": _now_sec(),
        "repo": None,
        "id": deal_id,
        "title": None,
        "status": None,
        "assignee": None,
        "terminal": False,
        "closed": None,
        "created_at": None,
        "body": "",
        "body_truncated": False,
        "spec_text": "",
        "spec_text_truncated": False,
        "deal": None,
        "comments": [],
        "attachments": [],
        "timeline": [],
        "budget": None,
        "errors": [{"source": DEALS_SOURCE, "reason": reason}],
    }


def _deal_error(exc: RuntimeError, deal_id: Optional[str]) -> JSONResponse:
    """Отвергнутая мутация: 404 для «дела нет», 400 для «вход не тот», не 500.

    Тип в подписи — `RuntimeError` (`deals.DealError` — его наследник): модуль
    своего store загружается по пути, и как аннотация он не тип для проверяльщика,
    а поведение от этого не меняется.
    """
    status = 404 if isinstance(exc, deals.DealNotFound) else 400
    return JSONResponse(
        status_code=status,
        headers=NO_STORE_HEADERS,
        content=_error_body(deal_id, str(exc)),
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
    _, journal_note, _ = _journal_context(home)
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
    """`POST /deals` и `duty create`: одно правило на оба входа.

    `board` в теле больше не принимается (0.0.6): доск нет, дело адресуется своим
    `d_<hex>`, а группировка — `repo`. Отказ по `board` назван словами, а не
    проигнорирован: иначе старый скрипт молча завёл бы дело без группировки и
    решил, что доска «сработала».

    `repo` вычисляется здесь, в одном месте на API и CLI: из `spec.repo`, если
    спека дана и в ней есть `repo`; иначе из явного `repo`; иначе `NULL` —
    «репозиторий не назван». Спека сильнее явного значения, потому что она —
    записанный граф дела: если `worktree`-этап строит дерево из `spec.repo`,
    группировка обязана совпадать с ним, иначе дело попадёт не в ту группу.
    """
    if payload.get("board") is not None:
        raise deals.DealError(
            "create: доск больше нет — дело адресуется своим id, группировка задаётся 'repo'"
        )
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
    repo = payload.get("repo")
    if repo is not None and not isinstance(repo, str):
        raise deals.DealError("create: 'repo' — путь репозитория (строка) или null")
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
                repo=_repo_of(spec, repo),
                title=str(payload.get("title") or ""),
                body=str(body or ""),
                assignee=assignee,
                parents=parents,
                source=source,
                spec_path=spec,
                external_ref=external_ref,
                dedup_window=dedup_window,
            ),
        )
    finally:
        conn.close()


def _repo_of(spec: Optional[str], repo: Optional[str]) -> Optional[str]:
    """Репозиторий нового дела: `spec.repo`, иначе явный `repo`, иначе `None`.

    Нечитаемая спека не отменяет создание дела по `repo`: отказ здесь запретил бы
    заводить дело по графу, который «пока не читается», хотя `repo` назвал сам
    вызывающий. Но и молчать нельзя — поэтому пустая строка от `spec.repo`
    трактуется как «в спеке репозитория нет», а не как репозиторий `""`.
    """
    from_spec: Optional[str] = None
    if spec:
        try:
            from_spec = spec_mod.load_spec(spec).repo
        except Exception:  # noqa: BLE001 - спека не разобралась: остаётся явный repo
            from_spec = None
    chosen = (from_spec or "").strip() or (repo or "").strip()
    return chosen or None


def deals_patch(
    home: Path, deal_id: str, payload: dict[str, Any], *, source: str = "api"
) -> dict[str, Any]:
    """`PATCH /deals/{id}`: title/body/assignee и `status='archived'`.

    `repo` правкой не принимается (0.0.6): он задаётся при создании из спеки дела
    или явного значения, и отказ назван словами — молча проигнорированное поле
    читалось бы как «репозиторий сменён».
    """
    if payload.get("repo") is not None:
        raise deals.DealError(
            "patch: 'repo' — свойство дела, задаётся при создании; правкой не меняется"
        )
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
    """`POST /deals {repo?, title, body?, assignee?, parents?[], spec?, external_ref?}` → дело.

    Отвечает той же формой, что `/deal/{deal_id}`, плюс блок `mutation`. Ошибка
    входа (нет родителя, пустой title, `board` в теле) — 400 с `errors[]`: контракт
    прямо запрещает 500 на битом входе.

    `external_ref` — внешнее имя дела: если живое дело с этим именем в этом
    репозитории уже есть, ответ несёт `created: false` и `existing: true` с
    причиной в `reason`, а второе дело не заводится (`docs/OWNERSHIP.md`).
    """
    try:
        return deals_create(_hermes_home(), payload)
    except deals.DealError as exc:
        return _deal_error(exc, None)


@router.patch("/deals/{deal_id}")
def patch_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`PATCH /deals/{id} {title?, body?, assignee?, status?}` → дело."""
    try:
        return deals_patch(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/comment")
def comment_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/comment {author, body}` → дело с комментарием в таймлайне."""
    try:
        return deals_comment(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/block")
def block_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/block {kind, reason}` → дело в `blocked`."""
    try:
        return deals_block(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/unblock")
def unblock_deal_route(deal_id: str, payload: dict[str, Any] = Body(default={})) -> Any:
    """`POST /deals/{id}/unblock {}` → дело снова в очереди."""
    try:
        return deals_unblock(_hermes_home(), deal_id, payload or {})
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/decide")
def decide_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/decide {decision: continue|stop, reason?}` → факт решения.

    `continue` — тот же факт, что `unblock`; `stop` — `completed` с
    `cause='stopped_by_human'`. Оба идемпотентны: повтор не пишет второй факт.
    """
    try:
        return deals_decide(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/close")
def close_deal_route(deal_id: str, payload: dict[str, Any] = Body(default={})) -> Any:
    """`POST /deals/{id}/close {reason}` → дело закрыто решением человека."""
    try:
        return deals_close(_hermes_home(), deal_id, payload or {})
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/claim")
def claim_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/claim {owner}` → дело `running` с названным владельцем."""
    try:
        return deals_claim(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/complete")
def complete_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/complete {summary, review?}` → дело закрыто или в ревью."""
    try:
        return deals_complete(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/review")
def review_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/review {reviewer?, summary?}` → `status='review'`."""
    try:
        return deals_review(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


@router.post("/deals/{deal_id}/changes")
def changes_deal_route(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """`POST /deals/{id}/changes {reason, reviewer?}` → возврат на доработку."""
    try:
        return deals_changes(_hermes_home(), deal_id, payload)
    except deals.DealError as exc:
        return _deal_error(exc, deal_id)


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
        return _deal_error(exc, deal_id)


BOARD_FILTER_HINT = (
    "фильтра 'board' больше нет (0.0.6): доск нет, дело адресуется своим id d_<hex>, "
    "а группировка задаётся 'repo' — передай ?repo=<путь>, ?repo= для дел без "
    "репозитория или убери параметр целиком"
)


def _refuse_board_filter(board: Optional[str]) -> None:
    """Отказ на `?board=`: явная ошибка с подсказкой, а не молчаливое игнорирование.

    FastAPI пропускает незнакомый query-параметр молча, и вызов со старым
    `?board=stack` вернул бы полный срез — то есть выдал бы «фильтр по доске» за
    применённый. Два способа адресовать одно дело (и два фильтра на одно поле)
    дали бы две правды, поэтому старый параметр назван словами.
    """
    if board is None:
        return
    raise HTTPException(
        status_code=400,
        headers=NO_STORE_HEADERS,
        detail=f"?board={board!r}: {BOARD_FILTER_HINT}",
    )


def _finished_statuses(status: Optional[str]) -> tuple[str, ...]:
    """`?status=done,archived` → кортеж терминальных статусов.

    Незнакомый статус — отказ (400), а не пустой список: `?status=blocked`
    выглядел бы как «завершённых дел нет», хотя такие дела есть — они просто не
    завершены. Активные дела показывает `/now`, и второй список для них здесь не
    заводится (`docs/DESIGN.md`, «Экраны»).
    """
    if status is None:
        return FINISHED_STATUSES
    names = tuple(part.strip() for part in status.split(",") if part.strip())
    if not names:
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=f"?status={status!r}: пустой список — ожидалось {list(FINISHED_STATUSES)}",
        )
    unknown = [name for name in names if name not in FINISHED_STATUSES]
    if unknown:
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=(
                f"?status={status!r}: {', '.join(unknown)} — не завершённые статусы. "
                f"Список отвечает про {list(FINISHED_STATUSES)}; активные дела — GET /now"
            ),
        )
    return names


@router.get("/deals")
def deals_list(
    status: Optional[str] = None,
    repo: Optional[str] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Завершённые дела: списком, группа — `repo`, свежие сверху.

    `?status=done,archived` — какие терминальные статусы показать (по умолчанию
    оба): это весь набор состояний, в которых дело уже сделано. `?repo=<путь>` —
    только один репозиторий; `?repo=` (пустая строка) — ровно дела **без**
    репозитория; неизвестный `repo` — пустой список, а не 404 (репозиторий —
    свойство дела, а не адрес). `?limit=N` — сколько строк отдать (по умолчанию
    50, не больше 500): `total` и `truncated` называют, сколько дел подошло под
    фильтр целиком. `?board=` — отказ (400) с подсказкой: доск нет.

    `?cursor=<finished_at>:<id>` — курсорная пагинация: отдать только дела
    **строго старше** названного (ключ — пара «факт закрытия + id», тот же, по
    которому список отсортирован). `next_cursor` в ответе — ключ последней строки
    страницы, `null` — страница последняя. Смещение (`offset`) не заводится
    намеренно: список меняется между запросами (новое закрытие сдвигает окно), и
    страница «2» по смещению показывала бы повтор или пропуск. Кривой курсор —
    отказ 400 с форматом, а не пустой список.

    Строка несёт `finished_at`, `cause`, `stages`, `attempts`, `questions`,
    `published` и `duration_sec` — те же факты журнала, что у
    `GET /deal/{deal_id}` и `GET /duty/{deal_id}/attempts`, поэтому числа строки и
    карточки одного дела совпадают. Создаётся этим списком ничего: чтение идёт
    `?mode=ro`, файла store роут не создаёт.
    """
    _refuse_board_filter(board)
    statuses = _finished_statuses(status)
    shown = DEFAULT_DEALS_LIMIT if limit is None else int(limit)
    if shown <= 0 or shown > MAX_DEALS_LIMIT:
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=(
                f"?limit={shown}: ожидалось 1..{MAX_DEALS_LIMIT} — список завершённых "
                "дел не выгружается целиком"
            ),
        )
    return collect_deals(
        _hermes_home(), _now_sec(), statuses=statuses, repo=repo, limit=shown, cursor=cursor
    )


@router.get("/now")
def now(
    repo: Optional[str] = None,
    state: Optional[str] = None,
    board: Optional[str] = None,
) -> dict[str, Any]:
    """Активные дела: лизинг, попытка, последнее действие, ожидание.

    `?repo=<путь>` — только один репозиторий; `?repo=` (пустая строка) — только
    дела **без** репозитория; `?state=<waiting.state>` — только дела в этом
    состоянии ожидания. Неизвестный `repo` — это фильтр, а не ресурс: ответ 200 с
    пустым списком, ровно как у `/stats` (`docs/SPEC.md`).
    `?board=` — отказ (400) с подсказкой: доск нет, фильтр называется `repo`.

    Прохода ingest больше нет (`?ingest=1` удалён вместе с досками): срез только
    читает, журнал открывается `?mode=ro` и файла не создаёт.
    """
    _refuse_board_filter(board)
    if state is not None and state not in WAITING_PRIORITY:
        raise HTTPException(
            status_code=400,
            headers=NO_STORE_HEADERS,
            detail=f"unknown state '{state}', expected one of {list(WAITING_PRIORITY)}",
        )
    home = _hermes_home()
    return collect_now(home, _now_sec(), repo=repo, state=state)


@router.post("/ask/{deal_id}")
def ask_deal(deal_id: str, payload: dict[str, Any] = Body(...)) -> Any:
    """Явный канал вопроса по делу: пул `messaging/pool.db` плюс событие журнала.

    Кто и когда зовёт: человек или координатор (из UI/CLI), когда дело стоит в
    `blocked`/`needs_input` **без** живого вопроса — тогда `/now` честно говорит
    `waiting.human_kind="status"`, а этот роут превращает «нужен человек» в
    конкретный вопрос со вариантами ответа. Автоматически вопросы не создаются:
    пул решений не должен наполняться проходами по срезу.

    Адрес — только `d_<hex>` (0.0.6): доски, которая связывала дело с карточкой,
    больше нет, и `POST /ask/{board}/{task_id}` не алиасится — два способа
    адресовать одно дело дали бы две правды.

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
    resolved = _resolve_deal(home, deal_id)

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

    deal = {"id": deal_id, "repo": resolved.get("repo"), "home": home}
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
                "errors": [{"source": ask.QUESTION_SOURCE, "reason": f"ask {deal_id}: {exc}"}],
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


# Границы этапов на таймлайне: начало и конец попытки. `attempt_progress` сюда
# не входит намеренно — его на два порядка больше, а рисунку он не нужен.
SPAN_KINDS = ("attempt_started", "attempt_ended")
# Потолок дел в одном запросе: страница спрашивает разом за все дорожки, и
# «пришли весь store» было бы приглашением к запросу на 500 дел.
SPAN_MAX_DEALS = 40


def _spans_of(events: list[dict[str, Any]], now: int) -> list[dict[str, Any]]:
    """Полосы этапов дела: пара `attempt_started` ↔ `attempt_ended` по `run_id`.

    Конец без начала, как и в ленте попыток, — мусор и пропускается: полоса без
    начала не рисуется. Незакрытая попытка остаётся с `end: null` — поверхность
    тянет её до `now` сама, потому что «кончилась сейчас» было бы догадкой.

    `outcome`/`kind` берутся из `attempt_ended`: по ним полоса красится (успех,
    провал, инфраструктура), поэтому они едут вместе с границами, а не читаются
    поверхностью из другого ответа.
    """
    opened: dict[str, dict[str, Any]] = {}
    spans: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        stage = str(payload.get("stage") or "")
        attempt = payload.get("attempt")
        run_id = str(payload.get("run_id") or "") or f"{stage}#{attempt}"
        kind = str(event.get("kind") or "")
        if kind == "attempt_started":
            if run_id in opened:
                continue
            opened[run_id] = {
                "stage": stage,
                "role": str(payload.get("contract", {}).get("role") or "") if isinstance(payload.get("contract"), dict) else "",
                "attempt": attempt if isinstance(attempt, int) else None,
                "start": int(event.get("ts") or 0),
                "end": None,
                "outcome": None,
                "kind": None,
            }
            continue
        span = opened.pop(run_id, None)
        if span is None:
            continue
        span["end"] = int(event.get("ts") or 0)
        span["outcome"] = str(payload.get("outcome") or "") or None
        span["kind"] = str(payload.get("kind") or "") or None
        spans.append(span)
    spans.extend(opened.values())
    spans.sort(key=lambda item: (item["start"], item["stage"]))
    return spans


@router.get("/spans")
def spans(ids: str = "", board: Optional[str] = None) -> dict[str, Any]:
    """Границы этапов для нарисованных дорожек: `{"d_x": [{stage,start,end,…}]}`.

    Одна ручка на много дел, потому что таймлайн показывает их рядом: запрос на
    дело превратил бы верхушку страницы в 14 запросов. `missing` называет дела,
    которых в store нет, — поверхность не должна молча рисовать пустую дорожку.
    """
    wanted = [item.strip() for item in str(ids or "").split(",") if item.strip()]
    if len(wanted) > SPAN_MAX_DEALS:
        raise HTTPException(
            status_code=400,
            detail=f"ids: не больше {SPAN_MAX_DEALS} дел за запрос, пришло {len(wanted)}",
        )
    _refuse_board_filter(board)
    home = _hermes_home()
    now = _now_sec()
    if not wanted:
        return {"now": now, "count": 0, "spans": {}, "missing": []}
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 - свой store не открылся
        raise HTTPException(
            status_code=503,
            headers=NO_STORE_HEADERS,
            detail=f"store unreadable: {type(exc).__name__}: {exc}",
        ) from exc
    try:
        events = journal.read_kinds(conn, wanted, SPAN_KINDS)
        known = {
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM deals WHERE id IN (" + ",".join("?" * len(wanted)) + ")",
                tuple(wanted),
            )
        }
    finally:
        conn.close()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(str(event.get("deal_id") or ""), []).append(event)
    return {
        "now": now,
        "count": len(wanted),
        "spans": {deal_id: _spans_of(items, now) for deal_id, items in grouped.items() if deal_id},
        "missing": [deal_id for deal_id in wanted if deal_id not in known],
        "note": "",
    }


@router.get("/duty/{deal_id}/attempts")
def duty_attempts(deal_id: str) -> dict[str, Any]:
    """Таймлайн попыток дела: все попытки по возрастанию старта плюс их события.

    Попытки строятся по журналу: движок спавнит процесс и пишет
    `attempt_started`/`attempt_progress`/`attempt_ended`, а живость незакрытой
    попытки берётся из лизинга — без `ps` и `/proc` (`docs/PROBES.md`). Форма:
    этап, номер, начало, конец, длительность, исход, класс отказа, причина,
    `base`, `commit`, `pid`, `reaped`, число событий прогресса — чтобы консоль и
    страница рисовали одну форму, а не две.

    Адрес — только `d_<hex>` (0.0.6). Попыток карточек kanban здесь нет: карточка
    не дело, и её `task_runs` — наблюдение (`/cards`), а не таймлайн дела.

    Пустой список — законный ответ: дело существует, и «попыток не было» — это
    факт о нём, а не отсутствие дела. `note` в этом случае называет настоящую
    причину.
    """
    return _own_attempts(_hermes_home(), deal_id)


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
        "stage_kind": row["stage_kind"],
        "attempt": row["n"],
        "status": status,
        "start": row["started_at"],
        "end": ended_at,
        "started_at": row["started_at"],
        "ended_at": ended_at,
        "duration_sec": duration,
        "exit": row["exit"],
        "outcome": row["outcome"],
        "kind": row["kind"],
        # Имя класса словами: слаг (`infra`, `contract`) ничего не говорит человеку,
        # а перевод в каждой поверхности заново разъехался бы. Текст приходит
        # готовым — обе поверхности только печатают его.
        "kind_text": failures.describe_class(row["kind"]),
        "reason": reason,
        "error": reason,
        "summary": "",
        "base": row["base"],
        "commit": row["commit"],
        "pid": row["pid"],
        "reaped": row["reaped"],
        # Живость лизинга отдельным полем: `status` называет попытку
        # (`running`/`orphan`/`unknown`), а поверхность ещё и пишет, почему она
        # так решила — «идёт» или «без лизинга». Без этого поля экран не отличал
        # идущую попытку от неизвестной и говорил «неизвестно» про живую (09.11).
        "lease": row.get("lease"),
        "lease_expires": row.get("lease_expires"),
        "live": row.get("live"),
        "dead": row.get("dead"),
        "progress": row["progress"],
        # Израсходовано токенов попыткой. Поле было в срезе этапов (`stagefeed`),
        # но не в этом ответе — и таймлайн на экране дела всегда печатал «—» с
        # подписью «телеметрия не писала», хотя числа лежали в журнале
        # (`details.usage_tokens`). `None` — у попытки телеметрии правда нет
        # (shell-этапы её не пишут), и это не ноль.
        "tokens": row.get("tokens"),
        # Вызовы инструментов из телеметрии (`attempt_progress`): по паре
        # «старт ↔ результат» на вызов. Без них таймлайн рисует только полосу
        # попытки — а вопрос «что она делала» остаётся без ответа.
        "calls": row.get("calls") or [],
        "calls_omitted": row.get("calls_omitted") or 0,
        # Событий доски у своей попытки нет: шаги процесса считаны числом
        # (`progress`), а не подставлены чужими по виду строками.
        "events": [],
    }


def _own_attempts(home: Path, deal_id: str) -> dict[str, Any]:
    """Таймлайн попыток дела: журнал плюс таблица лизингов, без `ps`."""
    resolved = _resolve_deal(home, deal_id)
    try:
        conn = deals.open_store(home, create=False, readonly=True)
    except Exception as exc:  # noqa: BLE001 - свой store не открылся
        raise HTTPException(
            status_code=503,
            headers=NO_STORE_HEADERS,
            detail=f"store unreadable: {type(exc).__name__}: {exc}",
        ) from exc
    try:
        events = journal.read_events(conn, deal_id)
        # Срок из store важнее срока из `attempt_started`: лизинг продлевается
        # heartbeat'ом (`_own_leases`).
        leases = _own_leases(conn, deal_id)
    finally:
        conn.close()
    now = _now_sec()
    attempts = [_own_attempt(row, now) for row in stagefeed.attempts(events, now, leases=leases)]
    return {
        "deal_id": deal_id,
        "repo": resolved.get("repo"),
        "title": resolved.get("title"),
        "count": len(attempts),
        "crash_outcomes": list(CRASH_OUTCOMES),
        "attempts": attempts,
        # Строка итога: расход дела целиком против верхнего `budget` спеки —
        # рядом с по-попыточным расходом, а не вместо него.
        "budget": budget.describe_events(events),
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
