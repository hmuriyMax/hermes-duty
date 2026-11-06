"""Лента этапов дела: состояние графа и попытки — **только из журнала**.

Экран «Дело» отвечает на вопрос «что было и что делает», а не «в каком статусе
карточка» (спека-источник, «Наблюдаемость: четыре экрана»). Поэтому источник у
этого модуля ровно один — `duty/duty.db`: срез этапов считает `transit.stage_states`
по событиям, а попытки берутся из `attempt_started`/`attempt_ended`/`attempt_progress`.

Чего здесь принципиально нет:

- `ps`, `lsof`, чтения `/proc` и вообще любого взгляда на живые процессы. Живость
  попытки определяется по журналу: нет `attempt_ended` и лизинг ещё не истёк —
  `live: true`; нет `attempt_ended`, а срок лизинга прошёл — `live: false` и
  `dead: true`. Это и есть «мёртвый процесс виден», без выдуманного статуса:
  состояние этапа остаётся тем, что записано (`running`), а мёртвость процесса
  называется отдельным полем.
- второго набора правил «что значит готово»: состояния этапов считает
  `transit.stage_states`, а не этот файл. Здесь только форма ответа.

Что модуль отдаёт:

- `snapshot(...)` — блок `stages` для `GET /deal`: этапы спеки в порядке
  объявления, у каждого состояние, номер попытки, раунды, ожидание, что делала
  каждая попытка и что доказано из объявленных команд;
- `attempts(...)` — попытки дела в форме таймлайна (`GET /duty/{deal_id}/attempts`):
  тот же разбор пары `attempt_started` ↔ `attempt_ended`, плюс `base`/`commit`,
  `reaped` и число событий прогресса. Живость незакрытой попытки может уточняться
  строкой лизинга (`leases`), если вызывающий её прочитал: лизинг продлевается
  heartbeat'ом, а срок в `attempt_started` — только начальный;
- `current(...)` — этап и номер попытки для среза `/now`: «что сейчас» видно без
  открытия дела;
- `spec_of(...)` — спека, записанная в журнале при создании дела
  (`created.payload.spec`): путь к графу берётся из журнала, а не угадывается.

Спека — файл, а не факт о процессе: граф читается `dashboard/spec.py` тем же
`load_spec()`, что и у движка. Журнал говорит, **какая** спека у дела; файл —
**из чего** она состоит. Если спеки нет или она не читается, `stages` пуст, а
`note` называет причину: пустая лента без объяснения выглядела бы как «этапов
нет», а это разные факты.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

# Модули плагина грузятся по пути и живут в `sys.modules` под общими именами:
# вторая загрузка означала бы вторую копию констант и правил в одном процессе.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
TRANSIT_MODULE_NAME = "hermes_duty_transit"
SPEC_MODULE_NAME = "hermes_duty_spec"


def _load(name: str, filename: str) -> Any:
    """`dashboard/<filename>` — тот же модуль, что грузит остальной плагин."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = _load(JOURNAL_MODULE_NAME, "journal.py")
transit = _load(TRANSIT_MODULE_NAME, "transit.py")
spec_module = _load(SPEC_MODULE_NAME, "spec.py")

ATTEMPT_STARTED = "attempt_started"
ATTEMPT_ENDED = "attempt_ended"
ATTEMPT_PROGRESS = "attempt_progress"

# Потолок вызовов на попытку в ответе: телеметрия пишется на каждый шаг, и у
# длинной попытки их тысячи. Обрезанное число честно называется в `calls_omitted`,
# а не прячется: экран рисует ровно то, что отдал журнал.
MAX_CALLS = 800
# Факт публикации (0.0.5): что ушло в origin и чем это подтверждено. Ложится в
# строку своей попытки — «опубликовано» видно там же, где вердикт этапа.
PUBLISHED = "published"

# Вид этапа по факту начала попытки (`attempt_started.stage_kind`). Старые записи
# поля не имеют — и это `agent`: до 0.0.5 других видов у этапа не было.
STAGE_KIND_AGENT = "agent"

# Живость попытки: три ответа, а не два. `None` — журнал не записал срок лизинга
# (старая запись или событие без `lease_expires`), и тогда «жив» было бы догадкой.
LIVE = "live"
DEAD = "dead"
UNKNOWN = "unknown"

# Причины, по которым ленты может не быть. Текст уходит в `note` как есть: экран
# печатает его рядом с пустым списком, поэтому «нет спеки» и «спека не читается»
# остаются различимыми.
NO_SPEC = "у дела нет спеки: этапы строятся из графа `duty.yaml` — укажи её в `duty create --spec` или в `?spec=<путь>`"
RELATIVE_SPEC = "путь к спеке должен быть абсолютным: относительный не восстановить в журнале"
NO_STAGES = "в спеке нет этапов: показывать нечего"


def _int(value: Any) -> int:
    """Целое из payload: `True` числом не считается, мусор — нулём."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _maybe_int(value: Any) -> Optional[int]:
    """Целое или `None`: код выхода и токены отсутствуют честно, а не нулём."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> Any:
    """`question_id` числом, если это число: пул отдаёт и `284`, и `"284"`."""
    if value is None or value == "":
        return None
    text = str(value)
    return int(text) if text.isdigit() else text


def _normalise(value: Any) -> str:
    """Команда без лишних пробелов: `go  build` и `go build` — одна команда."""
    return " ".join(str(value or "").split())


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    """`payload` события объектом: разбор тот же, что у журнала (мусор → `{}`)."""
    return journal.payload_of(event)


def spec_of(events: Iterable[Any]) -> Optional[str]:
    """Спека дела по журналу: `created.payload.spec`, записанный при создании.

    Путь берётся из события, а не из строки дела: журнал — единственная правда о
    том, по какому графу дело создавалось. Событий без спеки — `None`.
    """
    for event in events or ():
        if not isinstance(event, dict) or str(event.get("kind") or "") != "created":
            continue
        path = _payload(event).get("spec")
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


def _attempts(
    events: Iterable[Any], leases: Optional[dict[tuple[str, int], int]] = None
) -> list[dict[str, Any]]:
    """Попытки дела по журналу: начало, конец и всё, что попытка о себе сказала.

    Попытка — это процесс, поэтому у неё есть pid, начало, конец, код выхода,
    исход, класс отказа, токены и число инструментов. Факты берутся из двух
    событий одной попытки (`attempt_started` + `attempt_ended`) по `run_id`:
    конец без начала — не попытка, а мусор, и он пропускается.

    Повтор `attempt_started` с тем же `run_id` (движок пишет его один раз на
    попытку) не удваивает строку: первое начало и есть начало.

    `base`/`commit` — два факта о дереве попытки: от чего этап отводился и что
    произвёл. `reaped` — попытку закрыла уборка (`dashboard/reaper.py`), а не
    исполнитель. `progress` — сколько шагов попытка сообщила о себе
    (`attempt_progress`): у этих событий нет `run_id`, пара ищется по
    `(stage, attempt)`, которые они несут.

    `leases` — срок лизинга из таблицы store (`{(stage, attempt): expires_at}`),
    если вызывающий его прочитал. Он важнее срока из `attempt_started`: тот
    записан один раз при старте, а лизинг продлевается heartbeat'ом, и попытка,
    которая думает дольше TTL, без этого выглядела бы мёртвой.
    """
    started: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    progress: dict[tuple[str, int], int] = {}
    published: dict[tuple[str, int], dict[str, Any]] = {}
    raw_calls: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for event in sorted(
        (item for item in events or () if isinstance(item, dict)),
        key=lambda item: _int(item.get("id")),
    ):
        payload = _payload(event)
        run_id = str(payload.get("run_id") or "") or f"#{_int(event.get('id'))}"
        kind = str(event.get("kind") or "")
        if kind == ATTEMPT_PROGRESS:
            key = (str(payload.get("stage") or ""), _int(payload.get("attempt")))
            progress[key] = progress.get(key, 0) + 1
            raw_calls.setdefault(key, []).append(payload)
        elif kind == PUBLISHED:
            # Публикация — факт попытки, а не отдельная попытка: она ложится в ту
            # строку, чей этап её записал (`runner._published_event`).
            key = (str(payload.get("stage") or ""), _int(payload.get("attempt")))
            published[key] = payload
        elif kind == ATTEMPT_STARTED and run_id not in started:
            started[run_id] = {"start": event, "start_payload": payload, "end": None, "end_payload": {}}
            order.append(run_id)
        elif kind == ATTEMPT_ENDED and run_id in started:
            started[run_id]["end"] = event
            started[run_id]["end_payload"] = payload

    rows: list[dict[str, Any]] = []
    # Вызовы инструментов сводятся в пары «старт ↔ результат» один раз на ключ
    # `(stage, attempt)`: у событий прогресса нет `run_id`, поэтому пара ищется
    # там же, где считается `progress`.
    calls: dict[tuple[str, int], list[dict[str, Any]]] = {}
    omitted: dict[tuple[str, int], int] = {}
    for key, payloads in raw_calls.items():
        calls[key], omitted[key] = _calls(payloads)
    for run_id in order:
        item = started[run_id]
        start, start_payload = item["start"], item["start_payload"]
        end, end_payload = item["end"], item["end_payload"]
        details = end_payload.get("details")
        details = details if isinstance(details, dict) else {}
        contract = start_payload.get("contract")
        contract = contract if isinstance(contract, dict) else {}
        stage = str(start_payload.get("stage") or "")
        number = _int(start_payload.get("attempt")) or len(rows) + 1
        lease = _maybe_int(start_payload.get("lease_expires"))
        if leases and (stage, number) in leases:
            lease = _maybe_int(leases[(stage, number)])
        started_at = _int(start.get("ts"))
        finished_at = None if end is None else _int(end.get("ts"))
        rows.append(
            {
                "n": number,
                "run_id": run_id,
                "stage": stage,
                "stage_kind": str(start_payload.get("stage_kind") or STAGE_KIND_AGENT),
                "role": str(contract.get("role") or start_payload.get("role") or ""),
                "pid": _maybe_int(start_payload.get("pid")),
                "started_at": started_at or None,
                "finished_at": finished_at or None,
                "duration_sec": None
                if finished_at is None
                else max(0, finished_at - started_at),
                "lease_expires": lease,
                "exit": _maybe_int(details.get("exit_code")),
                "outcome": str(end_payload.get("outcome") or ""),
                "kind": str(end_payload.get("kind") or ""),
                "tokens": _maybe_int(details.get("usage_tokens")),
                "tools": _maybe_int(details.get("tools")),
                "reason": str(end_payload.get("reason") or ""),
                "base": str(end_payload.get("base") or start_payload.get("base") or ""),
                "commit": str(end_payload.get("commit") or ""),
                "reaped": bool(end_payload.get("reaped")),
                "progress": progress.get((stage, number), 0),
                "calls": calls.get((stage, number), []),
                "calls_omitted": omitted.get((stage, number), 0),
                "evidence": _records(end_payload.get("result")),
                "published": published.get((stage, number)),
                "end": end is not None,
            }
        )
    return rows


def _calls(payloads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Вызовы инструментов попытки: старт, длительность, имя — парами.

    Телеметрия пишет два события на вызов: `tool_use` (имя и момент старта, без
    длительности) и `tool_result` (то же имя и `duration_ms`). Длительность есть
    только у результата — поэтому вызов получает её из своей пары, а его начало
    считается назад от конца (`ts - duration_ms`). Вызов без результата остаётся
    с `ms: null`: попытка оборвалась посреди вызова, и придумывать ей срок нельзя.

    Пара ищется по имени, ближайшая по порядку `seq`: параллельных вызовов у
    исполнителя нет, но повтор имени подряд — обычное дело (`read_file` дважды).
    """
    ordered = sorted(payloads, key=lambda item: _int(item.get("seq")))
    open_uses: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for item in ordered:
        phase = str(item.get("phase") or "")
        name = str(item.get("name") or "")
        at = _int(item.get("ts"))
        if phase == "tool_use":
            open_uses.append({"seq": _int(item.get("seq")), "name": name, "at": at})
            continue
        duration = _maybe_int(item.get("duration_ms"))
        index = next((i for i, use in enumerate(open_uses) if use["name"] == name), None)
        if index is None and open_uses:
            index = 0
        if index is None:
            # Результат без начала: вызов начался до журнала (или старт потерян).
            out.append({"seq": _int(item.get("seq")), "name": name, "at": at, "ms": duration})
            continue
        use = open_uses.pop(index)
        start = at if duration is None else round(at - duration / 1000.0, 3)
        out.append({"seq": use["seq"], "name": use["name"] or name, "at": start, "ms": duration})
    for use in open_uses:
        out.append({"seq": use["seq"], "name": use["name"], "at": use["at"], "ms": None})
    out.sort(key=lambda call: (call["at"], call["seq"]))
    omitted = max(0, len(out) - MAX_CALLS)
    return out[:MAX_CALLS], omitted


def _records(result: Any) -> list[dict[str, Any]]:
    """Записи объявленных команд из отчёта попытки: `{cmd, exit, started_at}`.

    `result.json` — факт попытки о себе: гейт (`dashboard/evidence.py`) сверяет
    его с деревом, а лента показывает тем же списком. Запись без `cmd` не
    команда, и в ленту не идёт: пустая строка и «команда не названа» — разные
    факты.
    """
    if not isinstance(result, dict):
        return []
    raw = result.get("evidence")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        cmd = _normalise(entry.get("cmd"))
        if not cmd:
            continue
        out.append(
            {
                "cmd": cmd,
                "exit": _maybe_int(entry.get("exit")),
                "started_at": _maybe_int(entry.get("started_at")),
            }
        )
    return out


def _alive(row: dict[str, Any], now: int) -> dict[str, Any]:
    """Живость попытки по журналу: `live`/`dead` плюс `lease` текстом.

    Три ответа, а не два. Закрытая попытка (`attempt_ended`) — `live: false`
    всегда: процесса уже нет по факту, а не по сроку. Незакрытая с действующим
    лизингом — `live: true`. Незакрытая с истёкшим лизингом — `live: false` и
    `dead: true`: это и есть «процесс мёртв», увиденное без `ps`.
    """
    if row["end"]:
        return {"live": False, "dead": False, "lease": "ended"}
    expires = row["lease_expires"]
    if expires is None:
        return {"live": None, "dead": None, "lease": UNKNOWN}
    if int(now) < int(expires):
        return {"live": True, "dead": False, "lease": LIVE}
    return {"live": False, "dead": True, "lease": DEAD}


def _public(row: dict[str, Any], now: int) -> dict[str, Any]:
    """Попытка в форме ответа: без внутреннего флага `end`, с живостью."""
    alive = _alive(row, now)
    return {
        "n": row["n"],
        "run_id": row["run_id"],
        "stage": row["stage"],
        "stage_kind": row["stage_kind"],
        "role": row["role"],
        "pid": row["pid"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_sec": row["duration_sec"],
        "lease_expires": row["lease_expires"],
        "exit": row["exit"],
        "outcome": row["outcome"],
        "kind": row["kind"],
        "tokens": row["tokens"],
        "tools": row["tools"],
        "reason": row["reason"],
        "base": row["base"],
        "commit": row["commit"],
        "reaped": row["reaped"],
        "progress": row["progress"],
        # Вызовы инструментов попытки: `[{seq, name, at, ms}]`, где `at` — начало
        # вызова (по результату и длительности), `ms` — длительность или `null`,
        # если результат не пришёл. `calls_omitted` — сколько не поместилось под
        # потолком `MAX_CALLS`: экран называет это числом, а не молчит.
        "calls": row["calls"],
        "calls_omitted": row["calls_omitted"],
        "live": alive["live"],
        "dead": alive["dead"],
        "lease": alive["lease"],
        "evidence": row["evidence"],
        # Публикация попытки: `{sha, branch, mr_url, ref, verified_by}` или `null`.
        # Один факт на попытку — последний (`published` пишется один раз).
        "published": _publication(row.get("published")),
    }


def _publication(fact: Any) -> Optional[dict[str, Any]]:
    """Факт `published` в форме ответа: только то, что было записано.

    Пустой словарь вместо отсутствия публикации читался бы как «опубликовано
    ничего», поэтому нет факта — `None`, и поверхность печатает прочерк.
    """
    if not isinstance(fact, dict):
        return None
    return {
        "sha": str(fact.get("sha") or ""),
        "branch": str(fact.get("branch") or ""),
        "mr_url": str(fact.get("mr_url") or ""),
        "ref": str(fact.get("ref") or ""),
        # Заявка исполнителя рядом с фактом: `sha` подтверждён `ls-remote` и
        # полный, а здесь — то, что он написал (обычно сокращённая форма).
        "claimed_sha": str(fact.get("claimed_sha") or ""),
        "verified_by": str(fact.get("verified_by") or ""),
        "stage": str(fact.get("stage") or ""),
        "attempt": _maybe_int(fact.get("attempt")),
    }


def attempts(
    events: Iterable[Any], now: Any, *, leases: Optional[dict[tuple[str, int], int]] = None
) -> list[dict[str, Any]]:
    """Попытки дела в порядке старта — из журнала, без взгляда на процессы.

    Форма — та же, что у ленты этапов, потому что разбор пары
    `attempt_started` ↔ `attempt_ended` один на модуль: вторая копия этого
    правила разошлась бы с первой на первом же повторе `run_id`.

    Живость незакрытой попытки считается по сроку лизинга (`_alive`): лизинг жив
    — `live`, истёк — `dead` (попытку закроет `dashboard/reaper.py`), срока нет —
    ни то, ни другое. `ps` здесь не спрашивается: журнал и лизинг — те факты,
    которые есть у плагина.
    """
    moment = _int(now)
    rows = [_public(row, moment) for row in _attempts(events, leases)]
    return sorted(rows, key=lambda row: (row["started_at"] or 0, row["n"]))


def _join_evidence(declared: Any, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Объявленные команды против записей последней попытки: что доказано.

    `must` — из спеки, `got` — из отчёта, `ok` — «доказано»: запись есть и код
    совпал. Записи, которых спека не объявляла, тоже показываются (`must: null`):
    молча выкинуть лишний факт значило бы спрятать, что попытка запускала ещё
    что-то.
    """
    out: list[dict[str, Any]] = []
    used: set[int] = set()
    for item in declared or ():
        cmd = _normalise(getattr(item, "cmd", ""))
        must = _int(getattr(item, "must", 0))
        match = next(
            ((index, record) for index, record in enumerate(records) if record["cmd"] == cmd),
            None,
        )
        if match is None:
            out.append({"cmd": cmd, "must": must, "got": None, "ok": False, "at": None})
            continue
        index, record = match
        used.add(index)
        out.append(
            {
                "cmd": cmd,
                "must": must,
                "got": record["exit"],
                "ok": record["exit"] == must,
                "at": record["started_at"],
            }
        )
    for index, record in enumerate(records):
        if index in used:
            continue
        out.append(
            {
                "cmd": record["cmd"],
                "must": None,
                "got": record["exit"],
                "ok": None,
                "at": record["started_at"],
            }
        )
    return out


def _waits(
    state: str,
    question_id: Optional[str],
    reason: str,
    parked: Optional[dict[str, Any]] = None,
    retry_at: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Чего этап ждёт: решения по парковке, вопрос человека, паузу или гейт графа.

    `wait_human` без номера вопроса — ожидание человека без вопроса (ответить
    некуда), и это сказано видом `human`, а не подставленным номером.

    Парковка (`parked`) — ожидание того же человека, но с причиной: класс отказа и
    счётчик попыток видны на экране, а не выводятся из текста. Вид ждёт первым:
    исчерпанный этап в графе `failed`, и без этой ветки «почему стоит» отвечал бы
    отказ без единого слова о том, что человек уже решает, продолжать ли.
    """
    if parked is not None:
        fact: dict[str, Any] = {
            "kind": "parked",
            "question_id": _number(question_id),
            "reason": reason,
            "park_reason": parked.get("reason") or "",
        }
        if parked.get("gate"):
            # Парковка гейта: ни класса, ни счётчика — этап не падал, человек не
            # ответил. Поля оставляем отсутствующими, а не нулевыми: «0 из 0» —
            # это утверждение о попытках, которых не было.
            fact["gate"] = True
            return fact
        fact.update(
            {
                "class": parked.get("kind") or "",
                "attempts": parked.get("attempts"),
                "limit": parked.get("limit"),
            }
        )
        return fact
    if state == transit.WAIT_HUMAN:
        return {
            "kind": "question" if question_id else "human",
            "question_id": _number(question_id),
            "reason": reason,
        }
    if retry_at is not None:
        return {"kind": "retry", "question_id": None, "reason": reason, "retry_at": retry_at}
    if state == transit.WAIT_GATE:
        return {"kind": "gate", "question_id": None, "reason": reason}
    return None


def stages(spec: Any, events: Iterable[Any], now: Any) -> list[dict[str, Any]]:
    """Этапы спеки со срезом, попытками и доказательствами — в порядке объявления.

    Состояние и номер попытки — из `transit.stage_states` (единый предикат «готов»
    для консоли и движка). Попытки — из журнала. Ни одного процесса, ни одной
    карточки: этапы, до которых дело не дошло, видны как `pending`/`wait_gate`
    именно потому, что спека их объявляет, а не потому, что кто-то их запускал.

    Парковка читается из журнала (`journal.parked_of`): у неё нет состояния этапа,
    потому что исчерпанный этап в графе `failed`, — а ждёт он человека.
    """
    moment = _int(now)
    states = transit.stage_states(spec, events)
    rows = [_public(row, moment) for row in _attempts(events)]
    parked = journal.parked_of(events)
    out: list[dict[str, Any]] = []
    for stage in spec.stages:
        state = states[stage.id]
        mine = [row for row in rows if row["stage"] == stage.id]
        last_with_records = next((row for row in reversed(mine) if row["evidence"]), None)
        mine_parked = parked if parked is not None and str(parked.get("stage") or "") == stage.id else None
        out.append(
            {
                "id": stage.id,
                "kind": getattr(stage, "kind", STAGE_KIND_AGENT),
                "role": stage.role,
                "state": state.state,
                "attempt": state.attempt,
                "rounds": state.rounds,
                "needs": list(state.needs),
                "reason": state.reason,
                "failures": {kind: count for kind, count in state.failures},
                "waits": _waits(
                    state.state,
                    state.question_id,
                    state.reason,
                    mine_parked,
                    state.retry_at if state.retry_at is not None and moment < state.retry_at else None,
                ),
                "attempts": mine,
                "evidence": _join_evidence(
                    getattr(stage, "evidence", ()),
                    last_with_records["evidence"] if last_with_records else [],
                ),
            }
        )
    return out


def current(events: Iterable[Any], now: Any) -> Optional[dict[str, Any]]:
    """Этап и номер попытки для `/now`: «что сейчас» без открытия дела.

    Источник — только журнал: последняя попытка дела. Её `state` — факт попытки
    (`running` — не закрыта, `done`/`failed` — чем кончилась), а не состояние
    графа: граф без спеки не считается, и подставлять сюда `wait_gate` значило бы
    выдумать факт. Живость — тем же правилом лизинга, что у ленты этапов.
    """
    rows = _attempts(events)
    if not rows:
        return None
    row = _public(rows[-1], _int(now))
    if row["finished_at"] is None:
        state = transit.RUNNING
    elif row["outcome"] in transit.OK_OUTCOMES and not row["kind"]:
        state = transit.DONE
    else:
        state = transit.FAILED
    return {
        "id": row["stage"],
        "role": row["role"],
        "attempt": row["n"],
        "state": state,
        "live": row["live"],
        "since": row["started_at"],
    }


def snapshot(events: Iterable[Any], now: Any, *, spec_path: Optional[str] = None) -> dict[str, Any]:
    """Блок `stages` для `GET /deal`: граф дела по журналу и файлу спеки.

    `spec_path` — путь, названный вызывающим (событие `created` или `?spec=`);
    приоритет решает роут, а не этот слой. Путь принимается только абсолютный:
    относительный не восстановить из журнала (`docs/SPEC.md`, «Путь к спеке»).
    """
    events = list(events or ())
    if not spec_path:
        return {"spec": None, "stages": [], "note": NO_SPEC}
    path = Path(spec_path).expanduser()
    if not path.is_absolute():
        return {"spec": str(path), "stages": [], "note": RELATIVE_SPEC}
    try:
        spec = spec_module.load_spec(path)
    except Exception as exc:  # noqa: BLE001 - спека не читается: это ответ, а не падение
        reason = str(exc) or type(exc).__name__
        return {"spec": str(path), "stages": [], "note": f"спека не читается: {reason}"}
    if not spec.stages:
        return {"spec": str(path), "stages": [], "note": NO_STAGES}
    try:
        return {"spec": str(path), "stages": stages(spec, events, now), "note": ""}
    except Exception as exc:  # noqa: BLE001 - мусор в журнале не роняет карточку
        reason = str(exc) or type(exc).__name__
        return {"spec": str(path), "stages": [], "note": f"срез этапов не посчитан: {reason}"}
