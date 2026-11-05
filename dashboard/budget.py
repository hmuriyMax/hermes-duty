"""Бюджет дела целиком: расход по журналу и сверка с потолком `budget` спеки.

Зачем это отдельный слой. До 0.0.5 потолок был только у этапа: `budget.tokens`
сверялся с `usage` одной попытки (`dashboard/evidence.py`), а `budget.minutes`
держал срок лизинга (`dashboard/runner.py`). Дело из пяти этапов с ретраями
могло сжечь сколько угодно — потолок не видел никто, потому что каждый этап
укладывался в свой. Верхний `budget` в корне спеки (`docs/SPEC.md`) — это
потолок **на дело целиком**, суммарно по всем этапам и попыткам.

Границы слоя, они же причина, почему он отдельный:

- расход считается **только** по фактам журнала: `attempt_ended.details.usage_tokens`
  (телеметрия попытки, а если её нет — `result.usage.total_tokens` из отчёта,
  который лежит в том же событии) и разница `attempt_ended.ts - attempt_started.ts`.
  Счётчика в памяти процесса нет: он потерялся бы при рестарте, а бюджет обязан
  переживать рестарт — перезапуск движка продолжает то же дело с тем же расходом;
- `kind: shell` не тратит токенов, но тратит минуты: у его события нет
  `usage_tokens`, зато есть длительность попытки — она и попадает в сумму;
- незакрытая попытка не тратит ничего: её расход ещё не факт журнала. Это
  ограничение названо честно, а не замазано «примерно»: попытка, которая идёт
  дольше потолка минут, увидит свой расход только когда допишет `attempt_ended`,
  и следующую попытку тогда уже не поднимут;
- слою не нужны ни БД, ни процессы, ни часы: на вход — события и спека, на
  выход — числа и имя исчерпанного потолка (`spend`, `refusal`, `describe`).

Имя причины отказа — то же, что у класса `capability` в `dashboard/failures.py`:
стена, которую повтор не рассасывает. Решение «не запускать попытку» принимает
движок (`dashboard/runner.py`), а текст причины и числа берутся отсюда, чтобы
журнал, `bin/duty why` и `bin/duty deal` называли одни и те же числа.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

# Модули плагина грузятся по пути и живут в `sys.modules` под общими именами:
# вторая загрузка означала бы вторую копию констант и разбора событий.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
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
spec_module = _load(SPEC_MODULE_NAME, "spec.py")

# Потолки, которые бывают у дела: имена те же, что у полей `budget` в спеке.
TOKENS = "tokens"
MINUTES = "minutes"

# Единица причины: числа печатаются той единицей, которой объявлен потолок.
UNITS = {TOKENS: "токенов", MINUTES: "минут"}

SECONDS_IN_MINUTE = 60

ATTEMPT_STARTED = "attempt_started"
ATTEMPT_ENDED = "attempt_ended"


def _int(value: Any) -> Optional[int]:
    """Целое из факта журнала; `True` и строка без цифр — не число, а мусор."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    """`payload` события объектом: разбор тот же, что у журнала (мусор → `{}`)."""
    return journal.payload_of(event)


@dataclass(frozen=True)
class Spend:
    """Израсходовано делом: токены и секунды — сумма по всем закрытым попыткам."""

    tokens: int = 0
    seconds: int = 0
    attempts: int = 0

    @property
    def minutes(self) -> int:
        """Минуты целыми вниз: сравнение идёт по секундам, а печать — тем же числом."""
        return self.seconds // SECONDS_IN_MINUTE


@dataclass(frozen=True)
class Cap:
    """Исчерпанный потолок дела: какой именно и с какими числами.

    `used`/`limit` — в той единице, которую называет `kind`: токены или минуты.
    Причина для журнала собирается здесь же (`reason`), чтобы у движка и у
    поверхностей не было второго написания одной фразы.
    """

    kind: str
    used: int
    limit: int

    def reason(self) -> str:
        """Причина отказа словами: «бюджет дела исчерпан: N/M токенов» (или минут)."""
        return f"бюджет дела исчерпан: {self.used}/{self.limit} {UNITS.get(self.kind, self.kind)}"

    def as_payload(self) -> dict[str, Any]:
        """Тот же факт полем события: единица, расход и потолок — числами."""
        return {"kind": self.kind, "used": self.used, "limit": self.limit}


def _tokens_of(payload: dict[str, Any]) -> int:
    """Токены попытки: телеметрия движка, а если её нет — `usage` из отчёта.

    `details.usage_tokens` пишет сам движок по потоку (`dashboard/runner.py`), а
    `result.usage.total_tokens` — слово исполнителя в том же событии. Оба факта
    лежат в `attempt_ended`, поэтому выбор между ними не выдумывает число, а
    берёт то, которое записано; нет ни одного — ноль, и это тоже факт.
    """
    details = payload.get("details")
    details = details if isinstance(details, dict) else {}
    total = _int(details.get("usage_tokens"))
    if total is not None:
        return max(0, total)
    result = payload.get("result")
    usage = result.get("usage") if isinstance(result, dict) else None
    total = _int(usage.get("total_tokens")) if isinstance(usage, dict) else None
    return max(0, total or 0)


def _seconds_of(payload: dict[str, Any], *, started_at: Optional[int], ended_at: int) -> int:
    """Длительность попытки: разница двух фактов журнала, а без начала — `details`.

    `details.duration_sec` есть у `kind: shell` (движок мерил сам). Попытка без
    `attempt_started` (отказ до старта, как у гейта) времени не тратила, и ноль
    здесь — правда, а не заглушка.
    """
    if started_at is not None:
        return max(0, int(ended_at) - int(started_at))
    details = payload.get("details")
    details = details if isinstance(details, dict) else {}
    return max(0, _int(details.get("duration_sec")) or 0)


def spend(events: Iterable[Any]) -> Spend:
    """Расход дела по журналу: сумма закрытых попыток, ничего больше.

    Пара `attempt_started` ↔ `attempt_ended` ищется по `run_id` — той же парой,
    которой попытки читает лента (`dashboard/stagefeed.py`), и по тому же правилу:
    повтор начала не удваивает попытку, а конец без начала времени не тратит.
    События применяются в порядке `id`: журнал append-only, и порядок в нём —
    порядок фактов.
    """
    ordered = sorted(
        (item for item in events or () if isinstance(item, dict)),
        key=lambda item: _int(item.get("id")) or 0,
    )
    starts: dict[str, int] = {}
    tokens = 0
    seconds = 0
    attempts = 0
    for event in ordered:
        kind = str(event.get("kind") or "")
        if kind not in (ATTEMPT_STARTED, ATTEMPT_ENDED):
            continue
        payload = _payload(event)
        run_id = str(payload.get("run_id") or "")
        if kind == ATTEMPT_STARTED:
            if run_id and run_id not in starts:
                starts[run_id] = _int(event.get("ts")) or 0
            continue
        attempts += 1
        tokens += _tokens_of(payload)
        seconds += _seconds_of(
            payload,
            started_at=starts.get(run_id) if run_id else None,
            ended_at=_int(event.get("ts")) or 0,
        )
    return Spend(tokens=tokens, seconds=seconds, attempts=attempts)


def limit(spec: Any) -> Any:
    """Верхний `budget` спеки; `None` — потолка на дело нет (спека 0.0.4 и раньше).

    Никаких умолчаний на дело здесь нет намеренно: «умолчание» означало бы, что
    старая спека внезапно упёрлась в потолок, которого её автор не писал.
    """
    budget = getattr(spec, "budget", None)
    if budget is None:
        return None
    if getattr(budget, TOKENS, None) is None and getattr(budget, MINUTES, None) is None:
        return None
    return budget


def refusal(spec: Any, events: Iterable[Any]) -> Optional[Cap]:
    """Исчерпан ли потолок дела прямо сейчас: `Cap` или `None` — можно запускать.

    Порядок — токены, потом минуты: оба потолка независимы, и назвать один из них
    обязательно, а не «оба сразу». Потолок считается исчерпанным и тогда, когда
    расход его достиг ровно: следующая попытка этот потолок гарантированно
    пробьёт, а «потолок исчерпан» — это отсутствие места, а не перерасход.
    """
    cap = limit(spec)
    if cap is None:
        return None
    used = spend(events)
    tokens = getattr(cap, TOKENS, None)
    if tokens is not None and used.tokens >= int(tokens):
        return Cap(TOKENS, used.tokens, int(tokens))
    minutes = getattr(cap, MINUTES, None)
    if minutes is not None and used.seconds >= int(minutes) * SECONDS_IN_MINUTE:
        return Cap(MINUTES, used.minutes, int(minutes))
    return None


def text(used: Spend, cap: Any) -> str:
    """«израсходовано N из M токенов / X из Y минут» — строка для поверхностей.

    Называются только объявленные потолки: «из 0 минут» вместо отсутствующего
    потолка было бы выдумкой, из-за которой дело выглядело бы ограниченным.
    """
    parts: list[str] = []
    if getattr(cap, TOKENS, None) is not None:
        parts.append(f"{used.tokens} из {int(cap.tokens)} токенов")
    if getattr(cap, MINUTES, None) is not None:
        parts.append(f"{used.minutes} из {int(cap.minutes)} минут")
    return "израсходовано " + (" / ".join(parts) if parts else "—")


def describe(spec: Any, events: Iterable[Any]) -> Optional[dict[str, Any]]:
    """Блок `budget` для ответа роутов: расход, потолки и строка словами.

    `None` — потолка на дело нет: у поверхности тогда нечего показывать, и пустой
    блок с нулями выдавал бы отсутствие потолка за нулевой бюджет.
    """
    cap = limit(spec)
    if cap is None:
        return None
    used = spend(events)
    exhausted = refusal(spec, events)
    return {
        TOKENS: None
        if getattr(cap, TOKENS, None) is None
        else {"spent": used.tokens, "limit": int(cap.tokens)},
        MINUTES: None
        if getattr(cap, MINUTES, None) is None
        else {"spent": used.minutes, "limit": int(cap.minutes)},
        "text": text(used, cap),
        "attempts": used.attempts,
        "exhausted": None if exhausted is None else exhausted.kind,
    }


def spec_path(events: Iterable[Any]) -> Optional[str]:
    """Спека дела по журналу: `created.payload.spec` — та же правда, что у ленты."""
    for event in events or ():
        if not isinstance(event, dict) or str(event.get("kind") or "") != "created":
            continue
        raw = _payload(event).get("spec")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def describe_events(events: Iterable[Any]) -> Optional[dict[str, Any]]:
    """Блок `budget` по одним событиям: спека читается из журнала, файла — с диска.

    Нужно поверхностям (`/now`, `/deal`, `/attempts`): у них есть только события
    дела, а потолок объявлен в `duty.yaml`. Файл читается тем же `load_spec()`,
    что у движка; нет спеки или она не читается — `None`, потому что выдумывать
    потолок вместо нечитаемого файла значило бы приписать делу чужие границы.
    """
    events = list(events or ())
    path = spec_path(events)
    if not path:
        return None
    try:
        spec = spec_module.load_spec(Path(path).expanduser())
    except Exception:  # noqa: BLE001 - нечитаемая спека не должна валить поверхность
        return None
    return describe(spec, events)
