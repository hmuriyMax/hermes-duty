"""Адрес вопроса: кому он задан и почему — чистая функция, никакой модели.

Вопрос исполнителя — это не сообщение, а **требование входа**: без ответа дальше
работать нельзя. Поэтому у вопроса есть адрес, и адрес выбирает спека
детерминированно (`duty-model.html`, «Вопросы: кто отвечает и почему»). У адреса
три поставщика: роль (`ask.allowed`), человек (пул) и объявленный дефолт
(`to_human: never` + `default`). Этот модуль — резолвер: вход — объявление в
спеке и разметка вопроса, выход — `Target`. Ни БД, ни сети, ни процессов, ни
часов, ни модели: один и тот же вход всегда даёт один и тот же адрес.

Границы модуля:

- **никакой модели и никакого разбора текста**: класс вопроса («фактовый» /
  «властный») берётся только из явной разметки (`kind`, `risky`) и из
  объявлений спеки. Угадывание по словам вопроса — это и есть «координатор»,
  которого здесь нет;
- модуль ничего не пишет и никого не спрашивает: вопрос физически задаёт
  `dashboard/ask.py` (единственный канал пула), движок — `dashboard/runner.py`;
- адрес не зависит от того, ответили на вопрос или нет: срок ответа не
  превращает властный вопрос в решение (`docs/SPEC.md`, `ask`).

## Что чем решается (порядок — он же приоритет)

| # | условие | адрес | причина |
|---|---|---|---|
| 1 | `risky: true` в спеке **или** в вопросе | `human` | `risky` |
| 2 | `to_human: never` и объявленный дефолт | `spec_default:<значение>` | `declared_default` |
| 3 | вопрос размечен властным | `human` | `authority` |
| 4 | вопрос не размечен вовсе | `human` | `unmarked` (безопасная сторона) |
| 5 | роли нет: список пуст / только сам себя / незнакомая | `human_routing_failed` | `allowed_empty` / `only_self` / `unknown_role` |
| 6 | `hops_done >= max_hops` | `human` | `hops_exhausted` |
| 7 | фактовый вопрос → роль из `allowed` | `role:<имя>` | `declared_role` |

Почему `risky` выше дефолта: властный вопрос — публикация, мерж, скоуп, доступы,
приоритет — **никогда** не идёт по дефолту, и `to_human: never` этого не
отменяет: иначе объявление «человеку не спрашивать» молча решало бы за человека.
Почему «не размечен» уходит человеку: молчание разметки — не разрешение.

Откуда берётся объявленный дефолт: **спека** говорит, спрашивать ли человека
(`ask.to_human: never`), а **вопрос** несёт значение (`default` в `result.json`).
В спеке (`docs/SPEC.md`, W1) поля `ask.default` нет — формат объявляет адрес, а
не ответ; поэтому `spec_default` — это значение вопроса, разрешённое спекой.
Модель при этом не спрашивается: значение читается из объявленного поля, а не
выводится из текста.

`hops_done` считает **уже сделанные передачи** (у свежего вопроса 0), поэтому
`max_hops: 0` — «по цепочке не передавать вовсе, сразу человеку», а `max_hops: 2`
пропускает две передачи роли и третью отправляет человеку. Роль **не отвечает на
собственный вопрос**: своё имя из `allowed` выкидывается до выбора.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# --------------------------------------------------------------- виды адреса

ROLE = "role"
HUMAN = "human"
DEFAULT = "spec_default"
FAILED = "human_routing_failed"

TARGET_KINDS = (ROLE, HUMAN, DEFAULT, FAILED)

# ------------------------------------------------------------ причины решения

REASON_RISKY = "risky"
REASON_DEFAULT = "declared_default"
REASON_AUTHORITY = "authority"
REASON_UNMARKED = "unmarked"
REASON_HOPS = "hops_exhausted"
REASON_DECLARED = "declared_role"
REASON_ALLOWED_EMPTY = "allowed_empty"
REASON_ONLY_SELF = "only_self"
REASON_UNKNOWN_ROLE = "unknown_role"

# Закрытый набор причин: он попадает в журнал (`question_asked.payload.routing`),
# и по нему через месяц видно, почему вопрос ушёл этой роли, — это правило и
# дописывается в спеку.
REASONS = (
    REASON_RISKY,
    REASON_DEFAULT,
    REASON_AUTHORITY,
    REASON_UNMARKED,
    REASON_HOPS,
    REASON_DECLARED,
    REASON_ALLOWED_EMPTY,
    REASON_ONLY_SELF,
    REASON_UNKNOWN_ROLE,
)

# Разметка класса вопроса. Значения сравниваются в нижнем регистре; всё, чего
# нет в `FACT_KLASSES`, фактовым не считается (безопасная сторона).
FACT_KLASSES = ("fact", "factual", "факт", "фактовый")
AUTHORITY_KLASSES = ("authority", "властный")

# `to_human: never` — «этот этап вопросов человеку не задаёт» (`docs/SPEC.md`).
NEVER = "never"


@dataclass(frozen=True)
class Target:
    """Адрес вопроса: вид, значение и причина, по которой он такой.

    `kind` — один из `TARGET_KINDS`; `value` заполнен у `role` (имя роли) и у
    `spec_default` (значение объявленного дефолта); `reason` — код из `REASONS`,
    и он обязателен: адрес без причины нельзя ни проверить, ни объяснить.
    """

    kind: str
    value: str = ""
    reason: str = ""

    @property
    def address(self) -> str:
        """Адрес строкой — то, что читает человек в журнале и в панели."""
        if self.kind in (ROLE, DEFAULT):
            return f"{self.kind}:{self.value}"
        return self.kind

    @property
    def to_human(self) -> bool:
        """Вопрос уходит человеку (в пул) — в том числе когда адресат потерян."""
        return self.kind in (HUMAN, FAILED)

    def as_payload(self) -> dict[str, str]:
        """Форма для события: адрес и причина, словами контракта."""
        return {"target": self.address, "reason": self.reason}


def route(
    spec_stage: Any,
    question: Any,
    hops_done: int = 0,
    known_roles: Iterable[str] = (),
) -> Target:
    """Кому адресован вопрос и почему — без модели, по объявлениям.

    `spec_stage` — этап спеки (`spec.Stage`: берутся его `ask` и `role`) либо само
    объявление адреса (`spec.Ask`); `None` — объявления нет, и тогда вопрос
    считается властным. `question` — разметка вопроса из `result.json` (`kind`,
    `risky`, `default`) либо строка текста: строка не размечена, значит адрес —
    человек.

    `hops_done` — сколько передач по цепочке уже сделано; `known_roles` — какие
    роли существуют. Пустой `known_roles` значит «списка нет», а не «ролей нет»:
    тогда незнакомая роль не отбрасывается. Тотальность: мусор вместо вопроса или
    вместо объявления даёт `human`, а не исключение (правило 8 `transit`).
    """
    declared = _declared(spec_stage)
    marked = _marking(question)

    # 1. Властный флаг сильнее всего, включая объявленный дефолт: таймаут и
    #    «никогда не спрашивать человека» не превращают властный вопрос в решение.
    if bool(getattr(declared, "risky", False)) or marked["risky"]:
        return Target(HUMAN, reason=REASON_RISKY)

    # 2. Объявленный дефолт: спека сказала, что человеку этот вопрос не идёт.
    if _text(getattr(declared, "to_human", "")) == NEVER and marked["default"]:
        return Target(DEFAULT, value=marked["default"], reason=REASON_DEFAULT)

    # 3–4. Класс вопроса: фактовые — ролям, всё остальное — человеку.
    if marked["kind"] not in FACT_KLASSES:
        reason = REASON_AUTHORITY if marked["kind"] in AUTHORITY_KLASSES else REASON_UNMARKED
        return Target(HUMAN, reason=reason)

    # 5–6. Роль из объявленного списка: не своя и существующая. Сначала адресат,
    #    потом длина цепочки: пустой список — это `routing_failed` («адрес не
    #    назван»), и называть его «цепочка исчерпана» значило бы соврать.
    name, why = _pick(
        _names(getattr(declared, "allowed", ())),
        own=_text(getattr(spec_stage, "role", "")),
        known=_names(known_roles),
    )
    if not name:
        return Target(FAILED, reason=why)

    # 7. Цепочка конечна: передачи исчерпаны — дальше решает человек.
    if _int(hops_done) >= max(0, _int(getattr(declared, "max_hops", 0))):
        return Target(HUMAN, reason=REASON_HOPS)
    return Target(ROLE, value=name, reason=REASON_DECLARED)


def _declared(spec_stage: Any) -> Any:
    """Объявление адреса: `stage.ask`, сам `Ask` или `None`.

    Этап без `ask` адреса не объявил — это не то же самое, что пустой список
    ролей, но для адресации разницы нет: адрес всё равно не назван.
    """
    inner = getattr(spec_stage, "ask", None)
    if inner is not None:
        return inner
    if spec_stage is None or not hasattr(spec_stage, "allowed"):
        return None
    return spec_stage


def _marking(question: Any) -> dict[str, Any]:
    """Разметка вопроса: `kind`, `risky`, `default` — только объявленные поля.

    Строка вместо словаря — вопрос без разметки (`kind` пуст); неизвестный тип
    разметки — тоже. Текст вопроса здесь не читается вовсе: класс по словам —
    это модель, а её в адресации нет.
    """
    empty = {"kind": "", "risky": False, "default": ""}
    if not isinstance(question, dict):
        return empty
    return {
        "kind": _text(question.get("kind")),
        "risky": question.get("risky") is True,
        "default": _scalar(question.get("default")),
    }


def _pick(allowed: tuple[str, ...], own: str, known: tuple[str, ...]) -> tuple[str, str]:
    """Первая роль из `allowed`, которой можно адресовать вопрос, и причина отказа.

    Порядок — объявления в спеке (не алфавит и не догадка): спека называет
    адресатов в порядке предпочтения. Своё имя не годится — роль не отвечает на
    собственный вопрос; незнакомая роль (когда список существующих известен) —
    тем более. Причина отказа берётся от первого отброшенного адресата: она
    объясняет, почему объявленный список не дал адреса.
    """
    skipped = ""
    for name in allowed:
        if own and name == own:
            skipped = skipped or REASON_ONLY_SELF
            continue
        if known and name not in known:
            skipped = skipped or REASON_UNKNOWN_ROLE
            continue
        return name, ""
    return "", skipped or REASON_ALLOWED_EMPTY


def _names(raw: Any) -> tuple[str, ...]:
    """Список имён ролей из объявления: строки в нижнем регистре, мусор — мимо."""
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(_text(item) for item in raw if _text(item))


def _text(value: Any) -> str:
    """Строка в нижнем регистре без пробелов по краям; не строка — пусто."""
    return str(value).strip().lower() if isinstance(value, str) else ""


def _scalar(value: Any) -> str:
    """Значение объявленного дефолта строкой; объект дефолтом не считается."""
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return str(value).strip()
    return ""


def _int(value: Any) -> int:
    """Целое из объявления или из аргумента; `True` и мусор — ноль.

    Булево целым не считается (`max_hops: true` — не «один хоп»), а числовая
    строка считается: номер попытки и число хопов приходят и из JSON.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return 0
