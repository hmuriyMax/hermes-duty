"""Супервизор лида: сессия дела поднимается и переживает падение.

Контракт — `docs/ENGINE-2.0.md`, разделы 2 и 10. Роли три, и они разные:

1. **диспетчер** (`~/.hermes/scripts/duty-dispatcher.py`, cron раз в минуту) —
   будильник: на каждое незакрытое дело зовёт `bin/duty lead <id>`;
2. **супервизор** (`supervise`, то же `bin/duty lead`) — решает по журналу:
   лид жив → ничего; умер → поднять `--resume` с дайджестом; исчерпал резюмы →
   припарковать дело вопросом человеку;
3. **заход** (`run`, отдельный отсоединённый процесс) — берёт лизинг лида,
   поднимает `hermes chat -p lead` и пишет в журнал `lead_started`/`lead_exited`
   с pid, сессией, курсором и причиной выхода.

Почему заход — отдельный процесс, а не сама сессия модели. Сессию `hermes chat`
нельзя заставить написать факт о собственном конце: если модель убита (SIGKILL,
рестарт гейтвея, OOM), писать некому. Обёртка переживает смерть модели, потому
что ждёт её как дочерний процесс, и остаётся единственным писателем фактов лида.

Почему лизинг берётся на заход, а не на сессию. Взаимное исключение нужно **до**
спавна модели: два супервизора, увидевшие «лида нет» в один тик, иначе подняли бы
две сессии. Лизинг — та же строка `leases`, что у попыток (`docs/ENGINE-2.0.md`,
раздел 10: «двойной запуск гасится лизингом лида в журнале»), под зарезервированным
именем этапа `@lead`: `@` нет в именах этапов спеки, и строка лида не может
совпасть со строкой этапа.

Курсор сессии — `id` последнего факта, который сессия гарантированно видела: он
записывается в `lead_started.cursor` вместе с брифом или дайджестом. Дайджест
следующего резюма начинается с него, поэтому ни один факт не теряется. Обратная
цена названа честно: факты, которые лид прочитал сам через `duty_wait` (чтение в
журнал не пишется, `docs/ENGINE-2.0.md`, раздел 4), могут в дайджесте повториться.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, cast

# Модули плагина грузятся по пути и живут в `sys.modules` под общими именами:
# вторая загрузка означала бы вторую копию схемы, лизинга и правил бюджета.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
DEALS_MODULE_NAME = "hermes_duty_deals"
SPEC_MODULE_NAME = "hermes_duty_spec"
STAGEFEED_MODULE_NAME = "hermes_duty_stagefeed"
RUNNER_MODULE_NAME = "hermes_duty_runner"
ASK_MODULE_NAME = "hermes_duty_ask"
FAILURES_MODULE_NAME = "hermes_duty_failures"
BUDGET_MODULE_NAME = "hermes_duty_budget"


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
stagefeed = _load(STAGEFEED_MODULE_NAME, "stagefeed.py")
runner = _load(RUNNER_MODULE_NAME, "runner.py")
ask = _load(ASK_MODULE_NAME, "ask.py")
failures = _load(FAILURES_MODULE_NAME, "failures.py")
budget = _load(BUDGET_MODULE_NAME, "budget.py")

# Писатель фактов лида: это супервизор, а не модель. По источнику видно, что
# «лид поднят» сказал сторож, а решения по делу (`plan_accepted` и соседи) — сам
# лид (`dashboard/tools.SOURCE = "lead"`).
SOURCE = "supervisor"

# Виды фактов жизненного цикла лида (`docs/JOURNAL.md`, «Набор событий»).
LEAD_KIND_STARTED = "lead_started"
LEAD_KIND_EXITED = "lead_exited"

# Резервное имя лизинга лида в таблице `leases`: `@` в id этапа спеки не
# встречается, поэтому строка лида не совпадёт со строкой этапа.
LEAD_STAGE = "@lead"

# Профиль и скилл сессии лида (`docs/ENGINE-2.0.md`, раздел 2). Модель профиля не
# выбирается здесь: её задаёт `~/.hermes/profiles/lead/config.yaml`.
PROFILE = "lead"
SKILL = "project-delivery"

# Сколько живёт один заход лида и сколько сверх него держится лизинг: бюджет
# держит супервизор по часам — ребенок не умеет ограничивать себя сам, потому что
# `hermes chat` через живой гейтвей отказывает `--run-budget` (`docs/ENGINE-2.0.md`,
# раздел 11). Запас лизинга нужен, чтобы штатный выход не выглядел падением.
DEFAULT_RUN_BUDGET_SEC = 1800
LEASE_GRACE_SEC = 300

# Сколько раз супервизор поднимает упавшего лида (`charter.max_lead_resumes`).
DEFAULT_MAX_RESUMES = 6

# Режимы захода: первый (бриф) и продолжение (дайджест фактов после курсора).
MODE_FIRST = "brief"
MODE_RESUME = "resume"

# Телеметрия попытки — не факт о деле: в дайджесте ей нечего делать, иначе лид
# читал бы сотни строк «вызван инструмент» вместо «этап отчитался».
NOISE_KINDS = ("attempt_progress",)

# Сколько строк фактов дайджест показывает и сколько символов тела дела — бриф.
DIGEST_FACTS_LIMIT = 80
BRIEF_BODY_LIMIT = 2000

# Контракт движка: путь печатается в бриф и дайджест — сессия, поднятая после
# падения, обязана найти правила там же, где их видит человек.
ENGINE_DOC = Path(__file__).resolve().parents[1] / "docs" / "ENGINE-2.0.md"

# Каталог входов и логов лида: `<home>/duty/lead/` — тот же, где лежат логи
# попыток, поднятых инструментами лида.
LEAD_SUBDIR = "lead"

# Статусы, в которых дело человека ждёт: лида туда не поднимают (ответ человека
# проводит диспетчер, а не сессия лида).
NOT_LEAD_STATUSES = ("blocked", "review")

# Пауза между вопросами журнала: тот же шаг, что у ожидания фактов движка.
POLL_SEC = 0.2


class LeadError(RuntimeError):
    """Супервизор не может работать по причине ниже него самого: дела, спеки, лизинга."""


# ------------------------------------------------------------------ мелочи


def _int(value: Any) -> int:
    """Целое из факта: `True` и мусор — ноль, а не исключение."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _now(now: Any = None, clock: Optional[Callable[[], float]] = None) -> int:
    """Момент факта: аргумент (тесты), часы вызывающего или системные часы."""
    if now is not None:
        return int(now)
    return int((clock or time.time)())


def _short(text: Any, limit: int = 200) -> str:
    """Текст одной строкой: пробелы схлопнуты, хвост обрезан многоточием."""
    collapsed = " ".join(str(text or "").split())
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def _clock_of(ts: Any) -> str:
    """Метка времени для дайджеста: `ЧЧ:ММ` местного времени."""
    stamp = _int(ts)
    return time.strftime("%H:%M", time.localtime(stamp)) if stamp else "—"


def _age(ts: Any, now: int) -> str:
    """Возраст факта словами: дайджест читают, а не считают."""
    stamp = _int(ts)
    if not stamp:
        return "время не записано"
    seconds = max(0, int(now) - stamp)
    if seconds < 60:
        return f"{seconds}с назад"
    if seconds < 3600:
        return f"{seconds // 60}мин назад"
    return f"{seconds // 3600}ч назад"


def _event(
    deal: dict[str, Any], kind: str, payload: dict[str, Any], args: dict[str, Any], now: int
) -> dict[str, Any]:
    """Факт супервизора: ключ идемпотентности считается от аргументов.

    Формат ключа тот же, что у остальных писателей
    (`<source>:<deal_id>:<kind>:<sha1(аргументы)>`): повтор прохода по тому же
    состоянию даёт тот же ключ и второго факта не пишет.
    """
    return {
        "ts": now,
        "recorded_at": now,
        "deal_id": str(deal["id"]),
        "kind": kind,
        "payload": payload,
        "source": SOURCE,
        "idempotency_key": deals.event_key(SOURCE, str(deal["id"]), kind, args),
    }


def head(events: Iterable[Any]) -> int:
    """Курсор «всё прочитано»: `id` последнего факта дела (0 — журнал пуст)."""
    top = 0
    for event in events or ():
        if isinstance(event, dict):
            top = max(top, _int(event.get("id")))
    return top


# ------------------------------------------------------------ состояние лида


def state(
    events: Iterable[Any], *, now: Any = None, probe: Optional[Callable[[Any], Optional[bool]]] = None
) -> dict[str, Any]:
    """Состояние лида дела по журналу: сессия, курсор, счётчик резюмов, живость.

    Живость решает **pid**, а срок лизинга — второй сигнал:

    - pid жив (`probe` = `journal.pid_alive`) → лид жив, даже если сердцебиение
      лизинга отстало: убивать живую сессию из-за чужой забытой строки нельзя;
    - pid мёртв → лида нет, даже если лизинг ещё не истёк: иначе `kill` посреди
      дела не поднимал бы резюм до конца получасового лизинга;
    - pid неизвестен (`None` — чужая машина, нет процесса) → решает срок лизинга.

    `resumes` считается **от последнего `unblocked`**: ответ человека («продолжать»)
    открывает новый цикл, иначе дело, припаркованное на исчерпании резюмов,
    парковалось бы снова на первом же тике после ответа.
    """
    ordered = sorted(
        (event for event in events or () if isinstance(event, dict)),
        key=lambda event: _int(event.get("id")),
    )
    moment = _now(now)
    alive = probe or journal.pid_alive
    session = ""
    resumes = 0
    starts = 0
    cursor = 0
    last_fact_at = 0
    current: Optional[dict[str, Any]] = None
    last_exit: Optional[dict[str, Any]] = None
    for event in ordered:
        kind = str(event.get("kind") or "")
        payload = journal.payload_of(event)
        stamp = _int(event.get("ts"))
        last_fact_at = max(last_fact_at, stamp)
        if kind == "unblocked":
            # Человек сказал «продолжать»: цикл автоподъёмов начинается заново.
            resumes = 0
            continue
        if kind == LEAD_KIND_STARTED:
            starts += 1
            mode = str(payload.get("mode") or "")
            if mode == MODE_RESUME:
                resumes += 1
            found = str(payload.get("session_id") or "")
            if found:
                session = found
            cursor = _int(payload.get("cursor"))
            current = {
                "mode": mode,
                "pid": payload.get("pid"),
                "supervisor_pid": payload.get("supervisor_pid"),
                "started_at": stamp,
                "expires_at": payload.get("lease_expires"),
                "cursor": cursor,
                "session_id": found,
            }
            continue
        if kind == LEAD_KIND_EXITED:
            found = str(payload.get("session_id") or "")
            if found:
                session = found
            if current is not None:
                last_exit = {
                    "ts": stamp,
                    "reason": str(payload.get("reason") or ""),
                    "exit_code": payload.get("exit_code"),
                    "session_id": found or str(current.get("session_id") or ""),
                }
                current = None

    live = False
    if current is not None:
        seen = alive(current.get("pid"))
        supervisor_alive = alive(current.get("supervisor_pid")) is True
        expires = current.get("expires_at")
        lease_alive = expires is not None and moment < _int(expires)
        if seen is True:
            live = True
        elif seen is False:
            # Сессии уже нет, но обёртка захода жива: она вот-вот сама запишет
            # конец захода. Поднимать резюм в это окно (доли секунды) — значит
            # получить второй `lead_started` и второй факт о причине выхода.
            live = supervisor_alive
        else:
            live = lease_alive
    return {
        "session_id": session,
        "session_cursor": cursor,
        "starts": starts,
        "resumes": resumes,
        "open": current is not None,
        "live": live,
        "pid": (current or {}).get("pid"),
        "supervisor_pid": (current or {}).get("supervisor_pid"),
        "started_at": (current or {}).get("started_at"),
        "expires_at": (current or {}).get("expires_at"),
        "mode": (current or {}).get("mode", ""),
        "last_exit": last_exit,
        "last_fact_at": last_fact_at,
    }


def max_resumes(override: Any = None, *, spec: Any = None) -> int:
    """Потолок автоподъёмов лида: аргумент, `charter.max_lead_resumes`, 6.

    Поле хартии появляется вместе с 2.0-спекой (`docs/ENGINE-2.0.md`, раздел 3);
    спека 1.x его не объявляет, и тогда действует значение по умолчанию — как у
    остальных ключей хартии.
    """
    if override is not None:
        return max(0, _int(override))
    value = getattr(getattr(spec, "charter", None), "max_lead_resumes", None)
    return DEFAULT_MAX_RESUMES if value is None else max(0, _int(value))


# --------------------------------------------------------------------- спека


def _spec(events: Iterable[Any]) -> Optional[Any]:
    """Спека дела по журналу (`created.payload.spec`); нет или не читается — `None`.

    Второго источника «какая спека у дела» в плагине нет. Нечитаемый файл — не
    повод отказывать супервизору: бриф тогда скажет, что спеки нет, а лид
    разберётся по фактам.
    """
    path = stagefeed.spec_of(events)
    if not path:
        return None
    try:
        return spec_module.load_spec(Path(path).expanduser())
    except Exception:  # noqa: BLE001 - нечитаемая спека не валит супервизора
        return None


# ----------------------------------------------------------- текст для сессии


def _fact_line(event: dict[str, Any], now: int) -> str:
    """Один факт дела строкой: что произошло, чем кончилось, где отчёт.

    Формат — для чтения моделью: время, номер факта, вид и то, что этот вид
    значит по-русски. Незнакомый вид не выбрасывается: он печатается своими
    полями, потому что «журнал молчит о новом виде» хуже, чем строка без перевода.
    """
    kind = str(event.get("kind") or "")
    payload = journal.payload_of(event)
    stamp = f"#{_int(event.get('id'))} {_clock_of(event.get('ts'))} ({_age(event.get('ts'), now)})"
    if kind == "created":
        return f"{stamp} дело заведено: {_short(payload.get('title'))}"
    if kind == "claimed":
        return f"{stamp} взято в работу: {_short(payload.get('owner') or payload.get('claim_lock'))}"
    if kind == "released":
        return f"{stamp} дело отпущено движком: {_short(payload.get('reason'))}"
    if kind == "lease_lost":
        return f"{stamp} лизинг потерян (срок был {payload.get('previous_expires') or '—'})"
    if kind == "blocked":
        return f"{stamp} блок {payload.get('block_kind') or '—'}: {_short(payload.get('reason'))}"
    if kind == "unblocked":
        return f"{stamp} блок снят (был {payload.get('previous_kind') or '—'})"
    if kind == "completed":
        return f"{stamp} дело закрыто (причина: {payload.get('cause') or payload.get('status') or '—'})"
    if kind == "question_asked":
        routing = payload.get("routing") or {}
        target = routing.get("target") if isinstance(routing, dict) else ""
        number = payload.get("question_id")
        text = _short(payload.get("text") or "", 160)
        return f"{stamp} вопрос {f'#{number}' if number else '(роли)'} → {target or '—'}: {text}"
    if kind == "question_answered":
        return (
            f"{stamp} ответ на вопрос #{payload.get('question_id') or '—'} "
            f"({payload.get('by') or payload.get('answered_via') or '—'}, "
            f"{payload.get('answer_len') or 0} симв.)"
        )
    if kind == "attempt_started":
        return (
            f"{stamp} старт попытки {payload.get('stage')}#{payload.get('attempt')} "
            f"(pid {payload.get('pid') or '—'})"
        )
    if kind == "attempt_ended":
        details = payload.get("details") or {}
        line = (
            f"{stamp} попытка {payload.get('stage')}#{payload.get('attempt')}: "
            f"{payload.get('outcome') or '—'}"
        )
        if payload.get("kind"):
            line += f"/{payload.get('kind')}"
        line += f" — {_short(payload.get('reason') or 'причина не записана', 200)}"
        report = _report_path(details)
        if report:
            line += f"; отчёт: {report}"
        if payload.get("commit"):
            line += f"; коммит: {payload.get('commit')}"
        if details.get("tree_kept"):
            line += f"; сохранённое дерево: {details.get('tree_kept')}"
        tokens = details.get("usage_tokens")
        if tokens:
            line += f"; токенов: {tokens}"
        return line
    if kind == "review_requested":
        return f"{stamp} сдано в ревью: {payload.get('reviewer') or 'ревьюер не назначен'}"
    if kind == "collected":
        return f"{stamp} ревью одобрено: {_short(payload.get('summary') or payload.get('reason'))}"
    if kind == "changes_requested":
        return f"{stamp} возвращено на доработку: {_short(payload.get('reason'))}"
    if kind == "published":
        return f"{stamp} публикация: {payload.get('sha') or '—'} ({payload.get('branch') or 'ветка не названа'})"
    if kind == "patched":
        return f"{stamp} правка дела: {_short(payload.get('fields'))}"
    if kind == "commented":
        return f"{stamp} комментарий ({payload.get('author') or '—'}, {payload.get('len') or 0} симв.)"
    if kind == "attached":
        return f"{stamp} вложение: {_short(payload.get('filename'))}"
    if kind == "tree_reused":
        return f"{stamp} дерево прошлой попытки: {_short(payload.get('from'))}"
    if kind == "plan_proposed":
        stages = payload.get("stages") or []
        return (
            f"{stamp} план rev {payload.get('rev') or '—'}: этапов {len(stages)}, "
            f"причина: {_short(payload.get('reason'))}"
        )
    if kind == "plan_accepted":
        return f"{stamp} план rev {payload.get('rev') or '—'} принят ({payload.get('by') or '—'})"
    if kind == "plan_rejected":
        return (
            f"{stamp} план rev {payload.get('rev') or '—'} отклонён: {_short(payload.get('why'))}; "
            f"отказы: {'; '.join(str(item) for item in (payload.get('issues') or [])[:6])}"
        )
    if kind == "plan_gate":
        return (
            f"{stamp} план rev {payload.get('rev') or '—'} ждёт человека "
            f"(вопрос #{payload.get('question_id') or '—'}): {_short(payload.get('reason'))}"
        )
    if kind == "plan_rethink":
        return (
            f"{stamp} этап {payload.get('stage') or '—'} упал {payload.get('kind') or '—'} — "
            f"диагноз лиду: {_short(payload.get('why'))}"
        )
    if kind == LEAD_KIND_STARTED:
        return (
            f"{stamp} лид поднят ({payload.get('mode') or '—'}, pid {payload.get('pid') or '—'}, "
            f"курсор {payload.get('cursor') or 0})"
        )
    if kind == LEAD_KIND_EXITED:
        return (
            f"{stamp} лид вышел: {payload.get('reason') or '—'} "
            f"(код {payload.get('exit_code') if payload.get('exit_code') is not None else '—'})"
        )
    if kind in ("gate_opened", "gate_closed"):
        return f"{stamp} гейт родителей: {payload.get('from') or '—'} → {payload.get('to') or '—'}"
    pairs = ", ".join(f"{key}={_short(value, 60)}" for key, value in list(payload.items())[:6])
    return f"{stamp} {kind}: {pairs}" if pairs else f"{stamp} {kind}"


def _report_path(details: Any) -> str:
    """Путь отчёта попытки (`result.json` в её рабочем каталоге) или пустая строка.

    Дайджест отдаёт **пути**, а не тексты отчётов (`docs/ENGINE-2.0.md`,
    раздел 11): транскрипт лида растёт с каждым событием, и пересказ отчётов в
    нём стоил бы дороже самой работы.
    """
    if not isinstance(details, dict):
        return ""
    workdir = str(details.get("workdir") or "")
    if not workdir:
        return ""
    return str(runner.result_path(Path(workdir)))


def _facts_lines(events: list[dict[str, Any]], cursor: int, now: int) -> tuple[list[str], int]:
    """Строки фактов дела после курсора; второе значение — сколько не поместилось."""
    fresh = [
        event
        for event in events
        if _int(event.get("id")) > int(cursor) and str(event.get("kind") or "") not in NOISE_KINDS
    ]
    omitted = max(0, len(fresh) - DIGEST_FACTS_LIMIT)
    shown = fresh[omitted:]
    return [_fact_line(event, now) for event in shown], omitted


def _attempts_lines(events: list[dict[str, Any]], now: int) -> list[str]:
    """Состояние попыток дела: этап, номер, исход, класс, живость, отчёт."""
    rows = stagefeed.attempts(events, now)
    out: list[str] = []
    for row in rows:
        line = f"- {row.get('stage')}#{row.get('n')}: {row.get('outcome') or 'идёт'}"
        if row.get("kind"):
            line += f"/{row.get('kind')}"
        if row.get("live") is True:
            line += " (живая)"
        elif row.get("dead") is True:
            line += " (процесс мёртв)"
        if row.get("reason"):
            line += f" — {_short(row.get('reason'), 160)}"
        if row.get("tokens"):
            line += f"; токенов {row.get('tokens')}"
        report = _report_path({"workdir": (row.get("workdir") or "")})
        if report:
            line += f"; отчёт: {report}"
        out.append(line)
    return out


def _budget_line(spec: Any, events: list[dict[str, Any]]) -> str:
    """Остаток бюджета дела словами — тем же расчётом, что у поверхностей.

    Потолка нет или спека не читается — так и говорится: ноль вместо «не
    объявлено» лид прочитал бы как «бюджет кончился».
    """
    if spec is None:
        return "потолок бюджета неизвестен: спека дела не читается"
    block = budget.describe(spec, events)
    if block is None:
        return "потолка на дело нет (верхний `budget` в спеке не объявлен)"
    tokens = block.get("tokens") or {}
    minutes = block.get("minutes") or {}
    parts = []
    if tokens:
        parts.append(f"токенов {tokens.get('spent')} из {tokens.get('limit')} (остаток {tokens.get('limit', 0) - tokens.get('spent', 0)})")
    if minutes:
        parts.append(f"минут {minutes.get('spent')} из {minutes.get('limit')} (остаток {minutes.get('limit', 0) - minutes.get('spent', 0)})")
    text = "; ".join(parts) or "потолок объявлен без чисел"
    if block.get("exhausted"):
        text += f"; бюджет исчерпан ({block['exhausted']})"
    return text


def _head_block(deal: dict[str, Any], st: dict[str, Any], spec: Any, resume_limit: int) -> list[str]:
    """Шапка входа сессии: адрес дела, состояние лида, спека, контракт движка."""
    lines = [
        f"# Дело {deal['id']}: {_short(deal.get('title') or 'без заголовка', 200)}",
        "",
        f"- статус дела: {deal.get('status') or '—'}; репозиторий: {deal.get('repo') or '—'}",
        f"- спека: {deal.get('spec') or (getattr(spec, 'path', '') if spec is not None else '') or 'не записана'}",
        f"- лид: сессия {st.get('session_id') or 'ещё нет'}, "
        f"подъёмов {st.get('starts') or 0}, резюмов {st.get('resumes') or 0} из {resume_limit}",
    ]
    last_exit = st.get("last_exit") or {}
    if last_exit:
        lines.append(
            f"- прошлый заход лида: {last_exit.get('reason') or '—'} "
            f"(код {last_exit.get('exit_code')}, {_clock_of(last_exit.get('ts'))})"
        )
    lines.append(f"- контракт движка: {ENGINE_DOC}")
    return lines


def digest(
    deal: dict[str, Any],
    events: list[dict[str, Any]],
    st: dict[str, Any],
    *,
    spec: Any = None,
    resume_limit: int = DEFAULT_MAX_RESUMES,
    now: Any = None,
    run_budget: Optional[int] = None,
) -> str:
    """Дайджест резюма: факты дела после курсора сессии, пути отчётов, попытки, бюджет.

    Дайджест — не пересказ дела, а «что случилось без тебя»: сессия помнит всё до
    своего курсора, и повторять ей историю значило бы платить за неё второй раз
    (`docs/ENGINE-2.0.md`, раздел 11).
    """
    moment = _now(now)
    cursor = _int(st.get("session_cursor"))
    lines = _head_block(deal, st, spec, resume_limit)
    lines.append(f"- бюджет дела: {_budget_line(spec, events)}")
    if run_budget:
        lines.append(
            f"- бюджет этого захода: {int(run_budget)} с, после чего сторож остановит заход и поднимет резюм"
        )
    lines += ["", f"## Что произошло после курсора сессии ({cursor})"]
    facts, omitted = _facts_lines(events, cursor, moment)
    if omitted:
        lines.append(f"(ещё {omitted} фактов старше — они в журнале: `bin/duty why {deal['id']}`)")
    lines.extend(facts or ["(новых фактов нет: сессию подняли по сторожу, а не по событию)"])
    attempts = _attempts_lines(events, moment)
    if attempts:
        lines += ["", "## Попытки", *attempts]
    lines += [
        "",
        "## Что делать",
        "- продолжай дело с того места, где остановился: факты выше — всё, чего ты не видел;",
        "- этапы поднимай инструментами `duty` (`duty_plan`, `duty_start`, `duty_wait`, `duty_close`),",
        "  отчёты бери путями, а не пересказом: полный текст — в файле;",
        "- если ждёшь работу — `duty_wait` с курсором, это дешевле опроса;",
        "- если дело надо остановить или спросить человека — это `duty_ask_human` и `duty_close`.",
    ]
    return "\n".join(lines) + "\n"


def brief(
    deal: dict[str, Any],
    events: list[dict[str, Any]],
    st: dict[str, Any],
    *,
    spec: Any = None,
    resume_limit: int = DEFAULT_MAX_RESUMES,
    now: Any = None,
    run_budget: Optional[int] = None,
) -> str:
    """Бриф первого запуска: дело, хартия, seed-план, вся история и правила.

    Первый заход — единственный, где лид видит дело целиком: дальше он получает
    только то, чего не видел. Спека 1.x со `stages` приходит сюда **подсказкой**
    (seed-план), а не готовым графом (`docs/ENGINE-2.0.md`, раздел 3).
    """
    moment = _now(now)
    lines = _head_block(deal, st, spec, resume_limit)
    lines.append(f"- бюджет дела: {_budget_line(spec, events)}")
    if run_budget:
        lines.append(
            f"- бюджет этого захода: {int(run_budget)} с, после чего сторож остановит заход и поднимет резюм"
        )
    body = _short(deal.get("body") or "", BRIEF_BODY_LIMIT)
    lines += ["", "## Что за дело", body or "(тело дела не записано)"]
    if spec is not None:
        charter = getattr(spec, "charter", None)
        stages = list(getattr(spec, "stages", ()) or ())
        lines += ["", "## Хартия и seed-план"]
        if charter is not None:
            lines.append(
                f"- потолок плана: {getattr(charter, 'max_stages', DEFAULT_MAX_RESUMES)} этапов; "
                f"роли: {', '.join(getattr(charter, 'allowed_roles', ()) or ()) or 'не ограничены'}; "
                f"виды: {', '.join(getattr(charter, 'allowed_kinds', ()) or ()) or '—'}"
            )
        for stage in stages:
            needs = ", ".join(getattr(stage, "needs", ()) or ()) or "—"
            lines.append(
                f"- {stage.id}: роль {getattr(stage, 'role', '—')}, вид {getattr(stage, 'kind', 'agent')}, "
                f"needs: {needs}"
            )
        lines.append(
            "- seed-план — подсказка «как дело задумывали», а не приговор: план создаёшь ты "
            "(`duty_plan`), и вправе его переписать."
        )
    facts, omitted = _facts_lines(events, 0, moment)
    lines += ["", "## История дела"]
    if omitted:
        lines.append(f"(ещё {omitted} фактов старше — они в журнале: `bin/duty why {deal['id']}`)")
    lines.extend(facts or ["(журнал пуст: дело только заведено)"])
    lines += [
        "",
        "## Что делать",
        "- прочитай дело и его спеку, оцени скоуп и предложи план: `duty_plan(stages, reason, idempotency_key)`;",
        "- этапы запускай `duty_start(stage_id, idempotency_key)`; работу жди `duty_wait(cursor, timeout_sec)`;",
        "- вопросы воркеров приходят фактами в `duty_wait`: отвечай `duty_answer`, делегируй `duty_ask`,",
        "  властное — `duty_ask_human`;",
        "- закрывай дело сам: `duty_close(summary, verdict, idempotency_key)`;",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- команда


def command(mode: str, *, session_id: str, query_file: Path) -> list[str]:
    """Команда захода: ровно те ключи, что сверены с `hermes chat --help`.

    Первый заход поднимает скилл `project-delivery` (`-s`) — он и есть правила
    дела; резюм получает уже ту же сессию со скиллом в контексте, поэтому `-s` не
    повторяется (`docs/ENGINE-2.0.md`, раздел 2). `--format stream-json` нужен не
    для человека, а для факта: из потока берётся `session_id`, без которого
    резюм невозможен.

    Бюджета захода в команде нет намеренно: `hermes chat` через живой гейтвей
    отказывает `--run-budget` (он переехал в `agent.run_budget_seconds`, а это
    настройка процесса, а не сессии). Держит бюджет сам супервизор: он ждёт
    ребёнка по часам и на `overrun` останавливает его так же, как по выходу, —
    факт `lead_exited(overrun)` и резюм.
    """
    cmd = [runner.HERMES_BIN, "chat", "-p", PROFILE]
    if mode == MODE_FIRST:
        cmd.extend(["-s", SKILL])
    if mode == MODE_RESUME:
        if not session_id:
            raise LeadError("resume: session_id не назван — продолжать нечего, это первый заход")
        cmd.extend(["--resume", str(session_id)])
    cmd.extend(["--query-file", str(query_file), "--format", "stream-json", "--oneshot"])
    return cmd


def command_line(mode: str, *, session_id: str, query_file: Path) -> str:
    """Команда захода строкой — для отчёта карточки и разбора «чем поднимали»."""
    return " ".join(command(mode, session_id=session_id, query_file=query_file))


# ------------------------------------------------------------------ файлы


def lead_dir(home: Path) -> Path:
    """Каталог входов лида: `<home>/duty/lead/`."""
    return journal.journal_path(Path(home)).parent / LEAD_SUBDIR


def query_path(home: Path, deal_id: str, mode: str, now: int) -> Path:
    """Файл входа сессии: бриф или дайджест. Имя с меткой времени — история видна."""
    return lead_dir(home) / f"{deal_id}-{mode}-{int(now)}.txt"


def log_path(home: Path, deal_id: str, now: int) -> Path:
    """Лог захода: stdout/stderr обёртки, когда модель/спавн не поднялись."""
    return lead_dir(home) / f"{deal_id}-supervisor-{int(now)}.log"


def write_query(path: Path, text: str) -> Path:
    """Записать вход сессии на диск: процесс читает файл, а не аргументы."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------- лизинг


def _take_lease(
    conn: sqlite3.Connection,
    deal_id: str,
    *,
    attempt: int,
    pid: int,
    ttl: int,
    now: int,
    stale_pid: Optional[int] = None,
) -> bool:
    """Взять лизинг лида атомарно; `False` — заход уже за живым процессом.

    Одна транзакция и одно условие в SQL: строка свободна, если её нет, если срок
    истёк **или** если её держит заведомо мёртвый процесс (`stale_pid` — pid
    прошлого захода, про который `journal.pid_alive` сказал «нет»). Второго
    условия у попыток нет, потому что у них мёртвую строку закрывает `reaper`;
    у лида `reaper` нет, а ждать конца получасового лизинга после `kill` нельзя.
    """
    cursor = conn.execute(
        "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(deal_id, stage) DO UPDATE SET "
        "attempt = excluded.attempt, pid = excluded.pid, taken_at = excluded.taken_at, "
        "expires_at = excluded.expires_at "
        "WHERE leases.expires_at <= ? OR (? IS NOT NULL AND leases.pid = ?)",
        (deal_id, LEAD_STAGE, int(attempt), int(pid), now, now + int(ttl), now, stale_pid, stale_pid),
    )
    return int(cursor.rowcount) == 1


def lease_of(conn: sqlite3.Connection, deal_id: str) -> Optional[dict[str, Any]]:
    """Строка лизинга лида или `None`."""
    return runner.lease_of(conn, {"id": deal_id}, LEAD_STAGE)


# ------------------------------------------------------------------ заход


def _line_object(line: str) -> Optional[dict[str, Any]]:
    """Строка потока объектом; не JSON или не объект — мусор, а не факт."""
    try:
        data = json.loads(line)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run(
    home: Path,
    deal_id: str,
    *,
    run_budget: Optional[int] = None,
    now: Any = None,
    clock: Optional[Callable[[], float]] = None,
    monotonic: Optional[Callable[[], float]] = None,
    spawn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Один заход лида: лизинг, вход сессии, процесс, факты начала и конца.

    Заход — отдельный процесс (`python dashboard/lead.py run …`), поэтому он
    единственный, кто пишет `lead_started`/`lead_exited`: модель о своей смерти
    факта не оставит. Порядок ровно как у попытки (`runner.start_stage`):
    сначала взаимное исключение, потом процесс, потом факт с его pid.
    """
    home = Path(home).expanduser()
    budget_sec = DEFAULT_RUN_BUDGET_SEC if run_budget is None else max(1, _int(run_budget))
    clock = clock or time.time
    monotonic = monotonic or time.monotonic
    spawner = spawn or runner.spawn_process
    moment = _now(now, clock)

    conn = deals.open_store(home)
    if conn is None:  # pragma: no cover - `open_store` создаёт файл
        raise LeadError(f"store is unavailable under {home}")
    try:
        deal = _deal(conn, deal_id)
        events = journal.read_events(conn, str(deal["id"]))
        st = state(events, now=moment)
        mode = MODE_RESUME if st["session_id"] else MODE_FIRST
        cursor = _int(st["session_cursor"]) if mode == MODE_RESUME else 0
        spec = _spec(events)
        resume_limit = max_resumes(None, spec=spec)

        ttl = budget_sec + LEASE_GRACE_SEC
        attempt = int(st["starts"]) + 1
        held = lease_of(conn, str(deal["id"]))
        stale = None
        if held is not None and journal.pid_alive(held.get("pid")) is False:
            stale = _int(held.get("pid"))
        with conn:
            taken = _take_lease(
                conn,
                str(deal["id"]),
                attempt=attempt,
                pid=os.getpid(),
                ttl=ttl,
                now=moment,
                stale_pid=stale,
            )
        if not taken:
            return {
                "deal": str(deal["id"]),
                "started": False,
                "reason": "лид уже ведёт дело: лизинг занят живым процессом",
            }

        path = write_query(
            query_path(home, str(deal["id"]), mode, moment),
            (
                brief(deal, events, st, spec=spec, resume_limit=resume_limit, now=moment, run_budget=budget_sec)
                if mode == MODE_FIRST
                else digest(deal, events, st, spec=spec, resume_limit=resume_limit, now=moment, run_budget=budget_sec)
            ),
        )
        cmd = command(mode, session_id=str(st["session_id"]), query_file=path)
        try:
            process = spawner(cmd, Path(home))
        except OSError as exc:
            runner.release(conn, deal=deal, stage=LEAD_STAGE, pid=os.getpid())
            raise LeadError(f"спавн сессии лида не удался: {exc}") from exc

        child = int(process.pid)
        started_payload: dict[str, Any] = {
            "mode": mode,
            "pid": child,
            "supervisor_pid": os.getpid(),
            "session_id": str(st["session_id"]) or None,
            "cursor": head(events),
            "resumed_from": cursor if mode == MODE_RESUME else None,
            "run_budget": budget_sec,
            "resumes": _int(st["resumes"]) + (1 if mode == MODE_RESUME else 0),
            "cmd": cmd,
            "query_path": str(path),
            "lease_expires": moment + ttl,
        }
        journal.append(
            conn,
            [
                _event(
                    deal,
                    LEAD_KIND_STARTED,
                    started_payload,
                    {"mode": mode, "pid": child, "attempt": attempt},
                    moment,
                )
            ],
        )
        # Лизинг переезжает к процессу модели: именно его pid отвечает на вопрос
        # «лид жив?», и именно его гасят, если заход завис.
        if not runner.hand_over(
            conn, deal=deal, stage=LEAD_STAGE, from_pid=os.getpid(), pid=child, attempt=attempt, ttl=ttl, now=moment
        ):
            process.stop()
            return {
                "deal": str(deal["id"]),
                "started": False,
                "reason": "лизинг лида забрал другой процесс: заход остановлен",
                "pid": child,
            }

        session_id = str(st["session_id"])
        tokens = 0
        reason = "exit"
        deadline = monotonic() + budget_sec + LEASE_GRACE_SEC
        renew_every = max(ttl / 3.0, runner.POLL_SEC)
        last_renew = monotonic()
        while True:
            if monotonic() - last_renew >= renew_every:
                runner.renew(
                    conn, deal=deal, stage=LEAD_STAGE, pid=child, ttl=ttl, now=_now(None, clock)
                )
                last_renew = monotonic()
            line = process.next_line(POLL_SEC)
            if line is None:
                if process.finished():
                    break
                if monotonic() >= deadline:
                    reason = "overrun"
                    process.stop()
                    break
                continue
            data = _line_object(line)
            if data is None or str(data.get("type") or "") != "result":
                continue
            found = data.get("session_id")
            if isinstance(found, str) and found:
                session_id = found
            counted = data.get("tokens")
            if isinstance(counted, dict) and _int(counted.get("total")):
                tokens += _int(counted.get("total"))

        # Хвост очереди: последний `result` (с id сессии и расходом) мог остаться
        # в буфере ровно на выходе процесса.
        while True:
            line = process.next_line(0)
            if line is None:
                break
            data = _line_object(line)
            if data is not None and str(data.get("type") or "") == "result":
                found = data.get("session_id")
                if isinstance(found, str) and found:
                    session_id = found
                counted = data.get("tokens")
                if isinstance(counted, dict) and _int(counted.get("total")):
                    tokens += _int(counted.get("total"))

        exit_code = process.exit_code
        if exit_code is None:
            exit_code = process.wait(5)
        if reason == "exit" and exit_code not in (0, None):
            reason = "crashed"
        finished_at = _now(None, clock)
        fresh = journal.read_events(conn, str(deal["id"]))
        # Хвост stderr — свойство `runner.Process`: последние строки вывода.
        raw_tail = cast("list[Any]", getattr(process, "stderr_tail", []))
        tail = [str(line) for line in raw_tail or []][-5:]
        journal.append(
            conn,
            [
                _event(
                    deal,
                    LEAD_KIND_EXITED,
                    {
                        "mode": mode,
                        "pid": child,
                        "reason": reason,
                        "exit_code": exit_code,
                        "session_id": session_id or None,
                        "cursor": head(fresh),
                        "usage_tokens": tokens or None,
                        "duration_sec": max(0, finished_at - moment),
                        "stderr_tail": tail,
                    },
                    {"mode": mode, "pid": child, "exit": exit_code, "reason": reason},
                    finished_at,
                )
            ],
        )
        runner.release(conn, deal=deal, stage=LEAD_STAGE, pid=child)
        return {
            "deal": str(deal["id"]),
            "started": True,
            "mode": mode,
            "pid": child,
            "session_id": session_id,
            "cursor": _int(st["session_cursor"]),
            "reason": reason,
            "exit_code": exit_code,
            "usage_tokens": tokens,
            "query_path": str(path),
            "cmd": cmd,
        }
    finally:
        conn.close()


def _deal(conn: sqlite3.Connection, deal_id: Any) -> dict[str, Any]:
    """Строка своего дела; `t_…`, мусор и отсутствие строки — отказ словами."""
    try:
        row = deals.deal_row(conn, str(deal_id or ""))
    except deals.DealError as exc:
        raise LeadError(str(exc)) from exc
    if row is None:
        raise LeadError(f"дела {deal_id!r} нет в store: супервизор ведёт свои дела d_<hex>")
    return row


# -------------------------------------------------------------- супервизор


def _live_runtime(
    events: list[dict[str, Any]], *, probe: Optional[Callable[[Any], Optional[bool]]] = None
) -> Optional[dict[str, Any]]:
    """Живой рантайм старого движка на месте лида.

    До 2.0 дело ведёт `bin/duty run`: он поднимает попытки и пишет
    `attempt_started`, но фактов `lead_started` у такого дела нет вовсе. Считать
    это «лида нет» нельзя — сторож 2.0 тогда поднял бы свой заход поверх
    работающего рантайма, и по делу пошла бы двойная работа. Открытая попытка с
    живым pid — честный признак, что дело уже ведут.
    """
    found = journal._open_attempt(events)
    if not found:
        return None
    _, event = found
    payload = journal.payload_of(event)
    pid = payload.get("pid")
    if not (probe or journal.pid_alive)(pid):
        return None
    return {
        "run_id": str(payload.get("run_id") or ""),
        "stage": str(payload.get("stage") or ""),
        "pid": pid,
    }


def _legacy_driven(events: list[dict[str, Any]]) -> bool:
    """Дело ведёт рантайм старого движка, а не лид 2.0.

    Признак один и он в журнале: у дела 2.0 заведена хартия (`created.payload.spec`),
    у дел 0.1.x её нет вовсе — этапность жила в самой спеке, и поднимал их
    `bin/duty run`. Такое дело сторож не трогает ни с живым, ни с мёртвым pid:
    два движка на одном деле дали бы двойную работу и две правды в журнале.
    """
    return not stagefeed.spec_of(events)


def _not_led(deal: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """Почему лида не поднимают: закрыто, ждёт человека, ждёт родителей.

    `wait_gate` назван отдельно: у дела с незакрытыми родителями нет ни работы,
    ни вопроса — лид запустился бы, чтобы ничего не сделать и сжечь заход.
    """
    status = str(deal.get("status") or "")
    if status in deals.CLOSED_STATUSES:
        return f"дело закрыто (status={status})"
    if status in NOT_LEAD_STATUSES:
        return f"дело ждёт человека (status={status})"
    folded = journal.fold(events)
    if str(folded.get("state") or "") == "wait_gate":
        return "дело ждёт закрытия родителей (wait_gate)"
    return ""


def _existing_park(events: list[dict[str, Any]], limit: int) -> Optional[dict[str, Any]]:
    """Уже записанная парковка по исчерпанию резюмов — чтобы не спрашивать дважды."""
    parked = journal.parked_of(events)
    if parked is None:
        return None
    if str(parked.get("reason") or "") != failures.REASON_TOO_MANY:
        return None
    if _int(parked.get("limit")) != int(limit):
        return None
    return parked


def park(
    conn: sqlite3.Connection,
    deal: dict[str, Any],
    st: dict[str, Any],
    *,
    limit: int,
    home: Path,
    now: Any = None,
) -> dict[str, Any]:
    """Исчерпаны резюмы — запарковать дело и спросить человека, а не крутить заходы.

    Парковка — существующий `blocked` с `block_kind='parked'` (тот же путь, что у
    исчерпанных попыток, `docs/ENGINE-2.0.md`, раздел 10): отдельного вида факта
    нет, а состояние ожидания — `wait_human`, потому что дальше решает человек.
    Вопрос идёт в пул (`ask.ask`), и ответ человека приходит тому же лиду фактом.
    """
    moment = _now(now)
    last = st.get("last_exit") or {}
    resumes = _int(st.get("resumes"))
    code = "" if last.get("exit_code") is None else f" (код {last.get('exit_code')})"
    headline = (
        f"Лид падает: {resumes} автоподъёмов из {limit}, дело {deal['id']} остановлено. "
        f"Последний выход — {last.get('reason') or 'причина не записана'}{code}."
    )
    text_full = (
        f"{headline}\n\nРезюмы лида исчерпаны (`max_lead_resumes={limit}`): следующий заход "
        f"ничего не изменит, пока не понятно, почему сессия падает.\n"
        f"Сессия: {st.get('session_id') or 'ещё нет'}; последний выход: "
        f"{last.get('reason') or '—'} (код {last.get('exit_code')}).\n\n"
        f"continue — новый цикл автоподъёмов (счётчик обнулится, лид будет поднят снова); "
        f"stop — закрыть дело."
    )
    asked = ask.ask(
        conn,
        {**deal, "home": Path(home)},
        headline,
        text_full,
        list(failures.PARK_OPTIONS),
        routing={"target": "human", "reason": "lead_resumes_exhausted"},
    )
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind=failures.PARKED_KIND,
        reason=headline,
        author=SOURCE,
        source=SOURCE,
        details={
            "kind": failures.INFRA,
            "stage": "",
            "attempts": resumes,
            "limit": int(limit),
            "park_reason": failures.REASON_TOO_MANY,
            "question_id": int(asked.question_id),
            "lead": True,
        },
        now=moment,
    )
    return {
        "question_id": int(asked.question_id),
        "resumes": resumes,
        "limit": int(limit),
        "reason": last.get("reason") or "",
    }


def _close_dead(
    conn: sqlite3.Connection, deal: dict[str, Any], events: list[dict[str, Any]], st: dict[str, Any], *, now: int
) -> Optional[int]:
    """Записать недостающий `lead_exited` заходу, чей процесс уже мёртв.

    Факт пишет тот, кто ждал процесс; если обёртку убили снаружи, факта нет.
    Дописать его — та же работа, что у `reaper` для попыток: журнал обязан
    называть конец захода, иначе «лид отсутствует» и «лид умер» неразличимы.
    """
    if not st.get("open") or st.get("live"):
        return None
    fresh = journal.read_events(conn, str(deal["id"]))
    payload = {
        "mode": str(st.get("mode") or ""),
        "pid": st.get("pid"),
        "reason": "process_gone",
        "exit_code": None,
        "session_id": st.get("session_id") or None,
        "cursor": head(fresh),
        "duration_sec": max(0, int(now) - _int(st.get("started_at"))),
        "recovered": True,
    }
    return journal.append(
        conn,
        [
            _event(
                deal,
                LEAD_KIND_EXITED,
                payload,
                {"mode": payload["mode"], "pid": payload["pid"], "reason": payload["reason"]},
                int(now),
            )
        ],
    )


def _spawn_supervisor(home: Path, deal_id: str, run_budget: int) -> int:
    """Поднять заход отсоединённым процессом; вернуть его pid.

    `start_new_session=True` — своя группа процессов: смерть диспетчера (cron
    убивает скрипт по таймауту) не уносит сессию лида, а смерть сессии не тянет
    за собой диспетчер. Вывод обёртки идёт файлом: причина, по которой модель не
    поднялась, обязана быть видна.
    """
    log = log_path(home, deal_id, _now())
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", deal_id, str(home), str(int(run_budget))],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(home),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        handle.close()
    return int(process.pid)


def supervise(
    home: Path,
    deal_id: str,
    *,
    run_budget: Optional[int] = None,
    max_resumes_override: Any = None,
    now: Any = None,
    probe: Optional[Callable[[Any], Optional[bool]]] = None,
    spawn: Optional[Callable[..., int]] = None,
    write: bool = True,
) -> dict[str, Any]:
    """Тик сторожа по одному делу: жив ли лид, поднять резюм, запарковать.

    Ответ называет решение словами (`action`), потому что `bin/duty lead <id>`
    печатает именно его: «лид жив» и «лид поднят резюмом» — разные ответы, и
    человеку в терминале нужен второй.
    """
    home = Path(home).expanduser()
    budget_sec = DEFAULT_RUN_BUDGET_SEC if run_budget is None else max(1, _int(run_budget))
    moment = _now(now)
    conn = deals.open_store(home)
    if conn is None:  # pragma: no cover
        raise LeadError(f"store is unavailable under {home}")
    try:
        deal = _deal(conn, deal_id)
        events = journal.read_events(conn, str(deal["id"]))
        st = state(events, now=moment, probe=probe)
        # Потолок нужен во всех ответах: «резюмов 3 из 6» — то, по чему человек
        # решает, скоро ли дело запаркуется.
        limit = max_resumes(max_resumes_override, spec=_spec(events))
        skip = _not_led(deal, events)
        if skip:
            return {
                "deal": str(deal["id"]),
                "action": "skipped",
                "reason": skip,
                "lead": st,
                "limit": limit,
            }
        if st["live"]:
            return {"deal": str(deal["id"]), "action": "alive", "lead": st, "limit": limit}
        # Дело, которое ведёт рантайм старого движка (0.1.x): у него нет фактов
        # `lead_started`, но открытая попытка с живым pid — тот же «лид жив».
        # Без этой проверки сторож поднял бы второй заход поверх работающего
        # рантайма и удвоил бы работу по делу.
        runtime = _live_runtime(events, probe=probe)
        if runtime:
            return {
                "deal": str(deal["id"]),
                "action": "alive",
                "lead": {**st, "live": True, "mode": "runtime", "pid": runtime["pid"]},
                "limit": limit,
                "runtime": runtime,
            }
        if _legacy_driven(events):
            return {
                "deal": str(deal["id"]),
                "action": "skipped",
                "reason": "дело ведёт рантайм старого движка: попытки есть, фактов лида нет",
                "lead": st,
                "limit": limit,
            }
        recovered = _close_dead(conn, deal, events, st, now=moment) if write else None
        if _int(st["resumes"]) >= limit:
            known = _existing_park(events, limit)
            parked = known or (
                park(conn, deal, st, limit=limit, home=home, now=moment) if write else None
            )
            return {
                "deal": str(deal["id"]),
                "action": "parked",
                "lead": st,
                "limit": limit,
                "question": parked,
                "already": bool(known),
            }
        mode = MODE_RESUME if st["session_id"] else MODE_FIRST
        pid = (spawn or _spawn_supervisor)(home, str(deal["id"]), budget_sec) if write else None
        return {
            "deal": str(deal["id"]),
            "action": "started" if write else "would_start",
            "mode": mode,
            "session_id": st["session_id"],
            "pid": pid,
            "lead": st,
            "limit": limit,
            "recovered_exit": recovered,
        }
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    """Процесс захода: `lead.py run <deal_id> <home> [run_budget]`.

    Отдельная точка входа, а не функция CLI: обёртку поднимает `supervise`
    отсоединённой, и в её argv не должно быть ничего, кроме адреса дела.
    """
    words = list(sys.argv[1:] if argv is None else argv)
    if not words or words[0] != "run":
        print("usage: lead.py run <deal_id> <home> [run_budget]", file=sys.stderr)
        return 2
    if len(words) < 3:
        print("usage: lead.py run <deal_id> <home> [run_budget]", file=sys.stderr)
        return 2
    budget_sec = _int(words[3]) if len(words) > 3 else DEFAULT_RUN_BUDGET_SEC
    try:
        report = run(Path(words[2]).expanduser(), words[1], run_budget=budget_sec)
    except Exception:  # noqa: BLE001 - процесс отдельный: причина уходит в лог
        traceback.print_exc()
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("started") else 1


if __name__ == "__main__":
    sys.exit(main())
