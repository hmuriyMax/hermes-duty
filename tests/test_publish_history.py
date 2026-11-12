"""Гарантия публикации — в истории фактов, а не в форме графа (`ENGINE-2.0`, §8).

До этой карточки «публикация не обгонит ревью» держалась на форме графа
(`review-gate`/`publish-gate`), но граф пишет тот же лид, что по нему идёт, и
может его переписать: за историю оба правила не сработали ни разу, а отказы были
только по форме. Поэтому проверка переехала в `duty_start` и читает журнал:

1. `collected` (approve) от этапа с ролью `reviewer` этого дела;
2. коммит, который видело ревью, — база публикуемого дерева или её **предок**, и
   после approve нет `changes_requested` по тому же этапу;
3. рискованный этап не стартует без ответа человека вариантом из `allow` и без
   неизменного с момента вопроса дерева;
4. прежняя сверка `published.sha` с origin (`ls-remote`) остаётся как была.

Два слоя проверок и два уровня тестов:

- `publish.review()` — чистая функция фактов (без git, без БД): здесь проверяются
  сами правила и порядок отказов, словами;
- `tools.start()` — хук в `duty_start`: здесь проверяется, что отказ случается
  **до** спавна и что журнал от него не растёт. Родство коммитов спрашивается у
  настоящего `git` (репозиторий в `tmp_path`), потому что подменённый ответ
  доказал бы только то, что мы сами его и написали.

Каждый тест — красная проба: убери правило из `publish.py` или хук из `tools.py`,
и он падает.
"""

from __future__ import annotations

import importlib.util
import itertools
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Метка времени у всех фактов одна: порядок задаёт `id`, а не `ts`.
NOW = 1_790_000_000

# Ключи фактов уникальны в пределах прогона: иначе `INSERT OR IGNORE` склеил бы
# два разных факта одного вида.
_SEQ = itertools.count()

# Коммиты, которые «видело ревью» и которые «публикуются»: в чистом слое это
# просто строки, в тестах `duty_start` — настоящие sha из репозитория.
REVIEWED = "a" * 40
OTHER = "b" * 40


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути: пакета нет, грузим так же, как это делает движок."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec_module = load_module("hermes_duty_spec", "spec.py")
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
publish = load_module("hermes_duty_publish", "publish.py")
tools = load_module("hermes_duty_tools", "tools.py")


# ------------------------------------------------------ факты чистого слоя


def facts(*items: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """События журнала в том виде, в каком их отдаёт `journal.read_events`."""
    return [
        {"id": index + 1, "kind": kind, "ts": NOW, "payload": payload}
        for index, (kind, payload) in enumerate(items)
    ]


def approve(stage: str = "review", attempt: int = 1) -> tuple[str, dict[str, Any]]:
    """Приговор ревьюера: `collected` с этапом и номером попытки."""
    return (
        journal.VERDICT_APPROVE,
        {"stage": stage, "attempt": attempt, "target": "next", "reason": "дифф принят"},
    )


def changes(stage: str = "review", attempt: int = 2) -> tuple[str, dict[str, Any]]:
    """Возврат на доработку: тот же этап, следующий раунд."""
    return (
        journal.VERDICT_CHANGES,
        {"stage": stage, "attempt": attempt, "target": "impl", "reason": "нет тестов"},
    )


def ended(
    stage: str = "review", attempt: int = 1, commit: Optional[str] = REVIEWED
) -> tuple[str, dict[str, Any]]:
    """Исход попытки ревьюера: коммит, который он просмотрел, живёт здесь."""
    payload: dict[str, Any] = {
        "run_id": f"{stage}#{attempt}",
        "stage": stage,
        "attempt": attempt,
        "outcome": "done",
        "details": {"workdir": "/tmp/duty"},
    }
    if commit is not None:
        payload["commit"] = commit
    return "attempt_ended", payload


def ancestor_of(answer: Optional[bool]) -> Any:
    """Проба родства коммитов: запоминает, о чём её спросили."""

    calls: list[tuple[str, str]] = []

    def probe(older: str, newer: str) -> Optional[bool]:
        calls.append((older, newer))
        return answer

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


# ------------------------------------------------ 1. приговор ревью (факты)


def test_publish_without_an_approve_fact_is_refused():
    """`collected` нет вовсе — публиковать нечего, и причина называет факт."""
    result = publish.review(facts(ended(), ("attempt_started", {"stage": "review"})), ["review"], current=REVIEWED)

    assert result.ok is False
    assert result.commit == ""
    assert "нет факта collected" in result.reason


def test_approve_of_a_stage_without_the_reviewer_role_is_refused():
    """`collected` от реализатора — не приговор: роль этапа берётся у графа."""
    result = publish.review(
        facts(ended(stage="impl"), approve(stage="impl")),
        ["review"],
        current=REVIEWED,
    )

    assert result.ok is False
    assert result.reviewer == "impl"
    assert "роль не reviewer" in result.reason
    assert "review" in result.reason, "отказ называет, у кого роль ревьюера"


def test_approve_of_an_unknown_stage_is_refused():
    """Приговор от этапа, которого в графе нет, роль подтвердить нечем."""
    result = publish.review(facts(ended(stage="ghost"), approve(stage="ghost")), ["review"], current=REVIEWED)

    assert result.ok is False
    assert "роль не reviewer" in result.reason


def test_approve_without_the_reviewed_commit_is_refused():
    """Приговор без `attempt_ended` не называет коммит — сверять не с чем."""
    result = publish.review(facts(approve()), ["review"], current=REVIEWED)

    assert result.ok is False
    assert "не называет коммит" in result.reason


def test_approve_of_the_published_tree_is_ok():
    """Ревью видело ровно то дерево, которое публикуется: git даже не спрашивается."""
    probe = ancestor_of(True)

    result = publish.review(facts(ended(), approve()), ["review"], current=REVIEWED, ancestor=probe)

    assert result.ok is True
    assert (result.reviewer, result.commit) == ("review", REVIEWED)
    assert probe.calls == [], "совпавшие коммиты не требуют `merge-base`"


def test_approve_of_a_short_commit_is_the_same_tree():
    """Сокращённый sha в приговоре — та же ссылка, а не подмена дерева."""
    result = publish.review(
        facts(ended(commit=REVIEWED[:12]), approve()), ["review"], current=REVIEWED, ancestor=None
    )

    assert result.ok is True


def test_approve_of_an_ancestor_commit_is_ok():
    """Ревью случилось раньше последнего коммита, но в той же линии — можно."""
    probe = ancestor_of(True)

    result = publish.review(facts(ended(), approve()), ["review"], current=OTHER, ancestor=probe)

    assert result.ok is True
    assert probe.calls == [(REVIEWED, OTHER)], "спрашивается предок ли просмотренный коммит"


def test_approve_of_a_foreign_commit_is_refused():
    """Ревью видело коммит, которого в истории публикуемой базы нет — отказ."""
    probe = ancestor_of(False)

    result = publish.review(facts(ended(), approve()), ["review"], current=OTHER, ancestor=probe)

    assert result.ok is False
    assert result.commit == REVIEWED
    assert f"ревью видело commit {REVIEWED[:12]}" in result.reason
    assert f"{OTHER[:12]}" in result.reason
    assert "не в её истории" in result.reason


def test_unverifiable_kinship_is_a_refusal():
    """Родство проверить нечем (нет репозитория) — это отказ, а не «совпало»."""
    result = publish.review(facts(ended(), approve()), ["review"], current=OTHER, ancestor=None)

    assert result.ok is False
    assert "проверить нечем" in result.reason


def test_git_silence_is_a_refusal():
    """`git` не ответил (`None`) — публикация не начинается на догадке."""
    result = publish.review(facts(ended(), approve()), ["review"], current=OTHER, ancestor=ancestor_of(None))

    assert result.ok is False
    assert "git не ответил" in result.reason


def test_changes_requested_after_the_approve_is_refused():
    """Ревьюер забрал своё «ок»: действует возврат на доработку, публикации нет."""
    result = publish.review(
        facts(ended(), approve(), changes(), ended(attempt=2)), ["review"], current=REVIEWED
    )

    assert result.ok is False
    assert "вернул работу на доработку" in result.reason
    assert journal.VERDICT_CHANGES in result.reason


def test_changes_requested_before_the_approve_does_not_block():
    """Возврат прошлого раунда снят новым приговором: важно, что после approve."""
    result = publish.review(
        facts(ended(attempt=1), changes(), ended(attempt=2), approve(attempt=2)),
        ["review"],
        current=REVIEWED,
    )

    assert result.ok is True


def test_changes_of_another_stage_do_not_block():
    """Возврат другого этапа — не про это ревью."""
    result = publish.review(
        facts(ended(), approve(), changes(stage="review2")), ["review"], current=REVIEWED
    )

    assert result.ok is True


def test_the_last_approve_by_journal_order_wins():
    """Приговор выбирается по `id`, а не по `ts`: два приговора в одну секунду.

    Красная проба на порядок: последний приговор — про чужой коммит, и именно он
    решает. Сортировка по `ts` сделала бы выбор случайным, и публикация поехала бы
    по одобрению, которое ревьюер уже переписал.
    """
    rows = facts(ended(commit=REVIEWED), approve(attempt=1), ended(attempt=2, commit=OTHER), approve(attempt=2))
    for row in rows:
        row["ts"] = NOW

    probe = ancestor_of(False)
    result = publish.review(rows, ["review"], current=REVIEWED, ancestor=probe)

    assert result.ok is False, "последний приговор — про OTHER, а публикуется REVIEWED"
    assert result.commit == OTHER, "действует приговор с бо́льшим `id`, а не первый"
    assert probe.calls == [(OTHER, REVIEWED)]


def test_approve_without_a_commit_before_it_uses_the_last_attempt():
    """Старый журнал без `attempt` в приговоре: берётся последняя попытка до него."""
    approve_old = (journal.VERDICT_APPROVE, {"stage": "review", "target": "next"})
    result = publish.review(
        facts(ended(attempt=1, commit=OTHER), ended(attempt=2, commit=REVIEWED), approve_old),
        ["review"],
        current=REVIEWED,
    )

    assert result.ok is True
    assert result.commit == REVIEWED


# --------------------------------------- 2. `duty_start`: отказ до спавна


def make_home(tmp_path: Path) -> Path:
    """Корень Hermes: своя `duty/duty.db` и свой пул вопросов."""
    home = tmp_path / "home"
    (home / "messaging").mkdir(parents=True)
    conn = sqlite3.connect(home / "messaging" / "pool.db")
    conn.executescript(
        """
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
    )
    conn.commit()
    conn.close()
    return home


def git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Настоящий репозиторий с двумя коммитами: (репозиторий, первый, второй).

    Родство коммитов — факт git, а не наш ответ: подставленная проба доказала бы
    только то, что тест сам её и написал.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

    def commit(name: str, text: str) -> str:
        (repo / name).write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name],
            check=True,
        )
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        )
        return out.stdout.strip()

    first = commit("a.txt", "ok\n")
    second = commit("b.txt", "ok\n")
    return repo, first, second


def seed(repo: Path, base: str, *stages: str) -> str:
    """Спека-сид: общий верх плюс этапы, записанные блоками YAML."""
    lines = ["duty: гейт публикации", f"repo: {repo}", f"base: {base}", "stages:"]
    for stage in stages:
        lines.append(textwrap.indent(textwrap.dedent(stage).strip("\n"), "  "))
    return "\n".join(lines) + "\n"


IMPL = """
- id: impl
  role: implementer
  workspace: none
  evidence: [{cmd: "true", must: 0}]
"""

REVIEW = """
- id: review
  role: reviewer
  workspace: none
  needs: [impl]
  verdict: {approve: next, changes: impl, max_rounds: 1}
"""

PUBLISH = """
- id: publish
  role: publisher
  workspace: none
  needs: [impl]
"""


def write_spec(tmp_path: Path, text: str, name: str = "duty.yaml") -> Any:
    """Спека на диске: путь абсолютный — так её и читает движок."""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return spec_module.load_spec(path)


def open_store(home: Path) -> sqlite3.Connection:
    conn = deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(conn: sqlite3.Connection, spec: Any) -> dict[str, Any]:
    """Своё дело: строка store плюс путь спеки, записанный в `created`."""
    created = deals.create_deal(
        conn,
        repo="duty",
        title="Гейт публикации",
        body="",
        now=NOW,
        spec_path=str(spec.path),
    )
    deal = deals.deal_row(conn, str(created["deal"]["id"]))
    assert deal is not None
    return deal


def append(conn: sqlite3.Connection, deal: dict[str, Any], *items: tuple[str, dict[str, Any]]) -> None:
    """Факты в журнал дела: ключ события уникален, иначе пачка одного вида слипнется."""
    journal.append(
        conn,
        [
            {
                "ts": NOW + index,
                "recorded_at": NOW + index,
                "deal_id": str(deal["id"]),
                "kind": kind,
                "payload": payload,
                "source": "engine",
                "idempotency_key": f"test:{deal['id']}:{kind}:{next(_SEQ)}",
            }
            for index, (kind, payload) in enumerate(items)
        ],
    )


def row_count(conn: sqlite3.Connection, deal: dict[str, Any]) -> int:
    return len(journal.read_events(conn, str(deal["id"])))


def refusal(call: Any, *args: Any, **kwargs: Any) -> str:
    """Причина отказа инструмента: отказ — исключение, ответ готовит обработчик."""
    with pytest.raises(tools.ToolsError) as caught:
        call(*args, **kwargs)
    return str(caught.value)


def fake_spawn(written: list[tuple[Any, ...]]) -> Any:
    """Спавн попытки без процесса: фейк сам пишет `attempt_started` с ключом вызова."""

    def spawn(home: Path, deal_id: str, stage_id: str, attempt: int, key: str) -> int:
        written.append((deal_id, stage_id, attempt, key))
        conn = open_store(home)
        try:
            deal = deals.deal_row(conn, deal_id)
            assert deal is not None
            append(
                conn,
                deal,
                (
                    "attempt_started",
                    {
                        "run_id": f"{stage_id}#{attempt}",
                        "stage": stage_id,
                        "attempt": attempt,
                        "pid": 4242,
                        "lease_expires": int(time.time()) + 3600,
                        "idem": key,
                    },
                ),
            )
        finally:
            conn.close()
        return 4242

    return spawn


def test_publish_start_without_an_approve_is_refused(tmp_path: Path):
    """Публикация без ревью: отказ, попытка не спавнится, журнал не растёт.

    Красная проба карточки: убери хук из `tools.start` — и этап публикации пошёл
    бы в процесс без единого приговора в журнале.
    """
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, first, IMPL, REVIEW, PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    before = row_count(conn, deal)
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-pub", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "публикация без одобрения ревью" in reason
    assert "collected" in reason
    assert spawned == [], "попытка не поднимается"
    assert row_count(conn, deal) == before, "отказ не пишет фактов"


def test_publish_start_after_an_approve_of_a_foreign_tree_is_refused(tmp_path: Path):
    """Ревью видело коммит, которого нет в истории публикуемой базы, — отказ.

    Живой git: `second` — потомок `first`, а публикуется база `first`; ревью
    смотрело `second`, то есть дерево, которого в публикуемой линии ещё нет.
    """
    repo, first, second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, first, IMPL, REVIEW, PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(conn, deal, ended(stage="review", commit=second), approve(stage="review", attempt=1))
    before = row_count(conn, deal)
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-foreign", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert f"ревью видело commit {second[:12]}" in reason
    assert f"{first[:12]}" in reason
    assert "не в её истории" in reason
    assert spawned == []
    assert row_count(conn, deal) == before


def test_publish_start_after_the_reviewer_took_the_work_back_is_refused(tmp_path: Path):
    """`changes_requested` после approve: прежнее «ок» не действует — отказ."""
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, first, IMPL, REVIEW, PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        ended(stage="review", commit=first),
        approve(stage="review", attempt=1),
        changes(stage="review", attempt=2),
    )
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-back", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "вернул работу на доработку" in reason
    assert spawned == []


def test_publish_start_after_an_approve_of_an_ancestor_commit_starts(tmp_path: Path):
    """Ревью на предке публикуемой базы — законная публикация (живой git).

    Второй коммит появился после ревью, но ревью смотрело дерево, которое в этой
    линии есть: публиковать можно. Проверяется, что предок не отвергается вместе
    с чужим коммитом, — иначе гейт запрещал бы любую работу после ревью.
    """
    repo, first, second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, second, IMPL, REVIEW, PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(conn, deal, ended(stage="review", commit=first), approve(stage="review", attempt=1))
    spawned: list[tuple[Any, ...]] = []

    answer = tools.start(
        str(deal["id"]), "publish", "start-ancestor", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert answer["started"] is True
    assert answer["run_id"] == "publish#1"
    assert spawned == [(str(deal["id"]), "publish", 1, "start-ancestor")]


# --------------------------------------- 3. рискованный этап: нет ответа — нет старта


RISKY_GATE = """
- id: gate
  kind: gate
  workspace: none
  ask: {q: "Публикуем?", options: [публиковать, стоп]}
  allow: [публиковать]
"""

RISKY_PUBLISH = """
- id: publish
  role: publisher
  workspace: none
  needs: [gate]
  ask: {q: "Публикуем diff?", options: [публиковать, стоп], risky: true}
"""


def ask_gate(question_id: int, *, approved: str, stage: str = "gate") -> tuple[str, dict[str, Any]]:
    """Факт вопроса гейта: одобряемый коммит записан в момент вопроса."""
    return (
        "question_asked",
        {
            "question_id": question_id,
            "session_key": "duty",
            "routing": {"stage": stage, "attempt": 1, "approved_commit": approved},
        },
    )


def answer_gate(question_id: int) -> tuple[str, dict[str, Any]]:
    """Факт ответа человека: текст живёт в пуле, здесь — длина и способ."""
    return "question_answered", {"question_id": question_id, "answer_len": 11}


def gate_verdict(
    question_id: int, *, outcome: str = "done", answer: str = "публиковать"
) -> tuple[str, dict[str, Any]]:
    """Приговор гейта: пустой `kind` при `done` — ответ вошёл в `allow`."""
    payload: dict[str, Any] = {
        "run_id": "gate#1",
        "stage": "gate",
        "attempt": 1,
        "outcome": outcome,
        "details": {"stage_kind": "gate", "question_id": question_id, "answer": answer},
    }
    if outcome != "done":
        payload["kind"] = "contract"
        payload["reason"] = f"гейт: ответ «{answer}» вне allow [публиковать]"
    return "attempt_ended", payload


def test_risky_publish_without_a_human_answer_is_refused(tmp_path: Path):
    """Рискованный этап без вопроса человеку: спрашивать некого — нет старта."""
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, first, IMPL, REVIEW, RISKY_GATE, RISKY_PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(conn, deal, ended(stage="review", commit=first), approve(stage="review", attempt=1))
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-risky-quiet", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "гейт gate не спрашивал" in reason
    assert spawned == []


def test_risky_publish_refused_by_the_human_is_refused(tmp_path: Path):
    """Человек ответил «стоп»: ответ вне `allow` — публикации не будет."""
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, first, IMPL, REVIEW, RISKY_GATE, RISKY_PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        ended(stage="review", commit=first),
        approve(stage="review", attempt=1),
        ask_gate(7, approved=first),
        answer_gate(7),
        gate_verdict(7, outcome="failed", answer="стоп"),
    )
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-risky-no", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "человек не дал ок" in reason
    assert "стоп" in reason
    assert spawned == []


def test_risky_publish_waits_for_the_answer(tmp_path: Path):
    """Вопрос задан, ответа нет — «публиковать нечего», а не «по молчанию»."""
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, first, IMPL, REVIEW, RISKY_GATE, RISKY_PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        ended(stage="review", commit=first),
        approve(stage="review", attempt=1),
        ask_gate(9, approved=first),
    )
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-risky-wait", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "вопрос #9" in reason
    assert "без ответа" in reason
    assert spawned == []


def test_risky_publish_after_an_allow_answer_starts(tmp_path: Path):
    """Ревью одобрило предка, человек сказал «публиковать» — попытка поднимается."""
    repo, first, second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, second, IMPL, REVIEW, RISKY_GATE, RISKY_PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        ended(stage="review", commit=first),
        approve(stage="review", attempt=1),
        ask_gate(11, approved=second),
        answer_gate(11),
        gate_verdict(11),
    )
    spawned: list[tuple[Any, ...]] = []

    answer = tools.start(
        str(deal["id"]), "publish", "start-risky-ok", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert answer["started"] is True
    assert answer["run_id"] == "publish#1"
    assert spawned == [(str(deal["id"]), "publish", 1, "start-risky-ok")]


def test_risky_publish_after_the_tree_moved_is_refused(tmp_path: Path):
    """Человек одобрил один коммит, а публикуется другой — отказ (дерево ушло)."""
    repo, first, second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(tmp_path, seed(repo, second, IMPL, REVIEW, RISKY_GATE, RISKY_PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(
        conn,
        deal,
        ended(stage="review", commit=first),
        approve(stage="review", attempt=1),
        ask_gate(12, approved=first),
        answer_gate(12),
        gate_verdict(12),
    )
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-risky-moved", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "человек одобрял другой коммит" in reason
    assert spawned == []


# ------------------------------- 4. статическая топология: предупреждение, не отказ


def test_plan_without_a_reviewer_passes_with_a_warning_and_publish_does_not_start(tmp_path: Path):
    """План без ревьюера проходит `duty_plan` с предупреждением — и публикация не стартует.

    Приёмка карточки: форма графа больше не отказ (`review-gate`/`publish-gate` —
    предупреждения), но и публикация по такому плану не начинается: жёсткая
    гарантия спрашивает факты, а их нет.
    """
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    # Сид без ревьюера: планом публикующий этап не добавляется (`plan.publish`).
    spec = write_spec(tmp_path, seed(repo, first, IMPL, PUBLISH))
    conn = open_store(home)
    deal = make_deal(conn, spec)

    answer = tools.plan(
        str(deal["id"]),
        [{"id": "impl2", "role": "implementer", "needs": ["impl"], "workspace": "none", "evidence": [{"cmd": "true", "must": 0}]}],
        "нужен второй проход",
        "plan-1",
        home=home,
        now=NOW,
    )

    assert answer["accepted"] is True, answer
    assert any("review-gate" in line for line in answer["warnings"]), answer["warnings"]
    assert any("publish-gate" in line for line in answer["warnings"]), answer["warnings"]
    assert answer["stages"] == ["impl2"]

    spawned: list[tuple[Any, ...]] = []
    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-after-plan", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "публикация без одобрения ревью" in reason
    assert spawned == []


# ------------------------------- 5. роль ревьюера и человеческий гейт — из хартии


def seed_with_charter(repo: Path, base: str, charter: str, *stages: str) -> str:
    """Сид с блоком `charter`: он записывается до `stages`, как в доке 2.0."""
    lines = ["duty: хартия публикации", f"repo: {repo}", f"base: {base}", "charter:"]
    for line in textwrap.dedent(charter).strip("\n").splitlines():
        lines.append(f"  {line}")
    lines.append("stages:")
    for stage in stages:
        lines.append(textwrap.indent(textwrap.dedent(stage).strip("\n"), "  "))
    return "\n".join(lines) + "\n"


QA_REVIEW = """
- id: review
  role: qa
  workspace: none
  needs: [impl]
  verdict: {approve: next, changes: impl, max_rounds: 1}
"""


@pytest.mark.parametrize(
    ("approved_by", "started"),
    [("review", True), ("impl", False)],
    ids=["qa approves", "implementer approves"],
)
def test_the_reviewer_role_comes_from_the_charter(tmp_path: Path, approved_by: str, started: bool):
    """Роль ревьюера — из `charter.publish.reviewer_role`, а не константа `reviewer`.

    Дело объявило ревьюером `qa` (`docs/ENGINE-2.0.md`, разделы 3 и 8): приговор
    этапа с этой ролью публикацию одобряет, приговор исполнителя — нет, и отказ
    называет ту роль, которой приговора не хватило.
    """
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(
        tmp_path,
        seed_with_charter(
            repo, first, "publish: {reviewer_role: qa, human_gate: false}", IMPL, QA_REVIEW, PUBLISH
        ),
    )
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(conn, deal, ended(stage=approved_by, commit=first), approve(stage=approved_by, attempt=1))
    spawned: list[tuple[Any, ...]] = []

    if started:
        answer = tools.start(
            str(deal["id"]), "publish", "start-qa", home=home, spawn=fake_spawn(spawned), ack_sec=0
        )

        assert answer["started"] is True
        assert spawned == [(str(deal["id"]), "publish", 1, "start-qa")]
        return
    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-qa", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "роль не qa" in reason
    assert spawned == []


HUMAN_GATE = """
- id: gate
  kind: gate
  workspace: none
  needs: [impl]
  ask: {q: "Публикуем?", options: [публиковать, стоп]}
  allow: [публиковать]
"""

GATED_PUBLISH = """
- id: publish
  role: publisher
  workspace: none
  needs: [review, gate]
"""


@pytest.mark.parametrize(
    ("charter", "answered", "started"),
    [
        ("publish: {human_gate: true}", True, True),
        ("publish: {human_gate: true}", False, False),
        ("publish: {human_gate: false}", False, True),
    ],
    ids=["gate asked and answered", "gate asked but silent", "no human gate"],
)
def test_human_gate_comes_from_the_charter(
    tmp_path: Path, charter: str, answered: bool, started: bool
):
    """`charter.publish.human_gate` решает, спрашивают ли человека перед публикацией.

    Поле было объявлено в хартии и не читалось никем (`docs/ENGINE-2.0.md`, раздел
    3): `false` не менял ничего. Теперь `true` — публикация требует **факта**
    ответа человека на гейт, `false` — не требует; ревью спрашивается в обоих
    случаях.
    """
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(
        tmp_path, seed_with_charter(repo, first, charter, IMPL, REVIEW, HUMAN_GATE, GATED_PUBLISH)
    )
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(conn, deal, ended(stage="review", commit=first), approve(stage="review", attempt=1))
    if answered:
        append(conn, deal, ask_gate(21, approved=first), answer_gate(21), gate_verdict(21))
    spawned: list[tuple[Any, ...]] = []

    if started:
        answer = tools.start(
            str(deal["id"]), "publish", "start-gate", home=home, spawn=fake_spawn(spawned), ack_sec=0
        )

        assert answer["started"] is True
        assert spawned == [(str(deal["id"]), "publish", 1, "start-gate")]
        return
    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-gate", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "гейт gate не спрашивал" in reason
    assert spawned == []


def test_risky_stage_asks_the_human_even_without_the_charter_gate(tmp_path: Path):
    """`ask.risky` сильнее хартии: `human_gate: false` не отменяет вопрос человеку.

    Раздел 8 требует ответа человека на рискованном этапе независимо от того, что
    хартия говорит про публикацию: «нет ответа — нет старта».
    """
    repo, first, _second = git_repo(tmp_path)
    home = make_home(tmp_path)
    spec = write_spec(
        tmp_path,
        seed_with_charter(
            repo, first, "publish: {human_gate: false}", IMPL, REVIEW, RISKY_GATE, RISKY_PUBLISH
        ),
    )
    conn = open_store(home)
    deal = make_deal(conn, spec)
    append(conn, deal, ended(stage="review", commit=first), approve(stage="review", attempt=1))
    spawned: list[tuple[Any, ...]] = []

    reason = refusal(
        tools.start, str(deal["id"]), "publish", "start-risky", home=home, spawn=fake_spawn(spawned), ack_sec=0
    )

    assert "гейт gate не спрашивал" in reason
    assert spawned == []
