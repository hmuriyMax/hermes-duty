"""Прогон задачи через плагин — демонстрация на песочном корне.

Зачем это здесь. Спека (`~/.hermes/widgets/duty-model.html`) описывает не экран,
а модель труда: дело, этапы, попытки, доказательства, вопросы, журнал. Проверить
модель «на словах» нельзя, поэтому в плагине есть прогон: одна команда

    bin/duty demo

проходит дело от создания до закрытия на **своём** песочном корне и печатает то,
что действительно записалось в журнал и что показывают поверхности. Ни одна
живая БД при этом не открывается на запись: песочница создаётся во временном
каталоге, а `~/.hermes` только читается, чтобы взять DDL схем (пул и доска).

Граница честности. В 0.0.4 есть журнал, вопрос, гейт, владение делом, движок
(этапы, лизинг, спавн `hermes chat -p <role>`, проверка evidence, классы отказа и
политика ретраев); нет reap и декомпозиции, и прогон это не прячет: шаги, которые
сегодня делает скрипт, помечены `script`, а то, что появится с движком, —
отдельным блоком в конце (`engine_todo`). Иначе демонстрация врала бы ровно так,
как спека запрещает.

Что проверяет сам прогон (и падает, если не сходится):

1. вопрос уводит дело в `wait_human`, ответ возвращает в работу;
2. поздний ответ на закрытое дело его не воскрешает (B3-класс);
3. гейт ребёнка открывается завершением родителя, а не вручную;
4. `/now`, `/deal` и журнал говорят одно и то же про одно дело;
5. `kanban.db` после прогона не изменился ни на байт (инвариант наблюдателя);
6. повторный `ingest` не плодит событий;
7. две параллельные полосы живут каждая в своей ветке и своём дереве, этап-слияние
   собирает их в ОДИН коммит с двумя родителями, а конфликт двух полос в одном
   файле не «закрывается» стратегией: движок называет файлы, сохраняет деревья
   полос и поднимает резолвера с обоими диффами (0.2.2);
8. нерождённый этап не переживает снятия: закрытие отказывает, пока он в графе,
   ревизия `{id, remove: true}` убирает его из графа движка и с экрана (0.2.2).

Граница слияния в прогоне. Полосы и резолвера здесь играет скрипт: демо гоняется
в гейте и модель не поднимает. Настоящее — всё, что делает движок: `git worktree`
на полосу, коммит и ветка полосы, слияние из двух полос, конфликт в индексе,
контракт резолвера на диске и решение гейта пакета по его отчёту.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Репозиторий демо-дела: группировка (не адрес). Дело адресуется своим id.
DEMO_REPO = "/tmp/duty-demo-repo"
# Каталог чужой kanban-БД в песочнице: демо читает её только наблюдением.
DEMO_BOARD = "duty-demo"
DEMO_TITLE = "тристейт в виджете: разобрать, починить, проверить"

# Схемы песочницы. Живую БД не копируем (там данные), берём только DDL — так
# песочная `questions` не разъедется с настоящей. Если живого файла нет
# (чистая машина, CI), берётся встроенный минимум: те колонки, которые читает
# `ask()`.
POOL_FALLBACK = """
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_key TEXT NOT NULL,
  project TEXT,
  text TEXT NOT NULL,
  text_full TEXT,
  origin TEXT,
  options_json TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','sent','answered','expired','pickup_gateway')),
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

KANBAN_FALLBACK = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT,
    created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT,
    branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER,
    tenant TEXT, result TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER, last_failure_error TEXT, max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER, current_run_id INTEGER, block_kind TEXT,
    block_recurrences INTEGER NOT NULL DEFAULT 0, merged_into TEXT
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT,
    step_key TEXT, status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER,
    worker_pid INTEGER, worker_started_at INTEGER, ended_at INTEGER,
    started_at TEXT, outcome TEXT, last_output TEXT, error TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    run_id INTEGER, kind TEXT NOT NULL, payload TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
"""

# Спека дела — тот же пример, что в `examples/demo.yaml`: прогон печатает её
# целиком, чтобы было видно, чего у 0.0.3 ещё нет (этапы и evidence-команды).
DEMO_SPEC: dict[str, Any] = {
    "duty": "sx-widgets-tristate",
    "repo": "~/GolandProjects/sx-widgets-api",
    "base": "origin/master",
    "stages": [
        {"id": "impl", "role": "implementer", "evidence": ["go build ./...", "go test ./widget/..."]},
        {"id": "verify", "role": "tester", "needs": ["impl"]},
        {"id": "review", "role": "reviewer", "needs": ["verify"], "verdict": {"approve": "done", "changes": "impl", "max_rounds": 2}},
    ],
}

# Спека дела, на котором прогон показывает снятие мёртвого этапа (`{id, remove:
# true}`): работа `impl` и закрытие `close`. Этап `extra` план добавляет ревизией 1
# и снимает ревизией 2. До 0.2.2 такой этап оставался в графе нерождённым и
# попадал в итог дела как сделанный (`d_e7fb271b`, 18.11).
REMOVAL_SPEC = """
duty: снятие мёртвого этапа
stages:
  - id: impl
    role: implementer
    workspace: none
    evidence:
      - {cmd: "true", must: 0}
  - id: close
    role: coordinator
    needs: [impl]
    workspace: none
    verdict: {approve: next, changes: impl, max_rounds: 2}
"""

# Чего у 0.1.0 нет — одной строкой, чтобы прогон сам называл границу версии.
# Декомпозиции дела на этапы здесь быть не может: план лида этапы и добавляет,
# и переставляет (`spec.parse_plan`, `transit.graph`), и это уже 0.1.0.
# Осталось то, что планом не решается: triage — разбор **этапа** на подзадачи.
ENGINE_TODO = (
    "triage внутри duty (разбор этапа на подзадачи) — не в 0.1.0",
)


class DemoError(RuntimeError):
    """Прогон не сошёлся: показывать его как успешный нельзя."""


def _now() -> int:
    return int(time.time())


def _ddl(db: Path, fallback: str) -> str:
    """DDL живой БД (только схема), иначе встроенный минимум."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return fallback
    sql = ";\n".join(str(row[0]) for row in rows if row[0])
    return f"{sql};\n" if sql.strip() else fallback


def _make_db(path: Path, ddl: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ddl)
        conn.commit()
    finally:
        conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_sandbox(home: Path, live: Path) -> dict[str, Any]:
    """Песочный корень Hermes: свой журнал, свой пул, своя доска, свой профиль.

    `live` — живой `~/.hermes`: из него берём только DDL (на чтение).
    """
    if Path(home).resolve() == Path(live).resolve():
        raise DemoError("песочный корень не может совпадать с живым: прогон пишет в свой пул")
    home.mkdir(parents=True, exist_ok=True)
    pool = home / "messaging" / "pool.db"
    _make_db(pool, _ddl(live / "messaging" / "pool.db", POOL_FALLBACK))
    board = home / "kanban" / "boards" / DEMO_BOARD / "kanban.db"
    _make_db(board, _ddl(live / "kanban" / "boards" / "hermes-duty" / "kanban.db", KANBAN_FALLBACK))
    (home / "profiles" / "implementer").mkdir(parents=True, exist_ok=True)
    sqlite3.connect(home / "profiles" / "implementer" / "state.db").close()
    return {"home": home, "pool": pool, "board": board}


def _seed_observed(board: Path, now: int) -> dict[str, Any]:
    """Наблюдаемое kanban-дело: у него есть попытки — их видно только через ingest.

    Своё дело (`d_`) попыток не имеет: попытка — это процесс, а процессов в
    0.0.3 нет. Поэтому вторая половина прогона показывает, откуда берутся
    `attempt_started`/`attempt_ended`: из ранов живой доски, на чтение.
    """
    task_id = "t_a1b2c3d4"
    conn = sqlite3.connect(board)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, created_at, claim_lock,"
            " claim_expires, worker_pid, current_run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "kanban-дело с двумя попытками", "тело", "implementer", "running", now - 900,
             "demo-host:1", now + 600, 4242, 2),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, "created", json.dumps({"status": "ready"}), now - 900),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (task_id, "claimed", json.dumps({"run_id": 2}), now - 300),
        )
        conn.executemany(
            "INSERT INTO task_runs (task_id, profile, status, started_at, worker_started_at,"
            " ended_at, outcome) VALUES (?,?,?,?,?,?,?)",
            [
                (task_id, "implementer", "crashed", f"{now - 800}", now - 800, now - 700, "crashed"),
                (task_id, "implementer", "running", f"{now - 300}", now - 300, None, None),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return {"task_id": task_id, "path": board}


# ------------------------------------------------------- слияние полос (0.2.2)
#
# Сценарий демо для fan-in: две полосы от одной базы, чистый merge и конфликт двух
# полос в ОДНОМ файле. Требование, ради которого версия делалась (Макс, 18.11):
# «каждое изменение — в своём ворктри/ветке, этап слияния вливает их в единую
# ветку дела, при этом решая конфликты и не теряя логику».
#
# Модель здесь не поднимается: демо гоняется в гейте. Роль играет `Scripted`, а
# всё, что доказывает слияние, делает движок по-настоящему — дерево `git worktree`
# на полосу, коммит и ветку полосы, слияние с двумя родителями, конфликт в индексе
# и решение гейта пакета по отчёту резолвера.


def _module(name: str, filename: str) -> Any:
    """Модуль плагина под общим именем: второй копии схемы в процессе не будет."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise DemoError(f"модуль не найден: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str) -> str:
    """`git` в песочнице демо: подпись задаётся здесь, чужие настройки не читаются."""
    done = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=duty-demo", "-c", "user.email=demo@local", *args],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise DemoError(f"git {' '.join(args)}: {(done.stderr or done.stdout).strip() or done.returncode}")
    return done.stdout.strip()


def git_optional(cwd: Path, *args: str) -> str:
    """`git` без отказа: пустая строка — «не нашлось» (ветки, которой ещё нет)."""
    try:
        return git(cwd, *args)
    except DemoError:
        return ""


def build_merge_repo(root: Path) -> tuple[Path, str]:
    """Репозиторий полос: одна база и два файла — от неё отводятся деревья полос."""
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("общая база\n", encoding="utf-8")
    (repo / "shared.txt").write_text("общая строка\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    return repo, git(repo, "rev-parse", "HEAD")


def merge_spec_text(repo: Path, base: str) -> str:
    """Спека сценария: три полосы в своих деревьях, слияние, проверка за слиянием.

    `needs` слияния названы в порядке, отличном от порядка объявления полос: так
    видно, что вливание идёт по объявлению, а не по времени готовности полос.
    Полос три (`impl_c` правит свой файл): слияние обязано собрать ВСЕ стороны, и
    конфликт на средней не теряет хвост.

    Вход один (`start`): спека с двумя входами — не то, что лид вправе написать
    (`spec check` отказывает правилом `entry`, «вход должен быть один»), а прогон
    обязан показывать форму, которую движок примет. Полосы ждут вход: без `needs`
    у каждой полосы дело начиналось бы дважды.
    """
    return (
        "duty: demo-lanes\n"
        f"repo: {repo}\n"
        f"base: {base}\n"
        "stages:\n"
        "  - {id: start, kind: shell, cmd: \"true\", workspace: none}\n"
        "  - {id: impl_a, role: implementer, workspace: worktree, needs: [start], parallel_with: [impl_b, impl_c]}\n"
        "  - {id: impl_b, role: implementer, workspace: worktree, needs: [start], parallel_with: [impl_a, impl_c]}\n"
        "  - {id: impl_c, role: implementer, workspace: worktree, needs: [start], parallel_with: [impl_a, impl_b]}\n"
        "  - {id: join, kind: merge, needs: [impl_b, impl_a, impl_c], workspace: worktree}\n"
        "  - {id: gates, role: tester, workspace: worktree, needs: [join]}\n"
    )


def lane_act(name: str, text: str) -> Callable[[Path, dict[str, Any]], dict[str, Any]]:
    """Полоса: правит свой файл и называет его артефактом (коммит делает движок)."""

    def act(tree: Path, contract: dict[str, Any]) -> dict[str, Any]:
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {
            "status": "done",
            "summary": f"полоса {contract.get('stage')}: правка {name}",
            "artifacts": [{"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}],
            "evidence": [],
        }

    return act


def resolver_act(text: str) -> Callable[[Path, dict[str, Any]], dict[str, Any]]:
    """Резолвер: первая попытка отказывается, вторая разводит обе стороны и коммитит.

    Отказ первой попытки — исход «не смог»: движок обязан назвать конфликт файлами
    (`merge.conflict`) и сохранить деревья полос, а не закрыть дело тишиной.
    Вторая попытка идёт в сохранённом дереве (состояние merge не отменялось) и
    получает merge-коммит с двумя родителями.
    """

    def act(tree: Path, contract: dict[str, Any]) -> dict[str, Any]:
        merge = contract.get("merge") or {}
        if int(contract.get("attempt") or 0) == 1:
            return {
                "status": "refused",
                "summary": "конфликт не разведён: стороны расходятся по существу",
                "evidence": [],
            }
        name = str((merge.get("conflict_files") or ["shared.txt"])[0])
        (tree / name).write_text(text, encoding="utf-8")
        git(tree, "add", name)
        git(tree, "commit", "-q", "--no-edit")
        return {
            "status": "done",
            "summary": f"конфликт {name} разведён: обе стороны на месте",
            "commit": git(tree, "rev-parse", "HEAD"),
            "evidence": [],
            "merge": {
                "files": list(merge.get("conflict_files") or []),
                "sides": [
                    {
                        "lane": str(lane.get("lane") or ""),
                        "sha": str(lane.get("sha") or ""),
                        "intent": f"полоса {lane.get('lane')} правила {name} по-своему",
                        "joined": "строка стороны сохранена в объединённом файле",
                    }
                    for lane in merge.get("lanes") or ()
                ],
            },
        }

    return act


class Scripted:
    """Скрипт за роль: `acts` — что «агент» делает на каждом этапе.

    Отчёт пишется туда, куда велит контракт (`report_to`), поэтому его судит тот
    же гейт пакета, что и настоящую попытку. Этап, которого скрипт не ждал,
    роняет прогон: молча поднятый процесс означал бы, что движок делает не то.
    """

    def __init__(self, acts: dict[str, Callable[[Path, dict[str, Any]], dict[str, Any]]]) -> None:
        self.acts = acts
        self.calls: list[dict[str, Any]] = []
        self.contracts: list[dict[str, Any]] = []
        self.pid = 5150
        self.exit_code = 0
        self.exit = 0
        self.stderr_text = ""
        self.stderr_head = ""

    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "Scripted":
        query = Path(cmd[list(cmd).index("--query-file") + 1])
        contract = json.loads(query.read_text(encoding="utf-8"))
        stage = str(contract.get("stage") or "")
        self.calls.append({"stage": stage, "cwd": str(cwd)})
        self.contracts.append(contract)
        act = self.acts.get(stage)
        if act is None:
            raise DemoError(f"прогон поднял процесс на этапе {stage!r}, которого не ждал")
        report = act(Path(cwd), contract)
        target = Path(str(contract.get("report_to") or ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return self

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        return None

    def finished(self) -> bool:
        return True

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.exit

    def stop(self, grace: float = 10) -> Optional[int]:
        return self.exit


def merge_scenario(
    api: Any, conn: sqlite3.Connection, home: Path, step: Callable[..., None]
) -> dict[str, Any]:
    """Две полосы, чистый merge и конфликт двух полос в одном файле.

    Полосы идут шагом за шагом, а не пачкой: пачку поднимает `transit`, а прогон
    проходит этапы по одному — правило «полоса в своём дереве и своей ветке» от
    этого не меняется, и проверяется именно оно.
    """
    runner = _module("hermes_duty_runner", "runner.py")
    spec_module = runner.spec_module
    deals = api.deals
    journal = api.journal
    root = home / "lanes"
    repo, base = build_merge_repo(root)

    def lane_run(stage_spec: Any, deal: Any, acts: dict[str, Any], stage: str) -> dict[str, Any]:
        report = runner.run_stage(conn, stage_spec, deal, stage, 1, home=home, spawn=Scripted(acts))
        if report.get("outcome") != "done":
            raise DemoError(f"этап {stage} не закрылся: {report.get('reason')}")
        return report

    def entry_run(stage_spec: Any, deal: Any) -> dict[str, Any]:
        """Вход дела — настоящая попытка движка (`kind: shell`, команда `true`).

        Вход в спеке один (`spec check`: два входа — отказ `entry`), и полосы ждут
        его: без этого факта полоса стояла бы `pending`, а прогон проходил бы мимо
        зависимости. Скрипт здесь не подставляется: команду исполняет движок тем
        же путём, что в деле, — роль за `true` не нужна.
        """
        report = runner.run_stage(conn, stage_spec, deal, "start", 1, home=home)
        if report.get("outcome") != "done":
            raise DemoError(f"вход дела не закрылся: {report.get('reason')}")
        return report

    # 1. Чистый fan-in: две полосы правят разные файлы.
    clean_path = root / "clean.yaml"
    clean_path.write_text(merge_spec_text(repo, base), encoding="utf-8")
    clean_spec = spec_module.load_spec(clean_path)
    # Спека прогона судится тем же валидатором, что `spec check`: демо — часть
    # приёмки версии, и форма, которую лид написать не может, доказывала бы не то.
    # Профили названы явно (`tester`, `implementer`): песочница прогона — свой
    # корень Hermes, и живого набора профилей машины в ней нет.
    refusals = spec_module.errors(
        spec_module.validate_spec(clean_spec, profiles=frozenset({"implementer", "tester"}))
    )
    if refusals:
        raise DemoError(
            "спека демо не проходит spec check: " + "; ".join(f"{issue.rule}: {issue.message}" for issue in refusals)
        )
    created = deals.create_deal(conn, repo=str(repo), title="три полосы и слияние", body="")
    clean_deal = deals.deal_row(conn, str(created["deal"]["id"]))
    own = {
        "impl_a": ("a.txt", "работа полосы a\n"),
        "impl_b": ("b.txt", "работа полосы b\n"),
        "impl_c": ("c.txt", "работа полосы c\n"),
    }
    entry_run(clean_spec, clean_deal)
    for stage, (name, text) in own.items():
        lane_run(clean_spec, clean_deal, {stage: lane_act(name, text)}, stage)
    lane_shas = {stage: git(repo, "rev-parse", runner.lane_branch(clean_deal, stage)) for stage in own}
    deal_branch_before = git_optional(repo, "rev-parse", "--verify", runner.deal_branch(clean_deal))
    step(
        14,
        "lanes   (три полосы)",
        f"{clean_deal['id']} · "
        + ", ".join(f"{stage} {sha[:8]}" for stage, sha in lane_shas.items())
        + f" · ветка дела: {deal_branch_before or 'не сдвинута'}",
    )

    # Чистое слияние процессов не поднимает: `spawn` без актов упадёт, если движок
    # всё-таки решит позвать агента.
    merged = runner.run_stage(conn, clean_spec, clean_deal, "join", 1, home=home, spawn=Scripted({}))
    if merged.get("outcome") != "done":
        raise DemoError(f"чистое слияние не собралось: {merged.get('reason')}")
    merge_sha = str(merged.get("commit") or "")
    # Полосы вливаются по объявлению: первая — fast-forward, каждая следующая —
    # `--no-ff`, поэтому при трёх полосах свидетель — последний merge-коммит, а в
    # нём предок КАЖДАЯ полоса (ancestry, а не число родителей).
    lane_inside = [
        stage
        for stage, sha in lane_shas.items()
        if git_optional(repo, "merge-base", "--is-ancestor", sha, merge_sha) == ""
    ]
    parents = git(repo, "rev-list", "--parents", "-n", "1", merge_sha).split()
    after = next(item for item in clean_spec.stages if item.id == "gates")
    dependent_base = runner.base_of_stage(clean_spec, after, journal.read_events(conn, clean_deal["id"]))
    merged_tree = runner.attempt_dir(home, clean_deal, "join", 1) / "tree"
    step(
        15,
        "merge   (чистый fan-in)",
        f"{clean_deal['id']} · коммит {merge_sha[:8]} · родителей {len(parents) - 1} · "
        f"полос в свидетеле {len(lane_inside)}/{len(lane_shas)} · "
        f"a.txt+b.txt+c.txt в дереве · база gates {dependent_base[:8]}",
        by="engine",
    )

    # 2. Конфликт: две полосы правят один файл по-своему, третья — свой.
    conflict_path = root / "conflict.yaml"
    conflict_path.write_text(merge_spec_text(repo, base), encoding="utf-8")
    conflict_spec = spec_module.load_spec(conflict_path)
    created = deals.create_deal(conn, repo=str(repo), title="две полосы в один файл, третья рядом", body="")
    conflict_deal = deals.deal_row(conn, str(created["deal"]["id"]))
    clash = {
        "impl_a": ("shared.txt", "строка от a\n"),
        "impl_b": ("shared.txt", "строка от b\n"),
        "impl_c": ("c.txt", "работа полосы c\n"),
    }
    entry_run(conflict_spec, conflict_deal)
    clash_shas: dict[str, str] = {}
    for stage, (name, text) in clash.items():
        landed = lane_run(conflict_spec, conflict_deal, {stage: lane_act(name, text)}, stage)
        clash_shas[stage] = str(landed.get("commit") or "")
    step(
        16,
        "conflict (общий файл)",
        f"{conflict_deal['id']} · полосы три, a и b правили shared.txt",
    )

    resolver = Scripted({"join": resolver_act("строка от a\nстрока от b\n")})
    refused = runner.run_stage(conn, conflict_spec, conflict_deal, "join", 1, home=home, spawn=resolver)
    contract = resolver.contracts[0] if resolver.contracts else {}
    brief = contract.get("merge") or {}
    diffs = {str(lane.get("lane") or ""): str(lane.get("diff") or "") for lane in brief.get("lanes") or ()}
    kept_trees = {stage: runner.attempt_dir(home, conflict_deal, stage, 1) / "tree" for stage in clash}
    step(
        17,
        "resolver(поднят на конфликт)",
        f"конфликтные файлы: {', '.join(brief.get('conflict_files') or []) or '—'} · "
        f"диффов в контракте: {len(diffs)} · процессов: {len(resolver.calls)}",
        by="engine",
    )
    if refused.get("outcome") != "failed":
        raise DemoError(f"отказ резолвера принят как {refused.get('outcome')}: {refused}")
    # Деревья полос при отказе остаются: в них единственная копия работы сторон.
    # Снимок берётся здесь — успешное слияние ниже их уберёт, и проверка после
    # факта мерила бы уже убранное.
    alive = {stage: tree.is_dir() for stage, tree in kept_trees.items()}
    step(
        18,
        "refused (не развёл)",
        f"{str(refused.get('kind'))} · деревья полос целы: "
        f"{sum(1 for alive_ in alive.values() if alive_)}/{len(alive)} · причина: "
        f"{str(refused.get('reason'))[:64]}…",
        by="engine",
    )

    resolved = runner.run_stage(conn, conflict_spec, conflict_deal, "join", 2, home=home, spawn=resolver)
    if resolved.get("outcome") != "done":
        raise DemoError(f"конфликт не разведён на повторе: {resolved.get('reason')}")
    resolved_sha = str(resolved.get("commit") or "")
    resolved_parents = git(repo, "rev-list", "--parents", "-n", "1", resolved_sha).split()
    # Повтор идёт в сохранённом дереве (`tree_reused`), поэтому путь берётся из
    # отчёта попытки, а не собирается из каталога: это дерево прошлой попытки.
    resolved_tree = Path(str(resolved.get("tree") or ""))
    merged_text = (resolved_tree / "shared.txt").read_text(encoding="utf-8")
    # Хвост полосы после конфликтной вливает движок: резолвер развёл пару, а
    # третью сторону доводит то же слияние — иначе её работа терялась.
    tail_text = (resolved_tree / "c.txt").read_text(encoding="utf-8") if (resolved_tree / "c.txt").exists() else ""
    lane_ancestors = {}
    for stage, lane_sha in clash_shas.items():
        lane_ancestors[stage] = (
            git_optional(repo, "merge-base", "--is-ancestor", lane_sha, resolved_sha) == ""
        )
    branches_after = git(repo, "branch", "--list", "--format=%(refname:short)").splitlines()
    step(
        19,
        "resolved(обе стороны)",
        f"коммит {resolved_sha[:8]} · родителей {len(resolved_parents) - 1} · "
        f"полос в свидетеле {sum(1 for ok in lane_ancestors.values() if ok)}/{len(lane_ancestors)} · "
        f"shared.txt: {merged_text.replace(chr(10), ' | ').strip()} · "
        f"c.txt: {tail_text.strip() or '—'} · "
        f"ветка дела {git(repo, 'rev-parse', runner.deal_branch(conflict_deal))[:8]}",
        by="engine",
    )

    return {
        "repo": str(repo),
        "base": base,
        "clean": {
            "deal": str(clean_deal["id"]),
            "lanes": lane_shas,
            "lanes_inside": lane_inside,
            "deal_branch_before": deal_branch_before,
            "merge": merge_sha,
            "parents": parents,
            "base_of_dependent": dependent_base,
            "deal_branch": git(repo, "rev-parse", runner.deal_branch(clean_deal)),
            "files": {
                name: (merged_tree / name).read_text(encoding="utf-8")
                for name in ("a.txt", "b.txt", "c.txt")
            },
            "order": list((_ended(conn, clean_deal["id"], "join") or {}).get("merge", {}).get("order") or ()),
        },
        "conflict": {
            "deal": str(conflict_deal["id"]),
            "files": list(brief.get("conflict_files") or ()),
            "diffs": diffs,
            "applied": list(brief.get("applied") or ()),
            "refused": str(refused.get("reason") or ""),
            "trees_kept": {stage: str(tree) for stage, tree in kept_trees.items()},
            "kept_at_refusal": alive,
            "merge": resolved_sha,
            "parents": resolved_parents,
            "merged_text": merged_text,
            "tail_text": tail_text,
            "lane_ancestors": lane_ancestors,
            "lanes_cleaned": [
                branch
                for branch in (runner.lane_branch(conflict_deal, stage) for stage in clash)
                if branch not in branches_after
            ],
            "contract": str(contract.get("stage") or ""),
        },
    }


def removal_scenario(
    api: Any, conn: sqlite3.Connection, home: Path, step: Callable[..., None]
) -> dict[str, Any]:
    """Нерождённый этап: план его добавил, rethink снял — и он исчез у читателей.

    Живое дело `d_e7fb271b` (18.11): лид схлопнул цепочку перестановкой и не
    записал ни одного снятия — четыре `impl_*` остались в графе нерождёнными и
    попали в итог дела наравне со сделанной работой. Прогон проходит тот же путь
    фактами журнала: ревизия 1 добавляет `extra`, закрытие отказывает (работа по
    этапу не шла), ревизия 2 снимает его записью `{id, remove: true}`, и граф
    экрана обязан сойтись с графом движка.
    """
    deals = api.deals
    journal = api.journal
    tools = _module("hermes_duty_tools", "tools.py")
    transit = _module("hermes_duty_transit", "transit.py")
    runner = _module("hermes_duty_runner", "runner.py")

    plan_spec = home / "removal.yaml"
    plan_spec.write_text(REMOVAL_SPEC, encoding="utf-8")
    spec = runner.spec_module.load_spec(plan_spec)
    created = deals.create_deal(
        conn,
        repo=DEMO_REPO,
        title="снять мёртвый этап планом",
        body="",
        spec_path=str(plan_spec),
        now=_now(),
    )
    planned_id = str(created["deal"]["id"])

    def plan_fact(kind: str, payload: dict[str, Any]) -> None:
        moment = _now()
        journal.append(
            conn,
            [
                {
                    "ts": moment,
                    "recorded_at": moment,
                    "deal_id": planned_id,
                    "kind": kind,
                    "payload": payload,
                    "source": "script",
                    "idempotency_key": deals.event_key("script", planned_id, kind, payload),
                }
            ],
        )

    def screen() -> list[str]:
        """Граф экрана: то, что рисует `GET /deal` по тому же журналу."""
        rows = api.stagefeed.snapshot(
            journal.read_events(conn, planned_id), _now(), spec_path=str(plan_spec)
        )
        return [str(row["id"]) for row in rows["stages"]]

    plan_fact(
        "plan_proposed",
        {
            "rev": 1,
            "stages": [
                {
                    "id": "extra",
                    "role": "implementer",
                    "needs": ["impl"],
                    "workspace": "none",
                    "evidence": [{"cmd": "true", "must": 0}],
                }
            ],
            "reason": "скоуп",
            "source_stage": "impl",
        },
    )
    plan_fact("plan_accepted", {"rev": 1, "by": "script"})
    with_extra = screen()
    # Закрытие нерождённый этап не принимает: этап без единой попытки — не
    # сделанная работа, и итог, перечисливший его, врал бы.
    refused = ""
    try:
        tools.close(planned_id, "сделано", "approve", "demo-close-unborn", home=home, now=_now())
    except tools.ToolsError as exc:
        refused = str(exc)
    step(
        20,
        "plan    (этап добавлен)",
        f"{planned_id} · граф экрана: {', '.join(with_extra)} · "
        f"close: {'отказ на extra' if refused else 'принят'}",
    )

    plan_fact(
        "plan_proposed",
        {
            "rev": 2,
            "stages": [{"id": "extra", "remove": True}],
            "reason": "работы для этапа не будет",
            "source_stage": "impl",
        },
    )
    plan_fact("plan_accepted", {"rev": 2, "by": "script"})
    without_extra = screen()
    events = journal.read_events(conn, planned_id)
    unborn = list(transit.unborn_stages(spec, events))
    step(
        21,
        "rethink (снятие этапа)",
        f"{planned_id} · граф экрана: {', '.join(without_extra)} · "
        f"нерождённые: {', '.join(unborn) or '—'} (extra снят)",
        by="engine",
    )
    return {
        "deal": planned_id,
        "spec": str(plan_spec),
        "with_extra": with_extra,
        "without_extra": without_extra,
        "close_refused": refused,
        "unborn": unborn,
        "graph": [stage.id for stage in transit.graph(spec, events)],
    }


def _ended(conn: sqlite3.Connection, deal_id: str, stage: str) -> Optional[dict[str, Any]]:
    """`details` принятого закрытия этапа из журнала: то, что движок записал сам."""
    journal = _module("hermes_duty_journal", "journal.py")
    found = [
        dict((event.get("payload") or {}).get("details") or {})
        for event in journal.read_events(conn, deal_id)
        if str(event.get("kind")) == "attempt_ended"
        and str((event.get("payload") or {}).get("stage")) == stage
        and str((event.get("payload") or {}).get("outcome")) == "done"
    ]
    return found[-1] if found else None


def run_demo(
    api: Any,
    *,
    home: Optional[Path] = None,
    live: Optional[Path] = None,
    keep: bool = False,
    echo: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Пройти дело по спеке на песочном корне и вернуть отчёт прогона.

    `api` — модуль `dashboard/plugin_api.py` (его же грузит плагин): прогон
    ходит через те же функции, что роуты, а не через второй набор правил.
    """
    say = echo or (lambda _line: None)
    live_root = Path(live) if live else Path.home() / ".hermes"
    sandbox = Path(home) if home else Path(tempfile.mkdtemp(prefix="duty-demo-"))
    sand = build_sandbox(sandbox, live_root)
    observed = _seed_observed(sand["board"], _now())
    board_before = _sha(sand["board"])

    env_before = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(sandbox)
    steps: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    try:
        journal = api.journal
        deals = api.deals
        ask = api.ask
        # Свой store открывает схему целиком (журнал + дела + доски): писать
        # прогон должен тем же соединением, что и мутации, иначе «таблицы нет».
        conn = deals.open_store(sandbox) or journal.open_journal(sandbox)
        assert conn is not None, "store не открылся"

        def step(n: int, what: str, detail: str, *, by: str = "script") -> None:
            steps.append({"n": n, "what": what, "detail": detail, "by": by})
            say(f"{n:>2}. {what:<26} {detail}  [{by}]")

        # 1. Дело заводится явно: карточки нет, статуса нет, есть событие created.
        created = deals.create_deal(conn, repo=DEMO_REPO, title=DEMO_TITLE,
                                    body="по спеке: impl → verify → review", assignee=None)
        deal_id = str(created["deal"]["id"])
        step(1, "create  (дело)", f"{deal_id} · status={created['deal']['status']}")

        # 2. Исполнитель берёт дело. В 0.0.3 это запись намерения, не аренда.
        deals.claim_deal(conn, deal_id, owner="implementer")
        step(2, "claim   (взял в работу)", "owner=implementer · status=running")

        # 3. Вопрос человеку — единственным каналом ask(): объект в пуле плюс
        #    событие в журнале, а не «block_kind в чужой таблице».
        asked = ask.ask(conn, {"id": deal_id, "repo": DEMO_REPO, "title": DEMO_TITLE,
                               "home": sandbox},
                        "Режем скоуп до одного виджета или чиним тристейт целиком?",
                        "Полная формулировка: в спеке три этапа; если режем — review не нужен.",
                        options=["режем", "чиним целиком"], critical=True)
        fold = journal.fold(journal.read_events(conn, deal_id))
        step(3, "ask     (вопрос в пул)", f"question #{asked.question_id} · ждёт: {fold.get('state')}/{fold.get('human_kind')}")

        # 4. Ответ приходит из пула (панель, iMessage — неважно), факт пишет sync.
        pool = sqlite3.connect(sand["pool"])
        try:
            pool.execute("UPDATE questions SET status='answered', answer='чиним целиком',"
                         " answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), answered_via='panel'"
                         " WHERE id=?", (asked.question_id,))
            pool.commit()
        finally:
            pool.close()
        synced = ask.sync_answers(conn, sandbox)
        fold = journal.fold(journal.read_events(conn, deal_id))
        step(4, "answer  (ответ получен)", f"+{synced['appended']} событие · дело снова в работе: {fold.get('state') or 'нет ожидания'}")

        # 5. Гейт: ребёнок ждёт родителя и открывается его завершением, не вручную.
        parent = deals.create_deal(conn, repo=DEMO_REPO, title="проверить контракт виджета",
                                   assignee="researcher")
        parent_id = str(parent["deal"]["id"])
        child = deals.create_deal(conn, repo=DEMO_REPO, title="прогнать тесты после фикса",
                                  parents=[parent_id])
        child_id = str(child["deal"]["id"])
        gate = journal.fold(journal.read_events(conn, child_id))
        step(5, "gate    (ждёт родителя)", f"{child_id} · {gate.get('state')} · родитель {parent_id}")
        deals.complete_deal(conn, parent_id, summary="контракт проверен, вопросов нет")
        gate = journal.fold(journal.read_events(conn, child_id))
        step(6, "gate    (открылся)", f"{child_id} · {gate.get('state') or 'ожидание снято'}")

        # 6. Комментарий и вложение: факты таймлайна, состояние не меняют.
        deals.add_comment(conn, deal_id, author="max", body="решение принято: чиним целиком")
        deals.attach_file(conn, sandbox, deal_id, filename="plan.md",
                          content=b"# plan\nimpl -> verify -> review\n", content_type="text/markdown")
        step(7, "comment (факт таймлайна)", "состояние ожидания не появилось")

        # 7. Карточки kanban — только наблюдение: своим ответом, без дел и без
        #    попыток в журнале. Своего журнала они не касаются вовсе.
        before = journal.counts(conn)
        cards = api.cards()
        after = journal.counts(conn)
        step(8, "cards   (наблюдение)", f"карточек: {cards['count']} · дел в журнале {before['deals']}→{after['deals']}")
        step(9, "cards   (повтор)", f"карточек: {api.cards()['count']} · событий не прибавилось")

        # 8. Поверхности читают журнал, а не колонки.
        now_payload = api.now()
        own_ids = [d["id"] for d in now_payload["deals"]]
        step(10, "surface /now", f"дел в срезе: {len(own_ids)} · наше {deal_id}: {'да' if deal_id in own_ids else 'НЕТ'}")

        # 9. Второй вопрос задаётся ДО закрытия: так и бывает на практике —
        #    спросили, а решение пришло позже. Ответ пока не пришёл.
        late = ask.ask(conn, {"id": deal_id, "repo": DEMO_REPO, "title": DEMO_TITLE, "home": sandbox},
                       "Оставить ли фича-флаг?", "дело закрывают, вопрос задан раньше",
                       critical=False)
        step(11, "ask     (второй вопрос)", f"question #{late.question_id} · дело снова wait_human")

        # 10. Закрытие. После него дело обязано уйти из среза.
        deals.complete_deal(conn, deal_id, summary="фикс влит, evidence-команды зелёные")
        card = api.deal(deal_id)
        now_after = [d["id"] for d in api.now()["deals"]]
        step(12, "complete", f"terminal={card.get('terminal')} · в /now: {'да' if deal_id in now_after else 'нет'}")

        # 11. Поздний ответ на второй вопрос не воскрешает закрытое дело.
        pool = sqlite3.connect(sand["pool"])
        try:
            pool.execute("UPDATE questions SET status='answered', answer='нет',"
                         " answered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), answered_via='panel'"
                         " WHERE id=?", (late.question_id,))
            pool.commit()
        finally:
            pool.close()
        ask.sync_answers(conn, sandbox)
        folded = journal.fold(journal.read_events(conn, deal_id))
        card = api.deal(deal_id)
        step(13, "late answer", f"active={folded.get('active')} · waiting={'есть' if card.get('waiting') else 'нет'}")

        ledger = [
            {"id": e["id"], "kind": e["kind"], "ts": e["ts"], "detail": _event_line(e)}
            for e in journal.read_events(conn, deal_id)
        ]

        # 12. Слияние параллельных полос (0.2.2): своё дерево и своя ветка у каждой
        #     полосы, fan-in в один коммит, конфликт — через резолвера, а не
        #     стратегией. Дела сценария свои: слияние живёт в графе дела, а не в
        #     закрытом основном.
        merge_facts = merge_scenario(api, conn, sandbox, step)

        # 13. Снятие мёртвого этапа (0.2.2): ревизия плана `{id, remove: true}` —
        #     ход лида, а не побочный эффект перестановки; до него нерождённые
        #     этапы копились в графе и попадали в итог дела как сделанные.
        removal_facts = removal_scenario(api, conn, sandbox, step)

        # ---------------------------------------------------------------- проверки
        def check(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        check("вопрос увёл дело в wait_human",
              any(e["kind"] == "question_asked" for e in journal.read_events(conn, deal_id))
              and any(s["what"].startswith("ask") and "wait_human" in s["detail"] for s in steps),
              "question_asked в журнале и состояние wait_human на шаге 3")
        check("ответ вернул дело в работу",
              any(e["kind"] == "question_answered" for e in journal.read_events(conn, deal_id)),
              "question_answered после sync_answers")
        check("гейт открыт завершением родителя",
              any(e["kind"] == "gate_opened" for e in journal.read_events(conn, child_id)),
              "gate_opened у ребёнка")
        check("закрытое дело ушло из /now", deal_id not in now_after, "срез без закрытого дела")
        check("поздний ответ не воскресил дело", folded.get("active") is False and not card.get("waiting"),
              "fold.active=False и waiting отсутствует")
        check("наблюдение карточек не пишет в журнал", after == before,
              "счётчики журнала до и после /cards совпадают")
        check("kanban.db не изменён", _sha(sand["board"]) == board_before,
              "sha256 чужой БД совпадает с исходным")

        clean, conflict = merge_facts["clean"], merge_facts["conflict"]
        check("полоса ведёт свою ветку",
              clean["deal_branch_before"] == ""
              and all(bool(sha) for sha in clean["lanes"].values())
              and len(set(clean["lanes"].values())) == len(clean["lanes"]),
              f"ветки полос {', '.join(sha[:8] for sha in clean['lanes'].values())}, "
              "ветка дела их коммитами не сдвинута")
        check("чистое слияние — merge-коммит со всеми полосами",
              len(clean["parents"]) >= 3
              and set(clean["lanes_inside"]) == set(clean["lanes"])
              and all(text for text in clean["files"].values()),
              f"коммит {clean['merge'][:8]} с двумя и более родителями, полос в свидетеле "
              f"{len(clean['lanes_inside'])}/{len(clean['lanes'])}, файлы "
              f"{', '.join(sorted(clean['files']))} в дереве")
        check("зависимый этап стоит на merge-коммите",
              clean["base_of_dependent"] == clean["merge"] and clean["deal_branch"] == clean["merge"],
              "base_of_stage(gates) = merge-коммит, ветка дела на нём же")
        check("порядок вливания — объявление", clean["order"] == ["impl_b", "impl_a", "impl_c"],
              f"needs слияния: {', '.join(clean['order']) or '—'} (не по времени готовности)")
        check("конфликт назван файлами, полосы целы",
              "merge.conflict" in conflict["refused"] and "shared.txt" in conflict["refused"]
              and len(conflict["kept_at_refusal"]) == 3
              and all(conflict["kept_at_refusal"].values()),
              "отказ merge.conflict с файлом и обоими sha; деревья всех полос сохранены")
        check("резолвер получил дифф каждой полосы",
              set(conflict["diffs"]) == {"impl_a", "impl_b", "impl_c"}
              and "строка от a" in conflict["diffs"]["impl_a"]
              and "строка от b" in conflict["diffs"]["impl_b"]
              and conflict["applied"] == ["impl_b"],
              "контракт резолвера: конфликтный файл, дифф каждой полосы и что уже влито")
        check("резолвер сохранил обе стороны",
              "строка от a" in conflict["merged_text"] and "строка от b" in conflict["merged_text"]
              and len(conflict["parents"]) >= 3,
              "в shared.txt обе строки, свидетель — merge-коммит")
        check("хвост полосы влит движком, ветки полос убраны",
              "работа полосы c" in conflict["tail_text"]
              and all(conflict["lane_ancestors"].values())
              and len(conflict["lanes_cleaned"]) == 3,
              "c.txt в слитом дереве, каждая полоса — предок свидетеля, три ветки убраны")

        check("rethink снял мёртвый этап",
              "extra" in removal_facts["with_extra"]
              and "extra" not in removal_facts["without_extra"]
              and "extra" not in removal_facts["graph"]
              and "extra" not in removal_facts["unborn"],
              f"граф экрана: {removal_facts['with_extra']} → {removal_facts['without_extra']}")
        check("закрытие не приняло нерождённый этап",
              "extra" in removal_facts["close_refused"]
              and "{id, remove: true}" in removal_facts["close_refused"],
              "отказ закрытия называет id этапа и сам ход снятия")

        failed = [c["name"] for c in checks if not c["ok"]]
        if failed:
            raise DemoError("прогон не сошёлся: " + ", ".join(failed))

        return {
            "ok": True,
            "home": str(sandbox),
            "repo": DEMO_REPO,
            "deal": deal_id,
            "parent": parent_id,
            "child": child_id,
            "observed": observed["task_id"],
            "spec": DEMO_SPEC,
            "steps": steps,
            "checks": checks,
            "ledger": ledger,
            "merge": merge_facts,
            "removal": removal_facts,
            "now": {"deals": len(now_after), "ids": now_after},
            "pool": {"questions": _questions(sand["pool"])},
            "engine_todo": list(ENGINE_TODO),
            "keep": keep,
        }
    finally:
        if env_before is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = env_before
        if not keep:
            # `--keep` — единственный способ сохранить песочницу: иначе прогон
            # не оставляет мусора ни во временном каталоге, ни в переданном.
            shutil.rmtree(sandbox, ignore_errors=True)


def _questions(pool: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{pool}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, session_key, status, kind, critical, answer FROM questions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "session_key": r[1], "status": r[2], "kind": r[3], "critical": r[4], "answer": r[5]}
        for r in rows
    ]


def _event_line(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    if event["kind"] == "question_asked":
        # Текст вопроса живёт в пуле (одна строка на вопрос), в журнале — только
        # ссылка на неё: иначе формулировка человека и журнал разошлись бы.
        return f"question #{payload.get('question_id')} · critical={payload.get('critical')}"
    if event["kind"] == "question_answered":
        return f"answer via {payload.get('answered_via') or '?'} · len={payload.get('answer_len')}"
    if event["kind"] in ("gate_opened", "gate_closed"):
        return f"{payload.get('from')} → {payload.get('to')} · parent {payload.get('parent')}"
    if event["kind"] == "created":
        return f"title: {str(payload.get('title') or '')[:48]}"
    if event["kind"] == "completed":
        return f"status → {payload.get('status') or 'done'}"
    if payload:
        return ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])
    return ""


def _demo_version() -> str:
    """Версия плагина из `plugin.yaml` — единственный источник номера.

    Литерал в подписи отчёта переживает bump и обещает «вне 0.0.4» на версии 0.0.8:
    пятая копия номера в коде рассинхронизируется молча.
    """
    meta = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        for line in meta.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return "?"


def render_demo(report: dict[str, Any]) -> str:
    """Текст прогона для терминала: спека, шаги, журнал, проверки, граница."""
    out: list[str] = []
    out.append("ПРОГОН ЗАДАЧИ ЧЕРЕЗ HERMES-DUTY")
    out.append(f"песочный корень: {report['home']}  (живые БД только читались: DDL)")
    out.append("")
    spec = report["spec"]
    out.append("СПЕКА ДЕЛА (examples/demo.yaml) — движок 0.1.0 её исполнит, 0.0.3 показывает учёт:")
    out.append(f"  duty: {spec['duty']}   repo: {spec['repo']}   base: {spec['base']}")
    for stage in spec["stages"]:
        needs = f" needs={','.join(stage['needs'])}" if stage.get("needs") else ""
        ev = f" evidence={len(stage['evidence'])} команд" if stage.get("evidence") else ""
        out.append(f"  - {stage['id']:<6} role={stage['role']:<12}{needs}{ev}")
    out.append("")
    out.append("ШАГИ")
    for step in report["steps"]:
        out.append(f"{step['n']:>2}. {step['what']:<26} {step['detail']}")
    out.append("")
    out.append(f"ЖУРНАЛ ДЕЛА {report['deal']} (append-only, состояние = fold)")
    for event in report["ledger"]:
        out.append(f"  #{event['id']:<3} {event['kind']:<17} {event['detail']}")
    out.append("")
    out.append(f"ПУЛ ВОПРОСОВ: {len(report['pool']['questions'])} строк")
    for q in report["pool"]["questions"]:
        out.append(
            f"  #{q['id']} {q['session_key']:<18} status={q['status']:<9} kind={q['kind']:<9}"
            f" critical={q['critical']} answer={q['answer']!r}"
        )
    out.append("")
    out.append("ПРОВЕРКИ")
    for check in report["checks"]:
        out.append(f"  [{'OK' if check['ok'] else 'FAIL'}] {check['name']}: {check['detail']}")
    merge = report.get("merge") or {}
    if merge:
        clean, conflict = merge["clean"], merge["conflict"]
        out.append("")
        out.append(f"СЛИЯНИЕ ПОЛОС (настоящий git в песочнице: {merge['repo']})")
        out.append(f"  база полос {merge['base'][:12]}")
        out.append(
            f"  чистое: {clean['deal']} · полосы "
            + ", ".join(f"{stage} {sha[:8]}" for stage, sha in clean["lanes"].items())
        )
        out.append(
            f"          merge {clean['merge'][:12]} · родителей {len(clean['parents']) - 1} · "
            f"порядок {', '.join(clean['order'])} · база gates {clean['base_of_dependent'][:12]}"
        )
        out.append(f"  конфликт: {conflict['deal']} · файлы {', '.join(conflict['files'])}")
        out.append(f"          отказ: {conflict['refused'][:96]}")
        out.append(
            f"          merge {conflict['merge'][:12]} · родителей {len(conflict['parents']) - 1} · "
            f"shared.txt: {conflict['merged_text'].strip().replace(chr(10), ' | ')}"
        )
    removal = report.get("removal") or {}
    if removal:
        out.append("")
        out.append(f"СНЯТИЕ МЁРТВОГО ЭТАПА (план: {removal['deal']})")
        out.append(f"  ревизия 1: граф экрана {', '.join(removal['with_extra'])}")
        out.append(f"             close: {removal['close_refused'][:110]}")
        out.append(
            f"  ревизия 2: граф экрана {', '.join(removal['without_extra'])} · "
            f"нерождённые: {', '.join(removal['unborn']) or '—'} (extra снят)"
        )
    out.append("")
    out.append(f"ЧЕГО ЕЩЁ НЕТ (вне {_demo_version()}):")
    for item in report["engine_todo"]:
        out.append(f"  · {item}")
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    """`python dashboard/demo.py` — прогон без CLI (для ручной проверки)."""
    import argparse

    parser = argparse.ArgumentParser(prog="duty demo", description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--home", help="песочный корень (по умолчанию временный)")
    parser.add_argument("--live", help="живой корень Hermes, только для DDL")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep", action="store_true", help="не удалять песочницу")
    args = parser.parse_args(argv)
    here = Path(__file__).resolve().parent
    spec_ = importlib.util.spec_from_file_location("hermes_duty_plugin_api", here / "plugin_api.py")
    assert spec_ is not None and spec_.loader is not None
    module = importlib.util.module_from_spec(spec_)
    sys.modules["hermes_duty_plugin_api"] = module
    spec_.loader.exec_module(module)
    report = run_demo(module, home=Path(args.home) if args.home else None,
                      live=Path(args.live) if args.live else None,
                      keep=args.keep, echo=None if args.json else print)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_demo(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    raise SystemExit(main())
