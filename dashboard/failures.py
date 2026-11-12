"""Классы отказа и политика ретраев — единственное место, где они определены.

Спека-источник называет три класса отказа (`infra` / `contract` / `capability`) и
требует, чтобы в 0.0.4 классы и лимиты были **политикой**, а не комментарием в
коде. Этот модуль и есть та политика: словарь классов, правило «из чего класс
выводится», лимиты повторов на этап и на класс, пауза между попытками и общий
предохранитель на дело.

## Классы

- **infra** — процесс не дожил: таймаут, ненулевой код выхода без `result.json`,
  смерть от сигнала (код `128 + N` или отрицательный код), падение спавна,
  недоступность модели/прокси (признак один — спавн вернул ошибку);
- **contract** — процесс отчитался не по контракту или не отчитался:
  `result.json` отсутствует при коде 0, схема не та, evidence не сходится
  (`dashboard/evidence.py`), `status: "refused"`;
- **capability** — работа упёрлась в стену: бюджет токенов, отказ доступа, «нужна
  роль, которой нет в спеке», внешний сервис требует прав, **сломан инструмент
  проверки** (тулчейн линтера, модуль, которого нет: подпись из закрытого
  `TOOLCHAIN_SIGNATURES`).

Правило: класс выводится **только** из пяти источников — `result.json`, кода
выхода процесса, наличия файла отчёта, `Verdict.kind` evidence-гейта и подписи
сломанного инструмента из закрытого списка `TOOLCHAIN_SIGNATURES`. Разбора
текста ошибки регулярками здесь нет и не будет: подпись ищется сравнением с
именованным списком, и совпадение означает ровно «сломан названный инструмент», а
не «в тексте мелькнуло знакомое слово». Если класса не хватает источнику, это
`contract` плюс `reason` с цитатой, а не догадка по словам.

## Политика

`retry: {max: N, on: [infra, contract]}` (по умолчанию `max: 1, on: [infra]`) —
сколько раз повторять отказ **этого** класса. Счётчик считается на этап и на класс:
`infra` и `contract` — разные причины, и общий счётчик склеил бы их в один.

Без объявления движок повторяет только `infra` (`DEFAULT_ON`): остальные классы
называются в спеке поимённо. Объявленный набор (`retry.on`) **уважается как
есть** — хартия главнее политики движка, и `on: [infra, capability]` включает
повтор `capability`. Ограничивают серию не догадки движка, а `max` этапа и общий
предохранитель на дело.

Пауза между попытками растёт экспоненциально от 30 с и упирается в потолок 5 мин.
Пауза не блокирует движок: дело просто ждёт `retry_at` в срезе, а решение о
следующем шаге отвечает `wait("retry", …)` (`dashboard/transit.py`). Собственная
серия движка (2.0, `runner.run_attempts`) идёт без паузы: попытка — отдельный
процесс, и спать в нём значило бы держать этап без лизинга, пока `reaper` считает
дело осиротевшим.

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

# Человеческие имена классов — по смыслу, а не по слагу. Класс нужен затем, чтобы
# понять, чья это работа, поэтому имя отвечает на вопрос «кто», а не повторяет
# слово из кода: слаг, показанный как есть, человек переводит в голове, а перевод,
# сделанный в каждом шейпере заново, разъезжается между поверхностями.
CLASS_TEXT = {
    INFRA: "среда",
    CONTRACT: "договорённость",
    CAPABILITY: "доступ",
}

# Значение вне закрытого набора. Догадка («наверное, инфраструктура») врала бы про
# причину, поэтому неизвестный класс называется неизвестным.
UNKNOWN_CLASS_TEXT = "неизвестный класс"


def describe_class(kind: object) -> str:
    """Человеческое имя класса отказа по его слагу.

    Известный слаг получает имя из `CLASS_TEXT`; любое другое значение — включая
    пустую строку, другой регистр и не-строку — `UNKNOWN_CLASS_TEXT`. Исключения
    не бросаются: класс приходит из данных (журнал, чужая доска), и падать на
    неожиданном значении значит валить срез из-за одного дела. Регистр и пробелы
    намеренно не прощаются: прощающий разбор сделал бы закрытый набор открытым
    сбоку.
    """
    return CLASS_TEXT.get(kind, UNKNOWN_CLASS_TEXT) if isinstance(kind, str) else UNKNOWN_CLASS_TEXT


# Подписи сломанного инструмента проверки в stderr: сломан не код, а тот, кто его
# собирает и линтует. Список **закрыт намеренно**: `can't load config` — это
# golangci-lint с чужим тулчейном (живой случай RP-14338: код 3, «Go version
# (go1.26) is lower than the targeted (1.27)»), остальные — go без модуля/GOROOT/
# тулчейна и оболочка без программы. Расширять список — решение (правка этого
# файла вместе с тестом), а не догадка по очередному тексту ошибки: иначе разбор
# stderr тихо стал бы разбором «на что похоже».
TOOLCHAIN_SIGNATURES = (
    "can't load config",
    "go: cannot find main module",
    "GOROOT",
    "toolchain not available",
    "command not found",
)

# Смерть от сигнала в коде возврата: оболочка отдаёт `128 + N`, а `returncode`
# самого процесса — отрицательный номер. Оба вида значат «команда не дожила».
SIGNAL_EXIT_BASE = 128


def toolchain_signature(stderr: str) -> str:
    """Подпись сломанного инструмента из stderr; пусто — подписи нет.

    Совпадение ищется подстрокой по закрытому `TOOLCHAIN_SIGNATURES`: инструмент
    печатает подпись внутри строки (`Error: can't load config: ...`), и сравнение
    строк целиком не сработало бы вовсе. Регистр значим — список закрыт, а
    прощающий разбор сделал бы его открытым сбоку.
    """
    text = stderr if isinstance(stderr, str) else ""
    for signature in TOOLCHAIN_SIGNATURES:
        if signature in text:
            return signature
    return ""


def signal_of(exit_code: Optional[int]) -> Optional[int]:
    """Номер сигнала, которым умер процесс; `None` — умер не от сигнала.

    Два вида одного факта: `returncode` процесса отрицателен (`-15`), а код,
    который отдаёт оболочка, — `128 + N` (`143`). Ровно `128` сигналом не
    считается: `128 + 0` не существует, такой код команда вернула сама — значит
    она сказала «нет», а не «не дожила».
    """
    if exit_code is None or isinstance(exit_code, bool):
        return None
    code = _int(exit_code)
    if code < 0:
        return -code
    if SIGNAL_EXIT_BASE < code <= 255:
        return code - SIGNAL_EXIT_BASE
    return None

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
# Остальные классы без объявления не повторяются: `contract` и `capability`
# называются в спеке поимённо, и тогда движок их повторяет (хартия главнее).
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

    Порядок: **готовый отчёт исполнителя сильнее наших потолков**. Если
    `result.json` есть и он валиден, попытка кончилась тем, что в нём написано:
    бюджет и таймаут ограничивают работу, а не отменяют уже сделанную. Обратный
    порядок выбрасывал готовую работу — попытка успевала закоммитить и отчитаться
    (`status=done`, доказательства с кодом 0), а её добивал SIGTERM за токены
    (дело `d_a7040118`, 2026-11-06).

    Дальше — потолки, потом код выхода. Текст ошибки в stderr причиной не
    считается: процесс может ругаться и при успехе, а может умереть молча.

    Отдельный вход `result_error` — причина, почему отчёта нет (нет файла, битый
    JSON, не объект): это тоже факт источника, а не текст ошибки процесса.
    """
    if timed_out and result is None:
        return Ended(
            OUTCOME_FAILED,
            INFRA,
            f"таймаут попытки: процесс не уложился в бюджет времени ({timeout_sec} с)",
        )
    if over_budget and result is None:
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
    """Политика ретраев этапа: сколько повторов и на каких классах.

    `on` — набор классов, по которым движок повторит: без объявления в спеке —
    `DEFAULT_ON`, с объявлением — то, что названо (`retry.on`), в пределах
    словаря `CLASSES`. Объявленный набор не сужается: хартия главнее политики
    движка (`docs/ENGINE-2.0.md`, раздел 7).
    """

    max: int = DEFAULT_MAX
    on: tuple[str, ...] = DEFAULT_ON

    def allows(self, kind: str) -> bool:
        """Повторяется ли этот класс вообще (до счёта попыток).

        Условие одно: класс назван в политике этапа. Спека, объявившая
        `on: [infra, capability]`, получает повтор `capability` — лимит `max` и
        предохранитель на дело ограничивают серию, а не догадка о классе.
        """
        return kind in self.on


def policy(stage_spec: Any) -> Policy:
    """Политика этапа из спеки; объявления нет — политика по умолчанию.

    `retry` в спеке не задан → `{max: 1, on: [infra]}` (спека-источник,
    `docs/SPEC.md`). Отрицательный `max` читается как ноль: повторов не будет,
    а не «сколько-нибудь». Объявленные классы берутся как есть, кроме имён вне
    словаря `CLASSES`: опечатку спека отвергает до запуска (`spec._check_retry`),
    а здесь чужое имя просто не стало бы повтором.
    """
    retry = getattr(stage_spec, "retry", None)
    if retry is None:
        return Policy()
    maximum = getattr(retry, "max", DEFAULT_MAX)
    declared = tuple(str(kind) for kind in (getattr(retry, "on", None) or ()))
    on = tuple(kind for kind in declared if kind in CLASSES)
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


def auto_retries(events: Iterable[Any], stage: Optional[str] = None) -> dict[str, Any]:
    """Сколько автоматических повторов сделал движок и по каким классам.

    Повтор движка отличим от повторного `duty_start` лида по факту: у
    `attempt_started` повторной попытки есть `retried_from` — номер попытки, чей
    отказ повторяется (`dashboard/runner.py`, `start_stage`). Класс берётся из
    `attempt_ended` той самой попытки, а не из нашего слова: «два повтора, оба
    `contract`» — это факт журнала, и лид читает его, не пересчитывая.

    Ответ — `{"count": N, "kinds": {класс: N}}`: число повторов и их состав.
    """
    started: dict[int, Optional[int]] = {}
    ended: dict[int, str] = {}
    for event in events or ():
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        payload = _payload(event)
        if stage is not None and str(payload.get("stage") or "") != str(stage):
            continue
        number = _int(payload.get("attempt"))
        if kind == "attempt_started":
            retried_from = payload.get("retried_from")
            started[number] = None if retried_from is None else _int(retried_from)
        elif kind == "attempt_ended" and str(payload.get("outcome") or "").strip().lower() == OUTCOME_FAILED:
            ended[number] = str(payload.get("kind") or "").strip() or UNCLASSIFIED
    counts: dict[str, int] = {}
    total = 0
    for retried_from in started.values():
        if retried_from is None:
            continue
        total += 1
        cause = ended.get(retried_from, UNCLASSIFIED)
        counts[cause] = counts.get(cause, 0) + 1
    return {"count": total, "kinds": counts}


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
