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

`retry: {max: N, on: [infra, contract]}` (по умолчанию `max: 4, on: [infra, contract]`,
у `contract` потолок класса — один повтор, `CONTRACT_MAX_RETRIES`) —
сколько раз повторять отказ **этого** класса. Счётчик считается на этап и на класс:
`infra` и `contract` — разные причины, и общий счётчик склеил бы их в один.

Без объявления движок повторяет `infra` и `contract` (`DEFAULT_ON`): остальные
классы называются в спеке поимённо. Спека может **сузить** набор, но не расширить:
`retry.on` читается в пересечении с классами, которые движок повторяет вообще
(`AUTO_ON` — `infra` и `contract`). `capability` не повторяется никогда: стена
(бюджет, доступ, роль) повтором не рассасывается, и `retry.on: [capability]`
означает пустой набор — спека говорит об этом предупреждением, а не отказом
(`spec._check_retry`). Ограничивают серию не догадки движка, а `max` этапа и общий
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

# Признак «попытку прервал рестарт рантайма» (0.2.2). Это **не** класс политики:
# в `CLASSES` его нет и `retry.on` им не объявить — повторять тут нечего, попытку
# не проиграли. 35 из 91 провала за неделю пришлись ровно на это, и кластеры
# совпадают с рестартами гейтвея до секунды (13.11 18:39:20 — 5 падений в трёх
# делах; 17.11 четыре рестарта, а в
# `boards/duty/workspaces/d_95f55146-check3-1/.duty/stderr.log` прямо
# `Error: runtime_draining`). Движок писал это `infra`-провалом этапа, и при
# `max=1` дело сразу парковалось вопросом человеку.
DRAINED = "drained"
# Маркер дренажа в stderr самой попытки: рантайм говорит им, что гасит процесс.
DRAIN_MARKERS = ("runtime_draining",)
# Сигнал, которым рестарт рантайма убивает попытку: `SIGINT` (номер 2 — из POSIX,
# чтобы модуль не тащил `signal` ради одного числа). `SIGTERM` здесь **не** назван
# намеренно: им же гасят попытку наш собственный таймаут и рука человека, и
# записывать такую смерть «рестартом» значило бы ослабить судейство `infra`
# (RP-14338: `check2#1` умер кодом 143, и это был честный инфра-отказ). Дренаж по
# `SIGTERM` узнаётся по маркеру в stderr — он и есть факт «гасит рантайм».
DRAIN_SIGNALS = (2,)
# Причина, которую видит лид и человек: рестарт называется рестартом.
DRAIN_TEXT = "попытку прервал рестарт рантайма"

# Вид этапа-гейта (`spec.GATE_KIND`): этап ничего не исполняет, он задаёт вопрос
# человеку и читает ответ. Имя вида нужно здесь, потому что политика повторов
# зависит от него (`policy`): отказ гейта — слово человека, а не форма отчёта.
GATE_KIND = "gate"

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

# Не класс политики, а факт журнала: попытку прервал рестарт рантайма. Имя нужно,
# потому что `kind` этого факта читают поверхности, и «неизвестный класс» на
# честной попытке читался бы как отказ работы. В `CLASS_TEXT` его нет: там имена
# ровно тех классов, которыми политика распоряжается (`test_class_names_are_one_per_class`).
DRAINED_TEXT = "перезапуск рантайма"

# Значение вне закрытого набора. Догадка («наверное, инфраструктура») врала бы про
# причину, поэтому неизвестный класс называется неизвестным.
UNKNOWN_CLASS_TEXT = "неизвестный класс"


def describe_class(kind: object) -> str:
    """Человеческое имя класса отказа по его слагу.

    Известный слаг получает имя из `CLASS_TEXT`, а `drained` — своё имя рядом с
    набором классов (`DRAINED_TEXT`): это факт журнала, а не класс политики, и
    держать его внутри `CLASS_TEXT` значило бы сломать правило «ключи `CLASS_TEXT`
    — ровно `CLASSES`». Любое другое значение — включая пустую строку, другой
    регистр и не-строку — `UNKNOWN_CLASS_TEXT`. Исключения не бросаются: класс
    приходит из данных (журнал, чужая доска), и падать на неожиданном значении
    значит валить срез из-за одного дела. Регистр и пробелы намеренно не
    прощаются: прощающий разбор сделал бы закрытый набор открытым сбоку.
    """
    if not isinstance(kind, str):
        return UNKNOWN_CLASS_TEXT
    if kind == DRAINED:
        return DRAINED_TEXT
    return CLASS_TEXT.get(kind, UNKNOWN_CLASS_TEXT)


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

# Политика этапа по умолчанию: четыре повторные попытки, и только на `infra`.
# Остальные классы без объявления не повторяются: `contract` называется в спеке
# поимённо, и тогда движок его повторяет.
#
# Число согласовано с `spec.DEFAULT_MAX_ATTEMPTS` (5): «попыток на этап» — это
# первая попытка плюс повторы, поэтому умолчание политики — 4. Иначе потолок
# хартии был бы недостижим: класс `infra` упирался в один повтор, и этап с
# исчерпанной серией уходил лиду на переосмысление вместо повтора на месте
# (живое дело d_a8213c01: две попытки `docs` убил дренаж гейтвея, а четвёртой
# попытки в том же этапе не было — лид заводил новый id `docs2`).
DEFAULT_MAX = 4
# Классы, которые движок повторяет **без объявления** в спеке. До 0.2.1 здесь был
# только `infra`, и живой журнал показал цену: из 54 парковок 44 пришли с
# `(max=1, on=[infra])`, где классом отказа был `contract` — то есть роль
# отчиталась не по форме, а второго шанса не было вовсе (петля «отчёт не по
# схеме → попытки исчерпаны → дело лиду», хотя схема лежит в контракте попытки).
# `AUTO_ON` всегда разрешал `contract`; объявление `retry.on` в плане сужало набор
# до одного `infra`, и лид этим набором сам себе закрывал повтор. Теперь `contract`
# — умолчание наравне с `infra`, а объявление в плане может его только убрать.
DEFAULT_ON = (INFRA, CONTRACT)

# Сколько повторов даёт класс `contract`. Больше одного не даём осознанно: причина
# такого отказа — форма отчёта, а схема и причина прошлого отказа уезжают в бриф
# следующей попытки (`build_contract` → `previous_failures`). Второй повтор того
# же класса означает, что роль не читает схему, и третий не поможет — а стоит он
# полного процесса и префикса. `infra` этим потолком не ограничен: там причина
# внешняя (`retry.max`, по умолчанию 4).
CONTRACT_MAX_RETRIES = 1

# Классы, которые движок повторяет сам (`docs/ENGINE-2.0.md`, раздел 7). Это
# политика **движка**, а не спеки: `retry.on` этапа набор сужает, но расширить не
# может. `capability` в наборе нет и не будет — стена (бюджет, доступ, роль)
# повтором не рассасывается, и за историю такие переходы дали 0 done / 2 failed.
AUTO_ON = (INFRA, CONTRACT)

# Общий предохранитель на дело: столько упавших попыток (по любому классу) — и
# дело паркуется, а не крутится. Равен двум исчерпанным сериям этапа
# (`2 × DEFAULT_MAX_ATTEMPTS`): предохранитель считает дело, а не серию, и при
# потолке в одну серию он парковал бы дело посреди законного повтора.
MAX_ROUNDS_PER_DEAL = 10  # две исчерпанные серии этапа по 5 попыток

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


def drained(exit_code: Optional[int], stderr: str = "") -> str:
    """Попытку прервал рестарт рантайма: причина или пусто.

    Два признака одного события и оба — факты, а не догадка по тексту: маркер
    дренажа в stderr попытки (`DRAIN_MARKERS`, закрытый список) и смерть от
    сигнала рестарта (`DRAIN_SIGNALS`). Разбор «на что похоже» здесь не заводится
    по той же причине, что у `toolchain_signature`.

    Наша собственная остановка по сроку сюда не попадает: `classify` проверяет
    `timed_out` раньше, а его класс — `infra`, как и был.
    """
    text = stderr if isinstance(stderr, str) else ""
    for marker in DRAIN_MARKERS:
        if marker in text:
            return f"{DRAIN_TEXT}: в stderr «{marker}»"
    number = signal_of(exit_code)
    if number in DRAIN_SIGNALS:
        return f"{DRAIN_TEXT}: процесс убит сигналом {number}"
    return ""


def budget_reason(timeout_sec: int, *, subject: str = "процесс не уложился") -> str:
    """Причина падения по часам: бюджет в минутах и откуда его брать.

    Одна формулировка на агента и на этап-команду: «упал по бюджету N мин» читается
    лидом как оценка бюджета, а не как ошибка работы (`docs/ENGINE-2.0.md`, раздел 5).
    Второй раз текст не пишется: разъехавшиеся копии научили бы лида двум разным
    вещам про одно и то же падение. `subject` — кто не уложился: процесс попытки
    или команда этапа.
    """
    minutes = int(timeout_sec) // 60 if int(timeout_sec) > 0 else 0
    # Меньше минуты — говорим секундами: «упал по бюджету 0 мин» читается как
    # отсутствие бюджета, а не как его нехватка.
    budget = f"{minutes} мин" if minutes else f"{int(timeout_sec)} с"
    return (
        f"упал по бюджету {budget}: {subject} в бюджет времени — оценивай бюджет по "
        "измеренной длительности работы: сборка и тесты по прошлым прогонам этого репо, "
        "браузерные и скриншотные этапы с запасом не меньше 2×"
    )


def classify(
    *,
    result: Optional[dict[str, Any]],
    result_error: str = "",
    exit_code: Optional[int] = None,
    timed_out: bool = False,
    over_budget: bool = False,
    timeout_sec: int = 0,
    stderr: str = "",
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
        # «Упал по часам» звучит как ошибка работы, а это наша оценка бюджета:
        # живое занижение — 1200 с у `shots` (`d_fe6c91d0`), 1500 с у `check`
        # (`d_76b269ce`), 1800 с у `impl` (`d_b9c480c8`). Поэтому причина
        # называет бюджет и подсказывает, откуда его брать.
        return Ended(OUTCOME_FAILED, INFRA, budget_reason(timeout_sec))
    if over_budget and result is None:
        return Ended(OUTCOME_FAILED, CAPABILITY, "бюджет токенов попытки исчерпан")
    if result is not None:
        status = str(result.get("status") or "done").strip().lower()
        if status in BLOCKED_STATUSES:
            return Ended(OUTCOME_DONE, "", f"попытка отчиталась статусом {status}: ждёт человека")
        if status in REFUSED_STATUSES:
            return Ended(OUTCOME_FAILED, CONTRACT, f"исполнитель отказался: status={status}")
        return Ended(OUTCOME_DONE, "", "")
    # Рестарт рантайма — не отказ работы: попытку не проиграли, её погасили.
    # Класс `drained` поэтому не списывает политику этапа (`decide_park`).
    restarted = drained(exit_code, stderr)
    if restarted:
        return Ended(OUTCOME_FAILED, DRAINED, restarted)
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

    `on` — **эффективный** набор: то, что осталось от объявленного `retry.on`
    после сужения политикой движка (`AUTO_ON`). Объявленный набор читается в
    `spec` и виден там как есть; повторять движок будет только это. Сужение —
    правило 0.2.0: `capability` движок не повторяет никогда, и объявить его
    повтором нельзя (`docs/ENGINE-2.0.md`, раздел 7).
    """

    max: int = DEFAULT_MAX
    on: tuple[str, ...] = DEFAULT_ON

    def allows(self, kind: str) -> bool:
        """Повторяется ли этот класс вообще (до счёта попыток).

        Два условия, и второе сильнее первого: класс назван в политике этапа **и**
        он вообще подлежит автоматическому повтору. Так `retry.on: [capability]`
        не расширяет набор, а сужает его до пустого — повторов не будет вовсе.
        """
        return kind in self.on and kind in AUTO_ON


def policy(stage_spec: Any) -> Policy:
    """Политика этапа из спеки; объявления нет — политика по умолчанию.

    `retry` в спеке не задан → `{max: 4, on: [infra, contract]}` (`DEFAULT_MAX`,
    `DEFAULT_ON`; у `contract` поверх этого действует `CONTRACT_MAX_RETRIES`, см.
    `docs/SPEC.md`). Отрицательный `max` читается как ноль: повторов не будет,
    а не «сколько-нибудь». Объявленные классы сужаются до `AUTO_ON`: набор
    повторов — политика движка (`docs/ENGINE-2.0.md`, раздел 7), спека может
    только отказаться от части, но не потребовать невозможного. Имена вне словаря
    `CLASSES` в набор не попадают: опечатку спека отвергает до запуска
    (`spec._check_retry`), а здесь чужое имя просто не стало бы повтором.
    """
    retry = getattr(stage_spec, "retry", None)
    kind = str(getattr(stage_spec, "kind", "") or "")
    if retry is None:
        # Гейт (`kind: gate`) не повторяется по контракту вовсе: его отказ — это
        # ответ ЧЕЛОВЕКА, прочитанный и отвергнутый (`allow`), а не форма отчёта.
        # Повтор прочитал бы тот же ответ из пула и отказал тем же словом: попытка
        # и деньги в никуда (tests/test_driver.py, «ответ вне allow»).
        if kind == GATE_KIND:
            return Policy(on=tuple(item for item in DEFAULT_ON if item != CONTRACT))
        return Policy()
    maximum = getattr(retry, "max", DEFAULT_MAX)
    declared = tuple(str(kind) for kind in (getattr(retry, "on", None) or ()))
    on = tuple(kind for kind in declared if kind in AUTO_ON)
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


def _class_limit(policy_: Policy, kind: str) -> int:
    """Сколько повторов даёт этот класс: `policy.max`, но `contract` — не больше одного.

    Потолок класса — не то же, что потолок этапа: `max` объявляет лид (по умолчанию
    4), а `contract` упирается в `CONTRACT_MAX_RETRIES` — форма отчёта, которую
    роль не прочитала в схеме, третьим повтором не лечится (`dashboard/failures.py`,
    `CONTRACT_MAX_RETRIES`).
    """
    limit = max(0, _int(policy_.max))
    if str(kind or "") == CONTRACT:
        return min(limit, CONTRACT_MAX_RETRIES)
    return limit


def retriable(policy_: Policy, kind: str, attempts: int) -> bool:
    """Есть ли ещё попытка для этого класса: класс объявлен и лимит не исчерпан.

    `attempts` — сколько отказов **этого** класса уже случилось (включая
    последний). `max: 2` значит «две повторные попытки», то есть отказ
    ретраится, пока `attempts <= max`; у `contract` потолок класса свой
    (`_class_limit`).
    """
    if not policy_.allows(kind):
        return False
    return _int(attempts) <= _class_limit(policy_, kind)


def limit_for(policy_: Policy, kind: str) -> int:
    """Сколько попыток даёт этот класс по политике: повторы класса или одна.

    Класс, которого нет в `on`, не повторяется вообще — и тогда честный предел
    «1» (одна попытка без повторов), а не `policy.max`: иначе экран показывал бы
    «1/4 contract» там, где второго шанса нет и не будет.
    """
    if not policy_.allows(str(kind or UNCLASSIFIED)):
        return 1
    return _class_limit(policy_, str(kind or UNCLASSIFIED))


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
    if str(kind or "") == DRAINED:
        # Рестарт рантайма прервал попытку: ни класс этапа, ни предохранитель на
        # дело тут не при чём — работу не проиграли, её погасили. Повтор поднимает
        # диспетчер (дело возвращается `ready`), а попытка политики не списывает.
        return None
    if _int(total) >= MAX_ROUNDS_PER_DEAL:
        return REASON_TOO_MANY
    if retriable(policy_, str(kind or UNCLASSIFIED), attempts):
        return None
    return REASON_RETRIES


def park_limit(reason: str, policy_: Policy, kind: str = "") -> int:
    """Лимит, который показать рядом со счётчиком: предохранитель или класс.

    Класс называется, потому что у `contract` свой потолок (`CONTRACT_MAX_RETRIES`):
    без него счётчик печатал бы «2/4 contract» на парковке, где второго шанса уже
    не будет.
    """
    if reason == REASON_TOO_MANY:
        return MAX_ROUNDS_PER_DEAL
    return _class_limit(policy_, kind)


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
    """Сколько попыток дела упало по любому классу — предохранитель на дело.

    Прерванные рестартом рантайма (`DRAINED`) не считаются: это не проигранные
    попытки, и предохранитель, считающий их, парковал бы дело за рестарты
    гейтвея — ровно то, чего 0.2.2 и не делает (`decide_park`).
    """
    return sum(count for kind, count in failures_of(events).items() if kind != DRAINED)


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
