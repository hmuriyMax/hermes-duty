"""Классы отказа и политика ретраев — единственное место, где они определены.

Спека-источник называет три класса отказа (`infra` / `contract` / `capability`) и
требует, чтобы в 0.0.4 классы и лимиты были **политикой**, а не комментарием в
коде. Этот модуль и есть та политика: словарь классов, правило «из чего класс
выводится», лимиты повторов на этап и на класс, пауза между попытками и общий
предохранитель на дело.

## Классы

- **infra** — процесс не дожил: таймаут, ненулевой код выхода без `result.json`,
  падение спавна, недоступность модели/прокси (признак один — спавн вернул ошибку);
- **contract** — процесс отчитался не по контракту или не отчитался:
  `result.json` отсутствует при коде 0, схема не та, evidence не сходится
  (`dashboard/evidence.py`), `status: "refused"`;
- **capability** — работа упёрлась в стену: бюджет токенов, отказ доступа, «нужна
  роль, которой нет в спеке», внешний сервис требует прав.

Правило: класс выводится **только** из четырёх источников — `result.json`, кода
выхода процесса, наличия файла отчёта и `Verdict.kind` evidence-гейта. Разбора
текста ошибки регулярками здесь нет и не будет: если класса не хватает источнику,
это `contract` плюс `reason` с цитатой, а не догадка по словам.

## Политика

`retry: {max: N, on: [infra, contract]}` (по умолчанию `max: 1, on: [infra]`) —
сколько раз повторять отказ **этого** класса. Счётчик считается на этап и на класс:
`infra` и `contract` — разные причины, и общий счётчик склеил бы их в один.
`capability` по умолчанию не повторяется: стена не рассосётся.

Пауза между попытками растёт экспоненциально от 30 с и упирается в потолок 5 мин.
Пауза не блокирует движок: дело просто ждёт `retry_at` в срезе, а решение о
следующем шаге отвечает `wait("retry", …)` (`dashboard/transit.py`).

Общий предохранитель: если за дело набралось `MAX_ROUNDS_PER_DEAL` упавших попыток
по любому классу, дело паркуется — это защита от того самого `gave_up`-класса,
который в канбане жёг карточки.

Границы модуля: ни БД, ни файлов, ни часов, ни процессов. На вход — факты (отчёт,
код выхода, события журнала), на выход — значение. Поэтому политику можно
проверить без модели и без движка.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# --------------------------------------------------------------- словарь классов

# Классы отказа. Порядок — тот, в котором о них говорит спека.
CLASSES = ("infra", "contract", "capability")

INFRA = "infra"
CONTRACT = "contract"
CAPABILITY = "capability"

# Класса в событии нет. Это не догадка о причине, а честное имя отсутствия
# класса: такой отказ не ретраится, потому что `retry.on` его не называет.
UNCLASSIFIED = "unknown"

# `block_kind` парковки. Парковка — это `blocked` с этим видом плюс вопрос
# человеку (`dashboard/runner.py`): отдельного вида события журнала для неё нет.
PARKED_KIND = "parked"

# --------------------------------------------------- исходы и статусы отчёта

OUTCOME_DONE = "done"
OUTCOME_FAILED = "failed"

# Статусы `result.json`, которые считаются успехом: `status` не назван — тоже
# успех (процесс дошёл до конца и отчитался).
OK_STATUSES = ("done", "ok", "success", "passed", "accepted")

# Статусы `result.json`, которые означают «нужен человек»: вопрос, а не отказ.
BLOCKED_STATUSES = ("blocked", "needs_input", "wait_human")

# Статусы `result.json`, которые означают отказ исполнителя.
REFUSED_STATUSES = ("refused", "rejected")

# ------------------------------------------------------------------- лимиты

# Политика этапа по умолчанию: одна повторная попытка, и только на `infra`.
DEFAULT_MAX = 1
DEFAULT_ON = (INFRA,)

# Общий предохранитель на дело: столько упавших попыток (по любому классу) — и
# дело паркуется, а не крутится.
MAX_ROUNDS_PER_DEAL = 6

# Пауза между попытками: от 30 с, вдвое за попытку, потолок 5 мин.
BACKOFF_START_SEC = 30
BACKOFF_CAP_SEC = 300

# Почему дело запарковано. Код называет правило, текст причины — факты.
REASON_RETRIES = "retries_exhausted"
REASON_TOO_MANY = "too_many_failures"

# Варианты ответа человека на парковку: продолжение сбрасывает счётчики класса,
# остановка закрывает дело. Ни один из них не применяется автоматически.
PARK_OPTIONS = ("continue", "stop")


# ------------------------------------------------------------- исход попытки


@dataclass(frozen=True)
class Ended:
    """Чем кончилась попытка: исход, класс отказа и причина словами.

    Одна форма на все три ветки классификации, чтобы движок не собирал
    `(outcome, kind, reason)` из разных мест и не расходился с политикой.
    """

    outcome: str
    kind: str = ""
    reason: str = ""

    @property
    def failed(self) -> bool:
        """Попытка не удалась: исход `failed` (класс тогда всегда назван)."""
        return self.outcome == OUTCOME_FAILED


def classify(
    *,
    result: Optional[dict[str, Any]],
    result_error: str = "",
    exit_code: Optional[int] = None,
    timed_out: bool = False,
    over_budget: bool = False,
    timeout_sec: int = 0,
) -> Ended:
    """Чем кончилась попытка — по трём источникам и нашим собственным фактам.

    Порядок — не догадка, а природа источников: таймаут и бюджет проверили мы
    сами, дальше слово исполнителя (`result.json`), и только потом код выхода.
    Текст ошибки в stderr причиной не считается: процесс может ругаться и при
    успехе, а может умереть молча.

    Отдельный вход `result_error` — причина, почему отчёта нет (нет файла, битый
    JSON, не объект): это тоже факт источника, а не текст ошибки процесса.
    """
    if timed_out:
        return Ended(
            OUTCOME_FAILED,
            INFRA,
            f"таймаут попытки: процесс не уложился в бюджет времени ({timeout_sec} с)",
        )
    if over_budget:
        return Ended(OUTCOME_FAILED, CAPABILITY, "бюджет токенов попытки исчерпан")
    if result is not None:
        status = str(result.get("status") or "done").strip().lower()
        if status in BLOCKED_STATUSES:
            return Ended(OUTCOME_DONE, "", f"попытка отчиталась статусом {status}: ждёт человека")
        if status in REFUSED_STATUSES:
            return Ended(OUTCOME_FAILED, CONTRACT, f"исполнитель отказался: status={status}")
        return Ended(OUTCOME_DONE, "", "")
    if exit_code not in (0, None):
        return Ended(OUTCOME_FAILED, INFRA, f"процесс упал сам: код выхода {exit_code}")
    return Ended(OUTCOME_FAILED, CONTRACT, result_error or "исполнитель не отчитался: result.json нет")


def verdict_kind(verdict: Any) -> str:
    """Класс отказа из вердикта evidence-гейта (`Verdict.kind`, W4).

    Гейт выдаёт только `contract` и `capability`; неизвестное значение считается
    `contract`: «пакет не сошёлся» — это прежде всего несоблюдение контракта, и
    выдавать его за `infra` значило бы списать на процесс вину отчёта.
    """
    kind = str(getattr(verdict, "kind", "") or "")
    return kind if kind in (CONTRACT, CAPABILITY) else CONTRACT


# ------------------------------------------------------------------- политика


@dataclass(frozen=True)
class Policy:
    """Политика ретраев этапа: сколько повторов и на каких классах."""

    max: int = DEFAULT_MAX
    on: tuple[str, ...] = DEFAULT_ON

    def allows(self, kind: str) -> bool:
        """Повторяется ли этот класс вообще (до счёта попыток)."""
        return kind in self.on


def policy(stage_spec: Any) -> Policy:
    """Политика этапа из спеки; объявления нет — политика по умолчанию.

    `retry` в спеке не задан → `{max: 1, on: [infra]}` (спека-источник,
    `docs/SPEC.md`). Отрицательный `max` читается как ноль: повторов не будет,
    а не «сколько-нибудь».
    """
    retry = getattr(stage_spec, "retry", None)
    if retry is None:
        return Policy()
    maximum = getattr(retry, "max", DEFAULT_MAX)
    on = tuple(str(kind) for kind in (getattr(retry, "on", None) or ()))
    return Policy(max=max(0, _int(maximum)), on=on)


def backoff(attempts: int) -> int:
    """Пауза перед повтором после `attempts` отказов класса: 30 с, вдвое, ≤ 5 мин.

    Первый повтор ждёт `BACKOFF_START_SEC`, второй — вдвое дольше, дальше пауза
    упирается в потолок: «подожди ещё сутки» — это не ретрай, а парковка.
    """
    number = max(1, _int(attempts))
    delay = BACKOFF_START_SEC * (2 ** (number - 1))
    return min(delay, BACKOFF_CAP_SEC)


def retry_at(attempts: int, failed_at: int) -> int:
    """Момент, раньше которого повторять нельзя: время отказа плюс пауза."""
    return _int(failed_at) + backoff(attempts)


def retriable(policy_: Policy, kind: str, attempts: int) -> bool:
    """Есть ли ещё попытка для этого класса: класс объявлен и лимит не исчерпан.

    `attempts` — сколько отказов **этого** класса уже случилось (включая
    последний). `max: 2` значит «две повторные попытки», то есть отказ
    ретраится, пока `attempts <= max`.
    """
    if not policy_.allows(kind):
        return False
    return _int(attempts) <= max(0, _int(policy_.max))


def counter(kind: str, attempts: int, limit: int) -> str:
    """Счётчик «сколько из скольких» в том виде, в каком его читает человек."""
    return f"{_int(attempts)}/{_int(limit)} {kind or UNCLASSIFIED}"


def decide_park(*, kind: str, attempts: int, total: int, policy_: Policy) -> Optional[str]:
    """Пора ли парковать дело: код причины или `None` — «повторяем».

    Два правила и их порядок: общий предохранитель на дело (`too_many_failures`)
    сильнее политики этапа — дело, набравшее лимит упавших попыток, паркуется
    даже если каждый отдельный класс ещё «можно повторить». Иначе исчерпание
    лимита класса — `retries_exhausted`.
    """
    if _int(total) >= MAX_ROUNDS_PER_DEAL:
        return REASON_TOO_MANY
    if retriable(policy_, str(kind or UNCLASSIFIED), attempts):
        return None
    return REASON_RETRIES


def park_limit(reason: str, policy_: Policy) -> int:
    """Лимит, который показать рядом со счётчиком: предохранитель или класс."""
    return MAX_ROUNDS_PER_DEAL if reason == REASON_TOO_MANY else max(0, _int(policy_.max))


# ------------------------------------------------------------ факты из журнала


def failures_of(events: Iterable[Any], stage: Optional[str] = None) -> dict[str, int]:
    """Сколько отказов каждого класса в журнале (этапа, если он назван).

    Счёт берётся из `attempt_ended` с `outcome=failed` и полем `kind`: это факт
    движка, а не вывод из текста. События без класса считаются классом
    `unknown` — счётчик обязан сходиться с числом упавших попыток, иначе
    предохранитель молча не сработает.
    """
    counts: dict[str, int] = {}
    for event in events or ():
        if not isinstance(event, dict) or str(event.get("kind") or "") != "attempt_ended":
            continue
        payload = _payload(event)
        if stage is not None and str(payload.get("stage") or "") != str(stage):
            continue
        if str(payload.get("outcome") or "").strip().lower() != OUTCOME_FAILED:
            continue
        kind = str(payload.get("kind") or "").strip() or UNCLASSIFIED
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def total_failures(events: Iterable[Any]) -> int:
    """Сколько попыток дела упало по любому классу — предохранитель на дело."""
    return sum(failures_of(events).values())


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    """`payload` события объектом: мусор и чужие формы — пустой словарь."""
    raw = event.get("payload")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _int(value: Any) -> int:
    """Целое из факта: `True` числом не считается, мусор — нулём."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
