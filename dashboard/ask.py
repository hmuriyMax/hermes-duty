"""Единственный канал вопросов duty: пул `messaging/pool.db` плюс событие в журнал.

Контракт — `docs/JOURNAL.md`, раздел «`ask()`». Коротко:

- вопрос создаётся в пуле imessage-gateway по схеме пула: `session_key=t_<deal_id>`,
  `origin='duty'`, `kind='decision'`, `text` — краткая строка, `text_full` — развёрнутая,
  `options_json` — варианты ответа, если заданы;
- перед созданием ищется живой вопрос по делу; найденный возвращается как есть, дубль
  не создаётся. Живой вопрос **своего** продюсера (`origin='duty'`) при этом получает
  факт `question_asked` идемпотентно, живой вопрос **чужого** — только id, без факта:
  снять `question_asked` по чужому вопросу нечем (чужой продюсер в наш журнал не
  пишет), и дело залипло бы в «ждёт ответа» навсегда;
- событие `question_asked` пишется в журнал **после** успешной вставки в пул: сбой пула
  не оставляет в журнале ни вопроса, ни его тени — ошибка уходит вызывающему
  (`AskError`) и превращается в `errors[]` ответа;
- `sync_answers` — обратный ход: ответ (`answered`) или истёкший срок (`expired`),
  найденные в пуле, становятся фактом журнала (`question_answered`), ровно один раз
  на вопрос. Отвеченный вопрос и вопрос с истёкшим TTL одинаково перестают быть
  живым фактом ожидания — иначе дело вечно «ждёт ответа» у вопроса, которого нет.

Границы модуля:

- писать в пул вправе **только** этот модуль (`ask`); остальной код duty открывает
  `pool.db` через `?mode=ro` — `journal.connect_ro` и `plugin_api._connect_ro`;
- `kanban.db` и `state.db` здесь не открываются вовсе;
- `fold` вопросов по-прежнему не читает: он работает по событиям, которые пишет этот
  модуль.

Кто и когда зовёт `ask()`:

- явный путь — `POST /ask/{deal_id}` (`dashboard/plugin_api.py`): вопрос задаёт
  человек или координатор, когда дело в `blocked`/`needs_input` стоит без вопроса.
  Это и есть решение п.5 заметки 0.0.2: дело без вопроса больше не выдаёт себя за
  «ждёт ответа» (`waiting.human_kind = "status"`), а получает вопрос осознанным
  действием, а не побочным эффектом ingest;
- автоматом вопросы **не** создаются: пул решений не должен наполняться вопросами
  от каждого прохода по срезу (правило пула: карточные вопросы эскалирует
  координатор, а не сканер). Ответы пула дочитывает `sync_answers` — его зовёт
  драйвер перед каждым решением (`bin/duty run`), а не проход ingest: прохода
  ingest с 0.0.6 нет.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
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
# `ask()` за эти секунды создал бы дубль). Набор — единственный источник правды для
# живых статусов: его же читает срез пула (`plugin_api._pool_questions`).
LIVE_STATUSES = ("pending", "sent")

# Вопрос закрыт пулом: `answered` — человек ответил, `expired` — вышел срок. Срок
# (`timeout`) duty задаёт некритичному вопросу, и пул переводит такую строку в
# `expired` своим чистящим циклом. Оба статуса закрывают факт в журнале: без
# второго неотвеченный вопрос с истёкшим TTL остался бы открытым навсегда.
CLOSED_STATUSES = ("answered", "expired")

# Признак в `answered_via` для закрытия по сроку: пул меняет статус, но
# `answered_via` не заполняет, и без пометки истечение выглядело бы ответом.
EXPIRED_VIA = "expired"

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
    «пул недоступен» — разные факты, и второй нельзя выдавать за первый. Сюда же
    уходит сбой журнала после успешной вставки в пул: вопрос уже создан, и это
    должно быть сказано, а не выдано за 500 без подробностей.
    """


@dataclass(frozen=True)
class AskResult:
    """Итог `ask()`: id вопроса и что именно произошло.

    `created` — строка пула создана этим вызовом; `adopted` — подхвачен живой
    вопрос чужого продюсера, по которому факта в журнале нет и не будет. Оба
    признака нужны роуту: «вернул существующий вопрос» и «вернул существующий
    чужой вопрос, факт не записан» — разные факты, и второй нельзя показывать
    так же молча, как первый.
    """

    question_id: int
    created: bool
    adopted: bool


def pool_path(home: Path) -> Path:
    """Путь пула вопросов: `<home>/messaging/pool.db`."""
    return Path(home) / POOL_SUBDIR / POOL_FILE


def session_key(deal_id: str) -> str:
    """Ключ сессии пула для дела: `t_` + id дела.

    У kanban-дела id уже `t_<hex>`, поэтому ключ — он сам; у своего дела id
    `d_<hex>`, и ключ получается `t_d_<hex>` (схема пула ждёт префикс `t_`).
    Обратный ход — `deal_id_of`, и он же единственное место, где это правило
    разворачивается: разъехавшиеся «туда» и «обратно» дали бы вечное «ждёт
    ответа» (находка координатора 03.11).
    """
    key = str(deal_id or "").strip()
    if not key:
        raise ValueError("ask: у дела нет id")
    return key if key.startswith("t_") else f"t_{key}"


def deal_id_of(key: str) -> Optional[str]:
    """id дела из `session_key` пула — один маппинг на оба пространства id.

    `t_<hex>` — kanban-дело, `t_d_<hex>` — своё: снимается ровно один префикс
    `t_`, остальное отдаётся как есть. Чужой `session_key` (другой продюсер,
    другая форма) — `None`: «не наше» и «наше, но не разобрали» это разные факты,
    и второй обязан попасть в `errors[]`, а не в тишину.
    """
    value = str(key or "").strip()
    if re.fullmatch(r"[td]_[0-9a-f]{6,}", value):
        return value
    if value.startswith("t_") and re.fullmatch(r"d_[0-9a-f]{6,}", value[2:]):
        return value[2:]
    return None


def answer_of(home: Path, question_id: Any) -> Optional[str]:
    """Текст ответа человека на вопрос пула — только чтение, `None` без ответа.

    Нужен поверхности: у запаркованного дела с полученным ответом доказательство
    обязано назвать, **что именно** человек ответил, а журнал хранит только
    `answer_len` (текст — факт пула, а не журнала). Возвращается одна строка:
    переводы строк ломают таблицу `duty now`. Вопрос без ответа, чужой номер и
    недоступный пул отвечают `None` — это «текста нет», а не ошибка чтения среза.
    """
    db = pool_path(Path(home))
    if not db.is_file():
        return None
    try:
        number = int(str(question_id).strip())
    except (TypeError, ValueError):
        return None
    try:
        conn = sqlite3.connect(
            db.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
        )
        try:
            row = conn.execute("SELECT answer FROM questions WHERE id = ?", (number,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    text = "" if row is None or row[0] is None else " ".join(str(row[0]).split())
    return text or None


def exchange_of(home: Path, question_id: Any) -> dict[str, str]:
    """Вопрос и ответ человека из пула — текстом, а не номерами.

    Журнал хранит только `question_id` и `answer_len`, поэтому экран, которому
    нужен сам текст, обязан спросить пул. Читается одна строка `mode=ro`:
    переводы строк схлопываются в пробел (текст живёт в подсказке одной строкой),
    а длинный текст обрезается многоточием — в маркере таймлайна он подпись, а
    не документ. Вопрос без номера, чужой номер и недоступный пул дают пустые
    строки: это «текста нет», а не ошибка чтения среза.
    """
    db = pool_path(Path(home))
    if not db.is_file():
        return {}
    try:
        number = int(str(question_id).strip())
    except (TypeError, ValueError):
        return {}
    try:
        conn = sqlite3.connect(
            db.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
        )
        try:
            row = conn.execute(
                "SELECT text, answer FROM questions WHERE id = ?", (number,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    text, answer = (" ".join(str(row[0] or "").split()), " ".join(str(row[1] or "").split()))
    return {"question": _clip(text), "answer": _clip(answer)}


def _clip(text: str, limit: int = 160) -> str:
    """Одна строка в пределах подписи: хвост длинного вопроса в подсказке не нужен."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:—-")
    return f"{cut}…"


def find_live(home: Path, deal_id: str) -> Optional[dict[str, Any]]:
    """Живой вопрос дела в пуле (`pending`/`sent`) или `None`.

    Читает пул `mode=ro`: сам поиск ничего не меняет, и это же правило действует
    для любого чтения пула в плагине. Отсутствие файла — не «вопросов нет», а
    `AskError`: иначе второй вопрос молча создался бы поверх недоступного пула.

    `origin` намеренно не фильтруется: живой вопрос по делу — факт пула, чей бы он
    ни был, и второй вопрос поверх него был бы дублем. Что с ним делать, решает
    `ask()`: чужой возвращается без записи в журнал, свой — с идемпотентным фактом.
    `created_at` нужен, чтобы факт в журнале получил настоящий `ts` вопроса, а не
    время вызова.
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
                f"SELECT id, status, critical, timeout, origin, created_at FROM questions "
                f"WHERE session_key = ? AND status IN ({marks}) ORDER BY id DESC LIMIT 1",
                (key, *LIVE_STATUSES),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AskError(f"pool.db unreadable: {db} ({exc})") from exc
    return None if row is None else dict(row)


def resolve(home: Path, question_id: Any, answer: str, *, via: str = "lead", deal_id: Any) -> bool:
    """Закрыть **живой** вопрос пула ответом лида — штатным путём пула.

    Лид отвечает воркеру (`duty_answer`) и делает это раньше человека: вопрос,
    оставленный в пуле открытым, висел бы в панели и в iMessage, человек нажимал
    бы кнопку по уже отвеченному вопросу, и его нажатие писало бы второй факт по
    тому же `question_id` (живое дело `d_8ec6a401`, вопрос #555). Поэтому ответ
    лида закрывает строку пула так же, как `msgq resolve`: теми же полями
    (`status`, `answer`, `answered_at`, `answered_via`).

    Строка сверяется **вместе с делом**: `session_key` (`t_<deal_id>`) стоит в том
    же `WHERE`, что и номер. Номер сам по себе — не адрес: названный не тем делом
    он закрыл бы чужой живой вопрос другого дела тихо и безвозвратно, и человек
    никогда не увидел бы вопроса, на который его просили ответить.

    Перезаписи не происходит: `WHERE status IN ('pending','sent')` — ответ
    человека, дошедший первым, остаётся его ответом, а повтор лида даёт `False`.
    Возврат — «строка была живой и закрыта этим вызовом»; `False` значит «пул
    закрыл вопрос сам или вопрос чужой» и это не ошибка.
    """
    db = pool_path(Path(home))
    if not db.is_file():
        raise AskError(f"pool.db not found: {db}")
    try:
        number = int(str(question_id).strip())
    except (TypeError, ValueError):
        raise ValueError(f"ask: номер вопроса не разобран: {question_id!r}") from None
    key = session_key(str(deal_id or ""))
    marks = ", ".join("?" * len(LIVE_STATUSES))
    try:
        conn = sqlite3.connect(db, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            cur = conn.execute(
                "UPDATE questions SET status='answered', answer=?, "
                "answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), answered_via=? "
                f"WHERE id = ? AND session_key = ? AND status IN ({marks})",
                (str(answer), via, number, key, *LIVE_STATUSES),
            )
            conn.commit()
            return int(cur.rowcount or 0) > 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise AskError(f"pool.db write failed: {db} ({exc})") from exc


def ask(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    text: str,
    text_full: Optional[str],
    options: Optional[Iterable[str]] = None,
    critical: Optional[bool] = None,
    routing: Optional[dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> AskResult:
    """Задать вопрос по делу; вернуть id вопроса и что именно произошло.

    `conn` — соединение с журналом (`journal.open_journal`), `deal` — факты дела:
    обязательны `id` и `home` (корень Hermes, где лежит пул); `repo` необязателен
    и попадает в пул как `project` — группировка вопроса (0.0.6).

    `routing` — адрес вопроса и причина решения (`dashboard/routing.py`): движок
    знает этап и попытку, которые спросили, и кладёт их в факт `question_asked`.
    В пул адрес не пишется: у пула своя схема, и возврат ответа держится на
    `session_key` и `question_id`, а этап и попытка — свойство журнала.

    `critical=None` означает «решить по фактам журнала»: дело ждёт человека дольше
    `ASK_CRITICAL_SEC` — вопрос критичный, иначе у него есть срок
    (`ASK_TIMEOUT_HOURS`). Явное значение вызывающего важнее расчёта, но только при
    создании вопроса: у подхваченного живого вопроса аргументы (`text`, `text_full`,
    `options`, `critical`) не применяются — вопрос уже задан, и его факты берутся из
    строки пула (контракт `docs/JOURNAL.md`, «`ask()`»).

    `timeout` — срок ответа в секундах, объявленный вызывающим (у гейта это
    `budget.minutes`). Он сильнее расчёта по журналу и делает вопрос некритичным:
    критичный вопрос пул не истекает вовсе, а срок гейта существует именно для
    того, чтобы однажды истечь и вернуть дело человеку с причиной. `None` —
    срок по общим правилам.

    Три исхода, все в `AskResult`:

    - `created=True` — строка пула создана этим вызовом, `question_asked` записан;
    - `created=False, adopted=False` — живой **свой** вопрос: тот же id, факт
      `question_asked` дописан идемпотентно (так журнал узнаёт о вопросе, созданном
      до включения журнала);
    - `created=False, adopted=True` — живой вопрос **чужого** продюсера
      (`origin != 'duty'`): id возвращается, в журнал не пишется ничего. Ответ на
      такой вопрос журналу принципиально не виден, `question_asked` снять было бы
      нечем — дело залипло бы в «ждёт ответа» навсегда.
    """
    deal_id = str(deal.get("id") or "")
    home = deal.get("home")
    if home is None:
        raise ValueError("ask: в фактах дела нет home")
    key = session_key(deal_id)
    short = " ".join(str(text or "").split())
    if not short:
        raise ValueError("ask: пустой текст вопроса")

    live = find_live(Path(home), deal_id)
    if live is not None:
        question_id = int(live["id"])
        if str(live.get("origin") or "") != ASK_ORIGIN:
            return AskResult(question_id=question_id, created=False, adopted=True)
        _record_asked(
            conn,
            deal_id=deal_id,
            question_id=question_id,
            key=key,
            critical=int(live.get("critical") or 0),
            timeout=live.get("timeout"),
            ts=_iso_sec(live.get("created_at")) or int(time.time()),
            routing=routing,
        )
        return AskResult(question_id=question_id, created=False, adopted=False)

    flag = 0 if timeout is not None else _critical(conn, deal_id, critical)
    ttl = float(timeout) if timeout is not None else (None if flag else ASK_TIMEOUT_HOURS)
    question_id = _insert(
        pool_path(Path(home)),
        key=key,
        project=str(deal.get("repo") or ""),
        text=short,
        text_full=(str(text_full).strip() or None) if text_full else None,
        options=list(options) if options else None,
        critical=flag,
        timeout=ttl,
    )
    if question_id <= 0:
        # `ask:0:*` в журнале заглушил бы ключом все будущие `question_asked`
        # (UNIQUE по `idempotency_key`), поэтому нулевой id — отказ, а не id.
        raise AskError(f"pool.db returned no question id: {pool_path(Path(home))}")
    _record_asked(
        conn,
        deal_id=deal_id,
        question_id=question_id,
        key=key,
        critical=flag,
        timeout=ttl,
        ts=int(time.time()),
        routing=routing,
    )
    return AskResult(question_id=question_id, created=True, adopted=False)


def sync_answers(conn: sqlite3.Connection, home: Path) -> dict[str, Any]:
    """Закрытые вопросы пула → журнал: `question_answered` по каждому.

    Читаются только свои вопросы (`origin='duty'`) в статусе `answered` или
    `expired`: чужой вопрос в журнал не попадает вовсе (`ask()` его не
    записывает), поэтому и снимать по нему нечего — свои факты чужой продюсер
    пишет сам, а в наш журнал ему писать некуда.

    `expired` закрывает вопрос так же, как ответ: некритичный вопрос получает срок
    (`ASK_TIMEOUT_HOURS`), пул переводит протухшую строку в `expired`, и без этого
    факта дело ждало бы ответа на вопрос, которого уже нет. Признак истечения
    пишется в `answered_via='expired'`, чтобы ответ человека и выход срока не
    читались как одно и то же.

    Событие идемпотентно по ключу `ask:<question_id>:question_answered`, поэтому
    повторный проход (в том числе после падения между проходами) даёт `appended: 0`,
    а не второй факт.

    Вопрос, по которому в журнале **уже** есть `question_answered`, пропускается:
    его закрыл кто-то раньше — лид (`duty_answer`) или прошлый проход этого
    модуля. Второй факт по тому же `question_id` — это второй ответ на один
    вопрос: в живом деле `d_8ec6a401` (вопрос #555) лид ответил в 03:35:16,
    человек нажал кнопку в 03:35:57, и факт человека пришёл без `routing`, то есть
    срез этапа его вопросом не видел вовсе. Поэтому свой факт несёт `routing` из
    `question_asked` — адрес вопроса (`stage`/`attempt`) принадлежит журналу, а не
    строке пула.

    Ответ и истечение в самом пуле этот модуль не закрывает и не переписывает:
    вопрос закрывается штатным путём пула (`msgq resolve`, реплай в iMessage,
    панель `/imsg`, чистящий цикл демона), а duty только замечает факт. Ответ лида
    закрывает строку пула раньше — `ask.resolve` из `duty_answer`.
    """
    db = pool_path(Path(home))
    report: dict[str, Any] = {"appended": 0, "errors": []}
    if not db.is_file():
        report["errors"].append({"source": QUESTION_SOURCE, "reason": f"pool.db not found: {db}"})
        return report
    try:
        rows = _closed_rows(db)
    except sqlite3.Error as exc:
        report["errors"].append({"source": QUESTION_SOURCE, "reason": f"pool.db unreadable: {db} ({exc})"})
        return report

    journal = _journal()
    now = int(time.time())
    events: list[dict[str, Any]] = []
    # Журнал читается один раз на дело: адрес вопроса (`routing`) и признак
    # «уже отвечен» — свойства фактов дела, а не строк пула.
    seen: dict[str, tuple[dict[str, dict[str, Any]], set[str]]] = {}
    for row in rows:
        deal_id = deal_id_of(str(row.get("session_key") or ""))
        if deal_id is None:
            # Неразобранный ключ — не «нет факта», а «факт потерян»: вопрос пул
            # закрыл, и без строки в `errors[]` это исчезло бы молча.
            report["errors"].append(
                {
                    "source": QUESTION_SOURCE,
                    "reason": (
                        f"вопрос #{row.get('id')} закрыт, но session_key "
                        f"{row.get('session_key')!r} не разобран в id дела — question_answered не записан"
                    ),
                }
            )
            continue
        if deal_id not in seen:
            seen[deal_id] = _questions_of(journal.read_events(conn, deal_id))
        routing, answered = seen[deal_id]
        number = str(int(row["id"]))
        if number in answered:
            # Вопрос уже закрыт фактом журнала — вторым ответом он не закрывается,
            # а второй факт по тому же номеру читается как два разных ответа на
            # один вопрос.
            continue
        expired = str(row.get("status") or "") == "expired"
        payload: dict[str, Any] = {
            "question_id": int(row["id"]),
            "answer_len": 0 if expired else len(str(row.get("answer") or "")),
            # Причину закрытия называет пул, если назвал: истёкший по сроку
            # вопрос приходит без `answered_via` (его ставит только наша
            # пометка), а закрытый чужой причиной — со своим значением.
            "answered_via": row.get("answered_via") or (EXPIRED_VIA if expired else None),
        }
        if routing.get(number):
            # Адрес вопроса — из `question_asked`: без него срез этапа вопрос
            # отвеченным не видит (`dashboard/tools.py`, `_question_pair`).
            payload["routing"] = dict(routing[number])
        events.append(
            {
                # У истёкшего вопроса пул `answered_at` не заполняет: момент
                # истечения в пуле не записан, поэтому `ts` — когда закрытие
                # увидела duty, и это сказано признаком `answered_via`.
                "ts": _iso_sec(row.get("answered_at")) or now,
                "recorded_at": now,
                "deal_id": deal_id,
                "kind": "question_answered",
                "payload": payload,
                "source": ASK_SOURCE,
                "idempotency_key": f"ask:{int(row['id'])}:question_answered",
            }
        )
    report["appended"] = journal.append(conn, events)
    return report


def _questions_of(events: Iterable[Any]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Адреса вопросов дела и номера уже отвеченных — по фактам журнала.

    `routing` пишет `ask()` в факт `question_asked` (`dashboard/ask.py`), и до
    сих пор он жил только там: факт закрытия его не повторял. Срез этапа
    (`dashboard/tools.py`, `_question_pair`) адресует вопрос именно по
    `payload.routing.stage`, поэтому закрытие без адреса вопрос не закрывает.

    Второй ответ на вопрос (`answered`) — признак «закрыт раньше»: так выглядит
    пара «лид ответил, человек нажал кнопку» в живом журнале.
    """
    routing: dict[str, dict[str, Any]] = {}
    answered: set[str] = set()
    for event in events or ():
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        if kind not in ("question_asked", "question_answered"):
            continue
        payload = _journal().payload_of(event)
        number = str(payload.get("question_id") or "")
        if not number:
            continue
        if kind == "question_answered":
            answered.add(number)
            continue
        address = payload.get("routing")
        if isinstance(address, dict):
            routing[number] = address
    return routing, answered


def _closed_rows(db: Path) -> list[dict[str, Any]]:
    """Свои закрытые вопросы пула (`answered`/`expired`) — только чтение.

    Полный список, а не «старше курсора»: вопрос закрывается не в порядке `id`
    (у каждого дела свой вопрос, и ответы приходят как придётся), поэтому
    водяной знак `id > last_seen` пропустил бы поздно закрытый вопрос с меньшим
    id — ровно тот случай, когда факт остался бы висеть навсегда. Цена — один
    проход по своим вопросам пула, которых не больше, чем было вызовов `ask()`.
    """
    marks = ", ".join("?" * len(CLOSED_STATUSES))
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, session_key, status, answer, answered_at, answered_via FROM questions "
            f"WHERE origin = ? AND status IN ({marks}) ORDER BY id",
            (ASK_ORIGIN, *CLOSED_STATUSES),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


ASK_DEDUPE_HOURS = 24.0


def _recent_same_question(conn: sqlite3.Connection, key: str, text: str) -> Optional[int]:
    """id живого вопроса того же дела с тем же текстом (антидубль), иначе None.

    Живой — `pending`/`sent`: дедуп не даёт задваить вопрос, на который человек
    ещё не ответил. Отвеченный или истёкший вопрос дедупом не перекрывается:
    его ответ уже израсходован, и та же ситуация — новый вопрос, а не дубль.
    Кейс d_bdf8edfb: парковка задаёт детерминированный текст («Попытки
    исчерпаны: 0/1…» — счётчик после continue сброшен), дедуп возвращал id
    отвеченного вопроса, движок парковался с чужим ответом, и в пуле не было
    открытого вопроса — дело зависало в «решает человек» без вопроса.
    """
    norm = " ".join((text or "").split()).strip().lower()
    if not norm or ASK_DEDUPE_HOURS <= 0:
        return None
    row = conn.execute(
        "SELECT id, text FROM questions WHERE session_key = ? AND origin = ? "
        "AND status IN ('pending', 'sent') "
        "AND created_at >= strftime('%Y-%m-%dT%H:%M:%fZ','now', ?) "
        "ORDER BY id DESC LIMIT 20",
        (key, ASK_ORIGIN, f"-{ASK_DEDUPE_HOURS:g} hours"),
    ).fetchall()
    for candidate in row:
        if " ".join((candidate[1] or "").split()).strip().lower() == norm:
            return int(candidate[0])
    return None


def _insert(
    db: Path,
    *,
    key: str,
    project: str,
    text: str,
    text_full: Optional[str],
    options: Optional[list[str]],
    critical: int,
    timeout: Optional[float],
) -> int:
    """Вставка вопроса в пул: единственная запись duty в чужую БД.

    Схема — пула imessage-gateway (`questions`), поэтому колонки названы его
    словами: `session_key`, `origin`, `kind`, `options_json`. `project` пул
    группирует по проекту; в duty группы теперь задаёт `repo` дела (0.0.6), и
    именно он сюда пишется — пустая строка, если репозиторий не назван.
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
            dup = _recent_same_question(conn, key, text)
            if dup is not None:
                # то же дело уже спрашивали этим же текстом недавно — второй раз
                # человека не дёргаем (иначе в пуле копятся «старые» вопросы)
                conn.rollback()
                return dup
            cur = conn.execute(
                "INSERT INTO questions "
                "(session_key, project, text, text_full, origin, options_json, status,"
                " timeout, critical, kind) "
                "VALUES (?,?,?,?,?,?,'pending',?,?,?)",
                (key, project, text, text_full, ASK_ORIGIN, options_json, timeout, critical, ASK_KIND),
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
    question_id: int,
    key: str,
    critical: int,
    timeout: Any,
    ts: int,
    routing: Optional[dict[str, Any]] = None,
) -> None:
    """Факт «вопрос создан» в журнал — после успешной вставки в пул, идемпотентно.

    Сбой журнала здесь — не 500: строка пула уже создана, и вызывающий обязан
    узнать, что вопрос есть, а факта нет. `AskError` говорит это словами, а
    повторное действие лечит расхождение идемпотентным ключом.

    `routing` (адрес и причина решения) кладётся **вложенным** ключом
    `payload.routing`, а не на верхний уровень: верхний уровень `payload` —
    словарь пула (`question_id`, `session_key`), а `stage` верхнего уровня читают
    `transit`/`journal.fold` как «событие относится к этапу». Адрес этапа знает
    движок, и он же читает его оттуда — форма журнала от этого не меняется.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "question_id": question_id,
        "session_key": key,
        "critical": critical,
        "timeout": timeout,
    }
    if routing:
        payload["routing"] = dict(routing)
    try:
        _journal().append(
            conn,
            [
                {
                    "ts": ts,
                    "recorded_at": now,
                    "deal_id": deal_id,
                    "kind": "question_asked",
                    "payload": payload,
                    "source": ASK_SOURCE,
                    "idempotency_key": f"ask:{question_id}:question_asked",
                }
            ],
        )
    except sqlite3.Error as exc:
        raise AskError(
            f"вопрос #{question_id} создан в пуле, факт журнала не записан "
            f"(идемпотентный повтор лечит): {exc}"
        ) from exc


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
