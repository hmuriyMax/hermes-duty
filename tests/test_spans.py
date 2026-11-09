"""`GET /spans` — границы этапов для нарисованных дорожек таймлайна (0.0.9).

Таймлайн рисует одну дорожку на дело и полосы этапов внутри неё, поэтому
поверхности нужен один запрос на много дел: запрос на дело превратил бы верхушку
страницы в 14 запросов. Здесь закреплено то, на что опирается рисунок: пара
`attempt_started` ↔ `attempt_ended`, честный `end: null` у незакрытой попытки,
пропуск конца без начала, потолок дел и `missing` для дел, которых в store нет.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_plugin_api_spans", MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
plugin_api = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = plugin_api
_SPEC.loader.exec_module(plugin_api)

deals = plugin_api.deals
journal = plugin_api.journal

NOW = 1_793_630_300


def _event(deal_id: str, kind: str, ts: int, payload: dict, key: str) -> dict:
    return {
        "deal_id": deal_id,
        "kind": kind,
        "ts": ts,
        "recorded_at": ts,
        "source": "engine",
        "payload": payload,
        "idempotency_key": key,
    }


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Два дела: у первого этап закрыт и второй идёт, у второго — только «конец»."""
    root = tmp_path / "hermes"
    root.mkdir()
    conn = deals.open_store(root)
    assert conn is not None
    try:
        first = str(deals.create_deal(conn, repo="sx-widgets-api", title="дело с этапами", now=NOW - 900)["deal"]["id"])
        second = str(deals.create_deal(conn, repo="", title="дело без начала", now=NOW - 800)["deal"]["id"])
        journal.append(
            conn,
            [
                _event(
                    first,
                    "attempt_started",
                    NOW - 700,
                    {"run_id": "impl#1", "stage": "impl", "attempt": 1, "contract": {"role": "implementer"}},
                    "spans:first:start:impl",
                ),
                _event(
                    first,
                    "attempt_ended",
                    NOW - 600,
                    {"run_id": "impl#1", "stage": "impl", "attempt": 1, "outcome": "done", "kind": None},
                    "spans:first:end:impl",
                ),
                _event(
                    first,
                    "attempt_started",
                    NOW - 500,
                    {"run_id": "review#1", "stage": "review", "attempt": 1, "contract": {"role": "reviewer"}},
                    "spans:first:start:review",
                ),
                # Конец без начала — мусор: полосы без начала не бывает.
                _event(
                    second,
                    "attempt_ended",
                    NOW - 400,
                    {"run_id": "ghost#1", "stage": "ghost", "attempt": 1, "outcome": "failed"},
                    "spans:second:end:ghost",
                ),
            ],
        )
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(plugin_api, "_now_sec", lambda: NOW)
    return root


@pytest.fixture()
def client(home: Path) -> TestClient:
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def test_spans_pairs_start_and_end_and_keeps_open_attempts_open(home: Path, client: TestClient) -> None:
    conn = deals.open_store(home, create=False, readonly=True)
    assert conn is not None
    try:
        rows = [row["id"] for row in conn.execute("SELECT id FROM deals ORDER BY created_at")]
    finally:
        conn.close()

    body = client.get(f"/spans?ids={','.join(rows)}").json()
    assert body["count"] == 2
    assert body["missing"] == []

    spans = body["spans"][rows[0]]
    # Этап, у которого есть начало и конец: обе границы и исход.
    assert spans[0]["stage"] == "impl"
    assert spans[0]["start"] == NOW - 700
    assert spans[0]["end"] == NOW - 600
    assert spans[0]["outcome"] == "done"
    assert spans[0]["role"] == "implementer"
    # Незакрытая попытка: `end` честно пустой — поверхность тянет её до «сейчас».
    assert spans[1]["stage"] == "review"
    assert spans[1]["end"] is None
    # Конец без начала не превращается в полосу.
    assert body["spans"].get(rows[1], []) == []


def test_spans_names_deals_the_store_does_not_have(home: Path, client: TestClient) -> None:
    body = client.get("/spans?ids=d_00000000").json()
    assert body["spans"] == {}
    assert body["missing"] == ["d_00000000"]


def test_spans_refuses_more_than_the_ceiling(home: Path, client: TestClient) -> None:
    ids = ",".join(f"d_{index:08x}" for index in range(plugin_api.SPAN_MAX_DEALS + 1))
    response = client.get(f"/spans?ids={ids}")
    assert response.status_code == 400
    assert "не больше" in response.json()["detail"]


def test_spans_without_ids_is_empty_not_an_error(home: Path, client: TestClient) -> None:
    body = client.get("/spans").json()
    assert body["count"] == 0
    assert body["spans"] == {}
