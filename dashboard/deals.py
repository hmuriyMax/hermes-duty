"""Свой store дел и мутации по контракту `docs/OWNERSHIP.md`.

Duty до 0.0.3 только наблюдал kanban. Здесь он **владеет** делом: заводит его,
кладёт спеку, комментирует, блокирует, берёт в работу, закрывает. Движок (аренда,
спавн воркеров, ретраи) остаётся следующей версии — этот модуль про store и
переходы, а не про исполнение.

Границы модуля:

- таблицы `boards`/`deals`/`deal_links`/`comments`/`attachments` живут в **той же**
  `duty/duty.db`, что и журнал (`dashboard/journal.py`), отдельными таблицами;
  схема `events` не меняется;
- каждая мутация — **одна транзакция**: строка store и событие журнала вместе.
  Журнал пишется `journal.append_in_tx` (без собственного `commit`), поэтому сбой
  записи события откатывает и строку: две правды не разъезжаются;
- id своих дел — `d_<hex>`, kanban-дел — `t_<hex>`; префикс проверяется на входе,
  и `origin='kanban'` строк в `deals` не появляется вовсе (наблюдение остаётся в
  журнале);
- `deals.status` — намерение жизненного цикла; «почему стоит» по-прежнему считает
  `fold` журнала. `ready` не ставится руками: он вычисляется по родителям, и
  каждый переход гейта пишется событием (`gate_opened`/`gate_closed`);
- чужие БД (`kanban.db`, `state.db`, `pool.db`) этот модуль не открывает: о
  досках он знает только по слагам, которые ему передали (`known_boards`).

Повторный вызов с теми же аргументами — no-op: `idempotency_key` события
(`<source>:<deal_id>:<kind>:<sha1(аргументы)>`), поэтому повтор даёт `events: 0` и
не двигает ни `status`, ни `updated_at`, ни комментарии. Исключение одно и
названо вслух: `create` не дедуплицируется — id порождает сам вызов, и «ещё одно
такое же дело» это второе дело, а не повтор (как в kanban).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

# `dashboard/journal.py` рядом: плагин грузит модули по пути, пакета нет, поэтому
# журнал берём из `sys.modules` под тем же именем, что и остальной код, — второй
# загрузки (и второй схемы) в процессе быть не должно.
JOURNAL_MODULE_NAME = "hermes_duty_journal"


def _journal() -> Any:
    """`dashboard/journal.py` — модуль журнала, один на процесс."""
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


journal = _journal()

# Своё пространство id: `d_<hex>`; kanban-дела — `t_<hex>` (`docs/OWNERSHIP.md`).
OWN_PREFIX = "d_"

# Жизненный цикл дела (`deals.status`) — закрытый набор контракта.
DEAL_STATUSES = ("todo", "ready", "running", "blocked", "review", "done", "archived")

# Статусы, в которых дело закрыто: его не гейтит незавершённый родитель и оно не
# попадает в «дела сейчас».
CLOSED_STATUSES = ("done", "archived")

# Статусы, которые пересчитываются гейтом по родителям: пока дело не взято в
# работу, оно `todo`/`ready`, и решает это граф, а не рука.
GATED_STATUSES = ("todo", "ready")

# Единственный статус, который разрешено ставить через PATCH: остальные — только
# командами (`docs/OWNERSHIP.md`), иначе журнал перестал бы объяснять переходы.
PATCH_STATUSES = ("archived",)

# Сколько байт вложения принимаем: предел назван числом, а не «как получится».
ATTACH_LIMIT_BYTES = 25 * 1024 * 1024

# Схема — дословно из `docs/OWNERSHIP.md`. `REFERENCES deals(id)` держится
# `PRAGMA foreign_keys = ON` на соединении (`open_store`).
SCHEMA = """
CREATE TABLE IF NOT EXISTS boards (
  slug       TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
  id         TEXT PRIMARY KEY,
  board      TEXT NOT NULL REFERENCES boards(slug),
  title      TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  assignee   TEXT,
  status     TEXT NOT NULL,
  origin     TEXT NOT NULL DEFAULT 'duty',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deal_links (
  parent_id TEXT NOT NULL REFERENCES deals(id),
  child_id  TEXT NOT NULL REFERENCES deals(id),
  PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS comments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id    TEXT NOT NULL REFERENCES deals(id),
  author     TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id      TEXT NOT NULL REFERENCES deals(id),
  filename     TEXT NOT NULL,
  path         TEXT NOT NULL,
  content_type TEXT,
  size_bytes   INTEGER NOT NULL,
  created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS deals_board ON deals(board, status);
CREATE INDEX IF NOT EXISTS comments_deal ON comments(deal_id, id);
CREATE INDEX IF NOT EXISTS attachments_deal ON attachments(deal_id, id);
"""

# Каталог вложений рядом с БД: `<home>/duty/attachments/<deal_id>/`.
ATTACH_SUBDIR = "attachments"


class DealError(RuntimeError):
    """Мутация отвергнута: причина в тексте и уходит в `errors[]` ответа.

    Отдельный тип, а не `ValueError`, потому что «дела нет» и «кривой вход» —
    разные ответы (404 и 400), и оба обязаны быть названы словами: битый вход не
    должен превращаться в 500 с трассировкой.
    """


class DealNotFound(DealError):
    """Дела (или доски) нет — 404, код возврата CLI 1."""


def _now_sec() -> int:
    """Текущее время в unix-секундах — как все `*_at` в журнале и сторе."""
    return int(time.time())


def store_path(home: Path) -> Path:
    """Файл store — тот же `duty/duty.db`, что у журнала."""
    return journal.journal_path(home)


def open_store(home: Path, *, create: bool = True) -> Optional[sqlite3.Connection]:
    """Соединение со store: `create=True` заводит файл, схему журнала и схему дел.

    Читающие роуты (`/now`, `/deal`) зовут `create=False`: смотр экрана не должен
    создавать файл и таблицы как побочный эффект, иначе «посмотрел» и «включил
    store» стали бы одним действием. Отсутствие файла — не ошибка, а «дел ещё
    нет»: возвращается `None`.
    """
    conn = journal.open_journal(home, create=create)
    if conn is None:
        return None
    # FK держатся только на соединении: без этого `deal_links` принял бы ребёнка,
    # которого нет, и гейт считался бы по несуществующему делу.
    conn.execute("PRAGMA foreign_keys = ON")
    if create:
        conn.executescript(SCHEMA)
    return conn


def has_store(conn: Optional[sqlite3.Connection]) -> bool:
    """Есть ли таблицы дел в открытой БД (журнал мог появиться раньше store)."""
    if conn is None:
        return False
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='deals'").fetchone()
    return row is not None


# ------------------------------------------------------------ id и валидация


def own_id(deal_id: Any) -> str:
    """id своего дела: `d_<hex>`; иначе `DealError` с названной причиной.

    Проверка префикса — инвариант контракта: `t_…` там, где ждут своё дело,
    означало бы запись в store строки наблюдения, а `origin='kanban'` в `deals`
    не появляется.
    """
    value = str(deal_id or "").strip()
    if not re.fullmatch(r"d_[0-9a-f]{6,}", value):
        raise DealError(
            f"id {value!r} не своё дело: свои дела — d_<hex>, kanban-дела — t_<hex> "
            "(store свои и наблюдаемые не смешивает)"
        )
    return value


def kanban_id(deal_id: Any) -> str:
    """id kanban-дела: `t_<hex>`; своё дело здесь — `DealError`.

    Нужен там, где по аргументу должно быть чужое (наблюдаемое) дело: store на
    такую строку не заводится, и отказ обязан быть словами, а не молчанием.
    """
    value = str(deal_id or "").strip()
    if not re.fullmatch(r"t_[0-9a-f]{6,}", value):
        raise DealError(
            f"id {value!r} не kanban-дело: здесь ждут t_<hex>, а d_<hex> — свои дела "
            "(их ведёт store, а не kanban)"
        )
    return value


def is_own(deal_id: Any) -> bool:
    """Своё ли это дело по префиксу id (`d_<hex>`) — без исключения на чужой.

    Отдельно от `own_id`, потому что здесь решается ветка «кто владеет», а не
    проверка входа: роут `/deal` спрашивает про оба пространства и не должен
    ловить исключение, чтобы понять, куда идти.
    """
    return re.fullmatch(r"d_[0-9a-f]{6,}", str(deal_id or "").strip()) is not None


def new_id() -> str:
    """Новый id своего дела: `d_` + 8 hex-символов (случайные, не время)."""
    seed = f"{time.time_ns()}|{id(object())}".encode()
    return OWN_PREFIX + hashlib.sha1(seed).hexdigest()[:8]


# -------------------------------------------------------------------- чтение


def deal_row(conn: sqlite3.Connection, deal_id: str) -> Optional[dict[str, Any]]:
    """Строка дела из store или `None`.

    `DealNotFound` здесь не бросается: чтение отвечает «нет строки», а решает,
    ошибка это или нет, вызывающий (роут — 404, `/now` — просто без дела).
    """
    row = conn.execute(
        "SELECT id, board, title, body, assignee, status, origin, created_at, updated_at "
        "FROM deals WHERE id = ?",
        (own_id(deal_id),),
    ).fetchone()
    return None if row is None else dict(row)


def deal_rows(
    conn: sqlite3.Connection, *, board: Optional[str] = None, open_only: bool = True
) -> list[dict[str, Any]]:
    """Свои дела (по умолчанию — активные), по возрастанию `created_at`."""
    sql = (
        "SELECT id, board, title, body, assignee, status, origin, created_at, updated_at FROM deals"
    )
    params: list[Any] = []
    where: list[str] = []
    if open_only:
        where.append("status NOT IN ({})".format(", ".join("?" * len(CLOSED_STATUSES))))
        params.extend(CLOSED_STATUSES)
    if board is not None:
        where.append("board = ?")
        params.append(str(board))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at, id"
    return [dict(row) for row in conn.execute(sql, tuple(params))]


def parents_of(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    """Родители дела: `id` и `status` — то, чем считается гейт."""
    rows = conn.execute(
        "SELECT d.id AS id, d.status AS status FROM deal_links l "
        "JOIN deals d ON d.id = l.parent_id WHERE l.child_id = ? ORDER BY d.id",
        (own_id(deal_id),),
    )
    return [dict(row) for row in rows]


def children_of(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    """Дети дела: `id`, `board`, `status` — те, чей гейт двигает эта мутация."""
    rows = conn.execute(
        "SELECT d.id AS id, d.board AS board, d.status AS status FROM deal_links l "
        "JOIN deals d ON d.id = l.child_id WHERE l.parent_id = ? ORDER BY d.id",
        (own_id(deal_id),),
    )
    return [dict(row) for row in rows]


def comments_of(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    """Комментарии дела по возрастанию id — таймлайн, а не колонка карточки."""
    rows = conn.execute(
        "SELECT id, deal_id, author, body, created_at FROM comments WHERE deal_id = ? ORDER BY id",
        (own_id(deal_id),),
    )
    return [dict(row) for row in rows]


def attachments_of(conn: sqlite3.Connection, deal_id: str) -> list[dict[str, Any]]:
    """Метаданные вложений дела; файлы на диске этот модуль не удаляет."""
    rows = conn.execute(
        "SELECT id, deal_id, filename, path, content_type, size_bytes, created_at "
        "FROM attachments WHERE deal_id = ? ORDER BY id",
        (own_id(deal_id),),
    )
    return [dict(row) for row in rows]


def gate_open(conn: sqlite3.Connection, deal_id: str) -> bool:
    """Открыт ли гейт: все родители закрыты (`done` или `archived`).

    Нет родителей — гейт открыт: дело без зависимостей готово к работе. Закрытым
    родителем считается и `archived`: он терминален, и держать из-за него ребёнка
    в `todo` значило бы ждать того, чего уже не будет.
    """
    return all(str(parent["status"]) in CLOSED_STATUSES for parent in parents_of(conn, deal_id))


def board_known(conn: sqlite3.Connection, slug: str, known_boards: Iterable[str]) -> bool:
    """Есть ли доска: своя (строка `boards`) или kanban-доска с этим слагом."""
    if slug in {str(item) for item in known_boards}:
        return True
    return conn.execute("SELECT 1 FROM boards WHERE slug = ?", (slug,)).fetchone() is not None


def _ensure_board(conn: sqlite3.Connection, slug: str, now: int) -> None:
    """Завести строку доски при первом своём деле: имя по умолчанию — слаг."""
    conn.execute(
        "INSERT OR IGNORE INTO boards (slug, name, created_at) VALUES (?, ?, ?)", (slug, slug, now)
    )


# ------------------------------------------------------------------- события


def event_key(source: str, deal_id: str, kind: str, args: dict[str, Any]) -> str:
    """`idempotency_key` мутации: факт аргументов, а не время (контракт журнала).

    Формат контракта (`docs/OWNERSHIP.md`): `<source>:<deal_id>:<kind>:<sha1(аргументы)>`,
    где `source` — `cli` или `api`. Тот же вызов второй раз даёт тот же ключ,
    поэтому `INSERT OR IGNORE` отбрасывает его и повторов в журнале нет.
    """
    stable = hashlib.sha1(
        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    return f"{source}:{deal_id}:{kind}:{stable}"


def _event(
    deal: dict[str, Any],
    kind: str,
    payload: dict[str, Any],
    source: str,
    args: dict[str, Any],
    now: int,
) -> dict[str, Any]:
    """Событие журнала по факту мутации своего дела."""
    return {
        "ts": now,
        "recorded_at": now,
        "deal_id": str(deal["id"]),
        "board": str(deal["board"]),
        "kind": kind,
        "payload": payload,
        "source": source,
        "idempotency_key": event_key(source, str(deal["id"]), kind, args),
    }


def _set_status(
    conn: sqlite3.Connection, deal: dict[str, Any], status: str, now: int, **fields: Any
) -> None:
    """Сменить статус (и переданные поля) дела — внутри транзакции вызывающего."""
    assignments = ["status = ?", "updated_at = ?"]
    params: list[Any] = [status, now]
    for name, value in fields.items():
        assignments.append(f"{name} = ?")
        params.append(value)
    params.append(str(deal["id"]))
    conn.execute(f"UPDATE deals SET {', '.join(assignments)} WHERE id = ?", tuple(params))


def _gate_events(
    conn: sqlite3.Connection, parent: dict[str, Any], cause: str, source: str, now: int
) -> list[dict[str, Any]]:
    """Пересчитать гейт детей и вернуть события переходов.

    Строки детей меняются здесь же — той же транзакцией, что и мутация родителя:
    иначе «родитель вернулся на доработку» и «ребёнок снова ждёт гейт» разъехались
    бы, а переход ребёнка остался бы молчаливым (`docs/OWNERSHIP.md`, инвариант 5).
    Ключ события строится от `cause` — ключа события родителя: повтор той же
    причины перехода не плодит, а новая причина (другой переход родителя) даёт
    новый ключ.
    """
    out: list[dict[str, Any]] = []
    for child in children_of(conn, str(parent["id"])):
        if str(child["status"]) not in GATED_STATUSES:
            continue
        target = "ready" if gate_open(conn, str(child["id"])) else "todo"
        if target == str(child["status"]):
            continue
        conn.execute(
            "UPDATE deals SET status = ?, updated_at = ? WHERE id = ?",
            (target, now, str(child["id"])),
        )
        kind = "gate_opened" if target == "ready" else "gate_closed"
        stable = hashlib.sha1(f"{cause}|{child['id']}|{kind}".encode()).hexdigest()
        out.append(
            {
                "ts": now,
                "recorded_at": now,
                "deal_id": str(child["id"]),
                "board": str(child["board"]),
                "kind": kind,
                "payload": {"from": str(child["status"]), "to": target, "parent": str(parent["id"])},
                "source": source,
                "idempotency_key": f"{source}:{child['id']}:{kind}:{stable}",
            }
        )
    return out


def _require_row(conn: sqlite3.Connection, deal_id: str) -> dict[str, Any]:
    """Строка дела или `DealNotFound` — одна формулировка на все мутации."""
    row = deal_row(conn, deal_id)
    if row is None:
        raise DealNotFound(f"дела {own_id(deal_id)!r} нет в store")
    return row


def _reject_archived(deal: dict[str, Any], action: str) -> None:
    """`archived` — конечное состояние: команды жизни его не воскрешают.

    Без этой проверки `complete` или `claim` по убранному с доски делу молча
    вернули бы его в активные: на экране это выглядело бы как новое дело, а в
    журнале — как продолжение старого. Возврат в работу — отдельное осознанное
    действие (`PATCH`), а не побочный эффект команды.
    """
    if str(deal["status"]) == "archived":
        raise DealError(f"{action}: дело {deal['id']} archived — с архивным делом команды не работают")


def _apply(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    events: list[dict[str, Any]],
    status: str,
    now: int,
    **fields: Any,
) -> dict[str, Any]:
    """Записать события и строку дела одной транзакцией; вернуть итог мутации.

    Это единственное место, где мутация пишет: сначала события (`INSERT OR
    IGNORE` по ключу), и только если хоть одно легло — строка. Повтор с теми же
    аргументами даёт 0 событий и не трогает store: `idempotent: true` и тот же
    `events: 0` в ответе, а не молчаливое «получилось».

    Гейт детей пересчитывается здесь же: переход ребёнка — часть той же
    транзакции, иначе он остался бы без события.
    """
    with conn:
        written = journal.append_in_tx(conn, events)
        if written == 0:
            return {"deal": deal_row(conn, str(deal["id"])), "events": 0, "idempotent": True}
        _set_status(conn, deal, status, now, **fields)
        gate = _gate_events(conn, deal, str(events[-1]["idempotency_key"]), str(events[0]["source"]), now)
        if gate:
            journal.append_in_tx(conn, gate)
    return {"deal": deal_row(conn, str(deal["id"])), "events": written, "idempotent": False}


# ------------------------------------------------------------------ мутации


def create_deal(
    conn: sqlite3.Connection,
    *,
    board: str,
    title: str,
    body: str = "",
    assignee: Optional[str] = None,
    parents: Iterable[str] = (),
    known_boards: Iterable[str] = (),
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Завести своё дело: строка store плюс событие `created` в одной транзакции.

    `status` руками не ставится: дело получает `ready`, если все родители уже
    закрыты, иначе `todo` (`docs/OWNERSHIP.md`: «`ready` ставится, когда все
    родители `done`»). Родитель, которого нет, или родитель из пространства
    `t_…` — ошибка входа, а не 500: гейт обязан считаться по своим делам.

    `create` намеренно не дедуплицируется: id порождает сам вызов, поэтому
    повтор — это второе дело (как в kanban), а не идемпотентный повтор.
    """
    now = _now_sec() if now is None else int(now)
    slug = " ".join(str(board or "").split())
    if not slug:
        raise DealError("create: поле 'board' обязательно")
    clean_title = " ".join(str(title or "").split())
    if not clean_title:
        raise DealError("create: поле 'title' обязательно")
    if not board_known(conn, slug, known_boards):
        raise DealError(
            f"create: доски {slug!r} нет — ни своей в store, ни kanban-доски с таким слагом"
        )
    parent_ids: list[str] = []
    for raw in parents:
        parent = own_id(raw)
        if deal_row(conn, parent) is None:
            raise DealError(f"create: родитель {parent!r} не найден в store")
        if parent not in parent_ids:
            parent_ids.append(parent)

    deal_id = new_id()
    deal = {"id": deal_id, "board": slug}
    created = _event(
        deal,
        "created",
        {"title": clean_title, "assignee": assignee, "status": None, "parents": parent_ids},
        source,
        {
            "board": slug,
            "title": clean_title,
            "body": str(body or ""),
            "assignee": assignee,
            "parents": parent_ids,
        },
        now,
    )
    with conn:  # одна транзакция: доска, строка дела, связи, событие
        _ensure_board(conn, slug, now)
        conn.execute(
            "INSERT INTO deals (id, board, title, body, assignee, status, origin, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (deal_id, slug, clean_title, str(body or ""), assignee, "todo", "duty", now, now),
        )
        for parent_id in parent_ids:
            conn.execute(
                "INSERT INTO deal_links (parent_id, child_id) VALUES (?, ?)", (parent_id, deal_id)
            )
        status = "ready" if gate_open(conn, deal_id) else "todo"
        _set_status(conn, deal, status, now)
        created["payload"]["status"] = status
        written = journal.append_in_tx(conn, [created])
    return {"deal": deal_row(conn, deal_id), "events": written, "idempotent": written == 0}


def patch_deal(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    status: Optional[str] = None,
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Правка дела: `title`/`body`/`assignee` и `status='archived'`.

    `id` и `origin` не переписываются, а `status` меняется только на `archived`:
    остальные переходы — команды (`claim`/`block`/`complete`/…), иначе журнал
    перестал бы объяснять, почему дело сдвинулось. Архивация пишется тем же
    `completed` со `status="archived"` — отдельного `kind` для неё нет
    (`docs/JOURNAL.md`), а правка спецификации — фактом `patched`.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    changes: dict[str, Any] = {}
    if title is not None:
        clean = " ".join(str(title).split())
        if not clean:
            raise DealError("patch: 'title' не может быть пустым")
        changes["title"] = clean
    if body is not None:
        changes["body"] = str(body)
    if assignee is not None:
        changes["assignee"] = str(assignee) or None
    if status is not None:
        if str(status) not in DEAL_STATUSES:
            raise DealError(
                f"patch: неизвестный status={status!r} — ожидалось одно из {', '.join(DEAL_STATUSES)}"
            )
        if str(status) not in PATCH_STATUSES:
            raise DealError(
                f"patch: status={status!r} через PATCH нельзя — остальные переходы командами "
                f"({', '.join(PATCH_STATUSES)} — единственное исключение)"
            )
        changes["status"] = str(status)
    if not changes:
        raise DealError("patch: нечего менять — передайте title, body, assignee или status=archived")

    archived = "status" in changes
    if archived:
        kind = "completed"
        payload: dict[str, Any] = {"status": changes["status"]}
    else:
        kind = "patched"
        payload = {"fields": sorted(changes)}
    event = _event(deal, kind, payload, source, {"changes": dict(changes)}, now)
    target = str(changes.pop("status", deal["status"]))
    return _apply(conn, deal, [event], target, now, **changes)


def add_comment(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    author: str,
    body: str,
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Комментарий человека в деле: строка `comments` плюс событие `commented`.

    Текст сохраняется как есть (переносы строк — часть текста); в журнал идёт
    длина, а не сам текст: журнал про факты, и второй копии текста в нём быть не
    должно. Повтор того же комментария (тот же автор, тот же текст) второй строки
    не создаёт — ключ идемпотентности считается от аргументов.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    who = " ".join(str(author or "").split())
    if not who:
        raise DealError("comment: поле 'author' обязательно")
    text = str(body or "").strip()
    if not text:
        raise DealError("comment: пустой комментарий не пишем")
    event = _event(
        deal, "commented", {"author": who, "len": len(text)}, source, {"author": who, "body": text}, now
    )
    with conn:
        written = journal.append_in_tx(conn, [event])
        if written == 0:
            return {"deal": deal_row(conn, str(deal["id"])), "events": 0, "idempotent": True}
        conn.execute(
            "INSERT INTO comments (deal_id, author, body, created_at) VALUES (?,?,?,?)",
            (str(deal["id"]), who, text, now),
        )
        conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (now, str(deal["id"])))
    return {"deal": deal_row(conn, str(deal["id"])), "events": written, "idempotent": False}


def set_blocked(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    kind: str,
    reason: str,
    author: str = "max",
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Заблокировать дело: `status='blocked'` плюс событие `blocked`.

    `block_kind` — типизированная причина (`needs_input`, `capability`, …): по нему
    fold отличает «нужен человек» от «нужен доступ», и подменять его текстом
    причины нельзя. `author` — кто блокирует: факт человека, а не дела.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "block")
    block_kind = " ".join(str(kind or "").split()) or "needs_input"
    text = " ".join(str(reason or "").split())
    event = _event(
        deal,
        "blocked",
        {"block_kind": block_kind, "reason": text, "author": author},
        source,
        {"kind": block_kind, "reason": text, "author": author},
        now,
    )
    return _apply(conn, deal, [event], "blocked", now)


def set_unblocked(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    author: str = "max",
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Снять блокировку: событие `unblocked` и возврат дела в очередь.

    Дело возвращается туда, откуда его выбили блоком: `ready`/`todo` по гейту, а не
    в `running` — пока оно стояло в блоке, работы не было, и выдавать её за
    продолжение значило бы врать. `previous_kind` берётся из последнего открытого
    блока, а не выдумывается.

    Повтор того же снятия — не ошибка: контракт (`docs/OWNERSHIP.md`) требует, чтобы
    повторный тот же вызов не плодил событий. Поэтому «уже снято этим же вызовом»
    отвечает `events: 0`, а «снимать нечего по другой причине» — ошибка входа:
    эти два случая нельзя валить в один ответ.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "unblock")
    previous = _open_block_kind(conn, str(deal["id"]))
    event = _event(
        deal,
        "unblocked",
        {"previous_kind": previous, "author": author},
        source,
        {"previous_kind": previous, "author": author},
        now,
    )
    if str(deal["status"]) != "blocked":
        # Повтор того же снятия — не ошибка (контракт требует, чтобы повтор не
        # плодил событий): если последний факт дела — это `unblocked` того же
        # автора и того же источника, значит снимать уже нечего, потому что
        # сняли этим же вызовом. Ключ идемпотентности здесь не годится: он
        # считается от предыдущего блока, а тот в журнале уже перекрыт.
        history = journal.read_events(conn, str(deal["id"]))
        if history and str(history[-1].get("kind")) == "unblocked":
            last = history[-1]
            if (
                str(journal.payload_of(last).get("author") or "") == author
                and str(last.get("source") or "") == source
            ):
                return {"deal": deal_row(conn, str(deal["id"])), "events": 0, "idempotent": True}
        raise DealError(
            f"unblock: дело {deal['id']} не в blocked (status={deal['status']}) — снимать нечего"
        )
    target = "ready" if gate_open(conn, str(deal["id"])) else "todo"
    return _apply(conn, deal, [event], target, now)


def claim_deal(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    owner: str,
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Взять дело в работу: `status='running'` плюс событие `claimed`.

    Аренды в 0.0.3 нет (`docs/OWNERSHIP.md`, «что НЕ входит»): `claimed` здесь
    называет владельца, а `claim_expires` остаётся `NULL` — срока у факта нет.
    Незанятому делу владелец становится исполнителем; уже назначенного не
    переписываем — роль дела выбиралась осознанно.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "claim")
    who = " ".join(str(owner or "").split())
    if not who:
        raise DealError("claim: поле 'owner' обязательно")
    event = _event(
        deal,
        "claimed",
        {"claim_lock": who, "claim_expires": None, "owner": who},
        source,
        {"owner": who},
        now,
    )
    fields = {} if deal.get("assignee") else {"assignee": who}
    return _apply(conn, deal, [event], "running", now, **fields)


def complete_deal(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    summary: str = "",
    review: bool = False,
    reviewer: Optional[str] = None,
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Завершить дело; `review=True` — сдать в ревью вместо закрытия.

    Два пути и оба одним правилом: без ревью дело пишется `completed` и уходит из
    активных (fold правило 2), с ревью — `review_requested` и `status='review'`.
    Дело, уже стоящее в ревью, закрывается здесь же: событие `collected` («ревью
    пройдено») плюс `completed` в одной транзакции — вердикт и закрытие это два
    факта, и склеивать их в один значило бы потерять, что ревью было.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "complete")
    text = " ".join(str(summary or "").split())
    if review:
        return request_review(
            conn, str(deal["id"]), reviewer=reviewer, summary=text, source=source, now=now
        )
    events = [
        _event(deal, "completed", {"status": "done", "summary": text}, source, {"summary": text}, now)
    ]
    if str(deal["status"]) == "review":
        events.insert(
            0,
            _event(
                deal,
                "collected",
                {"summary": text},
                source,
                {"summary": text, "verdict": "collected"},
                now,
            ),
        )
    return _apply(conn, deal, events, "done", now)


def request_review(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    reviewer: Optional[str] = None,
    summary: str = "",
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Сдать дело в ревью: `status='review'` плюс событие `review_requested`.

    Исполнителя (`assignee`) не трогаем: ревьюер — отдельная роль и живёт в
    payload события, иначе «кто делал» и «кто смотрит» склеились бы в одну
    колонку и одно из двух потерялось бы.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "review")
    rev = " ".join(str(reviewer or "").split()) or None
    text = " ".join(str(summary or "").split())
    event = _event(
        deal,
        "review_requested",
        {"reviewer": rev, "summary": text},
        source,
        {"reviewer": rev, "summary": text},
        now,
    )
    return _apply(conn, deal, [event], "review", now)


def request_changes(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    reason: str,
    reviewer: Optional[str] = None,
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Вернуть дело на доработку: событие `changes_requested` и снова в очередь.

    Причина обязательна: возврат без причины — это «не принято» без объяснения, и
    исполнителю пришлось бы угадывать. Дело возвращается в `todo`/`ready` по
    гейту; если оно было `done`, переход закрывает гейт его детям — и это видно
    их собственными событиями `gate_closed`, а не молчанием.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "changes")
    text = " ".join(str(reason or "").split())
    if not text:
        raise DealError("changes: причина возврата обязательна")
    rev = " ".join(str(reviewer or "").split()) or None
    event = _event(
        deal,
        "changes_requested",
        {"reason": text, "reviewer": rev},
        source,
        {"reason": text, "reviewer": rev},
        now,
    )
    target = "ready" if gate_open(conn, str(deal["id"])) else "todo"
    return _apply(conn, deal, [event], target, now)


def attach_file(
    conn: sqlite3.Connection,
    home: Path,
    deal_id: str,
    *,
    filename: str,
    content: bytes,
    content_type: Optional[str] = None,
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Приложить файл: метаданные в `attachments` плюс событие `attached`.

    Имя файла берётся только последним компонентом: путь из имени (`../../etc`) —
    не «вложение», а попытка записи мимо каталога дела. Файл ложится в
    `<home>/duty/attachments/<deal_id>/<sha256[:12]>-<имя>` — одинаковое
    содержимое не занимает места дважды и не перезаписывает соседа с тем же
    именем.

    Файл пишется **до** транзакции: ключ идемпотентности считается от `sha256`
    байт, и без записи хеша не получить. Откат метаданных при этом оставляет
    файл-сироту — осознанная цена: удалять вложения с диска контракт не велит, а
    строка, на которую он не ссылается, вреда не несёт.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    name = Path(str(filename or "")).name.strip()
    if not name or name in (".", ".."):
        raise DealError("attach: у файла нет имени")
    data = bytes(content)
    if not data:
        raise DealError("attach: пустой файл не прикладываем")
    if len(data) > ATTACH_LIMIT_BYTES:
        raise DealError(f"attach: файл больше {ATTACH_LIMIT_BYTES} байт — предел вложения")
    digest = hashlib.sha256(data).hexdigest()
    target = attach_dir(home, str(deal["id"])) / f"{digest[:12]}-{name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    event = _event(
        deal,
        "attached",
        {"filename": name, "size_bytes": len(data), "content_type": content_type, "sha256": digest[:12]},
        source,
        {"filename": name, "sha256": digest},
        now,
    )
    with conn:
        written = journal.append_in_tx(conn, [event])
        if written == 0:
            return {"deal": deal_row(conn, str(deal["id"])), "events": 0, "idempotent": True}
        cur = conn.execute(
            "INSERT INTO attachments (deal_id, filename, path, content_type, size_bytes, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (str(deal["id"]), name, str(target), content_type, len(data), now),
        )
        conn.execute("UPDATE deals SET updated_at = ? WHERE id = ?", (now, str(deal["id"])))
    attachment = conn.execute(
        "SELECT id, deal_id, filename, path, content_type, size_bytes, created_at "
        "FROM attachments WHERE id = ?",
        (int(cur.lastrowid or 0),),
    ).fetchone()
    return {
        "deal": deal_row(conn, str(deal["id"])),
        "attachment": None if attachment is None else dict(attachment),
        "events": written,
        "idempotent": False,
    }


def attach_dir(home: Path, deal_id: str) -> Path:
    """Каталог вложений дела: `<home>/duty/attachments/<deal_id>/`."""
    return Path(home) / journal.JOURNAL_SUBDIR / ATTACH_SUBDIR / own_id(deal_id)


def _open_block_kind(conn: sqlite3.Connection, deal_id: str) -> Optional[str]:
    """Вид последнего открытого блока дела — то, что снимает `unblocked`."""
    for event in reversed(journal.read_events(conn, deal_id)):
        kind = str(event.get("kind") or "")
        if kind == "blocked":
            return journal.payload_of(event).get("block_kind")
        if kind == "unblocked":
            return None
    return None
