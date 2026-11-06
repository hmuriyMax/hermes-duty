"""CLI `bin/duty`: те же функции, что у роутов, и те же коды возврата.

Дело адресуется своим id `d_<hex>` (0.0.6: доск нет) и группируется по `repo`,
поэтому ни одна команда не берёт сегмент доски: `deal <id>`, `attempts <id>`,
`why <id> [этап]`, `now [--repo R]`, `stats [--repo R]`.

Проверяется то, что нельзя проверить глазами при ревью и что заявлено в карточке
W3: `--json` — ровно форма роута (сверка с ответом `/now`, `/stats`, `/deal`,
`/duty/.../attempts` через `TestClient`), коды 0/1/2 взяты из того, что решил
роут, `now`/`deal`/`stats` печатают группы по репозиториям («без репозитория» —
отдельной группой), а ни одна команда ничего не пишет в чужие БД и не создаёт
журнал.

Фикстуры свои и маленькие (`tmp_path` + `HERMES_HOME`): живые БД не читаются.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = ROOT / "dashboard" / "cli.py"
_SPEC = importlib.util.spec_from_file_location("hermes_duty_cli_tests", CLI_PATH)
assert _SPEC is not None and _SPEC.loader is not None
cli = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cli
_SPEC.loader.exec_module(cli)

NOW = 1_800_000_000

# Группировка дела — `repo`; адрес — свой id `d_<hex>`. Доски в адресе нет.
REPO = "sx-widgets-api"

# Схема пула: `_pool_questions` читает `status`, `session_key`, `text`, `text_full`,
# а `ask.answer_of` — текст ответа (`answer`) у запаркованного дела.
POOL_SCHEMA = """
CREATE TABLE questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT,
    text        TEXT,
    text_full   TEXT,
    origin      TEXT,
    status      TEXT,
    kind        TEXT,
    answer      TEXT
);
"""


def open_store(home: Path) -> sqlite3.Connection:
    """Соединение своего store: схему и журнал доводит открытие на запись."""
    conn = cli.api().deals.open_store(home)
    assert conn is not None
    return conn


def make_deal(
    home: Path,
    *,
    repo: Optional[str] = REPO,
    title: str = "своё дело",
    status: str = "ready",
    created_at: int = NOW - 3600,
) -> str:
    """Своё дело в store (`d_<hex>`): у него есть журнал и переходы."""
    conn = open_store(home)
    try:
        made = cli.api().deals.create_deal(
            conn, repo=repo, title=title, now=created_at
        )
        deal_id = str(made["deal"]["id"])
        if status != str(made["deal"]["status"]):
            with conn:
                conn.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))
    finally:
        conn.close()
    return deal_id


def block_deal(home: Path, deal_id: str, *, kind: str = "needs_input", reason: str = "нужно решение") -> None:
    """Заблокировать дело как движок: `blocked` — факт журнала, не поле строки."""
    conn = open_store(home)
    try:
        cli.api().deals.set_blocked(
            conn, deal_id, kind=kind, reason=reason, author="engine", source="engine", now=NOW - 60
        )
    finally:
        conn.close()


def make_pool(home: Path, rows=()) -> Path:
    path = home / "messaging" / "pool.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(POOL_SCHEMA)
    for row in rows:
        values = {
            "session_key": "t_000001",
            "text": "",
            "text_full": "",
            "origin": "duty",
            "status": "sent",
            "kind": "decision",
            "answer": None,
        }
        values.update(row)
        conn.execute(
            "INSERT INTO questions (session_key, text, text_full, origin, status, kind, answer)"
            " VALUES (?,?,?,?,?,?,?)",
            tuple(values.values()),
        )
    conn.commit()
    conn.close()
    return path


def make_card(home: Path, *, card_id: str = "t_aaa001") -> Path:
    """Карточка kanban — наблюдение: в срез дел и в статистику она не входит.

    Живёт в фикстуре намеренно: если карточка протечёт в `/now`, `/stats` или в
    адресацию, тест это увидит.
    """
    path = home / "kanban" / "boards" / "b1" / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT,"
        " assignee TEXT, status TEXT, priority INTEGER DEFAULT 0, created_at INTEGER,"
        " started_at INTEGER, completed_at INTEGER, worker_pid INTEGER, current_run_id INTEGER,"
        " claim_lock TEXT, claim_expires INTEGER, block_kind TEXT,"
        " consecutive_failures INTEGER DEFAULT 0, workspace_kind TEXT);"
        "CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT, profile TEXT, status TEXT, outcome TEXT, error TEXT, summary TEXT,"
        " started_at INTEGER, ended_at INTEGER, worker_pid INTEGER, last_heartbeat_at INTEGER,"
        " claim_lock TEXT, claim_expires INTEGER);"
        "CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);"
    )
    conn.execute(
        "INSERT OR REPLACE INTO tasks (id, title, body, assignee, status, created_at, block_kind)"
        " VALUES (?, 'карточка', '', 'implementer', 'blocked', ?, 'needs_input')",
        (card_id, NOW - 3600),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Корень Hermes: своё заблокированное дело, живой пул и карточка-наблюдение."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    deal_id = make_deal(tmp_path, title="первое дело")
    block_deal(tmp_path, deal_id)
    make_pool(tmp_path)
    make_card(tmp_path)
    return tmp_path


@pytest.fixture()
def deal(home):
    """id единственного дела фикстуры: адрес — он, а не доска."""
    conn = open_store(home)
    try:
        rows = cli.api().deals.deal_rows(conn, open_only=False)
    finally:
        conn.close()
    assert len(rows) == 1
    return str(rows[0]["id"])


@pytest.fixture()
def client(home):
    """Роуты того же модуля, что грузит CLI: сверять формы можно буквально."""
    app = FastAPI()
    app.include_router(cli.api().router)
    return TestClient(app)


@pytest.fixture()
def frozen_now(monkeypatch):
    """Один и тот же `now` у CLI и у роута: иначе `now` в ответах не сравнить."""
    monkeypatch.setattr(cli.api(), "_now_sec", lambda: NOW)
    return NOW


def run(argv, capsys):
    """Запустить команду как из терминала: `(код, stdout, stderr)`."""
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ------------------------------------------------------------------ одна форма


def test_now_json_is_exactly_the_route_payload(home, deal, client, frozen_now, capsys):
    code, out, _ = run(["now", "--json"], capsys)
    from_cli = json.loads(out)
    from_route = client.get("/now").json()

    assert code == 0
    assert from_cli == from_route
    assert from_cli["count"] == 1
    assert from_cli["deals"][0]["id"] == deal
    # Группировка — по репозиторию; карточка kanban в срез не входит.
    assert from_cli["repos"]["list"] == [{"repo": REPO, "deals": 1}]


def test_now_json_honours_repo_and_state_filters(home, deal, client, frozen_now, capsys):
    code, out, _ = run(["now", "--repo", REPO, "--state", "wait_human", "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/now", params={"repo": REPO, "state": "wait_human"}).json()


def test_stats_json_is_exactly_the_route_payload(home, deal, client, frozen_now, capsys):
    code, out, _ = run(["stats", "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/stats").json()


def test_stats_json_passes_repo_and_since_to_the_same_function(home, deal, client, frozen_now, capsys):
    since = NOW - 7200
    code, out, _ = run(["stats", "--repo", REPO, "--since", str(since), "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/stats", params={"repo": REPO, "since": since}).json()


def test_deal_json_is_exactly_the_route_payload(home, deal, client, frozen_now, capsys):
    code, out, _ = run(["deal", deal, "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get(f"/deal/{deal}").json()


def test_attempts_json_is_exactly_the_route_payload(home, client, frozen_now, capsys):
    deal_id = make_deal(home, title="дело с попытками")
    write_three_attempts(home, deal_id)
    code, out, _ = run(["attempts", deal_id, "--json"], capsys)
    assert code == 0
    assert json.loads(out) == client.get(f"/duty/{deal_id}/attempts").json()


# Ключи, которые `why` добавляет к форме роута: факты журнала, которых в роуте
# нет. Их ровно два, и набор закрыт — иначе «--json = форма роута» перестанет быть
# проверяемым утверждением.
WHY_EXTRA_KEYS = ("verdict", "failures")


def test_why_json_is_the_deal_route_payload(home, deal, client, frozen_now, capsys):
    code, out, _ = run(["why", deal, "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    route = client.get(f"/deal/{deal}").json()
    assert {key: value for key, value in payload.items() if key not in WHY_EXTRA_KEYS} == route
    assert payload["failures"]["found"] is False
    assert payload["failures"]["note"].startswith("спеки в журнале нет")


def test_json_flag_works_before_the_command_too(home, deal, client, frozen_now, capsys):
    code, out, _ = run(["--json", "now"], capsys)
    assert code == 0
    assert json.loads(out) == client.get("/now").json()


def test_deal_is_addressed_by_id_only(home, deal, client, frozen_now, capsys):
    """Красная проба B1b: вернуть `board` в адресацию — и этот тест падает.

    Дело адресуется своим id: `/deal/{deal_id}` и `/duty/{deal_id}/attempts`
    отвечают, а сегмента доски в адресе нет ни у одной ручки. Группировка
    называется `repo`; старый `?board=` — отказ с подсказкой, а не синоним, и
    CLI не принимает ни сегмент доски, ни флаг `--board`.
    """
    # 1) адрес — id, и только он
    assert client.get(f"/deal/{deal}").status_code == 200
    assert client.get(f"/duty/{deal}/attempts").status_code == 200

    # 2) сегмент доски в адресе — не ручка (404), а не второй способ адресовать:
    #    алиас вернул бы 200 и две правды об одном деле.
    assert client.get(f"/deal/{REPO}/{deal}").status_code == 404
    assert client.get(f"/duty/{REPO}/{deal}/attempts").status_code == 404

    # 3) `?board=` — отказ с подсказкой, а не молчаливый синоним `?repo=`
    for path in ("/now", "/stats"):
        refused = client.get(path, params={"board": REPO})
        assert refused.status_code == 400, path
        assert "repo" in refused.json()["detail"], path
        assert refused.json() != client.get(path).json(), path

    # 4) CLI: лишнее слово (доска) перед id — ошибка разбора, а не второй адрес
    with pytest.raises(SystemExit) as extra:
        run(["deal", REPO, deal], capsys)
    assert extra.value.code == cli.EXIT_SOURCE

    # 5) `--board` не принимается ни одной командой, `--repo` — принимается
    with pytest.raises(SystemExit) as flag:
        run(["now", "--board", REPO], capsys)
    assert flag.value.code == cli.EXIT_SOURCE
    assert run(["now", "--repo", REPO, "--json"], capsys)[0] == cli.EXIT_OK


# ------------------------------------------------------------------- коды 0/1/2


def test_unknown_repo_is_an_empty_slice_not_an_error(home, deal, capsys):
    """Неизвестный `--repo` — фильтр: пустой ответ и код 0, а не «доски нет»."""
    code, out, err = run(["now", "--repo", "nope"], capsys)
    assert code == cli.EXIT_OK
    assert err == ""
    assert "дел нет" in out
    stats, _, stats_err = run(["stats", "--repo", "nope"], capsys)
    assert stats == cli.EXIT_OK
    assert stats_err == ""


def test_missing_deal_is_code_1(home, deal, capsys):
    # `d_ffffffff` — форма своего id, которого нет в store: 404 → код 1.
    for argv in (["deal", "d_ffffffff"], ["attempts", "d_ffffffff"], ["why", "d_ffffffff"]):
        code, _, err = run(argv, capsys)
        assert code == cli.EXIT_NOT_FOUND
        assert "not found" in err


def test_unknown_state_is_code_2(home, capsys):
    code, _, err = run(["now", "--state", "bogus"], capsys)
    assert code == cli.EXIT_SOURCE
    assert "unknown state" in err


def test_unreadable_store_is_code_2(home, deal, capsys):
    (home / "duty" / "duty.db").write_bytes(b"not a database")
    code, out, _ = run(["now", "--json"], capsys)
    payload = json.loads(out)

    # Ответ приходит (роут отвечает 200), но источник не прочитан — код 2.
    assert code == cli.EXIT_SOURCE
    assert any(error["source"] == "duty/duty.db" for error in payload["errors"])
    assert payload["count"] == 0


def test_unreadable_store_on_stats_is_code_2(home, deal, capsys):
    (home / "duty" / "duty.db").write_bytes(b"not a database")
    code, _, _ = run(["stats", "--json"], capsys)
    assert code == cli.EXIT_SOURCE


def test_degradation_is_named_in_text_output_not_only_in_json(home, deal, capsys):
    (home / "messaging" / "pool.db").unlink()
    code, out, _ = run(["now"], capsys)

    assert code == cli.EXIT_SOURCE
    assert "! источник messaging/pool.db" in out
    assert deal in out  # дела читаемого store напечатаны, ответ не потерян


def test_clean_home_without_pool_reports_source_error(home, deal, capsys):
    (home / "messaging" / "pool.db").unlink()
    code, out, _ = run(["now", "--json"], capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_SOURCE
    assert any("pool.db" in error["source"] for error in payload["errors"])


def test_since_accepts_unix_and_iso_date():
    assert cli.since_arg("1800000000") == 1800000000
    assert cli.since_arg("2027-01-15") == int(time.mktime((2027, 1, 15, 0, 0, 0, 0, 0, -1)))
    assert cli.since_arg("2027-01-15 10:30") == int(time.mktime((2027, 1, 15, 10, 30, 0, 0, 0, -1)))


def test_bad_since_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as raised:
        run(["stats", "--since", "вчера"], capsys)
    assert raised.value.code == cli.EXIT_SOURCE


# ------------------------------------------------------------------ текст команд


def test_now_prints_the_columns_and_the_repo_group(home, deal, frozen_now, capsys):
    code, out, _ = run(["now"], capsys)
    assert code == 0
    for header in ("РЕПОЗИТОРИЙ", "ID", "ОЖИДАНИЕ", "ВОЗРАСТ", "ДОКАЗАТЕЛЬСТВО"):
        assert header in out
    assert f"{REPO} · дел: 1" in out  # группировка по репозиторию, а не по доске
    assert deal in out
    assert "t_aaa001" not in out, "карточка kanban — наблюдение, в срез дел не входит"
    assert "wait_human" in out
    assert "block_kind=needs_input" in out  # доказательство — текст бэкенда, не пересказ CLI


def test_now_groups_deals_without_repo_as_its_own_group(home, deal, frozen_now, capsys):
    """«Без репозитория» — отдельная группа после названных, а не пропущенная строка."""
    bare = make_deal(home, repo=None, title="дело без репозитория")

    code, out, _ = run(["now"], capsys)

    assert code == 0
    lines = out.splitlines()
    named_at = next(i for i, line in enumerate(lines) if line.startswith(f"{REPO} · дел:"))
    bare_at = next(i for i, line in enumerate(lines) if line.startswith("без репозитория · дел:"))
    assert named_at < bare_at
    assert "без репозитория · дел: 1" in out
    assert bare in out


def test_now_repo_filter_shows_one_group(home, deal, frozen_now, capsys):
    make_deal(home, repo=None, title="дело без репозитория")

    code, out, _ = run(["now", "--repo", ""], capsys)

    assert code == 0
    headers = [line for line in out.splitlines() if " · дел: " in line and not line.startswith("ДЕЛА СЕЙЧАС")]
    assert headers == ["без репозитория · дел: 1"]  # ровно одна группа — выбранная


def test_deal_prints_facts_and_attempt_timeline(home, frozen_now, capsys):
    deal_id = make_deal(home, title="дело с попытками")
    write_three_attempts(home, deal_id)

    code, out, _ = run(["deal", deal_id], capsys)

    assert code == 0
    assert f"ДЕЛО {deal_id} · {REPO}" in out  # группировка дела названа в шапке
    assert "попытка:" in out
    assert "лизинг:" in out
    assert "ПОПЫТКИ" in out
    # Попытка — факт журнала: этап и номер, исход (длительность), причина.
    assert "impl#1 · implementer · done (1 мин 40 с)" in out
    assert "review#1 · reviewer · failed(contract)" in out
    assert "причина: result.json нет" in out


def test_attempts_prints_only_the_timeline(home, frozen_now, capsys):
    deal_id = make_deal(home, title="дело с попытками")
    write_three_attempts(home, deal_id)

    code, out, _ = run(["attempts", deal_id], capsys)

    assert code == 0
    assert out.startswith("ПОПЫТКИ")
    assert "лизинг:" not in out
# ------------------------------- попытки своего дела в консоли (W18)
#
# Таймлайн своего дела строится по журналу: консоль печатает этап и номер,
# исход с длительностью и причину отказа, а `base`/`commit` — коротким sha.

BASE_ONE = "8d31edee15f8b15136b4ff1def2a49709621975d"
COMMIT_ONE = "8a2d5cb6bb4a380d53fdcf77af758f0b6e71a598"


def write_three_attempts(home: Path, deal_id: str) -> None:
    """Журнал своего дела с тремя попытками — той же формой, что пишет движок."""
    conn = cli.api().deals.open_store(home)
    assert conn is not None
    try:
        cli._load_journal().append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "repo": REPO,
                    "kind": kind,
                    "ts": ts,
                    "recorded_at": ts,
                    "source": "engine",
                    "payload": payload,
                    "idempotency_key": f"test:{deal_id}:{kind}:{ts}:{index}",
                }
                for index, (kind, ts, payload) in enumerate(
                    [
                        ("attempt_started", NOW - 600, {
                            "run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1,
                            "pid": 111, "base": BASE_ONE, "lease_expires": NOW - 400,
                            "contract": {"role": "implementer"},
                        }),
                        ("attempt_progress", NOW - 590, {
                            "stage": "impl", "attempt": 1, "seq": 1, "phase": "tool_use",
                        }),
                        ("attempt_progress", NOW - 580, {
                            "stage": "impl", "attempt": 1, "seq": 2, "phase": "tool_result",
                        }),
                        ("attempt_ended", NOW - 500, {
                            "run_id": f"{deal_id}:impl#1", "stage": "impl", "attempt": 1,
                            "outcome": "done", "kind": None, "reason": "",
                            "base": BASE_ONE, "commit": COMMIT_ONE,
                        }),
                        ("attempt_started", NOW - 400, {
                            "run_id": f"{deal_id}:review#1", "stage": "review", "attempt": 1,
                            "pid": 333, "base": COMMIT_ONE, "lease_expires": NOW - 200,
                            "contract": {"role": "reviewer"},
                        }),
                        ("attempt_ended", NOW - 300, {
                            "run_id": f"{deal_id}:review#1", "stage": "review", "attempt": 1,
                            "outcome": "failed", "kind": "contract", "reason": "result.json нет",
                            "base": COMMIT_ONE, "commit": None,
                        }),
                    ]
                )
            ],
        )
    finally:
        conn.close()


def test_attempts_json_of_own_deal_is_exactly_the_route_payload(home, client, frozen_now, capsys):
    """`--json` своего дела — дословно ответ роута, как и у kanban-дела."""
    deal_id = own_deal(home)
    write_three_attempts(home, deal_id)

    code, out, _ = run(["attempts", deal_id, "--json"], capsys)

    assert code == 0
    assert json.loads(out) == client.get(f"/duty/{deal_id}/attempts").json()


def test_attempts_of_own_deal_prints_stage_outcome_reason_and_short_sha(home, frozen_now, capsys):
    """Строка попытки: этап и номер, исход (длительность), причина плюс `base`/`commit`."""
    deal_id = own_deal(home)
    write_three_attempts(home, deal_id)

    code, out, _ = run(["attempts", deal_id], capsys)

    assert code == 0
    assert out.startswith("ПОПЫТКИ")
    assert "impl#1 · implementer · done (1 мин 40 с)" in out
    assert "review#1 · reviewer · failed(contract)" in out
    assert "причина: result.json нет" in out
    assert "base 8d31ede" in out
    assert "commit 8a2d5cb" in out
    assert "событий прогресса: 2" in out


def test_attempts_of_own_deal_without_attempts_prints_the_note(home, frozen_now, capsys):
    """Пустой таймлайн своего дела называет настоящую причину, а не «спавна нет»."""
    deal_id = own_deal(home)

    code, out, _ = run(["attempts", deal_id], capsys)

    assert code == 0
    assert "попыток не было" in out
    assert "0.0.3" not in out


def test_published_fact_is_shown_by_deal_now_and_attempts(home, frozen_now, capsys):
    """Публикация видна там же, где вердикт: строкой в `deal`, колонкой в `now`.

    Приёмка: «`bin/duty deal` показывает «опубликовано: sha, MR» строкой, `now` —
    то же; в `attempts` публикация видна как попытка с вердиктом». Текст собирается
    из факта журнала (`published`), поэтому тест пишет ровно то событие, которое
    пишет движок после `git ls-remote`.
    """
    deal_id = own_deal(home)
    write_three_attempts(home, deal_id)
    journal_module = cli._load_journal()
    conn = cli.api().deals.open_store(home)
    assert conn is not None
    try:
        journal_module.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "repo": REPO,
                    "kind": "published",
                    "ts": NOW - 100,
                    "recorded_at": NOW - 100,
                    "source": "engine",
                    "payload": {
                        "stage": "publish",
                        "attempt": 1,
                        "sha": COMMIT_ONE,
                        "branch": "release/v0.0.5",
                        "mr_url": "https://gitlab.example/mr/7",
                        "ref": "refs/heads/release/v0.0.5",
                        "verified_by": "git ls-remote",
                    },
                    "idempotency_key": f"test:{deal_id}:published",
                }
            ],
        )
    finally:
        conn.close()

    code, out, _ = run(["deal", deal_id], capsys)

    assert code == cli.EXIT_OK
    line = next(item for item in out.splitlines() if item.startswith("опубликовано:"))
    assert "8a2d5cb" in line
    assert "release/v0.0.5" in line
    assert "https://gitlab.example/mr/7" in line

    row = next(item for item in cli.api().now()["deals"] if item["id"] == deal_id)
    assert row["published"]["sha"] == COMMIT_ONE
    assert row["published"]["verified_by"] == "git ls-remote"

    code, now_out, _ = run(["now"], capsys)
    assert code == cli.EXIT_OK
    # Таблица режет ячейки по ширине, поэтому здесь проверяется колонка, а не
    # полная ссылка: полный текст — в `deal` выше и в `--json` роута.
    assert "ПУБЛИКАЦИЯ" in now_out
    assert "8a2d5cb" in now_out


def test_now_prints_a_dash_when_nothing_was_published(home, frozen_now, capsys):
    """Не публиковали — прочерк: «0» или пустая строка читались бы как факт."""
    deal_id = own_deal(home)
    write_three_attempts(home, deal_id)

    code, out, _ = run(["deal", deal_id], capsys)

    assert code == cli.EXIT_OK
    assert "опубликовано:" not in out
    row = next(item for item in cli.api().now()["deals"] if item["id"] == deal_id)
    assert row["published"] is None


# -------------------- факты попытки своего дела в консоли (FIX-4 / B2)
#
# `bin/duty deal` и строка «Сейчас» — одна карточка: попытка и лизинг читаются из
# журнала и store. До FIX-4 консоль печатала «попытка: нет» прямо перед
# таймлайном этой же попытки.

def start_live_attempt(
    home: Path, deal_id: str, *, pid: int, started_at: int, expires_at: int
) -> None:
    """Живая попытка своего дела: `attempt_started` плюс строка лизинга в store."""
    api = cli.api()
    journal = cli._load_journal()
    conn = api.deals.open_store(home)
    assert conn is not None
    try:
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "repo": REPO,
                    "kind": "attempt_started",
                    "ts": started_at,
                    "recorded_at": started_at,
                    "source": "engine",
                    "payload": {
                        "run_id": f"{deal_id}:impl#1",
                        "stage": "impl",
                        "attempt": 1,
                        "pid": pid,
                        "lease_expires": expires_at,
                        "contract": {"role": "implementer"},
                    },
                    "idempotency_key": f"test:{deal_id}:impl#1",
                }
            ],
        )
        conn.execute(
            "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) VALUES (?,?,?,?,?,?)",
            (deal_id, "impl", 1, pid, started_at, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_deal_of_own_deal_with_live_attempt_does_not_say_attempt_absent(home, frozen_now, capsys):
    """B2: консоль печатает ту же живую попытку, что и «Сейчас», а не «попытки нет»."""
    deal_id = own_deal(home)
    start_live_attempt(home, deal_id, pid=os.getpid(), started_at=NOW - 60, expires_at=NOW + 600)

    code, out, _ = run(["deal", deal_id], capsys)

    assert code == cli.EXIT_OK
    assert "попытка: нет" not in out
    assert "pid жив" in out
    assert "факты из journal" in out
    # Чего в карточке нет, то и не подставляется: ни `—` вместо статуса рана,
    # ни возраста heartbeat, которого журнал не пишет, ни `(—)` вместо claim_lock.
    assert "heartbeat не записан" in out
    assert "heartbeat — назад" not in out
    assert "(—)" not in out
    assert "лизинг: live, истекает через 10 мин" in out

    # Вторая поверхность того же факта: строка «Сейчас» не противоречит карточке.
    row = next(item for item in cli.api().now()["deals"] if item["id"] == deal_id)
    assert row["attempt"]["pid"] == os.getpid()
    assert row["attempt"]["pid_alive"] is True
    assert row["lease"]["state"] == "live"


def test_terminal_deal_has_no_invented_waiting_state(home, frozen_now, capsys):
    """Закрытое дело: состояние ожидания не выдумывается, назван факт закрытия."""
    deal_id = own_deal(home, title="закрытое дело")
    conn = cli.api().deals.open_store(home)
    assert conn is not None
    try:
        cli.api().deals.close_deal(conn, deal_id, reason="не нужно", now=NOW - 30)
    finally:
        conn.close()

    code, out, _ = run(["deal", deal_id], capsys)
    assert code == 0
    assert "закрыто —" in out and "status=done" in out

    code, out, _ = run(["why", deal_id], capsys)
    assert code == 0
    # У закрытого дела состояния ожидания нет: `why` называет записанный факт
    # закрытия, а не выдуманное ожидание и не пустоту.
    assert "состояние ожидания: закрыто — completed" in out
    assert "cause=closed_by_human" in out


def test_why_prints_backend_evidence_verbatim(home, deal, frozen_now, capsys):
    code, out, _ = run(["why", deal], capsys)
    evidence = cli.api().deal(deal)["deal"]["waiting"]["evidence"]

    assert code == 0
    assert evidence in out  # доказательство — слова бэкенда, CLI его не переписывает
    assert "факты, из которых оно собрано:" in out


def seed_attempt(home: Path, *, deal_id: str, stage: str = "impl", attempt: int = 1) -> None:
    """Факты попытки в журнал плагина: `why <этап>` читает именно их."""
    journal = cli._load_journal()
    conn = journal.open_journal(home)
    assert conn is not None
    base = {
        "deal_id": deal_id,
        "repo": REPO,
        "source": "engine",
    }
    event = {
        **base,
        "ts": NOW - 600,
        "recorded_at": NOW - 600,
        "kind": "attempt_started",
        "payload": {"stage": stage, "attempt": attempt, "pid": 999, "run_id": f"{stage}#{attempt}"},
        "idempotency_key": f"test:{deal_id}:{stage}:{attempt}:started",
    }
    journal.append(conn, [event])
    ended = {
        **base,
        "ts": NOW - 540,
        "recorded_at": NOW - 540,
        "kind": "attempt_ended",
        "payload": {
            "stage": stage,
            "attempt": attempt,
            "run_id": f"{stage}#{attempt}",
            "outcome": "failed",
            "kind": "contract",
            "reason": "result.json:evidence[0].exit: evidence.exit: exit=1, а объявлен must=0",
            "details": {
                "workdir": str(home / "worktrees" / f"{deal_id}-{stage}-{attempt}"),
                "verdict": {
                    "ok": False,
                    "kind": "contract",
                    "details": [
                        "result.json:evidence[0].exit: evidence.exit: exit=1, а объявлен must=0",
                        "result.json:artifacts[0].sha256: artifact.sha256: хеш не тот",
                    ],
                },
            },
        },
        "idempotency_key": f"test:{deal_id}:{stage}:{attempt}:ended",
    }
    journal.append(conn, [ended])
    conn.close()


def test_why_with_stage_prints_last_attempt_and_verdict(home, frozen_now, capsys):
    deal_id = own_deal(home)
    seed_attempt(home, deal_id=deal_id)
    code, out, _ = run(["why", deal_id, "impl"], capsys)

    assert code == 0
    assert "последняя попытка этапа impl:" in out
    assert "попытка #1" in out
    assert "outcome=failed" in out and "kind=contract" in out
    assert "причина: result.json:evidence[0].exit" in out
    assert "отказы гейта (2):" in out
    assert "evidence.exit" in out and "artifact.sha256" in out


def test_why_with_stage_reports_the_last_one_when_there_were_many(home, frozen_now, capsys):
    deal_id = own_deal(home)
    seed_attempt(home, deal_id=deal_id, attempt=1)
    seed_attempt(home, deal_id=deal_id, attempt=2)
    code, out, _ = run(["why", deal_id, "impl"], capsys)
    assert code == 0
    assert "попытка #2" in out


def test_why_with_stage_says_when_attempts_are_missing(home, frozen_now, capsys):
    deal_id = own_deal(home)
    code, out, _ = run(["why", deal_id, "impl"], capsys)
    assert code == 0
    assert "последняя попытка этапа impl:" in out
    assert "в журнале нет попыток этапа impl" in out


def test_why_json_with_stage_adds_verdict_without_touching_the_route_form(home, client, frozen_now, capsys):
    deal_id = own_deal(home)
    seed_attempt(home, deal_id=deal_id)
    code, out, _ = run(["why", deal_id, "impl", "--json"], capsys)
    payload = json.loads(out)

    assert code == 0
    route = client.get(f"/deal/{deal_id}").json()
    assert {key: value for key, value in payload.items() if key not in WHY_EXTRA_KEYS} == route
    assert payload["verdict"]["attempt"] == 1
    assert payload["verdict"]["kind"] == "contract"
    assert len(payload["verdict"]["details"]) == 2
    # Факты отказов едут отдельным ключом: класс, счётчик и `retry_at` считает
    # политика, а не терминал.
    assert payload["failures"]["found"] is True
    assert payload["failures"]["items"][-1]["kind"] == "contract"
    assert payload["failures"]["items"][-1]["counter"] == "1/1 contract"


def attempt_of(home: Path, deal_id: str, *, stage: str, number: int, started_at: int,
               ended_at: Optional[int], outcome: str = "done",
               kind: Optional[str] = None, reason: str = "",
               details: Optional[dict] = None) -> None:
    """Попытка этапа в журнале — той же формой, что пишет движок."""
    journal_module = cli._load_journal()
    conn = cli.api().deals.open_store(home)
    assert conn is not None
    run_id = f"{deal_id}:{stage}#{number}"
    events = [
        {
            "deal_id": deal_id,
            "repo": REPO,
            "kind": "attempt_started",
            "ts": started_at,
            "recorded_at": started_at,
            "source": "engine",
            "payload": {
                "run_id": run_id,
                "stage": stage,
                "attempt": number,
                "pid": None,
                "lease_expires": None,
                "contract": {"role": "implementer"},
            },
            "idempotency_key": f"test:{deal_id}:{stage}:{number}:started",
        }
    ]
    if ended_at is not None:
        events.append(
            {
                "deal_id": deal_id,
                "repo": REPO,
                "kind": "attempt_ended",
                "ts": ended_at,
                "recorded_at": ended_at,
                "source": "engine",
                "payload": {
                    "run_id": run_id,
                    "stage": stage,
                    "attempt": number,
                    "outcome": outcome,
                    "kind": kind,
                    "reason": reason,
                    "base": None,
                    "commit": None,
                    **({"details": details} if details is not None else {}),
                },
                "idempotency_key": f"test:{deal_id}:{stage}:{number}:ended",
            }
        )
    try:
        journal_module.append(conn, events)
    finally:
        conn.close()


def test_stats_prints_percentiles_and_first_try(home, frozen_now, capsys):
    """Здоровье по журналу своего store: доля этапов с первой попытки и ожидание."""
    first = make_deal(home, title="раз", created_at=NOW - 100)
    second = make_deal(home, title="два", created_at=NOW - 200)
    attempt_of(home, first, stage="impl", number=1, started_at=NOW - 90, ended_at=NOW - 80)
    attempt_of(
        home, second, stage="impl", number=1, started_at=NOW - 190, ended_at=NOW - 180, outcome="crashed"
    )
    code, out, _ = run(["stats"], capsys)

    assert code == 0
    assert "p50" in out and "p90" in out
    assert "с первой попытки: 1/2 (50%)" in out
    assert "crashed 1" in out
    assert f"{REPO}" in out  # группировка по репозиторию


def test_stats_since_counts_only_deals_created_after_it(home, frozen_now, capsys):
    old = make_deal(home, title="старое", created_at=NOW - 10_000)
    new = make_deal(home, title="новое", created_at=NOW - 100)
    attempt_of(home, old, stage="impl", number=1, started_at=NOW - 9_000, ended_at=NOW - 8_900)
    attempt_of(
        home, new, stage="impl", number=1, started_at=NOW - 90, ended_at=NOW - 80, outcome="crashed"
    )
    full = cli.api().stats()
    windowed = cli.api().stats(since=NOW - 1000)

    assert full["totals"]["deals"] == 3  # дело фикстуры плюс старое и новое
    assert windowed["totals"]["deals"] == 1
    assert windowed["totals"]["attempts_by_outcome"] == {"crashed": 1}
    assert windowed["totals"]["first_try"] == {"ok": 0, "closed": 1, "share": 0.0}
    # Ожидание пересчитано по окну (10 с), а не взято из полного среза (100 и 1000 с):
    # p90 полной выборки — 1000 с, у окна — 10 с.
    assert full["totals"]["waiting_sec"]["p90"] == 1000
    assert windowed["totals"]["waiting_sec"]["p90"] == 10
    assert windowed["totals"]["waiting_sec"]["count"] == 1


def test_stats_prints_deals_only_and_no_card_section(home, frozen_now, capsys):
    """Терминал печатает здоровье дел и ни одной строки про карточки kanban.

    Секция отвечает спека-вопросами (этапы, ожидание человека, отказы контракта,
    токены). Карточка фикстуры (`make_card`) в счёт дел не входит, а блока
    «КАРТОЧКИ КАНБАНА» в выводе больше нет: наблюдение живёт в `GET /cards`.
    Красная проба: вернуть в `render_stats` секцию карточек — тест падает.
    """
    deal_id = make_deal(home, title="с вопросами", created_at=NOW - 500)
    attempt_of(
        home,
        deal_id,
        stage="impl",
        number=1,
        started_at=NOW - 400,
        ended_at=NOW - 300,
        details={"usage_tokens": 1500},
    )
    journal_module = cli._load_journal()
    conn = cli.api().deals.open_store(home)
    assert conn is not None
    try:
        journal_module.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "repo": REPO,
                    "kind": "question_asked",
                    "ts": NOW - 350,
                    "recorded_at": NOW - 350,
                    "source": "engine",
                    "payload": {"question_id": 7, "critical": 0},
                    "idempotency_key": f"test:{deal_id}:asked",
                },
                {
                    "deal_id": deal_id,
                    "repo": REPO,
                    "kind": "question_answered",
                    "ts": NOW - 50,
                    "recorded_at": NOW - 50,
                    "source": "engine",
                    "payload": {"question_id": 7, "answer_len": 2},
                    "idempotency_key": f"test:{deal_id}:answered",
                },
            ],
        )
    finally:
        conn.close()
    code, out, _ = run(["stats"], capsys)

    assert code == 0
    assert "ЗДОРОВЬЕ ДЕЛ" in out
    assert "этапов с первой попытки: 1/1 (100%)" in out
    # Ожидание человека — 300 с (question_asked → question_answered), а не 100 с
    # (created → первая попытка): это две разные величины спеки.
    assert "ожидание человека: p50 5 мин" in out
    assert "ожидание до первой попытки этапа: p50 1 мин" in out
    assert "токены за окно: 2k (по 1 попыткам)" in out
    assert "отказы контракта: не было" in out
    # Карточек в здоровье нет: ни блока, ни их чисел, ни подписи наблюдения.
    assert "КАРТОЧКИ КАНБАНА" not in out
    assert "карточек:" not in out
    assert "статусы карточек" not in out
    assert "ранов:" not in out


# ----------------------------------------------------------------------- watch


def test_watch_redraws_every_tick(home, deal, frozen_now):
    stream = io.StringIO()
    code = cli.run_watch(interval=0, ticks=2, stream=stream)
    body = stream.getvalue()

    assert code == cli.EXIT_OK
    assert body.count("ДЕЛА СЕЙЧАС") == 2
    assert deal in body


def test_watch_survives_source_degradation(home, deal, frozen_now):
    (home / "messaging" / "pool.db").unlink()
    stream = io.StringIO()

    code = cli.run_watch(interval=0, ticks=2, stream=stream)
    body = stream.getvalue()

    # Деградация не роняет тик: причина строкой, дела store напечатаны.
    assert code == cli.EXIT_OK
    assert body.count("ДЕЛА СЕЙЧАС") == 2
    assert "! источник messaging/pool.db" in body
    assert deal in body


def test_watch_of_unknown_repo_keeps_looping_with_empty_slice(home, capsys):
    # Неизвестный `--repo` — фильтр, а не ресурс (0.0.6): репозиторий — свойство
    # дела, поэтому опечатка даёт пустой срез и код 0, а не «такого репозитория
    # нет». `ticks` — страховка теста от зависания: прогон кончается сам.
    stream = io.StringIO()
    code = cli.run_watch(interval=0, repo="nope", ticks=3, stream=stream)
    assert code == cli.EXIT_OK
    body = stream.getvalue()
    assert body.count("ДЕЛА СЕЙЧАС") == 3
    assert "дел нет" in body


def test_watch_with_bad_arguments_fails_fast_instead_of_looping(home):
    stream = io.StringIO()
    code = cli.run_watch(interval=0, state="bogus", ticks=3, stream=stream)
    assert code == cli.EXIT_SOURCE
    assert "unknown state" in stream.getvalue()


def test_watch_keeps_ticking_after_a_failed_tick(home, frozen_now, monkeypatch):
    """Отказ посреди работы — строка и следующий тик, а не выход."""
    route = cli.api().now
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise cli.SourceFailure("journal: недоступен")
        return route(**kwargs)

    monkeypatch.setattr(cli.api(), "now", flaky)
    stream = io.StringIO()
    code = cli.run_watch(interval=0, ticks=3, stream=stream)
    body = stream.getvalue()

    assert code == cli.EXIT_OK
    assert calls["n"] == 3
    assert "! источники: journal: недоступен" in body
    assert body.count("ДЕЛА СЕЙЧАС") == 2  # первый и третий тик напечатаны


def test_watch_json_is_one_route_payload_per_tick(home, client, frozen_now):
    stream = io.StringIO()
    code = cli.run_watch(interval=0, ticks=2, as_json=True, stream=stream)
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]

    assert code == cli.EXIT_OK
    assert len(lines) == 2
    assert json.loads(lines[0]) == json.loads(lines[1]) == client.get("/now").json()


def test_watch_json_keeps_stdout_parsable_on_source_degradation(home, frozen_now, monkeypatch, capsys):
    """`watch --json` при отказе источника: stdout — только JSON, причина в stderr.

    Построчный потребитель (`duty --json watch | jq -c`) не должен получать из
    stdout строку `! источники: …`: в json-режиме деградация идёт в stderr, а
    stdout остаётся потоком ответов роута.
    """
    route = cli.api().now
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise cli.SourceFailure("journal: недоступен")
        return route(**kwargs)

    monkeypatch.setattr(cli.api(), "now", flaky)
    stream = io.StringIO()
    code = cli.run_watch(interval=0, ticks=3, as_json=True, stream=stream)

    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert code == cli.EXIT_OK
    assert calls["n"] == 3
    assert len(lines) == 2
    for line in lines:
        json.loads(line)
    assert "! источники: journal: недоступен" in capsys.readouterr().err


# ---------------------------------------------------------- чтение без записи


def test_no_command_writes_to_foreign_databases(home, deal, frozen_now, monkeypatch):
    """Читающие команды не пишут ни в карточки, ни в пул, ни в свой журнал."""
    board = home / "kanban" / "boards" / "b1" / "kanban.db"
    pool = home / "messaging" / "pool.db"
    journal = home / "duty" / "duty.db"
    board_before = board.read_bytes()
    pool_before = pool.read_bytes()
    journal_before = journal.read_bytes()

    for argv in (
        ["now"],
        ["now", "--json"],
        ["deal", deal],
        ["attempts", deal],
        ["why", deal],
        ["stats"],
    ):
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        cli.main(argv)

    assert board.read_bytes() == board_before, "карточки kanban — только чтение"
    assert pool.read_bytes() == pool_before, "пул вопросов — только чтение"
    assert journal.read_bytes() == journal_before, "чтение дописало факты в журнал"


def test_help_without_command_is_not_an_error(capsys):
    code, out, _ = run([], capsys)
    assert code == cli.EXIT_OK
    assert "usage: duty" in out


# --------------------------------------------- решение человека: close и decide
#
# Запаркованное дело ждёт **решения**, а не ответа: ответ приходит текстом
# (`question_answered`), а `continue|stop` — командой. Эти тесты держат обе
# половины: закрытие дела (`close`) и одну команду на оба решения (`decide`),
# плюс границу «текст ответа — не сигнал» (`docs/RUNBOOK.md`).

ANSWER = "stop: прогон закрыт координатором — это E2E-дело, продолжать не нужно"


def own_deal(home: Path, *, title: str = "своё дело") -> str:
    """Своё дело в store (`d_<hex>`) в репозитории фикстуры — через тот же роут.

    Путь тот же, что у `bin/duty create`: тесты мутаций проверяют CLI, а не
    второй способ завести дело.
    """
    payload = cli.api().deals_create(home, {"repo": REPO, "title": title}, source="cli")
    return str(payload["id"])


def park_deal(home: Path, deal_id: str, *, question_id: int = 293, answer: str = ANSWER) -> None:
    """Запарковать дело как движок: вопрос, парковка и ответ человека в пуле.

    Порядок фактов — журнальный: вопрос, парковка, ответ. Текст ответа живёт
    только в пуле (`questions.answer`), в журнале остаётся `answer_len` — ровно
    так их пишут `ask()` и `sync_answers`.
    """
    api = cli.api()
    journal = cli._load_journal()
    conn = api.deals.open_store(home)
    assert conn is not None
    try:
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "repo": REPO,
                    "kind": "question_asked",
                    "ts": NOW - 30,
                    "recorded_at": NOW - 30,
                    "source": "ask",
                    "payload": {
                        "question_id": question_id,
                        "session_key": f"t_{deal_id}",
                        "routing": {"target": "human", "reason": "risky", "stage": "verify", "attempt": 1},
                    },
                    "idempotency_key": f"test:{deal_id}:asked:{question_id}",
                }
            ],
        )
        api.deals.set_blocked(
            conn,
            deal_id,
            kind="parked",
            reason="Попытки исчерпаны: 1/1 contract на этапе verify",
            author="engine",
            source="engine",
            details={
                "kind": "contract",
                "stage": "verify",
                "attempts": 1,
                "limit": 1,
                "question_id": question_id,
                "park_reason": "retries_exhausted",
            },
        )
        journal.append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "repo": REPO,
                    "kind": "question_answered",
                    "ts": NOW - 10,
                    "recorded_at": NOW - 10,
                    "source": "ask",
                    "payload": {"question_id": question_id, "answer_len": len(answer), "answered_via": "manual"},
                    "idempotency_key": f"test:{deal_id}:answered:{question_id}",
                }
            ],
        )
    finally:
        conn.close()
    pool = sqlite3.connect(home / "messaging" / "pool.db")
    try:
        pool.execute(
            "INSERT INTO questions (id, session_key, text, origin, status, kind, answer)"
            " VALUES (?,?,?,'duty','answered','question',?)",
            (question_id, f"t_{deal_id}", "вопрос по делу", answer),
        )
        pool.commit()
    finally:
        pool.close()


def journal_events(home: Path, deal_id: str) -> list[dict]:
    """События журнала дела — то, против чего сверяются тесты мутаций."""
    journal = cli._load_journal()
    conn = journal.open_journal(home, create=False)
    assert conn is not None
    try:
        return journal.read_events(conn, deal_id)
    finally:
        conn.close()


def deal_status(home: Path, deal_id: str) -> str:
    """Статус своего дела из store — намерение жизненного цикла."""
    api = cli.api()
    conn = api.deals.open_store(home, create=False, readonly=True)
    assert conn is not None
    try:
        row = api.deals.deal_row(conn, deal_id)
        assert row is not None
        return str(row["status"])
    finally:
        conn.close()


def test_close_writes_completed_with_human_cause(home, frozen_now, capsys):
    """`close` пишет один `completed` с причиной человека и закрывает дело."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, out, _ = run(["close", deal_id, "--reason", "прогон закрыт координатором"], capsys)

    assert code == cli.EXIT_OK
    assert "ЗАКРЫТО" in out and "done" in out
    completed = [event for event in journal_events(home, deal_id) if event["kind"] == "completed"]
    assert len(completed) == 1
    payload = cli._load_journal().payload_of(completed[0])
    assert payload["cause"] == "closed_by_human"
    assert payload["reason"] == "прогон закрыт координатором"
    assert payload["stage"] == "verify"
    assert payload["from"] == "wait_human/decision"
    assert deal_status(home, deal_id) == "done"
    # Закрытое дело больше не в ожидающих: журнал говорит `completed`.
    assert deal_id not in [item["id"] for item in cli.api().now()["deals"]]


def test_close_is_idempotent(home, frozen_now, capsys):
    """Повторный `close` с теми же словами не пишет второй факт."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    first, _, _ = run(["close", deal_id, "--reason", "закрыто"], capsys)
    code, out, _ = run(["close", deal_id, "--reason", "закрыто"], capsys)

    assert (first, code) == (cli.EXIT_OK, cli.EXIT_OK)
    assert "событий записано: 0" in out and "повтор" in out
    kinds = [event["kind"] for event in journal_events(home, deal_id)]
    assert kinds.count("completed") == 1


def test_close_refuses_to_rewrite_another_close(home, frozen_now, capsys):
    """Закрытое другим фактом дело не перезакрывается молча."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)
    run(["close", deal_id, "--reason", "закрыто"], capsys)

    code, _, err = run(["close", deal_id, "--reason", "другие слова"], capsys)

    assert code == cli.EXIT_SOURCE
    assert "уже закрыто" in err
    assert [e["kind"] for e in journal_events(home, deal_id)].count("completed") == 1


def test_decide_stop_closes_with_stopped_cause_and_the_answer_as_reason(home, frozen_now, capsys):
    """`decide stop` без `--reason` закрывает дело словами самого человека."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, out, _ = run(["decide", deal_id, "stop"], capsys)

    assert code == cli.EXIT_OK
    assert "ОСТАНОВЛЕНО" in out
    completed = [event for event in journal_events(home, deal_id) if event["kind"] == "completed"]
    assert len(completed) == 1
    payload = cli._load_journal().payload_of(completed[0])
    assert payload["cause"] == "stopped_by_human"
    assert payload["reason"] == ANSWER  # ответ человека, а не выдуманная причина
    assert deal_status(home, deal_id) == "done"


def test_decide_continue_unparks_the_deal(home, frozen_now, capsys):
    """`decide continue` пишет тот же факт, что `unblock`: парковка снята."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, out, _ = run(["decide", deal_id, "continue"], capsys)

    assert code == cli.EXIT_OK
    assert "ПРОДОЛЖЕНО" in out
    events = journal_events(home, deal_id)
    last = cli._load_journal().payload_of(events[-1])
    assert events[-1]["kind"] == "unblocked"
    assert last["previous_kind"] == "parked"
    assert deal_status(home, deal_id) in ("ready", "todo")
    assert [e["kind"] for e in events].count("completed") == 0


def test_decide_on_a_deal_nobody_asked_about_refuses_without_writing(home, frozen_now, capsys):
    """Дело без парковки и без блока — отказ входа, а не тихая запись."""
    deal_id = own_deal(home)
    before = [event["kind"] for event in journal_events(home, deal_id)]

    code, out, err = run(["decide", deal_id, "stop"], capsys)

    assert code == cli.EXIT_SOURCE
    assert out == ""
    assert "не запарковано" in err and "status=ready" in err
    assert [event["kind"] for event in journal_events(home, deal_id)] == before
    assert deal_status(home, deal_id) == "ready"


def test_decide_rejects_an_unknown_decision_word(home, frozen_now, capsys):
    """Решений ровно два: иначе слово не решение, а опечатка."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, _, err = run(["decide", deal_id, "maybe"], capsys)

    assert code == cli.EXIT_SOURCE
    assert "неизвестно" in err and "continue" in err and "stop" in err
    assert [e["kind"] for e in journal_events(home, deal_id)].count("completed") == 0


def test_answer_text_is_not_a_command(home, frozen_now, capsys):
    """`stop: …` в ответе пула — текст, а не сигнал: дело остаётся запаркованным."""
    deal_id = own_deal(home)
    park_deal(home, deal_id, answer="stop: закрываем, продолжать не нужно")

    payload = cli.api().collect_now(home, NOW)
    shown = next(item for item in payload["deals"] if item["id"] == deal_id)

    assert shown["waiting"]["state"] == "wait_human"
    assert shown["waiting"]["human_kind"] == "decision"
    assert [e["kind"] for e in journal_events(home, deal_id)].count("completed") == 0
    # Та же граница и во вторую сторону: `continue: …` тоже ничего не поднимает.
    park_deal(home, own_deal(home), question_id=294, answer="continue: давай дальше")
    assert len([item for item in cli.api().now()["deals"] if item["waiting"]["human_kind"] == "decision"]) == 2


def test_evidence_names_the_answer_and_the_decision_command(home, frozen_now, capsys):
    """Доказательство говорит, что ответ есть и чего не хватает: команды."""
    deal_id = own_deal(home)
    park_deal(home, deal_id)

    code, out, _ = run(["why", deal_id], capsys)
    evidence = next(
        item for item in cli.api().now()["deals"] if item["id"] == deal_id
    )["waiting"]["evidence"]

    assert code == cli.EXIT_OK
    assert "ответ #293 получен: stop: прогон закрыт координатором" in evidence
    assert f"нужно решение: bin/duty decide {deal_id} continue|stop" in evidence
    assert evidence in out  # CLI не переписывает текст бэкенда
    # В таблице «сейчас» доказательство обрезано по ширине колонки, но состояние
    # названо целиком: `wait_human/decision` — это и есть «ответ есть, решение нет».
    code, out, _ = run(["now"], capsys)
    assert code == cli.EXIT_OK
    assert "wait_human/decision" in out


# ------------------- W21: `decide stop` на бегущем деле без живого драйвера
#
# Живой случай: попытка погибла при рестарте гейтвея, драйвера нет, а лизинг
# живёт ещё 40 минут — остановить было нечем, `decide stop` отказывал «дело
# решения человека не ждёт». Правило: `running` без живого лизинга останавливать
# можно (и факт этот пишется), живой лизинг — по-прежнему отказ и ни одной записи.

def running_deal(home: Path, *, lease_expires: Optional[int] = None) -> str:
    """Дело в работе, как его оставляет шаг движка: `claimed` плюс попытка.

    `lease_expires` задан — в store лежит строка лизинга с этим сроком (живая или
    протухшая, это решает тест); не задан — лизинга нет вовсе, то есть драйвера
    тоже нет. pid в попытке — свой: «процесс попытки жив, а драйвера нет» — ровно
    тот живой случай из карточки (гейтвей перезапустили, лизинг остался).
    """
    deal_id = own_deal(home)
    api = cli.api()
    conn = api.deals.open_store(home)
    assert conn is not None
    try:
        api.deals.claim_deal(conn, deal_id, owner="duty", source="engine", now=NOW)
        cli._load_journal().append(
            conn,
            [
                {
                    "deal_id": deal_id,
                    "repo": REPO,
                    "kind": "attempt_started",
                    "ts": NOW - 60,
                    "recorded_at": NOW - 60,
                    "source": "engine",
                    "payload": {
                        "run_id": f"{deal_id}:impl#1",
                        "stage": "impl",
                        "attempt": 1,
                        "pid": os.getpid(),
                        "lease_expires": lease_expires,
                        "contract": {"role": "implementer"},
                    },
                    "idempotency_key": f"test:{deal_id}:impl#1",
                }
            ],
        )
        if lease_expires is not None:
            conn.execute(
                "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (deal_id, "impl", 1, os.getpid(), NOW - 60, lease_expires),
            )
            conn.commit()
    finally:
        conn.close()
    return deal_id


@pytest.fixture()
def frozen_deals_now(frozen_now, monkeypatch):
    """Часы store заморожены: срок лизинга в тесте — `NOW`, а не живые часы.

    `frozen_now` замораживает только часы роутов, а `deals._now_sec` читает
    настоящий момент: лизинг «истёк 10 секунд назад» по часам теста оказался бы
    живым ещё на годы, и правило «нет живого драйвера» проверить было бы нельзя.
    """
    monkeypatch.setattr(cli.api().deals, "_now_sec", lambda: NOW)
    return NOW


def leases_of(home: Path, deal_id: str) -> list[dict]:
    """Строки лизинга дела — единственный признак «кто-то работает»."""
    api = cli.api()
    conn = api.deals.open_store(home, create=False, readonly=True)
    assert conn is not None
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT stage, pid, expires_at FROM leases WHERE deal_id = ?", (deal_id,)
            )
        ]
    finally:
        conn.close()


def test_decide_stop_closes_a_running_deal_without_a_driver(home, frozen_deals_now, capsys):
    """Бегущее дело без лизинга: остановка разрешена и пишется фактом."""
    deal_id = running_deal(home)

    code, out, _ = run(["decide", deal_id, "stop", "--reason", "попытка погибла"], capsys)

    assert code == cli.EXIT_OK
    assert "ОСТАНОВЛЕНО" in out and "done" in out
    completed = [event for event in journal_events(home, deal_id) if event["kind"] == "completed"]
    assert len(completed) == 1
    payload = cli._load_journal().payload_of(completed[0])
    assert payload["cause"] == "stopped_by_human"
    assert payload["reason"] == "попытка погибла"
    # `from` — состояние **журнала** на момент закрытия (`working`: попытка идёт),
    # а не слово строки store: причина закрытия не пересказывает статус.
    assert payload["from"] == "working"
    assert deal_status(home, deal_id) == "done"
    assert leases_of(home, deal_id) == []
    # Закрытое дело уходит из «сейчас»: журнал и срез согласны.
    assert deal_id not in [item["id"] for item in cli.api().now()["deals"]]


def test_decide_stop_refuses_a_running_deal_held_by_a_live_lease(home, frozen_deals_now, capsys):
    """Живой лизинг: отказ и **ни одной записи** — чужую работу не рвём."""
    deal_id = running_deal(home, lease_expires=NOW + 600)
    before = [event["kind"] for event in journal_events(home, deal_id)]

    code, out, err = run(["decide", deal_id, "stop"], capsys)

    assert code == cli.EXIT_SOURCE
    assert out == ""
    assert "живой лизинг" in err and "impl" in err
    assert [event["kind"] for event in journal_events(home, deal_id)] == before
    assert deal_status(home, deal_id) == "running"
    assert leases_of(home, deal_id), "живой лизинг не снимается никогда"


def test_decide_stop_after_the_lease_expired_is_allowed(home, frozen_deals_now, capsys):
    """Срок лизинга прошёл — драйвера нет, остановка разрешена и лизинг снят."""
    deal_id = running_deal(home, lease_expires=NOW - 10)

    code, out, _ = run(["decide", deal_id, "stop", "--reason", "гейтвей перезапустили"], capsys)

    assert code == cli.EXIT_OK
    assert "ОСТАНОВЛЕНО" in out
    assert deal_status(home, deal_id) == "done"
    assert leases_of(home, deal_id) == [], "протухший лизинг снимается тем же фактом"
    completed = [event for event in journal_events(home, deal_id) if event["kind"] == "completed"]
    assert len(completed) == 1
    assert cli._load_journal().payload_of(completed[0])["cause"] == "stopped_by_human"


def test_decide_continue_on_a_running_deal_still_refuses(home, frozen_deals_now, capsys):
    """Исключение ровно одно: `continue` бегущему делу нечего продолжать."""
    deal_id = running_deal(home)
    before = [event["kind"] for event in journal_events(home, deal_id)]

    code, out, err = run(["decide", deal_id, "continue"], capsys)

    assert code == cli.EXIT_SOURCE
    assert out == ""
    assert "status=running" in err
    assert [event["kind"] for event in journal_events(home, deal_id)] == before
    assert deal_status(home, deal_id) == "running"


# ------------- «шага движка не было» над таймлайном (FIX-6 / остаток B1)
#
# Своё дело с попытками, запаркованное и разблокированное человеком, живого факта
# не имеет — и `bin/duty deal` печатал «шага движка ещё не было» прямо над
# таймлайном этих попыток. Теперь доказательство называет то, что подтверждено:
# идущей попытки нет, следующего шага не было. Текст — один на три поверхности.


def test_deal_of_unblocked_own_deal_does_not_contradict_its_attempt_timeline(
    home, frozen_now, capsys
):
    """FIX-6: «доказательство:» не отрицает шаги, которые напечатаны ниже."""
    deal_id = own_deal(home)
    write_three_attempts(home, deal_id)

    code, _, _ = run(["block", deal_id, "исчерпаны попытки: 2/2", "--kind", "parked"], capsys)
    assert code == cli.EXIT_OK
    code, _, _ = run(["unblock", deal_id], capsys)
    assert code == cli.EXIT_OK
    assert deal_status(home, deal_id) in ("ready", "todo")

    code, out, _ = run(["deal", deal_id], capsys)

    assert code == cli.EXIT_OK
    evidence = next(line for line in out.splitlines() if line.startswith("доказательство:"))
    assert "шага движка ещё не было" not in evidence
    assert "идущей попытки нет" in evidence
    assert "попыток в журнале: 2" in evidence
    # Таймлайн ниже — те же шаги: строка доказательства и он больше не спорят.
    assert "impl#1" in out and "review#1" in out

    # Тот же факт на остальных поверхностях: `/now` и `why` называют то же
    # состояние ожидания и ту же строку доказательства.
    row = next(item for item in cli.api().now()["deals"] if item["id"] == deal_id)
    assert row["waiting"]["state"] == "wait_slot"
    assert row["waiting"]["evidence"].endswith(evidence.split("доказательство: ", 1)[1])

    code, why_out, _ = run(["why", deal_id], capsys)
    assert code == cli.EXIT_OK
    assert row["waiting"]["evidence"] in why_out
    assert "состояние ожидания: wait_slot" in why_out
