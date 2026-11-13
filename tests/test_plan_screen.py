"""Отказ плана виден на экране дела (P3): причина, круг и «пересборок больше нет».

Карточка держится на живом деле `d_12d5fc44`. В его журнале три отказа плана:
04:09:26 ревизия 1 отклонена шестью строками валидатора (`unknown-key: 'title'`
×4, `plan.evidence.role`, `kind.cmd`), 04:10:52 ревизия 2 — одной
(`plan.evidence: этап 'fix' … evidence пуст`), 04:11:18 ревизия 3 принята, а
позже лид предложил ревизию 4 — и она отклонена шестью строками `plan.id`. На
экране всё это время было «coordinator: running»: без плана, без причины, без
счётчика кругов, хотя в журнале лежали внятные отказы.

Проверяется проекция, а не рендер: `stagefeed.plan` — чистая функция от фактов
журнала, и `/deal` кладёт её рядом с лидом (`deal.plan`), а не внутрь этапов.
Тексты отказов не переучиваются: это те же строки `spec.parse_plan`, что
записал движок, — экран их показывает, а не пересказывает. Строки ниже — дословно
из журнала `d_12d5fc44`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

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


# Те же имена, что у плагина: одна копия модуля на процесс.
journal = load_module("hermes_duty_journal", "journal.py")
deals = load_module("hermes_duty_deals", "deals.py")
stagefeed = load_module("hermes_duty_stagefeed", "stagefeed.py")
plugin_api = load_module("hermes_duty_plugin_api", "plugin_api.py")

NOW = 1_800_000_000
REPO = "duty"

# Граф с этапом плана и хартией: лимит пересборок — факт спеки, а не догадка
# поверхности. `max_rounds_replan: 3` — дефолт движка, как у живого дела.
SPEC = """
duty: отказ плана на экране
charter:
  max_stages: 6
  max_rounds_replan: 3
stages:
  - id: coordinator
    role: lead
    workspace: none
  - id: research
    role: researcher
    needs: [coordinator]
"""

# Спека с исчерпывающимся лимитом: один отказ переживаем, второй — уже `failed`.
SPEC_TIGHT = SPEC.replace("max_rounds_replan: 3", "max_rounds_replan: 1")

# Строки отказов — дословно из журнала d_12d5fc44 (`spec.parse_plan` их и пишет).
TITLE_ISSUE = (
    "unknown-key: неизвестный ключ 'title'; допустимые: id, kind, role, workspace, "
    "needs, parallel_with, evidence, retry, budget, ask, allow, verdict, cmd, must, artifacts"
)
EVIDENCE_ISSUE = (
    "plan.evidence: этап 'fix' что-то производит, а evidence пуст: планом добавляется "
    "только этап, чью работу можно проверить — объяви evidence {cmd, must}"
)
ROLE_ISSUE = (
    "plan.evidence.role: этап 'research': у роли 'researcher' нет терминала — объявленные "
    "evidence (test -s requirements-audit.md) нечем выполнить"
)
SHELL_ISSUE = "kind.cmd: этап 'gates' с kind: shell без cmd: команде этапа нечего запускать"
# Четвёртый отказ живого дела — перестановка, которая переписывает уже собранные
# этапы: шесть строк, по одной на этап (`budget`, `kind`, `role`, `workspace`…).
PLAN_ID_ISSUES = [
    "plan.id: id 'research' уже есть в графе: перестановка называет только needs/parallel_with, а поля budget, kind, role, workspace переписывают этап",
    "plan.id: id 'fix' уже есть в графе: перестановка называет только needs/parallel_with, а поля budget, evidence, kind, role, workspace переписывают этап",
    "plan.id: id 'check-build' уже есть в графе: перестановка называет только needs/parallel_with, а поля budget, cmd, kind, must, role, workspace переписывают этап",
    "plan.id: id 'check-test' уже есть в графе: перестановка называет только needs/parallel_with, а поля budget, cmd, kind, must, role, workspace переписывают этап",
    "plan.id: id 'check-lint' уже есть в графе: перестановка называет только needs/parallel_with, а поля budget, cmd, kind, must, role, workspace переписывают этап",
    "plan.id: id 'review' уже есть в графе: перестановка называет только needs/parallel_with, а поля budget, kind, role, verdict, workspace переписывают этап",
]


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Корень Hermes: store заводится на первом обращении, пул не нужен."""
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


def spec_file(home: Path, text: str = SPEC, name: str = "duty.yaml") -> Path:
    path = home / name
    path.write_text(text, encoding="utf-8")
    return path


def new_deal(client: TestClient, home: Path, *, text: str = SPEC, name: str = "duty.yaml") -> str:
    response = client.post(
        "/deals",
        json={"repo": REPO, "title": "дело с планом", "spec": str(spec_file(home, text, name))},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def append(home: Path, deal_id: str, facts: list[tuple[str, dict[str, Any], int]]) -> None:
    """Факты в журнал: `(kind, payload, ts)` — по порядку, как их пишет движок."""
    conn = deals.open_store(home)
    assert conn is not None
    try:
        journal.append(
            conn,
            [
                {
                    "ts": ts,
                    "recorded_at": ts,
                    "deal_id": deal_id,
                    "kind": kind,
                    "payload": payload,
                    "source": "test",
                    "idempotency_key": f"{deal_id}:{kind}:{index}",
                }
                for index, (kind, payload, ts) in enumerate(facts)
            ],
        )
    finally:
        conn.close()


def events_of(home: Path, deal_id: str) -> list[dict[str, Any]]:
    conn = deals.open_store(home)
    assert conn is not None
    try:
        return journal.read_events(conn, deal_id)
    finally:
        conn.close()


def proposed(rev: int) -> dict[str, Any]:
    """Предложение плана: ревизия и этапы — так его пишет `duty_plan`."""
    return {"rev": rev, "stages": [{"id": "research", "role": "researcher", "needs": ["coordinator"]}]}


def rejected(rev: int, why: str, issues: list[str]) -> dict[str, Any]:
    return {"rev": rev, "why": why, "issues": issues, "source_stage": "coordinator"}


def live_plan_facts() -> list[tuple[str, dict[str, Any], int]]:
    """Журнал `d_12d5fc44` целиком: три отказа, приёмка третьей ревизии и отказ четвёртой.

    Первый и третий отказы — по шесть строк валидатора, второй — одна строка: на
    них видно, что список обрезается числом, а не теряет причины. После приёмки
    третьей ревизии лид предложил четвёртую (`source_stage: lead`), и её отказ так
    и стоит.
    """
    first = [TITLE_ISSUE, TITLE_ISSUE, TITLE_ISSUE, TITLE_ISSUE, ROLE_ISSUE, SHELL_ISSUE]
    return [
        ("plan_proposed", proposed(1), NOW - 700),
        ("plan_rejected", rejected(1, "план ревизии 1 отклонён: отказов 6", first), NOW - 700),
        ("plan_proposed", proposed(2), NOW - 614),
        ("plan_rejected", rejected(2, "план ревизии 2 отклонён: отказов 1", [EVIDENCE_ISSUE]), NOW - 614),
        ("plan_proposed", proposed(3), NOW - 588),
        ("plan_accepted", {"rev": 3, "by": "engine"}, NOW - 588),
        ("plan_proposed", {**proposed(4), "source_stage": "lead"}, NOW - 532),
        ("plan_rejected", {**rejected(4, "план ревизии 4 отклонён: отказов 6", PLAN_ID_ISSUES), "source_stage": ""}, NOW - 532),
    ]


def test_live_journal_names_every_refusal_with_its_round(client: TestClient, home: Path) -> None:
    """Проекция на живом журнале: три отказа, у каждого причины и номер круга.

    Номер круга — порядок предложений, а не `rev`: третья ревизия была принята,
    поэтому четвёртый отказ — это круг 4, и человек читает «отказ четвёртого
    круга», а не сверяет ревизии с журналом.
    """
    deal_id = new_deal(client, home)
    append(home, deal_id, live_plan_facts())

    block = stagefeed.plan(events_of(home, deal_id))

    assert block is not None
    assert (block["proposals"], block["rejections"]) == (4, 3)
    assert [row["round"] for row in block["refusals"]] == [1, 2, 4]
    assert [row["at"] for row in block["refusals"]] == [NOW - 700, NOW - 614, NOW - 532]

    first, second, last = block["refusals"]
    # Шесть строк отказа обрезаны до пяти: остаток назван числом, а не потерян.
    assert len(first["issues"]) == 5
    assert first["more"] == 1
    assert first["issues"][0] == TITLE_ISSUE
    assert first["issues"][4] == ROLE_ISSUE
    assert first["why"] == "план ревизии 1 отклонён: отказов 6"
    assert second["issues"] == [EVIDENCE_ISSUE]
    assert second["more"] == 0
    assert last["issues"] == PLAN_ID_ISSUES[:5]
    assert last["more"] == 1
    # Последний отказ стоит: приёмки после него в журнале не было.
    assert block["refusal"]["round"] == 4
    # Лимит движка не исчерпан: `max_rounds_replan: 3`, а свёртка сосчитала два
    # реплан-факта (четвёртая ревизия назвала `source_stage: lead`, такого этапа
    # в графе нет). Экран повторяет вердикт движка, а не свой счёт.
    assert (block["limit"], block["exhausted"]) == (3, False)

    # Журнал на момент приёмки третьей ревизии (04:11:18): строка снята приёмкой,
    # а счётчики кругов и отказов остались — дело пережило два отказа.
    history = events_of(home, deal_id)
    accepted_at = next(index for index, item in enumerate(history) if item.get("kind") == "plan_accepted")
    early = stagefeed.plan(history[: accepted_at + 1])
    assert early is not None
    assert "refusal" not in early
    assert (early["proposals"], early["rejections"]) == (3, 2)


def test_refusal_stands_on_the_deal_until_the_plan_is_accepted(client: TestClient, home: Path) -> None:
    """`/deal` отдаёт отказ рядом с лидом, а не внутри этапов; без отказа поля нет."""
    deal_id = new_deal(client, home)
    # Журнал каким он был в 04:10:52: ревизия 2 отклонена, дело ждёт пересборки.
    append(home, deal_id, live_plan_facts()[:4])

    body = client.get(f"/deal/{deal_id}").json()

    deal = body["deal"]
    assert "lead" in deal, "блок лида пропал: план обязан стоять рядом с ним"
    plan = deal["plan"]
    assert plan["refusal"]["round"] == 2
    assert plan["refusal"]["issues"] == [EVIDENCE_ISSUE]
    assert (plan["proposals"], plan["rejections"]) == (2, 2)
    assert plan["exhausted"] is False
    # Не внутри этапов: у ленты свой блок, и отказа в нём нет.
    assert all("plan" not in stage for stage in body["stages"])
    assert "plan" not in body

    # Плана не отклоняли ни разу — поля нет вовсе, а не `null`-заглушка.
    clean = new_deal(client, home, name="clean.yaml")
    clean_body = client.get(f"/deal/{clean}").json()
    assert "plan" not in clean_body["deal"]


def test_exhausted_limit_is_the_engines_own_verdict(client: TestClient, home: Path) -> None:
    """Лимит исчерпан — это вердикт движка, а не пересчёт проекции.

    Счётчик `charter.max_rounds_replan` жгут и отказы, и переосмысления
    (`plan_rethink`): с лимитом `1` один отказ плюс одно переосмысление — уже
    «пересборок плана больше нет», и движок помечает этап плана `failed(contract)`
    причиной «репланов N больше max_rounds_replan=M». Экран читает этот факт, а
    не заводит второй счётчик: на живом журнале `d_12d5fc44` четвёртая ревизия
    называла `source_stage: lead`, такого этапа в графе нет, и собственный счёт
    проекции разошёлся бы с движком.
    """
    deal_id = new_deal(client, home, text=SPEC_TIGHT, name="tight.yaml")
    append(
        home,
        deal_id,
        [
            ("plan_proposed", proposed(1), NOW - 700),
            ("plan_rejected", rejected(1, "план ревизии 1 отклонён: отказов 1", [EVIDENCE_ISSUE]), NOW - 700),
            ("plan_rethink", {"failed_stage": "research", "kind": "contract", "attempt": 1, "why": "этап упал контрактом"}, NOW - 400),
        ],
    )

    plan = client.get(f"/deal/{deal_id}").json()["deal"]["plan"]

    assert (plan["rejections"], plan["limit"]) == (1, 1)
    assert plan["exhausted"] is True
    # Строка причины остаётся: «упёрся» — это тот же отказ плюс отсутствие пути.
    assert plan["refusal"]["round"] == 1
