"""Уборка осиротевших попыток: у попытки без конца появляется конец — `infra`.

Зачем это отдельный слой. `runner` пишет `attempt_ended` сам: он дожидается
процесса попытки и записывает факт о его смерти. Но движок может умереть
**раньше** попытки — `bin/duty run`, убитый снаружи, не дожидается ничего и не
пишет конец. В журнале остаётся `attempt_started` без пары, и дальше расходятся
две поверхности: срез (`journal.fold`, смотрит pid) говорит «crashed», переход
(`transit.stage_states`, только журнал) говорит «попытка идёт» — и закрыть её
некому, дело стоит вечно. Это тот же класс, что B1/B2 в 0.0.3, только наоборот:
факт в мире есть, а записи о нём нет.

Модуль пишет **недостающий факт**, а не подменяет состояние: `attempt_ended` с
`kind=infra` и `reaped=true`. После этого `fold` и `transit` сходятся сами —
потому что в журнале появилось то, чего им не хватало. Ни одного `if` в них для
этого схождения не нужно, и если бы понадобился — это значило бы, что здесь
пишется не тот факт.

## Правила

1. **Только по факту срока.** Живой лизинг этапа — не трогать, даже если pid не
   отвечает: движок может быть на другой машине или в другом namespace, а `ps`
   движку недоступен (`docs/PROBES.md`: журнал, не процессы). Живость pid здесь
   не спрашивается вовсе — иначе уборка решала бы по тому, чего не знает.
2. **Идемпотентность.** Наличие парного `attempt_ended` — единственный признак
   «попытка закрыта». Повторный `sweep` не пишет второй факт: пары ищутся по
   `run_id`, а ключ идемпотентности события — от того же `run_id`.
3. **Срок — из того же места, что у лизинга** (TTL попытки: `runner.ttl_sec`,
   `budget.minutes` спеки), с запасом `ttl + max(60 с, ttl/3)`: попытку, которая
   просто долго думает, уборка не прибивает. Спека берётся у самого дела —
   `created.payload.spec` в его журнале (`stagefeed.spec_of`), а не подставляется
   от соседнего дела: чужой `budget` — не срок этой попытки.
4. **Убираются только свои дела** (`d_<hex>`). Попытки наблюдаемых kanban-дел
   закрывает их собственное ядро: `attempt_started` из ingest — перенос чужого
   факта, и дописывать к нему наш конец значило бы врать про чужую доску.
5. **Класс `infra`.** Причина внешняя по отношению к контракту: мы знаем только,
   что отчёта нет. Политика ретраев (`dashboard/failures.py`) применяет
   `retry.on` как обычно — `infra` повторяется по умолчанию.
6. **`reaped: true`** в payload: в консоли и `bin/duty why` видно, что попытку
   закрыл движок, а не исполнитель. «Исполнитель упал» сказать нельзя — факта о
   его падении у нас нет.

## Чего модуль не делает

Не смотрит процессы (`ps`, `lsof`, `/proc`), не меняет схему `events`, не заводит
новых видов событий (`attempt_ended` уже есть), не трогает `fold` и `transit`, не
закрывает дело и не паркует его: уборка одного факта — один факт.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

# Модули плагина грузятся по пути и живут в `sys.modules` под общими именами:
# вторая загрузка означала бы вторую копию схемы и констант в одном процессе.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
DEALS_MODULE_NAME = "hermes_duty_deals"
SPEC_MODULE_NAME = "hermes_duty_spec"
RUNNER_MODULE_NAME = "hermes_duty_runner"
STAGEFEED_MODULE_NAME = "hermes_duty_stagefeed"


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
runner = _load(RUNNER_MODULE_NAME, "runner.py")
# Путь спеки дела читается тем же слоем, что рисует ленту этапов
# (`stagefeed.spec_of` — `created.payload.spec`): второго чтения «какая спека у
# дела» в плагине быть не должно.
stagefeed = _load(STAGEFEED_MODULE_NAME, "stagefeed.py")

# Источник факта: `engine`, а не `cli`/`api`. Уборку делает движок, и в журнале
# это видно тем же словом, что у остальных его фактов (`runner.ENGINE_SOURCE`).
SOURCE = runner.ENGINE_SOURCE

# Исход и класс закрытия: словарь один на плагин (`dashboard/failures.py`),
# сюда он приходит через движок, который его уже прочитал.
OUTCOME_FAILED = runner.OUTCOME_FAILED
FAIL_INFRA = runner.FAIL_INFRA

# Запас к TTL: `ttl + max(60 с, ttl/3)`. Меньше минуты запаса не бывает — при
# коротком бюджете (`budget.minutes: 1`) одна секунда форы не отличие «думает» от
# «умер».
GRACE_MIN_SEC = 60
GRACE_DIVISOR = 3


# --------------------------------------------------------------------- порог


def threshold_sec(ttl: int) -> int:
    """Порог закрытия попытки: TTL лизинга плюс запас `max(60 с, ttl/3)`.

    Считается от **возраста попытки**, а не от срока лизинга: лизинг мог
    продлеваться heartbeat'ом, и его `expires_at` говорит «когда истечёт», а не
    «сколько попытка уже живёт». Поэтому запаса два: срок лизинга и возраст — и
    уборка ждёт обоих (правило 3 карточки).
    """
    seconds = max(0, int(ttl))
    return seconds + max(GRACE_MIN_SEC, seconds // GRACE_DIVISOR)


def _ttl(spec: Any, stage_id: str) -> int:
    """TTL лизинга этапа — тот же `runner.ttl_sec`, что у самого лизинга.

    Спеки нет или этапа в ней нет — TTL по умолчанию (`DEFAULT_TTL_MIN`), и это
    не догадка о бюджете, а тот же срок, который взял бы лизинг без объявления
    `budget.minutes`. Подставлять сюда чужую спеку нельзя: `budget` другого дела
    сделал бы порог или короче, или длиннее, чем у этой попытки.
    """
    stage = None
    for item in getattr(spec, "stages", ()) or ():
        if item.id == stage_id:
            stage = item
            break
    return int(runner.ttl_sec(stage))


# ------------------------------------------------------------------- открытые


def open_attempts(events: Iterable[Any]) -> list[dict[str, Any]]:
    """Попытки дела без парного `attempt_ended` — по журналу, в порядке начала.

    Пара ищется по `run_id` — тем же признаком, что у `journal.fold`: начало и
    конец одной попытки называют один ран. Попытка без `run_id` пропускается:
    пару ей не найти, а выдуманный `run_id` связал бы её с чужой.

    Ран без `stage` — это перенос чужого факта (`journal.ingest` пишет
    `attempt_started` из `task_runs` наблюдаемой доски). Уборке он не подлежит:
    у чужой доски свой конец рана, и дописывать к нему наш — значит врать про
    доску, которую мы только читаем.
    """
    started: dict[str, dict[str, Any]] = {}
    for event in sorted(events or (), key=lambda item: int(item.get("id") or 0)):
        kind = str(event.get("kind") or "")
        if kind not in ("attempt_started", "attempt_ended"):
            continue
        payload = journal.payload_of(event)
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            continue
        if kind == "attempt_ended":
            started.pop(run_id, None)
            continue
        stage = payload.get("stage")
        if not isinstance(stage, str) or not stage:
            continue
        started[run_id] = {
            "run_id": run_id,
            "stage": stage,
            "attempt": _number(payload.get("attempt")),
            "pid": payload.get("pid"),
            "started_at": _number(event.get("ts")),
            "lease_expires": payload.get("lease_expires"),
            "event": event,
        }
    return list(started.values())


def _number(value: Any) -> int:
    """Целое из факта: `True` числом не считается, мусор — нулём."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------- уборка


def sweep(
    conn: Any, now: Any, *, spec: Any = None, dry_run: bool = False
) -> list[dict[str, Any]]:
    """Закрыть осиротевшие попытки своего store; вернуть, что закрыто (или было бы).

    `conn` — соединение своего store (`deals.open_store`): журнал и таблица
    лизингов живут в одной `duty.db`, и срок лизинга читается оттуда же, откуда
    его читает движок.

    `spec` — спека дела, которая у вызывающего уже в руках (драйвер её прочитал):
    она берётся, только если это спека **того же** дела (путь совпал с
    `created.payload.spec`). Иначе срок каждого дела считается по его собственной
    спеке из журнала, а нет её — по TTL по умолчанию.

    `dry_run` — посчитать и вернуть, ничего не записывая: `bin/duty reap
    --dry-run` должен показывать ровно то, что сделал бы настоящий проход.

    Журнал читается целиком: «только открытые попытки» нельзя взять одним запросом,
    не заведя вторую копию правила пары (`attempt_started` ↔ `attempt_ended` по
    `run_id`). Уборка — редкий проход (драйвер зовёт её раз на шаг, `reap` — руками),
    а строка журнала дешевле повторения правила: на живом store это сотни строк,
    а не сотни тысяч.
    """
    at = int(now)
    by_deal: dict[str, list[dict[str, Any]]] = {}
    for event in journal.read_events(conn):
        by_deal.setdefault(str(event.get("deal_id") or ""), []).append(event)

    specs: dict[str, Any] = {}
    out: list[dict[str, Any]] = []
    for deal_id in sorted(by_deal):
        if not deals.is_own(deal_id):
            continue  # наблюдаемое дело: его раны закрывает его собственное ядро
        attempts = open_attempts(by_deal[deal_id])
        if not attempts:
            continue
        deal_spec = _spec_of(by_deal[deal_id], spec=spec, cache=specs)
        for attempt in attempts:
            ttl = _ttl(deal_spec, attempt["stage"])
            if at - attempt["started_at"] < threshold_sec(ttl):
                continue  # попытка моложе порога: может, ещё думает
            # Лизинг читается тем же аксессором, что у движка: «жив ли этап» —
            # одно правило, и второй его копии здесь быть не должно.
            lease = runner.lease_of(conn, {"id": deal_id}, attempt["stage"])
            if lease is not None and int(lease["expires_at"]) > at:
                continue  # этап ведёт живой движок — не трогаем (правило 1)
            reason = (
                "процесс попытки не отчитался: лизинг истёк "
                f"{max(0, at - _expired_at(lease, attempt, ttl))}с назад"
            )
            payload = _ended_payload(attempt, reason)
            written = 0 if dry_run else _write(conn, attempt, payload, at)
            out.append(
                {
                    "deal": deal_id,
                    **payload,
                    "dry_run": bool(dry_run),
                    "written": written,
                }
            )
    return out


def _spec_of(events: list[dict[str, Any]], *, spec: Any, cache: dict[str, Any]) -> Any:
    """Спека дела: уже прочитанная (если это она) или своя из журнала дела.

    Путь берётся из `created.payload.spec` — единственное место, где записано,
    по какому графу дело заводилось. Нет пути — нет и спеки: TTL по умолчанию
    честнее чужого `budget`. Нечитаемый файл — тот же ответ: спека исчезла, а
    «срок неизвестен» не повод не убирать мёртвую попытку.
    """
    path = stagefeed.spec_of(events)
    if not path:
        return None
    if spec is not None and str(getattr(spec, "path", "")) == str(path):
        return spec
    if path not in cache:
        cache[path] = _load_spec(path)
    return cache[path]


def _load_spec(path: str) -> Any:
    """Спека по пути из журнала; `None` — файла нет или он не читается."""
    try:
        return spec_module.load_spec(Path(path).expanduser())
    except Exception:  # noqa: BLE001 - чужая спека не повод ронять уборку
        return None


def _expired_at(lease: Optional[dict[str, Any]], attempt: dict[str, Any], ttl: int) -> int:
    """Когда истёк срок лизинга этой попытки — по сроку, а не по «на глаз».

    Строка лизинга — текущий срок (её двигает heartbeat). Строки нет — срок,
    который движок записал в `attempt_started.lease_expires`: это тот же срок,
    только из журнала. Нет и его — срок, который взял бы лизинг по TTL этапа.
    """
    if lease is not None:
        return _number(lease.get("expires_at"))
    recorded = attempt["lease_expires"]
    if recorded is not None and not isinstance(recorded, bool):
        try:
            return int(recorded)
        except (TypeError, ValueError):
            pass
    return attempt["started_at"] + int(ttl)


def _ended_payload(attempt: dict[str, Any], reason: str) -> dict[str, Any]:
    """Payload события `attempt_ended` — ровно тот факт, который мы знаем.

    Ни `details`, ни `result`: у уборки нет ни отчёта попытки, ни кода выхода, и
    пустые поля выдали бы отсутствие факта за его значение. `pid` едет, только
    если он был записан при старте: `null` вместо него сказал бы «pid не было», а
    это разные факты.
    """
    payload: dict[str, Any] = {
        "run_id": attempt["run_id"],
        "stage": attempt["stage"],
        "attempt": attempt["attempt"],
        "outcome": OUTCOME_FAILED,
        "kind": FAIL_INFRA,
        "reason": reason,
        "reaped": True,
    }
    if attempt["pid"] is not None:
        payload["pid"] = attempt["pid"]
    return payload


def _write(conn: Any, attempt: dict[str, Any], payload: dict[str, Any], now: int) -> int:
    """Дописать `attempt_ended` и отпустить лизинг этой попытки — одной транзакцией.

    Ключ идемпотентности — от `run_id`, а не от времени: повторный проход того же
    факта даёт тот же ключ и `INSERT OR IGNORE` его отбрасывает. Это второй
    поясок поверх правила 2 (парного события нет — единственный признак), а не
    вместо него.

    Лизинг удаляется по номеру попытки, а не по паре «дело + этап»: строку мог
    уже перехватить следующий ран, и стирать её значило бы снять чужую защиту от
    двух движков на одном этапе.
    """
    event = {
        "ts": int(now),
        "recorded_at": int(now),
        "deal_id": str(attempt["event"].get("deal_id") or ""),
        "kind": "attempt_ended",
        "payload": payload,
        "source": SOURCE,
        "idempotency_key": deals.event_key(
            SOURCE,
            str(attempt["event"].get("deal_id") or ""),
            "attempt_ended",
            {"reaped": True, "run_id": attempt["run_id"]},
        ),
    }
    with conn:
        written = journal.append_in_tx(conn, [event])
        conn.execute(
            "DELETE FROM leases WHERE deal_id = ? AND stage = ? AND attempt = ?",
            (
                str(attempt["event"].get("deal_id") or ""),
                attempt["stage"],
                attempt["attempt"],
            ),
        )
    return int(written)


# ---------------------------------------------------------------- вход уборки


def reap(
    home: Path, *, dry_run: bool = False, now: Optional[int] = None
) -> dict[str, Any]:
    """Вход `bin/duty reap`: открыть свой store и убрать осиротевшие попытки.

    Store открывается **без создания файла**: уборка по несуществующему store —
    это «убирать нечего», а не «завёл пустую БД». Прохода по чужим доскам здесь
    нет и быть не может: `sweep` смотрит только свой журнал.
    """
    home = Path(home)
    at = int(time.time()) if now is None else int(now)
    store = deals.store_path(home)
    conn = deals.open_store(home, create=False)
    if conn is None:
        return {
            "store": str(store),
            "dry_run": bool(dry_run),
            "count": 0,
            "reaped": [],
            "note": f"store ещё нет: {store} — убирать нечего",
        }
    try:
        rows = sweep(conn, at, dry_run=dry_run)
    finally:
        conn.close()
    note = ""
    if dry_run and rows:
        note = "ничего не записано: --dry-run показывает, что закрыл бы проход"
    return {
        "store": str(store),
        "dry_run": bool(dry_run),
        "count": len(rows),
        "reaped": rows,
        "note": note,
    }
