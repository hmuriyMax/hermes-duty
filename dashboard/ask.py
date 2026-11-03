"""Единственный канал вопросов duty: пул `messaging/pool.db` плюс событие в журнал.

Контракт — `docs/JOURNAL.md`, раздел «`ask()`». Коротко:

- вопрос создаётся в пуле imessage-gateway по схеме пула: `session_key=t_<deal_id>`,
  `origin='duty'`, `kind='decision'`, `text` — краткая строка, `text_full` — развёрнутая,
  `options_json` — варианты ответа, если заданы;
- перед созданием ищется живой вопрос по делу; найденный возвращается как есть, дубль
  не создаётся;
- событие `question_asked` пишется в журнал **после** успешной вставки в пул: сбой пула
  не оставляет в журнале ни вопроса, ни его тени — ошибка уходит вызывающему
  (`AskError`) и превращается в `errors[]` ответа;
- `sync_answers` — обратный ход: ответ, найденный в пуле, становится фактом журнала
  (`question_answered`), ровно один раз на вопрос.

Границы модуля:

- писать в пул вправе **только** этот модуль (`ask`); остальной код duty открывает
  `pool.db` через `?mode=ro` — `journal.connect_ro` и `plugin_api._connect_ro`;
- `kanban.db` и `state.db` здесь не открываются вовсе;
- `fold` вопросов по-прежнему не читает: он работает по событиям, которые пишет этот
  модуль.

Кто и когда зовёт `ask()`:

- явный путь — `POST /ask/{board}/{task_id}` (`dashboard/plugin_api.py`): вопрос задаёт
  человек или координатор, когда дело в `blocked`/`needs_input` стоит без вопроса.
  Это и есть решение п.5 заметки 0.0.2: дело без вопроса больше не выдаёт себя за
  «ждёт ответа» (`waiting.human_kind = "status"`), а получает вопрос осознанным
  действием, а не побочным эффектом ingest;
- автоматом ingest вопросов **не** создаёт: пул решений не должен наполняться
  вопросами от каждого прохода по доске (правило пула: карточные вопросы эскалирует
  координатор, а не сканер). `POST /ingest` только дочитывает ответы (`sync_answers`).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Каталог и файл пула рядом с остальным Hermes (`messaging/pool.db`).
POOL_SUBDIR = "messaging"
POOL_FILE = "pool.db"

# Сколько ждать чужую запись в пул: пул пишет демон моста, а не мы одни.
BUSY_TIMEOUT_MS = 2000

# Атрибуты нашего вопроса в пуле (контракт `docs/JOURNAL.md`, раздел «`ask()`»).
ASK_ORIGIN = "duty"
ASK_KIND = "decision"

# Живой вопрос по делу: `sent` — по контракту, `pending` — тот же вопрос до первого
# тика демона моста (он ещё не ушёл в iMessage, но уже существует; без него второй
# `ask()` за эти секунды создал бы дубль).
LIVE_STATUSES = ("pending", "sent")

# Дело, которое ждёт человека дольше этого порога, получает `critical=1`
# (вопрос поднимается в панели и не истекает).
ASK_CRITICAL_SEC = 7200

# Срок жизни некритичного вопроса. Пул по умолчанию (`timeout IS NULL`) вопрос не
# истекает вовсе; здесь срок задан явно, иначе «вопрос смертен по обычному TTL»
# означало бы «живёт вечно».
ASK_TIMEOUT_HOURS = 24.0

# Имя источника в `errors[]`: тот же, что у остальных сообщений о пуле.
QUESTION_SOURCE = "messaging/pool.db"

# Источник события в журнале (`events.source`).
ASK_SOURCE = "ask"

JOURNAL_MODULE_NAME = "hermes_duty_journal"


def _journal() -> Any:
    """`dashboard/journal.py` — тот же модуль, что грузит `plugin_api` (`sys.modules`).

    Второй загрузки быть не должно: `append` пишет в тот же файл, и два разных
    объекта модуля означали бы две копии схемы и констант.
    """
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


class AskError(RuntimeError):
    """Пул вопросов не принял вопрос или не читается.

    Причина в тексте — её видно в `errors[]` ответа как есть: «вопроса нет» и
    «пул недоступен» — разные факты, и второй нельзя выдавать за первый.
    """


def pool_path(home: Path) -> Path:
    """Путь пула вопросов: `<home>/messaging/pool.db`."""
    return Path(home) / POOL_SUBDIR / POOL_FILE


def session_key(deal_id: str) -> str:
    """Ключ сессии пула для дела: `t_<deal_id>` (id карточки уже с префиксом `t_`)."""
    key = str(deal_id or "").strip()
    if not key:
        raise ValueError("ask: у дела нет id")
    return key if key.startswith("t_") else f"t_{key}"


def find_live(home: Path, deal_id: str) -> Optional[dict[str, Any]]:
    """Живой вопрос дела в пуле (`pending`/`sent`) или `None`.

    Читает пул `mode=ro`: сам поиск ничего не меняет, и это же правило действует
    для любого чтения пула в плагине. Отсутствие файла — не «вопросов нет», а
    `AskError`: иначе второй вопрос молча создался бы поверх недоступного пула.
    """
    db = pool_path(home)
    if not db.is_file():
        raise AskError(f"pool.db not found: {db}")
    key = session_key(deal_id)
    marks = ", ".join("?" * len(LIVE_STATUSES))
    try:
        conn = sqlite3.connect(
            db.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
        )
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT id, status, critical, timeout, origin FROM questions "
                f"WHERE session_key = ? AND status IN ({marks}) ORDER BY id DESC LIMIT 1",
                (key, *LIVE_STATUSES),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AskError(f"pool.db unreadable: {db} ({exc})") from exc
    return None if row is None else dict(row)


def ask(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    text: str,
    text_full: Optional[str],
    options: Optional[Iterable[str]] = None,
    critical: Optional[bool] = None,
) -> int:
    """Задать вопрос по делу; вернуть `question_id` (существующий или новый).

    `conn` — соединение с журналом (`journal.open_journal`), `deal` — факты дела:
    обязательны `id`, `board` и `home` (корень Hermes, где лежит пул).

    `critical=None` означает «решить по фактам журнала»: дело ждёт человека дольше
    `ASK_CRITICAL_SEC` — вопрос критичный, иначе у него есть срок
    (`ASK_TIMEOUT_HOURS`). Явное значение вызывающего важнее расчёта.

    Живой вопрос по делу (второй `ask()` по тому же делу) возвращается как есть:
    строка в пуле не создаётся, а факт `question_asked` дописывается идемпотентно
    (ключ `ask:<question_id>:question_asked`) — так журнал узнаёт и о вопросе,
    созданном другим продюсером пула.
    """
    deal_id = str(deal.get("id") or "")
    board = str(deal.get("board") or "")
    home = deal.get("home")
    if not board:
        raise ValueError("ask: у дела нет доски")
    if home is None:
        raise ValueError("ask: в фактах дела нет home")
    key = session_key(deal_id)
    short = " ".join(str(text or "").split())
    if not short:
        raise ValueError("ask: пустой текст вопроса")

    live = find_live(Path(home), deal_id)
    if live is not None:
        _record_asked(
            conn,
            deal_id=deal_id,
            board=board,
            question_id=int(live["id"]),
            key=key,
            critical=int(live.get("critical") or 0),
            timeout=live.get("timeout"),
        )
        return int(live["id"])

    flag = _critical(conn, deal_id, critical)
    timeout = None if flag else ASK_TIMEOUT_HOURS
    question_id = _insert(
        pool_path(Path(home)),
        key=key,
        board=board,
        text=short,
        text_full=(str(text_full).strip() or None) if text_full else None,
        options=list(options) if options else None,
        critical=flag,
        timeout=timeout,
    )
    _record_asked(
        conn,
        deal_id=deal_id,
        board=board,
        question_id=question_id,
        key=key,
        critical=flag,
        timeout=timeout,
    )
    return question_id


def sync_answers(conn: sqlite3.Connection, home: Path) -> dict[str, Any]:
    """Ответы пула → журнал: `question_answered` по каждому отвеченному вопросу.

    Читаются только свои вопросы (`origin='duty'`) в статусе `answered`: чужие
    продюсеры пула пишут свои факты сами. Событие идемпотентно по ключу
    `ask:<question_id>:question_answered`, поэтому повторный проход (в том числе
    после падения между проходами) даёт `appended: 0`, а не второй факт ответа.

    Ответ в самом пуле этот модуль не закрывает и не переписывает: вопрос
    закрывается штатным путём пула (`msgq resolve`, реплай в iMessage, панель
    `/imsg`), а duty только замечает факт.
    """
    db = pool_path(Path(home))
    report: dict[str, Any] = {"appended": 0, "errors": []}
    if not db.is_file():
        report["errors"].append({"board": QUESTION_SOURCE, "reason": f"pool.db not found: {db}"})
        return report
    try:
        rows = _answered_rows(db)
    except sqlite3.Error as exc:
        report["errors"].append({"board": QUESTION_SOURCE, "reason": f"pool.db unreadable: {db} ({exc})"})
        return report

    journal = _journal()
    now = int(time.time())
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            deal_id = _deal_of(str(row.get("session_key") or ""))
        except ValueError:
            continue
        events.append(
            {
                "ts": _iso_sec(row.get("answered_at")) or now,
                "recorded_at": now,
                "deal_id": deal_id,
                "board": _board_of(conn, deal_id),
                "kind": "question_answered",
                "payload": {
                    "question_id": int(row["id"]),
                    "answer_len": len(str(row.get("answer") or "")),
                    "answered_via": row.get("answered_via") or None,
                },
                "source": ASK_SOURCE,
                "idempotency_key": f"ask:{int(row['id'])}:question_answered",
            }
        )
    report["appended"] = journal.append(conn, events)
    return report


def _answered_rows(db: Path) -> list[dict[str, Any]]:
    """Свои отвеченные вопросы пула (только чтение)."""
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, session_key, answer, answered_at, answered_via FROM questions "
            "WHERE origin = ? AND status = 'answered' ORDER BY id",
            (ASK_ORIGIN,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _insert(
    db: Path,
    *,
    key: str,
    board: str,
    text: str,
    text_full: Optional[str],
    options: Optional[list[str]],
    critical: int,
    timeout: Optional[float],
) -> int:
    """Вставка вопроса в пул: единственная запись duty в чужую БД.

    Схема — пула imessage-gateway (`questions`), поэтому колонки названы его
    словами: `session_key`, `origin`, `kind`, `options_json`. `project` — слаг
    доски: пул группирует вопросы по нему так же, как для карточных.
    """
    if not db.is_file():
        raise AskError(f"pool.db not found: {db}")
    options_json = None
    if options:
        if not all(isinstance(option, str) for option in options):
            raise ValueError("ask: options — список простых строк, не объектов")
        options_json = json.dumps(options, ensure_ascii=False)
    try:
        conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            cur = conn.execute(
                "INSERT INTO questions "
                "(session_key, project, text, text_full, origin, options_json, status,"
                " timeout, critical, kind) "
                "VALUES (?,?,?,?,?,?,'pending',?,?,?)",
                (key, board, text, text_full, ASK_ORIGIN, options_json, timeout, critical, ASK_KIND),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AskError(f"pool.db write failed: {db} ({exc})") from exc


def _record_asked(
    conn: sqlite3.Connection,
    *,
    deal_id: str,
    board: str,
    question_id: int,
    key: str,
    critical: int,
    timeout: Any,
) -> None:
    """Факт «вопрос создан» в журнал — после успешной вставки в пул, идемпотентно."""
    now = int(time.time())
    _journal().append(
        conn,
        [
            {
                "ts": now,
                "recorded_at": now,
                "deal_id": deal_id,
                "board": board,
                "kind": "question_asked",
                "payload": {
                    "question_id": question_id,
                    "session_key": key,
                    "critical": critical,
                    "timeout": timeout,
                },
                "source": ASK_SOURCE,
                "idempotency_key": f"ask:{question_id}:question_asked",
            }
        ],
    )


def _critical(conn: sqlite3.Connection, deal_id: str, explicit: Optional[bool]) -> int:
    """Критичность вопроса: явное значение или факт долгого ожидания из журнала."""
    if explicit is not None:
        return 1 if explicit else 0
    since = _waiting_since(conn, deal_id)
    if since is None:
        return 0
    return 1 if int(time.time()) - since > ASK_CRITICAL_SEC else 0


def _waiting_since(conn: sqlite3.Connection, deal_id: str) -> Optional[int]:
    """Начало текущего ожидания человека по журналу: `ts` открытого блока.

    Считается по событиям дела (как `journal.fold`), а не по времени создания
    карточки: старое дело могло ждать человека минуту, а молодое — сутки. Нет
    открытого блока — нет факта, и критичность не выдумывается.
    """
    events = _journal().read_events(conn, deal_id)
    since: Optional[int] = None
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == "blocked":
            since = int(event.get("ts") or 0)
        elif kind in ("unblocked", "completed"):
            since = None
    return since


def _deal_of(key: str) -> str:
    """id дела из `session_key` пула (`t_<hex>`); иное — не наше дело."""
    value = str(key or "").strip()
    if not value.startswith("t_") or len(value) < 3:
        raise ValueError(f"ask: session_key is not a deal id: {value}")
    return value


def _board_of(conn: sqlite3.Connection, deal_id: str) -> str:
    """Доска дела — по последнему событию журнала; пустая строка, если её нет."""
    row = conn.execute("SELECT board FROM events WHERE deal_id = ? ORDER BY id DESC LIMIT 1", (deal_id,)).fetchone()
    return "" if row is None else str(row[0])


def _iso_sec(value: Any) -> Optional[int]:
    """ISO-время пула (`2026-11-03T16:39:22.123Z`) → unix-секунды; мусор → `None`."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())
