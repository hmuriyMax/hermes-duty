"""Правило публикации само по себе: без репозитория, без БД, без часов.

`dashboard/publish.py` — чистый слой: на вход события журнала, ответ гейта и
текущий коммит дерева, на выход `Approval` или причина отказа. Поэтому главные
проверки карточки S4 («одобрен один коммит, публикуется другой», «человек не дал
ок», «вопроса не было») проверяются здесь словами, а не прогоном git: движок в
`tests/test_runner.py` только исполняет этот приговор.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]

# Метка времени у всех фактов одна: порядок обязан задаваться `id`, а не `ts`.
NOW = 1_790_000_000

# Одобренный коммит и то, что появилось в дереве после ответа.
APPROVED = "a" * 40
MOVED = "b" * 40


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути — под тем же именем, что и у остального кода."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


publish = load_module("hermes_duty_publish", "publish.py")


def events(*items: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """События журнала в том виде, в каком их отдаёт `journal.read_events`."""
    return [
        {"id": index + 1, "kind": kind, "ts": NOW, "payload": payload}
        for index, (kind, payload) in enumerate(items)
    ]


def asked(
    question_id: int = 7,
    *,
    gate: str = "gate",
    approved_commit: Optional[str] = APPROVED,
    kind: str = "question_asked",
) -> tuple[str, dict[str, Any]]:
    """Факт вопроса гейта: адрес этапа и одобренный коммит живут в `routing`."""
    routing: dict[str, Any] = {"stage": gate, "attempt": 1}
    if approved_commit is not None:
        routing["approved_commit"] = approved_commit
    return kind, {"question_id": question_id, "routing": routing}


def answered(question_id: int = 7, *, answer_len: int = 6, via: Optional[str] = None) -> tuple[str, dict[str, Any]]:
    """Факт ответа: текст живёт в пуле, в журнале — длина и способ закрытия."""
    payload: dict[str, Any] = {"question_id": question_id, "answer_len": answer_len}
    if via is not None:
        payload["answered_via"] = via
    return "question_answered", payload


def verdict(
    question_id: int = 7,
    *,
    gate: str = "gate",
    outcome: str = "done",
    kind: Optional[str] = None,
    reason: str = "гейт: ответ «да» входит в allow [да]",
    answer: str = "да",
) -> tuple[str, dict[str, Any]]:
    """Приговор гейта: `attempt_ended` с номером вопроса в `details`."""
    return "attempt_ended", {
        "stage": gate,
        "attempt": 2,
        "outcome": outcome,
        "kind": kind,
        "reason": reason,
        "details": {"stage_kind": "gate", "question_id": question_id, "answer": answer},
    }


# ------------------------------------------------------------------ одобрение


def test_approval_without_gate_question_names_the_rule():
    """Вопроса не было — публиковать нечего, и причина называет правило спеки."""
    result = publish.approval(events(("created", {"status": "ready"})), ["gate"], current=APPROVED)

    assert result.ok is False
    assert result.commit == ""
    assert "публикация требует вопроса человеку (kind: gate) в предках" in result.reason
    assert "гейт gate не спрашивал" in result.reason


def test_approval_of_a_foreign_gate_does_not_count():
    """Вопрос чужого гейта (не предка) одобрением не становится: считается граф спеки."""
    result = publish.approval(events(asked(gate="other")), ["gate"], current=APPROVED)

    assert result.ok is False
    assert "не спрашивал" in result.reason


def test_approval_without_named_commit_is_refused():
    """Вопрос без `approved_commit` — сверять не с чем: одобрение не выдумывается."""
    result = publish.approval(events(asked(approved_commit=None)), ["gate"], current=APPROVED)

    assert result.ok is False
    assert result.gate == "gate"
    assert result.question_id == "7"
    assert "не назвал одобренный commit" in result.reason


def test_approval_without_answer_is_refused():
    """Вопрос задан, ответа нет — «публиковать нечего», а не «одобрено»."""
    result = publish.approval(events(asked()), ["gate"], current=APPROVED)

    assert result.ok is False
    assert result.commit == APPROVED
    assert "без ответа" in result.reason


def test_expired_question_is_not_an_approval():
    """Истёкший вопрос закрыл не человек: тишина одобрением не становится."""
    result = publish.approval(events(asked(), answered(answer_len=0, via="expired")), ["gate"], current=APPROVED)

    assert result.ok is False
    assert "человек не ответил" in result.reason


def test_answer_without_gate_verdict_is_refused():
    """Ответ есть, приговора гейта нет — судить ответ нечем, публикация ждёт."""
    result = publish.approval(events(asked(), answered()), ["gate"], current=APPROVED)

    assert result.ok is False
    assert "приговора нет" in result.reason


def test_verdict_of_another_question_does_not_count():
    """Приговор относится к вопросу по номеру, а не «к последнему ответу вообще»."""
    result = publish.approval(
        events(asked(7), answered(7), verdict(9)),
        ["gate"],
        current=APPROVED,
    )

    assert result.ok is False
    assert "приговора нет" in result.reason


def test_answer_outside_allow_is_not_an_approval():
    """«Не публиковать» — отказ человека, и причина цитирует приговор гейта."""
    result = publish.approval(
        events(
            asked(),
            answered(),
            verdict(outcome="failed", kind="contract", reason="гейт: ответ «нет» вне allow [да]", answer="нет"),
        ),
        ["gate"],
        current=APPROVED,
    )

    assert result.ok is False
    assert result.answer == "нет"
    assert "человек не дал ок" in result.reason
    assert "«нет» вне allow" in result.reason


def test_approval_of_the_same_tree_is_ok():
    """Ответ из `allow` и то же дерево, что одобряли: публиковать можно именно этот коммит."""
    result = publish.approval(
        events(asked(), answered(), verdict()),
        ["gate"],
        current=APPROVED,
    )

    assert result.ok is True
    assert (result.commit, result.gate, result.question_id) == (APPROVED, "gate", "7")
    assert result.answer == "да"


def test_approval_of_a_moved_tree_is_refused():
    """Красная проба карточки: одобрен один коммит, а в дереве уже другой.

    Без этой сверки человек одобрял бы `a…`, а в origin уходил `b…` — ровно та
    подмена, от которой правило и заведено. Цитата обязана назвать оба коммита.
    """
    result = publish.approval(
        events(asked(), answered(), verdict()),
        ["gate"],
        current=MOVED,
    )

    assert result.ok is False
    assert f"одобрен commit {APPROVED[:12]}" in result.reason
    assert f"в дереве {MOVED[:12]}" in result.reason
    assert result.commit == APPROVED


def test_empty_tree_is_not_an_approval():
    """Пустой коммит в дереве — не «совпало»: публиковать нечего."""
    result = publish.approval(events(asked(), answered(), verdict()), ["gate"], current="")

    assert result.ok is False
    assert "в дереве —" in result.reason


def test_last_question_wins_whatever_the_timestamps():
    """Порядок — по `id`, а не по `ts`: два вопроса в одну секунду различаются номером.

    Внутри секунды `ts` у фактов одинаков, и сортировка по нему сделала бы выбор
    «какой вопрос последний» случайным — а от него зависит, какой коммит одобрен.
    """
    rows = events(
        asked(7, approved_commit=APPROVED),
        answered(7),
        verdict(7),
        asked(8, approved_commit=MOVED),
        answered(8),
        verdict(8),
    )
    for row in rows:
        row["ts"] = NOW

    result = publish.approval(rows, ["gate"], current=MOVED)

    assert result.ok is True
    assert (result.question_id, result.commit) == ("8", MOVED)


def test_second_question_without_answer_keeps_the_deal_waiting():
    """Второй вопрос перекрывает первое «ок»: одобрение относится к последнему вопросу."""
    result = publish.approval(
        events(asked(7), answered(7), verdict(7), asked(8, approved_commit=MOVED)),
        ["gate"],
        current=MOVED,
    )

    assert result.ok is False
    assert "вопрос #8" in result.reason and "без ответа" in result.reason


# --------------------------------------------------------------------- заявка


def test_claim_without_report_is_refused():
    """Отчёта нет — сверять с origin нечего."""
    claim, why = publish.claim(None)

    assert claim is None
    assert "отчёта нет" in why


def test_claim_without_published_is_refused():
    """Публикующий этап отчитался без `published`: «опубликовано» — слово, а не факт."""
    claim, why = publish.claim({"status": "done", "summary": "запушил"})

    assert claim is None
    assert "нет published" in why


def test_claim_without_sha_is_refused():
    """Заполненная заявка без sha не говорит, что именно ушло в origin."""
    claim, why = publish.claim({"published": {"branch": "main", "mr_url": ""}})

    assert claim is None
    assert "без sha" in why


def test_claim_with_non_string_field_is_refused():
    """Поле заявки — строка: число вместо неё читалось бы как «почти правильно»."""
    claim, why = publish.claim({"published": {"sha": 12345}})

    assert claim is None
    assert "published.sha — строка" in why


def test_claim_names_the_three_fields_and_strips_them():
    """Заявка закрыта тремя полями; пробелы по краям — не часть ссылки."""
    claim, why = publish.claim(
        {"published": {"sha": " aaa ", "branch": "origin/main ", "mr_url": " https://mr/1"}}
    )

    assert why == ""
    assert claim == {"sha": "aaa", "branch": "origin/main", "mr_url": "https://mr/1"}


def test_claim_without_branch_is_legal():
    """Публикация по тегу или в известную ветку: sha без ветки — законная заявка."""
    claim, why = publish.claim({"published": {"sha": "aaa"}})

    assert why == ""
    assert claim == {"sha": "aaa", "branch": "", "mr_url": ""}


# ------------------------------------------------------------- факт журнала


def test_published_of_is_none_until_the_engine_confirms():
    """`None` — публикации не было: поверхность не выдумывает нули."""
    assert publish.published_of(events(("created", {"status": "ready"}))) is None
    assert publish.published_of([]) is None


def test_published_of_returns_the_last_fact():
    """Несколько фактов (повторная публикация) — действует последний по журналу."""
    rows = events(
        ("published", {"stage": "publish", "attempt": 1, "sha": APPROVED, "branch": "main"}),
        ("published", {"stage": "publish", "attempt": 2, "sha": MOVED, "branch": "main"}),
    )

    fact = publish.published_of(rows)

    assert fact is not None
    assert (fact["attempt"], fact["sha"]) == (2, MOVED)
