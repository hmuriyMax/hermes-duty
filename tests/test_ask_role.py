"""Вопрос этапа идёт лиду: роль отвечает движку, а не пул человеку (1.0).

Проверяется то, что видно без модели, — контракт 0.1.0 («вопрос этапа идёт
лиду; лид либо отвечает, либо переадресует») и `docs/JOURNAL.md` («второй канал
не заводится: ответ роли это новая попытка, а не строка пула»):

1. фактовый вопрос из `ask.allowed` уходит **роли** (`ask_role`): пул не
   участвует, дело не паркуется, а адрес и текст вопроса ложатся в журнал;
2. ответ роли — факт `question_answered` с `by=<роль>` и `hops=<n+1>`, и этап
   поднимается **новой** попыткой, которая получает текст ответа;
3. пока роль отвечает, дело стоит в `wait_human` — отдельного вида ожидания нет;
4. `authority: true` в ответе роли — «это не мне решать»: вопрос уходит человеку
   пулом, как раньше;
5. исчерпанная цепочка (`max_hops`) — сразу человеку, без спавна роли;
6. упавшая попытка ответа вопрос не сжигает: он уходит человеку с причиной.

Часы нигде не читаются: `now` передаётся аргументом, как и в остальных слоях.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from test_runner import (  # noqa: E402 - пакета нет, pytest кладёт `tests/` в путь
    NOW,
    Answering,
    Fake,
    deals,
    events,
    journal,
    kinds,
    make_deal,
    make_home,
    only,
    open_store,
    runner,
    run,
    transit,
    write_spec,
)
from test_driver import (  # noqa: E402 - тот же приём: драйвер грузится тестом прогона
    Fake as DriverFake,
    answer_in_pool,
    driver,
    seed as seed_fact,
)


def lead_spec(tmp_path: Path, *, max_hops: int = 2, allowed: str = "lead") -> Any:
    """Спека с одним этапом, чьи фактовые вопросы адресованы лиду."""
    return write_spec(
        tmp_path,
        f"""
        duty: probe
        stages:
          - id: impl
            role: implementer
            workspace: none
            ask: {{allowed: [{allowed}], to_human: decisions_only, max_hops: {max_hops}}}
        """,
    )


def asking(
    question: str = "какой формат отчёта?", kind: str = "fact", answer: Any = None
) -> Answering:
    """Отчёт попытки, которая просит ответа: вопрос размечен как фактовый."""
    return Answering(
        answer=answer,
        result={"status": "blocked", "summary": "нужен ответ", "question": {"text": question, "kind": kind}},
    )


def contract_of(call: dict[str, Any]) -> dict[str, Any]:
    """Контракт, который движок передал процессу ключом `--query-file`."""
    cmd = call["cmd"]
    path = Path(cmd[cmd.index("--query-file") + 1])
    return json.loads(path.read_text(encoding="utf-8"))


def roles(fake: Fake) -> list[str]:
    """Профили спавнов по порядку: `-p <роль>` из команды."""
    return [call["cmd"][call["cmd"].index("-p") + 1] for call in fake.calls]


def pool_rows(home: Path) -> list[dict[str, Any]]:
    """Строки пула вопросов: у вопроса роли их быть не должно вовсе."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM questions").fetchall()]
    finally:
        conn.close()


def prefix_through(conn: sqlite3.Connection, deal: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """События дела до первого `kind` включительно — журнал «в середине» шага."""
    out: list[dict[str, Any]] = []
    for event in events(conn, deal):
        out.append(event)
        if str(event["kind"]) == kind:
            break
    return out


def test_the_lead_answers_the_stage_question_and_the_answer_is_a_fact(tmp_path: Path) -> None:
    """Фактовый вопрос `allowed: [lead]` уходит лиду; его ответ — факт журнала.

    Пул здесь не участвует: строки вопроса нет, дело не паркуется, роль читает
    вопрос из своего контракта. Ответ ложится парой `question_asked` →
    `question_answered` с автором-ролью и номером передачи — иначе через месяц
    «почему этап пошёл так» разбиралось бы по логам.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = asking()

    report = run(conn, spec, deal, home, fake)

    assert report["question_target"] == "role:lead"
    assert report["question_reason"] == "declared_role"
    assert report["question_id"] is None, "у вопроса роли нет номера пула"
    assert roles(fake) == ["implementer", "lead"], "вопрос задаёт роль из `ask.allowed`"
    asked = only(conn, deal, "question_asked")["payload"]
    assert asked["routing"] == {
        "target": "role:lead",
        "reason": "declared_role",
        "stage": "impl",
        "attempt": 1,
        "hops": 1,
    }
    assert asked["text"] == "какой формат отчёта?"
    assert "question_id" not in asked and "session_key" not in asked, "пула у вопроса роли нет"
    answered = only(conn, deal, "question_answered")["payload"]
    assert answered["by"] == "lead"
    assert answered["hops"] == 1
    assert answered["answer"] == "в db/migrations"
    assert answered["answer_len"] == len("в db/migrations")
    assert "answered_via" not in answered, "ответ роли — ответ, а не переадресация"
    assert runner.hops_done(conn, deal, "impl") == 1
    assert "blocked" not in kinds(conn, deal), "вопрос роли дело не паркует"
    assert deals.deal_row(conn, deal["id"])["status"] != "blocked"
    assert pool_rows(home) == [], "второго канала нет: в пул по-прежнему пишет только `ask()`"


def test_the_role_contract_carries_the_question_and_the_report_that_asked_it(tmp_path: Path) -> None:
    """Контракт ответа: вопрос, отчёт спросившей попытки и никакого evidence.

    Отвечающая роль не сдаёт этап: её отчёт — ответ, поэтому `evidence_to_produce`
    пуст, схема отчёта — `{answer}`, а контекст — отчёт той попытки, которая
    спросила (без него ответ был бы догадкой о том, о чём спросили).
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = asking()

    run(conn, spec, deal, home, fake)

    contract = contract_of(fake.calls[1])
    assert contract["role"] == "lead"
    assert contract["question"]["text"] == "какой формат отчёта?"
    assert contract["asked"]["stage"] == "impl"
    assert contract["asked"]["attempt"] == 1
    assert contract["asked"]["summary"] == "нужен ответ"
    assert contract["asked"]["report"]["question"]["kind"] == "fact"
    assert contract["evidence_to_produce"] == []
    assert list(contract["report_schema"]) == ["answer"]
    assert Path(contract["report_to"]).parent.parent.name == runner.ASK_ROLE_SUBDIR


def test_while_the_role_answers_the_deal_waits_human(tmp_path: Path) -> None:
    """Пока роль отвечает, дело стоит в `wait_human` — отдельного ожидания нет.

    Между фактом «вопрос задан» и фактом «ответ получен» работа не сделана, и
    срез обязан называть ожидание вопроса, а не «попытку идёт»: иначе поверхность
    показывала бы работающий этап, которого никто не запускал.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)

    run(conn, spec, deal, home, asking())

    middle = prefix_through(conn, deal, "question_asked")
    assert transit.stage_states(spec, middle)["impl"].state == "wait_human"
    waiting = transit.next_stage(spec, middle, NOW + 5)
    assert (waiting.kind, waiting.what) == (transit.WAIT, "question")
    assert journal.fold(middle, now=NOW + 5)["state"] == "wait_human"

    after = journal.fold(events(conn, deal), now=NOW + 5)
    assert after["state"] != "wait_human", "ответ роли закрывает вопрос: ожидание не вечное"


def test_the_answered_stage_runs_again_with_the_answer_in_the_contract(tmp_path: Path) -> None:
    """Ответ роли — вход **новой** попытки того же этапа, а не соседнего этапа.

    Текст ответа живёт в журнале (пула у вопроса роли нет), поэтому продолжение
    берёт его оттуда: без этого лид ответил бы, а этап продолжил бы работу, не
    увидев ответа, — и спросил бы то же самое второй раз.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = asking()

    run(conn, spec, deal, home, fake)
    decision = transit.next_stage(spec, events(conn, deal), NOW + 5)
    assert (decision.kind, decision.pair) == (transit.RUN, [("impl", 2)])

    second = runner.run_stage(conn, spec, deal, "impl", 2, home=home, spawn=fake, clock=lambda: NOW + 5)

    assert second["started"]
    contract = contract_of(fake.calls[2])
    assert contract["answer_this_attempt"] == "в db/migrations"
    assert contract["attempt"] == 2
    assert contract["role"] == "implementer"


def test_authority_in_the_role_answer_sends_the_question_to_the_human(tmp_path: Path) -> None:
    """`authority: true` — «это не мне решать»: вопрос идёт человеку, не сгорая.

    Переадресация — не отказ роли: вопрос тот же, адресат новый. Передача роли
    закрывается фактом `redirected`, а человеческий вопрос получает адрес
    `human` с причиной `role_redirected` — по ней видно, почему спрашивают
    человека, хотя спека объявила роль.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = asking(answer={"answer": {"text": "", "authority": True}})

    report = run(conn, spec, deal, home, fake)

    assert report["question_target"] == "human"
    assert report["question_reason"] == "role_redirected"
    assert report["question_id"] is not None, "переадресованный вопрос ждёт человека в пуле"
    forwarded = [event for event in events(conn, deal) if event["kind"] == "question_asked"]
    assert len(forwarded) == 2, "сначала вопрос роли, потом вопрос человеку"
    assert forwarded[1]["payload"]["routing"]["target"] == "human"
    assert forwarded[1]["payload"]["routing"]["reason"] == "role_redirected"
    answered = only(conn, deal, "question_answered")["payload"]
    assert answered["by"] == "lead"
    assert answered["answered_via"] == "redirected"
    assert "authority" in answered["reason"]
    assert pool_rows(home)[0]["text"] == "какой формат отчёта?", "вопрос не сгорел"
    block = only(conn, deal, "blocked")["payload"]
    assert "role_redirected" in block["reason"]
    assert journal.fold(events(conn, deal), now=NOW + 5)["state"] == "wait_human"
    # Переадресация — не срок: вопрос жив и ждёт **человека**, поэтому этап стоит
    # в `wait_human`, а не «припаркован по сроку без ответа».
    states = transit.stage_states(spec, events(conn, deal))
    assert states["impl"].state == "wait_human"
    decision = transit.next_stage(spec, events(conn, deal), NOW + 5)
    assert (decision.kind, decision.what) == (transit.WAIT, "question")


def test_exhausted_chain_asks_the_human_without_spawning_the_role(tmp_path: Path) -> None:
    """`max_hops: 1` — второй вопрос сразу человеку: цепочка конечна.

    Передачи считаются фактами журнала (`hops_done`), а не счётом в памяти,
    поэтому второй вопрос того же этапа уже не идёт роли: спавна нет, вопрос в
    пуле с причиной `hops_exhausted`.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path, max_hops=1)
    fake = asking()

    run(conn, spec, deal, home, fake)
    assert roles(fake) == ["implementer", "lead"]
    second = runner.run_stage(conn, spec, deal, "impl", 2, home=home, spawn=fake, clock=lambda: NOW + 5)

    assert second["question_target"] == "human"
    assert second["question_reason"] == "hops_exhausted"
    assert roles(fake) == ["implementer", "lead", "implementer"], "цепочка исчерпана — спавна роли нет"
    asked = [event for event in events(conn, deal) if event["kind"] == "question_asked"]
    assert asked[-1]["payload"]["routing"]["reason"] == "hops_exhausted"
    assert asked[-1]["payload"]["question_id"] == second["question_id"]
    assert runner.hops_done(conn, deal, "impl") == 2, "передачи считаются фактами журнала"


def test_a_failed_answer_attempt_hands_the_question_to_the_human(tmp_path: Path) -> None:
    """Упавшая попытка ответа — вопрос человеку с причиной, а не тишина.

    Роль не ответила (отчёт без `answer.text`) — это отказ `contract` её попытки,
    а не потеря вопроса: он уходит человеку, передача закрывается фактом
    `failed` с причиной, и в причине блока она тоже названа.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = asking(answer={"status": "done", "summary": "отвечать не стал"})

    report = run(conn, spec, deal, home, fake)

    assert report["question_target"] == "human"
    assert report["question_reason"] == "role_answer_failed"
    answered = only(conn, deal, "question_answered")["payload"]
    assert answered["answered_via"] == "failed"
    assert "не назвала answer.text" in answered["reason"]
    assert pool_rows(home)[0]["text"] == "какой формат отчёта?", "вопрос не сгорел"
    block = only(conn, deal, "blocked")["payload"]
    assert "role_answer_failed" in block["reason"]
    assert "не назвала answer.text" in block["reason"], "причина отказа роли видна человеку"
    assert transit.stage_states(spec, events(conn, deal))["impl"].state == "wait_human", (
        "упавшая роль — не истёкший вопрос: он ждёт человека"
    )


def test_a_role_that_left_without_a_report_does_not_burn_the_question(tmp_path: Path) -> None:
    """Роль вышла, не отчитавшись: класс отказа `infra`, вопрос — человеку.

    Класс берётся из источника (нет отчёта), а не из текста: упавшая роль — это
    инфраструктура передачи, и «вопрос потерян» читалось бы как решение роли.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = Silent(result={"status": "blocked", "summary": "нужен ответ", "question": {"text": "какой формат отчёта?", "kind": "fact"}})

    report = run(conn, spec, deal, home, fake)

    assert report["question_target"] == "human"
    assert report["question_reason"] == "role_answer_failed"
    answered = only(conn, deal, "question_answered")["payload"]
    assert answered["answered_via"] == "failed"
    assert "без ответа" in answered["reason"]
    assert report["question_id"] is not None


class Silent(Fake):
    """Спавн роли, который не пишет отчёт вовсе: вопрос не должен сгореть.

    Попытка этапа отчитывается как обычно, а на контракт ответа (`answer_report`)
    процесс молчит и выходит с кодом 1: ровно то, что даёт живая роль, свалившаяся
    до `write_file`.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("exit_code", 1)
        super().__init__(**kwargs)

    def _report(self, query_file: Path, cwd: Path) -> None:
        try:
            contract = json.loads(query_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            contract = {}
        if isinstance(contract, dict) and contract.get("answer_report"):
            return
        super()._report(query_file, cwd)


def test_marked_question_keeps_its_marking_out_of_the_role_chain(tmp_path: Path) -> None:
    """Властный вопрос не идёт роли вовсе: `risky`/`authority` сильнее `allowed`.

    Адрес решает `routing.route`, и разметка вопроса сильнее объявленного
    списка ролей: «не мне решать» не превращается в роль из спеки. Спавна роли
    в такой цепочке нет, вопрос идёт человеку с причиной `authority`.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = asking(kind="authority")

    report = run(conn, spec, deal, home, fake)

    assert report["question_target"] == "human"
    assert report["question_reason"] == "authority"
    assert roles(fake) == ["implementer"], "властный вопрос роли не задаётся"
    assert "question_answered" not in kinds(conn, deal)
    assert runner.hops_done(conn, deal, "impl") == 1, "вопрос человеку — одна передача, учтённая журналом"


def test_a_bare_string_answer_report_is_the_same_answer(tmp_path: Path) -> None:
    """`answer` строкой — тот же ответ: короткая форма отчёта не превращается в отказ.

    Роль, которая пишет `{"answer": "так"}`, ответила; требовать от неё словаря
    значило бы отказывать по форме там, где смысл назван.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = asking(answer={"answer": "в db/migrations"})

    report = run(conn, spec, deal, home, fake)

    answered = only(conn, deal, "question_answered")["payload"]
    assert (report["question_target"], answered["by"], answered["answer"]) == (
        "role:lead",
        "lead",
        "в db/migrations",
    )


def test_authority_is_only_the_explicit_true(tmp_path: Path) -> None:
    """`authority` — только явное `true`: строка `"true"` переадресацией не считается.

    Ответ, размеченный `authority: "true"` (строка) или `1`, — обычный ответ
    роли: иначе опечатка в отчёте решала бы за человека и переадресовывала
    вопрос, который роль вправе решить сама.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    fake = asking(answer={"answer": {"text": "оставляем как есть", "authority": "true"}})

    report = run(conn, spec, deal, home, fake)

    assert report["question_target"] == "role:lead"
    assert pool_rows(home) == []
    assert only(conn, deal, "question_answered")["payload"]["answer"] == "оставляем как есть"


# ------------------------------------- оборванная передача: вопрос не сгорает


def lost_role_question(conn: sqlite3.Connection, deal: dict[str, Any], *, text: str = "какой формат отчёта?") -> None:
    """Журнал «движок ушёл между вопросом и ответом»: пара не закрыта.

    Ровно те факты, которые движок успел записать до жёсткой смерти: попытка
    этапа закрылась `done`, вопрос ушёл роли — и ни ответа, ни строки пула. Так
    выглядит SIGKILL/разрыв гейтвея посреди `ask_role`, и по этим фактам дело
    не должно оставаться ждущим вечно.
    """
    seed_fact(
        conn,
        deal,
        "attempt_started",
        {"run_id": "impl#1", "stage": "impl", "attempt": 1, "pid": 4242},
        key="test:impl:1:started",
    )
    seed_fact(
        conn,
        deal,
        "attempt_ended",
        {"run_id": "impl#1", "stage": "impl", "attempt": 1, "outcome": "done"},
        key="test:impl:1:ended",
    )
    seed_fact(
        conn,
        deal,
        "question_asked",
        {
            "routing": {
                "target": "role:lead",
                "reason": "declared_role",
                "stage": "impl",
                "attempt": 1,
                "hops": 1,
            },
            "text": text,
        },
        key="test:impl:1:asked",
    )


def test_a_lost_role_answer_does_not_leave_the_deal_waiting_forever(tmp_path: Path) -> None:
    """Ответа роли не будет — вопрос уходит человеку, а не висит в `wait_human`.

    Движок задаёт вопрос роли и получает ответ одним вызовом, поэтому открытый
    вопрос роли, до которого дожил проход по журналу, никто не дожидается: строки
    пула у него нет (ответить нечем), а спавна роли больше не будет (попытка
    закрыта). Без восстановления дело стояло бы так вечно — то есть работа
    терялась бы. Проверяется весь путь: передача закрывается фактом
    `answered_via='failed'`, вопрос ложится в пул с причиной `role_answer_failed`,
    дело паркуется `needs_input`, и **следующий** прогон идёт дальше — как только
    человек ответил.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)
    lost_role_question(conn, deal)

    report = driver.run(conn, deal, spec=spec, home=home, spawn=DriverFake(), clock=lambda: NOW)

    assert [row["role"] for row in report["recovered"]] == ["lead"], "восстановление названо в отчёте"
    answered = only(conn, deal, "question_answered")["payload"]
    assert answered["answered_via"] == "failed"
    assert answered["by"] == "lead"
    assert answered["hops"] == 1, "передача одна: номер у вопроса и у ответа совпадает"
    assert "движок" in answered["reason"], "причина называет, почему ответа не стало"
    asked = [event for event in events(conn, deal) if event["kind"] == "question_asked"]
    assert asked[-1]["payload"]["routing"]["target"] == "human"
    assert asked[-1]["payload"]["routing"]["reason"] == "role_answer_failed"
    question_id = report["recovered"][0]["question_id"]
    assert asked[-1]["payload"]["question_id"] == question_id
    assert pool_rows(home)[0]["text"] == "какой формат отчёта?", "вопрос не сгорел"
    block = only(conn, deal, "blocked")["payload"]
    assert block["block_kind"] == "needs_input"
    assert f"#{question_id}" in block["reason"]
    assert deals.deal_row(conn, deal["id"])["status"] == "blocked"
    assert report["decision"]["what"] == "question", "дело ждёт ответа человека, а не тишины"
    assert str(question_id) in report["decision"]["reason"]

    # Повторный прогон двигает дело дальше: ответ человека — вход новой попытки.
    # Часы — живые: `answered_at` пула настоящий, и ответ обязан лечь позже
    # вопросов, а не раньше (журнал применяет факты по времени).
    answer_in_pool(home, int(question_id), answer="в db/migrations")
    second = driver.run(
        conn, deal, spec=spec, home=home, spawn=DriverFake(), clock=time.time
    )

    assert second["recovered"] == [], "восстанавливать больше нечего"
    assert [(row["stage"], row["attempt"]) for row in second["steps"]] == [("impl", 2)]
    assert deals.deal_row(conn, deal["id"])["status"] == "done", "дело пошло дальше и закрылось"


def test_a_human_question_is_not_recovered_as_a_lost_role_one(tmp_path: Path) -> None:
    """Вопрос человека восстановление не трогает: у него есть номер и адресат.

    Переспрашивать за человека нечего — он и есть адресат. Восстановление,
    закрывшее такой вопрос «от имени роли», отняло бы у дела единственный живой
    канал ответа, поэтому проверяется, что второго вопроса и второго ответа нет.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = lead_spec(tmp_path)

    run(conn, spec, deal, home, asking(kind="authority"))
    report = driver.run(conn, deal, spec=spec, home=home, spawn=DriverFake(), clock=lambda: NOW)

    assert report["recovered"] == []
    assert len([event for event in events(conn, deal) if event["kind"] == "question_asked"]) == 1
    assert "question_answered" not in kinds(conn, deal)
    assert len(pool_rows(home)) == 1


def test_an_open_attempt_is_not_recovered(tmp_path: Path) -> None:
    """Пока попытка идёт, восстановления нет: вопрос ещё может дождаться ответа.

    Вопрос роли задаётся по **закрытой** попытке (`attempt_ended` пишется до
    него), поэтому открытая попытка означает «спрашивать рано»: закрыть передачу
    здесь значило бы отнять вопрос у живой роли.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    seed_fact(
        conn,
        deal,
        "attempt_started",
        {"run_id": "impl#1", "stage": "impl", "attempt": 1, "pid": 4242},
        key="test:impl:1:started",
    )
    seed_fact(
        conn,
        deal,
        "question_asked",
        {
            "routing": {"target": "role:lead", "reason": "declared_role", "stage": "impl", "attempt": 1, "hops": 1},
            "text": "какой формат отчёта?",
        },
        key="test:impl:1:asked",
    )

    assert runner.recover_role_questions(conn, deal, home=home, clock=lambda: NOW) == []
    assert "question_answered" not in kinds(conn, deal)
    assert pool_rows(home) == []
