"""Отпускание дела, которое осталось `running`, когда движка уже нет.

Зачем это отдельный слой. `reaper` закрывает попытку **без конца**: пары
`attempt_started`/`attempt_ended` не хватало, и он её дописывает. Но бывает
обратное: конец попытки записан, а строка дела всё равно `running` — движок ушёл
(таймаут попытки, `--once`, предел шагов, Ctrl-C, прогон, убитый между шагом и
решением), и записать ответ `transit` стало некому. Тогда расходятся журнал и
строка: `fold` говорит `no_live_fact`, `transit.next_stage` — `run`/`done`/
`failed`, а колонка `deals.status` говорит «в работе» и будет говорить так, пока
человек не заметит. Живое дело `d_eb4ad2bb`: 02:51 создано, 04:22 попытка
закрылась `failed/infra` по таймауту, дальше — ничего; строку `running` закрыл
человек в 11:53, то есть решение «продолжать» лежало в журнале 7.5 часа и его
никто не спросил.

Модуль пишет **недостающий факт**, а не подменяет состояние. После него `fold` и
`transit` сходятся сами, потому что в журнале появилось то, чего им не хватало:
отпущенное дело (`released`) возвращается в очередь, закрытое — закрывается
`completed`, остановленное — блокируется тем же писателем, что и на обычном пути.
Ни одного `if` под этот случай в `fold`, `transit` и на поверхностях не нужно.

## Правила

1. **Предмет — состояние store, а не исход прогона.** «Движок ушёл» — это строка
   `running` без живого лизинга, без открытой попытки, без блока и без вопроса,
   а не код возврата: таблица исходов (0/1/2/3/4/130) описывала бы правило
   семь раз, и каждая копия разошлась бы с остальными. Поэтому факт пишет проход,
   спрашивающий журнал.
2. **Материализуется уже посчитанный ответ `transit`** тем писателем, который у
   этого ответа есть: `run`/`wait(retry)` → `deals.release_deal`; `done` →
   `deals.close_stages_done` (граф с закрывающим этапом закрывает лид — `cause`
   и итог даёт `deals.closure_of`); `failed` → `deals.set_blocked` (отказ движка
   без начала попытки, `details.refused`) либо `runner.park_exhausted` (попытки
   исчерпаны); `park` гейта → `runner.park_gate`. Своего решения проход не
   принимает: «а может, закрыть?» здесь не спрашивают.
3. **Идемпотентность — ключом события от журнала**, а не от решения: `after` =
   id последнего state-changing факта, кроме самого `released` (`attempt_ended`
   той попытки). Повтор даёт тот же ключ → `events: 0` и `updated_at` не
   двигается. Ключ не включает `decision` намеренно: между двумя проходами ответ
   `transit` может смениться (`wait(retry)` → `run`), и это тот же отпуск.
4. **Только свои дела и только строка `running`** (`d_<hex>`, `deals.is_own`).
   Дело, которое держит человек (`blocked`, `review`, `archived`), не трогается:
   его статус — не `running`, и решение человека фактом движка не перебивается.
5. **Живое дело не трогается.** Открытая попытка — предмет `reaper`;
   живой лизинг — «этап ведёт движок»; открытый вопрос или блок — «ждём
   человека». Плюс пауза `RELEASE_GRACE_SEC` от последнего факта: живой движок
   между двумя шагами лизинга не держит, и его свежий `attempt_ended` —
   единственный признак «я ещё здесь».
6. **`claimed` — факт человека, а не движка.** Дело, которое человек только что
   взял в работу, остаётся `running`: отпускание вернуло бы его в очередь и
   стёрло намерение человека.
7. **`released` не пишется там, где решение `failed`/`done`.** Отказ — не
   «отпущено продолжать»: он либо паркуется (человек решает `continue|stop`),
   либо блокируется классом отказа. Закрытие (`done`) идёт `completed`, иначе
   дело осталось бы живым с пройденным графом.

## Чего модуль не делает

Не меняет `fold` и `transit`, не расширяет `reaper.sweep` (его предмет — попытка
без конца), не заводит новый статус строки и слов причин закрытия, не трогает
лизинг и попытки, не решает «на глаз»: спека берётся у самого дела
(`created.payload.spec`), а её отсутствие называется причиной в отчёте, а не
угадывается. Не спавнит процессов и не задаёт вопросов, кроме парковки —
её вопрос пишет `runner.park_exhausted`, тот же писатель, что на обычном пути.

Честная граница, и её нельзя замалчивать: факт пишет процесс, а процесса нет.
Окно «строка врёт» сокращается с «пока человек не заметит» до «до первого касания
store движком» (`bin/duty run`, `bin/duty reap`), и в этом окне поверхность уже
честна — `/now` показывает `no_signal` (`plugin_api.OWN_WAITING_FALLBACK`).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Модули плагина грузятся по пути и живут в `sys.modules` под общими именами:
# вторая загрузка означала бы вторую копию схемы и констант в одном процессе.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
DEALS_MODULE_NAME = "hermes_duty_deals"
SPEC_MODULE_NAME = "hermes_duty_spec"
TRANSIT_MODULE_NAME = "hermes_duty_transit"
RUNNER_MODULE_NAME = "hermes_duty_runner"
STAGEFEED_MODULE_NAME = "hermes_duty_stagefeed"
REAPER_MODULE_NAME = "hermes_duty_reaper"


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
deals = _load(DEALS_MODULE_NAME, "deals.py")
spec_module = _load(SPEC_MODULE_NAME, "spec.py")
transit = _load(TRANSIT_MODULE_NAME, "transit.py")
runner = _load(RUNNER_MODULE_NAME, "runner.py")
# Путь спеки дела читается тем же слоем, что рисует ленту этапов
# (`stagefeed.spec_of` — `created.payload.spec`): второго чтения «какая спека у
# дела» в плагине быть не должно.
stagefeed = _load(STAGEFEED_MODULE_NAME, "stagefeed.py")
# «Попытка без конца» — предикат уборки, а не наш: открытую попытку отпускание не
# трогает, и второй разбор пары `attempt_started` ↔ `attempt_ended` разошёлся бы
# с `reaper`.
reaper = _load(REAPER_MODULE_NAME, "reaper.py")

# Источник факта: `engine`, а не `cli`/`api`. Отпустил движок, и в журнале это
# видно тем же словом, что у остальных его фактов (`runner.ENGINE_SOURCE`).
SOURCE = runner.ENGINE_SOURCE

# Строка, которую отпускает проход: `running` — «дело в работе».
RUNNING_STATUS = "running"

# Пауза от последнего факта журнала: два порога `reaper.GRACE_MIN_SEC`. «Движок
# только что был» — не «движок ушёл»: между шагами лизинг не держится, и свежий
# `attempt_ended` живого прогона обязан дождаться своей паузы.
RELEASE_GRACE_SEC = 2 * reaper.GRACE_MIN_SEC

# Что проход пишет — словами отчёта (действие, а не вид события).
ACTION_RELEASED = "released"
ACTION_CLOSED = "closed"
ACTION_PARKED = "parked"
ACTION_BLOCKED = "blocked"


# ------------------------------------------------------------------ состояние


def _recorded_at(event: dict[str, Any]) -> int:
    """Когда факт узнали: `recorded_at`, а старое событие — по `ts`."""
    value = event.get("recorded_at")
    if value is None:
        value = event.get("ts")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _last_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Последнее state-changing событие, считая `released` не в счёт.

    Отпускание считается от факта **до** себя: иначе повторный проход взял бы
    основанием ключа собственный `released` и записал второй отпуск той же
    попытки. Виды из `journal.NON_STATE_KINDS` не в счёт по тому же правилу, по
    которому их не читает `fold`.
    """
    for event in reversed(events):
        kind = str(event.get("kind") or "")
        if kind in journal.NON_STATE_KINDS or kind == "released":
            continue
        return event
    return {}


def _last_state_id(events: list[dict[str, Any]]) -> int:
    """id последнего state-changing факта до отпускания — основание ключа события."""
    try:
        return int(_last_state(events).get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _last_attempt(events: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Последний `attempt_ended` дела: этап, номер, исход, класс и `details`.

    Это ответ на «чем кончилось», который проход пересказывает в причине
    отпускания: второй раз текст ошибки не сочиняется — он остался в самом
    `attempt_ended`, и пересказ разошёлся бы с ним.
    """
    for event in reversed(events):
        if str(event.get("kind") or "") != "attempt_ended":
            continue
        payload = journal.payload_of(event)
        return {
            "run_id": str(payload.get("run_id") or ""),
            "stage": str(payload.get("stage") or ""),
            "attempt": _number(payload.get("attempt")),
            "outcome": str(payload.get("outcome") or ""),
            "kind": str(payload.get("kind") or ""),
            "reason": str(payload.get("reason") or ""),
            "details": payload.get("details") if isinstance(payload.get("details"), dict) else {},
        }
    return None


def _number(value: Any) -> int:
    """Целое из факта: `True` числом не считается, мусор — нулём."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _attempt_text(fact: Optional[dict[str, Any]]) -> str:
    """Чем кончилась последняя попытка — одной фразой для причины отпускания."""
    if fact is None:
        return "идущей попытки нет, продолжения не было"
    head = f"попытка {fact['run_id'] or '—'} закончилась {fact['outcome'] or '—'}"
    if fact["kind"]:
        head += f"/{fact['kind']}"
    return f"{head}, продолжения не было"


def _held_by(
    events: list[dict[str, Any]],
    conn: Any,
    deal_id: str,
    at: int,
    grace_sec: int,
) -> Optional[str]:
    """Чем дело держится — или `None`, если его действительно никто не ведёт.

    Одна проверка на весь разбор (правило 5): каждый живой факт назван своим
    аксессором, а не выведен из текста. Пустой ответ — «отпускать можно».
    """
    if journal.closed_fact(events) is not None:
        return "закрыто фактом completed"
    if journal.parked_of(events) is not None:
        return "запарковано: ждёт решения человека"
    if reaper.open_attempts(events):
        return "попытка без конца: предмет reaper.sweep"
    if deals.live_lease_of(conn, deal_id, now=at) is not None:
        return "живой лизинг: этап ведёт движок"
    if deals._open_block_kind(conn, deal_id) is not None:
        return "открыт блок: дело ждёт человека"
    if journal._open_question(events) is not None:
        return "вопрос без ответа: дело ждёт человека"
    last = _last_state(events)
    if str(last.get("kind") or "") == "claimed":
        return "дело взял человек (claimed)"
    if at - _recorded_at(last) < int(grace_sec):
        return "движок был только что: факт моложе паузы"
    return None


# --------------------------------------------------------------------- спека


def _spec_of(
    events: list[dict[str, Any]], *, spec: Any, cache: dict[str, Any]
) -> tuple[Any, str]:
    """Спека дела и причина, если её нет: уже прочитанная или своя из журнала.

    Путь берётся из `created.payload.spec` — единственное место, где записано,
    по какому графу дело заводилось. Нет пути или файл не читается — проход
    **пропускает** дело и называет причину: решение `transit` по чужой или
    выдуманной спеке было бы догадкой о том, чего движок не решал.
    """
    path = stagefeed.spec_of(events)
    if not path:
        return None, "спека дела не записана в created.payload.spec — решать нечего"
    if spec is not None and str(getattr(spec, "path", "")) == str(path):
        return spec, ""
    if path not in cache:
        cache[path] = _load_spec(path)
    loaded = cache[path]
    if loaded is None:
        return None, f"спека дела не читается: {path}"
    return loaded, ""


def _load_spec(path: str) -> Any:
    """Спека по пути из журнала; `None` — файла нет или он не читается."""
    try:
        return spec_module.load_spec(Path(path).expanduser())
    except Exception:  # noqa: BLE001 - чужая спека не повод ронять проход
        return None


def _stage_of(spec: Any, stage_id: Any) -> Any:
    """Этап спеки по id — тем же аксессором, что у драйвера (`spec.stage`)."""
    finder = getattr(spec, "stage", None)
    return finder(str(stage_id)) if callable(finder) else None


# -------------------------------------------------------------------- проход


def sweep(
    conn: Any,
    now: Any,
    *,
    home: Any,
    spec: Any = None,
    only: Any = None,
    grace_sec: int = RELEASE_GRACE_SEC,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Отпустить дела, которые остались `running` при ушедшем движке.

    Возвращает строки по фактам, которые проход записал (или записал бы при
    `dry_run`), и по делам, которые решить не смог: у таких строк есть ключ
    `error` с причиной — «не решил» не должно выглядеть как «решил и нечего».

    `spec` — спека дела, которая у вызывающего уже в руках (драйвер её прочитал):
    берётся, только если это спека **того же** дела (`created.payload.spec`).
    `only` — ограничить проход названными делами (`bin/duty reap` после уборки,
    `finally` прогона для своего дела); `None` — все.
    `grace_sec=0` — «этот движок уходит сам»: свежий факт не повод ждать паузу.
    `dry_run` — посчитать и вернуть, ничего не записывая.
    """
    at = int(now)
    wanted = None if only is None else {str(item) for item in only}
    cache: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    for deal in deals.deal_rows(conn, open_only=True):
        deal_id = str(deal.get("id") or "")
        if str(deal.get("status") or "") != RUNNING_STATUS:
            continue
        if wanted is not None and deal_id not in wanted:
            continue
        if not deals.is_own(deal_id):
            continue
        events = journal.read_events(conn, deal_id)
        if _held_by(events, conn, deal_id, at, grace_sec) is not None:
            continue
        deal_spec, missing = _spec_of(events, spec=spec, cache=cache)
        if deal_spec is None:
            out.append(
                {"deal": deal_id, "error": missing, "written": 0, "dry_run": bool(dry_run)}
            )
            continue
        try:
            row = _materialise(
                conn, deal, deal_spec, events, at, home=home, dry_run=dry_run
            )
        except deals.DealError as exc:
            # Гонка с человеком или с другим писателем: дело успело измениться
            # между чтением и записью. Отказ называется, а не выдаётся за отпуск.
            row = {"deal": deal_id, "error": str(exc), "written": 0, "dry_run": bool(dry_run)}
        if row is not None:
            out.append(row)
    return out


def _materialise(
    conn: Any,
    deal: dict[str, Any],
    deal_spec: Any,
    events: list[dict[str, Any]],
    at: int,
    *,
    home: Any,
    dry_run: bool,
) -> Optional[dict[str, Any]]:
    """Записать тот факт, который назвал `transit`, — своим писателем.

    `None` — ждать нечего и отпускать нечего: решение про ожидание, у которого
    есть свой писатель (`attempt`, `question`, `verdict`, `gate`), проход не
    подменяет.
    """
    decision = transit.next_stage(deal_spec, events, at)
    if decision.kind == transit.RUN:
        return _released(conn, deal, events, decision, at, dry_run=dry_run)
    if decision.kind == transit.WAIT and decision.what == transit.WAIT_RETRY:
        return _released(conn, deal, events, decision, at, dry_run=dry_run)
    if decision.kind == transit.DECISION_DONE:
        return _closed(conn, deal, deal_spec, events, at, dry_run=dry_run)
    if decision.kind == transit.DECISION_FAILED:
        return _stopped(conn, deal, deal_spec, events, at, home=home, dry_run=dry_run)
    if decision.kind == transit.PARK:
        return _parked_gate(conn, deal, deal_spec, decision, at, dry_run=dry_run)
    return None


def _released(
    conn: Any,
    deal: dict[str, Any],
    events: list[dict[str, Any]],
    decision: Any,
    at: int,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Дело отпущено: `released` и строка `ready`/`todo` — продолжение в очереди."""
    deal_id = str(deal["id"])
    fact = _last_attempt(events)
    reason = "движок отпустил дело: " + _attempt_text(fact)
    payload = {
        "run_id": fact["run_id"] if fact else "",
        "stage": fact["stage"] if fact else "",
        "attempt": fact["attempt"] if fact else 0,
        "decision": decision.kind,
        "wait": decision.what or None,
        "idle_sec": max(0, at - _recorded_at(_last_state(events))),
        "reason": reason,
    }
    target = "ready" if deals.gate_open(conn, deal_id) else "todo"
    written = 0
    if not dry_run:
        result = deals.release_deal(
            conn,
            deal_id,
            payload=payload,
            after=_last_state_id(events),
            source=SOURCE,
            now=at,
        )
        written = int(result.get("events") or 0)
        target = str((result.get("deal") or {}).get("status") or target)
    return {
        "deal": deal_id,
        "action": ACTION_RELEASED,
        "from": RUNNING_STATUS,
        "to": target,
        "stage": payload["stage"],
        "attempt": payload["attempt"],
        "decision": decision.kind,
        "wait": payload["wait"],
        "reason": reason,
        "written": written,
        "dry_run": bool(dry_run),
    }


def _closed(
    conn: Any,
    deal: dict[str, Any],
    deal_spec: Any,
    events: list[dict[str, Any]],
    at: int,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Все этапы пройдены, `completed` не записан: закрытие тем же писателем.

    Граф — из журнала (`transit.graph`), а не из файла спеки: принятый план растит
    граф, и закрытие по устаревшему набору назвало бы не те этапы
    (`docs/ENGINE-1.0.md`, раздел 7). Чем закрывается дело, решает
    `deals.closure_of`: граф без закрывающего этапа (`spec.CLOSE_STAGE`) —
    `cause='all_stages_done'`; граф с ним закрывает **лид** своим `approve`, и
    итог дела — его `summary`. Приговора нет — отказ (`DealError`), а не
    `completed` мимо лида: иначе ушедший движок закрывал бы дело сам.
    """
    stages = [str(stage.id) for stage in transit.graph(deal_spec, events)]
    cause, summary = deals.closure_of(events, stages)
    if not cause:
        raise deals.DealError(
            f"close: закрывающий этап {str(spec_module.CLOSE_STAGE)!r} в графе дела "
            f"{deal['id']} не закрыт приговором approve — дело закрывает лид, "
            "«все этапы пройдены» этого не заменяет"
        )
    written = 0
    if not dry_run:
        result = deals.close_stages_done(
            conn,
            str(deal["id"]),
            stages=stages,
            source=SOURCE,
            now=at,
            cause=cause,
            summary=summary,
        )
        written = int(result.get("events") or 0)
    reason = "все этапы пройдены: закрытие записано движком"
    if cause == deals.CLOSE_CAUSE_LEAD:
        reason = "лид закрыл дело приговором approve: закрытие записано движком"
    return {
        "deal": str(deal["id"]),
        "action": ACTION_CLOSED,
        "from": RUNNING_STATUS,
        "to": "done",
        "stages": stages,
        "cause": cause,
        "reason": reason,
        "written": written,
        "dry_run": bool(dry_run),
    }


def _stopped(
    conn: Any,
    deal: dict[str, Any],
    deal_spec: Any,
    events: list[dict[str, Any]],
    at: int,
    *,
    home: Any,
    dry_run: bool,
) -> Optional[dict[str, Any]]:
    """Попытки кончились — остановить тем фактом, который записал бы обычный путь.

    Два вида остановки, и оба уже названы журналом: отказ движка без начала
    попытки (`details.refused`: потолок дела или не-«ок» на публикацию) —
    `blocked` с классом отказа, без парковки (продолжать при исчерпанном потолке
    нечего); исчерпанные попытки — `runner.park_exhausted`, он же спрашивает
    человека `continue|stop`.
    """
    deal_id = str(deal["id"])
    fact = _last_attempt(events)
    if fact is None or not fact["stage"]:
        return None
    stage = _stage_of(deal_spec, fact["stage"])
    if stage is None:
        return {
            "deal": deal_id,
            "error": f"этап {fact['stage']!r} из attempt_ended не найден в спеке дела",
            "written": 0,
            "dry_run": bool(dry_run),
        }
    if fact["details"].get("refused"):
        reason = fact["reason"]
        target = "blocked"
        written = 0
        if not dry_run:
            details: dict[str, Any] = {"stage": fact["stage"], "attempt": fact["attempt"]}
            if fact["details"].get("budget") is not None:
                details["budget"] = fact["details"]["budget"]
            result = deals.set_blocked(
                conn,
                deal_id,
                kind=fact["kind"] or runner.FAIL_CAPABILITY,
                reason=reason,
                author=SOURCE,
                source=SOURCE,
                details=details,
                now=at,
            )
            written = int(result.get("events") or 0)
        return {
            "deal": deal_id,
            "action": ACTION_BLOCKED,
            "from": RUNNING_STATUS,
            "to": target,
            "stage": fact["stage"],
            "attempt": fact["attempt"],
            "reason": reason,
            "written": written,
            "dry_run": bool(dry_run),
        }
    reason = (
        f"попытки исчерпаны: {fact['kind'] or 'без класса'} на этапе {fact['stage']}, "
        f"причина последнего отказа: {fact['reason'] or 'не записана'}"
    )
    if dry_run:
        return {
            "deal": deal_id,
            "action": ACTION_PARKED,
            "from": RUNNING_STATUS,
            "to": "blocked",
            "stage": fact["stage"],
            "attempt": fact["attempt"],
            "reason": reason,
            "written": 0,
            "dry_run": True,
        }
    parked = runner.park_exhausted(
        conn,
        deal_spec,
        deal,
        stage,
        fact["attempt"],
        fact["kind"],
        fact["reason"],
        home=Path(home),
        clock=lambda: float(at),
    )
    if parked is None:
        # Журнал говорит «попытки исчерпаны», политика — «повтор разрешён»: два
        # правила разошлись, и выбирать за них проход не вправе.
        return {
            "deal": deal_id,
            "error": (
                f"transit говорит failed({fact['kind']}), а политика этапа "
                f"{fact['stage']!r} повтор разрешает — парковка не записана"
            ),
            "written": 0,
            "dry_run": False,
        }
    return {
        "deal": deal_id,
        "action": ACTION_PARKED,
        "from": RUNNING_STATUS,
        "to": "blocked",
        "stage": fact["stage"],
        "attempt": fact["attempt"],
        "question_id": parked.get("question_id"),
        "reason": reason,
        "written": 1,
        "dry_run": False,
    }


def _parked_gate(
    conn: Any, deal: dict[str, Any], deal_spec: Any, decision: Any, at: int, *, dry_run: bool
) -> Optional[dict[str, Any]]:
    """Вопрос гейта истёк: парковка гейта — тем же писателем, что в прогоне."""
    stage = _stage_of(deal_spec, decision.stage)
    if stage is None or str(getattr(stage, "kind", "")) != spec_module.GATE_KIND:
        return None
    reason = str(getattr(decision, "reason", "") or "")
    if dry_run:
        return {
            "deal": str(deal["id"]),
            "action": ACTION_PARKED,
            "from": RUNNING_STATUS,
            "to": "blocked",
            "stage": str(decision.stage or ""),
            "gate": True,
            "reason": reason,
            "written": 0,
            "dry_run": True,
        }
    # `park_gate` идемпотентен своим `set_blocked` (ключ от класса и причины), а
    # открытой парковки перед вызовом нет — значит факт в журнале теперь есть.
    fact = runner.park_gate(
        conn,
        deal,
        stage,
        reason,
        question_id=getattr(decision, "question_id", None),
        clock=lambda: float(at),
    )
    return {
        "deal": str(deal["id"]),
        "action": ACTION_PARKED,
        "from": RUNNING_STATUS,
        "to": "blocked",
        "stage": str(decision.stage or ""),
        "gate": True,
        "reason": str(fact.get("reason") or reason),
        "written": 1,
        "dry_run": False,
    }


# ------------------------------------------------------------------ вход CLI


def release(
    home: Any,
    *,
    only: Any = None,
    now: Any = None,
    grace_sec: int = RELEASE_GRACE_SEC,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Вход `bin/duty reap`: открыть свой store и отпустить дела уборки.

    Store открывается **без создания файла**: отпускать по несуществующему store
    — это «отпускать нечего», а не «завёл пустую БД». Прохода по чужим доскам
    здесь нет и быть не может: `sweep` смотрит только свои дела.
    """
    home = Path(home)
    at = int(time.time()) if now is None else int(now)
    store = deals.store_path(home)
    conn = deals.open_store(home, create=False)
    if conn is None:
        return {
            "store": str(store),
            "dry_run": bool(dry_run),
            "count": 0,
            "released": [],
            "errors": [],
            "note": f"store ещё нет: {store} — отпускать нечего",
        }
    try:
        rows = sweep(conn, at, home=home, only=only, grace_sec=grace_sec, dry_run=dry_run)
    finally:
        conn.close()
    released = [row for row in rows if not row.get("error")]
    errors = [row for row in rows if row.get("error")]
    note = ""
    if dry_run and released:
        note = "ничего не записано: --dry-run показывает, что отпустил бы проход"
    return {
        "store": str(store),
        "dry_run": bool(dry_run),
        "count": len(released),
        "released": released,
        "errors": errors,
        "note": note,
    }
