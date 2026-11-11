"""Решения лида как проекция журнала: лента, состояние и счётчики.

Контракт 2.0 (`docs/ENGINE-2.0.md`, раздел 9): «каждый инструмент сначала пишет
факт, потом действует». Поэтому экран не спрашивает живую сессию лида — он читает
журнал и говорит то, что в нём записано. Проекция живёт здесь одна, чтобы `now`,
экран дела, `why` и `stats` считали её одинаково и не разошлись в трактовке.

Две группы фактов:

- **решения** (`feed`) — `plan_accepted`/`plan_rejected` (принял или отклонил
  план, добавил или убрал этапы), `attempt_started` после отказа (перезапустил
  этап), `stage_cancelled` (погасил этап), `question_answered` (ответил воркеру —
  с автором `lead`/`role`/`human`), `completed` (закрыл или отменил дело) и
  `lead_*` (поднят сам или резюмом);
- **состояние** (`status`) — `lead_started`/`lead_resumed`/`lead_exited`: лид жив,
  отсутствует или поднят резюмом N из M.

Писателя фактов `lead_*` и `stage_cancelled` здесь нет: их пишут супервизор
`bin/duty lead` и инструменты `duty_cancel*` (`docs/ENGINE-2.0.md`, разделы 5 и
10). Модуль только читает, поэтому дело 1.x без единого `lead_*` факта читается
так же — «лид: нет фактов», и это честный ответ, а не выдуманное «жив».
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

# Модули плагина грузятся по пути и живут в `sys.modules` под общими именами:
# вторая загрузка означала бы вторую копию констант и разбора событий.
JOURNAL_MODULE_NAME = "hermes_duty_journal"


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

# Факты лида (`docs/ENGINE-2.0.md`, раздел 10). Пишет их супервизор: `lead_started`
# — сессия поднята с нуля, `lead_resumed` — `--resume` с дайджестом фактов,
# `lead_exited` — сессия закончилась (штатно, по турнам или падением).
LEAD_STARTED = "lead_started"
LEAD_RESUMED = "lead_resumed"
LEAD_EXITED = "lead_exited"
LEAD_KINDS = (LEAD_STARTED, LEAD_RESUMED, LEAD_EXITED)

PLAN_PROPOSED = "plan_proposed"
PLAN_ACCEPTED = "plan_accepted"
PLAN_REJECTED = "plan_rejected"
STAGE_CANCELLED = "stage_cancelled"
ATTEMPT_STARTED = "attempt_started"
ATTEMPT_ENDED = "attempt_ended"
QUESTION_ASKED = "question_asked"
QUESTION_ANSWERED = "question_answered"
COMPLETED = "completed"

# Отмена дела (`docs/ENGINE-2.0.md`, раздел 5): не «плохо закрыто», а «не считается
# завершённым». Поэтому у отмены своё слово в `cause` и свой статус.
CANCEL_CAUSE = "lead_cancelled"
CANCELLED_STATUS = "cancelled"
LEAD_CLOSE_CAUSE = "lead_closed"

# Состояние лида: жив, отсутствует, нет фактов вовсе (дела 1.x и дела, до которых
# супервизор ещё не дошёл).
STATE_LIVE = "live"
STATE_ABSENT = "absent"
STATE_UNKNOWN = "unknown"

# Автор ответа на вопрос воркера (`docs/ENGINE-2.0.md`, раздел 6): маршрут —
# решение лида, и оно видно в журнале.
AUTHOR_LEAD = "lead"
AUTHOR_ROLE = "role"
AUTHOR_HUMAN = "human"
AUTHOR_EXPIRED = "expired"

# Ответ роли пришёл без ответа: передача закрылась (`redirected`/`failed`) —
# это исход маршрута, а не ответ воркеру.
ROLE_VIA = ("redirected", "failed")

# Потолок резюмов, когда факты его не назвали: значение хартии из контракта 2.0
# (раздел 3). Хартия в спеке пока не разбирает `max_lead_resumes`, поэтому число
# по умолчанию одно на плагин, а факт может назвать своё (`max`).
DEFAULT_MAX_LEAD_RESUMES = 6

# Сколько решений показывает экран дела: свежие важнее всей истории, а полная
# лента — это журнал (`duty attempts` рисует попытки, здесь — решения).
FEED_LIMIT = 50

# Сколько знаков ответа попадает в ленту решений: текст ответа целиком живёт в
# журнале и в пуле, здесь нужна строка, по которой видно, о чём был ответ.
ANSWER_PREVIEW = 60


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


def _ordered(events: Iterable[Any]) -> list[dict[str, Any]]:
    """События в порядке применения — по `id`, как их читает fold."""
    return sorted(
        (event for event in events or () if isinstance(event, dict)),
        key=lambda event: _int(event.get("id")) or 0,
    )


def _short(text: Any, width: int = ANSWER_PREVIEW) -> str:
    """Одна строка не длиннее `width`: переводы строк ломают ленту."""
    value = " ".join(str("" if text is None else text).split())
    return value if len(value) <= width else value[: width - 1].rstrip() + "…"


def _stages_word(count: int) -> str:
    """«4 этапа», «1 этап», «12 этапов» — число с верным словом."""
    if count % 10 == 1 and count % 100 != 11:
        return "этап"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return "этапа"
    return "этапов"


# ---------------------------------------------------------------- состояние лида


def _cap(rows: list[dict[str, Any]]) -> int:
    """Потолок резюмов: из факта лида (`max`), иначе — хартия по умолчанию."""
    for event in reversed(rows):
        value = _int(_payload(event).get("max"))
        if value is not None and value > 0:
            return value
    return DEFAULT_MAX_LEAD_RESUMES


def status(events: Iterable[Any]) -> dict[str, Any]:
    """Состояние лида по журналу: жив, отсутствует или поднят резюмом N из M.

    Читаются только факты `lead_*`: живая сессия не спрашивается ничем, иначе
    экран отвечал бы про процесс, а не про дело. Последний факт решает слово
    («жив» после `lead_started`/`lead_resumed`, «отсутствует» после
    `lead_exited`), а число резюмов считается по всем фактам дела — это и есть
    счётчик `max_lead_resumes` из раздела 10 контракта.
    """
    rows = [event for event in _ordered(events) if str(event.get("kind") or "") in LEAD_KINDS]
    cap = _cap(rows)
    resumes = sum(1 for event in rows if str(event.get("kind") or "") == LEAD_RESUMED)
    if not rows:
        return {
            "state": STATE_UNKNOWN,
            "resumes": 0,
            "cap": cap,
            "since": None,
            "last": None,
            "reason": "",
            "text": "лид: нет фактов",
            "evidence": "журнал: ни одного факта lead_* — лида к этому делу не поднимали",
        }
    last = rows[-1]
    kind = str(last.get("kind") or "")
    payload = _payload(last)
    live = kind in (LEAD_STARTED, LEAD_RESUMED)
    reason = str(payload.get("reason") or "")
    if live and resumes:
        text = f"лид: резюм {resumes} из {cap}"
    elif live:
        text = "лид: жив"
    elif resumes:
        text = f"лид: отсутствует · резюм {resumes} из {cap}"
    else:
        text = "лид: отсутствует"
    evidence = f"журнал: последний факт лида {kind}"
    if live:
        evidence += f", пробуждений {resumes} из {cap}"
    elif reason:
        evidence += f": {reason}"
    return {
        "state": STATE_LIVE if live else STATE_ABSENT,
        "resumes": resumes,
        "cap": cap,
        "since": _int(last.get("ts")),
        "last": kind,
        "reason": reason,
        "text": text,
        "evidence": evidence,
    }


def wake_word(block: Any) -> str:
    """Сколько раз лида поднимали заново: «пробуждений нет» или «пробуждений 2 из 6»."""
    data = block or {}
    resumes = int(data.get("resumes") or 0)
    if not resumes:
        return "пробуждений нет"
    cap = data.get("cap") or DEFAULT_MAX_LEAD_RESUMES
    return f"пробуждений {resumes} из {cap}"


def lead_word(block: Any) -> str:
    """Состояние лида одним словом для фразы: «жив», «отсутствует», «не поднимался»."""
    state = str((block or {}).get("state") or "")
    if state == STATE_LIVE:
        return "жив"
    if state == STATE_ABSENT:
        return "отсутствует"
    return "не поднимался"


# ------------------------------------------------------------------ лента решений


def _proposals(ordered: list[dict[str, Any]]) -> dict[int, list[str]]:
    """Этапы предложенных планов по номеру ревизии: `{rev: [id, …]}`.

    Берутся только факты **плана** (ревью `stage` пуст): у ревизии этапа тот же
    счётчик, и смешать их значило бы приписать плану чужие этапы.
    """
    out: dict[int, list[str]] = {}
    for event in ordered:
        if str(event.get("kind") or "") != PLAN_PROPOSED:
            continue
        payload = _payload(event)
        if str(payload.get("stage") or ""):
            continue
        rev = _int(payload.get("rev"))
        if rev is None:
            continue
        stages = payload.get("stages")
        out[rev] = [
            str(item.get("id") or "")
            for item in stages or ()
            if isinstance(item, dict) and str(item.get("id") or "")
        ]
    return out


def _plan_text(rev: Optional[int], stages: list[str], previous: list[str]) -> str:
    """Решение по плану словами: «план создан (4 этапа)», «добавлен этап research».

    Первый принятый план — рождение графа, дальше ревизия называется составом:
    именно разница ревизий и есть решение лида («скоуп вырос»), а не номер.
    """
    if not previous:
        return f"план создан ({len(stages)} {_stages_word(len(stages))})" if stages else "план принят"
    prefix = f"план ревизии {rev}: " if rev is not None else "план: "
    added = [stage for stage in stages if stage not in previous]
    removed = [stage for stage in previous if stage not in stages]
    if added:
        noun = "этап" if len(added) == 1 else "этапы"
        verb = "добавлен" if len(added) == 1 else "добавлены"
        return f"{prefix}{verb} {noun} {', '.join(added)}"
    if removed:
        noun = "этап" if len(removed) == 1 else "этапы"
        verb = "убран" if len(removed) == 1 else "убраны"
        return f"{prefix}{verb} {noun} {', '.join(removed)}"
    return f"{prefix}состав этапов не изменился"


def _restart_text(stage: str, rows: list[dict[str, Any]]) -> str:
    """Перезапуск этапа словами: «этап verify перезапущен после contract ×2»."""
    failed = [row for row in rows if str(row.get("outcome") or "").lower() == "failed"]
    if not failed:
        return f"этап {stage} перезапущен"
    kind = str(failed[-1].get("kind") or "")
    if not kind:
        return f"этап {stage} перезапущен после отказа"
    same = 0
    for row in reversed(failed):
        if str(row.get("kind") or "") != kind:
            break
        same += 1
    # Повторы одного класса считаются подряд: «×2» — это два отказа `contract`, а
    # не два отказа вообще (иначе флейк выглядел бы как новая работа).
    return f"этап {stage} перезапущен после {kind}" + (f" ×{same}" if same > 1 else "")


def _restarts(ordered: list[dict[str, Any]]) -> dict[int, str]:
    """Перезапуски этапов по журналу: `{индекс события: текст решения}`.

    Решение видно там, где этап пошёл второй раз: `attempt_started` с номером
    больше первого. Класс прошлого отказа берётся из закрытой попытки того же
    этапа, а не из текста причины.
    """
    history: dict[str, list[dict[str, Any]]] = {}
    texts: dict[int, str] = {}
    for index, event in enumerate(ordered):
        kind = str(event.get("kind") or "")
        if kind not in (ATTEMPT_STARTED, ATTEMPT_ENDED):
            continue
        payload = _payload(event)
        stage = str(payload.get("stage") or "")
        number = _int(payload.get("attempt"))
        if not stage or number is None:
            continue
        rows = history.setdefault(stage, [])
        if kind == ATTEMPT_STARTED:
            if number > 1:
                texts[index] = _restart_text(stage, rows)
            rows.append({"attempt": number, "outcome": None, "kind": None})
            continue
        row = next((item for item in reversed(rows) if item["attempt"] == number), None)
        if row is None:
            continue
        row["outcome"] = str(payload.get("outcome") or "")
        row["kind"] = str(payload.get("kind") or "")
    return texts


def answer_author(payload: dict[str, Any]) -> str:
    """Кто ответил на вопрос: `lead`, `role`, `human` или `expired`.

    `by` в факте называет ответившую роль (лид — тоже роль, `duty_answer`), а
    отсутствие `by` значит пул, то есть человека. Истёкший по сроку вопрос
    ответом не был никогда (`answered_via='expired'`), и складывать его с
    человеческими ответами нельзя: это разные исходы маршрута.
    """
    by = str(payload.get("by") or "").strip()
    if by == AUTHOR_LEAD:
        return AUTHOR_LEAD
    if by:
        return AUTHOR_ROLE
    via = str(payload.get("answered_via") or "").strip()
    if via == AUTHOR_EXPIRED:
        return AUTHOR_EXPIRED
    if via in ROLE_VIA:
        # Передача роли закрылась без ответа (`redirected`/`failed`): исхода
        # человека здесь нет, вопрос остался в маршруте ролей.
        return AUTHOR_ROLE
    return AUTHOR_HUMAN


def _answer_text(payload: dict[str, Any]) -> str:
    """Ответ на вопрос словами: кто ответил, на что и — коротко — что сказал."""
    number = payload.get("question_id")
    ref = f"вопрос #{number}" if number else "вопрос роли"
    by = str(payload.get("by") or "").strip()
    via = str(payload.get("answered_via") or "").strip()
    answer = _short(payload.get("answer"))
    if by == AUTHOR_LEAD:
        text = f"ответ на {ref} дал лид"
    elif by:
        text = f"{ref} передан роли {by}"
    elif via == AUTHOR_EXPIRED:
        text = f"{ref} закрыт по сроку без ответа"
    elif via in ROLE_VIA:
        text = f"передача {ref} закрылась: {via}"
    elif via:
        text = f"ответ на {ref} пришёл от человека ({via})"
    else:
        text = f"ответ на {ref} пришёл от человека"
    return f"{text}: {answer}" if answer else text


def _completed_text(payload: dict[str, Any]) -> tuple[str, Optional[str]]:
    """Закрытие дела словами и автор решения: отмена — не завершение."""
    cause = str(payload.get("cause") or "").strip()
    words = _short(payload.get("summary") or payload.get("reason") or "", 120)
    if cause == CANCEL_CAUSE:
        return ("дело отменено лидом" + (f": {words}" if words else ""), AUTHOR_LEAD)
    if cause == LEAD_CLOSE_CAUSE:
        return ("дело закрыто лидом" + (f": {words}" if words else ""), AUTHOR_LEAD)
    return (f"дело закрыто (cause={cause or '—'})", None)


def _lead_text(kind: str, payload: dict[str, Any], resumes: int, cap: int) -> str:
    """Факт лида словами: поднят, поднят резюмом, ушёл."""
    reason = _short(payload.get("reason"), 120)
    if kind == LEAD_RESUMED:
        return f"лид поднят резюмом {resumes} из {cap}" + (f": {reason}" if reason else "")
    if kind == LEAD_EXITED:
        return "лид ушёл" + (f": {reason}" if reason else "")
    return "лид поднят"


def feed(events: Iterable[Any], *, limit: Optional[int] = FEED_LIMIT) -> list[dict[str, Any]]:
    """Лента решений лида: что он сделал с делом — по фактам, не по пересказу.

    Один список на все поверхности: план, перезапуски этапов, отмены, ответы
    воркерам, закрытие и собственные подъёмы лида. Порядок — журнальный
    (старые сверху); `limit` оставляет последние `limit` решений, `None` — все.

    Автор (`author`) называется там, где факт его называет: у ответа на вопрос —
    `lead`/`role`/`human` (это и есть выбранный маршрут), у решения по плану —
    `by` факта (`lead` в 2.0, `engine` в делах 1.x — переписывать историю нельзя),
    у отмены и закрытия — лид. У перезапуска этапа автора в факте нет, и
    выдумывать его нельзя: `attempt_started` пишет движок, а кто велел — из этого
    факта не видно.
    """
    ordered = _ordered(events)
    proposals = _proposals(ordered)
    restarts = _restarts(ordered)
    cap = _cap([event for event in ordered if str(event.get("kind") or "") in LEAD_KINDS])
    items: list[dict[str, Any]] = []
    accepted: list[str] = []
    resumes = 0
    for index, event in enumerate(ordered):
        kind = str(event.get("kind") or "")
        payload = _payload(event)
        at = _int(event.get("ts"))
        if kind == PLAN_ACCEPTED and not str(payload.get("stage") or ""):
            rev = _int(payload.get("rev"))
            stages = proposals.get(rev, []) if rev is not None else []
            items.append(
                {
                    "kind": kind,
                    "at": at,
                    "author": str(payload.get("by") or "") or None,
                    "stage": None,
                    "text": _plan_text(rev, stages, accepted),
                }
            )
            if stages:
                accepted = stages
            continue
        if kind == PLAN_REJECTED and not str(payload.get("stage") or ""):
            issues = payload.get("issues")
            count = len(issues) if isinstance(issues, list) else 0
            # Причина — слова самого факта (`why` движка): он уже назвал и ревизию,
            # и число отказов, и второй пересказ рядом читался бы как два отказа.
            why = _short(payload.get("why"), 120)
            text = why or (f"план отклонён ({count} отказов)" if count else "план отклонён")
            items.append(
                {
                    "kind": kind,
                    "at": at,
                    "author": str(payload.get("by") or "") or None,
                    "stage": None,
                    "text": text,
                }
            )
            continue
        if index in restarts:
            # Автора у перезапуска в факте нет: `attempt_started` пишет движок по
            # заказу лида, и приписать решение лиду значило бы выдумать факт (в
            # делах 1.x этап перезапускал шедулер). Текст называет этап и класс
            # прошлого отказа — этого хватает, чтобы видеть решение.
            items.append(
                {"kind": kind, "at": at, "author": None, "stage": str(payload.get("stage") or ""), "text": restarts[index]}
            )
            continue
        if kind == STAGE_CANCELLED:
            stage = str(payload.get("stage") or "")
            reason = _short(payload.get("reason"), 120)
            text = f"этап {stage} отменён" if stage else "этап отменён"
            items.append(
                {
                    "kind": kind,
                    "at": at,
                    "author": str(payload.get("by") or "") or None,
                    "stage": stage or None,
                    "text": f"{text}: {reason}" if reason else text,
                }
            )
            continue
        if kind == QUESTION_ANSWERED:
            routing = payload.get("routing")
            stage = str(routing.get("stage") or "") if isinstance(routing, dict) else ""
            items.append(
                {
                    "kind": kind,
                    "at": at,
                    "author": answer_author(payload),
                    "stage": stage or None,
                    "text": _answer_text(payload),
                }
            )
            continue
        if kind == COMPLETED:
            text, author = _completed_text(payload)
            items.append({"kind": kind, "at": at, "author": author, "stage": str(payload.get("stage") or "") or None, "text": text})
            continue
        if kind in LEAD_KINDS:
            if kind == LEAD_RESUMED:
                resumes += 1
            items.append(
                {
                    "kind": kind,
                    "at": at,
                    "author": AUTHOR_LEAD,
                    "stage": None,
                    "text": _lead_text(kind, payload, resumes, cap),
                }
            )
    if limit is not None and len(items) > limit:
        return items[-limit:]
    return items


# ------------------------------------------------------------------- счётчики


def token_spend(events: Iterable[Any]) -> dict[str, Any]:
    """Токены координации: сумма `usage_tokens` фактов лида.

    `None` — ни один факт лида токенов не записал: «лид не тратил» и «расход лида
    не измерен» — разные факты, и ноль здесь был бы выдумкой (так же считает
    `stats` по попыткам).
    """
    total = 0
    count = 0
    for event in _ordered(events):
        if str(event.get("kind") or "") not in LEAD_KINDS:
            continue
        payload = _payload(event)
        details = payload.get("details")
        value = _int(payload.get("usage_tokens"))
        if value is None and isinstance(details, dict):
            value = _int(details.get("usage_tokens"))
        if value is None:
            continue
        total += value
        count += 1
    return {"tokens": total if count else None, "count": count}


def question_outcomes(events: Iterable[Any]) -> dict[str, Any]:
    """Исходы вопросов: кто ответил и сколько ещё ждут.

    Вопрос считается по своему номеру в пуле (`question_id`): вопрос роли номера
    не имеет, и его ответ учитывается автором (`role`), а не ожиданием человека.
    """
    answered: dict[str, int] = {AUTHOR_LEAD: 0, AUTHOR_ROLE: 0, AUTHOR_HUMAN: 0, AUTHOR_EXPIRED: 0}
    asked: set[int] = set()
    closed: set[int] = set()
    for event in _ordered(events):
        kind = str(event.get("kind") or "")
        if kind not in (QUESTION_ASKED, QUESTION_ANSWERED):
            continue
        payload = _payload(event)
        number = _int(payload.get("question_id"))
        if kind == QUESTION_ASKED:
            if number is not None:
                asked.add(number)
            continue
        author = answer_author(payload)
        answered[author] = answered.get(author, 0) + 1
        if number is not None:
            closed.add(number)
    return {"asked": len(asked), "answered": answered, "waiting": len(asked - closed)}


def resume_count(events: Iterable[Any]) -> int:
    """Сколько раз лида поднимали резюмом — счётчик `max_lead_resumes`."""
    return sum(1 for event in _ordered(events) if str(event.get("kind") or "") == LEAD_RESUMED)


def hit_resume_cap(events: Iterable[Any]) -> bool:
    """Упёрлось ли дело в потолок резюмов: лид падал столько раз, сколько разрешено."""
    rows = [event for event in _ordered(events) if str(event.get("kind") or "") in LEAD_KINDS]
    return sum(1 for event in rows if str(event.get("kind") or "") == LEAD_RESUMED) >= _cap(rows)


def closing_cause(events: Iterable[Any]) -> Optional[str]:
    """Причина закрытия дела из последнего `completed`; `None` — дело не закрывалось."""
    for event in reversed(_ordered(events)):
        if str(event.get("kind") or "") != COMPLETED:
            continue
        return str(_payload(event).get("cause") or "")
    return None


def is_cancelled(events: Iterable[Any], status: Optional[str] = None) -> bool:
    """Отменено ли дело: статус `cancelled` или закрывающий факт `lead_cancelled`.

    Отмена — не завершение (`docs/ENGINE-2.0.md`, раздел 5): отменённое дело не
    считается сделанным, и `stats` обязан считать его отдельно. Статус читается
    тоже: слово в строке store и слово в факте могут прийти разными карточками.
    """
    if str(status or "") == CANCELLED_STATUS:
        return True
    return closing_cause(events) == CANCEL_CAUSE
