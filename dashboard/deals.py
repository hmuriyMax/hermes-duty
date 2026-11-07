"""Свой store дел и мутации по контракту `docs/OWNERSHIP.md`.

Duty до 0.0.3 только наблюдал kanban. Здесь он **владеет** делом: заводит его,
кладёт спеку, комментирует, блокирует, берёт в работу, закрывает. Движок (аренда,
спавн воркеров, ретраи) остаётся следующей версии — этот модуль про store и
переходы, а не про исполнение.

Границы модуля:

- таблицы `deals`/`deal_links`/`comments`/`attachments` живут в **той же**
  `duty/duty.db`, что и журнал (`dashboard/journal.py`), отдельными таблицами;
  схема `events` не меняется. Там же `leases` — лизинг этапа движка
  (`dashboard/runner.py`): store у дела один, и взаимное исключение двух
  процессов живёт рядом со строкой дела, а не во второй БД;
- каждая мутация — **одна транзакция**: строка store и событие журнала вместе.
  Журнал пишется `journal.append_in_tx` (без собственного `commit`), поэтому сбой
  записи события откатывает и строку: две правды не разъезжаются;
- id своих дел — `d_<hex>`, kanban-дел — `t_<hex>`; префикс проверяется на входе,
  и `origin='kanban'` строк в `deals` не появляется вовсе (наблюдение остаётся в
  журнале);
- `deals.status` — намерение жизненного цикла; «почему стоит» по-прежнему считает
  `fold` журнала. `ready` не ставится руками: он вычисляется по родителям, и
  каждый переход гейта пишется событием (`gate_opened`/`gate_closed`);
- доски как сущности нет (0.0.6): дело адресуется своим `d_<hex>`, а группируется
  по `repo` (`deals.repo`) — репозиторию из спеки дела. `repo` вычисляется при
  создании и руками после этого не правится; `NULL` — «репозиторий не назван»,
  законное состояние, а не ошибка.

Повторный вызов с теми же аргументами — no-op: `idempotency_key` события
(`<source>:<deal_id>:<kind>:<sha1(аргументы)>`), поэтому повтор даёт `events: 0` и
не двигает ни `status`, ни `updated_at`, ни комментарии. Исключение одно и
названо вслух: `create` без внешнего имени не дедуплицируется — id порождает сам
вызов, и «ещё одно такое же дело» это второе дело, а не повтор (как в kanban).
С внешним именем (`external_ref`) дело находится повторно: живое не дублируется,
закрытое не воскрешается (`docs/OWNERSHIP.md`, «Внешнее имя дела»).
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
CREATE TABLE IF NOT EXISTS deals (
  id         TEXT PRIMARY KEY,
  -- Репозиторий дела (0.0.6): из `spec.repo` при создании, иначе из явного
  -- `--repo`, иначе NULL — «репозиторий не назван». Это группировка, а не адрес:
  -- дело адресуется своим `d_<hex>`, и по repo его не найти.
  repo       TEXT,
  title      TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  assignee   TEXT,
  status     TEXT NOT NULL,
  origin     TEXT NOT NULL DEFAULT 'duty',
  -- Внешнее имя дела (0.0.4): `jira:RP-14080`, `jira:RP-14080#2`, … Строка без
  -- разбора — формат задаёт тот, кто заводит дело. Уникального индекса нет: у
  -- одного тикета бывает несколько дел (продолжение после паузы — новое дело).
  external_ref TEXT,
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

-- Лизинг этапа движка (0.0.4): кто ведёт этап и до какого срока. Строка одна на
-- пару (дело, этап) — это и есть взаимное исключение двух процессов; продление
-- сдвигает `expires_at`, отпускание удаляет строку. Таблица заведена здесь, а не
-- в `runner.py`, потому что store у дела один: `CREATE TABLE IF NOT EXISTS` при
-- каждом открытии и есть миграция уже существующей `duty.db` (схема `events` не
-- меняется).
CREATE TABLE IF NOT EXISTS leases (
  deal_id    TEXT NOT NULL REFERENCES deals(id),
  stage      TEXT NOT NULL,
  attempt    INTEGER NOT NULL,
  pid        INTEGER NOT NULL,
  taken_at   INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (deal_id, stage)
);
CREATE INDEX IF NOT EXISTS comments_deal ON comments(deal_id, id);
CREATE INDEX IF NOT EXISTS attachments_deal ON attachments(deal_id, id);
"""

# Индексы `deals` создаются не в `SCHEMA`, а в `_migrate`: на старой базе колонки
# `repo` ещё нет, и `CREATE INDEX ... (repo, status)` упал бы до пересборки таблицы.
DEALS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS deals_repo ON deals(repo, status)",
    # До 0.0.6 строка дела принадлежала доске, и поиск живого дела по внешнему
    # имени шёл с `board = ?`. Индекс на доску больше не нужен: пересборка таблицы
    # его и так снимает, а `DROP INDEX IF EXISTS` доводит базу, где он остался.
    "DROP INDEX IF EXISTS deals_board",
)

# Каталог вложений рядом с БД: `<home>/duty/attachments/<deal_id>/`.
ATTACH_SUBDIR = "attachments"

# Колонки строки дела, которые читает и пишет store. `external_ref` появилась в
# 0.0.4, и старая `duty.db` её не имеет: `CREATE TABLE IF NOT EXISTS` колонку в
# заведённую таблицу не добавляет, а файла миграций у плагина нет — схема
# доводится при открытии на запись (`_migrate`). Читающее соединение файл не
# меняет, поэтому список колонок собирается по факту схемы (`_deal_fields`), и
# «колонки ещё нет» читается как «внешнего имени нет», а не как ошибка.
DEAL_FIELDS = (
    "id",
    "repo",
    "title",
    "body",
    "assignee",
    "status",
    "origin",
    "external_ref",
    "created_at",
    "updated_at",
)
EXTERNAL_REF_FIELD = "external_ref"
REPO_FIELD = "repo"
# Колонка, которой больше нет (0.0.6): строку дела она привязывала к доске.
BOARD_FIELD = "board"


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


def open_store(
    home: Path, *, create: bool = True, readonly: bool = False
) -> Optional[sqlite3.Connection]:
    """Соединение со store: `create=True` заводит файл, схему журнала и схему дел.

    Читающие роуты (`/now`, `/deal`) зовут `create=False`: смотр экрана не должен
    создавать файл и таблицы как побочный эффект, иначе «посмотрел» и «включил
    store» стали бы одним действием. Отсутствие файла — не ошибка, а «дел ещё
    нет»: возвращается `None`.

    `readonly=True` — соединение `?mode=ro`: store живёт в том же WAL-файле, что и
    журнал, и обычное соединение при закрытии вправе сделать checkpoint. Экран не
    имеет права менять файл хотя бы на байт, поэтому чтение и запись идут разными
    соединениями.
    """
    if readonly:
        path = journal.journal_path(home)
        if not path.is_file():
            return None
        return journal.connect_ro(path, settled_only=True)
    conn = journal.open_journal(home, create=create)
    if conn is None:
        return None
    # FK держатся только на соединении: без этого `deal_links` принял бы ребёнка,
    # которого нет, и гейт считался бы по несуществующему делу.
    conn.execute("PRAGMA foreign_keys = ON")
    if create:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return conn


def _deal_columns(conn: sqlite3.Connection) -> set[str]:
    """Колонки таблицы `deals` по факту схемы — то, чем проверяется миграция."""
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(deals)")}


def _deal_fields(conn: sqlite3.Connection) -> str:
    """Список колонок строки дела для `SELECT`: отсутствующие читаются как `NULL`.

    Читающая ручка не имеет права доводить схему (смотр экрана не меняет файл),
    поэтому колонка, которую миграция ещё не завела, подставляется как
    `NULL AS <имя>`. Иначе чтение старой `duty.db` падало бы на `no such column`,
    хотя «внешнего имени нет» и «репозиторий не назван» — законные состояния, а
    не ошибки чтения.
    """
    columns = _deal_columns(conn)
    return ", ".join(
        name if name in columns else f"NULL AS {name}" for name in DEAL_FIELDS
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """Довести уже существующую `duty.db` до схемы 0.0.6: `repo` вместо `board`.

    Отдельного файла миграций у плагина нет (`CREATE TABLE IF NOT EXISTS` при
    каждом открытии и есть миграция), но убрать колонку так нельзя. Поэтому при
    открытии **на запись** схема доводится здесь, и каждый шаг идемпотентен по
    проверке: повторный запуск на мигрированной базе не делает ничего.

    Колонка `board` была `NOT NULL` и лежала в индексе `deals_board`, а SQLite
    отказывается удалять индексированную колонку — поэтому `deals` **пересобирается**
    (create new → copy → drop → rename), а не правится `DROP COLUMN`. Пересборка
    идёт в одной транзакции и с выключенными внешними ключами: у `deal_links`,
    `comments`, `attachments` и `leases` есть `REFERENCES deals(id)`, и `DROP TABLE`
    при включённых FK упал бы на их строках. `id` копируются как есть — порядок
    `deals.id` не меняется, а `repo` у старых дел остаётся `NULL`: доски, из
    которой его можно вывести, больше нет.

    Транзакция начинается явным `BEGIN IMMEDIATE` **до** DDL: в legacy-режиме
    `sqlite3` ставит `BEGIN` только перед DML, и `CREATE TABLE deals_v6` ушёл бы в
    autocommit — падение после него оставило бы пустую `deals_v6`, а следующее
    открытие на запись падало бы на «table deals_v6 already exists». Остаток
    прерванного запуска снимается `DROP TABLE IF EXISTS` перед `CREATE`, поэтому
    база лечится повторным запуском, без ручного `DROP`.
    """
    columns = _deal_columns(conn)
    if not columns:  # таблицы ещё нет: её заведёт `SCHEMA`
        return
    rebuild = BOARD_FIELD in columns
    if rebuild or REPO_FIELD not in columns:
        # `PRAGMA foreign_keys` не действует внутри транзакции — выключаем до неё.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")  # одна транзакция на всю пересборку
            try:
                if rebuild:
                    conn.execute("DROP TABLE IF EXISTS deals_v6")
                    conn.execute(
                        "CREATE TABLE deals_v6 ("
                        "id TEXT PRIMARY KEY, repo TEXT, title TEXT NOT NULL, "
                        "body TEXT NOT NULL DEFAULT '', assignee TEXT, status TEXT NOT NULL, "
                        "origin TEXT NOT NULL DEFAULT 'duty', external_ref TEXT, "
                        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
                    )
                    # Копируются колонки, которые в старой таблице реально есть:
                    # база 0.0.3 не знает `external_ref`, и жёсткий список на ней
                    # упал бы. `repo` не переносится — его не из чего вывести.
                    copied = [
                        name for name in DEAL_FIELDS if name in columns and name != REPO_FIELD
                    ]
                    conn.execute(
                        f"INSERT INTO deals_v6 ({REPO_FIELD}, {', '.join(copied)}) "
                        f"SELECT NULL, {', '.join(copied)} FROM deals"
                    )
                    conn.execute("DROP TABLE deals")
                    conn.execute("ALTER TABLE deals_v6 RENAME TO deals")
                else:
                    conn.execute(f"ALTER TABLE deals ADD COLUMN {REPO_FIELD} TEXT")
            except Exception:
                conn.rollback()
                raise
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        for statement in DEALS_INDEXES:
            conn.execute(statement)
        # `external_ref` появилась в 0.0.4, и база 0.0.3 её не знает: колонку
        # доводит та же миграция, что и `repo` — иначе первая же запись в такую
        # базу падала бы на `table deals has no column named external_ref`.
        if EXTERNAL_REF_FIELD not in _deal_columns(conn):
            conn.execute(f"ALTER TABLE deals ADD COLUMN {EXTERNAL_REF_FIELD} TEXT")
        # Таблица досок больше не нужна: дело адресуется своим id, а группировка —
        # по `repo`. Строки досок не переносятся никуда: вывести из них репозиторий
        # нечем, и придумывать его нельзя.
        conn.execute("DROP TABLE IF EXISTS boards")


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
    проверка входа: роут `/deal` спрашивает про префикс, чтобы ответить отказом
    словами, а не поймать исключение ради ветки.
    """
    return re.fullmatch(r"d_[0-9a-f]{6,}", str(deal_id or "").strip()) is not None


def is_card(deal_id: Any) -> bool:
    """Карточка kanban ли это по префиксу id (`t_<hex>`).

    Карточка — наблюдение, а не дело (0.0.6): доски, через которую она попадала
    в адресацию, больше нет. Отдельная проверка нужна, чтобы отказ на `t_…` был
    назван причиной, а не выглядел как «дела нет».
    """
    return re.fullmatch(r"t_[0-9a-f]{6,}", str(deal_id or "").strip()) is not None


def new_id() -> str:
    """Новый id своего дела: `d_` + 8 hex-символов (случайные, не время)."""
    seed = f"{time.time_ns()}|{id(object())}".encode()
    return OWN_PREFIX + hashlib.sha1(seed).hexdigest()[:8]


def repo_of(value: Any) -> Optional[str]:
    """Репозиторий дела: путь как есть (с `~` развёрнутым), `None` — не назван.

    Источник один на оба входа (`create` API и `cli`): `spec.repo`, иначе явный
    `--repo`. Значение не проверяется на существование: группировка не обязана
    быть живым каталогом, а «репозиторий объявлен, но его нет на диске» — дело
    спеки (`duty spec check`), а не отказ создания. Нормализация ровно одна —
    `~` разворачивается, иначе один и тот же репозиторий, записанный двумя
    способами, дал бы две группы.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return str(Path(text).expanduser())


def external_ref_of(value: Any) -> Optional[str]:
    """Внешнее имя дела: строка без пробелов по краям; `None` — имени нет.

    Формат задаёт тот, кто заводит дело (`jira:RP-14080`, `jira:RP-14080#2`):
    duty его не разбирает и не сверяет с тикетом — знание о Jira живёт там, где
    дело создают. Проверяется одно: имя не пустое, иначе `external_ref=''` стал
    бы вторым способом сказать «без имени», и поиск повторно нашёл бы пустоту.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise DealError(
            "create: 'external_ref' — непустая строка (например jira:RP-14080) или null"
        )
    return text


# -------------------------------------------------------------------- чтение


def deal_row(conn: sqlite3.Connection, deal_id: str) -> Optional[dict[str, Any]]:
    """Строка дела из store или `None`.

    `DealNotFound` здесь не бросается: чтение отвечает «нет строки», а решает,
    ошибка это или нет, вызывающий (роут — 404, `/now` — просто без дела).
    """
    row = conn.execute(
        f"SELECT {_deal_fields(conn)} FROM deals WHERE id = ?",
        (own_id(deal_id),),
    ).fetchone()
    return None if row is None else dict(row)


def deal_rows(
    conn: sqlite3.Connection, *, repo: Optional[str] = None, open_only: bool = True
) -> list[dict[str, Any]]:
    """Свои дела (по умолчанию — активные), по возрастанию `created_at`.

    `repo` — фильтр группировки, а не адрес: `repo=None` — все дела, в том числе
    без репозитория. Отфильтровать «без репозитория» отдельно этим параметром
    нельзя (`NULL` значил бы «не фильтровать»), и это честно: такой фильтр ставит
    вызывающий по полю строки.
    """
    sql = f"SELECT {_deal_fields(conn)} FROM deals"
    params: list[Any] = []
    where: list[str] = []
    if open_only:
        where.append("status NOT IN ({})".format(", ".join("?" * len(CLOSED_STATUSES))))
        params.extend(CLOSED_STATUSES)
    if repo is not None:
        where.append("repo = ?")
        params.append(str(repo))
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
    """Дети дела: `id`, `status` — те, чей гейт двигает эта мутация."""
    rows = conn.execute(
        "SELECT d.id AS id, d.status AS status FROM deal_links l "
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


def live_deal_by_ref(
    conn: sqlite3.Connection,
    repo: Optional[str],
    external_ref: str,
    *,
    now: Optional[int] = None,
    window: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Живое дело с этим внешним именем в этом репозитории — то, что не дублируется.

    Закрытое (`done`/`archived`) не возвращается никогда: продолжение после паузы
    — это новое дело, и оно должно быть видно отдельно, а не как воскрешение
    старого. Нескольких живых дел с одним именем штатный путь не создаёт; если они
    есть (заведены руками), берётся самое свежее — «продолжить» возвращает то, чем
    дело кончилось в последний раз, а не первое из истории.

    Область поиска — `repo` (0.0.6; до этого была доска): один тикет может идти
    в двух репозиториях, и это два разных дела. `repo IS NULL` — своя область
    «репозиторий не назван», а не «любая»: иначе дело без repo перехватывало бы
    повтор из названного репозитория.

    `window` — окно дедупликации в секундах: если задано, живое дело, созданное
    раньше окна, не переиспользуется (заводится новое — старое продолжение видно
    отдельно). По умолчанию окна нет: любое живое дело с этим именем — то же дело.
    """
    where = [
        "repo IS ?",
        "external_ref = ?",
        "status NOT IN ({})".format(", ".join("?" * len(CLOSED_STATUSES))),
    ]
    params: list[Any] = [repo, external_ref, *CLOSED_STATUSES]
    if window is not None:
        base = _now_sec() if now is None else int(now)
        where.append("created_at > ?")
        params.append(base - int(window))
    row = conn.execute(
        f"SELECT {_deal_fields(conn)} FROM deals WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return None if row is None else dict(row)


def gate_open(conn: sqlite3.Connection, deal_id: str) -> bool:
    """Открыт ли гейт: все родители закрыты (`done` или `archived`).

    Нет родителей — гейт открыт: дело без зависимостей готово к работе. Закрытым
    родителем считается и `archived`: он терминален, и держать из-за него ребёнка
    в `todo` значило бы ждать того, чего уже не будет.
    """
    return all(str(parent["status"]) in CLOSED_STATUSES for parent in parents_of(conn, deal_id))


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
    """Событие журнала по факту мутации своего дела.

    Доски в событии нет (0.0.6): дело адресуется своим `id`, а группировка —
    по `repo` строки дела. У старых событий доска при миграции переезжает из
    колонки в `payload.board` — историческим фактом остаётся она, а новых
    событий это не касается.
    """
    return {
        "ts": now,
        "recorded_at": now,
        "deal_id": str(deal["id"]),
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
    fields: Optional[dict[str, Any]] = None,
    *,
    expired_leases_at: Optional[int] = None,
) -> dict[str, Any]:
    """Записать события и строку дела одной транзакцией; вернуть итог мутации.

    Это единственное место, где мутация пишет: сначала события (`INSERT OR
    IGNORE` по ключу), и только если хоть одно легло — строка. Повтор с теми же
    аргументами даёт 0 событий и не трогает store: `idempotent: true` и тот же
    `events: 0` в ответе, а не молчаливое «получилось».

    Гейт детей пересчитывается здесь же: переход ребёнка — часть той же
    транзакции, иначе он остался бы без события.

    `expired_leases_at` — момент, на который срок лизинга считается истёкшим:
    задан — той же транзакцией снимаются лизинги дела со сроком не позже него
    (`decide stop` бегущего дела: работа мертва, и держать её срок нечему).
    Живой лизинг не снимается никогда, поэтому условие — по сроку, а не по факту
    «останавливаем».
    """
    with conn:
        written = journal.append_in_tx(conn, events)
        if written == 0:
            return {"deal": deal_row(conn, str(deal["id"])), "events": 0, "idempotent": True}
        _set_status(conn, deal, status, now, **(fields or {}))
        if expired_leases_at is not None:
            conn.execute(
                "DELETE FROM leases WHERE deal_id = ? AND expires_at <= ?",
                (str(deal["id"]), int(expired_leases_at)),
            )
        gate = _gate_events(conn, deal, str(events[-1]["idempotency_key"]), str(events[0]["source"]), now)
        if gate:
            journal.append_in_tx(conn, gate)
    return {"deal": deal_row(conn, str(deal["id"])), "events": written, "idempotent": False}


# ------------------------------------------------------------------ мутации


def create_deal(
    conn: sqlite3.Connection,
    *,
    repo: Optional[str] = None,
    title: str,
    body: str = "",
    assignee: Optional[str] = None,
    parents: Iterable[str] = (),
    source: str = "cli",
    spec_path: Optional[str] = None,
    external_ref: Optional[str] = None,
    dedup_window: Optional[int] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Завести своё дело: строка store плюс событие `created` в одной транзакции.

    `status` руками не ставится: дело получает `ready`, если все родители уже
    закрыты, иначе `todo` (`docs/OWNERSHIP.md`: «`ready` ставится, когда все
    родители `done`»). Родитель, которого нет, или родитель из пространства
    `t_…` — ошибка входа, а не 500: гейт обязан считаться по своим делам.

    `repo` — репозиторий дела (группировка, не адрес): вызывающий вычисляет его
    из `spec.repo`, иначе из явного `--repo`, иначе оставляет `NULL` — «репозиторий
    не назван». Обязательным он не бывает: дело без репозитория законно.

    `spec_path` — спека дела (`duty.yaml`), по которой строится лента этапов
    (`docs/SPEC.md`). Путь записывается в `created.payload.spec`, потому что
    консоль читает журнал: «по какому графу завели дело» — факт дела, а не
    настройка экрана. Принимается только абсолютный путь к читаемому файлу:
    относительный не восстановить из журнала, а несуществующий сделал бы ленту
    пустой без причины.

    `external_ref` — внешнее имя дела (`jira:RP-14080`), по которому его находят
    повторно (`docs/OWNERSHIP.md`, «Внешнее имя дела»). Если живое дело с этим
    именем в этом репозитории уже есть, новое не заводится: возвращается оно с
    `created: false` и причиной в `reason` — повторный клик «продолжить» не
    плодит второе дело. Закрытое дело не воскрешается: это продолжение, и оно
    заводится новым (`created: true`). `dedup_window` ограничивает поиск свежими
    делами (секунды); по умолчанию окна нет.

    Без `external_ref` `create` намеренно не дедуплицируется: id порождает сам
    вызов, поэтому повтор — это второе дело (как в kanban), а не идемпотентный
    повтор.
    """
    now = _now_sec() if now is None else int(now)
    clean_title = " ".join(str(title or "").split())
    if not clean_title:
        raise DealError("create: поле 'title' обязательно")
    repo_name = repo_of(repo)
    spec_file = "" if spec_path is None else str(spec_path).strip()
    if spec_file:
        path = Path(spec_file).expanduser()
        if not path.is_absolute():
            raise DealError(f"create: путь к спеке должен быть абсолютным: {spec_file!r}")
        if not path.is_file():
            raise DealError(f"create: спека не читается: {path}")
        spec_file = str(path)
    parent_ids: list[str] = []
    for raw in parents:
        parent = own_id(raw)
        if deal_row(conn, parent) is None:
            raise DealError(f"create: родитель {parent!r} не найден в store")
        if parent not in parent_ids:
            parent_ids.append(parent)

    ref = external_ref_of(external_ref)
    window: Optional[int] = None
    if dedup_window is not None:
        window = int(dedup_window)
        if window < 0:
            raise DealError(
                f"create: 'dedup_window' — неотрицательное число секунд, получено {window}"
            )
    if ref is not None:
        existing = live_deal_by_ref(conn, repo_name, ref, now=now, window=window)
        if existing is not None:
            # Живое дело с этим внешним именем уже есть — повтор («продолжить»
            # нажали дважды) не заводит второе. События не пишутся: создавать
            # нечего, и «уже было» отличается от «сделано» в ответе.
            return {
                "deal": existing,
                "events": 0,
                "idempotent": True,
                "created": False,
                "reason": (
                    f"живое дело {existing['id']} с external_ref={ref!r} "
                    f"(repo={repo_name or '—'}) уже есть — вернули его (закрытое дело "
                    "с этим именем не воскрешается: продолжение заводится новым)"
                ),
            }

    deal_id = new_id()
    deal = {"id": deal_id, "repo": repo_name}
    created = _event(
        deal,
        "created",
        {
            # Группировка дела — факт создания: доски в событии больше нет (0.0.6),
            # а `repo` её заменил, и по журналу видно, к какому репозиторию дело
            # отнесли в момент заведения.
            "repo": repo_name,
            "title": clean_title,
            "assignee": assignee,
            "status": None,
            "parents": parent_ids,
            "spec": spec_file or None,
            "external_ref": ref,
        },
        source,
        {
            "repo": repo_name,
            "title": clean_title,
            "body": str(body or ""),
            "assignee": assignee,
            "parents": parent_ids,
            "spec": spec_file or None,
            "external_ref": ref,
        },
        now,
    )
    with conn:  # одна транзакция: строка дела, связи, событие
        conn.execute(
            "INSERT INTO deals (id, repo, title, body, assignee, status, origin, external_ref, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (deal_id, repo_name, clean_title, str(body or ""), assignee, "todo", "duty", ref, now, now),
        )
        for parent_id in parent_ids:
            conn.execute(
                "INSERT INTO deal_links (parent_id, child_id) VALUES (?, ?)", (parent_id, deal_id)
            )
        status = "ready" if gate_open(conn, deal_id) else "todo"
        _set_status(conn, deal, status, now)
        created["payload"]["status"] = status
        events = [created]
        if status == "ready" and parent_ids:
            # Гейт был открыт уже в момент создания, и переход пишется фактом
            # рядом со снимком `created`: по одному снимку «родители были» нельзя
            # отличить закрытый гейт от незакрытого, и дело висело бы в
            # `wait_gate` при `status=ready`. Родитель в payload — тот, чьё
            # закрытие открыло гейт последним (у него самый свежий `updated_at`);
            # при равном времени — по `id`, чтобы выбор был воспроизводим.
            last_closed = conn.execute(
                "SELECT id FROM deals WHERE id IN ({}) ORDER BY updated_at DESC, id DESC LIMIT 1".format(
                    ", ".join("?" * len(parent_ids))
                ),
                tuple(parent_ids),
            ).fetchone()
            events.append(
                _event(
                    deal,
                    "gate_opened",
                    {"from": "todo", "to": status, "parent": str(last_closed["id"]), "cause": "created"},
                    source,
                    {"cause": "created", "parents": parent_ids},
                    now,
                )
            )
        written = journal.append_in_tx(conn, events)
    return {
        "deal": deal_row(conn, deal_id),
        "events": written,
        "idempotent": written == 0,
        "created": True,
    }


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
    return _apply(conn, deal, [event], target, now, changes)


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
    details: Optional[dict[str, Any]] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Заблокировать дело: `status='blocked'` плюс событие `blocked`.

    `block_kind` — типизированная причина (`needs_input`, `capability`, `parked`, …):
    по нему fold отличает «нужен человек» от «нужен доступ», и подменять его текстом
    причины нельзя. `author` — кто блокирует: факт человека, а не дела.

    `details` — дополнительные поля факта (`kind`, `stage`, `attempts`, `limit`,
    `question_id` у парковки): блок бывает не только «нужен ввод», и причина без
    класса и числа попыток заставляла бы поверхность их пересчитывать. Поля
    уникальны и перетирать `block_kind`/`reason` не могут — они добавляются рядом.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "block")
    block_kind = " ".join(str(kind or "").split()) or "needs_input"
    text = " ".join(str(reason or "").split())
    extra = {str(key): value for key, value in (details or {}).items() if key not in ("block_kind", "reason")}
    payload = {"block_kind": block_kind, "reason": text, "author": author, **extra}
    event = _event(
        deal,
        "blocked",
        payload,
        source,
        {"kind": block_kind, "reason": text, "author": author, **extra},
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
    return _apply(conn, deal, [event], "running", now, fields)


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


# Решение человека, которым закрывается дело (`completed.payload.cause`). Класс
# нужен, чтобы закрытие человеком не читалось как закрытие движком (у того в
# payload `summary`) и чтобы «закрыто» отличалось от «остановлено» без разбора
# текста причины.
CLOSE_CAUSE_HUMAN = "closed_by_human"
CLOSE_CAUSE_STOP = "stopped_by_human"
# Причина закрытия движком: все этапы графа пройдены. Пишет её драйвер, а не
# команда: до 0.0.4 прогон объявлял «дело закрыто» строкой stdout, а в журнале
# дело оставалось живым (`status='running'` без `completed`) — движок называл
# результат, которого в системе не было.
CLOSE_CAUSE_ALL_STAGES = "all_stages_done"

# Два решения человека одной командой (`duty decide`): слова закрытого набора,
# чтобы «продолжить» и «остановить» не угадывались по тексту ответа.
DECISION_CONTINUE = "continue"
DECISION_STOP = "stop"
DECISION_WORDS = (DECISION_CONTINUE, DECISION_STOP)


def close_deal(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    reason: str = "",
    cause: str = CLOSE_CAUSE_HUMAN,
    source: str = "cli",
    now: Optional[int] = None,
    expired_leases_at: Optional[int] = None,
) -> dict[str, Any]:
    """Закрыть дело решением человека: одно `completed` и строка дела.

    Это вторая половина решения по запаркованному делу: движок паркует работу и
    не поднимает её без человека, а «остановить» до 0.0.4 записать было нечем —
    запаркованное дело с ответом «stop» оставалось запаркованным навсегда.

    Payload называет, откуда закрыли, а не догадку о том: `cause` (кто решил),
    `reason` (слова человека, как их сказали), `stage` (этап, если он был) и
    `from` (состояние ожидания до закрытия). Схема `events` не меняется:
    `completed` — существующий вид, отдельного «закрыто человеком» нет
    (`docs/JOURNAL.md`).

    Ключ идемпотентности считается от `cause` и `reason`, а не от payload:
    `from` меняется после первого закрытия, и повтор того же вызова иначе
    записал бы второй `completed`. Закрытие **другим** фактом (иная причина или
    `complete` с итогом) — не повтор: дело уже терминально, и молча переписать
    причину закрытия нельзя.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "close")
    text = " ".join(str(reason or "").split())
    events = journal.read_events(conn, str(deal["id"]))
    if str(deal["status"]) in CLOSED_STATUSES:
        last = _last_completion(events)
        if last is None or str(last.get("cause") or "") != cause or str(last.get("reason") or "") != text:
            raise DealError(
                f"close: дело {deal['id']} уже закрыто (status={deal['status']}) — "
                "закрывать нечего, а причину закрытия не переписываем"
            )
    folded = journal.fold(events, now=now)
    before = folded["state"] or str(deal["status"])
    if folded.get("human_kind"):
        before = f"{before}/{folded['human_kind']}"
    stage = str((journal.parked_of(events) or {}).get("stage") or "")
    if not stage:
        for event in reversed(events):
            candidate = str(journal.payload_of(event).get("stage") or "")
            if candidate:
                stage = candidate
                break
    payload: dict[str, Any] = {"cause": cause, "reason": text, "from": before}
    if stage:
        payload["stage"] = stage
    event = _event(deal, "completed", payload, source, {"cause": cause, "reason": text}, now)
    return _apply(conn, deal, [event], "done", now, expired_leases_at=expired_leases_at)


# Статусы, в которых дело держит человек: факт закрытия движком их не перебивает.
# `blocked` — открытый блок (движок снимает только тот, что держал вопрос),
# `review` — дело ждёт приговора ревьюера. Закрытие движком здесь значило бы
# стереть решение человека фактом «все этапы пройдены».
HUMAN_HOLD_STATUSES = ("blocked", "review")


def close_stages_done(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    stages: Iterable[str] = (),
    source: str = "engine",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Закрыть дело по факту «все этапы пройдены»: одно `completed` и строка дела.

    Это закрытие **движком**, а не человеком: `cause='all_stages_done'`, а
    `stages` называет граф, который пройден (`["impl","verify","review"]`). Второй
    причины закрытия не заводится — вид события тот же `completed`, что у
    `close`/`complete` (`docs/JOURNAL.md`).

    Идемпотентность — ключом события: он считается от одного `cause`, поэтому
    повторный прогон на уже закрытом деле даёт `events: 0` и не трогает ни
    строку, ни `updated_at`. Гонка двух прогонов решается там же, `INSERT OR
    IGNORE`: второй факт не ляжет, и второй `completed` не появится.

    Дело, которое держит человек, — отказ (`DealError`) и ни одной записи:
    «все этапы пройдены» — факт графа, а не разрешение стереть блок или ожидание
    вердикта. Блок читается **журналом** (`blocked` без `unblocked`), а не только
    строкой store: шаг движка ставит делу `running`, и по одной строке открытый
    блок человека уже не виден.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "close")
    block = _open_block_kind(conn, str(deal["id"]))
    if block is not None:
        raise DealError(
            f"close: дело {deal['id']} заблокировано ({block}) — "
            "закрытие движком не перебивает решение человека"
        )
    if str(deal["status"]) in HUMAN_HOLD_STATUSES:
        raise DealError(
            f"close: дело {deal['id']} держит человек (status={deal['status']}) — "
            "закрытие движком не перебивает решение человека"
        )
    names = [str(stage) for stage in stages]
    event = _event(
        deal,
        "completed",
        {"cause": CLOSE_CAUSE_ALL_STAGES, "stages": names},
        source,
        {"cause": CLOSE_CAUSE_ALL_STAGES},
        now,
    )
    return _apply(conn, deal, [event], "done", now)


def _last_completion(events: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Payload последнего `completed` дела — чем именно оно закрыто."""
    for event in reversed(list(events)):
        if str(event.get("kind") or "") == "completed":
            return journal.payload_of(event)
    return None


# Источник фактов движка, когда модуль не назвал его сам (`runner.ENGINE_SOURCE`).
# Своё `deals.py` не грузит `runner` — тот грузит `deals`, и обратный импорт замкнул
# бы модули друг на друга. Значение одно на плагин: строка ниже — то же слово.
ENGINE_SOURCE = "engine"


def release_deal(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    payload: dict[str, Any],
    after: int,
    source: str = ENGINE_SOURCE,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Отпустить дело: событие `released` и строка `ready`/`todo` одной транзакцией.

    Это недостающий факт «движок отпустил дело», а не второе мнение о состоянии:
    писателя у решения `transit` до 0.0.8 не было, и дело, чья попытка закончилась
    при ушедшем движке, висело `running`, пока его не заметит человек (живое дело
    `d_eb4ad2bb`: 7.5 часа `running` при записанном `attempt_ended failed/infra`).

    Отпущенное дело возвращается **в очередь**, а не в новое состояние: `ready`,
    если гейт по родителям открыт, иначе `todo` — то же правило и тот же источник,
    что у `set_unblocked`. «Почему стоит» называет журнал: `fold` после `released`
    даёт `no_live_fact`, и поверхность говорит `wait_slot` с доказательством.

    `from`/`to` дописываются здесь, а не вызывающим: их знает только строка store.
    Остальной `payload` приходит готовым (`run_id`, `stage`, `attempt`, `decision`,
    `wait`, `idle_sec`, `reason`) — пересказывать решение второй раз не нужно.

    Идемпотентность — ключом события от `after` (id последнего state-changing
    факта **до** отпускания, не считая сам `released`): повторный проход даёт тот же
    ключ, `INSERT OR IGNORE` его отбрасывает, и `updated_at` не двигается. `after`
    в payload не едет: это основание ключа, а не факт дела.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    _reject_archived(deal, "release")
    if str(deal["status"]) != "running":
        raise DealError(
            f"release: дело {deal['id']} не в работе (status={deal['status']}) — "
            "отпускать нечего: строку ставит только прерванный движок"
        )
    target = "ready" if gate_open(conn, str(deal["id"])) else "todo"
    body = {**payload, "from": str(deal["status"]), "to": target}
    event = _event(deal, "released", body, source, {"after": int(after)}, now)
    return _apply(conn, deal, [event], target, now)


def live_lease_of(
    conn: sqlite3.Connection, deal_id: str, *, now: Optional[int] = None
) -> Optional[dict[str, Any]]:
    """Живой лизинг дела или `None` — единственный признак «кто-то работает».

    Срок лизинга и есть TTL попытки: его продлевает heartbeat движка
    (`runner.follow`), поэтому «нет живого драйвера» — это отсутствие строки либо
    срок в прошлом. Живой лизинг не снимается никогда: движок может быть на другой
    машине, и `ps` про него ничего не доказывает (`docs/PROBES.md`).
    """
    at = _now_sec() if now is None else int(now)
    row = conn.execute(
        "SELECT deal_id, stage, attempt, pid, taken_at, expires_at FROM leases "
        "WHERE deal_id = ? AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
        (own_id(deal_id), at),
    ).fetchone()
    return None if row is None else dict(row)


def decide_deal(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    decision: str,
    reason: str = "",
    author: str = "max",
    source: str = "cli",
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Одно решение человека по делу, которое его ждёт: `continue` или `stop`.

    Половин решения две, и обе — факты, которые уже есть: `continue` идёт тем же
    путём, что `duty unblock` (`unblocked` со `previous_kind='parked'` снимает
    парковку и поднимает этап), `stop` закрывает дело `completed` с
    `cause='stopped_by_human'`. Своего набора правил здесь нет — команда только
    выбирает, какой из двух путей позвать, чтобы человеку не помнить два.

    `stop` разрешён и на **бегущем** деле, если у него нет живого драйвера: нет
    лизинга либо его срок прошёл (`live_lease_of`). Живой случай: попытка погибла
    при рестарте гейтвея, драйвера нет, а лизинг живёт ещё полчаса — остановить
    было нечем. Тогда закрытие пишет `completed` с `cause='stopped_by_human'` и
    тем же фактом снимает истёкший лизинг (одна транзакция). Живой лизинг —
    по-прежнему отказ: чужую работу не рвём.

    Дело, которое человека не ждёт (не запарковано, не в `blocked`, не бегущее
    без драйвера), — отказ входа и **ни одной записи**: «закрыть» без открытого
    вопроса закрывало бы работу, о которой никто не спрашивал.
    """
    now = _now_sec() if now is None else int(now)
    deal = _require_row(conn, deal_id)
    choice = " ".join(str(decision or "").lower().split())
    if choice not in DECISION_WORDS:
        raise DealError(
            f"decide: решение {str(decision or '')!r} неизвестно — ожидалось "
            f"{' или '.join(DECISION_WORDS)}"
        )
    parked = journal.parked_of(journal.read_events(conn, str(deal["id"])))
    if parked is None and str(deal["status"]) != "blocked":
        dead = choice == DECISION_STOP and str(deal["status"]) == "running"
        if not dead:
            raise DealError(
                f"decide {choice}: дело {deal['id']} не запарковано и не заблокировано "
                f"(status={deal['status']}) — решения человека оно не ждёт"
            )
        held = live_lease_of(conn, str(deal["id"]), now=now)
        if held is not None:
            raise DealError(
                f"decide stop: дело {deal['id']} ведёт живой лизинг этапа "
                f"{held['stage']!r} (pid {held['pid']}, срок до {held['expires_at']}) — "
                f"чужую работу не рвём: остановить можно после истечения лизинга"
            )
        return close_deal(
            conn,
            str(deal["id"]),
            reason=reason,
            cause=CLOSE_CAUSE_STOP,
            source=source,
            now=now,
            # Лизинг бегущего дела снимается тем же фактом, что закрытие: срок
            # прошёл, а строка осталась бы единственным «признаком работы» у
            # уже закрытого дела.
            expired_leases_at=now,
        )
    if choice == DECISION_CONTINUE:
        return set_unblocked(conn, deal_id, author=author, source=source, now=now)
    return close_deal(
        conn, str(deal["id"]), reason=reason, cause=CLOSE_CAUSE_STOP, source=source, now=now
    )


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
