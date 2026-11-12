"""Тулсет `duty` — инструменты лида над журналом дел.

Контракт — `docs/ENGINE-2.0.md`, разделы 4–6: шедулером дела становится лид.
План предлагает `duty_plan`, этапы поднимает `duty_start`, факты читает
`duty_wait`, дело закрывает `duty_close`. Вопросы воркера — три исхода раздела 6:
`duty_answer` (лид отвечает сам и сессия воркера продолжается), `duty_ask`
(под-попытка профиля, ответ возвращается лиду строкой) и `duty_ask_human`
(вопрос в пул, дело встаёт в `wait_human`).

Своего состояния у модуля нет. Всё, что инструмент узнаёт, он читает из журнала
`<home>/duty/duty.db` (журнал — единственная правда, `docs/JOURNAL.md`), а всё,
что решает, — пишет фактом. Отсюда два свойства, ради которых это так:

1. **Повтор безопасен.** Каждый мутирующий инструмент несёт `idempotency_key`;
   ключ факта считается от него (`deals.event_key`), поэтому повтор вызова после
   падения лида даёт тот же ключ, `INSERT OR IGNORE` второго факта не пишет, а
   инструмент отвечает прежним результатом.
2. **Инструмент сначала пишет факт, потом действует.** Лид, упавший между
   вызовом и ответом, не теряет решения: оно видно в журнале и в дайджесте.

Почему `duty_wait` блокирующий (`docs/ENGINE-2.0.md`, раздел 4): каждый вызов
инструмента — это пересылка всего контекста заново, поэтому один блокирующий
вызов на 120–300 секунд дешевле шестидесяти опросов по 5 секунд. Ответ человека
на `duty_ask_human` приходит лиду следующим фактом: пул дочитывает сам
`duty_wait` (`ask.sync_answers`), потому что шедулера, который делал это перед
каждым решением, в 2.0 нет.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# Тулсет инструментов лида: под этим именем их видит профиль `lead`
# (`platform_toolsets`, `docs/ENGINE-2.0.md`, раздел 4).
TOOLSET = "duty"

# Источник фактов лида в журнале. Отличается от `engine`: по нему видно, что
# решение принял лид, а не движок (`docs/ENGINE-2.0.md`, раздел 9 — лента решений).
SOURCE = "lead"

# Откуда инструмент берёт корень **дела** (`docs/ENGINE-2.0.md`, раздел 4).
#
# Корень дела и корень сессии — разные вещи, и до приёмки 2.0 (дело d_eafce375)
# они молча смешивались в один `HERMES_HOME`. Сессия лида живёт в профиле
# (`HERMES_HOME=<корень>/profiles/lead`), а journal и store дела — в корне, куда
# дело заводили (`<корень>/duty/duty.db`). Поэтому супервизор называет корень
# дела отдельной переменной (`dashboard/lead.py`, `child_env`), а инструмент
# читает её первой: иначе `duty_plan` искал бы дело в профиле и отвечал «дела
# нет в store» на каждом вызове.
DUTY_HOME_ENV = "HERMES_DUTY_HOME"
# Корень Hermes сессии — второй источник: без `HERMES_DUTY_HOME` поведение
# прежнее (`bin/duty demo`, тесты, ручные вызовы).
HOME_ENV = "HERMES_HOME"

# Сколько попыток дела может идти одновременно (`docs/ENGINE-2.0.md`, раздел 11).
# Число то же, что у пачки движка (`transit.MAX_PARALLEL`), и проверяется здесь,
# потому что этапы теперь поднимает лид, а не фолд.
MAX_SLOTS = 2

# Потолок одного блокирующего вызова `duty_wait` и шаг его опроса журнала.
WAIT_CEILING_SEC = 300.0
WAIT_POLL_SEC = 0.5

# Сколько ждать факта `attempt_started` после спавна попытки: попытку поднимает
# отдельный процесс, и ответ инструмента обязан опираться на журнал, а не на
# факт спавна.
START_ACK_SEC = 10.0
START_POLL_SEC = 0.1

# Виды событий, которые лида не будят: телеметрия попытки (`attempt_progress`)
# пишется на каждый вызов инструмента воркера, и «новых фактов десятки» вместо
# «этап отчитался» стоило бы лиду турна на пустом месте.
NOISE_KINDS = ("attempt_progress",)

# Приговор, которым дело закрывается. Дело закрывает слово лида
# (`docs/ENGINE-1.0.md`, раздел 9), и это слово одно: провал гасит
# `duty_cancel_deal`, возврат в работу — новая ревизия плана.
CLOSE_VERDICT = "approve"

# Подкаталог полных логов попыток, которые поднял лид: журнал отвечает «чем
# кончилось», а не заменяет лог.
ATTEMPT_LOG_SUBDIR = "lead"

# «Этап» под-попытки роли для вопроса самого лида (`duty_ask`): у вопроса лида
# нет этапа графа, а `runner.ask_role` адресует свой контракт этапом и попыткой.
# Имя не совпадает ни с одним этапом спеки (`plan`, `close`, `impl`…), поэтому
# срез дела такой вопрос к этапу не относит.
ASK_STAGE = "ask"

# Вид блокировки, которым дело ждёт человека (`duty_ask_human`): тот же, что
# ставит движок под вопрос попытки, — «нужен ввод», а не «нужен доступ».
NEEDS_INPUT_KIND = "needs_input"

# Как часто `duty_wait` дочитывает пул ответов (`ask.sync_answers`): ответ
# человека — следующий факт лиду, но опрашивать пул на каждом тике ожидания
# (0.5 с) незачем — пул читается отдельным соединением.
WAIT_SYNC_SEC = 5.0

JOURNAL_MODULE_NAME = "hermes_duty_journal"
DEALS_MODULE_NAME = "hermes_duty_deals"
SPEC_MODULE_NAME = "hermes_duty_spec"
TRANSIT_MODULE_NAME = "hermes_duty_transit"
RUNNER_MODULE_NAME = "hermes_duty_runner"
STAGEFEED_MODULE_NAME = "hermes_duty_stagefeed"
BUDGET_MODULE_NAME = "hermes_duty_budget"
ASK_MODULE_NAME = "hermes_duty_ask"
ROUTING_MODULE_NAME = "hermes_duty_routing"
PUBLISH_MODULE_NAME = "hermes_duty_publish"


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
stagefeed = _load(STAGEFEED_MODULE_NAME, "stagefeed.py")
budget = _load(BUDGET_MODULE_NAME, "budget.py")
ask_module = _load(ASK_MODULE_NAME, "ask.py")
routing = _load(ROUTING_MODULE_NAME, "routing.py")
publish = _load(PUBLISH_MODULE_NAME, "publish.py")


class ToolsError(RuntimeError):
    """Отказ инструмента: вход не тот, дело не в том состоянии, факта нет.

    Это не падение: отказ уезжает лиду ответом (`ok: false` и причина), потому
    что решение принимает он, а не трейсбек.
    """


# ------------------------------------------------------------------ мелочи


def _int(value: Any) -> int:
    """Целое из факта: `True` и мусор — ноль, а не исключение."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _now(now: Any) -> int:
    """Момент факта: аргумент (тесты) или текущие часы."""
    return int(time.time() if now is None else now)


def _key(idempotency_key: Any) -> str:
    """Ключ идемпотентности вызова; пустой — отказ.

    Без ключа повтор не отличить от нового решения: лид, упавший между вызовом и
    записью, поднял бы вторую попытку или вторую ревизию плана
    (`docs/ENGINE-2.0.md`, раздел 4).
    """
    key = str(idempotency_key or "").strip()
    if not key:
        raise ToolsError(
            "idempotency_key не назван: мутирующий инструмент без ключа не отличает "
            "повтор от нового решения"
        )
    return key


def hermes_home(home: Optional[Path] = None) -> Path:
    """Корень **дела**: аргумент, `$HERMES_DUTY_HOME`, `$HERMES_HOME`, `~/.hermes`.

    Одна точка на все инструменты: журнал, store и лизинги живут в одном
    `<home>/duty/duty.db`, и второй способ их найти разошёлся бы с первым.

    `$HERMES_DUTY_HOME` идёт первым, потому что корень сессии — не корень дела:
    инструменты лида работают внутри сессии профиля (`HERMES_HOME` там
    `<корень>/profiles/lead`), а дело заведено в корне Hermes. Супервизор
    называет этот корень переменной перед спавном (`dashboard/lead.py`,
    `child_env`); без неё инструмент честно падал бы «дела нет в store» на
    каждом вызове — ровно это и показала приёмка 2.0 (дело d_eafce375).
    """
    if home is not None:
        return Path(home).expanduser()
    for name in (DUTY_HOME_ENV, HOME_ENV):
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return Path.home() / ".hermes"


def _store(home: Path) -> sqlite3.Connection:
    """Соединение со store своего дела: журнал и строка дела в одном файле."""
    conn = deals.open_store(home)
    if conn is None:  # pragma: no cover - `create=True` файла не теряет
        raise ToolsError(f"store is unavailable under {home}")
    return conn


def _deal(conn: sqlite3.Connection, deal_id: Any) -> dict[str, Any]:
    """Строка своего дела; `t_…`, мусор и отсутствие строки — отказ словами."""
    try:
        row = deals.deal_row(conn, str(deal_id or ""))
    except deals.DealError as exc:
        raise ToolsError(str(exc)) from exc
    if row is None:
        raise ToolsError(f"дела {deal_id!r} нет в store: инструменты лида работают со своими делами d_<hex>")
    return row


def _spec(events: Iterable[Any]) -> Any:
    """Спека дела по журналу (`created.payload.spec`) — единственное её имя.

    Второго источника «какая спека у дела» в плагине нет: путь записан при
    создании дела, а не угадывается по каталогу.
    """
    path = stagefeed.spec_of(events)
    if not path:
        raise ToolsError("у дела нет спеки: created.payload.spec пуст — этапы не из чего читать")
    try:
        return spec_module.load_spec(path)
    except spec_module.SpecError as exc:
        raise ToolsError(f"спека дела не читается: {exc}") from exc


def _issues_text(issues: Iterable[Any]) -> list[str]:
    """Отказы плана словами: `правило: текст`, без псевдопути предложения.

    Форма та же, что у отказов плана движка (`runner._plan_issues`): лид читает
    правило и текст, а `<plan>:1` — строка файла, которого нет.
    """
    out: list[str] = []
    for issue in issues:
        rule = str(getattr(issue, "rule", "") or "")
        message = str(getattr(issue, "message", "") or "")
        out.append(f"{rule}: {message}" if rule else (message or str(issue)))
    return out


def _lead_event(deal: dict[str, Any], kind: str, payload: dict[str, Any], args: dict[str, Any], now: int) -> dict[str, Any]:
    """Факт лида: ключ идемпотентности считается от аргументов вызова.

    Формат ключа — тот же, что у остальных писателей
    (`<source>:<deal_id>:<kind>:<sha1(аргументы)>`, `docs/JOURNAL.md`): повтор
    вызова даёт тот же ключ, и второго факта в журнале не появляется.
    """
    return {
        "ts": now,
        "recorded_at": now,
        "deal_id": str(deal["id"]),
        "kind": kind,
        "payload": payload,
        "source": SOURCE,
        "idempotency_key": deals.event_key(SOURCE, str(deal["id"]), kind, args),
    }


def _by_idem(events: Iterable[Any], kind: str, key: str) -> Optional[dict[str, Any]]:
    """Первый факт названного вида с этим ключом вызова — прежний результат.

    Ключ лежит в `payload.idem`: по колонке `idempotency_key` факт не найти —
    она хеш аргументов, и восстанавливать из неё ключ вызова значило бы
    повторить хеширование вместо чтения факта.
    """
    for event in events or ():
        if str(event.get("kind") or "") != kind:
            continue
        if str(journal.payload_of(event).get("idem") or "") == key:
            return event
    return None


def _plan_rev(events: Iterable[Any]) -> int:
    """Номер ревизии для нового предложения: на единицу больше последней.

    Считаются только факты плана целиком (без `stage`): у ревизий этапа плана
    движка тот же счётчик (`runner.report_plan`), и второй счётчик разошёлся бы с
    ним на первом же отказе.
    """
    top = 0
    for event in events or ():
        if str(event.get("kind") or "") not in (transit.PLAN_PROPOSED, transit.PLAN_ACCEPTED, transit.PLAN_REJECTED):
            continue
        payload = journal.payload_of(event)
        if str(payload.get("stage") or ""):
            continue  # факт этапа, а не факт плана
        top = max(top, _int(payload.get("rev")))
    return top + 1


def _live_attempts(events: Iterable[Any], now: int) -> list[dict[str, Any]]:
    """Незакрытые попытки дела, которые считаются живыми, — по журналу и лизингу.

    Живость считает лента этапов (`stagefeed._alive`): закрытая попытка мертва
    по факту, незакрытая с действующим лизингом — живая. Попытка без срока
    лизинга (`live: None`) считается занятой: «не знаю» — не повод поднять
    вторую работу поверх неё.
    """
    return [row for row in stagefeed.attempts(events, now) if row.get("live") is not False]


def _next_attempt(events: Iterable[Any], stage_id: str) -> int:
    """Номер новой попытки этапа: на единицу больше последней по журналу."""
    top = 0
    for row in stagefeed.attempts(events, 0):
        if str(row.get("stage") or "") == stage_id:
            top = max(top, _int(row.get("n")))
    return top + 1


def _question_stage(payload: dict[str, Any]) -> str:
    """Этап, которому принадлежит вопрос: `payload.routing.stage`.

    `ask()` пишет `question_asked` на уровне **дела** (у пула нет этапа), а адрес
    кладёт движок — `payload.routing` (`dashboard/ask.py`, «форма журнала от
    этого не меняется»). Пусто — вопрос задан мимо движка (`POST /ask`).
    """
    address = payload.get("routing")
    if not isinstance(address, dict):
        return ""
    return str(address.get("stage") or "")


def _question_number(payload: dict[str, Any]) -> str:
    """Номер вопроса в пуле строкой; пусто — вопрос роли (строки пула нет)."""
    return str(payload.get("question_id") or "")


def _question_pair(events: Iterable[Any], stage_id: str, number: str) -> tuple[Optional[dict[str, Any]], bool]:
    """Вопрос этапа и отвечен ли он: последний `question_asked` и факт ответа.

    Пара «вопрос → ответ» сверяется и этапом, и номером пула: у вопроса роли
    номера нет, и его закрывает ответ без номера по тому же адресу — ровно так
    же, как это делает срез дела (`dashboard/transit.py`, `_deal_answer`).
    Чужой ответ (другой этап, другой номер) вопрос не закрывает.
    """
    asked: Optional[dict[str, Any]] = None
    answered = False
    for event in events or ():
        kind = str(event.get("kind") or "")
        if kind not in ("question_asked", "question_answered"):
            continue
        payload = journal.payload_of(event)
        if _question_stage(payload) != stage_id or _question_number(payload) != number:
            continue
        if kind == "question_asked":
            asked, answered = event, False
        elif asked is not None:
            answered = True
    return asked, answered


def _open_question(events: Iterable[Any]) -> Optional[dict[str, Any]]:
    """Последний вопрос дела без ответа — по тому же правилу, что у среза дела.

    Своей пары «вопрос → ответ» здесь нет: срез читает её `journal._open_question`,
    и второй разбор разошёлся бы с ним на первом же вопросе без номера (у вопроса
    роли номера пула не существует). `None` — открытых вопросов нет.
    """
    ordered = sorted(
        (item for item in events or () if isinstance(item, dict)),
        key=lambda item: int(item.get("id") or 0),
    )
    found = journal._open_question(ordered)
    if found is None:
        return None
    return journal.payload_of(found[1])


def _awaiting(events: Iterable[Any]) -> tuple[str, int]:
    """Этап и попытка, которые ждут ответа: адрес открытого вопроса дела.

    Вопрос лида (`duty_ask`, `duty_ask_human`) задаётся ради открытого вопроса
    воркера, и адресом ему служит тот же этап: тогда срез дела показывает
    ожидание там, где оно есть, а не у последней попытки журнала. Открытых
    вопросов нет — `("", 0)`, и вопрос адресуется как вопрос мимо движка.
    """
    payload = _open_question(events)
    if not payload:
        return "", 0
    address = payload.get("routing")
    attempt = _int(address.get("attempt")) if isinstance(address, dict) else 0
    return _question_stage(payload), attempt


def _by_routing_idem(events: Iterable[Any], kind: str, key: str) -> Optional[dict[str, Any]]:
    """Факт этого вида с ключом вызова в `payload.routing.idem` — прежний вопрос лида.

    `ask()` пишет `question_asked` сам (`dashboard/ask.py`), и ключ вызова ему
    передаётся адресом: `payload.routing` уезжает в факт целиком, поэтому
    `duty_ask_human` находит свой прежний вопрос там же, где остальные
    инструменты находят свой — по `payload.idem`.
    """
    for event in reversed(list(events or ())):
        if str(event.get("kind") or "") != kind:
            continue
        address = journal.payload_of(event).get("routing")
        if isinstance(address, dict) and str(address.get("idem") or "") == key:
            return event
    return None


# ------------------------------------------------------------------- duty_plan


def plan(
    deal_id: Any,
    stages: Any,
    reason: Any,
    idempotency_key: Any,
    *,
    home: Optional[Path] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Предложить план дела и записать решение по нему.

    Валидатор — тот же `spec.parse_plan`, что у спеки, против хартии дела
    (`docs/ENGINE-2.0.md`, разделы 3 и 4): роли и виды из `charter`, `needs` по
    известному графу, `evidence` у этапа, который что-то производит, потолок
    `max_stages`. Своего набора проверок здесь нет — второй валидатор разошёлся
    бы со `spec check` на первом же новом поле.

    Успех — `plan_proposed` + `plan_accepted`: ревизия становится графом дела.
    Отказ — `plan_proposed` + `plan_rejected(issues[])`: граф не меняется, и ни
    один этап этого плана не запускается никогда — лид обязан переписать план.
    """
    key = _key(idempotency_key)
    home = hermes_home(home)
    moment = _now(now)
    conn = _store(home)
    deal = _deal(conn, deal_id)
    events = journal.read_events(conn, str(deal["id"]))
    if journal.closed_fact(events) is not None:
        raise ToolsError(f"дело {deal['id']} закрыто: план ему больше не нужен")
    spec = _spec(events)
    proposed = _by_idem(events, transit.PLAN_PROPOSED, key)
    if proposed is not None:
        return _plan_repeat(events, proposed, key)
    graph = transit.graph(spec, events)
    rev = _plan_rev(events)
    parsed, issues = spec_module.parse_plan(
        stages,
        charter=getattr(spec, "charter", None) or spec_module.Charter(),
        base_ids=[stage.id for stage in graph],
        base_needs={stage.id: stage.needs for stage in graph},
        started_ids=transit.started_stages(events),
        repo=getattr(spec, "repo", None),
    )
    proposal = _lead_event(
        deal,
        transit.PLAN_PROPOSED,
        {
            "rev": rev,
            "stages": stages if isinstance(stages, list) else [],
            "reason": str(reason or "").strip(),
            "source_stage": SOURCE,
            "idem": key,
        },
        {"rev": rev, "idem": key},
        moment,
    )
    if issues:
        text = _issues_text(issues)
        rejected = _lead_event(
            deal,
            transit.PLAN_REJECTED,
            {"rev": rev, "why": f"план ревизии {rev} отклонён: отказов {len(text)}", "issues": text, "idem": key},
            {"rev": rev, "idem": key},
            moment,
        )
        journal.append(conn, [proposal, rejected])
        return {
            "ok": True,
            "deal": str(deal["id"]),
            "rev": rev,
            "accepted": False,
            "issues": text,
            "text": "\n".join(text),
            "events": [transit.PLAN_PROPOSED, transit.PLAN_REJECTED],
        }
    accepted = _lead_event(
        deal,
        transit.PLAN_ACCEPTED,
        {"rev": rev, "by": SOURCE, "idem": key},
        {"rev": rev, "idem": key},
        moment,
    )
    # Топология — подсказка, а не отказ (`docs/ENGINE-2.0.md`, раздел 8): граф
    # пишет тот же лид, что по нему идёт, и может его переписать, а жёсткая
    # гарантия живёт в `duty_start` по фактам. Поэтому план без ревью перед
    # публикацией принимается, но лид видит предупреждение сразу, а не узнаёт о
    # нём отказом `duty_start` через два этапа.
    warnings = _plan_warnings(spec, events, proposal, accepted)
    written = journal.append(conn, [proposal, accepted])
    return {
        "ok": True,
        "deal": str(deal["id"]),
        "rev": rev,
        "accepted": True,
        "stages": [stage.id for stage in parsed],
        "graph": [stage.id for stage in transit.graph(spec, journal.read_events(conn, str(deal["id"])))],
        "warnings": warnings,
        "events": [transit.PLAN_PROPOSED, transit.PLAN_ACCEPTED],
        "written": written,
    }


def _plan_warnings(
    spec: Any, events: Iterable[Any], proposal: dict[str, Any], accepted: dict[str, Any]
) -> list[str]:
    """Предупреждения топологии на графе, который план только что произвёл.

    Читается тем же `transit.graph`, что и дело: граф «seed плюс принятый план»
    нельзя собрать вторым способом — он разошёлся бы с тем, по которому пойдут
    этапы. Факты ещё не записаны, поэтому предложение и решение подставляются в
    журнал читателя с номерами сразу после последнего (`id` — и есть порядок).
    """
    preview = list(events)
    head = max((_int(event.get("id")) for event in preview), default=0)
    preview += [{**proposal, "id": head + 1}, {**accepted, "id": head + 2}]
    graph = transit.graph_spec(spec, preview)
    return _issues_text(spec_module.gate_warnings(graph))


def _plan_repeat(events: Iterable[Any], proposed: dict[str, Any], key: str) -> dict[str, Any]:
    """Ответ на повтор `duty_plan` с тем же ключом: прежнее решение, без фактов.

    Ревизия берётся из самого предложения: после первого вызова счётчик ревизий
    уже сдвинут, и пересчитывать его значило бы ответить про другую ревизию.
    """
    payload = journal.payload_of(proposed)
    rev = _int(payload.get("rev"))
    raw = payload.get("stages")
    named = [str(item.get("id") or "") for item in raw or () if isinstance(item, dict)]
    accepted = _by_idem(events, transit.PLAN_ACCEPTED, key)
    rejected = _by_idem(events, transit.PLAN_REJECTED, key)
    if rejected is not None:
        issues = [str(item) for item in journal.payload_of(rejected).get("issues") or () if str(item)]
        return {
            "ok": True,
            "deal": str(proposed.get("deal_id") or ""),
            "rev": rev,
            "accepted": False,
            "issues": issues,
            "text": "\n".join(issues),
            "repeated": True,
        }
    return {
        "ok": True,
        "deal": str(proposed.get("deal_id") or ""),
        "rev": rev,
        "accepted": accepted is not None,
        "stages": [name for name in named if name],
        "repeated": True,
    }


# ------------------------------------------------------------------ duty_start


def _risky(stage: Any) -> bool:
    """Этап объявлен рискованным: вопрос человеку обязателен (`ask.risky`).

    Признак тот же, по которому спека требовала ревью в предках (`spec._check_review_gate`):
    рискованная работа не начинается без ответа человека (`docs/ENGINE-2.0.md`,
    раздел 8 — «нет ответа — нет старта»).
    """
    ask_decl = getattr(stage, "ask", None)
    return bool(getattr(ask_decl, "risky", False)) if ask_decl is not None else False


def _ancestor_probe(spec: Any) -> Optional[Callable[[str, str], Optional[bool]]]:
    """Родство коммитов по репозиторию дела; `None` — спросить не у кого.

    `publish.py` — слой без git, и родство ему подставляют. Подставляется ровно
    тот же `git merge-base --is-ancestor`, которым движок сравнивает базы этапов:
    второй способ решать «предок ли» разошёлся бы с первым. Репозитория нет —
    `None`, и отказ назовёт «проверить нечем», а не выдумает совпадение.
    """
    repo_text = str(getattr(spec, "repo", "") or "").strip()
    if not repo_text:
        return None
    repo = Path(repo_text).expanduser()
    if not repo.is_dir():
        return None
    return lambda older, newer: runner._ancestor(repo, older, newer)


def _publish_settings(spec: Any) -> tuple[str, bool]:
    """Роль-ревьюер и человеческий гейт из хартии: `(reviewer_role, human_gate)`.

    Оба поля объявлены в `charter.publish` (`docs/ENGINE-2.0.md`, разделы 3 и 8) и
    до сих пор не читались движком: гейт публикации фильтровал этапы по константе
    `spec.REVIEWING_ROLE`, а `human_gate` не спрашивал никто. Теперь роль берётся
    из хартии (умолчание — та же константа, поэтому спека без `publish` ведёт себя
    как 1.x), а `human_gate` работает так: объявленный `true` — человека перед
    публикацией спрашивают **по факту** ответа на гейт, объявленный `false` — нет
    (рискованный этап спрашивается всё равно, `ask.risky` сильнее хартии).

    `human_gate` действует только когда блок `publish` в хартии **объявлен**
    (`Publish.declared`): иначе умолчание `true` включило бы гейт у спек 1.x, где
    его не было, и обещание «нет блока — поведение 0.0.8» стало бы ложью.
    """
    publish_decl = getattr(getattr(spec, "charter", None), "publish", None)
    declared = bool(getattr(publish_decl, "declared", False))
    human_gate = bool(getattr(publish_decl, "human_gate", False)) if declared else False
    return spec_module.reviewer_role(spec), human_gate


def _publish_refusal(spec: Any, stage: Any, events: Iterable[Any]) -> Optional[str]:
    """Отказ публикации по фактам — или `None`, если попытку поднимать можно.

    Жёсткая гарантия 2.0 (`docs/ENGINE-2.0.md`, раздел 8) живёт здесь, а не в
    форме графа: граф пишет тот же лид, что по нему идёт, и может его переписать,
    поэтому «публикация не обгонит ревью» доказывается историей фактов — два
    приговора, оба из журнала:

    - у публикующего этапа — `publish.review()`: approve ревьюера, его коммит в
      истории публикуемой базы, и после approve нет возврата на доработку;
    - у рискованного — `publish.approval()`: ответ человека на гейт вариантом из
      `allow`, и с момента вопроса дерево не менялось.

    Роль ревьюера и необходимость вопроса человеку берутся из хартии дела
    (`_publish_settings`): дело вправе назвать ревьюером не `reviewer`, а другую
    роль, и вправе объявить, что человека перед публикацией не спрашивают.

    Оба смотрят на **базу публикуемого дерева** (`runner.resolved_base`), то есть
    на коммит, который действительно уйдёт в origin. Проверка стоит до лизинга и
    спавна: отказ не пишет фактов и не занимает слот.
    """
    publishing = runner.publishes(stage)
    risky = _risky(stage)
    if not publishing and not risky:
        return None
    reviewer_role, human_gate = _publish_settings(spec)
    events = list(events)
    try:
        # База может не сойтись у самого движка (разошедшиеся предшественники):
        # тогда публиковать не от чего, и это отказ, а не пустая сверка.
        current = runner.resolved_base(spec, stage, events)
    except runner.BaseError as exc:
        return f"этап {stage.id}: {exc}"
    if publishing:
        reviewers = [
            item.id
            for item in spec.stages
            if str(getattr(item, "role", "") or "") == reviewer_role
        ]
        approval = publish.review(
            events, reviewers, current=current, ancestor=_ancestor_probe(spec), role=reviewer_role
        )
        if not approval.ok:
            return f"этап {stage.id}: {approval.reason}"
    if not risky and not human_gate:
        return None
    gates = runner.publish_gates(spec, stage)
    if not gates:
        needs = (
            "объявлен рискованным (ask.risky)"
            if risky
            else "публикует, а хартия требует вопроса человеку (publish.human_gate)"
        )
        return (
            f"этап {stage.id} {needs}, а гейта "
            f"(kind: {spec_module.GATE_KIND}) в его предках нет: человека никто не "
            "спросит — нет ответа, нет старта"
        )
    # За рискованным этапом публикации может и не быть: коммит тогда гейт не
    # называет (`pin=None`), и сверять нечего — но факт ответа человека
    # требуется так же строго, поэтому `pin` ослабляет только сверку дерева.
    approval = publish.approval(events, gates, current=current, pin=True if publishing else None)
    if not approval.ok:
        return f"этап {stage.id}: {approval.reason}"
    return None


def start(
    deal_id: Any,
    stage_id: Any,
    idempotency_key: Any,
    *,
    home: Optional[Path] = None,
    spawn: Optional[Callable[..., int]] = None,
    clock: Optional[Callable[[], float]] = None,
    ack_sec: float = START_ACK_SEC,
) -> dict[str, Any]:
    """Поднять попытку этапа отсоединённым процессом и ответить её фактом.

    Отказы — до спавна и по фактам журнала (`docs/ENGINE-2.0.md`, разделы 5, 8 и
    11): этапа нет в принятом плане; серия попыток этапа упёрлась в
    `charter.max_attempts`; по этапу уже идёт живая попытка; живых попыток в деле
    не меньше `MAX_SLOTS`; бюджет дела исчерпан; публикация без приговора ревью (и
    без ответа человека, если этап рискованный или хартия требует гейта) —
    `_publish_refusal`.
    Ни один отказ ничего не пишет — журнал не растёт, слот не занимается.

    Попытку исполняет **отдельный процесс в своей группе** (`start_new_session`):
    падение лида не убивает работу, а падение работы не убивает лида
    (`docs/ENGINE-2.0.md`, раздел 2). Здесь только спавн и ожидание факта;
    лизинг, дерево, контракт и отчёт остаются делом `runner.run_stage`.
    """
    key = _key(idempotency_key)
    name = str(stage_id or "").strip()
    if not name:
        raise ToolsError("stage_id не назван: duty_start(deal, stage_id, idempotency_key)")
    home = hermes_home(home)
    tick = clock or time.time
    conn = _store(home)
    deal = _deal(conn, deal_id)
    events = journal.read_events(conn, str(deal["id"]))
    if journal.closed_fact(events) is not None:
        raise ToolsError(f"дело {deal['id']} закрыто: попытки ему не поднимаются")
    spec = _spec(events)
    graph = transit.graph_spec(spec, events)
    stage = next((item for item in graph.stages if str(getattr(item, "id", "")) == name), None)
    if stage is None:
        named = ", ".join(str(getattr(item, "id", "")) for item in graph.stages) or "—"
        raise ToolsError(f"этапа {name!r} нет в принятом плане: этапы дела — {named}")
    cancelled = _stage_cancelled(events, name)
    repeated = _by_idem(events, "attempt_started", key)
    if repeated is not None:
        payload = journal.payload_of(repeated)
        return {
            "ok": True,
            "started": True,
            "repeated": True,
            "stage": name,
            "attempt": _int(payload.get("attempt")),
            "run_id": str(payload.get("run_id") or ""),
            "pid": payload.get("pid"),
        }
    if cancelled is not None:
        # Отменённый этап не воскресает: иначе «лид отказался от работы» значило
        # бы только «попытка погашена», и следующий шаг вернул бы её в граф.
        raise ToolsError(
            f"этап {name!r} отменён лидом ({cancelled or 'причина не записана'}): "
            "попытки по нему не поднимаются — вернуть работу можно только новым "
            "этапом в новой ревизии плана"
        )
    # Потолок серии — хартия дела (`charter.max_attempts`, раздел 11 контракта):
    # «3 попытки на этап, включая авто-повторы». До этой проверки серию
    # ограничивал только `retry.max` этапа (для авто-повторов) — число ручных
    # `duty_start` по одному этапу не ограничивал никто.
    attempt = _next_attempt(events, name)
    ceiling = spec_module.attempts_ceiling(graph)
    # Публикация проверяется по фактам до лизинга и спавна (раздел 8 контракта):
    # отказ не пишет события и не занимает слот, а причина называет, какого
    # именно факта не хватает — «публикация без ревью» видна ДО процесса.
    refusal = _publish_refusal(graph, stage, events)
    if refusal is not None:
        raise ToolsError(refusal)
    moment = int(tick())
    live = _live_attempts(events, moment)
    busy = next((row for row in live if str(row.get("stage") or "") == name), None)
    if busy is not None:
        raise ToolsError(
            f"этап {name!r} уже идёт: попытка {busy['run_id']} (pid {busy.get('pid')}) — "
            "слот этапа занят, дождись её факта или отмени её"
        )
    if attempt > ceiling:
        raise ToolsError(
            f"этап {name!r}: попыток {attempt - 1} из {ceiling} — серия исчерпана "
            "хартией (charter.max_attempts): новая попытка — новая ревизия плана, "
            "а не повтор duty_start"
        )
    if len(live) >= MAX_SLOTS:
        named = ", ".join(str(row.get("run_id") or "") for row in live)
        raise ToolsError(
            f"живых попыток {len(live)} из {MAX_SLOTS} ({named}): слот освободится, "
            "когда попытка отчитается"
        )
    cap = budget.refusal(spec, events)
    if cap is not None:
        raise ToolsError(cap.reason())
    run_id = f"{name}#{attempt}"
    spawner = spawn or _spawn_attempt
    pid = spawner(home, str(deal["id"]), name, attempt, key)
    started = _await_start(conn, str(deal["id"]), key, deadline=tick() + float(ack_sec), tick=tick)
    if started is None:
        return {
            "ok": True,
            "started": True,
            "confirmed": False,
            "deal": str(deal["id"]),
            "stage": name,
            "attempt": attempt,
            "run_id": run_id,
            "pid": pid,
            "reason": (
                f"процесс попытки поднят (pid {pid}), факта attempt_started ещё нет за "
                f"{int(ack_sec)} с — её события придут в duty_wait"
            ),
        }
    payload = journal.payload_of(started)
    return {
        "ok": True,
        "started": True,
        "confirmed": True,
        "deal": str(deal["id"]),
        "stage": name,
        "attempt": _int(payload.get("attempt")) or attempt,
        "run_id": str(payload.get("run_id") or run_id),
        "pid": payload.get("pid"),
        "workdir": payload.get("workdir"),
    }


def _await_start(
    conn: sqlite3.Connection, deal_id: str, key: str, *, deadline: float, tick: Callable[[], float]
) -> Optional[dict[str, Any]]:
    """Дождаться факта `attempt_started` этого вызова; `None` — за отведённое время его нет.

    Попытку поднимает отдельный процесс, и факт пишет он: ответ «стартовало» по
    одному спавну был бы обещанием, а не фактом журнала.
    """
    while True:
        found = _by_idem(journal.read_events(conn, deal_id), "attempt_started", key)
        if found is not None:
            return found
        if tick() >= deadline:
            return None
        time.sleep(START_POLL_SEC)


def _attempt_log(home: Path, deal_id: str, stage_id: str, attempt: int) -> Path:
    """Полный лог процесса попытки: `<home>/duty/lead/<этап>-<номер>.log`."""
    return journal.journal_path(home).parent / ATTEMPT_LOG_SUBDIR / f"{deal_id}-{stage_id}-{attempt}.log"


def _answer_file(home: Path, deal_id: str, stage_id: str, attempt: int) -> Path:
    """Текст ответа лида для процесса попытки — файлом, а не аргументом.

    Кавычки, переводы строк и длина ответа — не забота спавна: процесс читает
    файл, а журнал (`question_answered.answer`) остаётся местом, где ответ виден
    целиком.
    """
    return journal.journal_path(home).parent / ATTEMPT_LOG_SUBDIR / f"{deal_id}-{stage_id}-{attempt}.answer"


def _spawn_attempt(
    home: Path, deal_id: str, stage_id: str, attempt: int, key: str, answer: Optional[str] = None
) -> int:
    """Запустить попытку отдельным процессом в своей группе; вернуть его pid.

    `start_new_session=True` — своя группа процессов: kill супервизора лида не
    уносит работу (`docs/ENGINE-2.0.md`, раздел 2). Полный вывод процесса идёт
    файлом: причина падения должна быть видна, а журнал — не место для логов.

    `answer` — ответ лида (`duty_answer`): он едет файлом и означает
    «продолжи сессию этой попытки с ответом», а не «начни работу заново».
    """
    log = _attempt_log(home, deal_id, stage_id, attempt)
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(Path(__file__).resolve()), deal_id, stage_id, str(attempt), key, str(home)]
    if answer is not None:
        path = _answer_file(home, deal_id, stage_id, attempt)
        path.write_text(answer, encoding="utf-8")
        argv.append(str(path))
    handle = log.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(home),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        handle.close()
    return int(process.pid)


def _attempt_main(argv: list[str]) -> int:
    """Сам процесс попытки: лизинг, дерево, отчёт и факты — через `runner.run_stage`.

    Здесь только сборка: правила попытки живут в движке, и второй реализации
    «как идёт попытка» быть не должно. Ответ лида (`answer_file`) отличает
    продолжение от новой попытки — тот же резюм-путь движка (`--resume`).

    Попыток в процессе может быть несколько: `infra` и `contract` движок
    повторяет сам в пределах политики этапа (`runner.run_attempts`,
    `docs/ENGINE-2.0.md`, раздел 7), и лид видит серию фактами `attempt_*`, не
    поднимая каждый повтор вручную.
    """
    if len(argv) < 5:
        print(
            "usage: tools.py <deal_id> <stage_id> <attempt> <idempotency_key> <home> [answer_file]",
            file=sys.stderr,
        )
        return 2
    home = Path(argv[4]).expanduser()
    answer: Optional[str] = None
    if len(argv) > 5 and argv[5]:
        answer = Path(argv[5]).read_text(encoding="utf-8")
    try:
        conn = _store(home)
        deal = _deal(conn, argv[0])
        events = journal.read_events(conn, str(deal["id"]))
        spec = _spec(events)
        runner.run_attempts(
            conn, spec, deal, argv[1], _int(argv[2]), home=home, answer=answer, idem=argv[3]
        )
    except Exception:  # noqa: BLE001 - процесс отдельный: причина уходит в лог, а не лиду
        traceback.print_exc()
        return 1
    return 0


# ------------------------------------------------------------------- duty_wait


def wait(
    deal_id: Any,
    cursor: Any = 0,
    timeout_sec: Any = 0,
    *,
    home: Optional[Path] = None,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> dict[str, Any]:
    """Дождаться новых фактов дела: блокирующий вызов, пачка и курсор.

    Курсор — `id` последнего прочитанного события: лид передаёт его обратно, и
    второй вызов не пересказывает уже виденное. Телеметрия попыток
    (`attempt_progress`) лида не будит, но курсор двигает: иначе журнал
    перечитывался бы с того же места вечно.

    Пул ответов дочитывается здесь же (`ask.sync_answers`): ответ человека на
    `duty_ask_human` — следующий факт лиду, а шедулера, который делал это перед
    каждым решением, в 2.0 нет. Чтение пула идёт раз в `WAIT_SYNC_SEC`, а не на
    каждом тике: ожидание не должно превращаться в опрос чужой БД.

    Потолок `timeout_sec` — `WAIT_CEILING_SEC` (300 с): дольше держать турн лида
    нельзя, а пустой ответ `{events: [], timed_out: true}` — не ошибка, а факт
    «ничего не случилось» (`docs/ENGINE-2.0.md`, раздел 4).
    """
    home = hermes_home(home)
    tick = clock or time.time
    pause = sleep or time.sleep
    conn = _store(home)
    deal = _deal(conn, deal_id)
    position = max(0, _int(cursor))
    window = min(max(0.0, float(timeout_sec or 0.0)), WAIT_CEILING_SEC)
    deadline = tick() + window
    answers = 0
    synced_at = 0.0
    while True:
        if tick() - synced_at >= WAIT_SYNC_SEC:
            answers += _sync_answers(conn, home)
            synced_at = tick()
        fresh = journal.read_events(conn, str(deal["id"]), since_id=position)
        if fresh:
            position = max(position, max(_int(event.get("id")) for event in fresh))
        facts = [event for event in fresh if str(event.get("kind") or "") not in NOISE_KINDS]
        if facts:
            return {"ok": True, "deal": str(deal["id"]), "events": [_brief(event) for event in facts],
                    "cursor": position, "timed_out": False, "answers": answers}
        if tick() >= deadline:
            return {"ok": True, "deal": str(deal["id"]), "events": [], "cursor": position,
                    "timed_out": True, "answers": answers}
        pause(min(WAIT_POLL_SEC, max(0.0, deadline - tick())))


def _sync_answers(conn: sqlite3.Connection, home: Path) -> int:
    """Дочитать закрытые вопросы пула в журнал: сколько фактов `question_answered`.

    Писатель — `ask.sync_answers` (`dashboard/ask.py`): второго разбора пула в
    плагине нет, и ключ события там идемпотентный (`ask:<id>:question_answered`),
    поэтому повторный проход даёт ноль, а не второй факт. Сбой пула не отказ
    инструмента: ожидание журнала от недоступного пула не зависит.
    """
    report = ask_module.sync_answers(conn, home)
    return max(0, _int((report or {}).get("appended")))


def _brief(event: dict[str, Any]) -> dict[str, Any]:
    """Событие для лида: `id`, вид, время и payload как есть.

    Payload не пересказывается словами: дайджест лиду отдаёт пути к отчётам, а не
    их текст (`docs/ENGINE-2.0.md`, раздел 11), и вычислять здесь вторую правду
    значило бы завести её в инструменте.
    """
    return {
        "id": _int(event.get("id")),
        "kind": str(event.get("kind") or ""),
        "ts": _int(event.get("ts")),
        "source": str(event.get("source") or ""),
        "payload": journal.payload_of(event),
    }


# ------------------------------------------------------------------ duty_close


def close(
    deal_id: Any,
    summary: Any,
    verdict: Any,
    idempotency_key: Any,
    *,
    home: Optional[Path] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Закрыть дело итогом лида: `completed` с `cause='lead_closed'`.

    Приговор — `approve`: дело закрывает слово лида, а не пройденный граф
    (`docs/ENGINE-1.0.md`, раздел 9). Другой приговор делом не закрывает: провал
    гасит `duty_cancel_deal` (статус `cancelled`), а возврат в работу — новая
    ревизия плана (`docs/ENGINE-2.0.md`, раздел 5).

    Живые попытки — отказ: сначала их гасит `duty_cancel`, иначе закрытие
    оставило бы работающие процессы без хозяина. Повтор с тем же итогом
    второго `completed` не пишет: ключ факта считается от `cause`
    (`deals.close_stages_done`), а другой итог по закрытому делу — отказ.
    """
    _key(idempotency_key)
    word = str(verdict or "").strip().lower()
    if word != CLOSE_VERDICT:
        raise ToolsError(
            f"приговор {word or '—'!r} делом не закрывает: закрытие — "
            f"{CLOSE_VERDICT!r}; провал гасит duty_cancel_deal, возврат в работу — "
            "новая ревизия плана"
        )
    home = hermes_home(home)
    moment = _now(now)
    conn = _store(home)
    deal = _deal(conn, deal_id)
    events = journal.read_events(conn, str(deal["id"]))
    live = _live_attempts(events, moment)
    if live:
        named = ", ".join(str(row.get("run_id") or "") for row in live)
        raise ToolsError(
            f"в деле {len(live)} живых попыток ({named}): сначала погаси их duty_cancel, "
            "иначе закрытие оставит работу без хозяина"
        )
    text = " ".join(str(summary or "").split())
    closed = journal.closed_fact(events)
    if closed is not None:
        before = str(closed["payload"].get("summary") or "")
        if before != text:
            raise ToolsError(
                f"дело {deal['id']} уже закрыто другим итогом: {before or '—'} — "
                "закрытие не переписывается"
            )
        return {
            "ok": True,
            "deal": str(deal["id"]),
            "closed": True,
            "repeated": True,
            "cause": str(closed["payload"].get("cause") or ""),
            "summary": before,
        }
    spec = _spec(events)
    try:
        applied = deals.close_stages_done(
            conn,
            str(deal["id"]),
            stages=[stage.id for stage in transit.graph(spec, events)],
            cause=deals.CLOSE_CAUSE_LEAD,
            summary=text,
            source=SOURCE,
            now=moment,
        )
    except deals.DealError as exc:
        raise ToolsError(str(exc)) from exc
    return {
        "ok": True,
        "deal": str(deal["id"]),
        "closed": True,
        "cause": deals.CLOSE_CAUSE_LEAD,
        "summary": text,
        "events": _int(applied.get("events")),
    }


# ----------------------------------------------------------------- duty_answer


def answer(
    deal_id: Any,
    stage_id: Any,
    question_id: Any,
    text: Any,
    idempotency_key: Any,
    *,
    home: Optional[Path] = None,
    spawn: Optional[Callable[..., int]] = None,
    clock: Optional[Callable[[], float]] = None,
    ack_sec: float = START_ACK_SEC,
) -> dict[str, Any]:
    """Ответ лида воркеру: факт `question_answered(by: lead)` и продолжение сессии.

    Работа не начинается заново (`docs/ENGINE-2.0.md`, раздел 6, исход 1): попытку
    продолжает **резюм сессии этой попытки** — `runner.run_stage(..., answer=text)`
    берёт `session_id` из `attempt_ended.details`, кладёт ответ во вход
    (`answer_this_attempt`) и собирает команду с `--resume`. Резюм-путь здесь не
    второй: `duty_start` поднимает попытку тем же `runner.run_stage`, разница
    только в ответе на входе.

    Отказы — до записи и до спавна: этапа нет в плане, вопроса этого этапа нет в
    журнале, вопрос уже отвечен, дело запарковано (попытки исчерпаны — продолжение
    решает человек), по этапу идёт живая попытка, живых попыток не меньше
    `MAX_SLOTS`, бюджет дела исчерпан.

    Повтор с тем же `idempotency_key` второго факта и второй попытки не пишет:
    ключ едет в `question_answered` (`payload.idem`) и в `attempt_started`, и
    повтор отвечает тем, что уже случилось.

    Строка пула закрывается этим же вызовом (`ask.resolve`): ответ лида — ответ на
    вопрос, и человеку больше нечего нажимать. Порядок — по правилу раздела 9:
    сначала факт журнала, потом действие в пуле.
    """
    key = _key(idempotency_key)
    name = str(stage_id or "").strip()
    if not name:
        raise ToolsError("stage_id не назван: duty_answer(deal, stage_id, question_id, text, idempotency_key)")
    number = str(question_id if question_id is not None else "").strip()
    word = " ".join(str(text or "").split())
    if not word:
        raise ToolsError("text не назван: ответ без текста работу не продолжает")
    home = hermes_home(home)
    tick = clock or time.time
    conn = _store(home)
    deal = _deal(conn, deal_id)
    events = journal.read_events(conn, str(deal["id"]))
    if journal.closed_fact(events) is not None:
        raise ToolsError(f"дело {deal['id']} закрыто: отвечать некому")
    spec = _spec(events)
    graph = transit.graph_spec(spec, events)
    stage = next((item for item in graph.stages if str(getattr(item, "id", "")) == name), None)
    if stage is None:
        named = ", ".join(str(getattr(item, "id", "")) for item in graph.stages) or "—"
        raise ToolsError(f"этапа {name!r} нет в принятом плане: этапы дела — {named}")
    repeated = _by_idem(events, "question_answered", key)
    asked: Optional[dict[str, Any]] = repeated
    if repeated is not None:
        lifted = _by_idem(events, "attempt_started", key)
        if lifted is not None:
            return _answer_repeat(repeated, lifted)
        # Факт записан, а попытка не поднялась (лид упал между записью и
        # спавном): повтор доводит начатое и второго факта не пишет.
    else:
        asked, answered = _question_pair(events, name, number)
        if asked is None:
            raise ToolsError(
                f"вопроса {number or '—'} этапа {name!r} нет в журнале дела: отвечать нечему"
            )
        if answered:
            raise ToolsError(
                f"вопрос {number or '—'} этапа {name!r} уже отвечен: второй ответ работу не продолжает"
            )
        if journal.parked_of(events) is not None:
            raise ToolsError(
                "дело запарковано: попытки этапа исчерпаны, и ответ на вопрос работу не поднимает — "
                "продолжение решает человек"
            )
    address = journal.payload_of(asked).get("routing")
    asked_attempt = _int(address.get("attempt")) if isinstance(address, dict) else 0
    moment = int(tick())
    live = _live_attempts(events, moment)
    busy = next((row for row in live if str(row.get("stage") or "") == name), None)
    if busy is not None:
        raise ToolsError(
            f"этап {name!r} уже идёт: попытка {busy['run_id']} (pid {busy.get('pid')}) — "
            "ответу нужен свободный этап, дождись её факта или отмени её"
        )
    if len(live) >= MAX_SLOTS:
        named = ", ".join(str(row.get("run_id") or "") for row in live)
        raise ToolsError(
            f"живых попыток {len(live)} из {MAX_SLOTS} ({named}): ответ поднимает попытку, "
            "а слот освободится, когда попытка отчитается"
        )
    cap = budget.refusal(spec, events)
    if cap is not None:
        raise ToolsError(cap.reason())
    attempt = _next_attempt(events, name)
    written = 0
    if repeated is None:
        payload: dict[str, Any] = {
            "by": SOURCE,
            "answer": word,
            "answer_len": len(word),
            "answered_via": SOURCE,
            "routing": {"stage": name, "attempt": asked_attempt},
            "idem": key,
        }
        if number:
            payload["question_id"] = question_id
        fact = _lead_event(
            deal,
            "question_answered",
            payload,
            {"stage": name, "attempt": asked_attempt, "by": SOURCE, "idem": key},
            moment,
        )
        written = journal.append(conn, [fact])
    # Ответ человека больше не нужен: строка пула закрывается штатным путём пула
    # (`ask.resolve`, те же поля, что у `msgq resolve`). Иначе вопрос висит в
    # панели/iMessage, человек жмёт кнопку по уже отвеченному вопросу, и его
    # нажатие даёт второй `question_answered` (живое дело `d_8ec6a401`, #555).
    # Сбой пула ответ не отменяет: факт записан, воркер продолжается — но об этом
    # сказано ответом (`pool_resolved`), а не тишиной.
    pool_resolved = False
    if number:
        try:
            pool_resolved = ask_module.resolve(home, question_id, word, via=SOURCE)
        except (ask_module.AskError, ValueError):
            pool_resolved = False
    # Блок `needs_input` ставил движок под вопрос попытки, и снимает его тот, кто
    # взял работу (`runner.close_answered_block`): иначе дело осталось бы в
    # `wait_human`, хотя попытка уже идёт.
    runner.close_answered_block(conn, deal, clock=tick)
    spawner = spawn or _spawn_attempt
    pid = spawner(home, str(deal["id"]), name, attempt, key, answer=word)
    started = _await_start(conn, str(deal["id"]), key, deadline=tick() + float(ack_sec), tick=tick)
    if started is None:
        return {
            "ok": True,
            "answered": True,
            "confirmed": False,
            "deal": str(deal["id"]),
            "stage": name,
            "question_id": question_id,
            "attempt": attempt,
            "run_id": f"{name}#{attempt}",
            "pid": pid,
            "written": written,
            "pool_resolved": pool_resolved,
            "reason": (
                f"ответ записан, попытка поднята (pid {pid}), факта attempt_started ещё нет за "
                f"{int(ack_sec)} с — её события придут в duty_wait"
            ),
        }
    lifted = journal.payload_of(started)
    return {
        "ok": True,
        "answered": True,
        "confirmed": True,
        "deal": str(deal["id"]),
        "stage": name,
        "question_id": question_id,
        "attempt": _int(lifted.get("attempt")) or attempt,
        "run_id": str(lifted.get("run_id") or f"{name}#{attempt}"),
        "pid": lifted.get("pid"),
        "workdir": lifted.get("workdir"),
        "written": written,
        "pool_resolved": pool_resolved,
    }


def _answer_repeat(answered: dict[str, Any], lifted: dict[str, Any]) -> dict[str, Any]:
    """Ответ на повтор `duty_answer`: прежний факт и прежняя попытка, без второго.

    Номер попытки берётся из `attempt_started`, а не пересчитывается по журналу:
    после первого вызова счётчик уже сдвинут, и пересчёт ответил бы про другую
    попытку.
    """
    payload = journal.payload_of(answered)
    lifted_payload = journal.payload_of(lifted)
    address = payload.get("routing")
    stage = str(address.get("stage") or "") if isinstance(address, dict) else ""
    return {
        "ok": True,
        "answered": True,
        "repeated": True,
        "confirmed": True,
        "deal": str(answered.get("deal_id") or ""),
        "stage": stage,
        "question_id": payload.get("question_id"),
        "attempt": _int(lifted_payload.get("attempt")),
        "run_id": str(lifted_payload.get("run_id") or ""),
        "pid": lifted_payload.get("pid"),
        "text": str(payload.get("answer") or ""),
    }


# ------------------------------------------------------------------ duty_cancel


def _stage_cancelled(events: Iterable[Any], stage_id: str) -> Optional[str]:
    """Причина, которой этап отменён; `None` — этап не отменяли.

    Отмена терминальна для этого id, и новая ревизия плана её не снимает. Так же
    читает журнал срез (`transit._stage_event`): отменённый этап остаётся
    `failed`, и `_runnable` движка его больше не поднимает. Снять отмену в
    решении и оставить этап отменённым на экране — значит разойтись в ответе на
    вопрос «идёт ли этап», а лид читает именно экран.

    Вернуть этап в работу можно только под новым id: единственная запись ревизии
    под занятым id — перестановка (`needs`/`parallel_with`), и её одного мало,
    чтобы «лид отказался» превратилось в «лид пока отказался».
    """
    reason: Optional[str] = None
    for event in events or ():
        if str(event.get("kind") or "") != transit.STAGE_CANCELLED:
            continue
        payload = journal.payload_of(event)
        if str(payload.get("stage") or "") == stage_id:
            reason = str(payload.get("reason") or "")
    return reason


def _attempt_of(row: dict[str, Any]) -> tuple[str, int, int]:
    """Этап, номер попытки и pid живой попытки: `(stage, attempt, pid)`."""
    stage = str(row.get("stage") or "")
    attempt = _int(row.get("n"))
    pid = _int(row.get("pid"))
    if not stage or attempt <= 0 or pid <= 0:
        raise ToolsError(
            f"попытка {row.get('run_id') or '—'} не называет этап, номер и pid: "
            "гасить нечего и не по чему — нужен человек"
        )
    return stage, attempt, pid


def _extinguish(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    row: dict[str, Any],
    *,
    reason: str,
    key: str,
    killer: Callable[[int], bool],
    moment: int,
    events: Iterable[Any],
) -> dict[str, Any]:
    """Погасить живую попытку: гард pid, сигнал, факт `attempt_ended(cancelled)`.

    Порядок — контрактный (`docs/ENGINE-2.0.md`, раздел 5): сначала гард
    принадлежности pid (`runner.attempt_owner`), потом сигнал, потом отпускание
    лизинга вместе с фактом `attempt_ended`. Сигнал до факта — не наоборот:
    живой процесс, чью попытку уже закрыли словами, дописал бы свой конец поверх
    отмены и «воскресил» её.

    `killer` — шов для тестов (`runner.stop_pid` в жизни): настоящий процесс под
    нож теста не попадает, а порядок «гард → сигнал → факт» проверяется как есть.
    `events` — журнал, уже прочитанный вызывающим: второй проход по нему дал бы
    ту же картину за лишнее чтение.
    """
    stage, attempt, pid = _attempt_of(row)
    why = runner.attempt_owner(conn, deal, stage=stage, attempt=attempt, pid=pid, events=events)
    if why:
        raise ToolsError(
            f"попытку {stage}#{attempt} не гашу: {why} — сигнал по чужому pid "
            "убил бы чужую работу, а попытка осталась бы живой в журнале"
        )
    stopped = bool(killer(pid))
    written = runner.cancel_attempt(
        conn,
        deal,
        stage=stage,
        attempt=attempt,
        pid=pid,
        reason=reason,
        now=moment,
        source=SOURCE,
    )
    return {
        "stage": stage,
        "attempt": attempt,
        "pid": pid,
        "stopped": stopped,
        "written": written,
        "idem": key,
    }


def _cancelled_event(
    deal: dict[str, Any], stage: str, attempt: int, reason: str, key: str, moment: int, killed: Optional[dict[str, Any]]
) -> dict[str, Any]:
    """Факт `stage_cancelled`: этап отменён лидом, с причиной и следом гашения."""
    payload: dict[str, Any] = {
        "stage": stage,
        "attempt": int(attempt),
        "reason": reason,
        "by": SOURCE,
        "idem": key,
    }
    if killed is not None:
        payload["pid"] = killed["pid"]
        payload["stopped"] = killed["stopped"]
    return _lead_event(deal, transit.STAGE_CANCELLED, payload, {"stage": stage, "idem": key}, moment)


def cancel(
    deal_id: Any,
    stage_id: Any,
    reason: Any,
    idempotency_key: Any,
    *,
    home: Optional[Path] = None,
    clock: Optional[Callable[[], float]] = None,
    kill: Optional[Callable[[int], bool]] = None,
) -> dict[str, Any]:
    """Отменить этап: `pending` — только факт, идущую попытку — погасить.

    `docs/ENGINE-2.0.md`, раздел 5: лид понял, что этап не нужен, и не должен
    ждать идущую попытку, чтобы освободить слот. Поэтому у отмены два исхода, и
    оба — факты журнала:

    - этап без живой попытки (`pending`) — только `stage_cancelled`: гасить
      нечего, работа по этапу не начиналась;
    - этап с живой попыткой — гард pid (`runner.attempt_owner`), сигнал процессу
      (SIGTERM, затем SIGKILL через `KILL_GRACE_SEC`, `runner.stop_pid`),
      отпускание лизинга вместе с `attempt_ended(outcome=cancelled)`, и
      `stage_cancelled` с причиной.

    Отказы — до сигнала и до записи: этапа нет в плане, дело закрыто, этап уже
    отменён, pid не доказуемо принадлежит этой попытке (тогда сигнал убил бы
    чужую работу, и попытка осталась бы живой в журнале — инструмент называет
    причину, а разбирается человек).

    Повтор с тем же `idempotency_key` второго факта не пишет и процессов не
    трогает: ключ едет в `payload.idem` обеих записей.
    """
    key = _key(idempotency_key)
    name = str(stage_id or "").strip()
    if not name:
        raise ToolsError("stage_id не назван: duty_cancel(deal, stage_id, reason, idempotency_key)")
    text = " ".join(str(reason or "").split())
    if not text:
        raise ToolsError("reason не назван: у отмены должна быть причина — её читают лид и человек")
    home = hermes_home(home)
    tick = clock or time.time
    conn = _store(home)
    deal = _deal(conn, deal_id)
    events = journal.read_events(conn, str(deal["id"]))
    repeated = _by_idem(events, transit.STAGE_CANCELLED, key)
    if repeated is not None:
        payload = journal.payload_of(repeated)
        return {
            "ok": True,
            "deal": str(deal["id"]),
            "stage": str(payload.get("stage") or name),
            "cancelled": True,
            "repeated": True,
            "attempt": _int(payload.get("attempt")),
            "stopped": bool(payload.get("stopped")),
        }
    if journal.closed_fact(events) is not None:
        raise ToolsError(f"дело {deal['id']} закрыто: отменять в нём нечего")
    spec = _spec(events)
    graph = transit.graph_spec(spec, events)
    stage = next((item for item in graph.stages if str(getattr(item, "id", "")) == name), None)
    if stage is None:
        named = ", ".join(str(getattr(item, "id", "")) for item in graph.stages) or "—"
        raise ToolsError(f"этапа {name!r} нет в принятом плане: этапы дела — {named}")
    earlier = _stage_cancelled(events, name)
    if earlier is not None:
        raise ToolsError(
            f"этап {name!r} уже отменён ({earlier or 'причина не записана'}): "
            "вторая отмена ничего не меняет — вернуть этап можно только новым "
            "этапом в новой ревизии плана"
        )
    moment = int(tick())
    killer = kill or runner.stop_pid
    live = _live_attempts(events, moment)
    busy = next((row for row in live if str(row.get("stage") or "") == name), None)
    killed: Optional[dict[str, Any]] = None
    if busy is not None:
        killed = _extinguish(
            conn, deal, busy, reason=text, key=key, killer=killer, moment=moment, events=events
        )
    attempt = _int(busy.get("n")) if busy is not None else 0
    fact = _cancelled_event(deal, name, attempt, text, key, moment, killed)
    written = journal.append(conn, [fact])
    return {
        "ok": True,
        "deal": str(deal["id"]),
        "stage": name,
        "cancelled": True,
        "attempt": attempt,
        "stopped": bool(killed["stopped"]) if killed is not None else False,
        "cancelled_facts": _int(written) + (_int(killed["written"]) if killed is not None else 0),
        "text": text,
    }


def cancel_deal(
    deal_id: Any,
    reason: Any,
    idempotency_key: Any,
    *,
    home: Optional[Path] = None,
    clock: Optional[Callable[[], float]] = None,
    kill: Optional[Callable[[int], bool]] = None,
) -> dict[str, Any]:
    """Отменить дело целиком: погасить все живые попытки, статус `cancelled`.

    `docs/ENGINE-2.0.md`, раздел 5: то же гашение, что у `duty_cancel`, но сразу
    для всех живых попыток, дальше `completed(cause=lead_cancelled)` и строка
    дела `cancelled` (`deals.cancel_deal`). Это не `duty_close`: отменённое дело
    не считается завершённым, и `duty now`/`stats` держат его отдельно.

    Сначала проверяются **все** живые попытки (гард pid), и только потом идёт
    сигнал: иначе одна недоказуемая попытка оставила бы дело наполовину
    погашенным, а лид — с вопросом «что именно уже отменено». Отказ называет
    попытку, на которой гард не сошёлся, и не пишет ничего.

    Повтор с тем же ключом второго `completed` не пишет и процессов не трогает.
    """
    key = _key(idempotency_key)
    text = " ".join(str(reason or "").split())
    if not text:
        raise ToolsError("reason не назван: у отмены должна быть причина — её читают лид и человек")
    home = hermes_home(home)
    tick = clock or time.time
    conn = _store(home)
    deal = _deal(conn, deal_id)
    events = journal.read_events(conn, str(deal["id"]))
    repeated = _by_idem(events, "completed", key)
    if repeated is not None:
        payload = journal.payload_of(repeated)
        return {
            "ok": True,
            "deal": str(deal["id"]),
            "cancelled": True,
            "repeated": True,
            "status": str(payload.get("status") or deals.STATUS_CANCELLED),
            "cause": str(payload.get("cause") or ""),
        }
    if journal.closed_fact(events) is not None:
        raise ToolsError(f"дело {deal['id']} уже закрыто: отменять нечего")
    if str(deal["status"]) in deals.CLOSED_STATUSES:
        # Строка store бывает закрыта и без факта `completed` (старый store, рука в
        # БД). Спрашивается она **до** сигнала: иначе попытки были бы погашены, а
        # дело — нет, и «наполовину отменённого не бывает» стало бы неправдой
        # (`deals.cancel_deal` отказал бы уже после гашения).
        raise ToolsError(f"дело {deal['id']} уже закрыто (status={deal['status']}): отменять нечего")
    moment = int(tick())
    killer = kill or runner.stop_pid
    live = _live_attempts(events, moment)
    for row in live:
        stage, attempt, pid = _attempt_of(row)
        why = runner.attempt_owner(conn, deal, stage=stage, attempt=attempt, pid=pid, events=events)
        if why:
            raise ToolsError(
                f"дело не отменяю: попытку {stage}#{attempt} не погасить — {why}. "
                "Сигнал по чужому pid убил бы чужую работу"
            )
    killed = [
        _extinguish(conn, deal, row, reason=text, key=key, killer=killer, moment=moment, events=events)
        for row in live
    ]
    if killed:
        journal.append(
            conn,
            [
                _cancelled_event(deal, row["stage"], row["attempt"], text, key, moment, row)
                for row in killed
            ],
        )
    try:
        applied = deals.cancel_deal(
            conn,
            str(deal["id"]),
            reason=text,
            source=SOURCE,
            now=moment,
            idem=key,
        )
    except deals.DealError as exc:
        raise ToolsError(str(exc)) from exc
    return {
        "ok": True,
        "deal": str(deal["id"]),
        "cancelled": True,
        "status": deals.STATUS_CANCELLED,
        "cause": deals.CLOSE_CAUSE_LEAD_CANCELLED,
        "reason": text,
        "attempts": [
            {"stage": row["stage"], "attempt": row["attempt"], "pid": row["pid"], "stopped": row["stopped"]}
            for row in killed
        ],
        "events": _int(applied.get("events")),
    }


# -------------------------------------------------------------------- duty_ask


def ask(
    deal_id: Any,
    role: Any,
    question: Any,
    idempotency_key: Any,
    *,
    home: Optional[Path] = None,
    spawn: Optional[Callable[..., Any]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Спросить профиль за лида: под-попытка роли, ответ возвращается строкой.

    Механика — существующая `runner.ask_role` (`docs/ENGINE-2.0.md`, раздел 6,
    исход 2): роль получает контракт ответа, её процесс живёт попыткой со своим
    каталогом, ответ читается из `result.json`. Пула здесь нет — строки вопроса
    тоже: второй канал не заводится (`docs/JOURNAL.md`), а факты пары
    `question_asked` → `question_answered(by: role)` пишутся в журнал.

    Адрес — лид: роль называет он, поэтому причина адреса `declared_role` (адрес
    объявлен, а не выведен из `ask.allowed`; сам `ask.allowed` остаётся подсказкой,
    раздел 6). Этап, к которому относятся факты, — тот, чей вопрос ждёт ответа:
    лид спрашивает ради него. Открытого вопроса нет — факты адресуются «этапу»
    под-попытки (`ASK_STAGE`), которого нет в графе.

    `hops` — номер передачи по цепочке: он считается той же `runner.hops_done`,
    что и у вопросов движка, поэтому «третья передача» читается одинаково.

    Ответ вернулся — `{ok: true, answer, text}`. Роль переадресовала вопрос
    человеку (`authority: true`) или её попытка упала — `{ok: false}` с причиной:
    вопрос не сгорает, и следующий шаг лида — `duty_ask_human`.
    """
    key = _key(idempotency_key)
    name = str(role or "").strip()
    if not name:
        raise ToolsError("role не назван: duty_ask(deal, role, question, idempotency_key)")
    word = " ".join(str(question or "").split())
    if not word:
        raise ToolsError("question не назван: спрашивать нечего")
    home = hermes_home(home)
    tick = clock or time.time
    conn = _store(home)
    deal = _deal(conn, deal_id)
    events = journal.read_events(conn, str(deal["id"]))
    if journal.closed_fact(events) is not None:
        raise ToolsError(f"дело {deal['id']} закрыто: вопрос задавать некому")
    repeated = _by_idem(events, "question_answered", key)
    if repeated is not None:
        payload = journal.payload_of(repeated)
        return {**_ask_result(payload, name), "repeated": True}
    spec = _spec(events)
    stage, attempt, hops = _ask_target(conn, deal, events, name)
    address = routing.Target(routing.ROLE, value=name, reason=routing.REASON_DECLARED)
    moment = int(tick())
    asked = runner._role_question_fact(
        deal, stage, attempt, word, {"text": word}, address, hops, moment, idem=key
    )
    journal.append(conn, [asked])
    result = runner.ask_role(
        conn,
        spec,
        deal,
        stage,
        attempt,
        {"text": word},
        address,
        home=home,
        previous=(),
        spawn=spawn,
        clock=tick,
    )
    after = int(tick())
    if result["ok"] and not result["authority"]:
        text = str(result["answer"] or "")
        answered = runner._role_answer_fact(
            deal, stage.id, attempt, name, hops + 1, text, "", after, idem=key
        )
        journal.append(conn, [answered])
        return {**_ask_result(journal.payload_of(answered), name), "workdir": result["workdir"]}
    # Вопрос не сгорает: и переадресация, и провал ответа закрывают передачу роли
    # причиной, а решает дальше лид (человек — `duty_ask_human`).
    why = (
        f"роль {name} переадресовала вопрос человеку: authority"
        if result["ok"]
        else str(result["reason"] or "")
    )
    closed = runner._role_answer_fact(
        deal,
        stage.id,
        attempt,
        name,
        hops + 1,
        "",
        transit.ROLE_ANSWER_REDIRECTED if result["ok"] else transit.ROLE_ANSWER_FAILED,
        after,
        reason=why,
        idem=key,
    )
    journal.append(conn, [closed])
    return {**_ask_result(journal.payload_of(closed), name), "authority": bool(result["ok"])}


def _ask_target(
    conn: sqlite3.Connection, deal: dict[str, Any], events: Iterable[Any], role: str
) -> tuple[Any, int, int]:
    """Этап, номер под-попытки и номер передачи для вопроса лида профилю.

    Вопрос задаётся ради открытого вопроса дела (лид передаёт его профилю), и
    тогда адрес — этап и попытка, которые ждут ответа: факты ложатся туда же, где
    их писал бы движок, и срез дела показывает ожидание на своём этапе. Открытого
    вопроса нет — «этап» под-попытки `ASK_STAGE` (в графе его нет) и номер
    передачи по этой же цепочке.
    """
    stage_id, attempt = _awaiting(events)
    if stage_id:
        hops = runner.hops_done(conn, deal, stage_id)
        return spec_module.Stage(id=stage_id, role=role, workspace="none"), max(attempt, 1), hops
    hops = runner.hops_done(conn, deal, ASK_STAGE)
    return spec_module.Stage(id=ASK_STAGE, role=role, workspace="none"), hops + 1, hops


def _ask_result(payload: dict[str, Any], role: str) -> dict[str, Any]:
    """Ответ `duty_ask` по факту ответа роли: строка лиду, автор и номер передачи.

    Факт один на два исхода (`question_answered`), и различает их `answered_via`:
    пусто — роль ответила, `redirected`/`failed` — вопрос ушёл дальше и текста нет.
    """
    via = str(payload.get("answered_via") or "")
    text = str(payload.get("answer") or "")
    if not text and via:
        return {
            "ok": False,
            "answered": False,
            "role": str(payload.get("by") or role),
            "hops": _int(payload.get("hops")),
            "error": str(payload.get("reason") or f"роль {role} не ответила: {via}"),
        }
    return {
        "ok": True,
        "answered": True,
        "by": str(payload.get("by") or role),
        "role": str(payload.get("by") or role),
        "hops": _int(payload.get("hops")),
        "answer": text,
        "text": text,
    }


# --------------------------------------------------------------- duty_ask_human


def ask_human(
    deal_id: Any,
    question: Any,
    options: Any = None,
    risky: Any = False,
    idempotency_key: Any = "",
    *,
    home: Optional[Path] = None,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Спросить человека: вопрос в пул `ask.ask`, дело встаёт в `wait_human`.

    Исход 3 раздела 6: властный вопрос (публикация, скоуп, доступы, приоритет)
    модель не решает никогда — его задаёт человеку лид. Канал — существующий
    `ask.ask` (`dashboard/ask.py`): строка пула, факт `question_asked` и блок
    `needs_input`, который и держит дело в ожидании. Ответ человека приходит
    лиду следующим фактом: пул дочитывает `duty_wait` (`ask.sync_answers`), и
    факт `question_answered` встаёт в журнал штатным путём пула.

    `risky: true` делает вопрос критичным (`critical` в пуле): властный вопрос по
    сроку не истекает — молчание не превращает его в ответ. Срок некритичного
    вопроса считает пул по общим правилам.

    Ключ вызова едет в `payload.routing.idem` (адрес уезжает в факт целиком, так
    пишет `ask()`), поэтому повтор с тем же ключом отвечает прежним вопросом и
    второго не задаёт.
    """
    key = _key(idempotency_key)
    word = " ".join(str(question or "").split())
    if not word:
        raise ToolsError("question не назван: спрашивать нечего")
    names = (
        [str(item).strip() for item in options if str(item).strip()]
        if isinstance(options, (list, tuple))
        else []
    )
    dangerous = bool(risky)
    home = hermes_home(home)
    moment = int((clock or time.time)())
    conn = _store(home)
    deal = _deal(conn, deal_id)
    events = journal.read_events(conn, str(deal["id"]))
    if journal.closed_fact(events) is not None:
        raise ToolsError(f"дело {deal['id']} закрыто: вопрос задавать некому")
    earlier = _by_routing_idem(events, "question_asked", key)
    if earlier is not None:
        payload = journal.payload_of(earlier)
        address = payload.get("routing")
        return {
            "ok": True,
            "repeated": True,
            "question_id": _int(payload.get("question_id")),
            "target": "human",
            "reason": str(address.get("reason") or "") if isinstance(address, dict) else "",
            "blocked": True,
            "text": word,
        }
    stage_id, attempt = _awaiting(events)
    address = routing.Target(
        routing.HUMAN, reason=routing.REASON_RISKY if dangerous else routing.REASON_AUTHORITY
    )
    marked: dict[str, Any] = {**address.as_payload(), "idem": key}
    if stage_id:
        marked.update({"stage": stage_id, "attempt": attempt})
    try:
        asked = ask_module.ask(
            conn,
            {**deal, "home": home},
            word,
            None,
            names or None,
            critical=True if dangerous else None,
            routing=marked,
        )
    except ask_module.AskError as exc:
        raise ToolsError(str(exc)) from exc
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind=NEEDS_INPUT_KIND,
        reason=f"вопрос лида #{asked.question_id}: {word}",
        author=SOURCE,
        source=SOURCE,
        details={"question_id": int(asked.question_id), "risky": dangerous},
        now=moment,
    )
    return {
        "ok": True,
        "question_id": int(asked.question_id),
        "target": "human",
        "reason": address.reason,
        "blocked": True,
        "options": names,
        "risky": dangerous,
        "text": word,
    }


# ------------------------------------------------------------------ инструменты


PLAN_SCHEMA = {
    "name": "duty_plan",
    "description": (
        "Предложить план дела: набор этапов проверяется тем же разбором, что и спека "
        "(хартия дела, роли, виды, evidence, потолок max_stages). Успех — факты "
        "plan_proposed + plan_accepted: этапы становятся графом дела. Отказ — "
        "plan_proposed + plan_rejected, инструмент возвращает список проблем, и ни один "
        "этап такого плана не запускается. Повтор с тем же idempotency_key возвращает "
        "прежний ответ и второго факта не пишет."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "stages": {
                "type": "array",
                "items": {"type": "object"},
                "description": (
                    "этапы плана: {id, role, kind, workspace, needs, evidence, retry, budget}; "
                    "этап, который что-то производит, обязан объявить evidence {cmd, must}"
                ),
            },
            "reason": {"type": "string", "description": "зачем такой план — слова лида, попадают в факт"},
            "idempotency_key": {
                "type": "string",
                "description": "ключ вызова: повтор с ним не пишет второй факт и отвечает прежним результатом",
            },
        },
        "required": ["deal", "stages", "idempotency_key"],
    },
}

START_SCHEMA = {
    "name": "duty_start",
    "description": (
        "Поднять попытку этапа принятого плана отдельным процессом (своя группа процессов, "
        "свой лизинг) и сразу вернуть её run_id. Отказывает, не поднимая ничего: этапа нет в "
        "плане, по этапу уже идёт живая попытка, живых попыток в деле не меньше 2, бюджет дела "
        "исчерпан. Повтор с тем же idempotency_key второго процесса не поднимает."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "stage_id": {"type": "string", "description": "id этапа из принятого плана"},
            "idempotency_key": {
                "type": "string",
                "description": "ключ вызова: повтор с ним не поднимает вторую попытку",
            },
        },
        "required": ["deal", "stage_id", "idempotency_key"],
    },
}

WAIT_SCHEMA = {
    "name": "duty_wait",
    "description": (
        "Дождаться новых фактов дела (блокирующий вызов, до 300 с) и вернуть пачку с курсором: "
        "{events, cursor, timed_out}. Передай cursor из прошлого ответа, чтобы не перечитывать "
        "виденное. Телеметрия попыток (attempt_progress) не возвращается. timed_out=true — "
        "ничего не случилось: решай, ждать ещё или спросить человека. Вызов дешевле десятков "
        "опросов: держи его, а не заканчивай турн."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "cursor": {
                "type": "integer",
                "description": "id последнего увиденного факта (0 — с начала журнала дела)",
                "default": 0,
            },
            "timeout_sec": {
                "type": "number",
                "description": "сколько ждать фактов; потолок 300 с",
                "default": 120,
                "maximum": 300,
            },
        },
        "required": ["deal"],
    },
}

CLOSE_SCHEMA = {
    "name": "duty_close",
    "description": (
        "Закрыть дело итогом лида: факт completed с cause=lead_closed и твоим summary. "
        "Требует приговора approve; живые попытки — отказ (сначала погаси их duty_cancel). "
        "Повтор с тем же итогом второго факта не пишет."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "summary": {"type": "string", "description": "итог дела словами лида"},
            "verdict": {"type": "string", "description": "приговор: approve", "default": CLOSE_VERDICT},
            "idempotency_key": {"type": "string", "description": "ключ вызова: повтор не пишет второй факт"},
        },
        "required": ["deal", "summary", "verdict", "idempotency_key"],
    },
}


ANSWER_SCHEMA = {
    "name": "duty_answer",
    "description": (
        "Ответить воркеру самому: записать вопрос как отвеченный тобой (question_answered, by=lead) "
        "и поднять продолжение той же попытки — движок резюмирует её сессию с твоим ответом, "
        "работа не начинается заново. Отказывает, не поднимая ничего: этапа нет в плане, вопроса "
        "этого этапа нет в журнале, вопрос уже отвечен, дело запарковано (попытки исчерпаны — "
        "продолжение решает человек), по этапу идёт живая попытка или слотов нет. Повтор с тем же "
        "idempotency_key второго факта и второй попытки не пишет."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "stage_id": {"type": "string", "description": "id этапа, чей вопрос ты отвечаешь"},
            "question_id": {
                "type": "string",
                "description": "номер вопроса из журнала (payload.question_id факта question_asked)",
            },
            "text": {"type": "string", "description": "ответ воркеру — с ним продолжится его сессия"},
            "idempotency_key": {
                "type": "string",
                "description": "ключ вызова: повтор не пишет второй факт и не поднимает вторую попытку",
            },
        },
        "required": ["deal", "stage_id", "question_id", "text", "idempotency_key"],
    },
}

ASK_SCHEMA = {
    "name": "duty_ask",
    "description": (
        "Спросить профиль за себя: под-попытка роли (как вопрос этапа движку), ответ возвращается "
        "тебе строкой — его передаёт воркеру duty_answer. Факты журнала: question_asked и "
        "question_answered (by=<роль>) с номером передачи (hops). Пула не касается: вопрос профилю "
        "остаётся в журнале дела. Ответа нет — ok=false с причиной (роль переадресовала вопрос "
        "человеку или её попытка упала): тогда следующий шаг — duty_ask_human. Повтор с тем же "
        "idempotency_key отвечает прежним результатом."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "role": {"type": "string", "description": "профиль, у которого спрашиваешь (researcher, reviewer…)"},
            "question": {"type": "string", "description": "вопрос профилю, своими словами"},
            "idempotency_key": {"type": "string", "description": "ключ вызова: повтор не задаёт вопрос второй раз"},
        },
        "required": ["deal", "role", "question", "idempotency_key"],
    },
}

ASK_HUMAN_SCHEMA = {
    "name": "duty_ask_human",
    "description": (
        "Спросить человека: вопрос уходит в пул решений (ask.ask), дело встаёт в wait_human, ответ "
        "приходит следующим фактом в duty_wait. Властные вопросы — публикация, скоуп, доступы, "
        "приоритет — решает только человек: на них твой ответ не годится. risky=true делает вопрос "
        "критичным: по сроку он не истекает. Повтор с тем же idempotency_key второго вопроса не задаёт."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "question": {"type": "string", "description": "вопрос человеку — короткая строка"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "варианты ответа словами (панель покажет их кнопками)",
            },
            "risky": {
                "type": "boolean",
                "description": "властный вопрос: критичный, сроком не закрывается",
                "default": False,
            },
            "idempotency_key": {"type": "string", "description": "ключ вызова: повтор не задаёт второй вопрос"},
        },
        "required": ["deal", "question", "idempotency_key"],
    },
}


CANCEL_SCHEMA = {
    "name": "duty_cancel",
    "description": (
        "Отменить этап — отказаться от работы, не дожидаясь её. Этап без живой попытки "
        "(pending): один факт stage_cancelled. Этап с идущей попыткой: сигнал процессу "
        "(SIGTERM, затем SIGKILL через KILL_GRACE_SEC), отпускание лизинга, факты "
        "attempt_ended(outcome=cancelled) и stage_cancelled с причиной — слот "
        "освобождается сразу. Перед сигналом проверяется, что pid принадлежит именно "
        "этой попытке (лизинг + attempt_started): чужой переиспользованный pid не "
        "гасится, и тогда инструмент отказывает, не записав ничего. Отменённый этап "
        "duty_start больше не поднимает — вернуть работу можно только новым этапом "
        "в новой ревизии плана."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "stage_id": {"type": "string", "description": "id отменяемого этапа из принятого плана"},
            "reason": {"type": "string", "description": "почему этап больше не нужен — причина уходит в факт"},
            "idempotency_key": {
                "type": "string",
                "description": "ключ вызова: повтор не пишет второй факт и процессов не трогает",
            },
        },
        "required": ["deal", "stage_id", "reason", "idempotency_key"],
    },
}

CANCEL_DEAL_SCHEMA = {
    "name": "duty_cancel_deal",
    "description": (
        "Отменить дело целиком: погасить все живые попытки (тот же гард pid и то же "
        "гашение, что у duty_cancel) и закрыть дело фактом completed(cause=lead_cancelled) "
        "со статусом cancelled. Это не duty_close: отменённое дело не считается "
        "завершённым и в duty now/stats не смешивается с завершённым. Если хоть одну "
        "живую попытку нельзя погасить (pid не доказуемо её), инструмент отказывает и "
        "ничего не пишет — наполовину отменённого дела не бывает."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "description": "id своего дела (d_<hex>)"},
            "reason": {"type": "string", "description": "почему дело отменяется — причина уходит в факт"},
            "idempotency_key": {"type": "string", "description": "ключ вызова: повтор второго факта не пишет"},
        },
        "required": ["deal", "reason", "idempotency_key"],
    },
}


def _json(value: dict[str, Any]) -> str:
    """Ответ инструмента строкой JSON: модель читает причину и решает сама."""
    return json.dumps(value, ensure_ascii=False)


def _run(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Отказ — ответ (`ok: false` и причина), а не исключение.

    Трейсбек стоил бы лиду турна без пользы: решение принимает он, а инструмент
    обязан назвать, что именно не так.
    """
    try:
        return call()
    except (
        ToolsError,
        deals.DealError,
        spec_module.SpecError,
        runner.RunnerError,
        ask_module.AskError,
        sqlite3.Error,
    ) as exc:
        return {"ok": False, "error": str(exc) or type(exc).__name__}


def handle_plan(params: dict, **kwargs: Any) -> str:
    """`duty_plan`: аргументы модели → ответ строкой.

    `home` — шов для тестов (как `spawn`/`clock` у движка): ядро его не передаёт,
    и инструмент берёт корень Hermes из окружения.
    """
    args = params or {}
    return _json(
        _run(
            lambda: plan(
                args.get("deal", ""),
                args.get("stages"),
                args.get("reason", ""),
                args.get("idempotency_key", ""),
                home=kwargs.get("home"),
            )
        )
    )


def handle_start(params: dict, **kwargs: Any) -> str:
    """`duty_start`: аргументы модели → ответ строкой; `home` — шов для тестов."""
    args = params or {}
    return _json(
        _run(
            lambda: start(
                args.get("deal", ""),
                args.get("stage_id", ""),
                args.get("idempotency_key", ""),
                home=kwargs.get("home"),
            )
        )
    )


def handle_wait(params: dict, **kwargs: Any) -> str:
    """`duty_wait`: аргументы модели → ответ строкой; `home` — шов для тестов."""
    args = params or {}
    return _json(
        _run(
            lambda: wait(
                args.get("deal", ""),
                args.get("cursor", 0),
                args.get("timeout_sec", 120),
                home=kwargs.get("home"),
            )
        )
    )


def handle_close(params: dict, **kwargs: Any) -> str:
    """`duty_close`: аргументы модели → ответ строкой; `home` — шов для тестов."""
    args = params or {}
    return _json(
        _run(
            lambda: close(
                args.get("deal", ""),
                args.get("summary", ""),
                args.get("verdict", CLOSE_VERDICT),
                args.get("idempotency_key", ""),
                home=kwargs.get("home"),
            )
        )
    )


def handle_answer(params: dict, **kwargs: Any) -> str:
    """`duty_answer`: аргументы модели → ответ строкой; `home` — шов для тестов."""
    args = params or {}
    return _json(
        _run(
            lambda: answer(
                args.get("deal", ""),
                args.get("stage_id", ""),
                args.get("question_id", ""),
                args.get("text", ""),
                args.get("idempotency_key", ""),
                home=kwargs.get("home"),
            )
        )
    )


def handle_ask(params: dict, **kwargs: Any) -> str:
    """`duty_ask`: аргументы модели → ответ строкой; `home` — шов для тестов."""
    args = params or {}
    return _json(
        _run(
            lambda: ask(
                args.get("deal", ""),
                args.get("role", ""),
                args.get("question", ""),
                args.get("idempotency_key", ""),
                home=kwargs.get("home"),
            )
        )
    )


def handle_ask_human(params: dict, **kwargs: Any) -> str:
    """`duty_ask_human`: аргументы модели → ответ строкой; `home` — шов для тестов."""
    args = params or {}
    return _json(
        _run(
            lambda: ask_human(
                args.get("deal", ""),
                args.get("question", ""),
                args.get("options"),
                args.get("risky", False),
                args.get("idempotency_key", ""),
                home=kwargs.get("home"),
            )
        )
    )


def handle_cancel(params: dict, **kwargs: Any) -> str:
    """`duty_cancel`: аргументы модели → ответ строкой; `home` — шов для тестов."""
    args = params or {}
    return _json(
        _run(
            lambda: cancel(
                args.get("deal", ""),
                args.get("stage_id", ""),
                args.get("reason", ""),
                args.get("idempotency_key", ""),
                home=kwargs.get("home"),
            )
        )
    )


def handle_cancel_deal(params: dict, **kwargs: Any) -> str:
    """`duty_cancel_deal`: аргументы модели → ответ строкой; `home` — шов для тестов."""
    args = params or {}
    return _json(
        _run(
            lambda: cancel_deal(
                args.get("deal", ""),
                args.get("reason", ""),
                args.get("idempotency_key", ""),
                home=kwargs.get("home"),
            )
        )
    )


if __name__ == "__main__":  # процесс попытки, который спавнит duty_start
    sys.exit(_attempt_main(sys.argv[1:]))
