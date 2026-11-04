"""Классы отказа и политика ретраев: одно место правил, проверенное по частям.

Проверяется не «функция что-то вернула», а **что именно** она решила:

- класс выводится только из четырёх источников (`result.json`, код выхода,
  наличие файла, `Verdict.kind` гейта) и не путается с соседним классом;
- лимит, пауза и предохранитель на дело — политика (`dashboard/failures.py`), а
  не комментарий: `max: 0` паркует сразу, `on: [contract]` не повторяет `infra`,
  счётчик считается по классу, пауза растёт и упирается в потолок;
- исчерпание паркует дело существующим `blocked` с `block_kind='parked'` плюс
  вопрос человеку, и парковка видна в `/now` причиной и счётчиком;
- ответ на вопрос парковки новую попытку **не** поднимает, а явное решение
  человека (`unblocked`) — поднимает и сбрасывает счётчики класса.

Спека читается настоящим загрузчиком, события пишутся в настоящий журнал,
вопрос — в настоящий пул (`tmp_path`): подставленные «parked» и «вопрос есть»
проверяли бы тест, а не движок.
"""

from __future__ import annotations

import importlib.util
import json
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Метка времени всех фактов теста: одинаковый `ts` ещё и проверяет порядок
# применения событий внутри одной секунды (`KIND_ORDER`).
NOW = 1_790_000_000
DEAL = "d_probe"


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


failures = load_module("hermes_duty_failures", "failures.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
runner = load_module("hermes_duty_runner", "runner.py")
transit = load_module("hermes_duty_transit", "transit.py")
evidence = load_module("hermes_duty_evidence", "evidence.py")
spec_module = load_module("hermes_duty_spec", "spec.py")
cli = load_module("hermes_duty_cli", "cli.py")


# ------------------------------------------------------------------ фикстуры

# Схема пула imessage-gateway — та, в которую пишет `ask()`.
POOL_SCHEMA = """
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  project TEXT,
  text TEXT NOT NULL,
  text_full TEXT,
  origin TEXT,
  options_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  sender_rowid INTEGER,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  sent_at TEXT,
  answered_at TEXT,
  answer TEXT,
  timeout REAL,
  critical INTEGER NOT NULL DEFAULT 0,
  notified_at TEXT,
  reminder_every REAL NOT NULL DEFAULT 4.0,
  kind TEXT NOT NULL DEFAULT 'question',
  link TEXT,
  answered_via TEXT,
  external_notified_at TEXT,
  external_ref TEXT
);
"""


def make_home(tmp_path: Path) -> Path:
    """Корень Hermes: своя `duty/duty.db` и свой пул вопросов."""
    home = tmp_path / "home"
    (home / "messaging").mkdir(parents=True)
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.executescript(POOL_SCHEMA)
    conn.commit()
    conn.close()
    return home


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(conn: sqlite3.Connection) -> dict[str, Any]:
    created = deals.create_deal(
        conn, board="duty", title="Починить шапку", body="", known_boards=["duty"], now=NOW
    )
    return deals.deal_row(conn, str(created["deal"]["id"]))


def write_spec(tmp_path: Path, text: str) -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / "duty.yaml"
    path.write_text(text, encoding="utf-8")
    return spec_module.load_spec(path)


def retry_spec(tmp_path: Path, retry: str = "{max: 1, on: [infra]}") -> Any:
    """Спека одного этапа с объявленной политикой ретраев."""
    return write_spec(
        tmp_path,
        f"""
duty: probe
stages:
  - id: impl
    role: implementer
    workspace: none
    retry: {retry}
""",
    )


def seed(conn: sqlite3.Connection, deal: dict[str, Any], kind: str, payload: dict[str, Any], *, key: str, ts: int = NOW) -> None:
    """Факт в журнал руками: предыстория, которую читает политика."""
    journal.append(
        conn,
        [
            {
                "ts": ts,
                "recorded_at": ts,
                "deal_id": str(deal["id"]),
                "board": str(deal["board"]),
                "kind": kind,
                "payload": payload,
                "source": "test",
                "idempotency_key": key,
            }
        ],
    )


def failed_attempt(conn: sqlite3.Connection, deal: dict[str, Any], attempt: int, kind: str) -> None:
    """Упавшая попытка в журнале: `attempt_started` + `attempt_ended` с классом."""
    seed(conn, deal, "attempt_started", {"stage": "impl", "attempt": attempt, "pid": 100 + attempt, "run_id": f"impl#{attempt}"}, key=f"started:{attempt}")
    seed(
        conn,
        deal,
        "attempt_ended",
        {
            "stage": "impl",
            "attempt": attempt,
            "run_id": f"impl#{attempt}",
            "outcome": "failed",
            "kind": kind,
            "reason": f"попытка {attempt}: отказ {kind}",
        },
        key=f"ended:{attempt}",
    )


def events(conn: sqlite3.Connection, deal: dict[str, Any]) -> list[dict[str, Any]]:
    return journal.read_events(conn, str(deal["id"]))


class Fake:
    """Фейковый спавн: пишет `result.json` (или не пишет) и отдаёт код выхода.

    Ни один тест не поднимает модель: движок проходит те же ветки, что и с живым
    процессом, потому что подменён только сам процесс.
    """

    def __init__(
        self,
        *,
        result: Optional[dict[str, Any]] = None,
        exit_code: int = 0,
        pid: int = 4242,
    ) -> None:
        self.result = result
        self.exit_code = exit_code
        self.pid = pid
        self.calls: list[dict[str, Any]] = []
        self.stopped = 0
        self.stderr_text = "fake stderr tail"

    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "Fake":
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        query = Path(cmd[cmd.index("--query-file") + 1])
        if self.result is None:
            return self
        contract = json.loads(query.read_text(encoding="utf-8"))
        target = Path(str(contract["report_to"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.result, ensure_ascii=False), encoding="utf-8")
        return self

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        return None

    def finished(self) -> bool:
        return True

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.exit_code

    def stop(self, grace: float = 10) -> Optional[int]:
        self.stopped += 1
        return self.exit_code

    @property
    def exit_code_of_process(self) -> Optional[int]:  # pragma: no cover - не используется
        return self.exit_code


def stage_of(spec: Any, stage_id: str = "impl") -> Any:
    stage = spec.stage(stage_id)
    assert stage is not None
    return stage


# ------------------------------------------------------- источники и классы


def test_timeout_is_infra() -> None:
    """Таймаут — наш собственный факт: `infra`, а не «процесс не отчитался»."""
    ended = failures.classify(result=None, exit_code=None, timed_out=True, timeout_sec=60)

    assert (ended.outcome, ended.kind) == (failures.OUTCOME_FAILED, "infra")
    assert "таймаут" in ended.reason
    assert failures.INFRA in failures.CLASSES


def test_exit_code_without_report_is_infra() -> None:
    """Код выхода ≠ 0 без отчёта — упал сам процесс: `infra`."""
    ended = failures.classify(result=None, result_error="result.json нет", exit_code=7)

    assert (ended.outcome, ended.kind) == (failures.OUTCOME_FAILED, "infra")
    assert "код выхода 7" in ended.reason


def test_zero_exit_without_report_is_contract() -> None:
    """Код 0 и нет отчёта — исполнитель не отчитался: `contract`."""
    ended = failures.classify(result=None, result_error="result.json нет: /tmp/x", exit_code=0)

    assert (ended.outcome, ended.kind) == (failures.OUTCOME_FAILED, "contract")
    assert "result.json нет" in ended.reason


def test_refused_status_is_contract() -> None:
    """`status: refused` — отказ исполнителя по контракту, а не сбой процесса."""
    ended = failures.classify(result={"status": "refused", "summary": "не буду"}, exit_code=0)

    assert (ended.outcome, ended.kind) == (failures.OUTCOME_FAILED, "contract")
    assert "refused" in ended.reason


def test_token_budget_is_capability() -> None:
    """Бюджет токенов — стена, а не сбой и не обман: `capability`."""
    ended = failures.classify(result=None, exit_code=None, over_budget=True)

    assert (ended.outcome, ended.kind) == (failures.OUTCOME_FAILED, "capability")


def test_blocked_result_is_not_a_failure() -> None:
    """`status: blocked` — вопрос человеку, а не отказ: класс не выдумывается."""
    ended = failures.classify(result={"status": "blocked", "question": {"text": "какую схему?"}})

    assert ended.outcome == failures.OUTCOME_DONE
    assert ended.kind == ""


@pytest.mark.parametrize(
    "source,expected",
    [
        ({"timed_out": True}, "infra"),
        ({"exit_code": 1}, "infra"),
        ({"exit_code": 0}, "contract"),
        ({"over_budget": True}, "capability"),
        ({"result": {"status": "refused"}}, "contract"),
        ({"result": {"status": "done", "summary": "готово"}}, ""),
    ],
)
def test_classes_do_not_bleed(source: dict[str, Any], expected: str) -> None:
    """Каждый источник даёт ровно свой класс: соседний не подставляется."""
    args: dict[str, Any] = {"result": source.get("result"), "exit_code": source.get("exit_code")}
    args["timed_out"] = bool(source.get("timed_out"))
    args["over_budget"] = bool(source.get("over_budget"))

    ended = failures.classify(**args)

    assert ended.kind == expected
    assert ended.kind in failures.CLASSES or ended.kind == ""


def test_verdict_kind_from_evidence_gate(tmp_path: Path) -> None:
    """Отказ гейта даёт класс из `Verdict.kind` W4: `contract` и `capability`."""
    workdir = tmp_path / "attempt"
    workdir.mkdir()
    stage = spec_module.Stage(id="impl", role="implementer", workspace="none")

    bare = evidence.verify({"status": "done"}, stage, workdir, attempt_started_at=NOW, now=NOW)
    assert bare.ok is False
    assert failures.verdict_kind(bare) == "contract"

    # Пакет в остальном сходится (артефакт на месте, хеш полный) — значит
    # отказ даёт именно бюджет, и класс обязан быть capability.
    note = workdir / "notes.txt"
    note.write_text("готово", encoding="utf-8")
    digest = hashlib.sha256(note.read_bytes()).hexdigest()
    report = {
        "status": "done",
        "summary": "готово",
        "artifacts": [{"path": "notes.txt", "sha256": digest}],
        "evidence": [],
        "usage": {"total_tokens": 900},
    }
    over = evidence.verify(
        report,
        spec_module.Stage(
            id="impl", role="implementer", workspace="none", budget=spec_module.Budget(tokens=100)
        ),
        workdir,
        attempt_started_at=NOW,
        now=NOW,
    )
    assert [issue.rule for issue in over.errors] == ["budget.tokens"]
    assert failures.verdict_kind(over) == "capability"


def test_unknown_verdict_kind_reads_contract() -> None:
    """Незнакомый класс вердикта — `contract`: пакет не сошёлся, это не `infra`."""

    class Odd:
        kind = "mystery"

    assert failures.verdict_kind(Odd) == "contract"
    assert failures.verdict_kind(None) == "contract"


# ------------------------------------------------------------------ политика


def test_policy_default_matches_the_contract(tmp_path: Path) -> None:
    """Этап без `retry` живёт по умолчанию: `{max: 1, on: [infra]}`."""
    spec = write_spec(
        tmp_path,
        """
duty: probe
stages:
  - id: impl
    role: implementer
""",
    )

    policy = failures.policy(stage_of(spec))

    assert (policy.max, policy.on) == (1, ("infra",))
    assert policy.allows("infra") and not policy.allows("contract")


def test_policy_reads_declared_retry(tmp_path: Path) -> None:
    """Объявленная политика читается из спеки как есть: классы и лимит."""
    spec = retry_spec(tmp_path, "{max: 3, on: [infra, contract]}")

    policy = failures.policy(stage_of(spec))

    assert (policy.max, policy.on) == (3, ("infra", "contract"))


def test_max_zero_parks_immediately(tmp_path: Path) -> None:
    """`max: 0` — повторов нет: первый же отказ паркует дело."""
    policy = failures.policy(stage_of(retry_spec(tmp_path, "{max: 0, on: [infra]}")))

    assert failures.decide_park(kind="infra", attempts=1, total=1, policy_=policy) == (
        failures.REASON_RETRIES
    )


def test_on_contract_does_not_retry_infra(tmp_path: Path) -> None:
    """`on: [contract]` не повторяет `infra`: класс назван поимённо, а не «любой»."""
    policy = failures.policy(stage_of(retry_spec(tmp_path, "{max: 5, on: [contract]}")))

    assert failures.decide_park(kind="infra", attempts=1, total=1, policy_=policy) == (
        failures.REASON_RETRIES
    )
    assert failures.decide_park(kind="contract", attempts=1, total=1, policy_=policy) is None


def test_counter_is_per_class_not_mixed(tmp_path: Path) -> None:
    """Счётчик считается по классу: отказы `contract` не съедают лимит `infra`."""
    spec = retry_spec(tmp_path, "{max: 1, on: [infra, contract]}")
    stage = stage_of(spec)
    events_ = [
        {"kind": "attempt_ended", "payload": {"stage": "impl", "outcome": "failed", "kind": "infra"}},
        {"kind": "attempt_ended", "payload": {"stage": "impl", "outcome": "failed", "kind": "contract"}},
        {"kind": "attempt_ended", "payload": {"stage": "impl", "outcome": "done", "kind": None}},
    ]

    counts = failures.failures_of(events_, "impl")

    assert counts == {"infra": 1, "contract": 1}
    policy = failures.policy(stage)
    assert failures.retriable(policy, "infra", counts["infra"]) is True
    assert failures.retriable(policy, "infra", counts["infra"] + 1) is False


def test_backoff_grows_and_caps() -> None:
    """Пауза растёт вдвое от 30 с и упирается в потолок 5 мин."""
    delays = [failures.backoff(number) for number in range(1, 8)]

    assert delays[:4] == [30, 60, 120, 240]
    assert delays[-1] == failures.BACKOFF_CAP_SEC == 300
    assert failures.retry_at(2, NOW) == NOW + 60


def test_too_many_failures_fuse(tmp_path: Path) -> None:
    """Предохранитель на дело сильнее политики этапа: 6 упавших — парковка."""
    policy = failures.policy(stage_of(retry_spec(tmp_path, "{max: 10, on: [infra]}")))

    assert failures.decide_park(kind="infra", attempts=1, total=5, policy_=policy) is None
    assert failures.decide_park(
        kind="infra", attempts=1, total=failures.MAX_ROUNDS_PER_DEAL, policy_=policy
    ) == failures.REASON_TOO_MANY
    assert failures.park_limit(failures.REASON_TOO_MANY, policy) == failures.MAX_ROUNDS_PER_DEAL


# ------------------------------------------------------------------ парковка


def park(conn: sqlite3.Connection, spec: Any, deal: dict[str, Any], home: Path, kind: str = "infra") -> Any:
    """Припарковать дело тем же путём, что и движок: `ask()` плюс `blocked`."""
    return runner.park_exhausted(
        conn,
        spec,
        deal,
        stage_of(spec),
        2,
        kind,
        "процесс упал сам: код выхода 7",
        home=home,
        clock=lambda: NOW,
    )


def pool_rows(home: Path) -> list[sqlite3.Row]:
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    pool.row_factory = sqlite3.Row
    try:
        return list(pool.execute("SELECT * FROM questions"))
    finally:
        pool.close()


def test_park_asks_a_question_and_records_facts(tmp_path: Path) -> None:
    """Исчерпание: вопрос человеку и `blocked` с классом, этапом и счётчиком."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 1, on: [infra]}")  # два отказа — предел
    failed_attempt(conn, deal, 1, "infra")
    failed_attempt(conn, deal, 2, "infra")

    report = park(conn, spec, deal, home)

    assert report is not None
    assert report["reason"] == failures.REASON_RETRIES
    assert (report["attempts"], report["limit"]) == (2, 1)
    rows = pool_rows(home)
    assert len(rows) == 1
    assert rows[0]["origin"] == "duty"
    assert json.loads(rows[0]["options_json"]) == ["continue", "stop"]
    assert "2/1 infra" in rows[0]["text"]
    blocked = [item for item in events(conn, deal) if item["kind"] == "blocked"]
    assert len(blocked) == 1
    payload = blocked[0]["payload"]
    assert payload["block_kind"] == "parked"
    assert (payload["kind"], payload["stage"], payload["attempts"], payload["limit"]) == (
        "infra",
        "impl",
        2,
        1,
    )
    assert payload["park_reason"] == failures.REASON_RETRIES
    assert payload["question_id"] == rows[0]["id"]
    assert deals.deal_row(conn, deal["id"])["status"] == "blocked"


def test_park_is_visible_in_now_as_wait_human_with_reason(tmp_path: Path) -> None:
    """`/now` показывает парковку как `wait_human` с причиной и счётчиком.

    Девятого состояния нет (`docs/DESIGN.md`): парковка ждёт **человека**, и это
    тот же `wait_human`. Различие видно фактами: `block_kind=parked`, класс,
    счётчик и номер вопроса — в `evidence` и в поле `parked`.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 2, on: [infra]}")
    failed_attempt(conn, deal, 1, "infra")
    failed_attempt(conn, deal, 2, "infra")
    failed_attempt(conn, deal, 3, "infra")

    report = park(conn, spec, deal, home)
    folded = journal.fold(events(conn, deal), now=NOW)

    assert report is not None
    assert folded["active"] is True
    assert folded["state"] == "wait_human"
    assert folded["human_kind"] == "question"
    assert folded["parked"]["kind"] == "infra"
    assert (folded["parked"]["attempts"], folded["parked"]["limit"]) == (3, 2)
    assert "3/2 infra" in folded["evidence"]
    assert "block_kind=parked" in folded["evidence"]
    assert folded["parked"]["question_id"] == report["question_id"]


def test_spec_rejects_unknown_failure_class(tmp_path: Path) -> None:
    """Опечатка в `retry.on` — отказ спеки, а не молча выключенный повтор.

    Словарь классов живёт в `dashboard/failures.py`; спека сверяется с ним, поэтому
    `infr` не станет «классом, который никогда не наступит», а `unknown` (имя
    отсутствия класса) нельзя объявить политикой.
    """
    spec = retry_spec(tmp_path, "{max: 2, on: [infr]}")

    issues = spec_module.validate_spec(spec, profiles=frozenset({"implementer"}))

    assert [issue.rule for issue in issues] == ["retry.on"]
    assert "infr" in issues[0].message
    assert "infra, contract, capability" in issues[0].message

    for kind in failures.CLASSES:
        good = retry_spec(tmp_path, f"{{max: 1, on: [{kind}]}}")
        assert spec_module.validate_spec(good, profiles=frozenset({"implementer"})) == []

    unclassified = retry_spec(tmp_path, "{max: 1, on: [unknown]}")
    assert [issue.rule for issue in spec_module.validate_spec(unclassified, profiles=frozenset({"implementer"}))] == [
        "retry.on"
    ]


def test_park_not_made_while_policy_allows_retry(tmp_path: Path) -> None:
    """Пока лимит класса не исчерпан, парковки нет: повтор остаётся движку."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 2, on: [infra]}")
    failed_attempt(conn, deal, 1, "infra")

    assert park(conn, spec, deal, home) is None
    assert pool_rows(home) == []


def test_capability_is_not_retried_by_default(tmp_path: Path) -> None:
    """`capability` по умолчанию не повторяется: стена не рассосётся — парковка."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 3, on: [infra, contract]}")
    failed_attempt(conn, deal, 1, "capability")

    report = park(conn, spec, deal, home, kind="capability")

    assert report is not None and report["reason"] == failures.REASON_RETRIES
    assert report["kind"] == "capability"


def test_too_many_failures_parks_with_its_own_reason(tmp_path: Path) -> None:
    """Шесть упавших попыток по любому классу паркуют дело предохранителем."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 10, on: [infra, contract, capability]}")
    for attempt in range(1, failures.MAX_ROUNDS_PER_DEAL):
        failed_attempt(conn, deal, attempt, "contract")
    failed_attempt(conn, deal, failures.MAX_ROUNDS_PER_DEAL, "infra")

    report = park(conn, spec, deal, home)

    assert report is not None
    assert report["reason"] == failures.REASON_TOO_MANY
    assert (report["attempts"], report["limit"]) == (1, failures.MAX_ROUNDS_PER_DEAL)
    payload = [item for item in events(conn, deal) if item["kind"] == "blocked"][-1]["payload"]
    assert payload["park_reason"] == failures.REASON_TOO_MANY


def test_answer_on_parked_deal_does_not_raise_attempt(tmp_path: Path) -> None:
    """Ответ на вопрос парковки — не решение продолжать: попытка не поднимается."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 1, on: [infra]}")
    failed_attempt(conn, deal, 1, "infra")
    failed_attempt(conn, deal, 2, "infra")
    report = park(conn, spec, deal, home)
    assert report is not None
    seed(
        conn,
        deal,
        "question_answered",
        {
            "question_id": report["question_id"],
            "answer_len": 8,
            "answered_via": "panel",
        },
        key="answered:1",
    )

    decision = transit.next_stage(spec, events(conn, deal), NOW)

    assert (decision.kind, decision.what) == ("wait", transit.WAIT_PARKED)
    states = transit.stage_states(spec, events(conn, deal))
    assert states["impl"].state == "failed"
    assert transit.deal_state(spec, events(conn, deal)) == "wait_human"


def test_late_park_answer_is_visible_but_does_not_resurrect(tmp_path: Path) -> None:
    """Поздний ответ на парковку: факт записан, автоподъёма нет — и это проверяемо."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 0, on: [infra]}")
    failed_attempt(conn, deal, 1, "infra")
    report = park(conn, spec, deal, home)
    assert report is not None
    seed(conn, deal, "question_answered", {"question_id": report["question_id"], "answer_len": 2}, key="answered:late")

    folded = journal.fold(events(conn, deal), now=NOW)

    assert any(item["kind"] == "question_answered" for item in events(conn, deal))
    assert folded["state"] == "wait_human"
    assert transit.next_stage(spec, events(conn, deal), NOW).what == transit.WAIT_PARKED


def test_unblock_after_park_resets_counters_and_runs(tmp_path: Path) -> None:
    """Явное решение человека — единственный подъём: счётчики класса сброшены.

    Двух фактов требует сама природа входа: ответ в пуле закрывает вопрос (но
    попытку не поднимает — это проверено выше), а `unblocked` — решение
    продолжать. Только вместе они дают новую попытку, и она начинается со
    сброшенными счётчиками класса: иначе первый же отказ снова упёрся бы в
    исчерпанный лимит.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 1, on: [infra]}")
    failed_attempt(conn, deal, 1, "infra")
    failed_attempt(conn, deal, 2, "infra")
    report = park(conn, spec, deal, home)
    assert report is not None
    assert transit.next_stage(spec, events(conn, deal), NOW).what == transit.WAIT_PARKED
    # Вопрос в пуле помечен настоящим временем `ask()`: дальнейшие факты ставим
    # после него, иначе порядок применения событий был бы искусственным.
    after = max(int(item["ts"]) for item in events(conn, deal))

    seed(
        conn,
        deal,
        "question_answered",
        {"question_id": report["question_id"], "answer_len": 8, "answered_via": "panel"},
        key="answered:continue",
        ts=after + 1,
    )
    assert transit.next_stage(spec, events(conn, deal), after + 2).what == transit.WAIT_PARKED

    deals.set_unblocked(conn, str(deal["id"]), author="max", source="cli", now=after + 3)
    decision = transit.next_stage(spec, events(conn, deal), after + 4)

    assert decision.kind == "run"
    assert decision.pair == [("impl", 3)]
    states = transit.stage_states(spec, events(conn, deal))
    assert states["impl"].failures == ()
    assert states["impl"].state == "pending"


def test_pause_holds_the_stage_until_retry_at(tmp_path: Path) -> None:
    """Пауза не блокирует движок: этап ждёт `retry_at`, а не спит в `next_stage`.

    Отказ в `NOW` даёт паузу 30 с: до `retry_at` повтор в ready не входит (иначе
    «повторить сейчас же» обошло бы и паузу, и её смысл), а после — входит.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 2, on: [infra]}")
    failed_attempt(conn, deal, 1, "infra")

    waiting = transit.next_stage(spec, events(conn, deal), NOW)

    assert (waiting.kind, waiting.what) == ("wait", transit.WAIT_RETRY)
    assert waiting.retry_at == NOW + failures.BACKOFF_START_SEC
    assert transit.stage_states(spec, events(conn, deal))["impl"].retry_at == NOW + failures.BACKOFF_START_SEC
    assert transit.next_stage(spec, events(conn, deal), NOW + failures.BACKOFF_START_SEC).kind == "run"


def test_engine_parks_after_retries_are_exhausted(tmp_path: Path) -> None:
    """Движок паркует сам: первая инфра-попытка — повтор, вторая — вопрос человеку."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 1, on: [infra]}")

    first = runner.run_stage(
        conn, spec, deal, "impl", 1, home=home, spawn=Fake(exit_code=7), clock=lambda: NOW
    )
    assert (first["outcome"], first["kind"]) == ("failed", "infra")
    assert first["park"] is None  # лимит класса ещё не исчерпан
    assert pool_rows(home) == []

    second = runner.run_stage(
        conn, spec, deal, "impl", 2, home=home, spawn=Fake(exit_code=7), clock=lambda: NOW
    )
    assert second["park"] is not None
    assert second["park"]["reason"] == failures.REASON_RETRIES
    assert len(pool_rows(home)) == 1
    assert deals.deal_row(conn, deal["id"])["status"] == "blocked"
    assert transit.next_stage(spec, events(conn, deal), NOW).what == transit.WAIT_PARKED


def test_why_shows_counter_and_retry_at(tmp_path: Path) -> None:
    """`duty why` показывает класс, счётчик и `retry_at` из политики, а не из текста."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 2, on: [infra]}")
    seed(
        conn,
        deal,
        "created",
        {"title": "дело", "status": "running", "spec": str(spec.path)},
        key="created:spec",
        ts=NOW - 100,
    )
    failed_attempt(conn, deal, 1, "infra")

    report = cli.failure_log(home, str(deal["id"]))

    assert report["found"] is True
    item = report["items"][-1]
    assert (item["stage"], item["attempt"], item["kind"]) == ("impl", 1, "infra")
    assert item["counter"] == "1/2 infra"
    assert item["retry_at"] == item["at"] + failures.BACKOFF_START_SEC
    assert item["park"] is None
    text = cli.render_failure_log(report)
    assert "1/2 infra" in text and "retry_at" in text


def test_why_shows_parked_block_with_reason(tmp_path: Path) -> None:
    """`duty why` называет парковку: счётчик, этап, правило и номер вопроса."""
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    spec = retry_spec(tmp_path, "{max: 1, on: [infra]}")
    failed_attempt(conn, deal, 1, "infra")
    failed_attempt(conn, deal, 2, "infra")
    report = park(conn, spec, deal, home)
    assert report is not None

    log = cli.failure_log(home, str(deal["id"]))
    text = cli.render_failure_log(log)

    assert log["parked"]["reason"] == failures.REASON_RETRIES
    assert "парковка:" in text and "2/1 infra" in text
    assert f"вопрос #{report['question_id']}" in text
