"""Слой перехода: `next(spec, events)` — по тесту на каждое правило (W2).

Проверяется не «функция что-то вернула», а **что именно** она решила: исход
(`Decision.kind`), этап и номер попытки, состояние среза и слово о деле. Тест
держит контракт `dashboard/transit.py` (докстринг модуля) и `docs/SPEC.md`:
каждое правило перехода — отдельный тест, каждое правило — ответ, который можно
проверить без модели, процессов и часов.

Спека читается настоящим загрузчиком (`load_spec` из `dashboard/spec.py`), а не
собирается объектами: слой берёт из спеки граф `needs`, `parallel_with`,
`verdict` и `retry`, и тест должен видеть тот же объект, что увидит движок.
События строятся в форме журнала (`kind`, `payload`, `ts`, `id`) — как их отдаёт
`journal.read_events`.

`now` везде передаётся аргументом: часы модуль не читает (это проверяется
отдельно), поэтому прогон воспроизводим до последней буквы.
"""

from __future__ import annotations

import copy
import importlib.util
import random
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути: пакета нет, грузим так же, как это делает движок."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec_module = load_module("hermes_duty_spec", "spec.py")
transit = load_module("hermes_duty_transit", "transit.py")

NOW = 1793900000
DEAL = "d_probe"

# Граф из спеки-источника: impl → verify → review → (publish ∥ docs).
# `retry` у impl и `verdict` у review — те поля, ради которых слой и существует.
GRAPH = """
duty: probe
stages:
  - id: impl
    role: implementer
    retry: {max: 2, on: [infra, contract]}
  - id: verify
    role: tester
    needs: [impl]
  - id: review
    role: reviewer
    needs: [verify]
    verdict: {approve: next, changes: impl, max_rounds: 2}
  - id: publish
    role: publisher
    needs: [review]
    parallel_with: [docs]
  - id: docs
    role: documenter
    needs: [review]
    parallel_with: [publish]
"""

# Параллельные этапы объявлены в обратном порядке: порядок запуска обязан идти
# по спеке (docs первым), а не по алфавиту и не по порядку зависимостей.
REVERSED = """
duty: probe
stages:
  - id: impl
    role: implementer
  - id: review
    role: reviewer
    needs: [impl]
    verdict: {approve: next, changes: impl, max_rounds: 2}
  - id: docs
    role: documenter
    needs: [review]
    parallel_with: [publish]
  - id: publish
    role: publisher
    needs: [review]
    parallel_with: [docs]
"""

# Две ветви без `parallel_with`: одновременно идти разрешено только объявленное.
UNMARKED = """
duty: probe
stages:
  - id: impl
    role: implementer
  - id: left
    role: implementer
    needs: [impl]
  - id: right
    role: tester
    needs: [impl]
"""


@pytest.fixture()
def spec(tmp_path: Path) -> Any:
    """Граф из спеки-источника, прочитанный настоящим загрузчиком."""
    return write_spec(tmp_path, GRAPH)


def write_spec(tmp_path: Path, text: str, name: str = "duty.yaml") -> Any:
    """Спека на диске (путь абсолютный, как требует `load_spec`) → объект спеки."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


# --------------------------------------------------------------- события


_clock = [0]


def ev(
    kind_name: str, stage: str | None = None, ts: int | None = None, **payload: Any
) -> dict[str, Any]:
    """Одно событие в форме журнала: `id` и `ts` растут, как в `duty.db`.

    Первый аргумент назван не `kind`, потому что в payload у отказа тоже есть
    `kind` (класс): одно имя на два смысла — это ровно та склейка, из-за которой
    классификация начинает выводиться из текста ошибки.
    """
    _clock[0] += 1
    number = _clock[0]
    body: dict[str, Any] = dict(payload)
    if stage is not None:
        body["stage"] = stage
    return {
        "id": number,
        "ts": 1793800000 + number if ts is None else ts,
        "deal_id": DEAL,
        "board": "duty",
        "kind": kind_name,
        "payload": body,
        "source": "test",
        "idempotency_key": f"test:{number}",
    }


def started(stage: str, attempt: int = 1) -> dict[str, Any]:
    """Попытка этапа началась (процесс поднят)."""
    return ev("attempt_started", stage, attempt=attempt, run_id=f"{stage}:{attempt}", pid=4242)


def ok(stage: str, attempt: int = 1) -> dict[str, Any]:
    """Попытка закрылась успехом: этап `done` (так пишет движок — исход полем)."""
    return ev("attempt_ended", stage, attempt=attempt, outcome="done", kind=None, exit=0)


def crashed(stage: str, attempt: int = 1, kind: str | None = None) -> dict[str, Any]:
    """Провал попытки: `kind` приходит готовым, если он известен движку."""
    body: dict[str, Any] = {"attempt": attempt, "outcome": "failed", "reason": "не сошлось"}
    if kind is not None:
        body["kind"] = kind
    return ev("attempt_ended", stage, **body)


def asked(stage: str, question_id: int, attempt: int = 1) -> dict[str, Any]:
    """Этап задал вопрос человеку: попытка приостановлена."""
    return ev("question_asked", stage, attempt=attempt, question_id=question_id)


def answered(stage: str, question_id: int, answer_len: int = 12, via: str = "pool") -> dict[str, Any]:
    """Вопрос этапа закрыт: ответом (`pool`) или сроком (`expired`)."""
    return ev(
        "question_answered",
        stage,
        question_id=question_id,
        answer_len=answer_len,
        answered_via=via,
    )


def deal_asked(question_id: int) -> dict[str, Any]:
    """Вопрос дела — так его пишет `ask()`: без `stage`, адресата знает движок."""
    return ev("question_asked", question_id=question_id, session_key="t_d_probe")


def deal_answered(question_id: int, answer_len: int = 40, via: str = "pool") -> dict[str, Any]:
    """Ответ (или срок) по вопросу дела — тоже без `stage`."""
    return ev(
        "question_answered",
        question_id=question_id,
        answer_len=answer_len,
        answered_via=via,
    )


def approve(stage: str, target: str = "next") -> dict[str, Any]:
    """Приговор ревью: `approve` в `target` (или дальше по графу)."""
    return ev("collected", stage, target=target, verdict="collected")


def changes(stage: str, target: str = "impl") -> dict[str, Any]:
    """Приговор ревью: возврат на `target`."""
    return ev("changes_requested", stage, target=target, reason="нет доказательств")


def declare(*groups: Any) -> list[dict[str, Any]]:
    """Склеить события в один журнал в порядке объявления."""
    out: list[dict[str, Any]] = []
    for group in groups:
        out.extend(group if isinstance(group, list) else [group])
    return out


def passed(*stages: str, attempt: int = 1) -> list[dict[str, Any]]:
    """Этапы пройдены подряд: начата и закрыта попытка каждого."""
    out: list[dict[str, Any]] = []
    for stage in stages:
        out.extend([started(stage, attempt), ok(stage, attempt)])
    return out


# ------------------------------------------------------------------ правило 1


def test_empty_journal_starts_entry_stage(spec: Any) -> None:
    """Пустой журнал — не ошибка: дело начинается со входа (правило 1, 8)."""
    decision = transit.next_stage(spec, [], NOW)

    assert decision.kind == "run"
    assert decision.pair == [("impl", 1)]
    assert transit.deal_state(spec, []) == "pending"
    states = transit.stage_states(spec, [])
    assert states["impl"].state == "pending"
    assert [states[stage].state for stage in ("verify", "review", "publish", "docs")] == [
        "wait_gate",
        "wait_gate",
        "wait_gate",
        "wait_gate",
    ]
    assert states["verify"].needs == ("impl",)


def test_linear_chain_walks_through_stages(spec: Any) -> None:
    """Каждый закрытый этап открывает следующий ровно один (правило 1)."""
    journal = passed("impl")
    assert transit.next_stage(spec, journal, NOW).pair == [("verify", 1)]

    journal += passed("verify", attempt=1)
    assert transit.next_stage(spec, journal, NOW).pair == [("review", 1)]

    journal += passed("review", attempt=1)
    # review закрыт, но приговора ещё нет: дальше никого не пускаем (правило 2).
    assert transit.next_stage(spec, journal, NOW).kind == "wait"


def test_run_decision_is_one_shape_for_one_and_many(spec: Any) -> None:
    """`run` несёт кортеж этапов и кортеж попыток: одна форма, не `run_many`."""
    decision = transit.next_stage(spec, [], NOW)

    assert decision.stages == ("impl",)
    assert decision.attempts == (1,)
    assert decision.stage == "impl"
    assert decision.attempt == 1
    assert str(decision) == "run(impl#1)"


# ------------------------------------------------------------------ правило 2


def test_successor_waits_for_verdict(spec: Any) -> None:
    """Закрытый ревью без приговора держит публикацию (правило 2)."""
    journal = passed("impl", "verify", "review")

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "wait"
    assert decision.what == "verdict"  # решает человек-ревьюер, а не граф
    states = transit.stage_states(spec, journal)
    assert states["review"].state == "done"
    assert states["publish"].state == "wait_gate"
    assert "приговора" in states["publish"].reason


def test_approve_next_runs_parallel_pair_in_declaration_order(spec: Any) -> None:
    """`approve: next` открывает обе ветви, порядок — объявления в спеке (правило 3)."""
    journal = passed("impl", "verify", "review") + [approve("review")]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "run"
    assert decision.stages == ("publish", "docs")
    assert decision.attempts == (1, 1)
    assert str(decision) == "run(publish#1, docs#1)"


def test_parallel_order_follows_spec_not_graph(tmp_path: Path) -> None:
    """Порядок пачки — объявление: в этой спеке docs стоит раньше publish."""
    spec = write_spec(tmp_path, REVERSED)
    journal = passed("impl", "review") + [approve("review")]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.stages == ("docs", "publish")


def test_unmarked_successors_start_one_at_a_time(tmp_path: Path) -> None:
    """Две готовые ветви без `parallel_with` идут по одной: параллелизм — объявление."""
    spec = write_spec(tmp_path, UNMARKED)
    journal = passed("impl")

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.stages == ("left",)
    states = transit.stage_states(spec, journal)
    assert states["right"].state == "pending"  # готова, но ждёт своей очереди


def test_approve_named_stage_runs_only_it(spec: Any) -> None:
    """`approve: <id>` адресует следующий этап явно, а не всех наследников."""
    journal = passed("impl", "verify", "review") + [approve("review", "docs")]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.stages == ("docs",)
    states = transit.stage_states(spec, journal)
    assert states["docs"].state == "pending"
    assert states["publish"].state == "wait_gate"
    assert "адресует" in states["publish"].reason


def test_approved_stage_closes_the_address_restriction(spec: Any) -> None:
    """Названный приговором этап пройден — ограничение снято, ветвь догоняет."""
    journal = passed("impl", "verify", "review") + [approve("review", "docs")] + passed("docs")

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.stages == ("publish",)


def test_approve_ancestor_does_not_resurrect_it(spec: Any) -> None:
    """`approve: impl` не переоткрывает пройденный этап: воскрешает только `changes`."""
    journal = passed("impl", "verify", "review") + [approve("review", "impl")]

    decision = transit.next_stage(spec, journal, NOW)

    states = transit.stage_states(spec, journal)
    assert states["impl"].state == "done"
    assert decision.kind == "run"
    assert decision.stages == ("publish", "docs")


def test_changes_reopens_target_and_its_successors(spec: Any) -> None:
    """`changes: impl` возвращает работу на impl и снимает `done` у потомков (правило 2)."""
    journal = passed("impl", "verify", "review") + [changes("review", "impl")]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.pair == [("impl", 2)]  # ответ — новый вход, номер попытки растёт
    states = transit.stage_states(spec, journal)
    assert states["impl"].state == "pending"
    assert states["verify"].state == "wait_gate"
    assert states["review"].state == "wait_gate"
    assert states["review"].rounds == 1


def test_publish_cannot_run_on_old_review_verify(spec: Any) -> None:
    """После возврата публикация ждёт **нового** ревью, а не старого `done`."""
    journal = (
        passed("impl", "verify", "review")
        + [changes("review", "impl")]
        + passed("impl", "verify", "review", attempt=2)
    )

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "wait"
    states = transit.stage_states(spec, journal)
    assert states["review"].rounds == 1
    assert states["publish"].state == "wait_gate"
    assert "приговора" in states["publish"].reason


def test_max_rounds_exhausted_fails_contract(spec: Any) -> None:
    """Больше `max_rounds` возвратов — не бесконечный цикл, а `failed(contract)`."""
    journal = passed("impl", "verify", "review")
    for attempt in (2, 3, 4):
        journal += [changes("review", "impl")] + passed("impl", "verify", "review", attempt=attempt)

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "failed"
    assert decision.fail_kind == "contract"
    assert transit.stage_states(spec, journal)["review"].rounds == 3
    assert transit.deal_state(spec, journal) == "failed"


# ------------------------------------------------------------------ правило 3


def test_parallel_stages_produce_one_decision_with_both(spec: Any) -> None:
    """Параллельные этапы — одна пачка и один номер попытки на каждый (правило 3)."""
    journal = passed("impl", "verify", "review") + [approve("review")]

    decision = transit.next_stage(spec, journal, NOW)

    assert isinstance(decision.stages, tuple)
    assert len(decision.stages) == 2
    assert decision.attempts == (1, 1)


# ------------------------------------------------------------------ правило 4


def test_retry_repeats_stage_until_class_limit(spec: Any) -> None:
    """`retry.max: 2` на классе `infra` — три попытки, потом отказ этапа (правило 4)."""
    journal = [started("impl", 1), crashed("impl", 1, "infra")]
    assert transit.next_stage(spec, journal, NOW).pair == [("impl", 2)]

    journal += [started("impl", 2), crashed("impl", 2, "infra")]
    assert transit.next_stage(spec, journal, NOW).pair == [("impl", 3)]

    journal += [started("impl", 3), crashed("impl", 3, "infra")]
    decision = transit.next_stage(spec, journal, NOW)
    assert (decision.kind, decision.fail_kind) == ("failed", "infra")
    states = transit.stage_states(spec, journal)
    assert states["impl"].state == "failed"
    assert states["impl"].failures == (("infra", 3),)


def test_retry_class_not_listed_fails_at_once(spec: Any) -> None:
    """Класс вне `retry.on` не ретраится: отказ этапа сразу, без повторов."""
    journal = [started("impl", 1), crashed("impl", 1, "capability")]

    decision = transit.next_stage(spec, journal, NOW)

    assert (decision.kind, decision.fail_kind) == ("failed", "capability")


def test_retry_max_zero_means_no_retry(tmp_path: Path) -> None:
    """`retry: {max: 0}` — класс назван, но попыток повтора нет."""
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
            retry: {max: 0, on: [infra]}
        """,
    )
    journal = [started("impl", 1), crashed("impl", 1, "infra")]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "failed"
    assert transit.stage_states(spec, journal)["impl"].state == "failed"


def test_unclassified_failure_is_not_retried(spec: Any) -> None:
    """Класса в событии нет — ретрая нет: слой не выводит класс из текста ошибки."""
    journal = [started("impl", 1), crashed("impl", 1)]

    decision = transit.next_stage(spec, journal, NOW)

    assert (decision.kind, decision.fail_kind) == ("failed", "unknown")
    assert "не сошлось" not in decision.reason  # текст ошибки в класс не превращается


def test_failure_without_retry_spec_uses_default_policy(tmp_path: Path) -> None:
    """Этап без `retry` живёт по умолчанию: `{max: 1, on: [infra]}` (docs/SPEC.md).

    Политика — часть контракта, а не удобство: `infra` повторяется один раз и без
    объявления, а `contract` не повторяется вовсе, потому что умолчание называет
    только инфра-сбой. Пустая политика вместо объявленной заводила бы второй
    набор правил в спеке и в коде.
    """
    spec = write_spec(
        tmp_path,
        """
        duty: probe
        stages:
          - id: impl
            role: implementer
        """,
    )
    infra = [started("impl", 1), crashed("impl", 1, "infra")]
    assert transit.next_stage(spec, infra, NOW).pair == [("impl", 2)]

    contract = [started("impl", 1), crashed("impl", 1, "contract")]
    decision = transit.next_stage(spec, contract, NOW)
    assert (decision.kind, decision.fail_kind) == ("failed", "contract")


# ------------------------------------------------------------------ правило 5


def test_open_question_waits_for_human(spec: Any) -> None:
    """Вопрос в журнале без ответа — `wait`, дело `wait_human` (правило 5)."""
    journal = [started("impl", 1), asked("impl", 284)]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "wait"
    assert decision.what == "question"
    states = transit.stage_states(spec, journal)
    assert states["impl"].state == "wait_human"
    assert states["impl"].question_id == "284"
    assert transit.deal_state(spec, journal) == "wait_human"


def test_answer_starts_new_attempt(spec: Any) -> None:
    """Ответ — новый вход: этап возвращается в работу новой попыткой (правило 5)."""
    journal = [started("impl", 1), asked("impl", 284), answered("impl", 284)]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.pair == [("impl", 2)]
    states = transit.stage_states(spec, journal)
    assert states["impl"].state == "pending"
    assert states["impl"].attempt == 1  # последняя **начатая** попытка
    assert states["impl"].question_id is None


def test_answer_to_foreign_question_is_ignored(spec: Any) -> None:
    """Ответ на чужой вопрос не поднимает этап: точка возврата — по `question_id`."""
    journal = [started("impl", 1), asked("impl", 284), answered("impl", 999)]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "wait"
    assert transit.stage_states(spec, journal)["impl"].state == "wait_human"


def test_expired_question_parks_the_deal(spec: Any) -> None:
    """Вопрос закрыт по сроку без ответа: перезапускать нечего — `park` (правило 5)."""
    journal = [started("impl", 1), asked("impl", 284), answered("impl", 284, 0, "expired")]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "park"
    assert decision.stages == ("impl",)
    assert decision.question_id == "284"
    assert transit.stage_states(spec, journal)["impl"].state == "parked"
    assert transit.deal_state(spec, journal) == "parked"


def test_deal_level_question_waits_and_answer_raises_attempt(spec: Any) -> None:
    """`ask()` пишет вопрос без `stage`: адресат — последняя попытка, ответ — вход.

    Это тот самый цикл, ради которого правило 5 и написано: вопрос → `wait_human`
    → ответ в пуле (`sync_answers`) → `run(этап, attempt+1)`. Без адресации
    вопроса последней попытке этап читался бы `done` и движок ушёл бы дальше по
    графу, а вопрос повис бы без адреса.
    """
    journal = [started("impl", 1), ok("impl", 1), deal_asked(284)]

    waiting = transit.next_stage(spec, journal, NOW)
    assert waiting.kind == "wait"
    assert waiting.what == "question"
    states = transit.stage_states(spec, journal)
    assert states["impl"].state == "wait_human"
    assert states["impl"].question_id == "284"
    assert transit.deal_state(spec, journal) == "wait_human"

    journal += [deal_answered(284)]
    decision = transit.next_stage(spec, journal, NOW)
    assert decision.pair == [("impl", 2)]


def addressed_asked(question_id: int, stage: str, attempt: int = 1) -> dict[str, Any]:
    """Вопрос дела с адресом движка: `payload.routing` вместо `stage` верхнего уровня.

    Так его пишет `_parked` (`dashboard/runner.py`): `ask()` не знает этапа, а
    движок — знает и кладёт адрес вложенным ключом.
    """
    return ev(
        "question_asked",
        question_id=question_id,
        session_key="t_d_probe",
        routing={"target": "role:researcher", "stage": stage, "attempt": attempt},
    )


def test_addressed_question_belongs_to_its_own_stage(spec: Any) -> None:
    """Адрес вопроса важнее «последней попытки»: при параллельных этапах это разные вещи.

    Вопрос задал этап `verify`, а последняя попытка журнала — `impl`. Если
    адресовать вопрос последней, `impl` встал бы в `wait_human` на вопрос,
    которого не задавал, а `verify` остался бы `pending` — и движок запустил бы
    его поверх открытого вопроса.
    """
    journal = [started("impl", 1), ok("impl", 1), addressed_asked(284, "verify")]

    states = transit.stage_states(spec, journal)

    assert states["verify"].state == "wait_human"
    assert states["verify"].question_id == "284"
    assert states["impl"].state == "done"
    assert states["impl"].question_id is None
    assert transit.deal_state(spec, journal) == "wait_human"
    decision = transit.next_stage(spec, journal, NOW)
    assert decision.kind == "wait"
    assert decision.what == "question"


def test_question_with_unknown_stage_falls_back_to_the_last_attempt(spec: Any) -> None:
    """Незнакомый этап в адресе — не адрес: вопрос достаётся последней попытке."""
    journal = [started("impl", 1), ok("impl", 1), addressed_asked(284, "no-such-stage")]

    states = transit.stage_states(spec, journal)

    assert states["impl"].state == "wait_human"
    assert states["impl"].question_id == "284"


def test_expired_deal_question_parks_the_stage(spec: Any) -> None:
    """Срок вопроса дела истёк — этап припаркован, а не перезапущен."""
    journal = [started("impl", 1), ok("impl", 1), deal_asked(284), deal_answered(284, 0, "expired")]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "park"
    assert decision.question_id == "284"
    assert transit.stage_states(spec, journal)["impl"].state == "parked"


def test_answer_without_any_question_is_ignored(spec: Any) -> None:
    """Ответ, которому нечего закрывать, срез не двигает: адресат — по `question_id`."""
    journal = [started("impl", 1), deal_answered(999)]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "wait"
    assert decision.what == "attempt"


# ------------------------------------------------------------------ правило 6


def test_needs_gate_is_not_the_deal_gate(spec: Any) -> None:
    """Гейт этапа — это `needs`: до закрытия impl остальные не идут (правило 6)."""
    journal = [started("impl", 1)]

    states = transit.stage_states(spec, journal)

    assert states["verify"].state == "wait_gate"
    assert "impl" in states["verify"].reason
    assert transit.next_stage(spec, journal, NOW).what == "attempt"


# ------------------------------------------------------------------ правило 7


def test_late_comment_keeps_closed_stage(spec: Any) -> None:
    """Поздний `commented`/`attached`/`patched` закрытый этап не меняет (правило 7).

    Набор проверяется не перечислением, а тем же источником, что у журнала:
    `journal.NON_STATE_KINDS` минус ответ на вопрос. Так тест не устаревает, когда
    журнал заводит новый вид телеметрии, и по-прежнему ловит расхождение — если
    слой перестанет смотреть на общий набор.
    """
    quiet = [kind for kind in transit.journal.NON_STATE_KINDS if kind != "question_answered"]
    assert {"commented", "attached", "patched"} <= set(quiet)
    journal = passed("impl") + [ev(kind, "impl", author="max", size=3) for kind in quiet]

    states = transit.stage_states(spec, journal)

    assert states["impl"].state == "done"
    assert states["impl"].terminal is True
    assert transit.next_stage(spec, journal, NOW).pair == [("verify", 1)]


def test_late_question_answer_does_not_resurrect_stage(spec: Any) -> None:
    """Ответ, пришедший после закрытия этапа, работу не воскрешает (правило 7)."""
    journal = passed("impl") + [asked("impl", 284), answered("impl", 284)]

    states = transit.stage_states(spec, journal)

    assert states["impl"].state == "done"
    assert transit.next_stage(spec, journal, NOW).pair == [("verify", 1)]


def test_late_attempt_fact_does_not_reopen_closed_stage(spec: Any) -> None:
    """Запоздалый `attempt_started` поверх `done` срез не переписывает (правило 7)."""
    journal = passed("impl") + [started("impl", 5)]

    states = transit.stage_states(spec, journal)

    assert states["impl"].state == "done"
    assert states["impl"].attempt == 1


# ------------------------------------------------------------------ правило 8


def test_garbage_events_keep_the_function_total(spec: Any) -> None:
    """Мусор в журнале — не повод падать: ответ всё равно `Decision` (правило 8)."""
    journal: list[Any] = [
        "не событие",  # строка вместо словаря
        {"kind": "attempt_started", "payload": "не json и не словарь"},
        {"id": 3, "ts": "вчера", "kind": "completed", "payload": {"stage": "нет-такого"}},
        ev("attempt_started", "impl", attempt=1),
        dict(ev("attempt_ended", "impl", attempt=1, outcome="done"), payload={"stage": "чужой"}),
        ev("неизвестный-вид", "impl"),
        dict(ev("claimed", "impl"), deal_id="d_чужое"),
        {"kind": None, "payload": None},
    ]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind in transit.DECISION_KINDS
    assert transit.deal_state(spec, journal) in transit.DEAL_STATES
    assert set(transit.stage_states(spec, journal)) == set(spec.ids)


def test_membership_is_decided_by_stage_not_deal_id(spec: Any) -> None:
    """Принадлежность этапу решает `stage` в payload, а не `deal_id`.

    Журнал читается по делу (`journal.read_events(conn, deal_id)`), поэтому
    отбор по `deal_id` — работа читателя: второй фильтр здесь молча выкинул бы
    факт, если бы вызывающий передал события шире. Чужой этап при этом
    игнорируется — его нет в спеке.
    """
    applied = transit.next_stage(spec, [dict(ev("completed", "impl"), deal_id="d_сосед")], NOW)
    ignored = transit.next_stage(spec, [dict(ev("completed", "чужой"), deal_id=DEAL)], NOW)

    assert applied.stages == ("verify",)  # impl закрыт: факт прочитан
    assert ignored.stages == ("impl",)  # чужого этапа в спеке нет: факт пропущен


def test_repeated_event_is_the_same_fact(spec: Any) -> None:
    """Повтор того же `attempt_started` не двигает номер попытки: факт один."""
    journal = [started("impl", 1), started("impl", 1)]

    states = transit.stage_states(spec, journal)

    assert states["impl"].attempt == 1
    assert states["impl"].state == "running"


def test_permutation_of_events_never_crashes(spec: Any) -> None:
    """Случайная перестановка событий: ответ не «падает» (правило 8, воспроизводимо)."""
    journal = (
        passed("impl", "verify", "review")
        + [approve("review")]
        + passed("publish")
        + [started("docs", 1), asked("docs", 284)]
    )
    rng = random.Random(20261104)

    for _ in range(200):
        shuffled = journal[:]
        rng.shuffle(shuffled)
        decision = transit.next_stage(spec, shuffled, NOW)
        assert decision.kind in transit.DECISION_KINDS
        assert transit.deal_state(spec, shuffled) in transit.DEAL_STATES
        assert set(transit.stage_states(spec, shuffled)) == set(spec.ids)


def test_same_input_gives_equal_output(spec: Any) -> None:
    """Идемпотентность: два вызова на одном входе дают равный результат."""
    journal = passed("impl", "verify", "review") + [approve("review")]

    first = transit.next_stage(spec, journal, NOW)
    second = transit.next_stage(spec, journal, NOW)

    assert first == second
    assert transit.stage_states(spec, journal) == transit.stage_states(spec, journal)
    assert transit.deal_state(spec, journal) == transit.deal_state(spec, journal)


def test_input_is_not_mutated(spec: Any) -> None:
    """Слой ничего не пишет: поданный журнал остаётся байт в байт."""
    journal = passed("impl", "verify", "review") + [approve("review")]
    before = copy.deepcopy(journal)

    transit.stage_states(spec, journal)
    transit.next_stage(spec, journal, NOW)
    transit.deal_state(spec, journal)

    assert journal == before


# ------------------------------------------------------- часы, полнота, дело


def test_module_reads_no_clock() -> None:
    """Часов модуль не читает: `now` приходит аргументом (иначе прогон не воспроизводим)."""
    source = (ROOT / "dashboard" / "transit.py").read_text(encoding="utf-8")

    assert not hasattr(transit, "time")
    assert not hasattr(transit, "datetime")
    assert "import time" not in source
    assert "import datetime" not in source
    assert "time.time(" not in source
    assert "datetime.now(" not in source


def test_now_changes_only_the_wording(spec: Any) -> None:
    """`now` уточняет текст причины, но не выбор: решение от часов не зависит."""
    journal = [started("impl", 1), asked("impl", 284)]

    soon = transit.next_stage(spec, journal, 1793800010)
    late = transit.next_stage(spec, journal, NOW)

    assert (soon.kind, soon.what, soon.stages) == (late.kind, late.what, late.stages)
    assert soon.reason != late.reason  # возраст вопроса назван по-разному


def test_stage_kinds_are_journal_kinds() -> None:
    """Виды, которые читает слой, существуют в закрытом наборе журнала.

    Слой не заводит своих видов: `kind`, которого нет в `journal.EVENT_KINDS`,
    никто не запишет (`journal.append` отвергает), и правило на нём было бы
    обещанием без факта. Тест ловит именно это — выдуманное имя вида.
    """
    invented = set(transit.STAGE_KINDS) - set(transit.journal.EVENT_KINDS)

    assert invented == set()
    assert {"attempt_started", "attempt_ended", "question_asked"} <= set(transit.STAGE_KINDS)


def test_decision_kinds_are_exactly_five() -> None:
    """Исходов ровно пять, и «а ещё» нет: форма ответа закрыта."""
    assert transit.DECISION_KINDS == ("run", "wait", "done", "failed", "park")
    assert transit.STAGE_STATES == (
        "pending",
        "running",
        "done",
        "failed",
        "wait_human",
        "wait_gate",
        "parked",
    )
    assert transit.DEAL_STATES == (
        "pending",
        "running",
        "wait_human",
        "wait_gate",
        "done",
        "failed",
        "parked",
    )
    assert "verdict" in transit.WAIT_KINDS


def test_closed_deal_is_done(spec: Any) -> None:
    """Закрытие дела событием без `stage` — окончательный ответ `done`."""
    journal = passed("impl", "verify", "review") + [approve("review")] + [
        ev("completed", status="done")
    ]

    decision = transit.next_stage(spec, journal, NOW)

    assert decision.kind == "done"
    assert transit.deal_state(spec, journal) == "done"


def test_all_stages_done_is_done(spec: Any) -> None:
    """Все этапы закрыты — дело закрыто, запускать нечего."""
    journal = passed("impl", "verify", "review") + [approve("review")] + passed("publish", "docs")

    assert transit.next_stage(spec, journal, NOW).kind == "done"
    assert transit.deal_state(spec, journal) == "done"


@pytest.mark.parametrize(
    ("journal", "expected"),
    [
        ([], "pending"),
        ([started("impl", 1)], "running"),
        ([started("impl", 1), asked("impl", 284)], "wait_human"),
        ([started("impl", 1), asked("impl", 284), answered("impl", 284, 0, "expired")], "parked"),
        ([started("impl", 1), crashed("impl", 1, "capability")], "failed"),
        (passed("impl", "verify", "review") + [approve("review")] + passed("publish", "docs"), "done"),
    ],
)
def test_deal_state_names_the_whole_deal(spec: Any, journal: list[Any], expected: str) -> None:
    """Состояние дела — одно слово контракта, по тому же срезу."""
    assert transit.deal_state(spec, journal) == expected


def test_deal_state_wait_gate_when_graph_stalls(spec: Any) -> None:
    """Граф стоит (приговор адресует этап, которого нет) — `wait_gate`, а не «всё хорошо».

    Такую спеку отверг бы валидатор, но слой обязан остаться тотальным: молчание
    вместо ответа запрещено, поэтому «запускать нечего, а дело не закрыто» названо
    своим словом.
    """
    journal = passed("impl", "verify", "review") + [approve("review", "нет-такого-этапа")]

    assert transit.deal_state(spec, journal) == "wait_gate"
    decision = transit.next_stage(spec, journal, NOW)
    assert decision.kind == "wait"
    assert decision.what == "gate"
    assert "нет-такого-этапа" in decision.reason
