"""Вопросы воркера: три исхода лида (`docs/ENGINE-2.0.md`, раздел 6).

Проверяется то, что видно в журнале и в собранной команде попытки, а не в
docstring:

1. `duty_answer` продолжает **сессию той же попытки**: факт
   `question_answered(by: lead)` с текстом, а поднятая попытка собирает команду с
   `--resume <session_id из attempt_ended.details>`;
2. `duty_ask` поднимает под-попытку профиля (`runner.ask_role`), ответ
   возвращается лиду строкой, факты `question_asked` → `question_answered(by:
   <роль>)` несут номер передачи, а пул не участвует;
3. `duty_ask_human` кладёт вопрос в пул через `ask.ask` и переводит дело в
   `wait_human`, а ответ человека приходит лиду следующим фактом (`duty_wait`
   дочитывает пул);
4. повтор `duty_answer` с тем же ключом не пишет второго факта и не поднимает
   второй попытки.

Спавн попытки подменяется фейком, который проходит **настоящий** резюм-путь
`runner.run_stage`: тест проверяет сборку команды, а не живой `hermes chat`.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from test_runner import (  # noqa: E402 - пакета нет, pytest кладёт `tests/` в путь
    NOW,
    Fake,
    deals,
    journal,
    make_home,
    open_store,
    runner,
    spec_module,
    transit,
    write_spec,
)

ROOT = Path(__file__).resolve().parents[1]

# Этап без дерева и без evidence: вопрос и ответ проверяются на фактах, а не на
# пакете доказательств.
SEED = """
duty: вопросы лида
repo: ~/GolandProjects/duty
stages:
  - id: impl
    role: implementer
    workspace: none
"""

# Сессия воркера, которую обязан продолжить ответ лида: `--resume` берёт её из
# `attempt_ended.details` (`runner.last_session`).
WORKER_SESSION = "local-worker-7f3a"


def tools_module() -> Any:
    """`dashboard/tools.py` под тем же именем, что у плагина (одна загрузка)."""
    import sys

    loaded = sys.modules.get("hermes_duty_tools")
    if loaded is not None:
        return loaded
    import importlib.util

    path = ROOT / "dashboard" / "tools.py"
    spec = importlib.util.spec_from_file_location("hermes_duty_tools", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["hermes_duty_tools"] = module
    spec.loader.exec_module(module)
    return module


tools = tools_module()


# ------------------------------------------------------------------ фикстуры


def make_deal(conn: sqlite3.Connection, spec: Any) -> dict[str, Any]:
    """Своё дело: строка store плюс путь спеки, записанный в `created`."""
    created = deals.create_deal(
        conn,
        repo="duty",
        title="Вопросы воркера",
        body="",
        now=NOW,
        spec_path=str(spec.path),
    )
    deal = deals.deal_row(conn, str(created["deal"]["id"]))
    assert deal is not None
    return deal


def seed_worker_question(conn: sqlite3.Connection, deal: dict[str, Any], number: int = 41) -> None:
    """Журнал «воркер спросил и запарковался»: попытка, сессия, вопрос, блок.

    Так выглядит дело после `runner.finish_agent_stage`: `attempt_ended` с
    `details.session_id` (точка возврата резюма), `question_asked` с адресом
    этапа (`routing`) и `blocked(needs_input)`, который держит дело в ожидании.
    """
    journal.append(
        conn,
        [
            {
                "ts": NOW,
                "recorded_at": NOW,
                "deal_id": str(deal["id"]),
                "kind": "attempt_started",
                "payload": {
                    "run_id": "impl#1",
                    "stage": "impl",
                    "attempt": 1,
                    "pid": 111,
                    "lease_expires": NOW + 3600,
                    "session_id": None,
                },
                "source": "engine",
                "idempotency_key": "test:impl:1:started",
            },
            {
                "ts": NOW + 1,
                "recorded_at": NOW + 1,
                "deal_id": str(deal["id"]),
                "kind": "attempt_ended",
                "payload": {
                    "run_id": "impl#1",
                    "stage": "impl",
                    "attempt": 1,
                    "outcome": "done",
                    "kind": None,
                    "reason": "",
                    "details": {"session_id": WORKER_SESSION, "workdir": "/tmp/impl-1"},
                },
                "source": "engine",
                "idempotency_key": "test:impl:1:ended",
            },
            {
                "ts": NOW + 2,
                "recorded_at": NOW + 2,
                "deal_id": str(deal["id"]),
                "kind": "question_asked",
                "payload": {
                    "question_id": number,
                    "session_key": f"t_{deal['id']}",
                    "critical": 0,
                    "timeout": None,
                    "routing": {"target": "human", "reason": "risky", "stage": "impl", "attempt": 1},
                },
                "source": "ask",
                "idempotency_key": f"ask:{number}:question_asked",
            },
        ],
    )
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind="needs_input",
        reason=f"попытка impl#1: вопрос #{number} адресован human (risky) — без ответа",
        author="engine",
        source="engine",
        now=NOW + 3,
    )


def process_spawner(home: Path, spec: Any, answer_seen: list[Any], fake: Fake) -> Any:
    """Спавн попытки лида: проходит настоящий `runner.run_stage` с фейком процесса.

    Так проверяется резюм-путь целиком — контракт на диске и собранная команда, —
    а не только факт «лид позвал спавн».
    """

    def spawn(
        home_: Path,
        deal_id: str,
        stage_id: str,
        attempt: int,
        key: str,
        answer: Any = None,
    ) -> int:
        answer_seen.append(answer)
        conn = open_store(home_)
        try:
            deal = deals.deal_row(conn, deal_id)
            assert deal is not None
            runner.run_stage(
                conn,
                spec,
                deal,
                stage_id,
                attempt,
                home=home_,
                answer=answer,
                idem=key,
                spawn=fake,
                clock=lambda: NOW + 10,
            )
        finally:
            conn.close()
        return 4242

    return spawn


class RoleAnswer:
    """Спавн профиля-адресата: отвечает по контракту ответа (`answer_report`).

    `runner.ask_role` читает `result.json` из рабочего каталога ответа, поэтому
    фейк кладёт туда `{"answer": {"text": …, "authority": …}}` — ту же форму,
    которую пишет настоящий профиль.
    """

    def __init__(self, text: str = "", authority: bool = False, exit_code: int = 0) -> None:
        self.text = text
        self.authority = authority
        self.exit_code = exit_code
        self.calls: list[dict[str, Any]] = []
        self.contracts: list[dict[str, Any]] = []
        self.stderr_text = ""

    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "RoleAnswer":
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        contract_file = Path(cmd[cmd.index("--query-file") + 1])
        contract = json.loads(contract_file.read_text(encoding="utf-8"))
        self.contracts.append(contract)
        Path(contract["report_to"]).write_text(
            json.dumps({"answer": {"text": self.text, "authority": self.authority}}, ensure_ascii=False),
            encoding="utf-8",
        )
        return self

    def wait(self, timeout: Any = None) -> int:
        return self.exit_code

    def stop(self, grace: float = 10) -> int:
        return self.exit_code


def facts(conn: sqlite3.Connection, deal: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """Payload'ы фактов одного вида по порядку журнала."""
    return [
        journal.payload_of(event)
        for event in journal.read_events(conn, str(deal["id"]))
        if str(event["kind"]) == kind
    ]


def kinds(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[str]:
    return [str(event["kind"]) for event in journal.read_events(conn, str(deal["id"]))]


def pool_rows(home: Path) -> list[dict[str, Any]]:
    """Строки пула решений: вопросы лида человеку видны только там и в журнале."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM questions").fetchall()]
    finally:
        conn.close()


def answer_human(home: Path, number: int, text: str) -> None:
    """Человек ответил в пуле: строка `answered` — так её видит `ask.sync_answers`."""
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        conn.execute(
            "UPDATE questions SET status = 'answered', answer = ?, answered_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (text, number),
        )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------- 1. ответ лида продолжает сессию


def test_lead_answer_resumes_the_worker_session(tmp_path: Path) -> None:
    """Ответ лида — факт `question_answered(by: lead)` и продолжение той же сессии.

    Ключевое отличие исхода 1 от исхода 2 (`docs/ENGINE-2.0.md`, раздел 6): работа
    не начинается заново. Значит в собранной команде продолжения обязан быть
    `--resume` с `session_id` из `attempt_ended.details`, а во входе попытки —
    текст ответа.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    seed_worker_question(conn, deal, 41)
    process = Fake(pack=True, result={"status": "done", "summary": "продолжил по ответу лида"})
    seen: list[Any] = []

    report = tools.answer(
        str(deal["id"]),
        "impl",
        41,
        "делай по варианту Б: формат JSON",
        "answer-1",
        home=home,
        spawn=process_spawner(home, spec, seen, process),
        clock=lambda: NOW + 10,
        ack_sec=0,
    )

    assert report["ok"] is True
    assert report["answered"] is True
    assert report["stage"] == "impl"
    assert report["attempt"] == 2
    # Спавн получил текст ответа: продолжение без ответа было бы новой работой.
    assert seen == ["делай по варианту Б: формат JSON"]
    # Собранная команда — резюм сессии воркера, а не новый разговор.
    command = process.calls[0]["cmd"]
    assert "--resume" in command
    assert command[command.index("--resume") + 1] == WORKER_SESSION
    contract = json.loads(
        runner.contract_path(runner.attempt_dir(home, deal, "impl", 2), "impl", 2).read_text(
            encoding="utf-8"
        )
    )
    assert contract["answer_this_attempt"] == "делай по варианту Б: формат JSON"
    assert contract["session_id"] == WORKER_SESSION
    # Журнал: ответ с автором-лидом и текстом; вопрос попытки закрыт блоком снят.
    answered = facts(conn, deal, "question_answered")
    assert len(answered) == 1
    assert answered[0]["by"] == "lead"
    assert answered[0]["answer"] == "делай по варианту Б: формат JSON"
    assert str(answered[0]["question_id"]) == "41"
    assert answered[0]["routing"] == {"stage": "impl", "attempt": 1}
    assert "unblocked" in kinds(conn, deal)
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "running"


# ------------------------------------------------------- 2. под-попытка профиля


def test_lead_asks_a_role_and_gets_the_answer_as_a_string(tmp_path: Path) -> None:
    """`duty_ask` поднимает под-попытку роли: ответ строкой лиду, хопы в фактах.

    Пул не участвует (`docs/JOURNAL.md`: второго канала нет), а пара
    `question_asked` → `question_answered` ложится в журнал с автором-ролью и
    номером передачи — по нему видно, что вопрос прошёл цепочку, а не решился
    сам.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    seed_worker_question(conn, deal, 41)
    role = RoleAnswer(text="ответ в JSON: {answer}")

    report = tools.ask(
        str(deal["id"]),
        "researcher",
        "в каком формате отдавать отчёт?",
        "ask-1",
        home=home,
        spawn=role,
        clock=lambda: NOW + 10,
    )

    assert report["ok"] is True
    assert report["answer"] == "ответ в JSON: {answer}"
    assert report["by"] == "researcher"
    # Вопрос воркера — уже одна передача этапа, ответ роли — вторая.
    assert report["hops"] == 2
    # Процесс роли поднят профилем-адресатом, а не ролью этапа.
    profile = role.calls[0]["cmd"][role.calls[0]["cmd"].index("-p") + 1]
    assert profile == spec_module.spawn_profile("researcher")
    assert role.contracts[0]["role"] == "researcher"
    assert role.contracts[0]["question"]["text"] == "в каком формате отдавать отчёт?"
    assert role.contracts[0]["asked"]["stage"] == "impl"
    # Пул не тронут: вопрос профилю — журнал дела.
    assert pool_rows(home) == []
    asked = [item for item in facts(conn, deal, "question_asked") if item.get("routing", {}).get("target") == "role:researcher"]
    assert len(asked) == 1
    assert asked[0]["text"] == "в каком формате отдавать отчёт?"
    assert asked[0]["routing"]["stage"] == "impl"
    assert asked[0]["routing"]["hops"] == 2
    # Текст вопроса — без номера пула: этот канал его не заводит.
    assert "question_id" not in asked[0]
    role_answer = [item for item in facts(conn, deal, "question_answered") if item.get("by") == "researcher"]
    assert len(role_answer) == 1
    assert role_answer[0]["answer"] == "ответ в JSON: {answer}"
    assert role_answer[0]["hops"] == 2


def test_lead_ask_reports_a_redirect_to_the_human(tmp_path: Path) -> None:
    """Роль сказала «это не мне решать»: лид получает отказ, вопрос не сгорает.

    Вопрос не задаётся заново и не теряется: передача закрывается фактом с
    `answered_via='redirected'`, а следующий шаг лида — `duty_ask_human`.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    seed_worker_question(conn, deal, 41)
    role = RoleAnswer(text="", authority=True)

    report = tools.ask(
        str(deal["id"]),
        "reviewer",
        "можно публиковать?",
        "ask-2",
        home=home,
        spawn=role,
        clock=lambda: NOW + 10,
    )

    assert report["ok"] is False
    assert report["authority"] is True
    assert "authority" in report["error"]
    closed = [item for item in facts(conn, deal, "question_answered") if item.get("by") == "reviewer"]
    assert len(closed) == 1
    assert closed[0]["answered_via"] == "redirected"
    assert "answer" not in closed[0]


# --------------------------------------------------- 3. вопрос человеку в пул


def test_lead_asks_the_human_and_the_deal_waits(tmp_path: Path) -> None:
    """`duty_ask_human` — строка пула и `wait_human`; ответ приходит фактом.

    Канал — существующий `ask.ask` (`dashboard/ask.py`), поэтому проверяется то,
    что он обещает: вопрос в пуле с вариантами, дело в ожидании человека, а ответ
    человека лид получает следующим фактом своего же `duty_wait` (пул дочитывает
    он, а не шедулер: его в 2.0 нет).
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    seed_worker_question(conn, deal, 41)

    report = tools.ask_human(
        str(deal["id"]),
        "Публиковать релиз сейчас?",
        ["да", "нет"],
        True,
        "human-1",
        home=home,
        clock=lambda: NOW + 10,
    )

    assert report["ok"] is True
    number = report["question_id"]
    assert number > 0
    assert report["target"] == "human"
    assert report["reason"] == "risky"
    rows = pool_rows(home)
    assert len(rows) == 1
    assert rows[0]["id"] == number
    assert rows[0]["text"] == "Публиковать релиз сейчас?"
    assert json.loads(rows[0]["options_json"]) == ["да", "нет"]
    # Властный вопрос сроком не закрывается: молчание не станет ответом.
    assert rows[0]["critical"] == 1
    assert rows[0]["timeout"] is None
    events = journal.read_events(conn, str(deal["id"]))
    assert deals.deal_row(conn, str(deal["id"]))["status"] == "blocked"
    assert transit.deal_state(spec, events) == transit.WAIT_HUMAN
    asked = [item for item in facts(conn, deal, "question_asked") if str(item.get("question_id")) == str(number)]
    assert len(asked) == 1
    assert asked[0]["routing"]["target"] == "human"
    # Повтор с тем же ключом второго вопроса не задаёт.
    again = tools.ask_human(
        str(deal["id"]),
        "Публиковать релиз сейчас?",
        ["да", "нет"],
        True,
        "human-1",
        home=home,
        clock=lambda: NOW + 20,
    )
    assert again["question_id"] == number
    assert again["repeated"] is True
    assert len(pool_rows(home)) == 1
    # Человек ответил в пуле — лид получает это следующим фактом ожидания.
    answer_human(home, number, "нет")
    waited = tools.wait(str(deal["id"]), 0, 0, home=home, clock=lambda: NOW + 30)
    answered = [item for item in waited["events"] if item["kind"] == "question_answered"]
    assert len(answered) == 1
    assert answered[0]["payload"]["question_id"] == number
    assert waited["answers"] == 1


# ------------------- 4. один вопрос — один ответ: пул и журнал сходятся

def add_pool_question(
    home: Path, deal: dict[str, Any], number: int, text: str = "в каком формате отчёт?", *, session_key: str = ""
) -> None:
    """Строка пула ровно с номером вопроса из журнала — так выглядит живой вопрос.

    `session_key` по умолчанию — ключ этого дела (`t_<deal_id>`); другое значение
    ставит строку чужому делу ровно так, как её завёл бы пул.
    """
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        conn.execute(
            "INSERT INTO questions (id, session_key, text, origin, status, critical, timeout) "
            "VALUES (?, ?, ?, 'duty', 'sent', 0, 24.0)",
            (number, session_key or f"t_{deal['id']}", text),
        )
        conn.commit()
    finally:
        conn.close()


def test_lead_answer_closes_the_pool_question_and_the_button_changes_nothing(tmp_path: Path) -> None:
    """Лид ответил, человек нажал кнопку — один факт, вопрос закрыт в срезе этапа.

    Живой баг дела `d_8ec6a401` (вопрос #555): `question_answered` от лида в
    03:35:16 и от кнопки в 03:35:57, а `duty now` показывал `wait_human` по уже
    отвеченному вопросу. Два правила держат этот случай:

    - `duty_answer` закрывает строку пула (`ask.resolve`) — нажимать нечего; если
      нажатие всё же пришло, `sync_answers` второго факта не пишет: вопрос уже
      закрыт фактом журнала;
    - факт закрытия несёт `routing` вопроса, поэтому срез этапа видит вопрос
      отвеченным, а не ждущим.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    seed_worker_question(conn, deal, 41)
    add_pool_question(home, deal, 41)
    seen: list[Any] = []
    spawn = process_spawner(
        home, spec, seen, Fake(pack=True, result={"status": "done", "summary": "по ответу лида"})
    )

    report = tools.answer(
        str(deal["id"]),
        "impl",
        41,
        "формат JSON",
        "answer-1",
        home=home,
        spawn=spawn,
        clock=lambda: NOW + 10,
        ack_sec=0,
    )

    assert report["answered"] is True
    assert report["pool_resolved"] is True
    rows = pool_rows(home)
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["answer"], rows[0]["answered_via"]) == (
        "answered",
        "формат JSON",
        "lead",
    )

    # Кнопка человека в гонке: он нажал, пока строку пула видел живой.
    answer_human(home, 41, "стоп")
    waited = tools.wait(str(deal["id"]), 0, 0, home=home, clock=lambda: NOW + 30)

    assert waited["answers"] == 0, "второго факта по тому же вопросу нет"
    answered = facts(conn, deal, "question_answered")
    assert len(answered) == 1
    assert answered[0]["by"] == "lead"
    # Срез этапа: пара «вопрос → ответ» сходится по адресу, а не по номеру пула.
    asked, closed = tools._question_pair(journal.read_events(conn, str(deal["id"])), "impl", "41")
    assert asked is not None
    assert closed is True
    events = journal.read_events(conn, str(deal["id"]))
    folded = journal.fold(events, now=NOW + 30)
    assert folded["state"] != "wait_human"
    assert "ответа нет" not in str(folded["evidence"] or "")
    assert transit.deal_state(spec, events) != transit.WAIT_HUMAN


def test_lead_answer_does_not_close_a_foreign_question(tmp_path: Path) -> None:
    """Номер вопроса без своего дела чужой живой вопрос не закрывает.

    `ask.resolve` закрывает строку пула по номеру, а номер сам по себе — не адрес:
    ответ лида этого дела по номеру, который в пуле принадлежит другому делу,
    гасил чужой вопрос тихо и безвозвратно, и человек никогда не видел вопроса, на
    который его просили ответить. Поэтому `session_key` (`t_<deal_id>`) сверяется
    вместе с номером, а факт журнала пишется как обычно: воркеру ответ нужен
    независимо от того, чья строка в пуле.

    Красная проба: без сверки строка чужого дела уходит в `answered` с ответом
    этого дела.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    seed_worker_question(conn, deal, 41)
    add_pool_question(home, deal, 41, session_key="t_d_чужое")
    seen: list[Any] = []
    spawn = process_spawner(
        home, spec, seen, Fake(pack=True, result={"status": "done", "summary": "по ответу лида"})
    )

    report = tools.answer(
        str(deal["id"]),
        "impl",
        41,
        "формат JSON",
        "answer-foreign",
        home=home,
        spawn=spawn,
        clock=lambda: NOW + 10,
        ack_sec=0,
    )

    assert report["answered"] is True
    assert report["pool_resolved"] is False, "чужую строку пула этот ответ не закрывает"
    rows = pool_rows(home)
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["answer"], rows[0]["answered_via"]) == ("sent", None, None)
    answered = facts(conn, deal, "question_answered")
    assert [item["answer"] for item in answered] == ["формат JSON"]


# --------------------------------------------------------- 5. повтор безопасен


def test_repeat_answer_writes_one_fact_and_lifts_one_attempt(tmp_path: Path) -> None:
    """Повтор `duty_answer` с тем же ключом: один факт, одна попытка, прежний ответ.

    Лид, упавший между вызовом и ответом, повторяет вызов — и получает то, что уже
    случилось (`docs/ENGINE-2.0.md`, раздел 4). Второй факт означал бы второе
    решение, вторая попытка — вторую работу поверх той же сессии.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    seed_worker_question(conn, deal, 41)
    process = Fake(pack=True, result={"status": "done", "summary": "продолжил"})
    seen: list[Any] = []
    spawn = process_spawner(home, spec, seen, process)

    first = tools.answer(
        str(deal["id"]), "impl", 41, "вариант Б", "same-key", home=home, spawn=spawn,
        clock=lambda: NOW + 10, ack_sec=0,
    )
    second = tools.answer(
        str(deal["id"]), "impl", 41, "вариант Б", "same-key", home=home, spawn=spawn,
        clock=lambda: NOW + 20, ack_sec=0,
    )

    assert first["attempt"] == 2
    assert second["repeated"] is True
    assert second["attempt"] == 2
    assert second["run_id"] == first["run_id"] == "impl#2"
    # Один спавн, один `attempt_started`, один `question_answered` — повтор не работа.
    assert len(seen) == 1
    assert len(process.calls) == 1
    started = [item for item in facts(conn, deal, "attempt_started") if item.get("idem") == "same-key"]
    assert len(started) == 1
    answered = facts(conn, deal, "question_answered")
    assert len(answered) == 1
    assert answered[0]["answer"] == "вариант Б"


def test_answer_refuses_a_question_that_is_already_closed(tmp_path: Path) -> None:
    """Ответ на уже отвеченный вопрос — отказ, а не вторая попытка.

    Иначе лид, повторяющий вызов с новым ключом, поднимал бы работу по вопросу,
    который давно закрыт, и журнал врал бы про два ответа на один вопрос.
    """
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, SEED)
    conn = open_store(home)
    deal = make_deal(conn, spec)
    seed_worker_question(conn, deal, 41)
    seen: list[Any] = []
    spawn = process_spawner(home, spec, seen, Fake(pack=True, result={"status": "done", "summary": "готово"}))

    tools.answer(
        str(deal["id"]), "impl", 41, "вариант Б", "answer-1", home=home, spawn=spawn,
        clock=lambda: NOW + 10, ack_sec=0,
    )
    with pytest.raises(tools.ToolsError) as caught:
        tools.answer(
            str(deal["id"]), "impl", 41, "вариант В", "answer-2", home=home, spawn=spawn,
            clock=lambda: NOW + 20, ack_sec=0,
        )

    assert "уже отвечен" in str(caught.value)
    assert len(seen) == 1
    assert len(facts(conn, deal, "question_answered")) == 1
