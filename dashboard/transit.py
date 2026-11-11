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
| `question_answered` | без `stage`; у вопроса роли ещё `by` — номера пула нет | ответ на этот вопрос → новая попытка того же этапа |
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
запарковано, ждёт решения человека), `plan` (решается план дела: этап плана в
работе, предложение ждёт проверки или гейта плана — `docs/ENGINE-1.0.md`).

**Несколько этапов сразу — это `run` с кортежем `stages`, а не отдельный
`run_many`:** форма одна, значит и обрабатывать её в движке один раз.

Границы: слой только считает. В журнал не пишет (пишет движок — одна транзакция
на факт), процессов не поднимает, вопросов не задаёт.
"""

from __future__ import annotations

import dataclasses
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

# Политика ретраев и словарь классов живут в одном месте (`dashboard/failures.py`):
# слой перехода читает их оттуда, а не держит вторую копию правил.
failures = _load(FAILURES_MODULE_NAME, "failures.py")

# Спека — ради одного: этапы плана разбираются её же разбором (`spec.parse_plan`).
# Второй разбор предложения разошёлся бы с `spec check` на первом же новом поле.
spec_module = _load(SPEC_MODULE_NAME, "spec.py")

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
# План дела: этап плана в работе, предложение ждёт проверки или гейта плана.
# Отдельное слово, а не `attempt`/`gate`: «дело стоит, потому что решается план»
# — это факт о графе, и склеивание его с ожиданием попытки спрятало бы, что
# графа ещё нет (`docs/ENGINE-1.0.md`, раздел 3).
WAIT_PLAN = "plan"

# Родители ещё не закрыты — дело ждёт не события своего журнала, а чужой работы.
# Такой `wait` движок возвращает до первого шага (`driver.run`), и он же
# закрывает дыру «диспетчер поднимает `todo`»: гейт родителей — не только статус
# строки, но и отказ начинать попытку.
WAIT_NEED = "need"
WAIT_KINDS = (WAIT_ATTEMPT, WAIT_QUESTION, WAIT_VERDICT, WAIT_FOR_GATE, WAIT_RETRY, WAIT_PARKED, WAIT_PLAN, WAIT_NEED)

# Исходы попытки, которые считаются успехом. Набор объявлен, а не угадан по
# подстроке: `contract_refused` тоже содержит «contract», и «похоже на успех»
# здесь недопустимо.
OK_OUTCOMES = ("ok", "done", "success", "passed", "accepted")

# Исход отменённой попытки (`attempt_ended.outcome`, `runner.OUTCOME_CANCELLED`):
# не успех и не отказ — решение лида. Класса отказа у такой попытки нет, и слой
# не выводит его из отсутствия: `unknown` сказал бы про работу то, чего не было.
CANCELLED_OUTCOME = "cancelled"

# Класс отказа, которого в событии нет. Это не догадка о причине, а честное имя
# отсутствия класса: такой отказ не ретраится (`retry.on` его не называет).
# Словарь классов один на плагин — он живёт в `dashboard/failures.py`.
UNCLASSIFIED = failures.UNCLASSIFIED

# Вердикт «дальше по графу»: так пишет спека и так пишет событие. Слово одно на
# плагин — им же движок заполняет `verdict.target` по умолчанию
# (`dashboard/runner.report_verdict`), а не своей копией.
NEXT = journal.VERDICT_NEXT

# Чем закрывается передача вопроса роли, когда ответа нет: `redirected` — роль
# переадресовала вопрос человеку (`authority: true`), `failed` — попытка ответа не
# удалась (в том числе потому, что движок ушёл между вопросом и ответом:
# `runner.recover_role_questions`). Вопрос при этом **жив** — он ушёл дальше, — и
# пустой `answer_len` у такой пары сроком не считается (`_expired`): иначе
# переадресованный вопрос читался бы как истёкший, и дело парковалось бы мимо
# человека, который этот вопрос и должен получить.
ROLE_ANSWER_REDIRECTED = "redirected"
ROLE_ANSWER_FAILED = "failed"
ROLE_ANSWER_KINDS = (ROLE_ANSWER_REDIRECTED, ROLE_ANSWER_FAILED)

# Виды событий, которые срез этапа не меняют. Набор — журнальный
# `NON_STATE_KINDS`, минус `question_answered` (для дела ответ закрывающий факт, а
# для этапа ответ — вход, правило 5) и минус `stage_cancelled` (для дела отмена
# тихая, а для этапа это единственный факт отменённого `pending`-этапа).
# Терминальность (правило 7) считается раньше этой ветки, поэтому поздний ответ
# закрытый этап не воскрешает.
QUIET_KINDS = tuple(
    kind
    for kind in journal.NON_STATE_KINDS
    if kind not in ("question_answered", "stage_cancelled")
)

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
    "stage_cancelled",
    journal.VERDICT_APPROVE,
    journal.VERDICT_CHANGES,
)

# Вердикты этапа: это решения графа, а не факты попытки, поэтому они
# применяются и к закрытому этапу (иначе приговор некуда было бы прочитать).
VERDICT_KINDS = (journal.VERDICT_APPROVE, journal.VERDICT_CHANGES)

# Этап-гейт (`dashboard/spec.py`, `GATE_KIND`): сам ничего не исполняет, а задаёт
# вопрос человеку. Слою вид нужен только для текста ожидания и причины парковки —
# переходы у гейта те же, что у любого этапа.
GATE_KIND = "gate"

# Факты плана (`docs/ENGINE-1.0.md`, раздел 3). Виды журнальные
# (`journal.EVENT_KINDS`): слой не заводит своих, он их читает.
PLAN_PROPOSED = "plan_proposed"
PLAN_ACCEPTED = "plan_accepted"
PLAN_REJECTED = "plan_rejected"
PLAN_RETHINK = "plan_rethink"
PLAN_GATE = "plan_gate"
PLAN_KINDS = (PLAN_PROPOSED, PLAN_ACCEPTED, PLAN_REJECTED, PLAN_RETHINK, PLAN_GATE)

# Факт отмены этапа (`journal.EVENT_KINDS`): лид отказался от работы
# (`duty_cancel`, `docs/ENGINE-2.0.md`, раздел 5). Отдельный вид, а не
# `attempt_ended`: у `pending`-этапа попытки нет, и отменять в нём нечего.
STAGE_CANCELLED = "stage_cancelled"

# id этапа, чей отчёт — предложение плана: так он назван в seed-графе 1.0
# (`docs/ENGINE-1.0.md`, раздел 3, `stages: - id: plan`). Слово берётся у спеки:
# писатель (`runner.report_plan`) и читатель (`graph`) не должны разойтись в том,
# чей это план.
PLAN_STAGE = spec_module.PLAN_STAGE
PLAN_STAGE_LEGACY = spec_module.PLAN_STAGE_LEGACY

APPROVE = "approve"
CHANGES = "changes"

# Сколько этапов движок ведёт одновременно (`_batch`, `runner.step`). Предел —
# здесь, рядом с остальными лимитами слоя, и он же назван в `docs/SPEC.md` и
# `docs/ENGINE-1.0.md`: больше двух попыток пачки — это два процесса модели, два
# дерева и два лизинга на один шаг, и остальные этапы ждут своей пачки.
MAX_PARALLEL = 2


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


def failed(kind: str, reason: str = "", stage_id: str = "") -> Decision:
    """Дело остановлено отказом: класс пришёл из события или это `contract`.

    `stage_id` называет остановивший этап, когда он известен: по нему движок
    отличает остановку ПЛАНОМ (репланы сверх хартии — её нечем снять, кроме
    человека) от приговора человеку («ответ вне `allow`» — решение уже принято,
    переспрашивать нечего).
    """
    return Decision(
        kind=DECISION_FAILED,
        fail_kind=str(kind),
        reason=reason,
        stages=(str(stage_id),) if stage_id else (),
    )


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
    # Граф дела этой свёртки: спека плюс этапы принятого плана (`graph`). Срез и
    # решение считаются по нему, а не по файлу: иначе добавленный этап был бы в
    # журнале, но не в графе (`docs/ENGINE-1.0.md`, раздел 3).
    spec: Any = None
    # Предложение плана, которое ещё не решено: `{rev, stage, stages}`. Пока оно
    # ждёт решения, дело не идёт дальше по seed-графу — иначе `close` с
    # `needs: [plan]` закрыл бы дело, план которого ещё не решён.
    plan_pending: Optional[dict[str, Any]] = None
    # Открытый гейт плана: `{rev, stage, question_id, reason}` — ждём человека.
    plan_gate: Optional[dict[str, Any]] = None
    # id этапа плана из последнего предложения (а не догадка по имени файла).
    plan_stage: str = ""
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
    # id дела из событий: нужен тексту решения («`duty decide <id>`»), и брать его
    # из журнала честнее, чем передавать вторым аргументом — событие знает своё дело.
    deal_id: str = ""

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


def _is_gate(spec: Any, stage_id: str) -> bool:
    """Этап-гейт (`kind: gate`)? Один предикат: по нему различаются причина
    парковки и текст ожидания, и второе написание этого сравнения разошлось бы."""
    return str(getattr(_stage_spec(spec, stage_id), "kind", "")) == GATE_KIND


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

    Пустой `answer_len` — срок только у ответа человека. Передача роли без ответа
    (`redirected`/`failed`) закрывает **передачу**, а не вопрос: он ушёл дальше, и
    читать его как истёкший значило бы парковать дело мимо живого вопроса.
    """
    via = str(payload.get("answered_via") or "").strip().lower()
    if via == "expired":
        return True
    if via in ROLE_ANSWER_KINDS:
        return False
    return _int(payload.get("answer_len")) == 0


def _gate_park_reason(spec: Any, stage_id: str, question_id: Optional[str]) -> str:
    """Почему гейт припаркован: человек не ответил за объявленный срок.

    Срок берётся из `budget.minutes` этапа — того же поля, по которому вопрос ушёл
    в пул (`dashboard/runner.py`). Поле не задано — срок брался по умолчанию
    (`ask.ASK_TIMEOUT_HOURS`), и причина говорит «отведённое время», а не
    выдуманное число: слою неоткуда взять чужую константу, не заведя вторую копию.
    """
    stage = _stage_spec(spec, stage_id)
    minutes = getattr(getattr(stage, "budget", None), "minutes", None)
    tail = f" (вопрос #{question_id} закрыт по сроку)" if question_id else ""
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0:
        return f"гейт: человек не ответил за {minutes} мин{tail}"
    return f"гейт: человек не ответил за отведённое время{tail}"


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
    if kind == journal.VERDICT_APPROVE:
        _verdict_approve(board, node, payload)
        return
    if kind == journal.VERDICT_CHANGES:
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
        if outcome == CANCELLED_OUTCOME:
            # Отмена — решение лида, а не отказ работы (`docs/ENGINE-2.0.md`,
            # раздел 5): класс отказа не выдумывается (`unknown` сказал бы про
            # работы то, чего не было), счётчик отказов этапа не растёт, повтора
            # нет. Восьмого состояния среза не заводится — набор закрыт
            # контрактом, — поэтому отменённый этап терминален и называет себя
            # словами: вернуть работу можно только новым этапом в новой ревизии
            # плана (перестановка того же id отмену не снимает).
            node.state = FAILED
            reason = str(payload.get("reason") or "").strip()
            node.reason = f"попытка {node.attempt}: отменена лидом" + (
                f": {reason}" if reason else ""
            )
            return
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

    if kind == "stage_cancelled":
        # Этап, у которого попытки не было: отменять в нём нечего, но и
        # запускать его больше нельзя (`duty_start` откажет). Состояние —
        # терминальное, причина — слова лида, а не выдуманный класс отказа.
        node.state = FAILED
        node.question_id = None
        node.verdict = None
        reason = str(payload.get("reason") or "").strip()
        node.reason = "этап отменён лидом" + (f": {reason}" if reason else "")
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
            # номеру вопроса, а не по тексту причины. У гейта причина своя: он не
            # «упал», а не дождался человека, и счётчика попыток у него нет.
            node.state = PARKED
            node.reason = (
                _gate_park_reason(spec, node.id, node.question_id)
                if _is_gate(spec, node.id)
                else f"вопрос #{node.question_id} закрыт по сроку без ответа"
            )
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


def _deal_question(board: _Board, spec: Any, payload: dict[str, Any]) -> None:
    """Вопрос **без** `stage`: адресовать его некому, кроме названного адреса.

    `ask()` пишет `question_asked` на уровне дела (`dashboard/ask.py`): вопросам
    он не знает этапа, и это правильно — этап знает движок. Движок и записывает
    его в `payload.routing` (`dashboard/runner.py`), поэтому адрес берётся оттуда,
    а `board.last_attempt` — запасной источник для вопросов, заданных мимо
    движка (`POST /ask`) и для фактов до W5. Без адресации этап, у которого
    попытка закончилась вопросом, не `done`, а `wait_human`: работа не сделана,
    её ждёт человек, и после ответа нужна **новая** попытка (правило 5).

    Вопрос без `question_id` — вопрос **роли** (`dashboard/runner.ask_role`):
    строки пула у него нет, поэтому пара «вопрос → ответ» держится адресом
    (`routing.stage`/`routing.attempt`), а не номером. Ожидание у него то же
    (`wait_human`): пока роль отвечает, работа не сделана, и это сказано
    адресатом, а не пустым номером.
    """
    question_id = _question_id(payload)
    stage_id, attempt = _addressed(board, payload)
    board.question = (stage_id, question_id or "")
    node = board.stages.get(stage_id)
    if node is None:
        return
    if question_id is not None:
        node.question_id = question_id
    node.attempt = max(node.attempt, attempt)
    if node.state in (FAILED, PARKED) or board.parked is not None:
        # Вопрос запаркованного дела — факт парковки, а не повод поднять этап:
        # исчерпанный этап остаётся `failed`, а дело — ждёт решения человека.
        return
    node.state = WAIT_HUMAN
    if question_id is None:
        node.reason = f"вопрос ушёл {_address_of(payload) or 'адресату'}: ответа нет"
        return
    node.reason = (
        f"гейт: вопрос #{question_id} человеку — ждёт ответа"
        if _is_gate(spec, stage_id)
        else f"попытка {node.attempt} закончилась вопросом #{question_id}"
    )


def _address_of(payload: dict[str, Any]) -> str:
    """Адрес вопроса из `payload.routing` — для причины ожидания; пусто — не назван."""
    routing_ = payload.get("routing")
    if not isinstance(routing_, dict):
        return ""
    return str(routing_.get("target") or "")


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


def _deal_answer(board: _Board, spec: Any, payload: dict[str, Any]) -> None:
    """Ответ или срок по вопросу дела: новая попытка того же этапа либо `park`.

    Пара «вопрос → ответ» сверяется номером пула, а у вопроса роли номера нет —
    тогда её держит адрес (вопрос без `question_id` отвечается ответом без
    `question_id`). Смешивать нельзя: у вопроса с номером ответ без номера —
    ответ **не** на него, и наоборот.
    """
    stage_id, asked = board.question
    question_id = _question_id(payload)
    if asked != (question_id or ""):
        return  # ответ на чужой/уже снятый вопрос: снимать нечего
    board.question = ("", "")
    node = board.stages.get(stage_id)
    if node is None or node.state in TERMINAL_STATES:
        return
    number = f"#{asked}" if asked else ""
    if _expired(payload):
        node.state = PARKED
        node.reason = (
            _gate_park_reason(spec, stage_id, asked)
            if _is_gate(spec, stage_id)
            else f"вопрос {number or '—'} закрыт по сроку без ответа"
        )
        return
    node.question_id = None
    if board.parked is not None:
        # Парковка: ответ на вопрос — ещё не решение продолжать. Подъём только
        # явным `unblocked` (`continue`), иначе дело крутилось бы само.
        node.reason = f"ответ {number or '—'}: дело запарковано — ждёт решения человека"
        return
    node.state = PENDING
    # У гейта следующая передача не «новая попытка процесса», а суд над ответом:
    # ответ человека — вход этапа, и это сказано словами, а не номером попытки.
    # У ответа роли номер пула пуст, и её называет автор (`by`): «ответ роли lead».
    by = str(payload.get("by") or "").strip()
    who = f"роли {by}" if by else (number or "—")
    node.reason = (
        f"гейт: ответ {who} получен — этап судит его по allow"
        if _is_gate(spec, stage_id) and asked
        else f"ответ {who}: новая попытка {node.attempt + 1}"
    )


def _park_answered(board: _Board, payload: dict[str, Any]) -> None:
    """Запомнить, что ответ на вопрос парковки получен (а решения — ещё нет).

    Считается до разбора этапа: этап запаркованного дела обычно `failed`, а это
    терминальное состояние, и до ветки ответа разбор не дошёл бы. Сравнение по
    `question_id` то же, что у остальных ответов: чужой ответ парковку не трогает,
    а истёкший срок ответом не считается (`_expired`) — человек ничего не решал.
    """
    parked = board.parked
    if parked is None or _expired(payload):
        return
    asked = str(parked.get("question_id") or "")
    answered = _question_id(payload)
    if asked and answered and asked != answered:
        return
    parked["answered"] = True


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
    """Факты парковки из `blocked`: класс, этап, сколько попыток, лимит и вопрос.

    `gate` — парковка гейта (`dashboard/runner.park_gate`): у неё нет ни класса
    отказа, ни счётчика попыток — человек просто не ответил, и «0/0 unknown»
    выдавало бы отсутствие класса за класс.
    """
    return {
        "kind": str(payload.get("kind") or payload.get("fail_kind") or UNCLASSIFIED),
        "stage": str(payload.get("stage") or ""),
        "attempts": _int(payload.get("attempts")),
        "limit": _int(payload.get("limit")),
        "reason": str(payload.get("park_reason") or payload.get("reason") or ""),
        "question_id": _question_id(payload),
        "gate": bool(payload.get("gate")),
        # Ответ человека по вопросу парковки: `False` до `question_answered`.
        "answered": False,
    }


# ------------------------------------------------------------------ граф дела


class _Graph:
    """Спека плюс этапы принятого плана: тот же набор полей, другой граф.

    Заменяет спеку в свёртке и проекциях: слой не должен знать, пришёл этап из
    файла или из `plan_accepted` — правила перехода у него одни. Всё, чего у
    графа нет (`charter`, `path`, `base`, `budget`), делегируется спеке.
    """

    def __init__(self, spec: Any, stages: Iterable[Any]) -> None:
        self.spec = spec
        self.stages = tuple(stages)
        self.by_id = {stage.id: stage for stage in self.stages}

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(stage.id for stage in self.stages)

    def stage(self, stage_id: str) -> Any:
        return self.by_id.get(stage_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.spec, name)


def graph_spec(spec: Any, events: Iterable[Any]) -> Any:
    """Спека как граф дела: без плана — сама спека, с планом — `_Graph` над ней.

    Один вход для движка (`runner`) и поверхностей (`stagefeed`): попытка этапа,
    добавленного планом, обязана найти свой этап тем же графом, которым его
    запустило решение, — `spec.stage(id)` из файла его не знает.
    """
    stages = graph(spec, events)
    if tuple(getattr(spec, "stages", ()) or ()) == stages:
        return spec
    return _Graph(spec, stages)


def started_stages(events: Iterable[Any]) -> frozenset[str]:
    """id этапов, у которых была попытка: их план не переставляет.

    Перестановка — про порядок будущей работы (`docs/ENGINE-1.0.md`, раздел 4):
    этап, который уже начинали, переставляет не план, а приговор ревью
    (`verdict.changes`) — иначе лид возвращал бы сделанное назад в обход ревью.
    Факт попытки — `attempt_started` (и `attempt_ended` на случай журнала, где
    первую половину пары не писали): обе строки говорят «этап трогали».
    """
    found: set[str] = set()
    for event in _ordered(events):
        if str(event.get("kind") or "") not in ("attempt_started", "attempt_ended"):
            continue
        payload = _payload(event)
        stage_id = payload.get("stage")
        if isinstance(stage_id, str) and stage_id:
            found.add(stage_id)
    return frozenset(found)


def graph(spec: Any, events: Iterable[Any]) -> tuple[Any, ...]:
    """Граф дела: seed спеки плюс этапы последней принятой ревизии плана.

    Файл спеки — seed, а не весь граф (`docs/ENGINE-1.0.md`, раздел 3): принятый
    план живёт фактами журнала, поэтому граф — чистая функция тех же событий, что
    и состояние. Журнал без фактов плана даёт ровно `spec.stages` — поведение
    0.0.8 не меняется ни на факт.

    Этапы берутся из `plan_proposed` той ревизии, которую назвал `plan_accepted`:
    отдельной копии плана в событии принятия нет — иначе две копии одного графа
    разошлись бы. Разбор — тем же `spec.parse_plan` по хартии: граф, который
    валидатор отверг бы, не становится графом и при чтении (мусорный журнал не
    роняет дело, правило 8 — граф остаётся seed-ом).

    Ревизия несёт два вида записи: **добавление** (новый этап) и **перестановку**
    (этап, который уже есть в графе, с новыми `needs`/`parallel_with`). Второе
    проверяется без `started_ids`: «этап уже начат» — отказ момента предложения
    (`runner.report_plan` знает журнал), а принятая перестановка остаётся графом и
    после того, как её этап начали, — иначе граф сжался бы задним числом.
    """
    seed = tuple(getattr(spec, "stages", ()) or ())
    proposals: dict[int, Any] = {}
    accepted = 0
    for event in _ordered(events):
        kind = str(event.get("kind") or "")
        if kind not in (PLAN_PROPOSED, PLAN_ACCEPTED):
            continue
        payload = _payload(event)
        stage_id = payload.get("stage")
        if isinstance(stage_id, str) and stage_id:
            continue  # факт этапа, а не факт плана (смотри `_fold`)
        rev = _int(payload.get("rev"))
        if rev <= 0:
            continue
        if kind == PLAN_PROPOSED:
            stages = payload.get("stages")
            if isinstance(stages, list):
                proposals[rev] = payload
        elif rev > accepted:
            accepted = rev
    if accepted <= 0 or accepted not in proposals:
        return seed
    payload = proposals[accepted]
    charter = getattr(spec, "charter", None) or spec_module.Charter()
    stages, issues = spec_module.parse_plan(
        payload.get("stages"),
        charter=charter,
        base_ids=[stage.id for stage in seed],
        base_needs={stage.id: stage.needs for stage in seed},
        repo=getattr(spec, "repo", None),
    )
    if issues:
        return seed
    plan_stage = str(payload.get("source_stage") or "") or PLAN_STAGE
    return _relinked(seed, tuple(stages), plan_stage)


def _relinked(seed: tuple[Any, ...], proposed: tuple[Any, ...], plan_stage: str) -> tuple[Any, ...]:
    """Seed плюс план: перестановки применены, этапы seed ждут «последние» из плана.

    Запись предложения с `id` из seed — перестановка, а не новый этап: у неё
    берутся только `needs`/`parallel_with`, остальные поля этапа остаются
    спекой (`spec.parse_plan`, `REORDER_KEYS`).

    Без этого рёбра seed, ждавшие план, стали бы готовы сразу после принятия
    плана и закрыли бы дело до работы, которую план объявил
    (`docs/ENGINE-1.0.md`, раздел 9: plan → research → … → close). Рёбра не
    додумываются: этап seed получает ровно те добавленные этапы, от которых
    никто из добавленных не зависит, — «последние из плана».
    """
    seeded = {stage.id for stage in seed}
    moving = {stage.id: stage for stage in proposed if stage.id in seeded}
    added = tuple(stage for stage in proposed if stage.id not in seeded)
    ordered: list[Any] = []
    for stage in seed:
        moved = moving.get(stage.id)
        if moved is None:
            ordered.append(stage)
            continue
        ordered.append(dataclasses.replace(stage, needs=moved.needs, parallel_with=moved.parallel_with))
    if not added:
        return tuple(ordered)
    needed = {need for stage in added for need in stage.needs}
    tail = tuple(stage.id for stage in added if stage.id not in needed)
    by_id = {stage.id: stage for stage in added}
    out: list[Any] = []
    for stage in ordered:
        if plan_stage not in tuple(getattr(stage, "needs", ()) or ()):
            out.append(stage)
            continue
        # Параллель, объявленная планом, не превращается в ожидание: этап, который
        # идёт «вместе с» этим, не может быть его предшественником — иначе
        # объявление параллели давало бы ровно обратное.
        extra = tuple(stage_id for stage_id in tail if not _parallel(stage, by_id[stage_id]))
        out.append(dataclasses.replace(stage, needs=tuple(dict.fromkeys((*stage.needs, *extra)))))
    return tuple(out) + added


def _plan_stage_of(board: _Board) -> str:
    """id этапа плана: названный предложением, иначе координатор из seed-графа.

    Старые seed-графы называют вход `plan` (`PLAN_STAGE_LEGACY`): ищем новое имя,
    потом прежнее — журналы дел до переименования читаются дальше.
    """
    if board.plan_stage:
        return board.plan_stage
    for name in (PLAN_STAGE, *PLAN_STAGE_LEGACY):
        if name in board.stages:
            return name
    return ""


def _plan_wait(board: _Board) -> str:
    """Почему дело ждёт решения по плану; пустая строка — не ждёт.

    Три состояния, и все три — про граф, а не про попытку: гейт плана ждёт
    человека, решённое предложение ждёт проверки (пишет движок), а идущая
    попытка этапа плана ещё ничего не предложила. Пока это так, по seed-графу
    идти нельзя: следующий этап получил бы граф, которого ещё нет.
    """
    gate = board.plan_gate
    if gate is not None:
        return (
            "план ревизии {} ждёт человека: вопрос #{}".format(
                gate.get("rev") or "—", gate.get("question_id") or "—"
            )
        )
    pending = board.plan_pending
    if pending is not None:
        stages = pending.get("stages") or []
        named = ", ".join(str(item.get("id") or "—") for item in stages if isinstance(item, dict))
        return "план ревизии {} предложен и ждёт решения: этапы {}".format(
            pending.get("rev") or "—", named or "—"
        )
    stage_id = _plan_stage_of(board)
    node = board.stages.get(stage_id) if stage_id else None
    if node is None or node.state != RUNNING:
        return ""
    return f"этап плана {stage_id!r}: попытка {node.attempt} идёт — план ещё не предложен"


def _plan_fact(board: _Board, spec: Any, kind: str, payload: dict[str, Any], at: int) -> None:
    """Факт плана дела: предложение, решение по нему и гейт плана.

    `plan_proposed` граф не меняет (`journal.NON_STATE_KINDS`) — граф уже собран
    `graph()` по последней принятой ревизии. Меняют его `plan_accepted` (граф) и
    `plan_rejected` (раунд реплана этапа плана); `plan_gate` ставит этап плана в
    `wait_human` тем же фактом, что и вопрос гейт-этапа, — переходы у гейта плана
    те же, что у гейта графа.
    """
    if kind == PLAN_PROPOSED:
        rev = _int(payload.get("rev"))
        stages = payload.get("stages") if isinstance(payload.get("stages"), list) else []
        if rev <= 0 or not stages:
            return  # мусор в журнале не держит дело: предложения без ревизии нет
        board.plan_pending = {"rev": rev, "stage": str(payload.get("source_stage") or ""), "stages": stages}
        if board.plan_pending["stage"]:
            board.plan_stage = board.plan_pending["stage"]
        return
    rev = _int(payload.get("rev"))
    node = board.stages.get(_plan_stage_of(board))
    pending = board.plan_pending
    if pending is not None and (not rev or rev == pending.get("rev")):
        board.plan_pending = None
    if kind == PLAN_ACCEPTED:
        # Принятие закрывает этап плана: он «предложил план и закрылся им». Ждать
        # больше нечего (гейт снят), а `question_answered` до этого мог вернуть
        # этап в `pending` — вторая попытка плана при принятой ревизии не нужна.
        board.plan_gate = None
        if node is not None and node.state in (WAIT_HUMAN, PENDING):
            node.state = DONE
            node.question_id = None
            node.reason = f"план ревизии {rev or '—'} принят"
        return
    if kind == PLAN_GATE:
        question_id = _question_id(payload)
        board.plan_gate = {
            "rev": rev,
            "stage": _plan_stage_of(board),
            "question_id": question_id,
            "reason": str(payload.get("reason") or ""),
        }
        if node is not None and node.state not in TERMINAL_STATES:
            node.question_id = question_id
            node.state = WAIT_HUMAN
            node.since = at or node.since
            node.reason = f"план ревизии {rev or '—'} ждёт человека" + (
                f": вопрос #{question_id}" if question_id else ""
            )
        return
    # `plan_rejected`/`plan_rethink`: этап плана возвращается в работу с причиной,
    # и сверх `max_rounds_replan` дело останавливается — как у `verdict.max_rounds`,
    # иначе реплан превратился бы в бесконечный генератор бюджета. Отказ — реплан
    # по ошибкам формы; переосмысление — движок вернул дело лиду после исчерпания
    # попыток этапа: лид сначала разбирается в причине (диагноз в payload), и
    # ревизия плана — лишь один из исходов. Счётчик раундов общий: и отказы, и
    # переосмысления жгут ресурс лида.
    board.plan_gate = None
    if node is None:
        return
    node.rounds += 1
    charter = getattr(spec, "charter", None)
    max_rounds = _int(getattr(charter, "max_rounds_replan", 0))
    why = str(payload.get("why") or "").strip()
    if kind == PLAN_REJECTED:
        if not why:
            why = "план отклонён"
        issues = [str(item).strip() for item in payload.get("issues") or () if str(item).strip()]
        text = why + (f": {'; '.join(issues)}" if issues else "")
    else:
        # Диагноз едет в payload (`why` — причина последнего отказа,
        # `failed_stage` — упавший этап; у старых фактов диагноз лежал в
        # `stage`): без него лид начал бы с догадок, а не с фактов.
        stage_id = str(payload.get("failed_stage") or payload.get("stage") or "")
        fail_kind = str(payload.get("kind") or "")
        if not why:
            why = f"этап {stage_id or '?'} исчерпал попытки ({fail_kind or 'unknown'})"
        text = f"переосмысление: {why}"
    if max_rounds and node.rounds > max_rounds:
        node.state = FAILED
        node.fail_kind = "contract"
        node.reason = f"репланов {node.rounds} больше max_rounds_replan={max_rounds}: {text}"
        return
    node.state = PENDING
    node.question_id = None
    node.fail_kind = ""
    node.failed_at = 0
    node.reason = text if not max_rounds else f"{text} — реплан {node.rounds} из {max_rounds}"
    if kind == PLAN_RETHINK:
        # Упавший этап возвращается в работу вместе с планом: иначе срез
        # (`next_stage`) видел бы его FAILED раньше «план решается» и навсегда
        # отвечал `failed` — переосмысление не доходило бы до лида (кейс
        # d_c8551c18: review исчерпал попытки, движок вернул дело лиду, а
        # решение оставалось «дело упало»). Счётчики попыток не трогаются:
        # их пересматривает новая ревизия плана, не фолд.
        rethink_stage = str(payload.get("failed_stage") or payload.get("stage") or "")
        fallen = board.stages.get(rethink_stage) if rethink_stage else None
        if fallen is not None and fallen.state == FAILED:
            fallen.state = PENDING
            fallen.fail_kind = ""
            fallen.failed_at = 0
            fallen.question_id = None
            fallen.reason = f"переосмысление: этап ждёт решения лида ({why})"


def _fold(spec: Any, events: Iterable[Any]) -> _Board:
    """Свернуть события в состояние этапов и закрытость дела.

    Чистая функция: только вход. Чужие **этапы** и неизвестные виды пропускаются
    молча — журнал общий, и дело не должно падать от факта соседа (правило 8).
    Отбор по `deal_id` слой не делает: события уже отобраны по делу читателем
    (`journal.read_events(conn, deal_id)`), а признак принадлежности этапу —
    `stage` в `payload`, и второй фильтр здесь молча выкинул бы факт, если бы
    вызывающий передал события шире.

    Граф берётся из `graph(spec, events)`, а не из `spec.stages`: принятый план
    живёт фактами журнала, и свёртка обязана видеть те же этапы, что и решение.
    """
    listed = [event for event in events or () if isinstance(event, dict)]
    view = graph_spec(spec, listed)
    board = _Board(stages={stage.id: _Stage(id=stage.id) for stage in view.stages}, spec=view)
    known = set(board.stages)
    for event in _ordered(listed):
        kind = str(event.get("kind") or "")
        payload = _payload(event)
        if not board.deal_id:
            board.deal_id = str(event.get("deal_id") or "")
        stage_id = payload.get("stage")
        scoped = isinstance(stage_id, str) and bool(stage_id)
        # Факт плана — факт **дела**: `stage` в его payload не пишется (адрес
        # предложения — `source_stage`). Событие с `stage` — факт этапа, даже если
        # вид назван плановым: чужой/мусорный журнал не должен держать дело
        # ожиданием плана (правило 8). Исключение — старые `plan_rethink`: их
        # `stage` был диагнозом, а не адресом (кейс d_c8551c18), новый формат
        # пишет `failed_stage`.
        if kind in PLAN_KINDS and (not scoped or kind == PLAN_RETHINK):
            _plan_fact(board, view, kind, payload, _int(event.get("ts")))
            continue
        if kind == "question_answered":
            _park_answered(board, payload)
        if kind in ("blocked", "unblocked"):
            # Парковка — факт **дела**, даже когда в payload назван этап: `blocked`
            # с `stage` не делает блок фактом этапа (`stage` в журнале блокировки —
            # адрес отказа, а не принадлежность событию).
            _park_fact(board, kind, payload)
            continue
        if not scoped:
            if kind == "completed":
                board.closed = True
                board.closed_reason = "журнал: дело закрыто событием completed"
            elif kind == "question_asked":
                _deal_question(board, view, payload)
            elif kind == "question_answered":
                _deal_answer(board, view, payload)
            continue
        if stage_id not in known or kind not in STAGE_KINDS:
            continue
        _stage_event(view, board, board.stages[stage_id], kind, payload, _int(event.get("ts")))
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

    Размер пачки ограничен `MAX_PARALLEL`: остальные объявленные параллельными
    этапы ждут своей пачки и попадут в следующий шаг, когда место освободится.
    """
    picked: list[Any] = []
    for stage in runnable:
        if len(picked) >= MAX_PARALLEL:
            break
        if all(_parallel(stage, other) for other in picked):
            picked.append(stage)
    return tuple(stage.id for stage in picked)


# --------------------------------------------------------------- проекции


def stage_states(spec: Any, events: Iterable[Any]) -> dict[str, StageState]:
    """Срез этапов: что с каждым прямо сейчас. Порядок — граф дела.

    Состояние, номер попытки, раунды возвратов, открытый вопрос, класс последнего
    отказа и причина. Источник — только события; ни БД, ни процессов. Граф —
    из журнала (`graph`): этапы, добавленные принятым планом, в срезе есть, и
    реплей на том же журнале даёт тот же срез целиком, а не по полям.
    """
    board = _fold(spec, events)
    view = board.spec
    states = _slice(view, board)
    result: dict[str, StageState] = {}
    for stage in view.stages:
        node = board.stages[stage.id]
        state = states[stage.id]
        reason = (
            node.reason if state == node.state else _gate_reason(view, board, stage) or node.reason
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

    Перед пунктом 5 стоит один: пока решается план дела (`wait(plan)`), по
    seed-графу идти нельзя — иначе следующий этап получил бы граф, которого ещё
    нет (`docs/ENGINE-1.0.md`, раздел 3).

    `now` нужен тексту причины («попытка идёт 12мин») и решению о паузе перед
    повтором: до `retry_at` повтор не выдаётся. Часов модуль не читает.
    """
    board = _fold(spec, events)
    view = board.spec
    seconds = _int(now)
    if board.closed:
        return done(board.closed_reason)
    if not view.stages:
        return done("в спеке нет этапов: решать нечего")

    if board.parked is not None:
        parked = board.parked
        if parked.get("gate"):
            # Парковка гейта: ни класса, ни счётчика — человек не ответил вовремя.
            return wait(
                WAIT_PARKED,
                "дело запарковано: {}; продолжение — только решением человека".format(
                    parked["reason"] or "гейт: человек не ответил"
                ),
            )
        # Ответ получен, а решение — нет: без этой половины фразы («продолжение —
        # только решением человека») не видно, что ответ уже есть и чего не хватает.
        tail = (
            "; ответ #{} получен — нужно решение: duty decide {} continue|stop".format(
                parked["question_id"], board.deal_id or "<id>"
            )
            if parked.get("answered")
            else "; продолжение — только решением человека"
        )
        return wait(
            WAIT_PARKED,
            "дело запарковано: {} на этапе {!r} — {}".format(
                failures.counter(parked["kind"], parked["attempts"], parked["limit"]),
                parked["stage"] or "—",
                parked["reason"] or "попытки исчерпаны",
            )
            + (f"; вопрос #{parked['question_id']}" if parked["question_id"] else "")
            + tail,
        )

    # Срез — по графу журнала (`view`), а не по файлу: этап, добавленный принятым
    # планом, обязан быть в `states`, иначе `done` наступал бы, пока он не закрыт.
    states = _slice(view, board)

    stopped = _first(view, states, FAILED)
    if stopped is not None:
        node = board.stages[stopped.id]
        return failed(
            node.fail_kind or UNCLASSIFIED, f"этап {stopped.id!r}: {node.reason}", stopped.id
        )

    parked = _first(view, states, PARKED)
    if parked is not None:
        node = board.stages[parked.id]
        return park(parked.id, node.question_id, f"этап {parked.id!r}: {node.reason}")

    # Вопрос дела, которому не нашлось этапа: адрес неизвестен, но человек его
    # видит и ждёт ответа — запускать поверх открытого вопроса нельзя.
    if board.question[1] and not board.question[0]:
        return wait(WAIT_QUESTION, f"вопрос #{board.question[1]} без адресата в спеке: ответа нет")

    # План дела решается — по seed-графу идти рано: «готов» этап, чей граф ещё
    # не принят (`docs/ENGINE-1.0.md`, раздел 4).
    plan_why = _plan_wait(board)
    if plan_why:
        return wait(WAIT_PLAN, plan_why)

    runnable = _runnable(view, board, seconds)
    if runnable:
        batch = _batch(view, runnable)
        numbers = tuple(board.stages[stage_id].attempt + 1 for stage_id in batch)
        return run(batch, numbers, f"готовы: {', '.join(batch)}")

    running = _first(view, states, RUNNING)
    if running is not None:
        node = board.stages[running.id]
        return wait(
            WAIT_ATTEMPT,
            f"этап {running.id!r}: попытка {node.attempt} идёт, {_age(node.since, seconds)}",
        )

    question = _first(view, states, WAIT_HUMAN)
    if question is not None:
        node = board.stages[question.id]
        if _is_gate(view, question.id):
            # Гейт: у ожидания есть варианты ответа, и они объявлены спекой —
            # человеку не нужно открывать панель, чтобы вспомнить, из чего выбирать.
            stage = _stage_spec(view, question.id)
            options = ", ".join(getattr(getattr(stage, "ask", None), "options", ()) or ()) or "—"
            return wait(
                WAIT_QUESTION,
                f"гейт {question.id!r}: вопрос #{node.question_id or '—'} без ответа, "
                f"варианты: {options}, {_age(node.since, seconds)}",
            )
        return wait(
            WAIT_QUESTION,
            f"этап {question.id!r}: вопрос #{node.question_id or '—'} без ответа, "
            f"{_age(node.since, seconds)}",
        )

    waiting_retry = _waiting_retry(view, board, seconds)
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

    gated = next(stage for stage in view.stages if states[stage.id] != DONE)
    what, why = _gate(view, board, gated)
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

    Решаемый план — это ожидание, а не работа: гейт плана ждёт человека
    (`wait_human`), нерешённое предложение — графа (`wait_gate`). Без этих двух
    веток дело с предложенным, но не принятым планом выглядело бы `pending`,
    хотя запускать по seed-графу нельзя.
    """
    board = _fold(spec, events)
    view = board.spec
    if board.closed:
        return DONE
    if not view.stages:
        return DONE
    if board.parked is not None:
        return WAIT_HUMAN
    states = _slice(view, board)
    for wanted in (FAILED, PARKED):
        if _first(view, states, wanted) is not None:
            return wanted
    if board.question[1] and not board.question[0]:
        return WAIT_HUMAN  # вопрос дела без адресата: человек всё равно решает
    for wanted in (WAIT_HUMAN, RUNNING):
        if _first(view, states, wanted) is not None:
            return wanted
    if board.plan_pending is not None:
        return WAIT_GATE
    if all(state == DONE for state in states.values()):
        return DONE
    if _first(view, states, PENDING) is not None:
        return PENDING
    return WAIT_GATE
