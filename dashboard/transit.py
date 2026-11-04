"""Слой перехода дела: `next(spec, events)` — чистая функция, никакой модели.

Почему это отдельный слой. Спека (`dashboard/spec.py`, контракт `docs/SPEC.md`)
описывает, из каких этапов состоит работа и что считается доказательством.
Спека-источник называет и вторую половину: «Решение „что дальше“ — чистая
функция; её можно протестировать без модели». Здесь эта половина и живёт:
вход — спека и события журнала, выход — значение. Ни БД, ни сети, ни процессов,
ни часов: `now` приходит аргументом, а модуль не импортирует ни `time`, ни
`datetime`. Поэтому один и тот же вход всегда даёт один и тот же ответ — это и
есть предсказуемость, обещанная движком.

Три публичные функции — три проекции одного свёрточного прохода (`_fold`):

- `stage_states(spec, events)` — срез: что с каждым этапом (`StageState`);
- `next_stage(spec, events, now)` — `Decision`: что делать дальше;
- `deal_state(spec, events)` — одно слово о деле целиком.

Одна свёртка на все три нужна не ради экономии: срез и решение обязаны
согласовываться. Две независимые реализации разошлись бы на первом же мусорном
событии, и консоль показывала бы одно, а движок делал другое.

## Словарь событий этапа

Событие относится к этапу, если в `payload` есть `stage` — id этапа спеки.
Событие без `stage` — факт дела. Закрытый набор `kind` — журнальный
(`journal.EVENT_KINDS`); слой не заводит своих видов и не меняет журнал.

| `kind` | `payload` | что значит для этапа |
|---|---|---|
| `attempt_started` | `stage`, `attempt`, `run_id`, `pid` | попытка идёт: `running` |
| `attempt_ended` | `stage`, `attempt`, `outcome`, `kind` | `outcome` из `OK_OUTCOMES` → `done`, иначе провал класса `kind` |
| `question_asked` | `stage`, `attempt`, `question_id` | ждёт человека: `wait_human` |
| `question_answered` | `stage`, `question_id`, `answered_via`, `answer_len` | ответ → новая попытка; истёкший срок → `parked` |
| `completed` | `stage` | этап закрыт: `done` |
| `collected` | `stage`, `target` | приговор `approve`: `target` или `next` |
| `changes_requested` | `stage`, `target`, `reason` | приговор `changes`: возврат на `target` |
| `completed` | без `stage` | дело закрыто целиком |
| `question_asked` | без `stage` (так пишет `ask()`), адрес — в `routing.stage`/`routing.attempt` | вопрос этого этапа → `wait_human`; адреса нет — последней попытки |
| `question_answered` | без `stage` | ответ на этот вопрос → новая попытка того же этапа |
| `blocked` | `block_kind='parked'`, `kind`, `stage`, `attempts`, `limit`, `reason` | дело запарковано: `wait(parked)`, подъёма нет |
| `unblocked` | `previous_kind='parked'` | человек решил продолжать: счётчики класса сброшены, этап снова `pending` |

Исход попытки различается **полем** `outcome`, а не вторым видом события
(`docs/JOURNAL.md`, `dashboard/runner.py`): отдельного `attempt_failed` в
закрытом наборе журнала нет. Классификация отказа приходит готовой в `kind`;
слой её не выводит из текста ошибки.

`collected`/`changes_requested` взяты те же, что уже пишет `dashboard/deals.py`
для своих дел: у вида события одно значение на весь плагин, второй словарь
вердиктов заводить нельзя. Признак «это вердикт этапа, а не дела» — `stage` в
payload: события дел его не пишут.

Вопрос без `stage` (`ask()` не знает этапа — его знает движок) адресуется
последней попытке журнала: другого источника вопроса нет, и без этой адресации
этап, закончивший попытку вопросом, читался бы `done`, а движок пошёл бы дальше
по графу вместо ожидания человека.

## Правила перехода

1. этап стартует, когда он `pending` и все `needs` в состоянии `done`;
2. гейт `verdict` (`approve: next`, `approve: <id>`, `changes: <id>`), число
   возвратов ограничено `max_rounds`, дальше — `failed(contract)`;
3. `parallel_with` разрешает одновременный запуск; без объявления этапы идут по
   одному, порядок — объявления в спеке;
4. провал попытки → следующая попытка того же этапа, пока для этого класса не
   исчерпан лимит политики (`dashboard/failures.py`: `retry.max`, по умолчанию
   `{max: 1, on: [infra]}`); между попытками — пауза от 30 с с потолком 5 мин, и
   до `retry_at` решение — `wait("retry", retry_at)`, а не `run`; исчерпан →
   `failed` этапа, а движок паркует дело (`blocked` с `block_kind='parked'`);
5. вопрос в журнале без ответа → `wait`; ответ → новая попытка (номер растёт);
   вопрос, закрытый по сроку без ответа, → `park`;
6. гейт по родителям: для этапов это `needs`, правило дел (`deals.py`) не
   смешивается с ним — здесь читаются только события этапов;
7. `done`/`failed`/`parked` окончательны для среза: поздний `commented`,
   `attached`, `patched` (тот же `journal.NON_STATE_KINDS`) и поздний факт
   попытки их не меняют;
8. тотальность: на любой последовательности событий (чужие id, повторы, не по
   порядку, пустой журнал, мусор вместо словаря) функция возвращает `Decision`,
   а не падает;
9. парковка: пока открыт `blocked` с `block_kind='parked'`, ничего не
   запускается, а ответ на вопрос парковки попытку не поднимает — продолжение
   только явным `unblocked`, и оно сбрасывает счётчики класса.

## Исходы `Decision` — ровно пять

`run` (запустить попытку), `wait` (ждать факта, который придёт в журнал сам),
`done`, `failed`, `park` (припарковать вопрос человеку). Никаких «а ещё».

`wait` называет, чего ждёт: `attempt` (попытка идёт), `question` (ответ
человека), `verdict` (приговор ревьюера), `gate` (нужный этап/гейт),
`retry` (пауза перед повтором — с моментом `retry_at`), `parked` (дело
запарковано, ждёт решения человека).

**Несколько этапов сразу — это `run` с кортежем `stages`, а не отдельный
`run_many`:** форма одна, значит и обрабатывать её в движке один раз.

Границы: слой только считает. В журнал не пишет (пишет движок — одна транзакция
на факт), процессов не поднимает, вопросов не задаёт.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# `dashboard/journal.py` рядом: плагин грузит модули по пути, пакета нет. Журнал
# берём из `sys.modules` под тем же именем, что и остальной код, — второй
# загрузки (и второго набора `NON_STATE_KINDS`) в процессе быть не должно.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
FAILURES_MODULE_NAME = "hermes_duty_failures"


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

# Политика ретраев и словарь классов живут в одном месте (`dashboard/failures.py`):
# слой перехода читает их оттуда, а не держит вторую копию правил.
failures = _load(FAILURES_MODULE_NAME, "failures.py")

# ------------------------------------------------------------- состояния и виды

# Состояние этапа в срезе. `pending` — можно запускать (или ждёт новой попытки),
# `wait_gate` — ждёт чужих этапов/приговора, остальные говорят сами за себя.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
WAIT_HUMAN = "wait_human"
WAIT_GATE = "wait_gate"
PARKED = "parked"

STAGE_STATES = (PENDING, RUNNING, DONE, FAILED, WAIT_HUMAN, WAIT_GATE, PARKED)

# Окончательные состояния среза (правило 7): поздние факты их не меняют.
TERMINAL_STATES = (DONE, FAILED, PARKED)

# Состояние дела целиком — закрытый набор контракта.
DEAL_STATES = (PENDING, RUNNING, WAIT_HUMAN, WAIT_GATE, DONE, FAILED, PARKED)

# Исходы решения — ровно пять, и никаких «а ещё».
RUN = "run"
WAIT = "wait"
DECISION_DONE = "done"
DECISION_FAILED = "failed"
PARK = "park"

DECISION_KINDS = (RUN, WAIT, DECISION_DONE, DECISION_FAILED, PARK)

# Чего ждёт дело, когда ничего запускать не нужно.
WAIT_ATTEMPT = "attempt"
WAIT_QUESTION = "question"
WAIT_VERDICT = "verdict"
WAIT_FOR_GATE = "gate"
# Пауза перед повтором: этап упал, но политика класса разрешает ещё попытку —
# дело не крутится, а ждёт `retry_at` (её считает `dashboard/failures.py`).
WAIT_RETRY = "retry"
# Парковка: попытки исчерпаны, дело ждёт решения человека (`blocked` с
# `block_kind='parked'`). Подъёма автоматически нет — только `unblocked`.
WAIT_PARKED = "parked"
WAIT_KINDS = (WAIT_ATTEMPT, WAIT_QUESTION, WAIT_VERDICT, WAIT_FOR_GATE, WAIT_RETRY, WAIT_PARKED)

# Исходы попытки, которые считаются успехом. Набор объявлен, а не угадан по
# подстроке: `contract_refused` тоже содержит «contract», и «похоже на успех»
# здесь недопустимо.
OK_OUTCOMES = ("ok", "done", "success", "passed", "accepted")

# Класс отказа, которого в событии нет. Это не догадка о причине, а честное имя
# отсутствия класса: такой отказ не ретраится (`retry.on` его не называет).
# Словарь классов один на плагин — он живёт в `dashboard/failures.py`.
UNCLASSIFIED = failures.UNCLASSIFIED

# Вердикт «дальше по графу»: так пишет спека и так пишет событие.
NEXT = "next"

# Виды событий, которые срез этапа не меняют. Набор — журнальный
# `NON_STATE_KINDS`, минус `question_answered`: для дела ответ закрывающий факт,
# а для этапа ответ — вход (правило 5). Терминальность (правило 7) считается
# раньше этой ветки, поэтому поздний ответ закрытый этап не воскрешает.
QUIET_KINDS = tuple(kind for kind in journal.NON_STATE_KINDS if kind != "question_answered")

# События этапа, которые слой понимает; остальные виды журнала для этапа пусты.
#
# Имена — журнальные (`journal.EVENT_KINDS`): своего набора видов слой не заводит.
# Конец попытки — `attempt_ended` с полем `outcome` (`done`/`failed`) и готовым
# классом `kind`: движок различает исходы полем, а не вторым видом события
# (`docs/JOURNAL.md`, `dashboard/runner.py`). Отдельного `attempt_failed` в
# закрытом наборе журнала нет — и слой его не выдумывает.
STAGE_KINDS = (
    "attempt_started",
    "attempt_ended",
    "question_asked",
    "question_answered",
    "completed",
    "collected",
    "changes_requested",
)

# Вердикты этапа: это решения графа, а не факты попытки, поэтому они
# применяются и к закрытому этапу (иначе приговор некуда было бы прочитать).
VERDICT_KINDS = ("collected", "changes_requested")

APPROVE = "approve"
CHANGES = "changes"


# ------------------------------------------------------------------- структуры


@dataclass(frozen=True)
class StageState:
    """Срез одного этапа: состояние, попытка, раунды, ожидание, отказ.

    `failures` — пары «класс отказа → сколько раз» в стабильном порядке: срез
    должен быть сравнимым (`==`) и воспроизводимым, а не «словарь как получится».
    `retry_at` — момент, раньше которого повтор запрещён паузой политики
    (`dashboard/failures.py`); `None` — паузы нет: класс не повторяется или
    отказов не было.
    """

    id: str
    state: str
    attempt: int = 0
    rounds: int = 0
    needs: tuple[str, ...] = ()
    question_id: Optional[str] = None
    fail_kind: str = ""
    failures: tuple[tuple[str, int], ...] = ()
    reason: str = ""
    retry_at: Optional[int] = None

    @property
    def terminal(self) -> bool:
        """Окончателен ли этап: поздний комментарий его уже не меняет."""
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class Decision:
    """Что делать дальше. Ровно пять исходов, различаются полем `kind`.

    `run` несёт кортеж `stages` (один этап — кортеж из одного): несколько
    этапов сразу — это по-прежнему `run`, отдельного `run_many` нет. `wait`
    называет, чего ждём (`what`), `failed` — класс отказа (`fail_kind`), `park` —
    этап и вопрос человека. `retry_at` заполняется только у `wait("retry")`:
    это момент, раньше которого повторять нельзя.
    """

    kind: str
    stages: tuple[str, ...] = ()
    attempts: tuple[int, ...] = ()
    what: str = ""
    fail_kind: str = ""
    question_id: Optional[str] = None
    reason: str = ""
    retry_at: Optional[int] = None

    @property
    def stage(self) -> Optional[str]:
        """Единственный этап решения; у пачки этапов — `None` (смотри `stages`)."""
        return self.stages[0] if len(self.stages) == 1 else None

    @property
    def attempt(self) -> int:
        """Номер попытки единственного этапа; у пачки — 0 (смотри `attempts`)."""
        return self.attempts[0] if len(self.attempts) == 1 else 0

    @property
    def pair(self) -> list[tuple[str, int]]:
        """Пары «этап → попытка» в порядке запуска: одна форма для движка и тестов."""
        return list(zip(self.stages, self.attempts))

    def __str__(self) -> str:
        if self.kind == RUN:
            body = ", ".join(f"{stage}#{attempt}" for stage, attempt in self.pair)
            return f"run({body})"
        if self.kind == WAIT:
            return f"wait({self.what})"
        if self.kind == DECISION_FAILED:
            return f"failed({self.fail_kind})"
        if self.kind == PARK:
            return f"park({self.stages[0] if self.stages else '—'},{self.question_id or '—'})"
        return self.kind


# ------------------------------------------------------- конструкторы решений


def run(stages: Any, attempts: Any = None, reason: str = "") -> Decision:
    """Запустить попытки: `stages` — id или кортеж id, `attempts` — номер или номера.

    Единственная форма «несколько этапов сразу»: кортеж `stages` (правило 3,
    порядок — объявления в спеке). Отдельного `run_many` нет намеренно.
    """
    ids = (str(stages),) if isinstance(stages, str) else tuple(str(stage) for stage in stages)
    if attempts is None:
        numbers: tuple[int, ...] = tuple(1 for _ in ids)
    elif isinstance(attempts, int) and not isinstance(attempts, bool):
        numbers = tuple(int(attempts) for _ in ids)
    else:
        numbers = tuple(_int(number) for number in attempts)
    # Длины приводит конструктор, а не вызывающий: иначе движок читал бы
    # `zip` и молча запускал этап без номера попытки.
    numbers = tuple(numbers[index] if index < len(numbers) else 1 for index in range(len(ids)))
    return Decision(kind=RUN, stages=ids, attempts=numbers, reason=reason)


def wait(what: str, reason: str = "", retry_at: Optional[int] = None) -> Decision:
    """Ждать факта: идущей попытки, ответа человека, приговора или `needs`.

    `retry_at` — только у `wait("retry")`: момент, раньше которого пауза политики
    не пускает повтор (`dashboard/failures.py`). У остальных ожиданий его нет.
    """
    return Decision(kind=WAIT, what=str(what), reason=reason, retry_at=retry_at)


def done(reason: str = "") -> Decision:
    """Дело закрыто: все этапы пройдены (или в журнале записано закрытие)."""
    return Decision(kind=DECISION_DONE, reason=reason)


def failed(kind: str, reason: str = "") -> Decision:
    """Дело остановлено отказом: класс пришёл из события или это `contract`."""
    return Decision(kind=DECISION_FAILED, fail_kind=str(kind), reason=reason)


def park(stage_id: str, question_id: Any = None, reason: str = "") -> Decision:
    """Припарковать вопрос человеку: автоматически перезапускать нечего."""
    return Decision(
        kind=PARK,
        stages=(str(stage_id),),
        question_id=None if question_id is None else str(question_id),
        reason=reason,
    )


# ------------------------------------------------------------ свёртка журнала


@dataclass
class _Stage:
    """Внутреннее состояние этапа во время свёртки (изменяемое — меняется проходом)."""

    id: str
    state: str = PENDING
    attempt: int = 0
    rounds: int = 0
    question_id: Optional[str] = None
    fail_kind: str = ""
    failures: dict[str, int] = field(default_factory=dict)
    verdict: Optional[tuple[str, str]] = None
    run_id: str = ""
    since: int = 0
    reason: str = ""
    # Время последнего отказа: от него считается пауза перед повтором
    # (`retry_at`). Ноль — отказа не было.
    failed_at: int = 0


@dataclass
class _Board:
    """Результат свёртки: этапы по id (в порядке объявления) и закрытие дела."""

    stages: dict[str, _Stage] = field(default_factory=dict)
    closed: bool = False
    closed_reason: str = ""
    # Последняя попытка, о которой журнал сказал фактом: (этап, номер). Нужна для
    # вопроса без `stage` — адресовать его больше некому (смотри `_deal_question`).
    last_attempt: tuple[str, int] = ("", 0)
    # Открытый вопрос дела и этап, которому он принадлежит: ("", id) — вопрос есть,
    # адресат неизвестен (мусорный журнал), и тогда он всё равно держит дело.
    question: tuple[str, str] = ("", "")
    # Открытая парковка: `blocked` с `block_kind='parked'` без `unblocked`.
    # Парковка — не состояние ожидания, а факт «попытки исчерпаны, человек решает»;
    # подъём автоматически запрещён, и это правило держится здесь.
    parked: Optional[dict[str, Any]] = None

    def state_of(self, stage_id: str) -> str:
        """Состояние этапа или `wait_gate` для этапа, которого нет в спеке."""
        node = self.stages.get(stage_id)
        return node.state if node is not None else WAIT_GATE


def _int(value: Any) -> int:
    """Целое из payload: `True` числом не считается, мусор — нулём."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    """`payload` события объектом: разбор тот же, что у журнала (мусор → `{}`)."""
    return journal.payload_of(event)


def _payload_key(payload: dict[str, Any]) -> str:
    """Стабильный текст payload — последний ключ порядка: перестановка не решает исход."""
    try:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str уже всё сериализует
        return ""


def _kind_rank(kind: str) -> int:
    """Место вида в каноническом порядке применения (`journal.KIND_ORDER`)."""
    order = journal.KIND_ORDER
    return order.index(kind) if kind in order else len(order)


def _group(kind: str, stage_scoped: bool) -> int:
    """Ярус порядка при равном `ts`: факты этапа → приговоры → факты дела.

    Одной таблицей это не выражается: журнальный `KIND_ORDER` — порядок **дел**,
    где `collected` обязан примениться раньше `completed`. У этапа причинность
    обратная: попытка кончается, потом выносится приговор, потом (если дело
    закрывается) пишется закрытие. Поэтому ярус считается по принадлежности
    события, а вид внутри яруса по-прежнему берётся из журнальной таблицы — второй
    таблицы видов в плагине нет.
    """
    if not stage_scoped:
        return 2
    return 1 if kind in VERDICT_KINDS else 0


def _ordered(events: Iterable[Any]) -> list[dict[str, Any]]:
    """События в порядке применения: `(ts, id, ярус, место вида, payload)`.

    Порядок считается по данным события, а не по порядку входа: тогда тот же
    набор событий, поданный иначе, применяется так же — иначе «не по порядку» из
    правила 8 давало бы разные ответы. Мусор вместо словаря отбрасывается: это не
    событие, а строка в списке.
    """
    keyed: list[tuple[tuple[int, int, int, int, str], dict[str, Any]]] = []
    for event in events or ():
        if not isinstance(event, dict):
            continue
        payload = _payload(event)
        kind = str(event.get("kind") or "")
        stage_id = payload.get("stage")
        key = (
            _int(event.get("ts")),
            _int(event.get("id")),
            _group(kind, isinstance(stage_id, str) and bool(stage_id)),
            _kind_rank(kind),
            _payload_key(payload),
        )
        keyed.append((key, event))
    keyed.sort(key=lambda row: row[0])
    return [event for _, event in keyed]


def _children(spec: Any) -> dict[str, list[str]]:
    """Наследники этапа по `needs` (прямые), в порядке объявления."""
    children: dict[str, list[str]] = {stage.id: [] for stage in spec.stages}
    for stage in spec.stages:
        for need in stage.needs:
            children.setdefault(need, []).append(stage.id)
    return children


def _descendants(spec: Any) -> dict[str, set[str]]:
    """Все наследники этапа по `needs` (транзитивно): их возврат переоткрывает тоже."""
    children = _children(spec)
    result: dict[str, set[str]] = {}
    for stage in spec.stages:
        seen: set[str] = set()
        stack = list(children.get(stage.id, []))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(children.get(current, []))
        result[stage.id] = seen
    return result


def _stage_spec(spec: Any, stage_id: str) -> Any:
    """Этап спеки по id; `None` — спека такого этапа не знает."""
    finder = getattr(spec, "stage", None)
    return finder(stage_id) if callable(finder) else None


def _retriable(stage_spec: Any, node: _Stage, fail_kind: str) -> bool:
    """Есть ли ещё попытка для этого класса отказа (правило 4).

    Считается по классу: лимит повторов — сколько раз повторять отказ **этого**
    класса, а класс, которого нет в политике `on`, не повторяется вовсе.
    Инфра-сбой и отказ контракта — разные причины, и общий счётчик склеил бы их
    в один. Сама политика (лимит по умолчанию, пауза, предохранитель) живёт в
    `dashboard/failures.py`: здесь только чтение её ответа.
    """
    return failures.retriable(
        failures.policy(stage_spec), fail_kind, node.failures.get(fail_kind, 0)
    )


def _retry_at(stage_spec: Any, node: _Stage) -> Optional[int]:
    """Когда политика позволит повторить упавший этап; `None` — повтора не будет.

    Пауза считается от времени отказа и растёт с числом отказов класса
    (`dashboard/failures.py`). Возвращаемое время — факт из журнала плюс
    объявленная пауза, а не «сколько подождать»: часы слой не читает.
    """
    if not node.fail_kind or node.failed_at <= 0:
        return None
    policy = failures.policy(stage_spec)
    attempts = node.failures.get(node.fail_kind, 0)
    if not failures.retriable(policy, node.fail_kind, attempts):
        return None
    return failures.retry_at(attempts, node.failed_at)


def _expired(payload: dict[str, Any]) -> bool:
    """Закрыт ли вопрос по сроку, а не ответом: `answered_via='expired'` или пустой ответ.

    Это факт журнала (`docs/JOURNAL.md`): TTL снимает вопрос так же, как ответ, но
    входа у этапа не появляется — перезапускать попытку по пустому ответу значило
    бы выдать тишину за решение.
    """
    if str(payload.get("answered_via") or "").strip().lower() == "expired":
        return True
    return _int(payload.get("answer_len")) == 0


def _fail(node: _Stage, fail_kind: str, text: str, at: int = 0) -> None:
    """Записать провал попытки: счётчик по классу, класс и время отказа.

    Состояние здесь не ставится: решает политика ретрая (`_retriable`), и это
    решение принимает вызывающий — иначе «провал» и «этап остановлен» склеились
    бы в одно место и разошлись с политикой. `at` — время отказа из события: от
    него считается пауза перед повтором, и часы слою для этого не нужны.
    """
    node.failures[fail_kind] = node.failures.get(fail_kind, 0) + 1
    node.fail_kind = fail_kind
    node.reason = text
    node.failed_at = at or node.failed_at


def _reopen(board: _Board, spec: Any, stage_id: str, extra: Iterable[str] = ()) -> None:
    """Переоткрыть закрытые этапы: сам этап, его наследники и названные дополнительно.

    Переоткрываются только `done`-этапы: незакрытые (`running`, `wait_human`)
    доживают свою попытку. Приговор закрытого этапа живёт в его собственных
    событиях, поэтому при переоткрытии он снимается — иначе старый `approve`
    пропустил бы новую работу без нового ревью.
    """
    targets = {stage_id, *_descendants(spec).get(stage_id, set()), *extra}
    for target in sorted(targets):  # порядок стабилен: переоткрытие не зависит от хешей
        node = board.stages.get(target)
        if node is None or node.state != DONE:
            continue
        node.state = PENDING
        node.verdict = None
        node.question_id = None
        node.reason = "переоткрыт возвратом на доработку"


def _verdict_approve(board: _Board, node: _Stage, payload: dict[str, Any]) -> None:
    """Приговор `approve`: `next` — дальше по графу, `<id>` — названный этап."""
    target = str(payload.get("target") or payload.get(APPROVE) or NEXT)
    node.verdict = (APPROVE, target)
    node.reason = f"приговор approve: {target}"


def _verdict_changes(board: _Board, spec: Any, node: _Stage, payload: dict[str, Any]) -> None:
    """Приговор `changes`: возврат на предшественника под учётом `max_rounds`.

    Возврат закрывает раунд ревью целиком: переоткрывается названный этап и все,
    кто от него зависит, **и сам этап с приговором** — иначе следующая публикация
    прошла бы по старому `done` ревью, то есть ревью обогнала бы реализацию.
    """
    target = str(payload.get("target") or payload.get(CHANGES) or "")
    spec_stage = _stage_spec(spec, node.id)
    verdict = getattr(spec_stage, "verdict", None)
    max_rounds = _int(getattr(verdict, "max_rounds", 1)) or 1
    node.rounds += 1
    node.verdict = (CHANGES, target)
    if node.rounds > max_rounds:
        node.state = FAILED
        node.fail_kind = "contract"
        node.reason = f"возвратов {node.rounds} больше max_rounds={max_rounds}"
        return
    node.state = PENDING
    node.question_id = None
    node.reason = f"возврат на {target or '—'}: раунд {node.rounds} из {max_rounds}"
    if target:
        _reopen(board, spec, target, extra=(node.id,))


def _stage_event(
    spec: Any, board: _Board, node: _Stage, kind: str, payload: dict[str, Any], at: int = 0
) -> None:
    """Один факт этапа. Неизвестный вид и мусорный payload не меняют ничего."""
    if kind in QUIET_KINDS:
        return
    if kind == "collected":
        _verdict_approve(board, node, payload)
        return
    if kind == "changes_requested":
        _verdict_changes(board, spec, node, payload)
        return
    if node.state in TERMINAL_STATES:
        return

    if kind == "attempt_started":
        run_id = str(payload.get("run_id") or "")
        if run_id and run_id == node.run_id and node.state == RUNNING:
            return  # повтор того же факта: та же попытка, а не следующая
        node.run_id = run_id
        node.attempt = max(node.attempt, _int(payload.get("attempt")) or node.attempt + 1)
        node.state = RUNNING
        node.question_id = None
        node.failed_at = 0
        node.since = at or node.since
        node.reason = f"попытка {node.attempt} идёт"
        board.last_attempt = (node.id, node.attempt)
        return

    if kind == "attempt_ended":
        node.attempt = max(node.attempt, _int(payload.get("attempt")) or node.attempt)
        board.last_attempt = (node.id, node.attempt)
        outcome = str(payload.get("outcome") or "").strip().lower()
        fail_kind = str(payload.get("kind") or "").strip()
        if outcome in OK_OUTCOMES and not fail_kind:
            node.state = DONE
            node.question_id = None
            # Новое закрытие этапа закрывает и старый приговор: приговор прошлого
            # раунда не должен пропускать работу, которую ещё никто не смотрел.
            node.verdict = None
            node.reason = f"попытка {node.attempt}: {outcome or 'ok'}"
            return
        fail_kind = fail_kind or UNCLASSIFIED
        stage_spec = _stage_spec(spec, node.id)
        text = f"попытка {node.attempt}: отказ {fail_kind}"
        _fail(node, fail_kind, text, at)
        if not _retriable(stage_spec, node, fail_kind):
            node.state = FAILED
            node.reason = f"{text}: попытки для этого класса исчерпаны"
        else:
            node.state = PENDING  # следующая попытка того же этапа
            node.reason = f"{text}: будет попытка {node.attempt + 1}"
        return

    if kind == "completed":
        node.state = DONE
        node.question_id = None
        node.verdict = None  # закрытие этапа закрывает и приговор прошлого раунда
        node.reason = "этап закрыт событием completed"
        return

    if kind == "question_asked":
        node.question_id = _question_id(payload)
        node.attempt = max(node.attempt, _int(payload.get("attempt")) or node.attempt)
        node.state = WAIT_HUMAN
        node.since = at or node.since
        node.reason = f"вопрос #{node.question_id or '—'} без ответа"
        board.last_attempt = (node.id, node.attempt)
        return

    if kind == "question_answered":
        question_id = _question_id(payload)
        if node.question_id is None:
            return  # ответ, которому нечего закрывать: чужой или уже снятый вопрос
        if question_id and question_id != node.question_id:
            return
        if _expired(payload):
            # Вопрос остаётся названным: припаркованное дело человек открывает по
            # номеру вопроса, а не по тексту причины.
            node.state = PARKED
            node.reason = f"вопрос #{node.question_id} закрыт по сроку без ответа"
            return
        node.question_id = None
        if board.parked is not None:
            # Ответ на вопрос запаркованного дела попытку не поднимает: продолжение
            # — только явное решение человека (`unblocked`), а не ответ в пуле.
            node.reason = f"ответ #{question_id or '—'}: дело запарковано — ждёт решения"
            return
        node.state = PENDING
        node.reason = f"ответ #{question_id or '—'}: новая попытка {node.attempt + 1}"


def _question_id(payload: dict[str, Any]) -> Optional[str]:
    """`question_id` строкой: номер из пула бывает и числом, и строкой."""
    raw = payload.get("question_id")
    if raw is None or raw == "":
        return None
    return str(raw)


def _deal_question(board: _Board, payload: dict[str, Any]) -> None:
    """Вопрос **без** `stage`: адресовать его некому, кроме названного адреса.

    `ask()` пишет `question_asked` на уровне дела (`dashboard/ask.py`): вопросам
    он не знает этапа, и это правильно — этап знает движок. Движок и записывает
    его в `payload.routing` (`dashboard/runner.py`), поэтому адрес берётся оттуда,
    а `board.last_attempt` — запасной источник для вопросов, заданных мимо
    движка (`POST /ask`) и для фактов до W5. Без адресации этап, у которого
    попытка закончилась вопросом, не `done`, а `wait_human`: работа не сделана,
    её ждёт человек, и после ответа нужна **новая** попытка (правило 5).
    """
    question_id = _question_id(payload)
    stage_id, attempt = _addressed(board, payload)
    board.question = (stage_id, question_id or "")
    node = board.stages.get(stage_id)
    if node is None or question_id is None:
        return
    node.question_id = question_id
    node.attempt = max(node.attempt, attempt)
    if node.state in (FAILED, PARKED) or board.parked is not None:
        # Вопрос запаркованного дела — факт парковки, а не повод поднять этап:
        # исчерпанный этап остаётся `failed`, а дело — ждёт решения человека.
        return
    node.state = WAIT_HUMAN
    node.reason = f"попытка {node.attempt} закончилась вопросом #{question_id}"


def _addressed(board: _Board, payload: dict[str, Any]) -> tuple[str, int]:
    """Этап и попытка, которым принадлежит вопрос: адрес движка, иначе последняя.

    `payload.routing.stage` пишет `_parked` (`dashboard/routing.py` решает, кому
    вопрос адресован, движок знает, кто спросил). При параллельных этапах
    «последняя попытка журнала» — не обязательно спросившая, поэтому объявленный
    адрес важнее догадки. Мусор и незнакомый этап адресом не считаются.
    """
    routing = payload.get("routing")
    if isinstance(routing, dict):
        stage_id = str(routing.get("stage") or "")
        node = board.stages.get(stage_id)
        if node is not None:
            return stage_id, _int(routing.get("attempt")) or node.attempt
    return board.last_attempt


def _deal_answer(board: _Board, payload: dict[str, Any]) -> None:
    """Ответ или срок по вопросу дела: новая попытка того же этапа либо `park`."""
    stage_id, asked = board.question
    question_id = _question_id(payload)
    if not asked or (question_id and question_id != asked):
        return  # ответ на чужой/уже снятый вопрос: снимать нечего
    board.question = ("", "")
    node = board.stages.get(stage_id)
    if node is None or node.state in TERMINAL_STATES:
        return
    if _expired(payload):
        node.state = PARKED
        node.reason = f"вопрос #{asked} закрыт по сроку без ответа"
        return
    node.question_id = None
    if board.parked is not None:
        # Парковка: ответ на вопрос — ещё не решение продолжать. Подъём только
        # явным `unblocked` (`continue`), иначе дело крутилось бы само.
        node.reason = f"ответ #{asked}: дело запарковано — ждёт решения человека"
        return
    node.state = PENDING
    node.reason = f"ответ #{asked}: новая попытка {node.attempt + 1}"


def _park_fact(board: _Board, kind: str, payload: dict[str, Any]) -> None:
    """Парковка дела: `blocked` с `block_kind='parked'` и снятие её `unblocked`.

    Парковка — не девятое состояние ожидания (его нет, `docs/DESIGN.md`), а факт
    «попытки исчерпаны, человек решает»: он приходит существующим `blocked`
    (`dashboard/runner.py`, `deals.set_blocked`). Здесь слой запоминает его, чтобы
    (а) не запускать попытки, пока парковка открыта, и (б) поднять этап **только**
    явным решением человека: `unblocked` со `previous_kind='parked'` сбрасывает
    счётчики класса — иначе новый отказ сразу упёрся бы в исчерпанный лимит.

    Ответ на вопрос парковки (`question_answered`) сюда не относится и разбирается
    отдельно: он попытку не поднимает.
    """
    if kind == "blocked":
        if str(payload.get("block_kind") or "") != failures.PARKED_KIND:
            return
        board.parked = _parked_payload(payload)
        return
    if kind != "unblocked" or board.parked is None:
        return
    if str(payload.get("previous_kind") or "") != failures.PARKED_KIND:
        return
    parked = board.parked
    board.parked = None
    stage_id = str(parked.get("stage") or "")
    node = board.stages.get(stage_id)
    if node is None:
        return
    node.failures.clear()
    node.fail_kind = ""
    node.failed_at = 0
    node.state = PENDING
    node.reason = "человек решил продолжать: счётчики отказа сброшены"


def _parked_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Факты парковки из `blocked`: класс, этап, сколько попыток, лимит и вопрос."""
    return {
        "kind": str(payload.get("kind") or payload.get("fail_kind") or UNCLASSIFIED),
        "stage": str(payload.get("stage") or ""),
        "attempts": _int(payload.get("attempts")),
        "limit": _int(payload.get("limit")),
        "reason": str(payload.get("park_reason") or payload.get("reason") or ""),
        "question_id": _question_id(payload),
    }


def _fold(spec: Any, events: Iterable[Any]) -> _Board:
    """Свернуть события в состояние этапов и закрытость дела.

    Чистая функция: только вход. Чужие **этапы** и неизвестные виды пропускаются
    молча — журнал общий, и дело не должно падать от факта соседа (правило 8).
    Отбор по `deal_id` слой не делает: события уже отобраны по делу читателем
    (`journal.read_events(conn, deal_id)`), а признак принадлежности этапу —
    `stage` в `payload`, и второй фильтр здесь молча выкинул бы факт, если бы
    вызывающий передал события шире.
    """
    board = _Board(stages={stage.id: _Stage(id=stage.id) for stage in spec.stages})
    known = set(board.stages)
    for event in _ordered(events):
        kind = str(event.get("kind") or "")
        payload = _payload(event)
        if kind in ("blocked", "unblocked"):
            # Парковка — факт **дела**, даже когда в payload назван этап: `blocked`
            # с `stage` не делает блок фактом этапа (`stage` в журнале блокировки —
            # адрес отказа, а не принадлежность событию).
            _park_fact(board, kind, payload)
            continue
        stage_id = payload.get("stage")
        if not isinstance(stage_id, str) or not stage_id:
            if kind == "completed":
                board.closed = True
                board.closed_reason = "журнал: дело закрыто событием completed"
            elif kind == "question_asked":
                _deal_question(board, payload)
            elif kind == "question_answered":
                _deal_answer(board, payload)
            continue
        if stage_id not in known or kind not in STAGE_KINDS:
            continue
        _stage_event(spec, board, board.stages[stage_id], kind, payload, _int(event.get("ts")))
    return board


# --------------------------------------------------------------- гейт графа


def _verdict_of(spec: Any, board: _Board, stage_id: str) -> Optional[tuple[str, str]]:
    """Приговор этапа: последний записанный факт `collected`/`changes_requested`."""
    node = board.stages.get(stage_id)
    return node.verdict if node is not None else None


def _gate(spec: Any, board: _Board, stage: Any) -> tuple[str, str]:
    """Чего этап ждёт и почему: `("", "")` — может стартовать.

    Один предикат на срез и на решение: консоль и движок не должны расходиться в
    том, что такое «готов». Правило 1 (`needs`) и правило 2 (гейт `verdict`)
    сходятся здесь, а не в двух местах. Первое поле — вид ожидания: `needs` и
    адрес приговора ждут графа, отсутствующий приговор — решения человека.
    """
    unmet = [need for need in stage.needs if board.state_of(need) != DONE]
    if unmet:
        return WAIT_FOR_GATE, f"ждёт needs: {', '.join(unmet)}"
    for need in stage.needs:
        need_spec = _stage_spec(spec, need)
        if need_spec is None or getattr(need_spec, "verdict", None) is None:
            continue
        verdict = _verdict_of(spec, board, need)
        if verdict is None:
            return WAIT_VERDICT, f"ждёт приговора этапа {need!r}"
        action, target = verdict
        if action != APPROVE or target in (NEXT, "") or target == stage.id:
            continue
        if board.state_of(target) == DONE and board.stages.get(target) is not None:
            continue  # названный этап уже пройден: ограничение приговора снято
        return WAIT_FOR_GATE, f"приговор {need!r} адресует этап {target!r}"
    return "", ""


def _gate_reason(spec: Any, board: _Board, stage: Any) -> str:
    """Почему этап ещё не может стартовать; пустая строка — может."""
    return _gate(spec, board, stage)[1]


def _slice(spec: Any, board: _Board) -> dict[str, str]:
    """Состояния этапов в порядке объявления: `pending` без хода превращается в `wait_gate`."""
    result: dict[str, str] = {}
    for stage in spec.stages:
        state = board.state_of(stage.id)
        if state == PENDING and _gate_reason(spec, board, stage):
            state = WAIT_GATE
        result[stage.id] = state
    return result


def _first(spec: Any, states: dict[str, str], wanted: str) -> Any:
    """Первый этап спеки в нужном состоянии: порядок объявления, а не случай."""
    for stage in spec.stages:
        if states.get(stage.id) == wanted:
            return stage
    return None


def _runnable(spec: Any, board: _Board, now: int) -> list[Any]:
    """Этапы, которые можно запускать прямо сейчас, в порядке объявления.

    Этап, у которого пауза политики ещё не вышла, в список не попадает: повтор
    «когда захочется» обошёл бы и паузу, и её смысл.
    """
    ready: list[Any] = []
    for stage in spec.stages:
        if board.state_of(stage.id) != PENDING or _gate_reason(spec, board, stage):
            continue
        until = _pause_until(stage, board)
        if until is not None and now < until:
            continue
        ready.append(stage)
    return ready


def _pause_until(stage: Any, board: _Board) -> Optional[int]:
    """До какого момента повтор этапа запрещён паузой; `None` — запрета нет."""
    node = board.stages.get(stage.id)
    if node is None or node.state != PENDING:
        return None
    return _retry_at(stage, node)


def _parallel(left: Any, right: Any) -> bool:
    """Объявлены ли этапы друг другу `parallel_with` (в любую сторону).

    Взаимность не требуется: валидатор её не проверяет, и односторонняя пометка —
    это всё равно объявление, а не догадка слоя о том, что «имелось в виду».
    """
    return right.id in tuple(left.parallel_with or ()) or left.id in tuple(right.parallel_with or ())


def _batch(spec: Any, runnable: list[Any]) -> tuple[str, ...]:
    """Пачка запуска: первый готовый этап и все, кто объявлен с ним параллельным.

    Одновременно идёт только объявленное (правило 3): два готовых этапа без
    `parallel_with` запускаются по одному, иначе слой сам придумал бы параллелизм,
    которого спека не просила. Порядок внутри пачки — объявления в спеке.
    """
    picked: list[Any] = []
    for stage in runnable:
        if all(_parallel(stage, other) for other in picked):
            picked.append(stage)
    return tuple(stage.id for stage in picked)


# --------------------------------------------------------------- проекции


def stage_states(spec: Any, events: Iterable[Any]) -> dict[str, StageState]:
    """Срез этапов: что с каждым прямо сейчас. Порядок — объявления в спеке.

    Состояние, номер попытки, раунды возвратов, открытый вопрос, класс последнего
    отказа и причина. Источник — только события; ни БД, ни процессов.
    """
    board = _fold(spec, events)
    states = _slice(spec, board)
    result: dict[str, StageState] = {}
    for stage in spec.stages:
        node = board.stages[stage.id]
        state = states[stage.id]
        reason = (
            node.reason if state == node.state else _gate_reason(spec, board, stage) or node.reason
        )
        result[stage.id] = StageState(
            id=stage.id,
            state=state,
            attempt=node.attempt,
            rounds=node.rounds,
            needs=tuple(stage.needs or ()),
            question_id=node.question_id,
            fail_kind=node.fail_kind,
            failures=tuple(sorted(node.failures.items())),
            reason=reason,
            retry_at=_pause_until(stage, board),
        )
    return result


def _age(ts: int, now: int) -> str:
    """Возраст метки словами; время не записано — так и говорится."""
    if ts <= 0:
        return "время не записано"
    seconds = max(0, now - ts)
    if seconds < 60:
        return f"{seconds}с"
    if seconds < 3600:
        return f"{seconds // 60}мин"
    if seconds < 86400:
        return f"{seconds // 3600}ч"
    return f"{seconds // 86400}д"


def next_stage(spec: Any, events: Iterable[Any], now: Any) -> Decision:
    """Что делать дальше — ровно один `Decision` на любой вход (правило 8).

    Порядок разбора (он же приоритет, он же контракт):

    1. дело закрыто событием `completed` без `stage` → `done`;
    2. дело запарковано (`blocked` с `block_kind='parked'`) → `wait(parked)`: без
       решения человека ничего не запускается и ничего не поднимается;
    3. любой этап `failed` → `failed` (класс — из события, `contract` — из раундов);
    4. любой этап `parked` (вопрос закрыт без ответа) → `park`;
    5. есть готовые этапы → `run` пачкой по правилу 3;
    6. идёт попытка → `wait(attempt)`;
    7. есть открытый вопрос → `wait(question)`;
    8. этап ждёт паузы перед повтором → `wait(retry, retry_at)`;
    9. все этапы `done` → `done`;
    10. иначе граф стоит → `wait(gate)`.

    `now` нужен тексту причины («попытка идёт 12мин») и решению о паузе перед
    повтором: до `retry_at` повтор не выдаётся. Часов модуль не читает.
    """
    board = _fold(spec, events)
    seconds = _int(now)
    if board.closed:
        return done(board.closed_reason)
    if not spec.stages:
        return done("в спеке нет этапов: решать нечего")

    if board.parked is not None:
        parked = board.parked
        return wait(
            WAIT_PARKED,
            "дело запарковано: {} на этапе {!r} — {}".format(
                failures.counter(parked["kind"], parked["attempts"], parked["limit"]),
                parked["stage"] or "—",
                parked["reason"] or "попытки исчерпаны",
            )
            + (f"; вопрос #{parked['question_id']}" if parked["question_id"] else "")
            + "; продолжение — только решением человека",
        )

    states = _slice(spec, board)

    stopped = _first(spec, states, FAILED)
    if stopped is not None:
        node = board.stages[stopped.id]
        return failed(node.fail_kind or UNCLASSIFIED, f"этап {stopped.id!r}: {node.reason}")

    parked = _first(spec, states, PARKED)
    if parked is not None:
        node = board.stages[parked.id]
        return park(parked.id, node.question_id, f"этап {parked.id!r}: {node.reason}")

    # Вопрос дела, которому не нашлось этапа: адрес неизвестен, но человек его
    # видит и ждёт ответа — запускать поверх открытого вопроса нельзя.
    if board.question[1] and not board.question[0]:
        return wait(WAIT_QUESTION, f"вопрос #{board.question[1]} без адресата в спеке: ответа нет")

    runnable = _runnable(spec, board, seconds)
    if runnable:
        batch = _batch(spec, runnable)
        numbers = tuple(board.stages[stage_id].attempt + 1 for stage_id in batch)
        return run(batch, numbers, f"готовы: {', '.join(batch)}")

    running = _first(spec, states, RUNNING)
    if running is not None:
        node = board.stages[running.id]
        return wait(
            WAIT_ATTEMPT,
            f"этап {running.id!r}: попытка {node.attempt} идёт, {_age(node.since, seconds)}",
        )

    question = _first(spec, states, WAIT_HUMAN)
    if question is not None:
        node = board.stages[question.id]
        return wait(
            WAIT_QUESTION,
            f"этап {question.id!r}: вопрос #{node.question_id or '—'} без ответа, "
            f"{_age(node.since, seconds)}",
        )

    waiting_retry = _waiting_retry(spec, board, seconds)
    if waiting_retry is not None:
        stage, until = waiting_retry
        node = board.stages[stage.id]
        return wait(
            WAIT_RETRY,
            "этап {!r}: пауза перед повтором после отказа {}, повтор не раньше чем через {}с".format(
                stage.id,
                failures.counter(node.fail_kind, node.failures.get(node.fail_kind, 0), failures.policy(stage).max),
                max(0, until - seconds),
            ),
            retry_at=until,
        )

    if all(state == DONE for state in states.values()):
        return done("все этапы закрыты")

    gated = next(stage for stage in spec.stages if states[stage.id] != DONE)
    what, why = _gate(spec, board, gated)
    return wait(what or WAIT_FOR_GATE, f"этап {gated.id!r}: {why or 'граф стоит'}")


def _waiting_retry(spec: Any, board: _Board, now: int) -> Optional[tuple[Any, int]]:
    """Первый по спеке этап, который ждёт паузы перед повтором, и её конец."""
    for stage in spec.stages:
        if board.state_of(stage.id) != PENDING:
            continue
        until = _pause_until(stage, board)
        if until is not None and now < until:
            return stage, until
    return None


def deal_state(spec: Any, events: Iterable[Any]) -> str:
    """Состояние дела целиком — одно из `DEAL_STATES`.

    Приоритет повторяет журнальный порядок ожиданий: человек (`wait_human`),
    припаркованный вопрос, отказ, работа. Запаркованное дело — `wait_human`:
    парковка ждёт **человека**, и отдельного состояния для неё нет
    (`docs/DESIGN.md`). `pending` — есть что запускать; `wait_gate` — запускать
    нечего, а дело не закрыто (граф стоит).
    """
    board = _fold(spec, events)
    if board.closed:
        return DONE
    if not spec.stages:
        return DONE
    if board.parked is not None:
        return WAIT_HUMAN
    states = _slice(spec, board)
    for wanted in (FAILED, PARKED):
        if _first(spec, states, wanted) is not None:
            return wanted
    if board.question[1] and not board.question[0]:
        return WAIT_HUMAN  # вопрос дела без адресата: человек всё равно решает
    for wanted in (WAIT_HUMAN, RUNNING):
        if _first(spec, states, wanted) is not None:
            return wanted
    if all(state == DONE for state in states.values()):
        return DONE
    if _first(spec, states, PENDING) is not None:
        return PENDING
    return WAIT_GATE
