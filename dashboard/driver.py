"""Драйвер дела: цикл «решение → шаг → перечитать журнал».

Зачем это отдельный слой. `dashboard/transit.py` отвечает, что делать дальше, но
ничего не делает; `dashboard/runner.py` делает ровно один шаг и ничего не решает.
Между ними не хватало цикла, и до сих пор каждая попытка поднималась вручную из
Python — то есть гейт «дело проходит impl → verify → review на настоящем
репозитории» проверить было нечем.

Драйвер **тонкий**: он не знает ни одного правила перехода. Его работа — спросить
`transit.next_stage`, отдать решение `runner.step`, снова прочитать журнал и
спросить заново. Ни классификации отказа, ни проверки пакета, ни решения о
парковке здесь нет: они живут в своих модулях (`failures.py`, `evidence.py`,
`runner.py`), и вторая копия правила разошлась бы с первой.

## Правила

1. **Один шаг — один факт.** Состояние прогона — журнал, а не память процесса:
   после шага события перечитываются целиком (`journal.read_events`) и решение
   считается заново. Поэтому прогон, убитый после `impl`, при повторном запуске
   идёт в `verify`: место в графе знает журнал, а не предыдущий процесс.
2. **`wait` — это выход, а не занятая петля.** Ожидание человека, приговора,
   паузы перед повтором или чужого лизинга не крутится внутри: драйвер выходит с
   кодом 3 и называет, чего ждёт (`wait_human` → номер вопроса, `retry` →
   `retry_at`, `gate` → кто держит). Запускать заново — после ответа. `sleep;
   continue` внутри означал бы, что ожидание человека держит процесс.
3. **`--once`** — один шаг и выход: отладка и тесты не должны гонять весь граф.
4. **Предел шагов** (`--max-steps`, по умолчанию 20): превышение — код 4 и
   причина в stderr через код возврата. Предел — предохранитель от петли, а не
   правило перехода: дело при этом не меняется, следующий прогон продолжит с того
   же места.
5. **Процессы не остаются сиротами.** Драйвер дожидается каждой попытки (её
   таймаут уже внутри `runner`), на SIGINT гасит детей и выходит, а лизинг
   отпускается в любом исходе — иначе следующая попытка увидела бы чужой лизинг и
   отказалась бы работать.
6. **Сироты лечатся сами.** Перед каждым решением драйвер зовёт
   `reaper.sweep`: попытка без `attempt_ended`, у которой истёк лизинг и прошёл
   срок, закрывается движком как `infra` (`reaped: true`). Так `bin/duty run`,
   убитый снаружи во время попытки, лечится следующим запуском без ручных
   действий: факт появляется в журнале, и решение перестаёт быть «попытка идёт».

## Коды возврата — контракт `bin/duty run`

`0` — дело закрыто (`done`), `1` — остановлено отказом (`failed`), `2` —
припарковано (`park`: вопрос человеку уже записал `runner.park_exhausted`), `3` —
ждёт (`wait`), `4` — упёрлось в предел шагов, `130` — прервано с клавиатуры.

## Чего модуль не делает

Сам не пишет в журнал: факт попытки — дело `runner` (одна транзакция на факт), а
закрытие осиротевшей попытки — `reaper` (тоже один факт и одна транзакция);
драйвер их только зовёт. Не задаёт вопросов (только `ask()`), не классифицирует
отказы (только `failures.py`), не проверяет пакет (только `evidence.py`), не
запускает модель сам (только `runner.spawn_process`), не открывает чужие БД.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

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
# Уборка осиротевших попыток (`dashboard/reaper.py`) грузится после ленты: она
# берёт у неё «какая спека у дела», а не читает журнал своим разбором.
reaper = _load(REAPER_MODULE_NAME, "reaper.py")

# Коды возврата драйвера. Первые три совпадают с кодами остального CLI: дело
# закрыто / отказ / припарковано. Остальные — только у прогона: ожидание, предел
# шагов и Ctrl-C.
EXIT_DONE = 0
EXIT_FAILED = 1
EXIT_PARKED = 2
EXIT_WAIT = 3
EXIT_LIMIT = 4
EXIT_INTERRUPTED = 130

# Предел шагов по умолчанию: цепочка из трёх этапов с повторами проходит целиком,
# а петля упирается в число, а не в терпение человека.
DEFAULT_MAX_STEPS = 20

# Исходы прогона. Первые четыре — имена решений `transit`; остальные три есть
# только у драйвера: «сделан один шаг по `--once`», «упёрся в предел», «прерван».
OUTCOME_DONE = transit.DECISION_DONE
OUTCOME_FAILED = transit.DECISION_FAILED
OUTCOME_PARKED = transit.PARK
OUTCOME_WAIT = transit.WAIT
OUTCOME_STEP = "step"
OUTCOME_LIMIT = "limit"
OUTCOME_INTERRUPTED = "interrupted"

# Исход прогона → код возврата. Одна таблица: иначе слово и код разъехались бы.
EXIT_CODES = {
    OUTCOME_DONE: EXIT_DONE,
    OUTCOME_FAILED: EXIT_FAILED,
    OUTCOME_PARKED: EXIT_PARKED,
    OUTCOME_WAIT: EXIT_WAIT,
    OUTCOME_STEP: EXIT_DONE,
    OUTCOME_LIMIT: EXIT_LIMIT,
    OUTCOME_INTERRUPTED: EXIT_INTERRUPTED,
}

# Исходы решения, которые закрывают прогон сами по себе (без шага).
TERMINAL_KINDS = (OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_PARKED)


class DriverError(RuntimeError):
    """Прогон не может начаться: нет store, спеки или дела — причина словами.

    Отдельный тип, потому что «дело не доведено» и «дело довести нельзя» — разные
    ответы: первый называет исход прогона и его код, второй не начинался вовсе и
    код у него — ошибка источников (2).
    """


# ---------------------------------------------------------------- дети прогона


class _Children:
    """Процессы попыток этого прогона: чтобы SIGINT и выход не оставили сирот.

    `runner.step` поднимает процесс внутри себя и возвращает отчёт, а не ручку:
    погасить ребёнка по SIGINT было бы нечем. Поэтому драйвер подставляет
    `runner`'у свою обёртку спавна (шов уже объявлен в `runner.spawn_process` —
    единственное место, которое тесты и подменяют) и помнит, кого поднял.
    """

    def __init__(self, spawn: Callable[..., Any]) -> None:
        self._spawn = spawn
        self._live: list[Any] = []

    def spawn(self, cmd: list[str], cwd: Path) -> Any:
        """Запустить попытку и запомнить процесс — тот же вызов, что у `runner`."""
        process = self._spawn(cmd, cwd)
        if not any(process is known for known in self._live):
            self._live.append(process)
        return process

    def stop(self) -> None:
        """Погасить тех, кто ещё идёт; вышедших не трогаем (гасить нечего)."""
        for process in self._live:
            finished = getattr(process, "finished", None)
            if callable(finished) and finished():
                continue
            stopper = getattr(process, "stop", None)
            if callable(stopper):
                stopper()
        self._live.clear()


class _Interrupt:
    """SIGINT во время прогона: погасить детей и пометить, что нас прервали.

    Выходить из обработчика нельзя: попытка в этот момент идёт, и её факты
    (`attempt_ended`, отпускание лизинга) обязан записать `runner` — иначе в
    журнале осталась бы попытка без исхода. Поэтому обработчик только гасит
    процессы и ставит флаг, а цикл доводит шаг и выходит сам.
    """

    def __init__(self, children: _Children) -> None:
        self.children = children
        self.hit = False
        self._previous: Any = None

    def __enter__(self) -> "_Interrupt":
        try:
            self._previous = signal.signal(signal.SIGINT, self._handle)
        except ValueError:  # не главный поток: сигналы тут не ловятся, и это не отказ шага
            self._previous = None
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGINT, self._previous)

    def _handle(self, signum: int, frame: Any) -> None:
        self.hit = True
        self.children.stop()


# --------------------------------------------------------------- шаг и решения


def _decide(conn: sqlite3.Connection, deal: Any, spec: Any, clock: Callable[[], float]) -> Any:
    """Что делать дальше — по журналу, а не по памяти прогона (правило 1).

    События читаются целиком на каждый шаг: журнал append-only, и «сколько уже
    сделано» — это его содержимое, а не счётчик в процессе. Счётчик нужен только
    пределу шагов.
    """
    events = journal.read_events(conn, str(deal["id"]))
    return transit.next_stage(spec, events, int(clock()))


def _rows(number: int, step_result: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    """Строки отчёта по шагу: одна на попытку, которую назвало решение.

    Поля — ровно то, что сказал `runner.step`, плюс номер шага и длительность:
    отчёт обязан показывать факт, а не пересказ (`--json` — та же форма).
    """
    rows: list[dict[str, Any]] = []
    for run in step_result.get("runs") or ():
        rows.append(
            {
                "n": int(number),
                "stage": str(run.get("stage") or ""),
                "attempt": int(run.get("attempt") or 0),
                "started": bool(run.get("started")),
                "outcome": str(run.get("outcome") or "") or None,
                "kind": str(run.get("kind") or "") or None,
                "reason": str(run.get("reason") or ""),
                "question_id": run.get("question_id"),
                # База дерева и коммит попытки: по ним видно, от чего этап работал
                # и что оставил следующему (W14). В человеческую строку они не
                # идут — она про исход, — но в `--json` и отчёте прогона нужны.
                "base": run.get("base"),
                "commit": run.get("commit"),
                "duration_sec": round(float(duration), 1),
            }
        )
    return rows


def _outcome_of(decision: Any) -> str:
    """Имя исхода прогона по решению: `done`/`failed`/`park`, всё остальное — `wait`."""
    kind = str(getattr(decision, "kind", "") or "")
    return kind if kind in TERMINAL_KINDS else OUTCOME_WAIT


def _decision_dict(decision: Any) -> dict[str, Any]:
    """Решение `transit` словарём — машинная часть отчёта (`--json`)."""
    if dataclasses.is_dataclass(decision) and not isinstance(decision, type):
        return dataclasses.asdict(decision)
    return {"kind": str(decision)}


def _report(
    deal: Any,
    spec: Any,
    steps: list[dict[str, Any]],
    decision: Any,
    outcome: str,
    note: str,
    reaped: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Отчёт прогона: шаги, уборка, финальное решение, исход и код возврата.

    `reaped` — попытки, закрытые движком как осиротевшие (`reaper.sweep`): это
    факт прогона, а не шаг, поэтому он рядом со шагами, а не среди них.
    """
    return {
        "deal": str(deal.get("id") or ""),
        "board": str(deal.get("board") or ""),
        "spec": str(getattr(spec, "path", "") or ""),
        "steps": list(steps),
        "reaped": list(reaped or ()),
        "outcome": outcome,
        "note": note,
        "decision": _decision_dict(decision),
        "exit_code": EXIT_CODES[outcome],
    }


def _release_mine(conn: sqlite3.Connection, deal: Any, spec: Any) -> None:
    """Отпустить лизинг, взятый самим драйвером, — в любом исходе прогона.

    `runner` отпускает лизинг попытки на её смерти сам; здесь — страховка на
    случай, когда шаг прервался между взятием лизинга и спавном. Чужой лизинг
    (другой pid) не трогается: `runner.release` удаляет строку только свою.
    """
    pid = os.getpid()
    for stage in getattr(spec, "stages", ()):
        runner.release(conn, deal=deal, stage=stage.id, pid=pid)


def run(
    conn: sqlite3.Connection,
    deal: Any,
    *,
    spec: Any,
    home: Path,
    max_steps: int = DEFAULT_MAX_STEPS,
    once: bool = False,
    spawn: Optional[Callable[..., Any]] = None,
    clock: Optional[Callable[[], float]] = None,
    monotonic: Optional[Callable[[], float]] = None,
    on_step: Optional[Callable[[dict[str, Any]], None]] = None,
    on_reap: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Довести дело до терминального исхода: решение → шаг → перечитать журнал.

    Возвращает отчёт (`_report`): шаги попыток, уборка осиротевших попыток,
    финальное решение, исход и код возврата. `on_step` зовётся на каждую строку
    сразу после шага, `on_reap` — на каждую попытку, закрытую движком; консоль
    может печатать по строке, не дожидаясь конца прогона, а форматирования здесь
    нет, потому что оформление живёт в CLI.

    `spawn`, `clock`, `monotonic` — те же швы, что у `runner.step`: тесты
    подменяют спавн фейком, а живые часы — неподвижными.
    """
    tick = clock or time.time
    measure = monotonic or time.monotonic
    children = _Children(spawn or runner.spawn_process)
    limit = int(max_steps)
    steps: list[dict[str, Any]] = []
    reaped: list[dict[str, Any]] = []
    made = 0
    outcome = ""
    note = ""

    def decide() -> Any:
        """Решение по журналу, а перед ним — уборка осиротевших попыток.

        Уборка идёт **до** расчёта решения (правило 4 карточки): попытка без
        конца и с истёкшим лизингом иначе навсегда осталась бы «идущей», и
        решение вернуло бы `wait(attempt)` — то самое вечное стояние, ради
        которого уборка и заведена. Кладётся она в отчёт отдельным списком: это
        факт прогона, а не шаг.
        """
        for row in reaper.sweep(conn, int(tick()), spec=spec):
            reaped.append(row)
            if on_reap is not None:
                on_reap(row)
        return _decide(conn, deal, spec, tick)

    decision = decide()
    try:
        with _Interrupt(children) as watch:
            while True:
                if watch.hit:
                    # Прервали между шагами: шаг уже записан, но продолжать нельзя.
                    outcome = OUTCOME_INTERRUPTED
                    note = "с клавиатуры: дети погашены, лизинг отпущен"
                    decision = decide()
                    break
                if decision.kind != transit.RUN:
                    outcome = _outcome_of(decision)
                    note = str(decision.reason or "")
                    break
                if made >= limit:
                    outcome = OUTCOME_LIMIT
                    note = (
                        f"сделано {made} из {limit} — дело не тронуто, "
                        "следующий запуск продолжит с этого же места"
                    )
                    break
                started_at = measure()
                step_result = runner.step(
                    conn,
                    spec,
                    deal,
                    decision,
                    home=Path(home),
                    spawn=children.spawn,
                    clock=tick,
                    monotonic=measure,
                )
                made += 1
                rows = _rows(made, step_result, max(0.0, measure() - started_at))
                steps.extend(rows)
                if on_step is not None:
                    for row in rows:
                        on_step(row)
                if watch.hit:
                    continue  # верх цикла оформит прерывание
                if once:
                    outcome = OUTCOME_STEP
                    note = f"сделан один шаг ({made})"
                    decision = decide()
                    break
                if not any(row["started"] for row in rows):
                    # Попытку поднял кто-то другой (лизинг занят живым процессом):
                    # крутиться дальше значило бы гонять предел шагов вместо того,
                    # чтобы уступить этап соседу и выйти.
                    outcome = OUTCOME_WAIT
                    busy = [row["reason"] for row in rows if row["reason"]]
                    note = "; ".join(busy) or "попытка не поднялась"
                    decision = decide()
                    break
                decision = decide()
    finally:
        children.stop()
        _release_mine(conn, deal, spec)
    return _report(deal, spec, steps, decision, outcome, note, reaped)


# --------------------------------------------------------------- вход прогона


def spec_path_of(conn: sqlite3.Connection, deal: Any) -> Optional[str]:
    """Путь спеки дела по журналу (`created.payload.spec`); `None` — спеки нет."""
    return stagefeed.spec_of(journal.read_events(conn, str(deal["id"])))


def drive(
    home: Path,
    deal_id: str,
    *,
    spec_path: Optional[str] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
    once: bool = False,
    spawn: Optional[Callable[..., Any]] = None,
    clock: Optional[Callable[[], float]] = None,
    monotonic: Optional[Callable[[], float]] = None,
    on_step: Optional[Callable[[dict[str, Any]], None]] = None,
    on_reap: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Вход `bin/duty run`: открыть store, найти спеку и довести дело.

    Спека берётся из журнала дела (`created.payload.spec`), а `--spec` её
    перекрывает: у дела может не быть записанного пути (заведено до появления
    спеки в `create`) или хочется прогнать другой граф по тому же журналу.
    Store открывается **без создания файла**: прогон по несуществующему store —
    это «дела нет», а не «завёл пустую БД».
    """
    home = Path(home)
    conn = deals.open_store(home, create=False)
    if conn is None:
        raise DriverError(f"store не найден: {deals.store_path(home)} — дел ещё нет")
    try:
        deal = deals.deal_row(conn, deals.own_id(deal_id))
        if deal is None:
            raise deals.DealNotFound(f"дела {deal_id!r} нет в store {deals.store_path(home)}")
        path = _spec_file(conn, deal, spec_path)
        return run(
            conn,
            deal,
            spec=spec_module.load_spec(path),
            home=home,
            max_steps=max_steps,
            once=once,
            spawn=spawn,
            clock=clock,
            monotonic=monotonic,
            on_step=on_step,
            on_reap=on_reap,
        )
    finally:
        conn.close()


def _spec_file(conn: sqlite3.Connection, deal: Any, spec_path: Optional[str]) -> Path:
    """Файл спеки прогона: названный `--spec` или записанный в деле, но существующий.

    Нечитаемый путь называется словами и до первого шага: иначе прогон упал бы
    на первом же решении, уже взяв лизинг.
    """
    if spec_path:
        path = Path(spec_path).expanduser().resolve()
    else:
        found = spec_path_of(conn, deal)
        if not found:
            raise DriverError(
                f"у дела {deal.get('id')} нет спеки: заведи его с `duty create --spec <duty.yaml>` "
                "или передай --spec"
            )
        path = Path(found).expanduser()
    if not path.is_file():
        raise DriverError(f"спека не читается: {path}")
    return path
