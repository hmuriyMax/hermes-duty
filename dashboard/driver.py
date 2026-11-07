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
   срок **или** умер её pid (`os.kill(pid, 0)`; только своё дело, не раньше
   `GRACE_MIN_SEC`), закрывается движком как `infra` (`reaped: true`). Так `bin/duty run`,
   убитый снаружи во время попытки, лечится следующим запуском без ручных
   действий: факт появляется в журнале, и решение перестаёт быть «попытка идёт».
7. **Ответы пула дочитываются сами.** Сразу после `reaper.sweep` и до решения
   драйвер зовёт `ask.sync_answers`: закрытый вопрос пула (`answered`/`expired`)
   становится фактом журнала `question_answered`. Без этого движок, запущенный
   из терминала или как процесс, ответов пула не видел вовсе — дело навсегда
   оставалось `wait_human`, хотя человек уже ответил («вопрос, на который
   ответили, но никто не забрал»). Порядок обязателен: ответ — факт журнала, а
   решение считается по журналу, поэтому синк идёт **до** `transit.next_stage`.
   Ошибки разбора (`errors[]`: пул недоступен, `session_key` не разобран) не
   глотаются: они попадают в отчёт (`answers_errors`) и в вывод шага, чтобы
   «вопрос закрыт, но не разобран» не читалось как «ответа нет».
8. **«Все этапы закрыты» — это факт журнала, а не строка вывода.** Когда `transit`
   отвечает `done`, драйвер пишет `completed` (`cause='all_stages_done'`, список
   пройденных этапов) и строку дела **одной транзакцией** — тем же путём, что
   `bin/duty close` (`deals.close_stages_done`). Без этого прогон объявлял
   «итог: дело закрыто», а в журнале не было `completed`, и дело оставалось
   `status='running'` (живой прогон `d_6e764042`). Печать итога — после записи и
   по ней. Повторный прогон на закрытом деле второго `completed` не пишет и
   отвечает «уже закрыто» с причиной и временем из журнала.
9. **Парковка гейта — тоже факт, а не решение.** Когда `transit` отвечает `park`
   по истечении вопроса гейта, драйвер пишет `blocked` с `block_kind='parked'` и
   причиной решения («гейт: человек не ответил за N») — `_gate_park`. Иначе экран
   показывал бы `running` без живого факта: решение «припарковано» без записи
   неотличимо от «движок не дошёл». Парковку исчерпанных попыток по-прежнему
   пишет `runner.park_exhausted` — второго писателя того же факта нет.
10. **Исчерпанный потолок дела — остановка, а не ожидание.** Попытку сверяет с
   `budget` дела сам `runner` (`refuse_budget`) до лизинга и спавна: драйвер об
   этом узнаёт из строки шага (`refused: true`) и выходит кодом 1, называя причину
   из факта отказа («бюджет дела исчерпан: N/M токенов»). Без этой ветки отказ
   читался бы как «этап занят соседом» — драйвер ушёл бы в `wait` и дело стояло
   бы вечно, хотя места в бюджете уже нет. Никакого счётчика расхода драйвер не
   держит: расход — сумма фактов журнала (`dashboard/budget.py`), и он переживает
   рестарт.
11. **Движок ушёл — дело не остаётся `running`.** Перед каждым решением (после
   `reaper.sweep` и `ask.sync_answers`) и в `finally` для своего дела драйвер
   зовёт `release.sweep`: если журнал говорит, что попытки не идут, а строка всё
   ещё «в работе», недостающий факт материализуется тем писателем, который у
   ответа `transit` есть (`released`, `completed`, парковка, блок). Без этого
   дело, чья попытка закончилась при ушедшем движке, висело `running`, пока его
   не заметит человек (живое дело `d_eb4ad2bb`: 7.5 часа). Ошибки прохода
   (дело решить нечем: спеки нет, правила разошлись) не глотаются — они в
   `release_errors` отчёта.

## Коды возврата — контракт `bin/duty run`

`0` — дело закрыто (`done`), `1` — остановлено отказом (`failed`), `2` —
припарковано (`park`: вопрос человеку уже записал `runner.park_exhausted`), `3` —
ждёт (`wait`), `4` — упёрлось в предел шагов, `130` — прервано с клавиатуры.

## Чего модуль не делает

Сам не пишет факты попыток: факт попытки — дело `runner` (одна транзакция на
факт), а закрытие осиротевшей попытки — `reaper` (тоже один факт и одна
транзакция); драйвер их только зовёт. Своими записями драйвер не обзаводится:
закрытие дела идёт через `deals` (`close_stages_done`), отпускание — через
`release.sweep`, который зовёт писателей store, а не пишет сам. Второго пути
записи `completed` или `released` в плагине нет. Не задаёт вопросов (только
`ask()`), не классифицирует отказы (только `failures.py`), не проверяет пакет
(только `evidence.py`), не запускает модель сам (только `runner.spawn_process`),
не открывает чужие БД.
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
ASK_MODULE_NAME = "hermes_duty_ask"
RELEASE_MODULE_NAME = "hermes_duty_release"


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
# Ответы пула (`ask.sync_answers`) — обратный ход того же модуля, которым движок
# задаёт вопросы: второй канал вопросов не заводится, и «прочитать ответ» обязан
# тот же код, что «записал вопрос».
ask = _load(ASK_MODULE_NAME, "ask.py")
# Отпускание дела, которое осталось `running` при ушедшем движке
# (`dashboard/release.py`) грузится после уборки: открытую попытку отпускание не
# трогает, а предикат «попытка без конца» берёт у `reaper`.
release = _load(RELEASE_MODULE_NAME, "release.py")

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

    `code` — код возврата, если отказ называет его точнее: негодная спека — код 1,
    ровно как у `bin/duty spec check` (`cli.spec_exit_code`); нечитаемая спека
    остаётся кодом 2, как и там.
    """

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


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

    def spawn(self, cmd: list[str], cwd: Path, *args: Any, **kwargs: Any) -> Any:
        """Запустить попытку и запомнить процесс — тот же вызов, что у `runner`.

        Доводы передаются как есть: агенту их два, а shell-этап называет ещё
        окружение и каталог полных логов. Обёртка не решает, чем запускать, —
        иначе она стала бы вторым движком.
        """
        process = self._spawn(cmd, cwd, *args, **kwargs)
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
                # Вид этапа: гейт отчитывается `done` и при этом называет вопрос —
                # без вида строка «ждёт ответа» читалась бы как «человек ещё не
                # ответил», хотя ответ уже разобран.
                "stage_kind": run.get("stage_kind"),
                # Попытка не поднималась: потолок дела исчерпан (`runner.refuse_budget`).
                # Строка обязана нести этот флаг, иначе драйвер прочитал бы отказ
                # как «этап занят соседом» и вышел бы ожиданием вместо остановки.
                "refused": bool(run.get("refused")),
                # Гейт закрыл дело решением человека («не публиковать»): строка
                # несёт флаг, иначе драйвер прочитал бы отказ гейта как отказ дела
                # и напечатал «попытки исчерпаны» поверх записанного `completed`.
                "closed": bool(run.get("closed")),
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
    answers: Optional[dict[str, Any]] = None,
    answers_errors: Optional[list[dict[str, Any]]] = None,
    closed: Optional[dict[str, Any]] = None,
    parked: Optional[dict[str, Any]] = None,
    released: int = 0,
    release_errors: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Отчёт прогона: шаги, уборка, ответы пула, финальное решение, исход и код.

    `reaped` — попытки, закрытые движком как осиротевшие (`reaper.sweep`): это
    факт прогона, а не шаг, поэтому он рядом со шагами, а не среди них.
    `answers` — сколько фактов `question_answered` дописал `ask.sync_answers` за
    прогон (ключи есть всегда: «нечего дочитывать» — ответ, а не отсутствие поля).
    `answers_errors` — что пул закрыл, а журнал не узнал (пул недоступен,
    `session_key` не разобран): пустой список вместо тишины, иначе «вопрос
    закрыт, но не разобран» выглядело бы как «ответа нет».
    `closed` — факт закрытия дела (`_closure`): записано ли им `completed` или
    дело уже было закрыто, с причиной и временем. `None` — прогон не кончился
    закрытием. Строку «итог: дело закрыто» CLI печатает **по этому полю**, а не по
    своему решению: состояние называет факт, который записан, а не намерение
    напечатать.
    `parked` — факт парковки гейта (`_gate_park`): записан ли `blocked` с причиной
    «гейт: человек не ответил за N» или он уже был. `None` — прогон не парковал
    гейт (парковку исчерпанных попыток пишет `runner.park_exhausted`).
    `released` — сколько фактов отпускания легло за прогон (`release.sweep`), а
    `release_errors` — дела, которые проход решить не смог (спеки нет, правила
    разошлись): пустой список вместо тишины, иначе «дело не отпущено» читалось бы
    как «отпускать было нечего».
    """
    return {
        "deal": str(deal.get("id") or ""),
        "spec": str(getattr(spec, "path", "") or ""),
        "steps": list(steps),
        "reaped": list(reaped or ()),
        "answers": {"appended": int((answers or {}).get("appended") or 0)},
        "answers_errors": list(answers_errors or ()),
        "outcome": outcome,
        "note": note,
        "decision": _decision_dict(decision),
        "closed": None if closed is None else dict(closed),
        "parked": None if parked is None else dict(parked),
        "released": int(released or 0),
        "release_errors": list(release_errors or ()),
        "exit_code": EXIT_CODES[outcome],
    }


def _closure(conn: sqlite3.Connection, deal: Any, spec: Any, now: float) -> dict[str, Any]:
    """Факт закрытия дела: записать `completed` или назвать уже записанный.

    `transit` говорит «все этапы закрыты» — но это **решение**, а не факт: до
    0.0.4 прогон печатал «итог: дело закрыто» и на этом кончал, оставляя дело
    `status='running'` без `completed` (живой прогон `d_6e764042`). Здесь
    недостающая половина: тот же путь, что у `bin/duty close` (W16) —
    `deals.close_stages_done` пишет `completed` с
    `cause='all_stages_done'` и строку дела одной транзакцией.

    Идемпотентность не в памяти прогона, а в журнале: есть `completed` — второй не
    пишется, и `updated_at` не двигается. Повторный `run` на закрытом деле поэтому
    отвечает «уже закрыто» с причиной и временем, а не вторым фактом.

    Ответ — `{written, already, cause, at, stages, status, refused}`: `written` —
    факт лёг этим прогоном, `already` — он был раньше (время тогда из журнала, а
    не из часов), `refused` — закрывать было нельзя (дело держит человек:
    `blocked`/`review`/`archived`), и причина названа словами. Отказ здесь не
    глотается: прогон обязан сказать «все этапы закрыты, но дело не закрыто», а
    не выдать одно за другое.
    """
    events = journal.read_events(conn, str(deal["id"]))
    fact = journal.closed_fact(events)
    if fact is not None:
        payload = fact["payload"]
        return {
            "written": False,
            "already": True,
            "cause": str(payload.get("cause") or ""),
            "at": int(fact["at"]),
            "stages": [str(stage) for stage in payload.get("stages") or ()],
            "status": str(payload.get("status") or "done"),
            "refused": "",
        }
    stages = [str(stage.id) for stage in getattr(spec, "stages", ())]
    try:
        result = deals.close_stages_done(
            conn,
            str(deal["id"]),
            stages=stages,
            source=runner.ENGINE_SOURCE,
            now=int(now),
        )
    except deals.DealError as exc:
        row = deals.deal_row(conn, str(deal["id"])) or {}
        return {
            "written": False,
            "already": False,
            "cause": deals.CLOSE_CAUSE_ALL_STAGES,
            "at": int(now),
            "stages": stages,
            "status": str(row.get("status") or ""),
            "refused": str(exc),
        }
    return {
        "written": int(result.get("events") or 0) > 0,
        "already": bool(result.get("idempotent")),
        "cause": deals.CLOSE_CAUSE_ALL_STAGES,
        "at": int(now),
        "stages": stages,
        "status": "done",
        "refused": "",
    }


def _gate_park(conn: sqlite3.Connection, deal: Any, spec: Any, decision: Any, now: float) -> Optional[dict[str, Any]]:
    """Гейт не дождался человека — записать парковку, названную решением `transit`.

    Это вторая половина требования «дело паркуется с причиной»: `transit` видит
    истёкший вопрос и говорит `park` с причиной, но фактом журнала это станет
    только здесь. Пишется тем же путём, что парковка исчерпанных попыток
    (`deals.set_blocked` с `block_kind='parked'`), с флагом `gate`: у гейта нет ни
    класса отказа, ни счётчика, и поверхность не должна показывать «парковка 0/0».
    `None` — решение не про гейт (парковку исчерпанных попыток уже записал
    `runner.park_exhausted`, второй факт не нужен).
    """
    stage_id = str(getattr(decision, "stage", "") or "")
    finder = getattr(spec, "stage", None)
    stage = finder(stage_id) if callable(finder) else None
    if stage is None or str(getattr(stage, "kind", "")) != spec_module.GATE_KIND:
        return None
    if journal.parked_of(journal.read_events(conn, str(deal["id"]))) is not None:
        return {"written": False, "already": True, "gate": True, "stage": stage_id}
    try:
        fact = runner.park_gate(
            conn,
            deal,
            stage,
            str(getattr(decision, "reason", "") or ""),
            question_id=getattr(decision, "question_id", None),
            clock=lambda: float(now),
        )
    except deals.DealError as exc:
        return {"written": False, "already": False, "gate": True, "stage": stage_id, "refused": str(exc)}
    return {"written": True, "already": False, **fact}


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
    on_answer_error: Optional[Callable[[dict[str, Any]], None]] = None,
    on_release: Optional[Callable[[dict[str, Any]], None]] = None,
    on_release_error: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Довести дело до терминального исхода: решение → шаг → перечитать журнал.

    Возвращает отчёт (`_report`): шаги попыток, уборка осиротевших попыток,
    дочитанные ответы пула, отпускания, финальное решение, исход и код возврата.
    `on_step` зовётся на каждую строку сразу после шага, `on_reap` — на каждую
    попытку, закрытую движком, `on_answer_error` — на каждый закрытый вопрос пула,
    который журнал не узнал, `on_release`/`on_release_error` — на каждый факт
    отпускания и на дело, которое отпустить нечем; консоль может печатать по
    строке, не дожидаясь конца прогона, а форматирования здесь нет, потому что
    оформление живёт в CLI.

    `spawn`, `clock`, `monotonic` — те же швы, что у `runner.step`: тесты
    подменяют спавн фейком, а живые часы — неподвижными.
    """
    tick = clock or time.time
    measure = monotonic or time.monotonic
    home = Path(home)
    children = _Children(spawn or runner.spawn_process)
    limit = int(max_steps)
    steps: list[dict[str, Any]] = []
    reaped: list[dict[str, Any]] = []
    answers: dict[str, Any] = {"appended": 0}
    answers_errors: list[dict[str, Any]] = []
    released: list[dict[str, Any]] = []
    release_errors: list[dict[str, Any]] = []
    made = 0
    outcome = ""
    note = ""
    closed: Optional[dict[str, Any]] = None
    parked: Optional[dict[str, Any]] = None

    def release_pass(*, only: Any = None, grace_sec: int = release.RELEASE_GRACE_SEC) -> None:
        """Отпустить дела, которые журнал уже отпустил, а строка — нет (правило 11).

        Зовётся там, где движок уже есть: перед решением (с паузой — чужой прогон
        мог только что записать свой факт) и в `finally` для своего дела (без
        паузы: этот движок уходит сам). Ошибки прохода не глотаются: они копятся
        в `release_errors` и отдаются вызывающему по одной строке.
        """
        for row in release.sweep(
            conn, int(tick()), home=home, spec=spec, only=only, grace_sec=grace_sec
        ):
            if row.get("error"):
                if row not in release_errors:
                    release_errors.append(row)
                    if on_release_error is not None:
                        on_release_error(row)
                continue
            if not row.get("written"):
                continue
            released.append(row)
            if on_release is not None:
                on_release(row)

    def decide() -> Any:
        """Уборка осиротевших попыток, дочитывание ответов пула, решение по журналу.

        Уборка идёт **до** расчёта решения (правило 4 карточки): попытка без
        конца и с истёкшим лизингом иначе навсегда осталась бы «идущей», и
        решение вернуло бы `wait(attempt)` — то самое вечное стояние, ради
        которого уборка и заведена. Кладётся она в отчёт отдельным списком: это
        факт прогона, а не шаг.

        Ответы пула читаются сразу после уборки и **до** решения (правило 7):
        ответ человека — факт журнала, а решение считается по журналу. Обратный
        порядок оставил бы дело в `wait_human` ещё на один шаг, хотя человек уже
        ответил. Ошибки разбора (`errors[]`) не глотаются: они копятся в отчёте и
        отдаются вызывающему по одной строке.

        Отпускание идёт последним и **до** решения (правило 11): строка `running`
        без живого факта — это тот же недостающий факт, что и осиротевшая
        попытка, и решение по журналу обязано читаться уже по полному журналу.
        """
        for row in reaper.sweep(conn, int(tick()), spec=spec):
            reaped.append(row)
            if on_reap is not None:
                on_reap(row)
        synced = ask.sync_answers(conn, home)
        answers["appended"] = int(answers.get("appended") or 0) + int(synced.get("appended") or 0)
        for error in synced.get("errors") or ():
            row = dict(error) if isinstance(error, dict) else {"source": "", "reason": str(error)}
            if row in answers_errors:
                continue  # один и тот же сбой пула не повторяется на каждом шаге
            answers_errors.append(row)
            if on_answer_error is not None:
                on_answer_error(row)
        release_pass()
        return _decide(conn, deal, spec, tick)

    decision = decide()
    # Дело, закрытое фактом журнала, попыток не поднимает — даже если `transit`
    # прочитал `completed` со `stage` как закрытие **этапа** (так пишет
    # `deals.close_deal`: `stage` там называет, где стояло дело) или увидел
    # старый блок. Предикат закрытия один — `journal.closed_fact`, и он же
    # правило 2 `fold`: закрытие терминально, поэтому прогон спрашивает его до
    # шага. Иначе закрытое человеком дело воскресало бы первой же попыткой.
    if journal.closed_fact(journal.read_events(conn, str(deal["id"]))) is not None:
        decision = transit.done("журнал: дело закрыто событием completed")
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
                refused = next((row for row in rows if row.get("refused")), None)
                if refused is not None:
                    # Потолок дела исчерпан: попытку движок не поднял, но факт
                    # отказа записал (`runner.refuse_budget`). Остановка — по
                    # этому факту, и причина берётся из него, а не из решения
                    # (`transit` называет класс, но не числа).
                    outcome = OUTCOME_FAILED
                    note = str(refused.get("reason") or "")
                    decision = decide()
                    break
                ended = next((row for row in rows if row.get("closed")), None)
                if ended is not None:
                    # Гейт закрыл дело решением человека («не публиковать»): это не
                    # отказ прогона, и «попытки исчерпаны» здесь было бы неправдой.
                    # Факт `completed` уже в журнале — итог берётся из него.
                    decision = decide()
                    closed = _closure(conn, deal, spec, tick())
                    outcome = OUTCOME_DONE
                    note = str(ended.get("reason") or "")
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
        if getattr(decision, "kind", "") == transit.DECISION_DONE:
            # Решение «все этапы закрыты» — ещё не факт: закрытие пишется тем же
            # путём, что `bin/duty close` (`deals.close_stages_done`), и только
            # потом CLI печатает «итог: дело закрыто» — по записанному.
            closed = _closure(conn, deal, spec, tick())
        if getattr(decision, "kind", "") == transit.PARK:
            # Парковка гейта — тоже решение, а не факт: вопрос истёк, и дело
            # паркуется здесь, с причиной «гейт: человек не ответил за N».
            parked = _gate_park(conn, deal, spec, decision, tick())
    finally:
        children.stop()
        _release_mine(conn, deal, spec)
        # Этот движок уходит сам: своё дело он отпускает без паузы — свежий
        # `attempt_ended` того же прогона иначе выглядел бы «движок только что был»
        # (правило 11). Чужие дела проход уже посмотрел перед последним решением.
        release_pass(only=[str(deal["id"])], grace_sec=0)
    return _report(
        deal,
        spec,
        steps,
        decision,
        outcome,
        note,
        reaped,
        answers,
        answers_errors,
        closed=closed,
        parked=parked,
        released=len(released),
        release_errors=release_errors,
    )


# --------------------------------------------------------------- вход прогона


def spec_path_of(conn: sqlite3.Connection, deal: Any) -> Optional[str]:
    """Путь спеки дела по журналу (`created.payload.spec`); `None` — спеки нет."""
    return stagefeed.spec_of(journal.read_events(conn, str(deal["id"])))


def _checked_spec(path: Path) -> Any:
    """Спека прогона, проверенная правилами `bin/duty spec check` до первого шага.

    `drive()` не исполняет спеку, которую `spec check` назвал бы отказом
    (`docs/SPEC.md`: каждое правило — отказ, случающийся раньше первого процесса).
    Иначе цикл в графе давал бы вечный `wait("gate")` вместо отказа,
    `verdict.changes` не на предшественника проходил бы молча, и публикация без
    ревью в `needs` — тоже.

    Отказы и коды — те же, что у `spec check`: `file:line: правило: текст` и код 1
    для негодной спеки (`cli.spec_exit_code`), код 2 для нечитаемой. Предупреждения
    (`check_base`: репозитория нет на этой машине, `git` не ответил) прогон не
    останавливают: это «правило проверить не удалось», а не отказ.
    """
    try:
        spec = spec_module.load_spec(path)
    except spec_module.SpecError as exc:
        raise DriverError(str(exc.issue())) from exc
    refusals = spec_module.errors(spec_module.validate_spec(spec) + spec_module.check_base(spec))
    if refusals:
        raise DriverError("\n".join(str(issue) for issue in refusals), code=EXIT_FAILED)
    return spec


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
    on_answer_error: Optional[Callable[[dict[str, Any]], None]] = None,
    on_release: Optional[Callable[[dict[str, Any]], None]] = None,
    on_release_error: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Вход `bin/duty run`: открыть store, найти спеку, проверить её и довести дело.

    Спека берётся из журнала дела (`created.payload.spec`), а `--spec` её
    перекрывает: у дела может не быть записанного пути (заведено до появления
    спеки в `create`) или хочется прогнать другой граф по тому же журналу.
    Store открывается **без создания файла**: прогон по несуществующему store —
    это «дела нет», а не «завёл пустую БД».

    Спека проверяется правилами `bin/duty spec check` **до** `run()` — то есть до
    первого процесса (`_checked_spec`): отказ раньше попытки, а не вечное
    `wait("gate")` посреди прогона (`docs/SPEC.md`).
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
            spec=_checked_spec(path),
            home=home,
            max_steps=max_steps,
            once=once,
            spawn=spawn,
            clock=clock,
            monotonic=monotonic,
            on_step=on_step,
            on_reap=on_reap,
            on_answer_error=on_answer_error,
            on_release=on_release,
            on_release_error=on_release_error,
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
