"""Публикация: одобрение человеком и проверка факта, а не заявки.

Публикует **роль** (`publisher`/`deployer`), а не движок: у `git push` нет
второго пути мимо этапа (`spec.FORBIDDEN_CMD_PREFIXES`). Движок делает две вещи,
и обе — проверки:

1. **Перед спавном** публикующего этапа сверяет журнал двумя приговорами, и оба
   берутся из фактов, а не из формы графа (`docs/ENGINE-2.0.md`, раздел 8):

   - `review()` — приговор ревью: есть `collected` от этапа с ролью `reviewer`,
     его `commit` совпадает с базой публикуемого дерева или является её предком,
     и после него нет `changes_requested` по тому же этапу. Иначе человек или
     ревьюер одобрили одно дерево, а публикуется другое;
   - `approval()` — ответ человека на гейт: гейт-предок спросил, человек ответил
     вариантом из `allow`, и с момента вопроса дерево не менялось.

   Иначе — отказ `contract` с цитатой («одобрен commit X, в дереве Y»).
2. **После отчёта** сверяет заявку с origin: `published: {sha, branch, mr_url}` в
   `result.json` — слова исполнителя, а факт даёт `git ls-remote`
   (`runner.published_claim`). Нет заявленного sha в origin — класс `contract`,
   «публикация заявлена, но не найдена». Сверка идёт по **префиксу**: сокращённый
   sha (так его пишет публикатор и печатает консоль) законен, пока однозначен
   (`same_commit`, `origin_hits`).

Почему это отдельный слой. Здесь нет ни git, ни БД, ни часов: на вход — события
журнала, ответ гейта и текущий коммит дерева, на выход — `Approval` или причина
отказа. Даже родство коммитов спрашивается у вызывающего (`ancestor`), поэтому
правило «одобрен один, публикуется другой» проверяется без репозитория и без
модели, а `dashboard/runner.py` только исполняет его.

Одобренный коммит — факт **вопроса**, а не ответа: его записывает движок в
`question_asked.payload.routing.approved_commit`, когда гейт спрашивает (в этот
момент он и есть дерево, которое публикуется). Ответ приходит позже, и к этому
времени дерево уже могло измениться: сверка двух коммитов и есть защита от
подмены. У приговора ревью такой сверки не было вовсе — публикация обгоняла
ревью молча; её и закрывает `review()`.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

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

# Приговоры ревью: слова берутся у журнала, а не переписываются здесь — писатель
# факта (`runner.report_verdict`) и читатель гейта обязаны называть одобрение
# одинаково (`journal.VERDICT_FACTS`).
REVIEW_APPROVE = _journal().VERDICT_APPROVE
REVIEW_CHANGES = _journal().VERDICT_CHANGES

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

    `reviewer` — этап-ревьюер, чей приговор одобрил публикацию (`review()`), а
    `gate` — этап-гейт, спросивший человека (`approval()`). Поля разные, потому
    что одобряют публикацию двое и разными фактами: приговор роли и ответ
    человека; одним полем называть их значило бы путать, какой именно факт
    проверен.
    """

    ok: bool
    reason: str = ""
    question_id: str = ""
    gate: str = ""
    commit: str = ""
    answer: str = ""
    reviewer: str = ""


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


def approval(
    events: Iterable[Any],
    gates: Iterable[str],
    *,
    current: str,
    pin: Optional[bool] = True,
) -> Approval:
    """Одобрение публикации по журналу: последний вопрос гейта-предка и его ответ.

    `gates` — этапы-гейты в предках публикующего этапа (по графу спеки),
    `current` — коммит, от которого сейчас отведётся дерево публикации. Берётся
    **последний** вопрос: если человек отвечал дважды, действует последнее «ок»,
    и одобрен именно тот коммит, который стоял в дереве на момент этого вопроса.

    `pin` — сверять ли дерево с коммитом, который назвал гейт. `True`
    (публикация): гейт обязан назвать коммит, и дерево обязано с ним совпасть.
    `None` (рискованный этап, за которым публикации нет): коммита гейт не
    называет — сверять нечего, и ответ человека принимается без сверки дерева,
    но факт ответа спрашивается так же строго.

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
    if not approved and pin:
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
    if approved and (not current or not same_commit(current, approved)):
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


def reviewed_commit(events: Iterable[Any], stage: str, attempt: int, up_to: int) -> str:
    """Коммит, который видело ревью: `commit` факта `attempt_ended` его попытки.

    Движок фиксирует работу попытки коммитом в её дереве (`runner.commit_tree`), и
    у ревьюера этот коммит — то, что он прочёл: ревьюер ничего не меняет, поэтому
    `HEAD` его дерева и есть просмотренное дерево. Второй источник — `commit`
    самого отчёта (исполнитель вправе закоммитить сам).

    `attempt` — номер попытки из приговора; нулевой (старый журнал, приговор без
    номера) читается как «последняя попытка этого этапа до приговора». Берутся
    только факты **не позже** приговора (`up_to`): приговор одного раунда не
    должен одобряться коммитом следующего.
    """
    found = ""
    for event in _ordered(events):
        if str(event.get("kind") or "") != "attempt_ended":
            continue
        if _seq(event) > int(up_to or 0):
            continue
        payload = _payload(event)
        if str(payload.get("stage") or "") != str(stage):
            continue
        if attempt and _number(payload.get("attempt")) != attempt:
            continue
        found = _commit_of(payload) or found
    return found


def _commit_of(payload: dict[str, Any]) -> str:
    """Коммит попытки из её факта: `payload.commit`, иначе `result.commit`."""
    commit = payload.get("commit")
    if isinstance(commit, str) and commit.strip():
        return commit.strip()
    result = payload.get("result")
    if isinstance(result, dict):
        commit = result.get("commit")
        if isinstance(commit, str) and commit.strip():
            return commit.strip()
    return ""


def _number(value: Any) -> int:
    """Целое из payload: `True` и мусор — нулём (номер попытки не угадывается)."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def review(
    events: Iterable[Any],
    reviewers: Iterable[str],
    *,
    current: str,
    ancestor: Optional[Callable[[str, str], Optional[bool]]] = None,
) -> Approval:
    """Одобрение публикации приговором ревью: последний `collected` и что он видел.

    Жёсткая гарантия 2.0 (`docs/ENGINE-2.0.md`, раздел 8) стоит на фактах, а не на
    форме графа: граф пишет тот же лид, что по нему идёт, и может его переписать.
    Поэтому перед спавном публикующего этапа проверяется история:

    1. в журнале есть `collected` (approve) от этапа, чья роль — `reviewer`
       (`reviewers` — id таких этапов графа дела; роль этапа планом и объявлена);
    2. приговор называет попытку, и её `commit` — тот коммит, который ревью
       просмотрело. Он обязан совпасть с `current` (базой публикуемого дерева) или
       быть её **предком**: ревью могло случиться раньше последнего коммита, но не
       может быть про другую линию;
    3. после этого приговора нет `changes_requested` по тому же этапу: вердикт
       «на доработку» отменяет прежнее «ок», иначе публикация поехала бы по
       одобрению, которое ревьюер уже забрал.

    `ancestor(older, newer)` — родство коммитов; его подставляет вызывающий
    (`git merge-base --is-ancestor`), потому что в этом слое нет ни git, ни БД:
    сюда приходят только факты журнала. `None` — родство проверить нечем, и это
    отказ: «не проверили» не равно «совпало».

    Порядок отказов — от «приговора не было» к «приговор не про это дерево».
    """
    roles = {str(item) for item in reviewers or () if str(item)}
    rows = _ordered(events)
    approve: Optional[dict[str, Any]] = None
    reviewed: Optional[dict[str, Any]] = None
    changed: dict[str, int] = {}
    for event in rows:
        kind = str(event.get("kind") or "")
        if kind not in (REVIEW_APPROVE, REVIEW_CHANGES):
            continue
        payload = _payload(event)
        stage = str(payload.get("stage") or "")
        if kind == REVIEW_CHANGES:
            changed[stage] = _seq(event)
            continue
        approve = event
        if stage in roles:
            reviewed = event
    if approve is None:
        return Approval(
            ok=False,
            reason=(
                "публикация без одобрения ревью: в журнале нет факта "
                f"{REVIEW_APPROVE} — публиковать нечего"
            ),
        )
    if reviewed is None:
        stage = str(_payload(approve).get("stage") or "")
        named = ", ".join(sorted(roles)) or "—"
        return Approval(
            ok=False,
            reviewer=stage,
            reason=(
                f"approve пришёл от этапа {stage or '—'}, а его роль не reviewer: "
                f"публикацию одобряет приговор ревьюера (этапы с этой ролью: {named})"
            ),
        )
    payload = _payload(reviewed)
    stage = str(payload.get("stage") or "")
    attempt = _number(payload.get("attempt"))
    commit = reviewed_commit(rows, stage, attempt, _seq(reviewed))
    if changed.get(stage, 0) > _seq(reviewed):
        return Approval(
            ok=False,
            reviewer=stage,
            commit=commit,
            reason=(
                f"ревьюер {stage} вернул работу на доработку после approve: "
                f"действует {REVIEW_CHANGES}, а не прежнее «ок»"
            ),
        )
    if not commit:
        return Approval(
            ok=False,
            reviewer=stage,
            reason=(
                f"приговор ревьюера {stage} не называет коммит, который он видел: "
                "сверять публикацию не с чем"
            ),
        )
    if not current:
        return Approval(
            ok=False,
            reviewer=stage,
            commit=commit,
            reason="база публикуемого дерева не названа: сверять одобрение ревью не с чем",
        )
    if same_commit(commit, current):
        return Approval(ok=True, reviewer=stage, commit=commit)
    if ancestor is None:
        return Approval(
            ok=False,
            reviewer=stage,
            commit=commit,
            reason=(
                f"ревью видело commit {commit[:SHORT_SHA]}, публикуется "
                f"{current[:SHORT_SHA]}: родство коммитов проверить нечем"
            ),
        )
    related = ancestor(commit, current)
    if related is None:
        return Approval(
            ok=False,
            reviewer=stage,
            commit=commit,
            reason=(
                f"git не ответил, предок ли commit {commit[:SHORT_SHA]} для "
                f"{current[:SHORT_SHA]}: без ответа публикация не начинается"
            ),
        )
    if not related:
        return Approval(
            ok=False,
            reviewer=stage,
            commit=commit,
            reason=(
                f"ревью видело commit {commit[:SHORT_SHA]}, а публикуемая база "
                f"{current[:SHORT_SHA]}: одобренный коммит не в её истории"
            ),
        )
    return Approval(ok=True, reviewer=stage, commit=commit)


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
