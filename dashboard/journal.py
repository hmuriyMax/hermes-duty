"""Свой журнал дел: события и fold по ним (контракт `docs/JOURNAL.md`).

До 0.0.3 состояние дела выводилось из изменяемых колонок kanban (`status`,
`claim_lock`, `claim_expires`, `task_runs`) — то есть из того же места, которое
отвечает и за факт, и за политику. Здесь состояние считается из событий:
`state(deal) = fold(events)`.

Границы модуля по контракту:

- журнал — собственность плагина: `$HERMES_HOME/duty/duty.db`, режим WAL;
- чужие БД (`kanban.db`, `state.db`) этот модуль не открывает вовсе: с 0.0.6 у
  журнала нет моста к канбану (прохода ingest больше нет — карточки читает только
  наблюдение `/cards` в `plugin_api.py`); пул вопросов (`pool.db`) пишет только
  `ask()`, пишем мы исключительно в свой журнал;
- `fold` — чистая функция: на вход список событий, на выход состояние ожидания.
  Ни БД, ни файлов она не открывает. Внешние факты, без которых правила 4–5
  контракта не сформулировать, — часы и живость pid: это не чтение источников;
- событие адресуется парой `deal_id` + `id`: доски в схеме журнала нет (0.0.6), а
  значение доски у старых событий при пересборке переезжает в `payload.board` —
  историческим фактом остаётся оно, а не колонка.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# Каталог и файл журнала рядом с остальными БД Hermes: `duty/` — наш namespace.
JOURNAL_SUBDIR = "duty"
JOURNAL_FILE = "duty.db"

# Имя источника в `errors[]`, когда не читается свой журнал: путь, как у остальных
# источников (`messaging/pool.db`), а не слаг доски — иначе «доска duty» читалась
# бы как ещё одна доска kanban.
JOURNAL_SOURCE = f"{JOURNAL_SUBDIR}/{JOURNAL_FILE}"

# Сколько ждать чужую запись, а не сколько живёт запрос (как в `plugin_api`).
BUSY_TIMEOUT_MS = 2000

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
#
# `review_requested`/`changes_requested`/`collected` — переходы своих дел
# (`docs/OWNERSHIP.md`): сдано в ревью, возвращено на доработку, ревью пройдено.
# `gate_opened`/`gate_closed` — переход гейта по родителям (своё дело стало
# `ready` или снова `todo`): `ready` не ставится руками, и переход обязан быть
# виден фактом, а не только колонкой.
# `patched` — правка спецификации своего дела (title/body/assignee).
# `commented`/`attached` — сами по себе факты ожидания не создают, но нужны
# таймлайну: без них «почему дело стоит» теряет последнее действие человека.
# `attempt_progress` — шаг попытки движка (вызов инструмента или его результат):
# состояние дела он не меняет, но без него «что делает прямо сейчас» не видно в
# журнале и разбор зависаний снова требует ps и хвостов логов.
# `released` — движок отпустил дело, оставив его `running`: попытка закончилась,
# а продолжения не было (`dashboard/release.py`). Факт меняет строку store
# (`running` → `ready`/`todo`), поэтому в `NON_STATE_KINDS` его нет.
# `plan_proposed`/`plan_accepted`/`plan_rejected`/`plan_gate` — план дела, который
# растёт из журнала (`docs/ENGINE-1.0.md`, `docs/JOURNAL.md`): спека на диске
# остаётся seed-ом, а принятый план живёт фактами. Предложение граф не меняет,
# принятие — меняет; отказ возвращает этап плана в работу, гейт плана ждёт человека.
# `plan_rethink` — диагноз упавшего этапа, который движок отдаёт лиду на
# переосмысление: ревизия плана — лишь один из исходов, лид сначала думает.
EVENT_KINDS = (
    "created",
    "claimed",
    "lease_lost",
    "attempt_started",
    "attempt_progress",
    "attempt_ended",
    "tree_reused",
    "released",
    "blocked",
    "unblocked",
    "completed",
    "review_requested",
    "changes_requested",
    "collected",
    "gate_opened",
    "gate_closed",
    "patched",
    "commented",
    "attached",
    "question_asked",
    "question_answered",
    "published",
    "plan_proposed",
    "plan_rethink",
    "plan_accepted",
    "plan_rejected",
    "plan_gate",
)

# Виды, которые живое ожидание не открывают (правило 12 `docs/JOURNAL.md`):
# комментарий, вложение и правка спецификации — факты таймлайна, а не перехода;
# `question_answered` — закрывающий факт (ответ на вопрос, в том числе пришедший
# уже после `completed`); `attempt_progress` — телеметрия попытки, а не факт о
# деле (движок пишет её по каждому вызову инструмента); `plan_proposed` — факт о
# **графе**, а не о состоянии: предложение ещё не план, этап плана закрыт своим
# `attempt_ended`. Закрытость и гейт считаются по последнему state-changing
# событию, поэтому любое из них поверх `completed` закрытое дело не воскрешает.
# Набор один на весь модуль: расползаться по `if` в вызывающих это правило не
# должно.
NON_STATE_KINDS = (
    "commented",
    "attached",
    "patched",
    "question_answered",
    "attempt_progress",
    "tree_reused",
    "published",
    "plan_proposed",
)

# Приговор ревью как факт графа: действие из структурного поля отчёта
# (`result.json:verdict.action`) и вид журнального события, которым оно
# становится. Словарь один на плагин: писатель (`runner.report_verdict`) и
# читатели (`transit._fold`, `runner.previous_reports`) не должны расходиться в
# том, какой факт означает «одобрено», — иначе гейт графа снова встанет навсегда
# в ожидании приговора, которого никто не пишет.
VERDICT_ACTION_APPROVE = "approve"
VERDICT_ACTION_CHANGES = "changes"
VERDICT_APPROVE = "collected"
VERDICT_CHANGES = "changes_requested"
VERDICT_FACTS = {
    VERDICT_ACTION_APPROVE: VERDICT_APPROVE,
    VERDICT_ACTION_CHANGES: VERDICT_CHANGES,
}

# Куда ведёт одобрение, когда отчёт не назвал этап: дальше по графу.
VERDICT_NEXT = "next"


def verdict_kind(action: str) -> str:
    """Вид факта по действию приговора; неизвестное действие — пустая строка."""
    return VERDICT_FACTS.get(str(action or "").strip().lower(), "")


def verdict_action(kind: str) -> str:
    """Действие приговора по виду факта; не приговор — пустая строка.

    Обратное чтение того же словаря: `previous_reports` называет вердикт
    предшественника словом отчёта (`approve`/`changes`), а не видом события.
    """
    for action, fact in VERDICT_FACTS.items():
        if kind == fact:
            return action
    return ""


# Вид `block_kind` парковки (`dashboard/failures.py`): попытки исчерпаны, дело
# ждёт решения человека. Отдельного вида события для парковки нет — она выражена
# существующим `blocked` (решение координатора по W7), поэтому набор видов
# остаётся закрытым, а состояние ожидания — тем же `wait_human`.
FAILURES_MODULE_NAME = "hermes_duty_failures"


def _failures() -> Any:
    """`dashboard/failures.py` — словарь классов и код парковки, один на процесс."""
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


# Код вида берётся из модуля политики, а не переписывается строкой: одно слово на
# весь плагин, иначе движок и fold разошлись бы в написании.
PARKED_BLOCK_KIND = _failures().PARKED_KIND

# Виды kanban-событий, которые переносятся в журнал один-к-одному.
INGEST_EVENT_KINDS = ("created", "claimed", "blocked", "unblocked", "completed", "archived")

# Порядок применения фактов с одинаковым `ts`: внутри одной секунды сначала
# «дело появилось», потом лизинг, попытка, блок, её снятие, конец попытки и
# завершение дела. Без этого при равном `ts` порядок `id` решался бы порядком
# таблиц-источников, и fold мог прочитать завершение раньше его начала.
#
# Попытка идёт **раньше** приговора: движок пишет `collected`/`changes_requested`
# в той же транзакции, что и `attempt_ended`, и при равном `ts` факт попытки
# обязан примениться первым — иначе закрытие этапа («попытка done») сняло бы
# приговор, записанный до него, и гейт снова ждал бы решение, которого в журнале
# уже нет (`tests/test_runner.py::test_review_verdict_opens_the_gate`).
#
# `changes_requested` по-прежнему раньше `collected`: возврат на доработку и
# одобрение одного дела не должны читаться как «ревью пройдено» в обратном
# порядке. `gate_opened`/`gate_closed` — раньше `completed`, чтобы гейт не
# перечитал закрытие как открытие.
#
# `released` — между `lease_lost` и `completed`: отпускание и закрытие одного
# дела в одну секунду обязаны примениться в этом порядке, иначе `_last_state_event`
# прочитал бы закрытое дело как отпущенное (а `released` — state-changing вид).
#
# Факты плана — раньше фактов этапа: при равном `ts` граф обязан существовать
# прежде, чем по нему пойдёт срез (`transit.graph`), иначе `attempt_ended` этапа,
# добавленного планом, применился бы к графу, в котором этого этапа ещё нет.
# Внутри плана порядок — причинный: предложение → гейт (вопрос человеку) →
# решение. Обратный порядок («решение до гейта») при равном `ts` вернул бы уже
# решённый план в ожидание человека.
KIND_ORDER = (
    "created",
    "patched",
    "claimed",
    "attempt_started",
    # Прогресс — сразу после начала попытки и до её конца: с одинаковым `ts`
    # он не может примениться после смерти той же попытки.
    "attempt_progress",
    "blocked",
    "unblocked",
    "question_asked",
    "question_answered",
    "commented",
    "attached",
    "review_requested",
    "attempt_ended",
    # Факт плана — решение по отчёту попытки, а не её пролог: применяется после
    # конца той попытки, которая его принесла. Пока он стоял раньше, отказ
    # `plan_rejected` (этап плана возвращается в работу) стирался следующим за ним
    # `attempt_ended(done)` — дело `d_1cae95b3` закрылось «все этапы пройдены»,
    # отвергнув собственный план и не запустив ни одного предложенного этапа.
    # Следующей попытке граф по-прежнему виден: она начинается отдельным фактом с
    # бо́льшим `ts`, а не тем же пакетом.
    "plan_proposed",
    "plan_gate",
    "plan_accepted",
    "plan_rejected",
    # Переосмысление — реакция на исчерпание попыток этапа: лид получает диагноз
    # и решает, что делать (ревизия плана — лишь один из исходов). Факт плана:
    # применяется после конца упавшей попытки, до следующего старта.
    "plan_rethink",
    "changes_requested",
    "collected",
    "gate_opened",
    "gate_closed",
    "published",
    "lease_lost",
    "released",
    "completed",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              INTEGER NOT NULL,
  recorded_at     INTEGER NOT NULL,
  deal_id         TEXT    NOT NULL,
  kind            TEXT    NOT NULL,
  payload         TEXT    NOT NULL DEFAULT '{}',
  source          TEXT    NOT NULL,
  idempotency_key TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS events_idem ON events(idempotency_key);
CREATE INDEX IF NOT EXISTS events_deal ON events(deal_id, id);
"""

# Колонка `board` была в схеме до 0.0.6 (`NOT NULL`, дело принадлежало доске).
# Удаляется пересборкой таблицы: `DROP COLUMN` не годится, пока на базе остался
# хоть один индекс или ограничение с этим именем, а строки обязаны сохраниться
# вместе с `id` — порядок `id` и есть порядок применения фактов в `fold`.
LEGACY_BOARD_FIELD = "board"

# Куда переезжает значение снятой колонки: до 0.0.6 доска была **полем строки**, а
# не ключом `payload`, поэтому при пересборке её нужно положить в payload — иначе
# вместе с колонкой исчез бы сам факт, к какой доске относилось событие. Уже
# записанный ключ не перезаписывается (payload — свидетельство, а не место для
# догадок). Неразбираемый payload (и payload, который не объект) не выбрасывается:
# он остаётся текстом в `payload_raw` — так же, как его читает `read_events`.
_BOARD_INTO_PAYLOAD = (
    "CASE "
    "WHEN json_valid(payload) AND json_type(payload) = 'object' "
    "AND json_type(payload, '$.board') IS NOT NULL THEN payload "
    "WHEN json_valid(payload) AND json_type(payload) = 'object' "
    "THEN json_set(payload, '$.board', board) "
    "ELSE json_object('board', board, 'payload_raw', payload) "
    "END"
)

EVENTS_COLUMNS = (
    "id",
    "ts",
    "recorded_at",
    "deal_id",
    "kind",
    "payload",
    "source",
    "idempotency_key",
)

# Длина текста причины блока в evidence.
REASON_LIMIT = 200


def _now_sec() -> int:
    """Текущее время в unix-секундах — как все `*_at` в источниках."""
    return int(time.time())


def journal_path(home: Path) -> Path:
    """Путь журнала в корне Hermes: `<home>/duty/duty.db`."""
    return Path(home) / JOURNAL_SUBDIR / JOURNAL_FILE


def connect_ro(
    path: Path, timeout: float = BUSY_TIMEOUT_MS / 1000, *, settled_only: bool = False
) -> sqlite3.Connection:
    """Read-only соединение со снимком: `?mode=ro` + `BEGIN`.

    Единственный способ, которым duty открывает чужую БД: `mode=ro` не создаёт
    файл и не берёт write-lock, а `BEGIN` фиксирует одно состояние на срез.

    `settled_only` — открыть **неизменяемым** (`immutable=1`), если рядом нет
    `-wal`: тогда всё уже в самом файле, и SQLite не заводит спутники
    `-shm`/`-wal` рядом с журналом. Это важно для читающих ручек: «показал
    экран» не должно оставлять новых файлов. Есть `-wal` — обычный `?mode=ro`:
    неизменяемое чтение не увидело бы незачекпойнченные события, то есть
    срез молча отстал бы от журнала.
    """
    base = Path(path).resolve().as_uri() + "?mode=ro"
    wal = Path(path).resolve().with_name(Path(path).name + "-wal")
    if settled_only and not wal.exists():
        base += "&immutable=1"
    conn = sqlite3.connect(base, uri=True, timeout=timeout)
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
    _migrate(conn)
    return conn


def _event_columns(conn: sqlite3.Connection) -> set[str]:
    """Колонки таблицы `events` по факту схемы — то, чем проверяется миграция."""
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(events)")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Довести уже существующий журнал до схемы 0.0.6: без колонки `board`.

    `CREATE TABLE IF NOT EXISTS` колонку не убирает, а `ALTER TABLE ... DROP COLUMN`
    на `board` не проходит: колонка была `NOT NULL` и лежала в индексах базы.
    Поэтому таблица **пересобирается** (create new → copy → drop → rename) в одной
    транзакции: строки сохраняются как есть, вместе с `id` (порядок `id` — это
    порядок применения фактов в `fold`) и `idempotency_key` (по нему дедуплицируются
    мутации).

    Значение доски не теряется: до 0.0.6 `board` был полем строки, а не ключом
    payload, поэтому при пересборке он переезжает в `payload.$.board` — у события
    без этого ключа. Уже записанный ключ не перезаписывается.

    Транзакция начинается явным `BEGIN IMMEDIATE` **до** DDL: в legacy-режиме
    `sqlite3` ставит `BEGIN` только перед DML, и `CREATE TABLE events_v6` ушёл бы в
    autocommit — падение после него оставило бы пустую `events_v6` в файле.
    Остаток прерванного запуска снимается `DROP TABLE IF EXISTS` перед `CREATE`,
    поэтому база лечится повторным запуском, без ручного `DROP`.

    Идемпотентность — по проверке схемы: на мигрированной базе функция ничего не
    делает, повторный запуск стоит одного `PRAGMA table_info`.
    """
    columns = _event_columns(conn)
    if not columns or LEGACY_BOARD_FIELD not in columns:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP INDEX IF EXISTS events_idem")
        conn.execute("DROP INDEX IF EXISTS events_deal")
        conn.execute("DROP TABLE IF EXISTS events_v6")
        conn.execute(
            "CREATE TABLE events_v6 ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
            "recorded_at INTEGER NOT NULL, deal_id TEXT NOT NULL, kind TEXT NOT NULL, "
            "payload TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL, "
            "idempotency_key TEXT NOT NULL)"
        )
        # `board` появилась в 0.0.3 вместе с `payload`, поэтому переносить есть куда:
        # колонка старше самого payload в схеме журнала не бывает.
        copied = [name for name in EVENTS_COLUMNS if name in columns]
        taken = [
            _BOARD_INTO_PAYLOAD if name == "payload" else name for name in copied
        ]
        conn.execute(
            f"INSERT INTO events_v6 ({', '.join(copied)}) "
            f"SELECT {', '.join(taken)} FROM events"
        )
        conn.execute("DROP TABLE events")
        conn.execute("ALTER TABLE events_v6 RENAME TO events")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_idem ON events(idempotency_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS events_deal ON events(deal_id, id)")
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def append(conn: sqlite3.Connection, events: Iterable[dict[str, Any]]) -> int:
    """Дописать события в журнал; вернуть число реально записанных строк.

    `INSERT OR IGNORE` по уникальному `idempotency_key`: повторный проход с теми
    же фактами даёт 0, и это видно числом, а не на глаз. Пачка пишется одной
    транзакцией и в порядке `(ts, KIND_ORDER)` — потому что порядок применения в
    fold это порядок `id`, и он обязан совпадать с порядком фактов.

    `kind` проверяется по закрытому набору `EVENT_KINDS`: набор, который держится
    одной дисциплиной, — не набор. Неизвестный вид отвергается целиком до записи,
    поэтому «почти записанная» пачка в журнале не остаётся.
    """
    rows = _event_rows(events)
    if not rows:
        return 0
    before = conn.total_changes
    with conn:  # одна транзакция на пачку
        conn.executemany(_INSERT_SQL, rows)
    return conn.total_changes - before


def append_in_tx(conn: sqlite3.Connection, events: Iterable[dict[str, Any]]) -> int:
    """Дописать события **внутрь уже открытой транзакции вызывающего**.

    Мутация своего дела пишет строку store и событие одной транзакцией
    (`docs/OWNERSHIP.md`): журнал и store не должны разъехаться. `append` здесь
    не годится — он делает `commit`, и откат строки дела после него уже ничего бы
    не отменил. Транзакцией владеет вызывающий: `commit`/`rollback` — его.

    Проверка набора `kind` та же, что у `append`: неизвестный вид отвергается до
    записи, поэтому «почти записанной» пачки не остаётся и внутри чужой
    транзакции.
    """
    rows = _event_rows(events)
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(_INSERT_SQL, rows)
    return conn.total_changes - before


_INSERT_SQL = (
    "INSERT OR IGNORE INTO events "
    "(ts, recorded_at, deal_id, kind, payload, source, idempotency_key) "
    "VALUES (?,?,?,?,?,?,?)"
)


def _event_rows(events: Iterable[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """События → строки для `INSERT` в порядке `(ts, KIND_ORDER)`; пустой вход — пусто.

    Порядок и проверка набора одни на оба пути записи (`append` и
    `append_in_tx`): второй копии этих правил быть не должно. Колонки `board` в
    журнале нет с 0.0.6: дело адресуется своим `deal_id`, а `board` в payload
    старых событий остаётся историческим фактом и не переписывается.
    """
    ordered = sorted(events, key=lambda ev: (int(ev["ts"]), _kind_rank(str(ev["kind"]))))
    if not ordered:
        return []
    for ev in ordered:
        if str(ev["kind"]) not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {ev['kind']!r}: allowed {', '.join(EVENT_KINDS)}")
    return [
        (
            int(ev["ts"]),
            int(ev["recorded_at"]),
            str(ev["deal_id"]),
            str(ev["kind"]),
            json.dumps(ev.get("payload") or {}, ensure_ascii=False),
            str(ev.get("source") or "engine"),
            str(ev["idempotency_key"]),
        )
        for ev in ordered
    ]


def read_events(
    conn: sqlite3.Connection, deal_id: Optional[str] = None, since_id: int = 0
) -> list[dict[str, Any]]:
    """События журнала по возрастанию `id`; `deal_id`/`since_id` — необязательный отбор.

    `payload` разворачивается в объект: битый JSON — не повод ронять срез, но и
    не повод молчать, поэтому неразобранное поле отдаётся как есть в `payload_raw`.
    """
    sql = (
        "SELECT id, ts, recorded_at, deal_id, kind, payload, source, idempotency_key "
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


def read_kinds(
    conn: sqlite3.Connection, deal_ids: Iterable[str], kinds: Iterable[str]
) -> list[dict[str, Any]]:
    """События названных видов по названным делам — одним запросом.

    Нужно границам этапов на таймлайне: начало и конец попытки — это
    `attempt_started`/`attempt_ended`, а `attempt_progress` пишется на два порядка
    чаще и для рисунка не нужен. Отбор по видам делает SQL, а не разбор: тащить в
    память 8 000 progress-событий дела, чтобы найти в них две строки, — та же
    работа, что читать журнал целиком.
    """
    ids = [str(item) for item in deal_ids or () if str(item)]
    wanted = [str(item) for item in kinds or () if str(item)]
    if not ids or not wanted:
        return []
    sql = (
        "SELECT id, ts, recorded_at, deal_id, kind, payload, source, idempotency_key "
        "FROM events WHERE deal_id IN (" + ",".join("?" * len(ids)) + ") "
        "AND kind IN (" + ",".join("?" * len(wanted)) + ") ORDER BY deal_id, id"
    )
    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, tuple(ids + wanted)):
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


def _last_state_event(ordered: list[dict[str, Any]]) -> dict[str, Any]:
    """Последнее state-changing событие: `NON_STATE_KINDS` не в счёт.

    Проверено ли дело закрытым и остался ли от него снимок гейта — решает не
    последняя строка журнала, а последний факт, менявший состояние. Fallback на
    последнее событие нужен только на вырожденном наборе, где state-changing нет
    вовсе: пустой список сюда не приходит (fold проверил его раньше).
    """
    for event in reversed(ordered):
        if str(event.get("kind") or "") not in NON_STATE_KINDS:
            return event
    return ordered[-1]


def _closed_kind(kind: str) -> bool:
    """Закрывает ли событие дело: `completed` — единственный такой факт.

    Отдельного `kind` для архива нет (`docs/JOURNAL.md`): `archived` приходит тем
    же `completed` со `status="archived"` в payload, и потому закрытость по нему
    считается так же честно, как по `done`.
    """
    return kind == "completed"


def fold(
    events: Iterable[dict[str, Any]],
    *,
    now: Optional[int] = None,
    pid_probe: Optional[Callable[[Any], Optional[bool]]] = None,
    answer_of: Optional[Callable[[Any], Optional[str]]] = None,
) -> dict[str, Any]:
    """Состояние ожидания дела по его событиям — правила 1–13 контракта.

    Чистая функция: ни БД, ни файлов. События применяются в порядке `id`
    (правило 1). Ответ:

    - `active: False` — последнее state-changing событие `completed` (правило 2):
      дело не активно, в `/now` не попадает; `state` тогда `None`;
    - `state` из восьми состояний — сработал живой факт (правила 3–6, 8, 9, 11):
      блок, вопрос человеку, лизинг без попытки, попытка без завершения,
      снимок `created` с незакрытыми родителями;
    - `parked` — факты парковки, если открытый блок назван `block_kind='parked'`
      (попытки исчерпаны, класс, этап, число попыток). Состояние при этом остаётся
      `wait_human`: парковка ждёт **человека**, девятого состояния нет;
    - `state: None` с `reason: "empty"` — событий нет вовсе (правило 7), а с
      `reason: "no_live_fact"` — события есть, но живого факта в них нет
      (например, только `created`). В обоих случаях состояние считает
      поверхность по kanban и говорит это в `evidence`: «нет данных» нельзя
      выдавать за «всё спокойно».

    `now` подставляется тестами; по умолчанию берутся часы. `pid_probe` — тот же
    шов для живости pid: по умолчанию `pid_alive` (то есть `os.kill`), тесты
    подставляют свой ответ, чтобы результат не зависел от живых процессов.
    `answer_of` — такой же шов для ответа пула: журнал сам пула не читает
    (чистая функция), но поверхность, у которой пул открыт, может подсказать,
    что человек уже ответил — иначе доказательство говорило «ответа нет» там,
    где ответ лежит в пуле и ждёт проведения решения (кейс d_2fc749fb).
    """
    ordered = sorted(events, key=lambda ev: int(ev.get("id") or 0))
    if not ordered:
        return _verdict(None, None, None, "empty", None)
    last_kind = str(ordered[-1].get("kind") or "")
    now_sec = _now_sec() if now is None else int(now)

    # 2. Закрытость — по последнему state-changing событию, а не по последней
    #    строке журнала: виды из `NON_STATE_KINDS` (правило 12) состояния не
    #    меняют, и закрытое дело после них остаётся закрытым. Иначе дело
    #    «воскресало» бы карточкой: fold уходил в `no_live_fact` с `active: True`,
    #    и дело снова попадало в состояние ожидания. Самый частый такой хвост —
    #    `question_answered` по делу, которое успели завершить до ответа.
    changed = _last_state_event(ordered)
    progress = _progress(ordered)
    if _closed_kind(str(changed.get("kind") or "")):
        status = _payload(changed).get("status") or "done"
        return _verdict(
            False,
            None,
            f"журнал: закрыто событием completed (status={status})",
            "closed",
            last_kind,
            progress=progress,
        )

    facts: dict[str, list[str]] = {}
    question = _open_question(ordered)

    # 6. Вопрос человеку: `question_asked` без ответа. Это и есть «ждёт ответа» —
    #    блокировка без вопроса даёт тот же wait_human, но с human_kind=status.
    #    У вопроса роли номера пула нет, и его называет адресат (`routing.target`):
    #    «вопрос #—» говорил бы о потерянном номере там, где номера не было.
    if question is not None:
        payload = _payload(question[1])
        number = payload.get("question_id")
        if number:
            answer = answer_of(number) if answer_of is not None else None
            if answer:
                asked = (
                    f"вопрос #{number} задан {_ago(question[1].get('ts'), now_sec)}, "
                    f"ответ получен: {answer[:40]} — решение не проведено (`duty decide`)"
                )
            else:
                asked = (
                    f"вопрос #{number} задан {_ago(question[1].get('ts'), now_sec)}, "
                    f"ответа нет (session_key {payload.get('session_key') or '—'})"
                )
        else:
            asked = (
                f"вопрос {_question_address(payload)} задан {_ago(question[1].get('ts'), now_sec)}, "
                f"ответа нет (пул не участвует: вопрос роли)"
            )
        facts.setdefault("wait_human", []).append(asked)

    # 3. Блок без последующего `unblocked`. Доступ/права — отдельное состояние
    #    `capability`, как в 0.0.2: это не «человек решает», а «человек даёт доступ».
    #    Парковка (`block_kind='parked'`) — `wait_human` без разбора текста причины:
    #    класс и число попыток пришли полями, и словесное совпадение («token» в
    #    причине) не должно превращать её в `capability`.
    block = _open_block(ordered)
    parked_fact: Optional[dict[str, Any]] = None
    if block is not None:
        payload = _payload(block[1])
        block_kind = str(payload.get("block_kind") or "")
        reason = str(payload.get("reason") or "")
        text = f"blocked: block_kind={block_kind or '—'}, причина: {_short(reason) or 'не записана'}"
        if block_kind == PARKED_BLOCK_KIND:
            parked_fact = _parked_fact(payload)
            # Ответ человека на вопрос парковки: факт «ответ есть» без текста.
            # Чем именно ответили, называет поверхность по пулу (`/now`,
            # `/deal`): журнал хранит `answer_len`, а не текст ответа.
            parked_fact["answered"] = _answered_question(ordered, parked_fact["question_id"])
            # Ожидание называется по тому, чего не хватает: до ответа — решения
            # человека, после — той же команды, и её называет поверхность
            # (`ответ #N получен: …; нужно решение: bin/duty decide …`). Две
            # половины одного факта не повторяются: «ждёт решения» поверх
            # «ответ получен» читается как противоречие.
            needs = "" if parked_fact["answered"] else " — ждёт решения человека"
            if parked_fact["gate"]:
                # Парковка гейта: класса и счётчика у неё нет — человек не ответил
                # на вопрос этапа, и «парковка 0/0» было бы выдумкой.
                facts.setdefault("wait_human", []).append(
                    "blocked: block_kind=parked, гейт: "
                    f"{_short(reason) or 'человек не ответил'}"
                    + (f", вопрос #{parked_fact['question_id']}" if parked_fact["question_id"] else "")
                    + needs
                )
            else:
                facts.setdefault("wait_human", []).append(
                    f"blocked: block_kind=parked, парковка: "
                    f"{parked_fact['attempts']}/{parked_fact['limit']} {parked_fact['kind']}"
                    f"{' на этапе ' + parked_fact['stage'] if parked_fact['stage'] else ''}, "
                    f"причина: {_short(reason) or 'не записана'}"
                    + (f", вопрос #{parked_fact['question_id']}" if parked_fact["question_id"] else "")
                    + needs
                )
        elif block_kind == "capability" or any(word in reason.lower() for word in CAPABILITY_WORDS):
            facts.setdefault("capability", []).append(text)
        else:
            facts.setdefault("wait_human", []).append(text)

    # 4. Лизинг, за которым не началась попытка: дело «взяли» и не пришли. Факт
    #    называется «протухшим» только тогда, когда в журнале записан срок
    #    (`claim_expires`) и он уже прошёл: «протух» — это свойство срока, а не
    #    возраста строки. У своего дела срока нет вовсе (`claim` пишет `NULL`,
    #    аренды в 0.0.3 не существует), и время по такому факту ничего не
    #    доказывает — иначе ответ противоречил бы `lease.state = "none"` того же
    #    дела. Срок — данные, а не происхождение записи: проверяется он.
    claim = _last_of_kind(ordered, "claimed")
    if claim is not None:
        claim_index, claim_event = claim
        payload = _payload(claim_event)
        expires = payload.get("claim_expires")
        started = any(str(ev.get("kind")) == "attempt_started" for ev in ordered[claim_index + 1 :])
        if not started and isinstance(expires, int) and expires <= now_sec:
            facts.setdefault("dead_lease", []).append(
                f"лизинг {payload.get('claim_lock') or '—'} взят {_ago(claim_event.get('ts'), now_sec)}, "
                f"срок истёк {_ago(expires, now_sec)} — attempt_started за ним нет"
            )

    # 5. Попытка без завершения: живость pid — единственный сигнал, который в
    #    закрытом наборе событий есть. Сердцебиение в журнал не пишется, и это
    #    сказано в evidence, а не подразумевается.
    attempt = _open_attempt(ordered)
    if attempt is not None:
        payload = _payload(attempt[1])
        pid = payload.get("pid")
        alive = (pid_probe or pid_alive)(pid)
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
    #    Считается по последнему state-changing событию — комментарий или правка
    #    спецификации снимок гейта не отменяют (правило 12). Закрытие родителей
    #    видно в их собственных событиях, а не в этих, поэтому факт назван
    #    снимком («родители на момент created»), а не сегодняшним состоянием графа.
    if str(changed.get("kind") or "") == "created":
        parents = _payload(changed).get("parents") or []
        if parents:
            facts.setdefault("wait_gate", []).append(
                "родители на момент created: "
                + ", ".join(str(parent) for parent in parents)
                + " (закрытие в журнале по делу не видно)"
            )

    # 9. Сдано в ревью и вердикта ещё нет: `review_requested` без последующего
    #    `collected`/`changes_requested`. Это `wait_human` с `human_kind="status"`
    #    — ровно то, чем статус `review` уже назван в `WAITING_FALLBACK` («ждёт
    #    вердикта ревьюера»): человеку нужно решение, а не ответ на вопрос.
    #    Своим делам этот факт отвечает на «почему стоит» без чтения колонки.
    verdict = _last_of_kinds(ordered, ("review_requested", "collected", "changes_requested"))
    if verdict is not None and str(verdict[1].get("kind")) == "review_requested":
        payload = _payload(verdict[1])
        reviewer = payload.get("reviewer")
        facts.setdefault("wait_human", []).append(
            f"сдано в ревью {_ago(verdict[1].get('ts'), now_sec)}"
            + (f", ревьюер {reviewer}" if reviewer else ", ревьюер не назначен")
            + " — вердикта в журнале нет"
        )

    # 10. Возврат на доработку: `changes_requested` без последующей сдачи. Это
    #     `wait_slot`, а не `wait_human`: решение «не принято» человек уже принял,
    #     и ждёт дело исполнителя, который переделает. Приоритет состояний это
    #     подтверждает — `claim`/попытка перебивают `wait_slot`, а `wait_human`
    #     перебил бы их сам и висел бы на деле, которое уже в работе. Взятое
    #     после возврата дело (`claimed`) этот факт снимает: причина возврата
    #     осталась в таймлайне, но «ждёт исполнителя» про него уже неправда.
    #     Причину называет payload — она в evidence, а не подразумевается.
    if verdict is not None and str(verdict[1].get("kind")) == "changes_requested":
        taken = any(str(ev.get("kind")) == "claimed" for ev in ordered[verdict[0] + 1 :])
        if not taken:
            payload = _payload(verdict[1])
            facts.setdefault("wait_slot", []).append(
                f"возвращено на доработку {_ago(verdict[1].get('ts'), now_sec)}: "
                f"{_short(str(payload.get('reason') or '')) or 'причина не записана'}"
            )

    # 11. Гейт по родителям: `gate_closed` без последующего `gate_opened` — дело
    #     снова ждёт родителя. Это тот же `wait_gate`, что и у снимка `created`, но
    #     по последнему известному факту: родителя вернули из `done`, и переход
    #     назван событием, а не выведен из колонки.
    gate = _last_of_kinds(ordered, ("gate_opened", "gate_closed"))
    if gate is not None and str(gate[1].get("kind")) == "gate_closed":
        payload = _payload(gate[1])
        facts.setdefault("wait_gate", []).append(
            f"гейт закрыт {_ago(gate[1].get('ts'), now_sec)}: родитель {payload.get('parent') or '—'} "
            f"больше не закрыт (status={payload.get('to') or '—'})"
        )

    # 12. Виды из `NON_STATE_KINDS` состояния не создают намеренно: «человек
    #     оставил комментарий», «поправил title» или «пришёл ответ на вопрос» —
    #     не ожидание и не повод показывать дело ждущим. Набор назван в одном
    #     месте, и им же считается закрытость (правило 2): ни один из этих видов
    #     закрытое дело не воскрешает.
    # 13. `collected` состояние не поднимает: вердикт снял ожидание ревью, и дело
    #     возвращается к тому, что говорит его `status`. Ветки здесь нет намеренно —
    #     `collected` участвует только в выборе вердикта (правила 9–10).

    state = next((name for name in WAITING_PRIORITY if name in facts), None)
    if state is None:
        return _verdict(None, None, None, "no_live_fact", last_kind, progress=progress, parked=parked_fact)

    human_kind = None
    if state == "wait_human":
        human_kind = "question" if question is not None else "status"
        if question is None and parked_fact is not None and parked_fact["answered"]:
            # Запаркованное дело с полученным ответом: человек нужен не как
            # ответчик (ответ есть) и не «по статусу», а как тот, кто выбирает
            # continue|stop (`duty decide`). Текст ответа живёт в пуле, а не в
            # журнале, поэтому вид ожидания называют здесь, а слова — поверхность.
            human_kind = "decision"
    return _verdict(
        True, state, _evidence(facts, state), "live_fact", last_kind, human_kind, progress, parked_fact
    )


def _verdict(
    active: Optional[bool],
    state: Optional[str],
    evidence: Optional[str],
    reason: str,
    last_kind: Optional[str],
    human_kind: Optional[str] = None,
    progress: Optional[dict[str, Any]] = None,
    parked: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Ответ fold одной формы: состояние, его доказательство и причина, почему его нет.

    `progress` — счётчик телеметрии попытки (`attempt_progress`): `tools`,
    `last_tool`, `last_at`. Состояние он не меняет, но консоль читает его отсюда,
    а не из хвоста логов (`docs/JOURNAL.md`, правило 14).

    `parked` — факты парковки (класс, этап, число попыток, лимит, вопрос), если
    дело запарковано: попытки исчерпаны и человек решает, продолжать ли. Это не
    отдельное состояние ожидания (`wait_human` остаётся им), а причина, по которой
    человек нужен, — поэтому она едет рядом с состоянием, а не вместо него.
    """
    return {
        "active": True if active is None else bool(active),
        "state": state,
        "human_kind": human_kind,
        "evidence": evidence,
        "reason": reason,
        "last_kind": last_kind,
        "progress": progress,
        "parked": parked,
    }


def _progress(ordered: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Счётчик телеметрии попытки: сколько вызовов инструментов и какой последний.

    `tools` считает `tool_use` (вызов), а не `tool_result`: один вызов — один
    инструмент, и пара «вызов + результат» не должна выглядеть двумя. Мусорный
    `payload` не роняет сверку: неизвестная фаза пропускается, как и в остальных
    правилах fold. `last_at` — метка времени последнего факта телеметрии, а не
    текущее время.

    `say` (агент говорил) двигает `last_at`, но не `tools`: это не вызов
    инструмента, зато честный признак, что попытка жива и работает, — модель,
    которая десять минут думает вслух, не должна выглядеть зависшей.
    """
    tools = 0
    last_tool = ""
    last_at: Optional[int] = None
    for event in ordered:
        if str(event.get("kind") or "") != "attempt_progress":
            continue
        payload = _payload(event)
        phase = str(payload.get("phase") or "")
        if phase not in ("tool_use", "tool_result", "say"):
            continue
        if phase == "tool_use":
            tools += 1
        name = payload.get("name")
        if isinstance(name, str) and name:
            last_tool = name
        ts = event.get("ts")
        if isinstance(ts, int) and not isinstance(ts, bool):
            last_at = ts
    if last_at is None and tools == 0:
        return None
    return {"tools": tools, "last_tool": last_tool, "last_at": last_at}


def _evidence(facts: dict[str, list[str]], state: str) -> str:
    """Победитель первым, остальные сработавшие факты — через `; ещё:` по приоритету."""
    order = {name: i for i, name in enumerate(WAITING_PRIORITY)}
    others = [f"{name}: {'; '.join(texts)}" for name, texts in facts.items() if name != state]
    others.sort(key=lambda line: order.get(line.split(":", 1)[0], len(order)))
    winner = "; ".join(facts[state])
    return winner if not others else f"{winner}; ещё: " + "; ".join(others)


def _open_question(ordered: list[dict[str, Any]]) -> Optional[tuple[int, dict[str, Any]]]:
    """Последний вопрос без ответа: `question_answered` снимает вопрос по `question_id`.

    У вопроса роли (`dashboard/runner.ask_role`) номера пула нет — строки пула у
    него не существует, — и пара закрывается по порядку журнала: ответ без номера
    снимает последний открытый вопрос без номера. Иначе вопрос роли, на который
    уже ответили, висел бы «без ответа» до конца жизни дела, и состояние
    `wait_human` стало бы постоянным.
    """
    open_questions: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, event in enumerate(ordered):
        kind = str(event.get("kind") or "")
        if kind not in ("question_asked", "question_answered"):
            continue
        question_id = str(_payload(event).get("question_id") or "")
        if kind == "question_asked":
            open_questions[question_id or f"#{index}"] = (index, event)
            continue
        if question_id:
            open_questions.pop(question_id, None)
            continue
        without_number = [key for key in open_questions if key.startswith("#")]
        if without_number:
            open_questions.pop(without_number[-1], None)
    if not open_questions:
        return None
    return max(open_questions.values(), key=lambda item: item[0])


def _question_address(payload: dict[str, Any]) -> str:
    """Адрес вопроса из `payload.routing`: `role:lead` и т. п.; пусто — «адресату»."""
    routing = payload.get("routing")
    if not isinstance(routing, dict):
        return "адресату"
    return str(routing.get("target") or "") or "адресату"


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


def parked_of(events: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Факты открытой парковки дела: `blocked` с `block_kind='parked'` без `unblocked`.

    Публичная форма того же разбора, что делает `fold`: консоли и `duty why`
    нужен класс, этап и число попыток, и второй разбор этих полей разошёлся бы с
    журналом. `None` — парковки нет (или она уже снята явным решением человека).
    """
    ordered = sorted(
        (item for item in events or () if isinstance(item, dict)),
        key=lambda item: int(item.get("id") or 0),
    )
    block = _open_block(ordered)
    if block is None:
        return None
    payload = _payload(block[1])
    if str(payload.get("block_kind") or "") != PARKED_BLOCK_KIND:
        return None
    fact = _parked_fact(payload)
    fact["answered"] = _answered_question(ordered, fact["question_id"])
    return fact


def closed_fact(events: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Последний факт закрытия дела: `completed`, его время и payload.

    Предикат тот же, что у `fold` (`_closed_kind`): дело закрыто, если в журнале
    есть `completed`. Отдельного отбора «только без `stage`» здесь нет намеренно:
    `deals.close_deal` называет в payload этап, на котором стояло дело, и такой
    факт закрывает дело, а не этап — второй предикат разошёлся бы с первым и
    «закрыто» читалось бы по-разному на трёх экранах.

    `None` — дело не закрывалось фактом (даже если строка store уже `done`:
    тогда закрытие назвать нечем, и выдумывать причину нельзя).
    """
    found: Optional[dict[str, Any]] = None
    ordered = sorted(
        (item for item in events or () if isinstance(item, dict)),
        key=lambda item: int(item.get("id") or 0),
    )
    for event in ordered:
        if str(event.get("kind") or "") != "completed":
            continue
        found = {"at": int(event.get("ts") or 0), "payload": _payload(event)}
    return found


def _answered_question(ordered: list[dict[str, Any]], question_id: Any) -> bool:
    """Получен ли ответ человека на вопрос парковки — по факту `question_answered`.

    Текст ответа в журнале не хранится (`answer_len` — факт, а не текст): здесь
    только «ответ есть», а чем именно ответили, называет поверхность по пулу.
    Истёкший срок ответом не считается — его пишет `answered_via='expired'`, и
    человек ничего не решал.
    """
    number = str(question_id or "")
    if not number:
        return False
    for event in reversed(ordered):
        if str(event.get("kind") or "") != "question_answered":
            continue
        payload = _payload(event)
        if str(payload.get("question_id") or "") != number:
            continue
        return str(payload.get("answered_via") or "").strip().lower() != "expired"
    return False


def _parked_fact(payload: dict[str, Any]) -> dict[str, Any]:
    """Поля парковки из `blocked`: класс, этап, сколько попыток, лимит, правило.

    `reason` payload — текст для человека (его пишет `set_blocked`), поэтому код
    правила едет отдельным ключом `park_reason` (`retries_exhausted` /
    `too_many_failures`). `stage` — адрес отказа, а не принадлежность события
    этапу; `fold` читает такой `blocked` как факт дела.

    `gate` — парковка гейта (`dashboard/runner.park_gate`): у неё нет ни класса
    отказа, ни счётчика попыток, и текст ожидания не должен показывать «0/0».
    """
    return {
        "kind": str(payload.get("kind") or payload.get("fail_kind") or ""),
        "stage": str(payload.get("stage") or ""),
        "attempts": _number(payload.get("attempts")),
        "limit": _number(payload.get("limit")),
        "reason": str(payload.get("park_reason") or ""),
        "question_id": payload.get("question_id"),
        "gate": bool(payload.get("gate")),
    }


def _number(value: Any) -> int:
    """Целое из payload: `True` числом не считается, мусор — нулём."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def _last_of_kinds(
    ordered: list[dict[str, Any]], kinds: tuple[str, ...]
) -> Optional[tuple[int, dict[str, Any]]]:
    """Индекс и событие последнего факта из набора видов — по одному проходу.

    Нужен там, где важен порядок между видами одного смысла (сдано в ревью,
    возвращено, ревью пройдено): «последний из набора» и есть текущий вердикт.
    """
    for index in range(len(ordered) - 1, -1, -1):
        if str(ordered[index].get("kind") or "") in kinds:
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


def payload_of(event: dict[str, Any]) -> dict[str, Any]:
    """`payload` события объектом — публичная форма `_payload` для соседних модулей.

    Своё дело (`dashboard/deals.py`) читает `block_kind` из журнала, и второй
    разбор JSON с другой трактовкой мусора разошёлся бы с fold.
    """
    return _payload(event)


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


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Есть ли таблица в открытой БД (для понятной причины вместо `no such table`)."""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchall()
    return bool(rows)
