"""Бюджет дела целиком (0.0.5): учёт по журналу, потолок спеки, отказ и видимость.

Тест держит карточку S3, а не внутренности модуля, и потому проверяет три вещи
по отдельности, как они и устроены:

- **учёт** (`dashboard/budget.py`): расход — сумма фактов журнала, а не счётчик в
  памяти. Поэтому события кладутся в настоящий store через `journal.append` и
  читаются назад `journal.read_events` — ровно та форма, которую видит движок
  после рестарта;
- **потолок спеки** (`dashboard/spec.py`): верхний `budget` разбирается как потолок
  дела, сумма этапных больше него — отказ с `file:line`, а спека без него остаётся
  годной (обратная совместимость);
- **видимость** (`dashboard/cli.py`): строка «израсходовано N из M» в `deal`,
  колонка `РАСХОД` в `now` и строка итога в `attempts` — по полям ответа роутов,
  без второго счёта по тем же событиям.

Отказ движка (попытка не поднимается) проверяется там, где он и живёт — в
`tests/test_driver.py`: `test_second_attempt_does_not_start_when_deal_budget_is_spent`
(красная проба из приёмки карточки).
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Any

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str) -> Any:
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Те же имена, что у плагина: одна копия модуля на процесс, иначе `Budget` из
# теста и из движка — разные классы.
journal = load_module("hermes_duty_journal", "journal.py")
spec_module = load_module("hermes_duty_spec", "spec.py")
deals = load_module("hermes_duty_deals", "deals.py")
budget = load_module("hermes_duty_budget", "budget.py")
plugin_api = load_module("hermes_duty_plugin_api", "plugin_api.py")
cli = load_module("hermes_duty_cli", "cli.py")

NOW = 1_800_000_000

# Профили ролей подставляются явно: снимок `~/.hermes/profiles/*` — факт машины, а
# не спеки, и тест не должен зависеть от того, кого на ней завели.
PROFILES = frozenset({"default", "implementer", "tester", "reviewer", "publisher"})


def write_spec(tmp_path: Path, text: str) -> Any:
    """Спека на диске и загруженная: путь абсолютный — так её читает движок."""
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def issues_of(spec: Any) -> list[Any]:
    return [
        issue for issue in spec_module.validate_spec(spec, profiles=PROFILES) if issue.rule == "budget"
    ]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes: свой store заводится на первом обращении (`deals.open_store`)."""
    root = tmp_path / "hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    conn = deals.open_store(root)
    conn.close()
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def open_store(home: Path) -> sqlite3.Connection:
    return deals.open_store(home)


def make_deal(conn: sqlite3.Connection, *, spec_path: Path | None = None) -> dict[str, Any]:
    """Своё дело в store; спека — в `created.payload.spec`, как у `duty create --spec`."""
    created = deals.create_deal(
        conn,
        repo="duty",
        title="Довести дело по спеке",
        body="",
        spec_path=str(spec_path) if spec_path is not None else None,
        now=NOW,
    )
    return deals.deal_row(conn, str(created["deal"]["id"]))


def append(conn: sqlite3.Connection, deal: dict[str, Any], facts: list[tuple[str, dict[str, Any], int]]) -> None:
    """Факты в журнал: `(kind, payload, ts)` — по порядку, как их пишет движок."""
    events = [
        {
            "ts": ts,
            "recorded_at": ts,
            "deal_id": str(deal["id"]),
            "kind": kind,
            "payload": payload,
            "source": "test",
            "idempotency_key": f"{kind}:{index}",
        }
        for index, (kind, payload, ts) in enumerate(facts)
    ]
    journal.append(conn, events)


def attempt(
    run_id: str, stage: str, number: int, *, tokens: int | None, seconds: int = 60
) -> list[tuple[str, dict[str, Any], int]]:
    """Одна закрытая попытка: начало, конец и телеметрия токенов на конце."""
    started = NOW - 3600
    details: dict[str, Any] = {"duration_sec": seconds}
    if tokens is not None:
        details["usage_tokens"] = tokens
    return [
        (
            "attempt_started",
            {"run_id": run_id, "stage": stage, "attempt": number, "pid": 4300, "contract": {"role": stage}},
            started,
        ),
        (
            "attempt_ended",
            {
                "run_id": run_id,
                "stage": stage,
                "attempt": number,
                "outcome": "done",
                "kind": None,
                "duration_sec": seconds,
                "details": details,
            },
            started + seconds,
        ),
    ]


DEAL_SPEC = """
duty: budget-deal
repo: .
budget:
  tokens: 1000
  minutes: 10

stages:
  - id: impl
    role: implementer
    budget:
      tokens: 600
      minutes: 6
  - id: check
    role: tester
    needs: [impl]
    budget:
      tokens: 400
      minutes: 4
"""


# ------------------------------------------------------------------ учёт: расход


def test_spend_sums_tokens_and_minutes_over_all_stages(home: Path) -> None:
    """Расход дела — сумма по всем этапам и попыткам, а не по одному этапу."""
    conn = open_store(home)
    deal = make_deal(conn)
    append(
        conn,
        deal,
        attempt("d:impl#1", "impl", 1, tokens=400, seconds=120)
        + attempt("d:impl#2", "impl", 2, tokens=350, seconds=60)
        + attempt("d:check#1", "check", 1, tokens=100, seconds=30),
    )

    used = budget.spend(journal.read_events(conn, str(deal["id"])))

    assert used.tokens == 850, "токены: 400 + 350 + 100"
    assert used.seconds == 210, "секунды: 120 + 60 + 30"
    assert used.minutes == 3, "минуты — те же секунды, целыми вниз"
    assert used.attempts == 3


def test_spend_ignores_attempt_that_has_not_ended(home: Path) -> None:
    """Незакрытая попытка расхода не тратит: её расход ещё не факт журнала."""
    conn = open_store(home)
    deal = make_deal(conn)
    append(
        conn,
        deal,
        attempt("d:impl#1", "impl", 1, tokens=400, seconds=60)
        + [
            (
                "attempt_started",
                {"run_id": "d:impl#2", "stage": "impl", "attempt": 2, "pid": 4301},
                NOW - 30,
            )
        ],
    )

    used = budget.spend(journal.read_events(conn, str(deal["id"])))

    assert used.tokens == 400
    assert used.attempts == 1


def test_spend_takes_report_usage_when_engine_telemetry_is_absent(home: Path) -> None:
    """`details.usage_tokens` нет — берётся `usage.total_tokens` из отчёта в событии."""
    conn = open_store(home)
    deal = make_deal(conn)
    append(
        conn,
        deal,
        [
            (
                "attempt_started",
                {"run_id": "d:impl#1", "stage": "impl", "attempt": 1, "pid": 4300},
                NOW - 120,
            ),
            (
                "attempt_ended",
                {
                    "run_id": "d:impl#1",
                    "stage": "impl",
                    "attempt": 1,
                    "outcome": "done",
                    "kind": None,
                    "result": {"status": "done", "usage": {"total_tokens": 777}},
                },
                NOW - 60,
            ),
        ],
    )

    used = budget.spend(journal.read_events(conn, str(deal["id"])))

    assert used.tokens == 777
    assert used.seconds == 60, "длительность — по двум фактам журнала"


def test_spend_counts_shell_attempt_minutes_without_tokens(home: Path) -> None:
    """`kind: shell` токенов не тратит, но минуты тратит — и они в сумме дела."""
    conn = open_store(home)
    deal = make_deal(conn)
    append(
        conn,
        deal,
        attempt("d:check#1", "check", 1, tokens=None, seconds=90),
    )

    used = budget.spend(journal.read_events(conn, str(deal["id"])))

    assert used.tokens == 0
    assert used.seconds == 90


# ------------------------------------------------------- потолок: отказ движка


def spent_events(home: Path, tokens: int, seconds: int = 60) -> list[dict[str, Any]]:
    conn = open_store(home)
    deal = make_deal(conn)
    append(conn, deal, attempt("d:impl#1", "impl", 1, tokens=tokens, seconds=seconds))
    return journal.read_events(conn, str(deal["id"]))


def test_refusal_names_tokens_when_deal_budget_is_spent(tmp_path: Path, home: Path) -> None:
    """Потолок исчерпан — причина с числами, класс называет движок (`capability`)."""
    spec = write_spec(tmp_path, DEAL_SPEC)
    events = spent_events(home, tokens=1000)

    refused = budget.refusal(spec, events)

    assert refused is not None
    assert refused.kind == budget.TOKENS
    assert refused.used == 1000 and refused.limit == 1000
    assert refused.reason() == "бюджет дела исчерпан: 1000/1000 токенов"
    assert refused.as_payload() == {"kind": "tokens", "used": 1000, "limit": 1000}


def test_refusal_names_minutes_when_only_time_is_spent(tmp_path: Path, home: Path) -> None:
    """Минуты — второй независимый потолок: он тоже останавливает дело."""
    spec = write_spec(tmp_path, DEAL_SPEC)
    events = spent_events(home, tokens=10, seconds=600)

    refused = budget.refusal(spec, events)

    assert refused is not None
    assert refused.kind == budget.MINUTES
    assert refused.reason() == "бюджет дела исчерпан: 10/10 минут"


def test_refusal_is_none_while_deal_has_room(tmp_path: Path, home: Path) -> None:
    """Есть место — отказа нет: проверка отвечает «можно», а не молчит."""
    spec = write_spec(tmp_path, DEAL_SPEC)

    assert budget.refusal(spec, spent_events(home, tokens=999, seconds=599)) is None


def test_refusal_of_exact_limit_is_refusal(tmp_path: Path, home: Path) -> None:
    """Расход ровно по потолку — уже исчерпан: следующая попытка его пробьёт."""
    spec = write_spec(tmp_path, DEAL_SPEC)

    assert budget.refusal(spec, spent_events(home, tokens=1000, seconds=1)) is not None


def test_spec_without_deal_budget_has_no_ceiling(tmp_path: Path, home: Path) -> None:
    """Спека 0.0.4 без верхнего `budget` работает как раньше — потолка нет вовсе."""
    spec = write_spec(
        tmp_path,
        """
        duty: legacy
        repo: .
        stages:
          - id: impl
            role: implementer
        """,
    )

    assert budget.limit(spec) is None
    assert budget.refusal(spec, spent_events(home, tokens=10_000_000)) is None
    assert budget.describe(spec, []) is None


def test_describe_prints_declared_ceilings_only(tmp_path: Path, home: Path) -> None:
    """Блок для поверхностей: расход, потолки и строка словами — без выдумок."""
    spec = write_spec(
        tmp_path,
        """
        duty: tokens-only
        repo: .
        budget:
          tokens: 2000
        stages:
          - id: impl
            role: implementer
        """,
    )

    block = budget.describe(spec, spent_events(home, tokens=500))

    assert block[budget.TOKENS] == {"spent": 500, "limit": 2000}
    assert block[budget.MINUTES] is None, "минуты не объявлены — потолка нет, а не ноль"
    assert block["text"] == "израсходовано 500 из 2000 токенов"
    assert block["exhausted"] is None


def test_describe_marks_exhausted_ceiling(tmp_path: Path, home: Path) -> None:
    """Исчерпанный потолок назван в том же блоке — по нему консоль пишет пометку."""
    spec = write_spec(tmp_path, DEAL_SPEC)

    block = budget.describe(spec, spent_events(home, tokens=1000))

    assert block["exhausted"] == budget.TOKENS
    assert block["text"] == "израсходовано 1000 из 1000 токенов / 1 из 10 минут"


# --------------------------------------------------------- спека: потолок дела


def test_deal_budget_is_parsed_from_spec_root(tmp_path: Path) -> None:
    """Верхний `budget` — потолок дела, а не этапа: он живёт в корне спеки."""
    spec = write_spec(tmp_path, DEAL_SPEC)

    assert spec.budget is not None
    assert spec.budget.tokens == 1000
    assert spec.budget.minutes == 10
    assert spec.line_of("budget.tokens") == 5, "строка числа — для отказа с file:line"
    assert issues_of(spec) == []


def test_stage_budgets_over_deal_budget_are_an_error(tmp_path: Path) -> None:
    """Этапы просят больше, чем есть у дела — отказ с `file:line` до запуска."""
    spec = write_spec(
        tmp_path,
        """
        duty: too-small
        repo: .
        budget:
          tokens: 500
        stages:
          - id: impl
            role: implementer
            budget:
              tokens: 400
          - id: check
            role: tester
            needs: [impl]
            budget:
              tokens: 400
        """,
    )

    issues = issues_of(spec)

    assert len(issues) == 1, "одно нарушение — один отказ"
    issue = issues[0]
    assert issue.path == spec.path
    assert issue.line == 5, "отказ указывает на число дела, а не на первую строку"
    assert "этапы просят больше, чем есть у дела" in issue.message
    assert "800" in issue.message and "500" in issue.message
    assert str(issue).startswith(f"{spec.path}:5: budget: ")


def test_deal_budget_must_be_positive(tmp_path: Path) -> None:
    """Потолок дела — положительное число; ноль и минус — отказ, а не «безлимит»."""
    spec = write_spec(
        tmp_path,
        """
        duty: zero
        repo: .
        budget:
          tokens: 0
        stages:
          - id: impl
            role: implementer
        """,
    )

    issues = issues_of(spec)

    assert [issue.line for issue in issues] == [5]
    assert "положительное число" in issues[0].message


def test_gate_deadline_does_not_count_against_deal_budget(tmp_path: Path) -> None:
    """Срок гейта — время человека, а не расход дела: сумма его не считает."""
    spec = write_spec(
        tmp_path,
        """
        duty: gate-time
        repo: .
        budget:
          minutes: 20
        stages:
          - id: impl
            role: implementer
            budget:
              minutes: 10
          - id: ask
            kind: gate
            needs: [impl]
            ask:
              q: продолжать?
              options: [да, нет]
            budget:
              minutes: 60
        """,
    )

    assert spec.budget is not None and spec.budget.minutes == 20
    assert issues_of(spec) == [], "гейт не просит бюджета: его минуты — срок ответа"


def test_deal_budget_unknown_key_is_refused(tmp_path: Path) -> None:
    """Опечатка в поле потолка — отказ: молча проигнорированный потолок хуже отказа."""
    spec = write_spec(
        tmp_path,
        """
        duty: typo
        repo: .
        budget:
          token: 500
        stages:
          - id: impl
            role: implementer
        """,
    )

    problems = [str(issue) for issue in spec_module.validate_spec(spec, profiles=PROFILES)]

    assert any("token" in problem for problem in problems), problems


# --------------------------------------------------------------- видимость: CLI


def budget_payload(tmp_path: Path, home: Path) -> dict[str, Any]:
    """Ответ `/now` и `/deal` для дела с потолком — то, что читает консоль."""
    spec = write_spec(tmp_path, DEAL_SPEC)
    conn = open_store(home)
    deal = make_deal(conn, spec_path=spec.path)
    append(
        conn,
        deal,
        attempt("d:impl#1", "impl", 1, tokens=400, seconds=120)
        + attempt("d:impl#2", "impl", 2, tokens=250, seconds=60),
    )
    return {"deal": deal, "id": str(deal["id"])}


def test_deal_route_shows_spend_and_ceiling(tmp_path: Path, home: Path, client: TestClient) -> None:
    """`/deal` показывает расход и потолок рядом с состоянием — по журналу дела."""
    made = budget_payload(tmp_path, home)

    body = client.get(f"/deal/{made['id']}").json()

    assert body["budget"] is not None
    assert body["budget"][budget.TOKENS] == {"spent": 650, "limit": 1000}
    assert body["budget"][budget.MINUTES] == {"spent": 3, "limit": 10}
    assert body["budget"]["text"] == "израсходовано 650 из 1000 токенов / 3 из 10 минут"


def test_attempts_route_carries_total_line(tmp_path: Path, home: Path, client: TestClient) -> None:
    """`/attempts` — расход по попыткам как раньше плюс строка итога по делу."""
    made = budget_payload(tmp_path, home)

    body = client.get(f"/duty/{made['id']}/attempts").json()

    assert body["count"] == 2, "по-попыточный расход не подменён итогом"
    assert body["budget"]["text"].startswith("израсходовано 650 из 1000 токенов")


def test_now_shows_spend_column(tmp_path: Path, home: Path, client: TestClient) -> None:
    """`/now` несёт тот же блок, а консоль печатает его колонкой `РАСХОД`."""
    made = budget_payload(tmp_path, home)

    body = client.get("/now").json()
    row = next(item for item in body["deals"] if item["id"] == made["id"])

    assert row["budget"]["text"] == "израсходовано 650 из 1000 токенов / 3 из 10 минут"
    printed = cli.render_now({"now": body["now"], "deals": [row]})
    assert "РАСХОД" in printed
    assert "650 из 1000 токенов" in printed


def test_deal_render_prints_budget_line(tmp_path: Path, home: Path, client: TestClient) -> None:
    """`bin/duty deal` печатает «бюджет дела: …» рядом с состоянием ожидания."""
    made = budget_payload(tmp_path, home)

    body = client.get(f"/deal/{made['id']}").json()
    printed = cli.render_deal(body, None, None)

    assert "бюджет дела: израсходовано 650 из 1000 токенов / 3 из 10 минут" in printed


def test_deal_render_says_nothing_without_ceiling(home: Path, client: TestClient) -> None:
    """Спека без потолка — строки нет: «0 из 0» выдавало бы отсутствие за бюджет."""
    conn = open_store(home)
    deal = make_deal(conn)
    body = client.get(f"/deal/{deal['id']}").json()

    assert body["budget"] is None
    assert "бюджет дела" not in cli.render_deal(body, None, None)


def test_attempts_render_prints_total_line(tmp_path: Path, home: Path, client: TestClient) -> None:
    """`bin/duty attempts` — строка итога после по-попыточного расхода."""
    made = budget_payload(tmp_path, home)

    body = client.get(f"/duty/{made['id']}/attempts").json()
    printed = cli.render_attempts(body)

    assert printed.rstrip().endswith("бюджет дела: израсходовано 650 из 1000 токенов / 3 из 10 минут")


def test_why_carries_ceiling_numbers(tmp_path: Path, home: Path, client: TestClient) -> None:
    """`why --json` — та же форма, что `/deal`: числа потолка видны в разборе."""
    made = budget_payload(tmp_path, home)

    body = client.get(f"/deal/{made['id']}").json()

    assert json.loads(json.dumps(body["budget"], ensure_ascii=False)) == body["budget"]
    assert body["budget"]["attempts"] == 2


def test_stats_window_in_the_future_is_called_empty_not_zero(home: Path, client: TestClient) -> None:
    """`--since` в будущем — пустое окно словами, а не «0 с задано».

    Числа в таком окне честно пустые (`доля не считается`, `нет данных`), и шапка
    не должна им противоречить бодрым «0 с». Красная проба: вернуть старую строку
    `{span} {source}` без ветки `sec <= 0` — тест падает на «0 с задано».
    """
    # Окно в будущем считается от «сейчас»: приколоченный epoch однажды
    # перестаёт быть будущим и тест падает по календарю, а не по коду.
    body = client.get("/stats", params={"since": int(time.time()) + 3600}).json()

    printed = cli.render_stats(body)

    assert body["window"]["sec"] == 0
    assert "0 с" not in printed, printed
    assert "пустое — граница в будущем" in printed
    assert "доля не считается" in printed
