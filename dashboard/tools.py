"""Тулсет `duty` — четыре инструмента лида над журналом дел.

Контракт — `docs/ENGINE-2.0.md`, разделы 4 и 5: шедулером дела становится лид.
План предлагает `duty_plan`, этапы поднимает `duty_start`, факты читает
`duty_wait`, дело закрывает `duty_close`.

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
вызов на 120–300 секунд дешевле шестидесяти опросов по 5 секунд.
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

JOURNAL_MODULE_NAME = "hermes_duty_journal"
DEALS_MODULE_NAME = "hermes_duty_deals"
SPEC_MODULE_NAME = "hermes_duty_spec"
TRANSIT_MODULE_NAME = "hermes_duty_transit"
RUNNER_MODULE_NAME = "hermes_duty_runner"
STAGEFEED_MODULE_NAME = "hermes_duty_stagefeed"
BUDGET_MODULE_NAME = "hermes_duty_budget"


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
    """Корень Hermes: аргумент, `$HERMES_HOME`, иначе `~/.hermes`.

    Одна точка на все инструменты: журнал, store и лизинги живут в одном
    `<home>/duty/duty.db`, и второй способ их найти разошёлся бы с первым.
    """
    if home is not None:
        return Path(home).expanduser()
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
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
    written = journal.append(conn, [proposal, accepted])
    return {
        "ok": True,
        "deal": str(deal["id"]),
        "rev": rev,
        "accepted": True,
        "stages": [stage.id for stage in parsed],
        "graph": [stage.id for stage in transit.graph(spec, journal.read_events(conn, str(deal["id"])))],
        "events": [transit.PLAN_PROPOSED, transit.PLAN_ACCEPTED],
        "written": written,
    }


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
    11): этапа нет в принятом плане; по этапу уже идёт живая попытка; живых
    попыток в деле не меньше `MAX_SLOTS`; бюджет дела исчерпан. Ни один отказ
    ничего не пишет — журнал не растёт, слот не занимается.

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
    moment = int(tick())
    live = _live_attempts(events, moment)
    busy = next((row for row in live if str(row.get("stage") or "") == name), None)
    if busy is not None:
        raise ToolsError(
            f"этап {name!r} уже идёт: попытка {busy['run_id']} (pid {busy.get('pid')}) — "
            "слот этапа занят, дождись её факта или отмени её"
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
    attempt = _next_attempt(events, name)
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


def _spawn_attempt(home: Path, deal_id: str, stage_id: str, attempt: int, key: str) -> int:
    """Запустить попытку отдельным процессом в своей группе; вернуть его pid.

    `start_new_session=True` — своя группа процессов: kill супервизора лида не
    уносит работу (`docs/ENGINE-2.0.md`, раздел 2). Полный вывод процесса идёт
    файлом: причина падения должна быть видна, а журнал — не место для логов.
    """
    log = _attempt_log(home, deal_id, stage_id, attempt)
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), deal_id, stage_id, str(attempt), key, str(home)],
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
    «как идёт попытка» быть не должно.
    """
    if len(argv) < 5:
        print("usage: tools.py <deal_id> <stage_id> <attempt> <idempotency_key> <home>", file=sys.stderr)
        return 2
    home = Path(argv[4]).expanduser()
    try:
        conn = _store(home)
        deal = _deal(conn, argv[0])
        events = journal.read_events(conn, str(deal["id"]))
        spec = _spec(events)
        runner.run_stage(conn, spec, deal, argv[1], _int(argv[2]), home=home, idem=argv[3])
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
    while True:
        fresh = journal.read_events(conn, str(deal["id"]), since_id=position)
        if fresh:
            position = max(position, max(_int(event.get("id")) for event in fresh))
        facts = [event for event in fresh if str(event.get("kind") or "") not in NOISE_KINDS]
        if facts:
            return {"ok": True, "deal": str(deal["id"]), "events": [_brief(event) for event in facts],
                    "cursor": position, "timed_out": False}
        if tick() >= deadline:
            return {"ok": True, "deal": str(deal["id"]), "events": [], "cursor": position, "timed_out": True}
        pause(min(WAIT_POLL_SEC, max(0.0, deadline - tick())))


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
    except (ToolsError, deals.DealError, spec_module.SpecError, sqlite3.Error) as exc:
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


if __name__ == "__main__":  # процесс попытки, который спавнит duty_start
    sys.exit(_attempt_main(sys.argv[1:]))
