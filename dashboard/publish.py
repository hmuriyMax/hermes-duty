"""Публикация: одобрение человеком и проверка факта, а не заявки.

Публикует **роль** (`publisher`/`deployer`), а не движок: у `git push` нет
второго пути мимо этапа (`spec.FORBIDDEN_CMD_PREFIXES`). Движок делает две вещи,
и обе — проверки:

1. **Перед спавном** публикующего этапа сверяет журнал: гейт-предок спросил
   человека, человек ответил «ок», и с момента ответа дерево не менялось. Иначе
   человек одобрил один коммит, а публикуется другой — это отказ `contract` с
   цитатой «одобрен commit X, в дереве Y».
2. **После отчёта** сверяет заявку с origin: `published: {sha, branch, mr_url}` в
   `result.json` — слова исполнителя, а факт даёт `git ls-remote`
   (`runner.published_claim`). Нет заявленного sha в origin — класс `contract`,
   «публикация заявлена, но не найдена». Сверка идёт по **префиксу**: сокращённый
   sha (так его пишет публикатор и печатает консоль) законен, пока однозначен
   (`same_commit`, `origin_hits`).

Почему это отдельный слой. Здесь нет ни git, ни БД, ни часов: на вход — события
журнала, ответ гейта и текущий коммит дерева, на выход — `Approval` или причина
отказа. Поэтому правило «одобрен один, публикуется другой» проверяется без
репозитория и без модели, а `dashboard/runner.py` только исполняет его.

Одобренный коммит — факт **вопроса**, а не ответа: его записывает движок в
`question_asked.payload.routing.approved_commit`, когда гейт спрашивает (в этот
момент он и есть дерево, которое публикуется). Ответ приходит позже, и к этому
времени дерево уже могло измениться: сверка двух коммитов и есть защита от
подмены.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

JOURNAL_MODULE_NAME = "hermes_duty_journal"


def _journal() -> Any:
    """`dashboard/journal.py` — тот же модуль, что у остального плагина."""
    loaded = sys.modules.get(JOURNAL_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "journal.py"
    spec = importlib.util.spec_from_file_location(JOURNAL_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"journal module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[JOURNAL_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


# Отчёт публикующего этапа: что именно ушло вовне. Поля закрыты, потому что
# движок проверяет их по одному, а «ещё что-нибудь» проверять нечем.
CLAIM_KEYS = ("sha", "branch", "mr_url")

# Признак закрытого по сроку вопроса — тот же, что у `ask.EXPIRED_VIA`: пул не
# заполняет `answered_via` у истёкшей строки, и наша пометка — единственный факт.
EXPIRED_VIA = "expired"

# Вид этапа-гейта: вопрос человеку, а не процесс.
GATE_KIND = "gate"

# Исход успешной попытки — тот же набор, по которому `transit` считает этап `done`.
OK_OUTCOMES = ("done",)

# Длина sha в цитате отказа: семь знаков сверяются с `git log` глазами.
SHORT_SHA = 12

# Сколько знаков sha достаточно, чтобы заявку можно было сверять: `git` принимает
# сокращённую форму, пока она однозначна, но три знака совпадают почти со всем, и
# «нашлось» было бы догадкой, а не фактом.
SHA_MIN = 4

# Полный sha — 40 знаков hex; длиннее в заявке не бывает ссылки на коммит.
SHA_MAX = 40

# Знаки sha: hex в нижнем регистре — регистр заявки не значит ничего.
SHA_CHARS = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class Approval:
    """Одобрение публикации: можно ли публиковать и чем это подтверждено.

    `ok=True` — вопрос задан, человек ответил вариантом из `allow`, и с момента
    вопроса дерево не менялось; тогда `commit` — ровно то, что публикуется.
    `ok=False` — `reason` называет отказ словами, и он уходит в журнал как класс
    `contract`: публикация без «ок» — не сбой исполнителя, а невыполненное
    условие движка.
    """

    ok: bool
    reason: str = ""
    question_id: str = ""
    gate: str = ""
    commit: str = ""
    answer: str = ""


def _payload(event: Any) -> dict[str, Any]:
    """Payload события словарём — его форма и разбирается здесь."""
    return _journal().payload_of(event)


def _ordered(events: Iterable[Any]) -> list[Any]:
    """События в порядке журнала: `id`, а не `ts` (внутри секунды он одинаков)."""
    rows = [item for item in events or () if isinstance(item, dict)]
    return sorted(rows, key=_seq)


def _seq(event: dict[str, Any]) -> int:
    """Номер события журнала: по нему и только по нему определяется порядок."""
    raw = event.get("id")
    return raw if isinstance(raw, int) else 0


def _qid(payload: dict[str, Any]) -> str:
    """`question_id` строкой: пул отдаёт и числом, и строкой; пусто — пустая строка."""
    raw = payload.get("question_id")
    if raw is None or raw == "":
        return ""
    return str(raw)


def _expired(answer: dict[str, Any]) -> bool:
    """Вопрос закрыт по сроку, а не ответом человека."""
    if str(answer.get("answered_via") or "").strip().lower() == EXPIRED_VIA:
        return True
    raw = answer.get("answer_len")
    try:
        return int(raw) == 0
    except (TypeError, ValueError):
        return False


def sha_shape(value: Any) -> str:
    """Причина «это не sha» или пустая строка, если ссылку можно сверять.

    Сверка идёт по префиксу полного sha, поэтому мусор в заявке (`abc`, `ветка`,
    41 знак) обязан отсекаться **до** сети: `ls-remote` на такой строке либо не
    ответит, либо «найдётся» случайно, и отказ назвал бы не ту причину.
    """
    text = str(value or "").strip().lower()
    if len(text) < SHA_MIN:
        return f"не sha: «{text}» короче {SHA_MIN} знаков"
    if len(text) > SHA_MAX:
        return f"не sha: «{text[:SHA_MAX]}…» длиннее {SHA_MAX} знаков"
    if any(char not in SHA_CHARS for char in text):
        return f"не sha: «{text}» содержит не hex-знаки"
    return ""


def same_commit(one: Any, other: Any) -> bool:
    """Один и тот же коммит: полное равенство или однозначная сокращённая форма.

    `git` принимает сокращённый sha, пока он однозначен, а человек видит коммит
    двенадцатью знаками (`SHORT_SHA`) и в вопросе, и в причине отказа. Поэтому
    «одобрен a1b2c3, в дереве a1b2c3d4…» — не подмена дерева, а одна ссылка в двух
    формах: сверка обязана это понимать, иначе гейт отказывает честной публикации.

    Не-hex имена (`HEAD`, `origin/master`) сверяются только полным равенством: у
    них нет семантики префикса, и `head` от `head~1` ничего не отличал бы.
    """
    left = str(one or "").strip().lower()
    right = str(other or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    if sha_shape(left) or sha_shape(right):
        return False
    short, full = sorted((left, right), key=len)
    return full.startswith(short)


def origin_hits(refs: Iterable[tuple[str, str]], sha: Any) -> list[tuple[str, str]]:
    """Ссылки origin, чей коммит начинается с заявленного sha: по одной на коммит.

    `refs` — пары `(sha, ref)` из `git ls-remote`. Заявка — префикс, поэтому
    сокращённый sha находит свой коммит; несколько **разных** коммитов с таким
    началом — неоднозначность (её видно по длине списка), а один коммит под
    несколькими ссылками (ветка и тег) — тот же факт, и первая ссылка называет его.
    """
    want = str(sha or "").strip().lower()
    hits: dict[str, str] = {}
    for full, ref in refs or ():
        text = str(full or "").strip().lower()
        if not text.startswith(want):
            continue
        hits.setdefault(text, str(ref or "").strip())
    return sorted(hits.items())


def approval(events: Iterable[Any], gates: Iterable[str], *, current: str) -> Approval:
    """Одобрение публикации по журналу: последний вопрос гейта-предка и его ответ.

    `gates` — этапы-гейты в предках публикующего этапа (по графу спеки),
    `current` — коммит, от которого сейчас отведётся дерево публикации. Берётся
    **последний** вопрос: если человек отвечал дважды, действует последнее «ок»,
    и одобрен именно тот коммит, который стоял в дереве на момент этого вопроса.

    Порядок отказов — от «вопроса не было» к «одобрено одно, публикуется другое»:
    каждый следующий имеет смысл, только если предыдущий пройден.
    """
    wanted = {str(gate) for gate in gates or ()}
    asked: Optional[tuple[dict[str, Any], dict[str, Any]]] = None
    for event in _ordered(events):
        if str(event.get("kind") or "") != "question_asked":
            continue
        payload = _payload(event)
        routing = payload.get("routing")
        if not isinstance(routing, dict) or str(routing.get("stage") or "") not in wanted:
            continue
        asked = (payload, routing)
    if asked is None:
        named = ", ".join(sorted(wanted)) or "—"
        return Approval(
            ok=False,
            reason=(
                "публикация требует вопроса человеку (kind: gate) в предках: "
                f"гейт {named} не спрашивал"
            ),
        )
    payload, routing = asked
    question_id = _qid(payload)
    gate = str(routing.get("stage") or "")
    approved = str(routing.get("approved_commit") or "")
    if not approved:
        return Approval(
            ok=False,
            gate=gate,
            question_id=question_id,
            reason=f"гейт {gate} не назвал одобренный commit: сверять публикацию не с чем",
        )

    answer: Optional[dict[str, Any]] = None
    ended: Optional[dict[str, Any]] = None
    for event in _ordered(events):
        kind = str(event.get("kind") or "")
        data = _payload(event)
        if kind == "question_answered" and _qid(data) == question_id:
            answer = data
            continue
        if kind != "attempt_ended" or str(data.get("stage") or "") != gate:
            continue
        details = data.get("details")
        if isinstance(details, dict) and _qid(details) == question_id:
            ended = data
    if answer is None:
        return Approval(
            ok=False,
            gate=gate,
            question_id=question_id,
            commit=approved,
            reason=f"вопрос #{question_id} гейта {gate} без ответа: публиковать нечего",
        )
    if _expired(answer):
        return Approval(
            ok=False,
            gate=gate,
            question_id=question_id,
            commit=approved,
            reason=f"гейт {gate}: человек не ответил на вопрос #{question_id}",
        )
    if ended is None:
        return Approval(
            ok=False,
            gate=gate,
            question_id=question_id,
            commit=approved,
            reason=f"ответ на вопрос #{question_id} гейтом {gate} не разобран: приговора нет",
        )
    outcome = str(ended.get("outcome") or "").strip().lower()
    if outcome not in OK_OUTCOMES or str(ended.get("kind") or "").strip():
        details = ended.get("details")
        given = ""
        if isinstance(details, dict):
            given = str(details.get("answer") or "")
        return Approval(
            ok=False,
            gate=gate,
            question_id=question_id,
            commit=approved,
            answer=given,
            reason=f"человек не дал ок: гейт {gate} — {str(ended.get('reason') or 'ответ вне allow')}",
        )
    if not current or not same_commit(current, approved):
        return Approval(
            ok=False,
            gate=gate,
            question_id=question_id,
            commit=approved,
            answer="",
            reason=(
                f"одобрен commit {approved[:SHORT_SHA]}, в дереве {current[:SHORT_SHA] or '—'}: "
                "человек одобрял другой коммит"
            ),
        )
    details = ended.get("details")
    given = str(details.get("answer") or "") if isinstance(details, dict) else ""
    return Approval(
        ok=True,
        question_id=question_id,
        gate=gate,
        commit=approved,
        answer=given,
    )


def claim(result: Any) -> tuple[Optional[dict[str, str]], str]:
    """Заявка публикации из отчёта: `{sha, branch, mr_url}` или причина отказа.

    `published` обязан быть у публикующего этапа, отчитавшегося `done`: без него
    «опубликовано» — слово, а не факт, и сверять с origin нечего. `sha` без
    `branch` законен (публикация по тегу или в уже известную ветку), пустая
    строка вместо поля — нет: это незаполненная заявка, а не публикация.

    `sha` — сокращённый или полный, но hex: `git` принимает префикс, пока он
    однозначен, а «ветка» или три знака в этом поле — не ссылка, и сверять их с
    origin значит ходить в сеть ради отказа (`sha_shape`).
    """
    if not isinstance(result, dict):
        return None, "публикация заявлена, но отчёта нет: сверять с origin нечего"
    published = result.get("published")
    if not isinstance(published, dict):
        return None, "этап публикует, но в отчёте нет published: {sha, branch, mr_url}"
    values: dict[str, str] = {}
    for key in CLAIM_KEYS:
        raw = published.get(key)
        if raw is None:
            values[key] = ""
            continue
        if not isinstance(raw, str):
            return None, f"published.{key} — строка, а пришло {type(raw).__name__}"
        values[key] = raw.strip()
    if not values["sha"]:
        return None, "published без sha: что именно ушло в origin, не названо"
    shape = sha_shape(values["sha"])
    if shape:
        return None, f"published.sha — {shape}: сверять с origin нечего"
    return values, ""


def published_of(events: Iterable[Any]) -> Optional[dict[str, Any]]:
    """Последний факт `published` дела — что ушло в origin по проверке движка.

    `None` — публикации не было (или она не подтверждена), и поверхность не
    выдумывает нули: «не публиковали» и «публикация не найдена» — разные факты,
    второй живёт в причине отказа попытки.
    """
    last: Optional[dict[str, Any]] = None
    for event in _ordered(events):
        if str(event.get("kind") or "") != "published":
            continue
        last = _payload(event)
    return last
