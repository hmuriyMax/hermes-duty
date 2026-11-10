"""Один шаг движка: лизинг, дерево попытки, контракт файлом, спавн, телеметрия.

Модуль **действует**: поднимает процесс роли, держит лизинг этапа и пишет факты в
журнал. Поэтому решений здесь нет — что запускать и с каким номером попытки,
решает `dashboard/transit.py` (`Decision.run(stage, attempt)`), что означает
спека — `dashboard/spec.py` (валидатор отработал до процесса). Здесь только
исполнение одного шага и запись того, что произошло.

Что модуль делает по шагам (`run_stage`):

1. **Лизинг** этапа в своей `duty/duty.db` (таблица `leases`). Взятие атомарно
   (`INSERT … ON CONFLICT … WHERE expires_at <= now`): два процесса не поведут
   один этап — второй получит `False` и не сделает ничего. TTL — из
   `stage.budget.minutes` (по умолчанию 30 мин), продление — по heartbeat'у
   попытки не реже TTL/3.
2. **Дерево попытки**. `worktree` — `git worktree add --detach <каталог
   попытки>/tree <база этапа>`; `dir` — каталог дела из `repo` спеки; `none` —
   дерева нет (этапы-гейты). База этапа — не `spec.base`: у этапа без `needs`
   это `spec.base`, у следующего — то, что произвёл предшественник
   (`base_of_stage`). Иначе второй этап получал бы чистое дерево и не видел
   работу первого. Каталог попытки лежит под
   `<home>/kanban/boards/duty/workspaces/<deal>-<stage>-<attempt>` — это
   разрешённый корень `role-access` (`docs/PROBES.md`, проба 3): иначе read-only
   роль не прочитает ни дерево, ни отчёт. Успешная попытка дерево оставляет,
   упавшая — удаляет (`git worktree remove --force`), путь и причина уходят в
   событие.
3. **Входной контракт файлом** `<каталог попытки>/.duty/in-<stage>-<attempt>.json`:
   дело, этап, попытка, роль, каталог, цель, границы, отчёты предшественников,
   что доказать, куда задавать вопросы, куда писать отчёт. Пишется до спавна и
   целиком уходит в событие `attempt_started` — консоль показывает вход, а не
   догадывается о нём.
4. **Спавн** `hermes chat -p <role> --query-file <контракт> --format stream-json
   --oneshot` с cwd дерева этапа. Профиль роли — из спеки, права не эскалируются.
   Бюджет: `minutes` → жёсткий таймаут (SIGTERM, затем SIGKILL через 10 с),
   `tokens` → превышение по `usage` из потока останавливает попытку.
   У этапа `kind: shell` (`docs/SPEC.md`) процесса роли нет вовсе: вместо спавна
   запускается `sh -c <cmd>` в том же дереве, а договор результата — код возврата
   `must` и объявленные `artifacts`, а не `result.json` (`run_shell_stage`).
5. **Телеметрия**: каждый `tool_use`/`tool_result` потока — событие
   `attempt_progress` (фаза, имя инструмента, длительность). Журнал отвечает
   «что делается прямо сейчас» без ps и хвостов логов.
6. **Смерть попытки** — три источника, без догадок по тексту ошибки: разобранный
   `result.json`; его отсутствие; код выхода процесса. Таймаут — наш собственный
   факт и потому проверяется первым (`failed(infra)`), результат до таймаута всё
   равно попадает в `details`.
7. **Проверка пакета** (`dashboard/evidence.py`): заявленное «готово» сверяется с
   деревом попытки — хеши файлов, резолв коммита, коды объявленных команд и их
   свежесть, бюджет. Только принятый пакет закрывает попытку `done`; иначе
   `failed(contract)` или `failed(capability)` с первыми отказами в `reason`.
   Принятую работу движок **фиксирует коммитом в дереве попытки**
   (`commit_tree`): исполнитель вправе отчитаться артефактами без коммита, но
   тогда следующему этапу не от чего отвести дерево. Sha уходит в
   `attempt_ended.payload.commit`.
8. **Вопрос из попытки**: `question` в `result.json` → `ask()` (единственный
   канал) и дело в `wait_human`. Продолжение — резюмом сессии (`RESUME-OK`,
   `docs/PROBES.md`): id сессии из события `result` хранится в
   `attempt_ended.details.session_id`, следующая попытка идёт с `--resume`.
   Выбор зафиксирован в `docs/RUNBOOK.md`.
9. **Политика ретраев** (`dashboard/failures.py`): класс отказа уже записан в
   `attempt_ended.kind`, а политика говорит, повторяется ли он. Если нет —
   `retries_exhausted`; если за дело набралось `max_rounds_per_deal` упавших
   попыток — `too_many_failures`. В обоих случаях дело **паркуется**: `ask()` с
   властным вопросом «продолжать ли» и `blocked` с `block_kind='parked'`, где
   едут класс, этап, число попыток и лимит. Отдельного вида события для парковки
   нет: набор журнала закрыт, а парковка ждёт человека так же, как любой вопрос.

Чего модуль не делает: не считает состояние дела (это `journal.fold`), не
назначает попытки (это `transit`), не запускает команды из `evidence` (их уже
выполнил исполнитель — гейт только сверяет заявленное с деревом), не ходит в
продовые системы и не пишет ни в чужую БД, ни в `kanban.db`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# Модули плагина грузятся по пути и живут в `sys.modules` под общими именами:
# вторая загрузка означала бы вторую копию схемы и констант в одном процессе.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
DEALS_MODULE_NAME = "hermes_duty_deals"
ASK_MODULE_NAME = "hermes_duty_ask"
SPEC_MODULE_NAME = "hermes_duty_spec"
TRANSIT_MODULE_NAME = "hermes_duty_transit"
EVIDENCE_MODULE_NAME = "hermes_duty_evidence"
ROUTING_MODULE_NAME = "hermes_duty_routing"
FAILURES_MODULE_NAME = "hermes_duty_failures"
BUDGET_MODULE_NAME = "hermes_duty_budget"
PUBLISH_MODULE_NAME = "hermes_duty_publish"


def _load(name: str, filename: str) -> Any:
    """`dashboard/<filename>` — тот же модуль, что грузит остальной плагин."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise ImportError(f"module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


journal = _load(JOURNAL_MODULE_NAME, "journal.py")
deals = _load(DEALS_MODULE_NAME, "deals.py")
ask = _load(ASK_MODULE_NAME, "ask.py")
spec_module = _load(SPEC_MODULE_NAME, "spec.py")
evidence_module = _load(EVIDENCE_MODULE_NAME, "evidence.py")
routing = _load(ROUTING_MODULE_NAME, "routing.py")
# Классы отказа и политика ретраев — одно место на плагин (`dashboard/failures.py`):
# движок берёт класс оттуда и не выводит его из текста ошибки.
failures = _load(FAILURES_MODULE_NAME, "failures.py")
# Расход дела и потолок `budget` (0.0.5): движок сверяет сумму по журналу с
# потолком спеки до спавна, а числа для причины берёт оттуда же — второй счётчик
# в памяти процесса не пережил бы рестарт.
budget = _load(BUDGET_MODULE_NAME, "budget.py")
# Публикация (0.0.5): одобрение человеком до спавна и сверка заявки с origin после
# отчёта. Правило живёт отдельно от исполнения — движок только спрашивает его и
# записывает факт (`dashboard/publish.py`).
publish = _load(PUBLISH_MODULE_NAME, "publish.py")
# Граф дела (`dashboard/transit.py`): принятый план живёт фактами журнала, и
# попытке нужен этап из графа, а не из файла. Обратной загрузки нет — `transit`
# не знает про `runner`, поэтому цикл модулей не замыкается.
transit = _load(TRANSIT_MODULE_NAME, "transit.py")

# Источник событий движка в журнале: `engine`, а не `cli`/`api`/`ingest` — по нему
# видно, что факт записал движок, а не человек и не проход по чужой доске.
ENGINE_SOURCE = "engine"

# TTL лизинга по умолчанию: `budget.minutes` в спеке не задан — 30 минут
# (`docs/PROBES.md`: тривиальная попытка 3–6 с, значит запас огромный).
DEFAULT_TTL_MIN = 30

# Продление лизинга — не реже TTL/3: два пропущенных heartbeat'а и лизинг ещё жив.
RENEW_DIVISOR = 3

# Сколько ждать процесс после SIGTERM, прежде чем убить его SIGKILL.
KILL_GRACE_SEC = 10

# Сколько последних строк stderr попытки попадает в событие: хвост ошибки нужен,
# а весь лог — нет (журнал не место для логов).
STDERR_TAIL = 20

# Виды этапа (`dashboard/spec.py`): `agent` исполняет профиль роли, `shell` —
# команда, `gate` — никто: этап спрашивает человека. Здесь — только исполнение:
# что запускать, решила спека.
STAGE_KIND_AGENT = "agent"
STAGE_KIND_SHELL = "shell"
STAGE_KIND_GATE = "gate"

# Источник событий shell-попытки в журнале: по нему видно, что этап исполнила
# команда, а не процесс профиля. `engine` остаётся источником решений движка
# (лизинг, парковка, закрытие) — они по-прежнему его.
SHELL_SOURCE = "shell"

# Чем запускается команда этапа: `sh -c <cmd>` — одна строка спеки как процесс.
SHELL_BIN = "/bin/sh"

# Хвост stdout/stderr shell-попытки в событии; полные логи лежат файлами рядом
# с отчётом — журнал отвечает «чем кончилось», а не заменяет лог.
SHELL_TAIL = 20
SHELL_STDOUT_LOG = "stdout.log"
SHELL_STDERR_LOG = "stderr.log"

# Переменные окружения, которые shell-этап не наследует: они называют роль и её
# доступы, а команде этапа ни роль, ни токены не нужны (она не действует вовне).
SHELL_ENV_ROLE_KEYS = ("HERMES_PROFILE", "HERMES_ROLE", "HERMES_SESSION_TOKEN", "__HERMES_SESSION_TOKEN__")

# Исходы попытки в журнале (`attempt_ended.outcome`): различаются полем, а не
# вторым видом события (`docs/JOURNAL.md`).
OUTCOME_DONE = failures.OUTCOME_DONE
OUTCOME_FAILED = failures.OUTCOME_FAILED

# Классы отказа попытки (`attempt_ended.kind`): `infra` — упал сам процесс,
# `contract` — исполнитель не отчитался, `capability` — упёрся в бюджет. Словарь
# один на плагин (`dashboard/failures.py`); здесь — только имена для читаемости.
FAIL_INFRA = failures.INFRA
FAIL_CONTRACT = failures.CONTRACT
FAIL_CAPABILITY = failures.CAPABILITY

# Статусы `result.json`, которые считаются успехом: `status` не назван — тоже
# успех (процесс дошёл до конца и отчитался).
OK_STATUSES = failures.OK_STATUSES

# Статусы `result.json`, которые паркуют дело на человеке и на отказе контракта.
BLOCKED_STATUSES = failures.BLOCKED_STATUSES
REFUSED_STATUSES = failures.REFUSED_STATUSES

# Решение `transit`: запускать попытку. Остальные значения — не наш шаг.
DECISION_RUN = "run"

# Каталог попытки внутри дерева Hermes: разрешённый корень `role-access`
# (`docs/PROBES.md`, проба 3). Менять его на `mktemp`/`~` нельзя — read-only роль
# тогда не прочитает ни дерево, ни отчёт.
#
# С 0.0.6 доск нет, поэтому каталог попытки больше не делится по слагу: он лежит
# прямо в `duty/workspaces/`. Каталог `duty` — не доска и никакого отношения к
# kanban не имеет: это единственное место под разрешённым корнем ролей, где duty
# вправе держать деревья попыток, и имя говорит, чьи они.
WORKSPACES_SUBDIR = ("kanban", "boards", "duty")

# Служебный каталог попытки рядом с деревом: контракт и отчёт.
DUTY_SUBDIR = ".duty"

# Подкаталог рабочего каталога попытки, где работает отвечающая роль: вопрос
# этапа адресован роли (`ask.allowed`), и её ответ — отдельный процесс со своим
# контрактом и отчётом. Дерева и лизинга у него нет: роль ничего не сдаёт, она
# отвечает на вопрос (`dashboard/runner.py`, `ask_role`).
ASK_ROLE_SUBDIR = "ask"

# Сколько ждать `git worktree add`/`remove`: checkout большого репозитория — не
# мгновенный, но и не бесконечный.
WORKTREE_TIMEOUT_SEC = 300

# Автор коммита движка: попытку фиксирует движок, а не человек, и в истории это
# должно быть видно. Настройки машины (`user.email`, подпись) на факт не влияют:
# без этого коммит движка падал бы на чужом окружении.
ENGINE_COMMIT_NAME = "duty"
ENGINE_COMMIT_EMAIL = "duty@local"

# Шаг опроса потока: короткий, чтобы heartbeat лизинга и таймаут срабатывали
# вовремя, но без busy-loop.
POLL_SEC = 0.2

# Чем запускается попытка: тот же `hermes`, что и у человека в PATH.
HERMES_BIN = shutil.which("hermes") or "hermes"


class RunnerError(RuntimeError):
    """Шаг не может быть выполнен по причине ниже движка: спека, каталог, лизинг.

    Отдельный тип, потому что «этап не запускался» и «попытка упала» — разные
    факты: первый не пишет событий вовсе, второй обязан быть в журнале.
    """


class BaseError(RunnerError):
    """База этапа не выводится из журнала: предшественники разошлись.

    Это отказ **контракта**, а не инфраструктуры: два разных коммита
    предшественников без общего потомка означают, что дерево, отведённое от
    любого из них, молча теряло бы работу другого. Выбирать «первый попавшийся»
    здесь нельзя — нужен человек (или merge), и `run_stage` пишет
    `failed(contract)` с этой причиной.
    """


# ------------------------------------------------------------------- лизинг


def ttl_sec(stage: Any) -> int:
    """TTL лизинга этапа: `budget.minutes` или 30 минут по умолчанию."""
    budget = getattr(stage, "budget", None)
    minutes = getattr(budget, "minutes", None) if budget is not None else None
    return int(minutes or DEFAULT_TTL_MIN) * 60


def _take(
    conn: sqlite3.Connection, deal_id: str, stage: str, attempt: int, pid: int, ttl: int, now: int
) -> int:
    """Строка лизинга атомарно: 1 — лизинг наш, 0 — этап уже за живым процессом.

    Без собственной транзакции: вызывающий решает, чем этот факт закрывается
    (в шаге движка — транзакцией вызывающего, в `lease()` — своей).
    """
    cursor = conn.execute(
        "INSERT INTO leases (deal_id, stage, attempt, pid, taken_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(deal_id, stage) DO UPDATE SET "
        "attempt = excluded.attempt, pid = excluded.pid, taken_at = excluded.taken_at, "
        "expires_at = excluded.expires_at "
        "WHERE leases.expires_at <= ?",
        (deal_id, stage, attempt, pid, now, now + ttl, now),
    )
    return int(cursor.rowcount)


def lease(
    conn: sqlite3.Connection,
    *,
    deal: Any,
    stage: str,
    attempt: int,
    pid: int,
    ttl: int,
    now: Optional[int] = None,
) -> bool:
    """Взять лизинг этапа; `False` — этап ведёт кто-то живой.

    Взятие — одна транзакция и одно условие в SQL: срок истёк или строки нет.
    Проверка «прочитал, потом записал» здесь не годится — между чтением и
    записью второй процесс успел бы взять тот же этап.
    """
    at = _now_sec(now)
    with conn:
        taken = _take(conn, str(deal["id"]), stage, int(attempt), int(pid), int(ttl), at)
    return taken == 1


def _hand_over(
    conn: sqlite3.Connection,
    deal_id: str,
    stage: str,
    from_pid: int,
    pid: int,
    attempt: int,
    ttl: int,
    now: int,
) -> int:
    """Передать лизинг процессу попытки: pid и срок; 1 — лизинг был наш."""
    cursor = conn.execute(
        "UPDATE leases SET pid = ?, attempt = ?, expires_at = ? "
        "WHERE deal_id = ? AND stage = ? AND pid = ?",
        (int(pid), int(attempt), now + int(ttl), deal_id, stage, int(from_pid)),
    )
    return int(cursor.rowcount)


def hand_over(
    conn: sqlite3.Connection,
    *,
    deal: Any,
    stage: str,
    from_pid: int,
    pid: int,
    attempt: int,
    ttl: int,
    now: Optional[int] = None,
) -> bool:
    """Лизинг, взятый движком, становится лизингом попытки (её pid и срок).

    Взять лизинг до спавна нельзя иначе: pid появляется только вместе с
    процессом, а взаимное исключение нужно **до** него. Две строки на это не
    нужны — pid просто переезжает к тому, кто теперь ведёт этап.
    """
    at = _now_sec(now)
    with conn:
        moved = _hand_over(conn, str(deal["id"]), stage, int(from_pid), int(pid), int(attempt), int(ttl), at)
    return moved == 1


def renew(
    conn: sqlite3.Connection, *, deal: Any, stage: str, pid: int, ttl: int, now: Optional[int] = None
) -> bool:
    """Продлить лизинг по heartbeat'у; `False` — этап уже не наш (не продлеваем)."""
    at = _now_sec(now)
    with conn:
        cursor = conn.execute(
            "UPDATE leases SET expires_at = ? WHERE deal_id = ? AND stage = ? AND pid = ?",
            (at + int(ttl), str(deal["id"]), stage, int(pid)),
        )
    return int(cursor.rowcount) == 1


def release(conn: sqlite3.Connection, *, deal: Any, stage: str, pid: int) -> bool:
    """Отпустить лизинг своего этапа: строка удаляется, а не помечается.

    Помеченная строка означала бы «срок ещё не истёк, но этап свободен», и
    следующее взятие упёрлось бы в неё же. История лизинга — это события
    попытки, а не строка в таблице.
    """
    with conn:
        cursor = conn.execute(
            "DELETE FROM leases WHERE deal_id = ? AND stage = ? AND pid = ?",
            (str(deal["id"]), stage, int(pid)),
        )
    return int(cursor.rowcount) == 1


def lease_of(conn: sqlite3.Connection, deal: Any, stage: str) -> Optional[dict[str, Any]]:
    """Строка лизинга этапа или `None` — факт для консоли и тестов."""
    row = conn.execute(
        "SELECT deal_id, stage, attempt, pid, taken_at, expires_at FROM leases "
        "WHERE deal_id = ? AND stage = ?",
        (str(deal["id"]), stage),
    ).fetchone()
    return None if row is None else dict(row)


# --------------------------------------------------------------------- пути


def attempt_dir(home: Path, deal: Any, stage: str, attempt: int) -> Path:
    """Каталог попытки: `<home>/kanban/boards/duty/workspaces/<deal>-<stage>-<n>`.

    Под `kanban/boards/**` — потому что это разрешённый корень чтения read-only
    роли: каталог в `mktemp` или профильном `cache/scratch` роль не увидит, и
    ревью окажется пустым не из-за отсутствия находок, а из-за отсутствия
    доступа. Слага доски в пути больше нет (0.0.6): дело адресуется своим id.
    """
    base = Path(home)
    for part in WORKSPACES_SUBDIR:
        base = base / part
    return base / "workspaces" / f"{deal['id']}-{stage}-{int(attempt)}"


def contract_path(workdir: Path, stage: str, attempt: int) -> Path:
    """Файл входного контракта попытки."""
    return Path(workdir) / DUTY_SUBDIR / f"in-{stage}-{int(attempt)}.json"


def result_path(workdir: Path) -> Path:
    """Файл отчёта попытки — то, что контракт называет `report_to`."""
    return Path(workdir) / DUTY_SUBDIR / "result.json"


def answer_path(workdir: Path, stage: str, attempt: int) -> Path:
    """Файл запроса для резюма: ответ человека — единственный вход продолжения."""
    return Path(workdir) / DUTY_SUBDIR / f"answer-{stage}-{int(attempt)}.txt"


def _git(args: Iterable[str], cwd: Path) -> tuple[int, str]:
    """`git` в каталоге `cwd`: код возврата и хвост stderr как причина отказа."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=WORKTREE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, " ".join(str(exc).split())
    return done.returncode, " ".join((done.stderr or done.stdout or "").split())


def _git_stdout(args: Iterable[str], cwd: Path) -> tuple[int, str]:
    """`git` в каталоге `cwd` с полным stdout — для команд, чей ответ и есть stdout.

    `_git` собирает stderr одной строкой для причины отказа, а `ls-remote` и
    `show --stat` отвечают **в stdout**: сверка публикации по «хвосту stderr»
    не увидела бы ни одной ссылки.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=WORKTREE_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, " ".join(str(exc).split())
    if done.returncode != 0:
        return done.returncode, " ".join((done.stderr or done.stdout or "").split())
    return 0, done.stdout or ""


# --------------------------------------------------------------------- дерево


def prepare_tree(
    spec: Any, stage: Any, workdir: Path, *, base: Optional[str] = None
) -> tuple[Optional[Path], Path]:
    """Дерево попытки по `workspace` этапа: (дерево или None, cwd процесса).

    `worktree` — своё дерево от базы этапа (`base`, по умолчанию `spec.base`:
    валидатор уже проверил, что rev существует); `dir` — каталог дела из `repo`
    спеки, без worktree; `none` — дерева нет, cwd — сам каталог попытки.
    """
    workdir = Path(workdir)
    if stage.workspace == "worktree":
        repo = Path(str(spec.repo or "")).expanduser()
        tree = workdir / "tree"
        rev = str(base if base is not None else spec.base or "")
        # `--detach`: база — это коммит, а не ветка. Ветка может быть уже занята
        # другим деревом (главный checkout, дерево прошлой попытки), и тогда
        # `git worktree add <путь> <ветка>` отказывает «is already used by
        # worktree». Попытка отводится от коммита, поэтому detach — верная модель:
        # дерево попытки ветку не занимает, и две попытки не спорят за имя.
        # Ссылку на работу даёт ветка ДЕЛА (`anchor_commit`): detached HEAD сам по
        # себе имени не даёт, и без этого ветку коммит жил бы до первого `git gc`.
        code, reason = _git(["worktree", "add", "--detach", str(tree), rev], repo)
        if code != 0:
            raise RunnerError(f"git worktree add {tree} от {rev!r} в {repo}: {reason or code}")
        return tree, tree
    if stage.workspace == "dir":
        if spec.repo is None:
            raise RunnerError(
                f"этап {stage.id!r} просит workspace: dir, а repo в спеке не задан — каталога нет"
            )
        repo = Path(str(spec.repo)).expanduser()
        if not repo.is_dir():
            raise RunnerError(f"каталог дела {repo} не найден (workspace: dir этапа {stage.id!r})")
        return None, repo
    return None, workdir


# ---------------------------------------------------------------- база этапа


def _successful_of(needs: Iterable[str], events: Iterable[Any]) -> dict[str, tuple[int, dict[str, Any]]]:
    """Последняя успешная попытка каждого предшественника: этап → (ts, payload).

    Успех — исход из `OK_STATUSES` без класса отказа: тот же набор, по которому
    `transit` считает этап `done`, — иначе база этапа и «этап пройден»
    разошлись бы. Берётся последняя по журналу (`ts`, `id`): возврат на
    доработку оставляет несколько успешных попыток, и годится последняя.
    """
    wanted = set(needs)
    found: dict[str, tuple[int, dict[str, Any]]] = {}
    for event in events or ():
        if not isinstance(event, dict) or str(event.get("kind") or "") != "attempt_ended":
            continue
        payload = journal.payload_of(event)
        stage_id = str(payload.get("stage") or "")
        if stage_id not in wanted:
            continue
        if str(payload.get("outcome") or "").strip().lower() not in OK_STATUSES:
            continue
        if str(payload.get("kind") or "").strip():
            continue
        found[stage_id] = (_int(event.get("ts")) or 0, payload)
    return found


def attempt_commit(payload: dict[str, Any]) -> str:
    """Что произвела попытка: `payload.commit`, коммит из отчёта, HEAD её дерева.

    Первый источник — движок (`commit_tree`): после успешной попытки работа
    зафиксирована коммитом в дереве, и sha лежит в событии. Второй — отчёт
    исполнителя: он вправе закоммитить сам, и гейт уже проверил, что коммит
    резолвится и не был предком базы. Третий — сохранённое дерево попытки без
    коммита (события до этой правки): его HEAD и есть то, что видел этап.
    Пустая строка — попытка не произвела ничего, от чего можно отвести дерево.
    """
    commit = payload.get("commit")
    if isinstance(commit, str) and commit.strip():
        return commit.strip()
    result = payload.get("result")
    if isinstance(result, dict):
        commit = result.get("commit")
        if isinstance(commit, str) and commit.strip():
            return commit.strip()
    details = payload.get("details")
    tree = details.get("tree") if isinstance(details, dict) else None
    if not isinstance(tree, str) or not tree:
        return ""
    code, out = _git(["rev-parse", "--verify", "HEAD^{commit}"], Path(tree))
    if code != 0 or not out:
        return ""
    return out.split()[0]


def _sha(repo: Path, rev: str) -> str:
    """Полный sha ревизии в репозитории; пустая строка — ревизия не резолвится."""
    if not rev:
        return ""
    code, out = _git(["rev-parse", "--verify", f"{rev}^{{commit}}"], repo)
    if code != 0 or not out:
        return ""
    return out.split()[0]


def _ancestor(repo: Path, older: str, newer: str) -> Optional[bool]:
    """`older` — предок или равен `newer`; `None` — git не ответил."""
    code, _ = _git(["merge-base", "--is-ancestor", older, newer], repo)
    if code == 0:
        return True
    if code == 1:
        return False
    return None


def base_of_stage(spec: Any, stage: Any, events: Iterable[Any]) -> str:
    """Коммит, от которого отводится дерево этапа — база **этапа**, не дела.

    У этапа без `needs` база — `spec.base`. У следующего — то, что произвёл
    предшественник: коммит его последней успешной попытки (`attempt_commit`),
    а если произвести было нечего — `spec.base`. Иначе второй этап получал бы
    чистое дерево и не видел работу первого: цепочка impl → verify → review
    была бы неисполнима в принципе (дефект, найденный прогоном E2E `d_a2a26b55`).

    Несколько предшественников: решает не время, а родство коммитов. Берётся
    самый поздний по журналу, и каждый следующий годится, только если он уже
    внутри выбранного (или наоборот — тогда он становится выбранным). Два
    расходящихся коммита без общего потомка — `BaseError`, то есть
    `failed(contract)` с названными коммитами: дерево от одного из них молча
    теряло бы работу другого, и «взяли первый попавшийся» здесь недопустимо.

    База этапа `dir`/`none` базой не становится: у первого каталог дела общий (и
    коммит движка туда не пишется), у второго работы нет вовсе — делиться нечем,
    и база остаётся `spec.base`.
    """
    needs = tuple(str(need) for need in (getattr(stage, "needs", None) or ()))
    if not needs:
        return str(getattr(spec, "base", "") or "")
    fallback = str(getattr(spec, "base", "") or "")
    found = _successful_of(needs, events)
    by_id = {item.id: item for item in getattr(spec, "stages", ())}
    rows = []
    for need in needs:
        hit = found.get(need)
        commit = attempt_commit(hit[1]) if hit is not None else ""
        if not commit:
            need_stage = by_id.get(need)
            if need_stage is not None and getattr(need_stage, "kind", "") == spec_module.GATE_KIND:
                # Гейт дерева не производит: он спрашивает человека. Для дерева он
                # прозрачен — базой следующего этапа служит то, что гейт одобрял, а
                # не `spec.base`. Иначе `publish` после гейта получил бы чистое
                # дерево и опубликовал бы пустоту.
                commit = base_of_stage(spec, need_stage, events)
        rows.append((hit[0] if hit is not None else 0, need, commit or fallback))
    # Поздний предшественник первый: при равном родстве выбранным остаётся он.
    rows.sort(key=lambda row: row[0], reverse=True)
    repo_text = str(getattr(spec, "repo", "") or "").strip()
    repo = Path(repo_text).expanduser() if repo_text else None
    resolved = []
    for ts, need, rev in rows:
        if not rev:
            # Предшественник не произвёл ни коммита, ни базы (цепочка гейтов без
            # репозитория): отводить дерево не от чего, и это не отказ.
            continue
        if repo is None or not repo.is_dir():
            raise BaseError(
                f"этап {stage.id!r}: база {rev!r} предшественника {need!r} названа, "
                f"а репозитория {repo_text or '—'} нет — дерево этапа не от чего отвести"
            )
        sha = _sha(repo, rev)
        if not sha:
            raise BaseError(
                f"этап {stage.id!r}: ревизия {rev!r} предшественника {need!r} "
                f"не резолвится в {repo} — дерево этапа не от чего отвести"
            )
        resolved.append((ts, need, sha))
    if not resolved or repo is None:
        return fallback
    chosen = resolved[0]
    for ts, need, sha in resolved[1:]:
        if sha == chosen[2]:
            continue
        if _ancestor(repo, sha, chosen[2]) is True:
            continue  # работа этого предшественника уже внутри выбранного коммита
        if _ancestor(repo, chosen[2], sha) is True:
            chosen = (ts, need, sha)
            continue
        raise BaseError(
            f"этап {stage.id!r}: предшественники разошлись — {chosen[1]!r} произвёл "
            f"{chosen[2][:12]}…, {need!r} произвёл {sha[:12]}…, общего потомка нет: "
            "дерево этапа, отведённое от одного из них, потеряло бы работу другого"
        )
    return chosen[2]


def commit_tree(tree: Path, deal: Any, stage_id: str, attempt: int) -> tuple[str, str]:
    """Зафиксировать работу попытки коммитом в её дереве: (sha, причина отказа).

    Исполнитель по контракту вправе отчитаться артефактами без коммита (так и
    сделал `impl` в E2E `d_a2a26b55`), но тогда следующему этапу не от чего
    отвести дерево. Поэтому факт фиксирует движок — **внутри дерева попытки**:
    дерево в detached HEAD, ветку это не двигает, чужие правки не трогаются, а
    полученный sha уходит в `attempt_ended.payload.commit`.

    Пустой коммит не ошибка: исполнитель мог закоммитить сам, и тогда `HEAD` —
    его работа. Отказ коммита отличается не кодом возврата, а деревом: если
    после неудачи `git status` чист, фиксировать больше нечего (исполнитель уже
    сделал это), а если грязно — работа не зафиксирована и попытке нельзя
    отдавать `done`: следующий этап повторил бы дефект W14.
    """
    tree = Path(tree)
    code, reason = _git(["add", "-A"], tree)
    if code != 0:
        return "", f"git add -A отказал: {reason or code}"
    code, reason = _git(
        [
            # `-c` перед подкомандой: коммит движка не зависит от `user.email`
            # и от подписи, настроенных на машине.
            "-c", f"user.name={ENGINE_COMMIT_NAME}",
            "-c", f"user.email={ENGINE_COMMIT_EMAIL}",
            "-c", "commit.gpgsign=false",
            "commit", "--no-verify",
            "-m", f"duty: {deal['id']} {stage_id} attempt {int(attempt)}",
        ],
        tree,
    )
    if code != 0:
        dirty_code, dirty = _git(["status", "--porcelain"], tree)
        if dirty_code != 0 or dirty.strip():
            return "", f"git commit отказал: {reason or code}"
    code, out = _git(["rev-parse", "--verify", "HEAD^{commit}"], tree)
    if code != 0 or not out:
        return "", f"git rev-parse HEAD отказал: {out or code}"
    return out.split()[0], ""


DEAL_BRANCH_PREFIX = "duty/"


def deal_branch(deal: Any) -> str:
    """Ветка дела: имя, за которое держится вся работа дела.

    Одна ветка на дело, а не на этап: этапы дела идут цепочкой (`needs`), поэтому
    их коммиты — одна линия. Имя не совпадает с ветками человека (`main`,
    `release/*`), так что двигать её движку можно без спроса.
    """
    return f"{DEAL_BRANCH_PREFIX}{deal['id']}"


def anchor_commit(tree: Path, deal: Any, commit: str) -> tuple[str, str]:
    """Поставить ветку дела на коммит: `(ветка, причина отказа)`.

    Дерево попытки остаётся в detached HEAD — ветку оно не занимает, и две
    попытки не спорят за неё. Но сам коммит получает ссылку: без неё работа дела
    живёт до первого `git gc`, и ровно это случилось с `d_9ea4d348`, где коммиты
    `impl_core`/`impl_wire` пришлось закреплять руками (`git branch duty/... `).
    Отказ закрепления не отменяет коммит: работа уже зафиксирована, поэтому
    причина уходит в событие, а не в вердикт попытки.
    """
    if not commit:
        return "", ""
    branch = deal_branch(deal)
    code, reason = _git(["branch", "-f", branch, commit], Path(tree))
    if code != 0:
        return branch, f"git branch -f {branch} отказал: {reason or code}"
    return branch, ""


def tree_dirty(tree: Path) -> tuple[bool, int]:
    """Есть ли в дереве попытки незакоммиченная работа: `(грязно, файлов)`.

    Нужна там, где решается судьба дерева упавшей попытки: удалять можно только
    пустое. `git status --porcelain` не читается — считаем «грязно»: не сумев
    подтвердить пустоту, дерево сохраняем, потому что в нём может лежать
    единственная копия работы.
    """
    code, out = _git_stdout(["status", "--porcelain"], Path(tree))
    if code != 0:
        return True, -1
    lines = [line for line in out.splitlines() if line.strip()]
    return bool(lines), len(lines)


def remove_tree(spec: Any, stage: Any, workdir: Path) -> tuple[bool, str]:
    """Убрать дерево упавшей попытки; `(убрано, причина)` — оба факта в событие.

    Уборка — не гарантия, а попытка: `git worktree remove --force` может
    отказать (занятый каталог, чужой лок). Тогда это видно причиной в событии, а
    не молчанием и не выдуманным «убрано». Зовётся только для `workspace:
    worktree`: у остальных видов дерева нет, и «убирать нечего» — не факт о
    попытке.
    """
    tree = Path(workdir) / "tree"
    if not tree.exists():
        return False, f"дерева уже нет: {tree}"
    repo = Path(str(spec.repo or "")).expanduser()
    code, reason = _git(["worktree", "remove", "--force", str(tree)], repo)
    if code != 0:
        return False, f"git worktree remove отказал: {reason or code}"
    return True, ""


# ------------------------------------------------------- контракт и предыстория


def previous_reports(
    conn: sqlite3.Connection,
    deal: Any,
    spec: Any,
    stage_id: str,
    *,
    events: Optional[Iterable[Any]] = None,
) -> list[dict[str, Any]]:
    """Отчёты предшественников из журнала: этап, коммит, артефакты, резюме, вердикт.

    Собирается только по фактам: `attempt_ended` с результатом даёт `commit`,
    `summary` и `artifacts`, `collected`/`changes_requested` — вердикт. Порядок —
    объявление этапов в спеке, чтобы файл контракта не зависел от порядка
    событий. `commit` обязателен: без него следующий исполнитель не знает, от
    чего отводиться, и «что видел этап» отвечалось бы только чтением кода
    (`base_of_stage` берёт базу оттуда же). `events` — уже прочитанный журнал
    того же дела: один проход на шаг, а не по проходу на каждый вопрос.
    """
    found: dict[str, dict[str, Any]] = {}
    verdicts: dict[str, str] = {}
    for event in events if events is not None else journal.read_events(conn, str(deal["id"])):
        payload = journal.payload_of(event)
        stage = payload.get("stage")
        if not isinstance(stage, str) or not stage:
            continue
        kind = str(event.get("kind") or "")
        if kind == "attempt_ended":
            if isinstance(payload.get("result"), dict):
                found[stage] = payload
        elif kind in (journal.VERDICT_APPROVE, journal.VERDICT_CHANGES):
            verdicts[stage] = journal.verdict_action(kind)
    out: list[dict[str, Any]] = []
    for stage in getattr(spec, "stages", ()):
        if stage.id == stage_id or (stage.id not in found and stage.id not in verdicts):
            continue
        payload = found.get(stage.id) or {}
        result = payload.get("result")
        result = result if isinstance(result, dict) else {}
        artifacts = result.get("artifacts")
        out.append(
            {
                "stage": stage.id,
                "commit": attempt_commit(payload),
                "artifacts": artifacts if isinstance(artifacts, list) else [],
                "summary": str(result.get("summary") or ""),
                "verdict": verdicts.get(stage.id),
            }
        )
    return out


def report_verdict(
    deal: Any,
    spec: Any,
    stage: Any,
    result: dict[str, Any],
    attempt: int,
    *,
    events: Iterable[Any],
    now: int,
) -> tuple[Optional[dict[str, Any]], str]:
    """Факт приговора этапа из структурного поля отчёта: (событие, причина отказа).

    Гейт графа (`dashboard/transit.py`, правило 2) требует **факт уровня этапа** —
    `collected`/`changes_requested` с `stage` в payload. `attempt_ended` таким
    фактом не является: в нём исход попытки, а `verdict` в его `details` — гейт
    пакета. Поэтому приговор берётся из отчёта полем `verdict: {action, target,
    reason}` — единственной структурной формой слова «одобрено» — и становится
    событием здесь, в той же транзакции, что и `attempt_ended`: читатель видит и
    смерть попытки, и приговор, а не одно из двух.

    Отказ, а не тишина: этап с `verdict` в спеке, отчитавшийся `done` без
    приговора, останавливает попытку `contract` — иначе следующий этап снова
    молча ждал бы приговор, которого никто не пишет. Тот же класс у приговора,
    адресующего несуществующий этап, у неизвестного `action` и у возврата сверх
    `max_rounds` (бесконечный возврат — не раунды ревью, а их отсутствие).
    """
    verdict = getattr(stage, "verdict", None)
    if verdict is None:
        return None, ""
    where = f"{getattr(spec, 'path', '')}:{stage.line_of('verdict')}"
    raw = result.get("verdict")
    if not isinstance(raw, dict):
        return None, (
            f"{where}: этап ревью обязан вернуть verdict: {{action, target, reason}}: "
            "иначе следующий этап не знает, одобрено ли"
        )
    action = str(raw.get("action") or "").strip().lower()
    kind = journal.verdict_kind(action)
    if not kind:
        return None, (
            f"{where}: verdict.action={raw.get('action')!r}: допустимо "
            f"{journal.VERDICT_ACTION_APPROVE} (одобрено) "
            f"или {journal.VERDICT_ACTION_CHANGES} (на доработку)"
        )
    target = str(raw.get("target") or "").strip()
    if not target:
        target = verdict.approve if action == journal.VERDICT_ACTION_APPROVE else verdict.changes
    known = {item.id for item in getattr(spec, "stages", ())}
    if target != journal.VERDICT_NEXT and target not in known:
        return None, (
            f"{where}: verdict.target={target!r}: такого этапа в спеке нет — "
            "приговор адресует next или id существующего этапа"
        )
    if action == journal.VERDICT_ACTION_CHANGES:
        rounds = sum(
            1
            for event in events
            if str(event.get("kind")) == journal.VERDICT_CHANGES
            and journal.payload_of(event).get("stage") == stage.id
        )
        max_rounds = int(getattr(verdict, "max_rounds", 1) or 1)
        if rounds >= max_rounds:
            return None, (
                f"{where}: возвратов на доработку уже {rounds} при max_rounds={max_rounds}: "
                "раунды ревью исчерпаны — решает человек"
            )
    return (
        _event(
            deal,
            kind,
            {
                "stage": stage.id,
                "target": target,
                "reason": str(raw.get("reason") or "").strip(),
                "attempt": int(attempt),
            },
            {"stage": stage.id, "attempt": int(attempt), "target": target},
            now,
        ),
        "",
    )


def is_plan_stage(stage: Any) -> bool:
    """Это этап плана? Только он обязан предложить план своим отчётом.

    id и вид, а не роль: роль лида бывает и у `close`, а «предложи план» — работа
    одного этапа (`docs/ENGINE-1.0.md`, раздел 4). Слово берётся у `transit`, чтобы
    писатель (`report_plan`) и читатель графа (`transit.graph`) не разошлись в том,
    чей это план.
    """
    return (
        str(getattr(stage, "id", "") or "") == transit.PLAN_STAGE
        and str(getattr(stage, "kind", STAGE_KIND_AGENT)) == STAGE_KIND_AGENT
    )


def _plan_issues(issues: Iterable[Any]) -> list[str]:
    """Отказы плана словами: `правило: текст` без псевдопути `<plan>:1`.

    Путь предложения не файловый (`spec.PLAN_PATH`), и в списке отказов он только
    мешает: лид видит, какое правило нарушено, а не строку несуществующего файла.
    """
    out: list[str] = []
    for issue in issues:
        rule = str(getattr(issue, "rule", "") or "")
        message = str(getattr(issue, "message", "") or "")
        out.append(f"{rule}: {message}" if rule else (message or str(issue)))
    return out


def plan_gate_reason(spec: Any, stages: Iterable[Any], graph: Iterable[Any]) -> str:
    """Почему план обязан спросить человека; пустая строка — порог не пройден.

    Порог объявлен хартией (`charter.plan_gate`, `docs/ENGINE-1.0.md`, раздел 5):
    число этапов **всего** графа и доля бюджета дела, которую забирают этапные
    бюджеты предложения. Ноль в поле — гейт по этому признаку выключен, поэтому
    спека без `plan_gate` вопросов человеку не задаёт.
    """
    gate = getattr(getattr(spec, "charter", None), "plan_gate", None)
    if gate is None:
        return ""
    total = len(tuple(graph))
    reasons: list[str] = []
    if int(getattr(gate, "min_stages", 0) or 0) > 0 and total > int(gate.min_stages):
        reasons.append(f"этапов в графе {total} больше порога {int(gate.min_stages)}")
    share = float(getattr(gate, "budget_share", 0.0) or 0.0)
    limit = getattr(getattr(spec, "budget", None), "tokens", None)
    if share > 0 and isinstance(limit, int) and limit > 0:
        asked = sum(int(getattr(getattr(stage, "budget", None), "tokens", 0) or 0) for stage in stages)
        if asked / limit > share:
            reasons.append(
                f"этапные бюджеты плана просят {asked} из {limit} токенов дела "
                f"({asked / limit:.0%}) — больше доли {share:.0%}"
            )
    return "; ".join(reasons)


def report_plan(
    deal: Any,
    spec: Any,
    stage: Any,
    result: dict[str, Any],
    attempt: int,
    *,
    events: Iterable[Any],
    now: int,
) -> tuple[list[dict[str, Any]], str]:
    """Факты плана из отчёта этапа плана: (события, причина отказа).

    Образец — `report_verdict`: структурное поле отчёта (`plan: {stages, reason}`)
    становится фактом журнала в той же транзакции, что и `attempt_ended`, потому
    что граф дела — это факт, а не слово в `summary` (`docs/ENGINE-1.0.md`,
    раздел 4).

    Три исхода, и все три — факты, а не догадки:

    - этап плана отчитался без `plan` → `([], причина)`, и попытка становится
      отказом `contract`: этап, который должен планировать, не вправе «просто
      закончиться»;
    - предложение не прошло `spec.parse_plan` → `plan_proposed` +
      `plan_rejected(issues[])`: лид видит, какое правило нарушено, а граф не
      растёт;
    - предложение прошло и порог `plan_gate` не пройден → `plan_proposed` +
      `plan_accepted(by: engine)`. Пройден → только `plan_proposed`: решение
      принимает человек (`runner.plan_gate`).

    Валидация — `spec.parse_plan`, а не собственные проверки: «принять план на
    глаз» значит завести второй валидатор, который разойдётся с `spec check` на
    первом же новом поле.
    """
    if not is_plan_stage(stage):
        return [], ""
    events = list(events or ())
    where = f"{getattr(spec, 'path', '')}:{getattr(stage, 'line', 0)}"
    raw = result.get("plan")
    if not isinstance(raw, dict):
        return [], (
            f"{where}: этап плана обязан вернуть plan: {{stages, reason}}: "
            "без плана следующие этапы не знают, что делать"
        )
    graph = transit.graph(spec, events)
    base_ids = [item.id for item in graph]
    proposals = [
        journal.payload_of(event)
        for event in events
        if str(event.get("kind") or "") == transit.PLAN_PROPOSED
        and str(journal.payload_of(event).get("stage") or "") == ""
    ]
    rev = 1 + max((int(_int(item.get("rev")) or 0) for item in proposals), default=0)
    raw_stages = raw.get("stages")
    charter = getattr(spec, "charter", None) or spec_module.Charter()
    stages, issues = spec_module.parse_plan(raw_stages, charter=charter, base_ids=base_ids)
    proposed = _event(
        deal,
        transit.PLAN_PROPOSED,
        {
            "rev": rev,
            "stages": raw_stages if isinstance(raw_stages, list) else [],
            "reason": str(raw.get("reason") or "").strip(),
            "source_stage": stage.id,
            "attempt": int(attempt),
        },
        {"stage": stage.id, "attempt": int(attempt), "rev": rev},
        now,
    )
    if issues:
        why = f"план ревизии {rev} отклонён: отказов {len(issues)}"
        rejected = _event(
            deal,
            transit.PLAN_REJECTED,
            {"rev": rev, "why": why, "issues": _plan_issues(issues), "source_stage": stage.id},
            {"stage": stage.id, "attempt": int(attempt), "rev": rev},
            now,
        )
        return [proposed, rejected], ""
    asked = plan_gate_reason(spec, stages, graph + tuple(stages))
    if asked:
        return [proposed], ""  # порог пройден: решает человек, граф пока не растёт
    accepted = _event(
        deal,
        transit.PLAN_ACCEPTED,
        {"rev": rev, "by": ENGINE_SOURCE},
        {"stage": stage.id, "attempt": int(attempt), "rev": rev},
        now,
    )
    return [proposed, accepted], ""


def last_session(conn: sqlite3.Connection, deal: Any, stage_id: str) -> str:
    """`session_id` последней попытки этапа — точка возврата для резюма.

    Проба 1 (`docs/PROBES.md`) дала `RESUME-OK`: id из события `result` держит
    контекст и после `clarify`, поэтому продолжение после ответа человека идёт
    `--resume`, а не новым процессом с прозой в запросе.
    """
    for event in reversed(journal.read_events(conn, str(deal["id"]))):
        payload = journal.payload_of(event)
        if payload.get("stage") != stage_id:
            continue
        kind = str(event.get("kind") or "")
        if kind == "attempt_ended":
            details = payload.get("details")
            session = ""
            if isinstance(details, dict):
                session = str(details.get("session_id") or "")
            if not session:
                result = payload.get("result")
                if isinstance(result, dict):
                    session = str(result.get("session_id") or "")
            return session
        if kind == "attempt_started":
            return str(payload.get("session_id") or "")
    return ""


def charter_payload(spec: Any) -> dict[str, Any]:
    """Хартия дела для контракта этапа плана: чем граф вправе расти.

    Тот же набор полей, что в `duty.yaml: charter`, — лид видит границы числами, а
    не узнаёт о них из отказа (`docs/ENGINE-1.0.md`, раздел 5).
    """
    charter = getattr(spec, "charter", None) or spec_module.Charter()
    gate = getattr(charter, "plan_gate", None) or spec_module.PlanGate()
    return {
        "max_stages": int(getattr(charter, "max_stages", 0) or 0),
        "max_rounds_replan": int(getattr(charter, "max_rounds_replan", 0) or 0),
        "allowed_roles": [str(role) for role in getattr(charter, "allowed_roles", ()) or ()],
        "allowed_kinds": [str(kind) for kind in getattr(charter, "allowed_kinds", ()) or ()],
        "plan_gate": {
            "min_stages": int(getattr(gate, "min_stages", 0) or 0),
            "budget_share": float(getattr(gate, "budget_share", 0.0) or 0.0),
        },
    }


def plan_payload(spec: Any, events: Iterable[Any]) -> dict[str, Any]:
    """Что этап плана знает о деле: существующий граф и остаток бюджета.

    План рисуется поверх графа, а не с нуля: `base_ids` — id, на которые можно
    ссылаться в `needs` (переопределять их планом нельзя), `remaining` — расход и
    потолок дела (если потолка нет, поля просто нет: «0 из 0» было бы выдумкой).
    """
    graph = transit.graph(spec, events)
    return {
        "graph": [
            {
                "id": stage.id,
                "role": getattr(stage, "role", ""),
                "kind": getattr(stage, "kind", STAGE_KIND_AGENT),
                "needs": [str(need) for need in getattr(stage, "needs", ()) or ()],
            }
            for stage in graph
        ],
        "base_ids": [stage.id for stage in graph],
        "remaining": budget.describe(spec, events),
        "report": "plan: {stages: [{id, role, kind, needs, evidence, budget}], reason}",
    }


def build_contract(
    spec: Any,
    deal: Any,
    stage: Any,
    attempt: int,
    *,
    workdir: Path,
    tree: Optional[Path],
    cwd: Path,
    previous: list[dict[str, Any]],
    deadline_sec: int,
    answer: Optional[str] = None,
    session_id: str = "",
    approved: Optional[dict[str, Any]] = None,
    events: Optional[Iterable[Any]] = None,
) -> dict[str, Any]:
    """Входной контракт попытки: файл, а не проза в командной строке.

    Состав — то, что попытке нужно знать и не нужно додумывать: цель (дело),
    границы (куда можно писать и куда отчитываться), отчёты предшественников
    (артефакты и вердикты из журнала), что доказать (`evidence`), кому задавать
    фактовые вопросы (`ask`) и куда положить отчёт. `answer_this_attempt`
    появляется только у попытки, поднятой после ответа человека.
    """
    report_to = result_path(workdir)
    boundaries = [
        "дерева нет: этап только читает и выносит приговор"
        if stage.workspace == "none"
        else f"рабочий каталог попытки: {cwd} — писать только в него",
        f"отчёт — файл {report_to}; вопрос — полем question в нём "
        "(clarify в режиме --oneshot ответа не получает)",
        "отчёт пишется инструментом write_file (валидный JSON по report_schema) — "
        "текстом в ответе движок его не увидит",
        "размечай вопрос: kind (fact | authority), risky (true) и default — "
        "без разметки вопрос считается властным и уходит человеку",
    ]
    evidence: list[dict[str, Any]] = []
    for item in getattr(stage, "evidence", ()):
        command: dict[str, Any] = {"cmd": item.cmd, "must": item.must}
        if item.timeout_sec != spec_module.DEFAULT_TIMEOUT_SEC:
            command["timeout_sec"] = item.timeout_sec
        evidence.append(command)
    stage_ask = getattr(stage, "ask", None)
    questions = {
        "allowed": list(stage_ask.allowed) if stage_ask is not None else [],
        "to_human": stage_ask.to_human if stage_ask is not None else "decisions_only",
        "max_hops": stage_ask.max_hops if stage_ask is not None else 0,
        "risky": bool(getattr(stage_ask, "risky", False)),
        "default": str(getattr(stage_ask, "default", "") or ""),
    }
    goal = str(deal.get("title") or "").strip()
    body = str(deal.get("body") or "").strip()
    if body:
        goal = f"{goal}\n\n{body}" if goal else body
    contract: dict[str, Any] = {
        "deal": str(deal["id"]),
        "stage": stage.id,
        "attempt": int(attempt),
        "role": stage.role,
        "workdir": str(workdir),
        "tree": str(tree) if tree is not None else None,
        "goal": goal,
        "boundaries": boundaries,
        "previous": previous,
        "evidence_to_produce": evidence,
        "report_schema": evidence_module.report_schema(stage),
        "questions": questions,
    }
    if answer is not None:
        contract["answer_this_attempt"] = answer
    if approved is not None:
        # Что именно человек одобрил (`dashboard/publish.py`): публикующему этапу
        # нужно публиковать этот коммит, а не то, что он найдёт в дереве.
        contract["approved"] = approved
    if session_id:
        contract["session_id"] = session_id
    # Этап плана получает в контракте то, без чего план не нарисовать: хартию
    # (границы роста графа), уже существующий граф и остаток бюджета дела.
    # «Не влез в хартию» и «не знал потолка» — разные отказы, и второй из них
    # узнавался бы только из `plan_rejected` (`docs/ENGINE-1.0.md`, раздел 4).
    if is_plan_stage(stage):
        contract["charter"] = charter_payload(spec)
        contract["plan"] = plan_payload(spec, events or ())
    contract["report_to"] = str(report_to)
    contract["deadline_sec"] = int(deadline_sec)
    return contract


def write_contract(path: Path, contract: dict[str, Any]) -> Path:
    """Записать контракт на диск до спавна: процесс читает файл, а не аргументы."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_answer(path: Path, answer: str, contract: Path) -> Path:
    """Запрос продолжения: ответ человека плюс адрес контракта и отчёта.

    `--query-file` не экранирует ничего, поэтому ответ попадает в процесс
    дословно (`docs/PROBES.md`, проба 1) — кавычки и `$(...)` в нём безопасны.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"Ответ человека на вопрос прошлой попытки:\n{answer}\n\n"
        f"Контракт попытки: {contract}\n"
        f"Отчёт: {result_path(path.parent.parent)}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def spawn_command(role: str, query_file: Path, session_id: str = "") -> list[str]:
    """Команда спавна: ровно те ключи, что сверены с `hermes chat --help` и пробой.

    `--format stream-json` подразумевает `--quiet` и одиночный запрос; `-p` —
    профиль роли из спеки, права не расширяются. `--resume` появляется только у
    продолжения (после ответа человека).
    """
    command = [HERMES_BIN, "chat", "-p", str(role)]
    if session_id:
        command.extend(["--resume", str(session_id)])
    command.extend(["--query-file", str(query_file), "--format", "stream-json", "--oneshot"])
    return command


# -------------------------------------------------------------------- процесс


class Process:
    """Запущенный процесс попытки: строки потока, код выхода, остановка.

    Поток читается отдельным потоком в очередь: иначе `for line in stdout` 
    заблокировал бы heartbeat лизинга и таймаут до самого конца процесса, и
    «что делает прямо сейчас» снова стало бы невидимым.

    `logs` — каталог полных логов (`stdout.log`, `stderr.log`). Агенту он не
    нужен (его поток — телеметрия, а хвост stderr и так пишется файлом), а
    shell-этапу нужен: от события остаётся только хвост, и разбор отказа без
    полного вывода команды был бы догадкой.
    """

    def __init__(
        self, cmd: list[str], cwd: Path, env: Optional[dict[str, str]] = None, logs: Optional[Path] = None
    ) -> None:
        self.cmd = list(cmd)
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._stderr: list[str] = []
        self._eof = False
        self._logs = Path(logs) if logs is not None else None
        self._stdout_log = self._open_log(SHELL_STDOUT_LOG)
        self._stderr_log = self._open_log(SHELL_STDERR_LOG)
        self._proc = subprocess.Popen(
            self.cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )
        self._out = threading.Thread(target=self._read_stdout, daemon=True)
        self._err = threading.Thread(target=self._read_stderr, daemon=True)
        self._out.start()
        self._err.start()

    def _open_log(self, name: str) -> Optional[Any]:
        """Файл полного лога потока; `None` — логи не просили или он не открылся."""
        if self._logs is None:
            return None
        try:
            self._logs.mkdir(parents=True, exist_ok=True)
            return (self._logs / name).open("w", encoding="utf-8")
        except OSError:  # pragma: no cover - каталог попытки уже создан движком
            return None

    def _write_log(self, handle: Optional[Any], line: str) -> None:
        """Строка в полный лог: поток идёт дальше своим чередом, лог не роняет попытку."""
        if handle is None:
            return
        try:
            handle.write(line)
            handle.flush()
        except (OSError, ValueError):  # pragma: no cover - диск кончился или файл закрыт
            pass

    def close_logs(self) -> None:
        """Закрыть полные логи: попытка кончилась, писать больше нечего."""
        for handle in (self._stdout_log, self._stderr_log):
            if handle is None:
                continue
            try:
                handle.close()
            except OSError:  # pragma: no cover - закрытие после сбоя записи
                pass

    @property
    def pid(self) -> int:
        return int(self._proc.pid)

    @property
    def exit_code(self) -> Optional[int]:
        return self._proc.returncode

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr)

    def _read_stdout(self) -> None:
        try:
            for line in self._proc.stdout or ():
                self._write_log(self._stdout_log, line)
                self._queue.put(line)
        finally:
            self._queue.put(None)

    def _read_stderr(self) -> None:
        for line in self._proc.stderr or ():
            self._write_log(self._stderr_log, line)
            text = line.rstrip("\n")
            if not text:
                continue
            self._stderr.append(text)
            del self._stderr[:-STDERR_TAIL]

    @property
    def stderr_text(self) -> str:
        """Хвост stderr (последние `STDERR_TAIL` строк): пишется файлом, не в журнал."""
        return "\n".join(self._stderr)

    def next_line(self, timeout: float = 0.0) -> Optional[str]:
        """Следующая строка потока; `None` — строки пока нет или поток закрыт."""
        if self._eof and self._queue.empty():
            return None
        try:
            item = self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None
        if item is None:
            self._eof = True
            return None
        return item

    def finished(self) -> bool:
        """Процесс завершился и потока больше нет: читать нечего."""
        return self._proc.poll() is not None and self._eof

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """Код возврата или `None`, если за отведённое время процесс не вышел."""
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self, grace: float = KILL_GRACE_SEC) -> Optional[int]:
        """Остановить попытку: SIGTERM, затем SIGKILL через `grace` секунд."""
        if self._proc.poll() is not None:
            return self._proc.returncode
        try:
            self._proc.terminate()
        except ProcessLookupError:  # pragma: no cover - вышел между poll и terminate
            pass
        code = self.wait(grace)
        if code is None:
            try:
                self._proc.kill()
            except ProcessLookupError:  # pragma: no cover - вышел между wait и kill
                pass
            code = self.wait(grace)
        return code


def spawn_process(
    cmd: list[str], cwd: Path, env: Optional[dict[str, str]] = None, logs: Optional[Path] = None
) -> Process:
    """Запустить попытку. Единственный шов: тесты подменяют его фейком.

    Ни один тест движка не поднимает модель: подмена пишет заданный поток и
    `result.json`, а шаг проходит те же ветки, что и с живым процессом.
    `logs` называет только shell-этап: его вывод целиком нужен файлом.
    """
    return Process(cmd, cwd, env=env, logs=logs)


# ------------------------------------------------------------ поток и события


def _now_sec(now: Optional[int] = None) -> int:
    """Текущее время в unix-секундах — как все `*_at` журнала и store."""
    return int(time.time()) if now is None else int(now)


def _int(value: Any) -> Optional[int]:
    """Целое из потока или `None`: `true`/строка без цифр целым не считаются."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event(
    deal: Any, kind: str, payload: dict[str, Any], args: dict[str, Any], now: int, source: str = ENGINE_SOURCE
) -> dict[str, Any]:
    """Событие движка: тот же ключ идемпотентности, что у остальных мутаций.

    `source` отличает писателя: у решения движка — `engine`, у попытки, которую
    исполнила команда, — `shell`. Ключ идемпотентности считается от того же
    источника, поэтому два писателя одного факта не столкнутся ключами.
    """
    return {
        "ts": now,
        "recorded_at": now,
        "deal_id": str(deal["id"]),
        "kind": kind,
        "payload": payload,
        "source": source,
        "idempotency_key": deals.event_key(source, str(deal["id"]), kind, args),
    }


def _progress_event(
    deal: Any, stage: str, attempt: int, seq: int, data: dict[str, Any], now: int
) -> dict[str, Any]:
    """Событие телеметрии по одному шагу попытки: инструмент и его результат.

    В payload нет ни текста команд, ни содержимого файлов: журнал отвечает «что
    делается», а не заменяет лог. Длительность есть только у результата — у
    вызова её в потоке нет, и выдумывать ноль нельзя.
    """
    phase = "tool_use" if str(data.get("type")) == "tool_use" else "tool_result"
    payload: dict[str, Any] = {
        "stage": stage,
        "attempt": int(attempt),
        "seq": int(seq),
        "phase": phase,
        "name": str(data.get("name") or ""),
        "ts": now,
    }
    if phase == "tool_result":
        duration = _int(data.get("duration_ms"))
        if duration is not None:
            payload["duration_ms"] = duration
    return _event(
        deal,
        "attempt_progress",
        payload,
        {"stage": stage, "attempt": int(attempt), "seq": int(seq), "phase": phase},
        now,
    )


def _stream_line(line: str) -> Optional[dict[str, Any]]:
    """Строка потока объектом; не JSON или не объект — не факт, а мусор."""
    try:
        data = json.loads(line)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class Followed:
    """Что дал поток: таймаут, бюджет, usage, id сессии, сколько шагов увидено."""

    def __init__(self) -> None:
        self.timed_out = False
        self.over_budget = False
        self.usage = 0
        self.session_id = ""
        self.tools = 0

    def __repr__(self) -> str:  # pragma: no cover - для отладки руками
        return (
            f"Followed(timed_out={self.timed_out}, over_budget={self.over_budget}, "
            f"usage={self.usage}, tools={self.tools}, session_id={self.session_id!r})"
        )


def follow(
    conn: sqlite3.Connection,
    process: Any,
    *,
    deal: Any,
    stage: Any,
    attempt: int,
    ttl: int,
    deadline: float,
    budget_tokens: Optional[int],
    monotonic: Callable[[], float],
    clock: Callable[[], float],
) -> Followed:
    """Читать поток до конца, писать телеметрию, держать лизинг и бюджет.

    Три вещи делаются по ходу, а не после: heartbeat лизинга (не реже TTL/3),
    проверка бюджета токенов по `usage` из `result` и таймаут. Иначе «процесс
    идёт» и «процесс висит» отличались бы только по ps.
    """
    state = Followed()
    seq = 0
    last_renew = monotonic()
    renew_every = max(float(ttl) / RENEW_DIVISOR, POLL_SEC)
    pid = int(process.pid)

    def handle(line: str) -> None:
        nonlocal seq
        data = _stream_line(line)
        if data is None:
            return
        kind = str(data.get("type") or "")
        if kind in ("tool_use", "tool_result"):
            seq += 1
            if kind == "tool_use":
                state.tools += 1
            journal.append(conn, [_progress_event(deal, stage.id, attempt, seq, data, _now_sec(int(clock())))])
            return
        if kind != "result":
            return
        session = data.get("session_id")
        if isinstance(session, str) and session:
            state.session_id = session
        tokens = data.get("tokens")
        total = _int(tokens.get("total")) if isinstance(tokens, dict) else None
        if total is not None:
            state.usage += total
            if budget_tokens is not None and state.usage > int(budget_tokens):
                state.over_budget = True

    while True:
        if monotonic() - last_renew >= renew_every:
            renew(
                conn,
                deal=deal,
                stage=stage.id,
                pid=pid,
                ttl=ttl,
                now=_now_sec(int(clock())),
            )
            last_renew = monotonic()
        line = process.next_line(POLL_SEC)
        if line is None:
            if state.over_budget:
                process.stop()
                break
            if process.finished():
                break
            if monotonic() >= deadline:
                state.timed_out = True
                process.stop()
                break
            continue
        handle(line)
        if state.over_budget:
            process.stop()
            break

    # Хвост очереди: процесс мог выйти, оставив строки в буфере. Без этого
    # последний `result` (а с ним id сессии и usage) терялся бы ровно на выходе.
    while True:
        line = process.next_line(0)
        if line is None:
            break
        handle(line)
    return state


def write_stderr(workdir: Path, text: str) -> Optional[Path]:
    """Хвост stderr попытки — файлом рядом с отчётом.

    В журнал он не идёт: «журнал не место для логов», и причина смерти попытки
    считается по трём источникам (отчёт, его отсутствие, код выхода), а не по
    тексту ошибки. Файл нужен человеку, который разбирает отказ, и путь на него
    попадает в событие.
    """
    if not text.strip():
        return None
    path = Path(workdir) / DUTY_SUBDIR / "stderr.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def read_result(path: Path) -> tuple[Optional[dict[str, Any]], str]:
    """`result.json` объектом или причина, почему его нет: отчёт — один источник.

    Битый файл — не «нет файла»: причина называется словами, чтобы в событии не
    было молчания вместо факта.
    """
    path = Path(path)
    if not path.is_file():
        return None, f"result.json нет: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"result.json не читается: {exc}"
    except (TypeError, ValueError) as exc:
        return None, f"result.json не разбирается: {exc}"
    if not isinstance(data, dict):
        return None, f"result.json не объект: {type(data).__name__}"
    return data, ""


def classify(
    *,
    result: Optional[dict[str, Any]],
    result_error: str,
    exit_code: Optional[int],
    followed: Followed,
    timeout_sec: int,
) -> tuple[str, str, str]:
    """Чем кончилась попытка: `(outcome, kind, reason)` — по трём источникам.

    Классификация не живёт здесь: правило «источник → класс» одно на плагин и
    лежит в `dashboard/failures.py`. Движок только собирает факты (отчёт, его
    отсутствие, код выхода, наши собственные таймаут и бюджет) и отдаёт их
    политике — иначе «почему класс именно такой» отвечалось бы в двух местах.
    """
    ended = failures.classify(
        result=result,
        result_error=result_error,
        exit_code=exit_code,
        timed_out=followed.timed_out,
        over_budget=followed.over_budget,
        timeout_sec=timeout_sec,
    )
    return ended.outcome, ended.kind, ended.reason


# --------------------------------------------------------------- shell-этап


def shell_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Окружение shell-этапа: как у попытки, но без роли и её токенов.

    Команда этапа не действует от имени профиля: роль и доступы ей не нужны, а
    лишняя переменная с токеном — это право, которого этап не просил. Остальное
    окружение наследуется: команда этапа — обычный процесс дерева (python, go,
    pytest), и вычищать из него всё подряд значило бы ломать ей сборку.
    """
    source = dict(os.environ if base is None else base)
    return {
        key: value
        for key, value in source.items()
        if key.upper() not in SHELL_ENV_ROLE_KEYS and "TOKEN" not in key.upper()
    }


def shell_command(cmd: str) -> list[str]:
    """Команда этапа как процесс: `sh -c <cmd>` — одна строка спеки."""
    return [SHELL_BIN, "-c", str(cmd)]


class ShellRun:
    """Что дал запуск команды: таймаут и хвосты вывода.

    Форма — та же, что у `Followed`: шаг читает один объект, а не три
    переменные, и разница «агент/команда» остаётся в способе запуска.
    """

    def __init__(self) -> None:
        self.timed_out = False
        self.stdout_tail: list[str] = []
        self.stderr_tail: list[str] = []


def follow_shell(
    conn: sqlite3.Connection,
    process: Any,
    *,
    deal: Any,
    stage: Any,
    attempt: int,
    ttl: int,
    deadline: float,
    monotonic: Callable[[], float],
    clock: Callable[[], float],
) -> ShellRun:
    """Довести команду этапа до конца: heartbeat лизинга, таймаут, хвост stdout.

    Хвост собирается здесь, а полный вывод пишет сам процесс файлом (`Process`,
    `logs`): журнал отвечает «чем кончилось», файл — «что было написано».
    Таймаут — наш собственный факт, поэтому проверяется по часам шага, а не по
    коду возврата: убитый по сроку процесс тоже вернёт код сигнала.
    """
    state = ShellRun()
    last_renew = monotonic()
    renew_every = max(float(ttl) / RENEW_DIVISOR, POLL_SEC)
    pid = int(process.pid)
    while True:
        if monotonic() - last_renew >= renew_every:
            renew(conn, deal=deal, stage=stage.id, pid=pid, ttl=ttl, now=_now_sec(int(clock())))
            last_renew = monotonic()
        line = process.next_line(POLL_SEC)
        if line is None:
            if process.finished():
                break
            if monotonic() >= deadline:
                state.timed_out = True
                process.stop()
                break
            continue
        state.stdout_tail.append(line.rstrip("\n"))
        del state.stdout_tail[:-SHELL_TAIL]
    # Хвост очереди: процесс мог выйти, оставив строки в буфере.
    while True:
        line = process.next_line(0)
        if line is None:
            break
        state.stdout_tail.append(line.rstrip("\n"))
        del state.stdout_tail[:-SHELL_TAIL]
    state.stderr_tail = process.stderr_tail[-SHELL_TAIL:]
    return state


def classify_shell(
    *, exit_code: Optional[int], timed_out: bool, must: int, timeout_sec: int
) -> tuple[str, str, str]:
    """Чем кончилась shell-попытка: `(outcome, kind, reason)` — по коду возврата.

    Источников три, как у агента, но отчёта среди них нет: наша собственная
    остановка по сроку (таймаут), смерть процесса (сигнал — отрицательный код) и
    его слово — код возврата. Ненулевой код — отказ **контракта** («код N, ждали
    M»), а не инфраструктуры: команда дошла до конца и сказала «нет».
    """
    if timed_out:
        return OUTCOME_FAILED, FAIL_INFRA, f"команда не дожила: таймаут этапа {int(timeout_sec)} с"
    if exit_code is None:
        return OUTCOME_FAILED, FAIL_INFRA, "команда не дожила: код выхода неизвестен"
    if int(exit_code) < 0:
        return OUTCOME_FAILED, FAIL_INFRA, f"команда не дожила: сигнал {-int(exit_code)}"
    if int(exit_code) != int(must):
        return OUTCOME_FAILED, FAIL_CONTRACT, f"код {int(exit_code)}, ждали {int(must)}"
    return OUTCOME_DONE, "", ""


def shell_result(stage: Any, root: Path, *, exit_code: int, duration_sec: float) -> dict[str, Any]:
    """Отчёт shell-этапа в той же форме, что `result.json` агента.

    Команда отчёта не пишет, но следующие этапы и лента читают пакет одним
    способом (`dashboard/stagefeed.py`, `previous_reports`). Поэтому пакет
    собирает движок — из того, что действительно произошло: код возврата,
    длительность и хеши объявленных артефактов. Хеш считается по файлу в дереве
    этапа: иначе «артефакт есть» доказывало бы только имя.
    """
    artifacts: list[dict[str, Any]] = []
    for path in getattr(stage, "artifacts", ()) or ():
        digest = ""
        try:
            handle = hashlib.sha256()
            with (Path(root) / str(path)).open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    handle.update(chunk)
            digest = handle.hexdigest()
        except OSError:
            digest = ""
        artifacts.append({"path": str(path), "sha256": digest})
    return {
        "status": "done",
        "artifacts": artifacts,
        "evidence": [],
        "summary": f"shell: {getattr(stage, 'cmd', '')} — код {int(exit_code)} за {float(duration_sec):.1f} с",
    }


def refuse_budget(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    stage: Any,
    attempt: int,
    cap: Any,
    *,
    clock: Callable[[], float],
) -> Optional[dict[str, Any]]:
    """Потолок дела исчерпан — попытка не поднимается, но факт в журнале есть.

    Проверка живёт **до** лизинга и спавна: лизинг означал бы «этап за процессом»,
    которого нет, а `attempt_started` — попытку, которую никто не начинал (уборка
    закрыла бы её как мёртвую). Поэтому отказ пишется `attempt_ended` без начала —
    ровно так же, как отказ гейта (`_gate_judge`): факт исхода без факт процесса.

    Два вида, оба уже в закрытом наборе журнала:

    - `attempt_ended` с `kind: capability` и причиной с числами — по нему `transit`
      переводит этап в `failed` (класс `capability` по умолчанию не повторяется,
      `dashboard/failures.py`), и дело останавливается, а не крутится;
    - `blocked` с `block_kind='capability'` и той же причиной — по нему `fold`
      показывает состояние `capability` и те же числа в `bin/duty why` и `duty deal`:
      без него поверхность сказала бы «status=running без живого факта».

    Парковки здесь нет намеренно: парковка спрашивает человека «продолжать?», а
    продолжать при исчерпанном потолке нечего — ответ «continue» сжёг бы ещё
    столько же. Потолок — стена, а не исчерпанный счётчик ретраев.

    `None` — потолок не исчерпан или его нет вовсе: запускать можно, и это ответ,
    а не отсутствие проверки.
    """
    if cap is None:
        return None
    now = _now_sec(int(clock()))
    run_id = f"{stage.id}#{int(attempt)}"
    ended = _event(
        deal,
        "attempt_ended",
        {
            "run_id": run_id,
            "stage": stage.id,
            "attempt": int(attempt),
            "outcome": OUTCOME_FAILED,
            "kind": FAIL_CAPABILITY,
            "reason": cap.reason(),
            "details": {
                "refused": True,
                "stage_kind": getattr(stage, "kind", STAGE_KIND_AGENT),
                "budget": cap.as_payload(),
            },
        },
        {"stage": stage.id, "attempt": int(attempt), "refused": True},
        now,
    )
    journal.append(conn, [ended])
    reason = f"этап {stage.id}: {cap.reason()} — попытка {int(attempt)} не поднималась"
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind=FAIL_CAPABILITY,
        reason=reason,
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        details={"stage": stage.id, "attempt": int(attempt), "budget": cap.as_payload()},
        now=now,
    )
    return {
        "started": False,
        "refused": True,
        "stage": stage.id,
        "attempt": int(attempt),
        "outcome": OUTCOME_FAILED,
        "kind": FAIL_CAPABILITY,
        "reason": cap.reason(),
    }


def run_shell_stage(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    stage_id: str,
    attempt: int,
    *,
    home: Path,
    spawn: Optional[Callable[..., Any]] = None,
    clock: Optional[Callable[[], float]] = None,
    monotonic: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Одна попытка shell-этапа: лизинг, дерево, команда, факты в журнал.

    Договор результата — код возврата и артефакты, а не `result.json`: команда не
    отчитывается, она либо сходится с `must`, либо нет. Вопросов человеку этап не
    задаёт (это отказ валидации спеки), поэтому `ask` здесь не участвует вовсе.
    Всё остальное — как у агента: лизинг этапа, дерево по `workspace`, база этапа,
    лизинг переезжает к процессу, отказ пишется событием и паркуется политикой.
    """
    spawner = spawn or spawn_process
    clock = clock or time.time
    monotonic = monotonic or time.monotonic
    # Этап ищется в графе дела, а не в файле: этап, добавленный принятым планом,
    # в `duty.yaml` не объявлен (`docs/ENGINE-1.0.md`, раздел 3).
    spec = transit.graph_spec(spec, journal.read_events(conn, str(deal["id"])))
    stage = next((item for item in spec.stages if item.id == stage_id), None)
    if stage is None:
        raise RunnerError(f"этапа {stage_id!r} нет в спеке {getattr(spec, 'path', '')}")
    attempt = int(attempt)
    if attempt < 1:
        raise RunnerError(f"номер попытки {attempt}: попытки считаются с 1")
    ttl = ttl_sec(stage)
    deadline_sec = ttl
    engine_pid = os.getpid()
    workdir = attempt_dir(home, deal, stage_id, attempt)
    run_id = f"{stage_id}#{attempt}"

    # Тот же потолок дела, что у агента: shell-этап токенов не тратит, но тратит
    # минуты, и исчерпанный потолок останавливает и его.
    spent = budget.refusal(spec, journal.read_events(conn, str(deal["id"])))
    refused = refuse_budget(conn, spec, deal, stage, attempt, spent, clock=clock)
    if refused is not None:
        return refused

    held_at = _now_sec(int(clock()))
    if not lease(conn, deal=deal, stage=stage_id, attempt=attempt, pid=engine_pid, ttl=ttl, now=held_at):
        held = lease_of(conn, deal, stage_id) or {}
        return {
            "started": False,
            "stage": stage_id,
            "attempt": attempt,
            "reason": f"этап уже за процессом pid={held.get('pid')}, срок до {held.get('expires_at')}",
        }

    stage_base = str(spec.base or "")
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        events = journal.read_events(conn, str(deal["id"]))
        stage_base = base_of_stage(spec, stage, events)
        tree, cwd = prepare_tree(spec, stage, workdir, base=stage_base)
    except (RunnerError, OSError) as exc:
        kind = FAIL_CONTRACT if isinstance(exc, BaseError) else FAIL_INFRA
        release(conn, deal=deal, stage=stage_id, pid=engine_pid)
        now = _now_sec(int(clock()))
        event = _event(
            deal,
            "attempt_ended",
            {
                "run_id": run_id,
                "stage": stage_id,
                "attempt": attempt,
                "outcome": OUTCOME_FAILED,
                "kind": kind,
                "reason": str(exc),
                "details": {"cmd": stage.cmd, "workdir": str(workdir), "tree": None, "base": stage_base},
            },
            {"stage": stage_id, "attempt": attempt, "outcome": OUTCOME_FAILED},
            now,
            SHELL_SOURCE,
        )
        journal.append(conn, [event])
        park = park_exhausted(conn, spec, deal, stage, attempt, kind, str(exc), home=home, clock=clock)
        return {
            "started": True,
            "stage": stage_id,
            "attempt": attempt,
            "outcome": OUTCOME_FAILED,
            "kind": kind,
            "reason": str(exc),
            "workdir": str(workdir),
            "tree": None,
            "base": stage_base,
            "park": park,
        }

    actual_base = stage_base
    if tree is not None:
        sha = _sha(Path(tree), "HEAD")
        if sha:
            actual_base = sha

    command = shell_command(stage.cmd)
    now = _now_sec(int(clock()))
    try:
        process = spawner(command, cwd, shell_env(), logs=workdir / DUTY_SUBDIR)
    except OSError as exc:
        release(conn, deal=deal, stage=stage_id, pid=engine_pid)
        if stage.workspace == "worktree":
            remove_tree(spec, stage, workdir)
        raise RunnerError(f"запуск команды этапа не удался: {exc}") from exc

    pid = int(process.pid)
    started = _event(
        deal,
        "attempt_started",
        {
            "run_id": run_id,
            "stage": stage_id,
            "attempt": attempt,
            "pid": pid,
            "cmd": command,
            "shell_cmd": stage.cmd,
            "stage_kind": STAGE_KIND_SHELL,
            "cwd": str(cwd),
            "workdir": str(workdir),
            "tree": str(tree) if tree is not None else None,
            "base": actual_base,
            "lease_expires": now + ttl,
        },
        {"stage": stage_id, "attempt": attempt},
        now,
        SHELL_SOURCE,
    )
    try:
        with conn:
            written = journal.append_in_tx(conn, [started])
            _hand_over(conn, str(deal["id"]), stage_id, engine_pid, pid, attempt, ttl, now)
            if written:
                deals._set_status(conn, deal, "running", now)
    except sqlite3.Error:
        process.stop()
        if not release(conn, deal=deal, stage=stage_id, pid=pid):
            release(conn, deal=deal, stage=stage_id, pid=engine_pid)
        raise

    began = monotonic()
    try:
        followed = follow_shell(
            conn,
            process,
            deal=deal,
            stage=stage,
            attempt=attempt,
            ttl=ttl,
            deadline=began + deadline_sec,
            monotonic=monotonic,
            clock=clock,
        )
    except sqlite3.Error:
        process.stop()
        release(conn, deal=deal, stage=stage_id, pid=pid)
        raise
    exit_code = process.exit_code if process.exit_code is not None else process.wait(KILL_GRACE_SEC)
    duration = max(0.0, monotonic() - began)
    process.close_logs()
    outcome, kind, reason = classify_shell(
        exit_code=exit_code, timed_out=followed.timed_out, must=int(getattr(stage, "must", 0)), timeout_sec=deadline_sec
    )

    # Пакет: команда отчёта не пишет, поэтому его собирает движок — и только
    # объявленные артефакты проверяются тем же гейтом, что у агента
    # (`dashboard/evidence.py`: путь, наличие, sha256).
    now = _now_sec(int(clock()))
    result: Optional[dict[str, Any]] = None
    verdict = None
    if outcome == OUTCOME_DONE:
        result = shell_result(stage, cwd, exit_code=int(exit_code or 0), duration_sec=duration)
        if getattr(stage, "artifacts", ()):
            verdict = evidence_module.verify(
                result,
                stage,
                workdir,
                attempt_started_at=int(started["ts"]),
                now=now,
                root=cwd,
                base=spec.base,
            )
            if not verdict.ok:
                outcome, kind, reason = OUTCOME_FAILED, failures.verdict_kind(verdict), verdict.reason()

    committed = ""
    branch = ""
    branch_error = ""
    if outcome == OUTCOME_DONE and tree is not None:
        committed, commit_error = commit_tree(tree, deal, stage_id, attempt)
        if not committed:
            outcome = OUTCOME_FAILED
            kind = FAIL_INFRA
            reason = f"работа попытки не зафиксирована коммитом: {commit_error}"
        else:
            branch, branch_error = anchor_commit(tree, deal, committed)

    # Дерево упавшей попытки убирается только пустым. Правило было перевёрнуто:
    # дерево удалялось ровно тогда, когда работы в коммите ещё не было, и падение
    # на 55-й минуте уносило единственную копию правок (дело d_7d92affb: 1300
    # строк, 284 вызова, коммита нет — и дерево стёрто). Уборка — не побочный
    # эффект падения: пустое дерево убирается, грязное остаётся и называется
    # путём в событии.
    cleaned = False
    cleanup = ""
    kept_tree = ""
    if outcome != OUTCOME_DONE and stage.workspace == "worktree":
        tree_path = Path(workdir) / "tree"
        dirty, _files = tree_dirty(tree_path) if tree_path.exists() else (False, 0)
        if dirty:
            kept_tree = str(tree_path)
        else:
            cleaned, cleanup = remove_tree(spec, stage, workdir)

    payload: dict[str, Any] = {
        "run_id": run_id,
        "stage": stage_id,
        "attempt": attempt,
        "outcome": outcome,
        "kind": kind or None,
        "reason": reason,
        "base": actual_base,
        "commit": committed or None,
        "details": {
            "cmd": stage.cmd,
            "exit_code": exit_code,
            "duration_sec": round(duration, 1),
            "must": int(getattr(stage, "must", 0)),
            "timed_out": followed.timed_out,
            "stdout_tail": followed.stdout_tail,
            "stderr_tail": followed.stderr_tail,
            "stdout_path": str(workdir / DUTY_SUBDIR / SHELL_STDOUT_LOG),
            "stderr_path": str(workdir / DUTY_SUBDIR / SHELL_STDERR_LOG),
            "workdir": str(workdir),
            "tree": str(tree) if tree is not None else None,
            "cleaned": cleaned,
            "cleanup": cleanup or None,
            # Сохранённое дерево упавшей попытки: адрес работы, которую не
            # успели закоммитить. Пусто — сохранять было нечего.
            "tree_kept": kept_tree or None,
            # Ветка дела и отказ её закрепления: ссылка на работу — факт, а не
            # догадка «коммит где-то есть».
            "branch": branch or None,
            "branch_error": branch_error or None,
            "verdict": verdict.as_payload() if verdict is not None else None,
            # У команды свидетель один — `commit|artifacts`; поле заполняется,
            # когда пакет действительно проверялся (объявлены `artifacts`).
            "witness": verdict.witness if verdict is not None else None,
        },
    }
    if result is not None:
        payload["result"] = result
    ended = _event(
        deal,
        "attempt_ended",
        payload,
        {"stage": stage_id, "attempt": attempt, "outcome": outcome},
        now,
        SHELL_SOURCE,
    )
    with conn:
        journal.append_in_tx(conn, [ended])
        conn.execute(
            "DELETE FROM leases WHERE deal_id = ? AND stage = ? AND pid = ?",
            (str(deal["id"]), stage_id, pid),
        )

    park = None
    if outcome == OUTCOME_FAILED:
        park = park_exhausted(conn, spec, deal, stage, attempt, kind, reason, home=home, clock=clock)

    return {
        "started": True,
        "stage": stage_id,
        "attempt": attempt,
        "outcome": outcome,
        "kind": kind or None,
        "reason": reason,
        "run_id": run_id,
        "workdir": str(workdir),
        "tree": str(tree) if tree is not None else None,
        "base": actual_base,
        "commit": committed or None,
        "exit_code": exit_code,
        "duration_sec": round(duration, 1),
        "verdict": verdict.as_payload() if verdict is not None else None,
        "park": park,
    }


# ----------------------------------------------------------------- публикация
#
# Движок не публикует (`FORBIDDEN_CMD_PREFIXES`, `spec.PUBLISHING_ROLES`): этап с
# ролью `publisher` — обычная попытка с правами, а движок делает две проверки —
# «человек одобрил именно это дерево» до спавна и «заявленный sha есть в origin»
# после отчёта. Правило первого живёт в `dashboard/publish.py`, здесь — git и
# факты журнала.

# Сколько строк `--stat` попадает в вопрос человеку: вопрос — одна строка, а
# подробности — «файлы и числа», а не дифф целиком.
PUBLISH_STAT_LINES = 20

# Сколько знаков коммита в вопросе и в причине: семь сверяются глазами с `git log`.
PUBLISH_SHORT = 12

# Факт «опубликовано» в журнале называет, чем он подтверждён: `git ls-remote` —
# единственный источник, который видит origin, а не слово исполнителя.
PUBLISH_VERIFY = "git ls-remote"

# Причина закрытия публикационного гейта: человек сказал «не публиковать».
NO_APPROVAL = "человек не дал ок"


def publishes(stage: Any) -> bool:
    """Этап действует вовне: его роль из `PUBLISHING_ROLES`.

    Одна константа на валидатор и движок: спека и рантайм обязаны считать
    публикующим одну и ту же роль, иначе проверка в спеке и проверка перед
    спавном разойдутся на первом же `deployer`.
    """
    return str(getattr(stage, "role", "") or "") in spec_module.PUBLISHING_ROLES


def publish_gates(spec: Any, stage: Any) -> list[str]:
    """Гейты в предках этапа — по тому же графу, что проверяет валидатор спеки."""
    ancestors = spec_module.ancestors(spec).get(stage.id, set())
    gates = {
        item.id
        for item in getattr(spec, "stages", ())
        if getattr(item, "kind", "") == spec_module.GATE_KIND
    }
    return sorted(ancestors & gates)


def guarded_stage(spec: Any, gate: Any) -> Optional[Any]:
    """Публикующий этап, которому служит гейт; `None` — гейт спрашивает о другом.

    По этому признаку решается, чем закончится ответ «не публиковать» (дело
    закрывается, а не падает) и что попадёт в вопрос: коммит, `--stat` и отчёт
    ревьюера нужны только тому гейту, за которым стоит публикация.
    """
    for stage in getattr(spec, "stages", ()):
        if publishes(stage) and gate.id in publish_gates(spec, stage):
            return stage
    return None


def resolved_base(spec: Any, stage: Any, events: Iterable[Any]) -> str:
    """База этапа полным sha, если репозиторий на месте; иначе — как её назвала спека.

    Сверка «одобрен X, в дереве Y» сравнивает коммиты, а не ревизии: `origin/master`
    вместо sha означал бы «сравнили две строки». Нерезолвящаяся база возвращается
    как есть — отказ назовёт её словами, а не выдумает совпадение.
    """
    rev = base_of_stage(spec, stage, events)
    repo_text = str(getattr(spec, "repo", "") or "").strip()
    if not rev or not repo_text:
        return rev
    repo = Path(repo_text).expanduser()
    if not repo.is_dir():
        return rev
    return _sha(repo, rev) or rev


def publish_facts(spec: Any, stage: Any, events: Iterable[Any]) -> dict[str, Any]:
    """Что человек видит перед «ок»: коммит, `--stat`, отчёт ревьюера, вердикт.

    Все четыре — из журнала и git, а не из пересказа: коммит — база публикующего
    этапа (то, что уйдёт в origin), `--stat` — его же дифф, отчёт и вердикт — из
    последней попытки ревьюера-предка. Чего нет, то названо словами: пустой
    `--stat` человек прочитал бы как «изменений нет», хотя git просто не ответил.
    """
    facts: dict[str, Any] = {"commit": "", "stat": "—", "review": "не записан", "verdict": "не записан"}
    try:
        commit = resolved_base(spec, stage, events)
    except BaseError as exc:
        facts["commit_error"] = str(exc)
        return facts
    facts["commit"] = commit
    if not commit:
        facts["commit_error"] = "база публикующего этапа пуста: дерево не от чего отвести"
        return facts
    repo_text = str(getattr(spec, "repo", "") or "").strip()
    repo = Path(repo_text).expanduser() if repo_text else None
    if repo is not None and repo.is_dir():
        code, out = _git_stdout(["show", "--stat", "--format=", commit], repo)
        if code == 0 and out.strip():
            lines = [line.rstrip() for line in out.strip().splitlines() if line.strip()]
            if len(lines) > PUBLISH_STAT_LINES:
                lines = lines[:PUBLISH_STAT_LINES] + [f"… ещё {len(lines) - PUBLISH_STAT_LINES} строк"]
            facts["stat"] = "\n".join(lines)
        else:
            facts["stat"] = f"git show --stat {commit[:PUBLISH_SHORT]} не ответил: {out or code}"
    facts.update(_review_facts(spec, stage, events))
    return facts


def _event_seq(event: dict[str, Any]) -> int:
    """Номер события журнала: по нему и только по нему определяется порядок."""
    raw = event.get("id")
    return raw if isinstance(raw, int) else 0


def _review_facts(spec: Any, stage: Any, events: Iterable[Any]) -> dict[str, str]:
    """Отчёт и вердикт ревьюера-предка: ссылка на файл плюс сказанное им словами.

    Путь отчёта — по контракту попытки (`<workdir>/.duty/result.json`), а не
    `<workdir>/result.json`: контракт кладёт отчёт туда, и ссылка на несуществующий
    файл была бы для человека хуже отсутствия ссылки. Вердикт — из исхода попытки
    и резюме отчёта: `reason` у успешной попытки движок не пишет.
    """
    ancestors = spec_module.ancestors(spec).get(stage.id, set())
    reviewers = {
        item.id: item
        for item in getattr(spec, "stages", ())
        if item.id in ancestors and getattr(item, "role", "") == spec_module.REVIEWING_ROLE
    }
    last: Optional[dict[str, Any]] = None
    # Порядок — по номеру события, а не по `ts`: внутри секунды он одинаков.
    ordered = sorted((item for item in events or () if isinstance(item, dict)), key=_event_seq)
    for event in ordered:
        if str(event.get("kind") or "") != "attempt_ended":
            continue
        payload = journal.payload_of(event)
        if str(payload.get("stage") or "") not in reviewers:
            continue
        last = payload
    if last is None:
        return {}
    details = last.get("details")
    details = details if isinstance(details, dict) else {}
    workdir = str(details.get("workdir") or "")
    report = str(Path(workdir) / ".duty" / "result.json") if workdir else ""
    result = last.get("result")
    result = result if isinstance(result, dict) else {}
    said = str(result.get("summary") or last.get("reason") or "").strip()
    outcome = str(last.get("outcome") or "").strip().lower()
    if outcome in failures.OK_STATUSES and not str(last.get("kind") or "").strip():
        verdict = "одобрено" + (f": {said}" if said else "")
    else:
        verdict = f"не одобрено: {said or 'причина не записана'}"
    return {"review": report or "не записан", "verdict": verdict}


def published_claim(spec: Any, result: Any) -> tuple[Optional[dict[str, str]], str]:
    """Заявка публикации, сверенная с origin: (факт, причина отказа).

    Заявка (`published: {sha, branch, mr_url}`) — слова исполнителя; факт даёт
    `git ls-remote origin [ветка]`: движок верит ссылке, а не отчёту. `mr_url`
    остаётся заявкой: он живёт в GitLab, а не в репозитории, и проверять его
    значило бы ходить в сеть из шага движка.

    Заявленный sha сверяется **по префиксу** (`publish.origin_hits`): публикатор
    пишет сокращённую форму, консоль печатает семь знаков, и полное равенство
    объявляло бы честную публикацию «нет в origin». Факт несёт **полный** sha из
    `ls-remote` (`sha` — то, что на самом деле в origin), а заявка рядом
    (`claimed_sha`): видно и факт, и что именно сказал исполнитель. Несколько
    разных коммитов с таким началом — неоднозначность и отказ: «нашли» здесь было
    бы догадкой.
    """
    claim, why = publish.claim(result)
    if claim is None:
        return None, why
    repo_text = str(getattr(spec, "repo", "") or "").strip()
    if not repo_text:
        return None, "публикация заявлена, но не найдена: repo в спеке не задан"
    repo = Path(repo_text).expanduser()
    if not repo.is_dir():
        return None, f"публикация заявлена, но не найдена: репозитория {repo} нет"
    args = ["ls-remote", "origin"] + ([claim["branch"]] if claim["branch"] else [])
    code, out = _git_stdout(args, repo)
    if code != 0:
        return None, f"публикация заявлена, но не найдена: git ls-remote отказал ({out})"
    refs: list[tuple[str, str]] = []
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if sha.strip():
            refs.append((sha.strip(), ref.strip()))
    hits = publish.origin_hits(refs, claim["sha"])
    if not hits:
        tail = f" по ветке {claim['branch']}" if claim["branch"] else ""
        return None, (
            f"публикация заявлена, но не найдена: sha {claim['sha'][:PUBLISH_SHORT]}… "
            f"нет в origin{tail}"
        )
    if len(hits) > 1:
        named = ", ".join(f"{sha[:PUBLISH_SHORT]} ({ref or '—'})" for sha, ref in hits)
        return None, (
            f"публикация заявлена, но sha {claim['sha'][:PUBLISH_SHORT]}… подходит к "
            f"нескольким ссылкам: {named} — назови коммит полнее"
        )
    sha, ref = hits[0]
    return {**claim, "sha": sha, "ref": ref, "claimed_sha": claim["sha"]}, ""


def _published_event(
    deal: Any, stage: Any, attempt: int, fact: dict[str, str], now: int
) -> dict[str, Any]:
    """Факт `published`: что ушло в origin и чем это подтверждено.

    Пишется движком, а не исполнителем: `published` в отчёте — заявка, и фактом
    журнала она становится только после `git ls-remote`. Поэтому в payload есть
    `verified_by`, а `mr_url` помечен как заявленный.
    """
    payload: dict[str, Any] = {
        "stage": stage.id,
        "attempt": int(attempt),
        "sha": fact["sha"],
        "branch": fact["branch"],
        "mr_url": fact["mr_url"],
        "ref": fact.get("ref") or "",
        # Заявка рядом с фактом: `sha` — то, что действительно в origin (полный
        # sha из `ls-remote`), а `claimed_sha` — что сказал исполнитель (обычно
        # сокращённая форма). Без этой пары не видно, сходилась ли заявка сама.
        "claimed_sha": fact.get("claimed_sha") or fact["sha"],
        "verified_by": PUBLISH_VERIFY,
    }
    return _event(
        deal,
        "published",
        payload,
        {"stage": stage.id, "attempt": int(attempt), "sha": fact["sha"]},
        now,
    )


def refuse_publish(
    conn: sqlite3.Connection,
    deal: Any,
    stage: Any,
    attempt: int,
    approval: Any,
    *,
    clock: Callable[[], float],
) -> Optional[dict[str, Any]]:
    """Публикация без «ок» на это дерево: попытка не поднимается, факт в журнале.

    Проверка стоит **до** лизинга и спавна — как отказ потолка
    (`refuse_budget`): лизинг означал бы этап за процессом, которого нет, а
    `attempt_started` — попытку, которую никто не начинал. Поэтому отказ
    пишется `attempt_ended` без начала.

    Класс — `contract`: это не сбой исполнителя, а невыполненное условие
    движка («одобрен один коммит, а публикуется другой»). Парковки нет: спрашивать
    «продолжать?» бессмысленно — продолжать можно только повторным вопросом
    человеку, и он задаётся гейтом, а не парковкой.
    """
    if approval is None or approval.ok:
        return None
    now = _now_sec(int(clock()))
    run_id = f"{stage.id}#{int(attempt)}"
    ended = _event(
        deal,
        "attempt_ended",
        {
            "run_id": run_id,
            "stage": stage.id,
            "attempt": int(attempt),
            "outcome": OUTCOME_FAILED,
            "kind": FAIL_CONTRACT,
            "reason": approval.reason,
            "details": {
                "refused": True,
                "stage_kind": getattr(stage, "kind", STAGE_KIND_AGENT),
                "publish": {
                    "gate": approval.gate,
                    "question_id": approval.question_id,
                    "approved_commit": approval.commit,
                },
            },
        },
        {"stage": stage.id, "attempt": int(attempt), "refused": True},
        now,
    )
    journal.append(conn, [ended])
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind="needs_input",
        reason=f"этап {stage.id}: {approval.reason}",
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        details={
            "stage": stage.id,
            "attempt": int(attempt),
            "publish": {"gate": approval.gate, "question_id": approval.question_id},
        },
        now=now,
    )
    return {
        "started": False,
        "refused": True,
        "stage": stage.id,
        "attempt": int(attempt),
        "outcome": OUTCOME_FAILED,
        "kind": FAIL_CONTRACT,
        "reason": approval.reason,
    }


# ---------------------------------------------------------------------- гейт


# Класс отказа гейта, когда ответ человека не входит в `allow`: то же, что
# «ревьюер сказал changes» — работа остановлена решением, а не сбоем процесса.
GATE_REFUSED_KIND = FAIL_CONTRACT

# Сколько знаков ответа цитируется в причине: цитата нужна, чтобы в журнале было
# видно, **что именно** ответил человек, но весь ответ — факт пула, а не журнала.
GATE_QUOTE_LIMIT = 80


def gate_wait_sec(stage: Any) -> float:
    """Сколько ждём человека на гейте: `budget.minutes` или срок вопроса (`ask`).

    `budget.minutes` у гейта — не бюджет процесса (его нет), а срок ответа: по нему
    вопрос уходит в пул и по нему же он однажды истекает. Не задан — тот же срок,
    что у обычного некритичного вопроса (`ask.ASK_TIMEOUT_HOURS`).
    """
    budget = getattr(stage, "budget", None)
    minutes = getattr(budget, "minutes", None) if budget is not None else None
    if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0:
        return float(minutes) * 60.0
    return float(ask.ASK_TIMEOUT_HOURS) * 3600.0


def gate_allow(stage: Any) -> tuple[str, ...]:
    """Ответы, которые считаются продолжением: `allow`, а без него — все `options`.

    Умолчание именно «все объявленные варианты», а не «любой текст»: гейт судит
    ответ по объявлению, и свободный текст продолжением не считается.
    """
    declared = tuple(getattr(stage, "allow", ()) or ())
    if declared:
        return declared
    ask_decl = getattr(stage, "ask", None)
    return tuple(getattr(ask_decl, "options", ()) or ())


def gate_answer_allowed(stage: Any, answer: Optional[str]) -> Optional[str]:
    """Вариант из `allow`, совпавший с ответом; `None` — ответ не в списке.

    Сравнение по точному тексту без пробелов по краям: варианты ответа объявлены
    спекой, и «да» в пуле — это ровно «да», а не «наверное, да». Приведение
    регистра или склейка слов сделали бы список допустимых ответов догадкой.
    """
    text = str(answer or "").strip()
    return next((item for item in gate_allow(stage) if item.strip() == text), None)


def _gate_quote(answer: Optional[str]) -> str:
    """Ответ человека цитатой для журнала: одна строка, с многоточием на длинном."""
    text = " ".join(str(answer or "").split())
    if not text:
        return "—"
    return text if len(text) <= GATE_QUOTE_LIMIT else text[:GATE_QUOTE_LIMIT] + "…"


def _gate_exchange(
    events: Iterable[Any], stage_id: str
) -> tuple[Optional[str], Optional[dict[str, Any]], int]:
    """Последний вопрос этого гейта, ответ на него и время ответа.

    Вопрос адресуется этапу в `payload.routing` (`ask()` пишет событие на уровне
    дела), поэтому свой вопрос ищется по адресу, а не по «последнему вопросу
    журнала»: у дела может быть вопрос другого этапа. Ответ сопоставляется по
    `question_id` — чужой ответ гейт не судит.

    Время ответа возвращается, потому что суд не может стоять в журнале **раньше**
    того, что он судит. Вопрос и ответ пишет `ask()` живыми часами
    (`time.time()`), а движок — своими (`clock`); при расхождении источников
    `attempt_ended` оказался бы в журнале до `question_answered`, и fold вернул бы
    гейт в ожидание уже после решения — прогон крутился бы на одном этапе.
    """
    asked: Optional[str] = None
    answers: dict[str, tuple[dict[str, Any], int]] = {}
    for event in sorted(
        (item for item in events or () if isinstance(item, dict)),
        key=lambda item: int(item.get("id") or 0),
    ):
        kind = str(event.get("kind") or "")
        payload = journal.payload_of(event)
        if kind == "question_asked":
            routing = payload.get("routing")
            if not isinstance(routing, dict) or str(routing.get("stage") or "") != stage_id:
                continue
            asked = _question_id(payload)
            continue
        if kind == "question_answered":
            number = _question_id(payload)
            if number is not None:
                answers[number] = (payload, _int(event.get("ts")) or 0)
    if asked is None:
        return None, None, 0
    answer, at = answers.get(asked, (None, 0))
    return asked, answer, at


def _question_id(payload: dict[str, Any]) -> Optional[str]:
    """`question_id` строкой: пул отдаёт и числом, и строкой; пусто — `None`."""
    raw = payload.get("question_id")
    if raw is None or raw == "":
        return None
    return str(raw)


def run_gate_stage(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    stage_id: str,
    attempt: int,
    *,
    home: Path,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Одна передача этапа-гейта: вопрос человеку или решение по его ответу.

    Процесса здесь нет вовсе, и лизинг не берётся: держать этап некому — гейт не
    работает, а ждёт. Первая передача задаёт вопрос единственным каналом `ask()`
    (`ask.q`/`ask.options` спеки) и оставляет дело человеку; после ответа,
    доехавшего до журнала (`ask.sync_answers` в драйвере), следующая передача
    судит ответ по `allow`. «Да» закрывает этап, всё остальное — отказ класса
    `contract` с цитатой ответа: гейт не решает за человека, что «нет» значит
    «продолжаем».

    Факты журнала — ровно два вида, оба уже в закрытом наборе: `question_asked`
    (его пишет `ask()`) и `attempt_ended` (исход гейта). `attempt_started` не
    пишется: он означал бы процесс, которого нет, и уборка осиротевших попыток
    закрыла бы ждущий человека гейт как мёртвый.
    """
    clock = clock or time.time
    # Гейт планом тоже добавляется (`charter.allowed_kinds`), поэтому этап ищется
    # в графе журнала, а не в файле.
    spec = transit.graph_spec(spec, journal.read_events(conn, str(deal["id"])))
    stage = next((item for item in spec.stages if item.id == stage_id), None)
    if stage is None:
        raise RunnerError(f"этапа {stage_id!r} нет в спеке {getattr(spec, 'path', '')}")
    attempt = int(attempt)
    if attempt < 1:
        raise RunnerError(f"номер попытки {attempt}: попытки считаются с 1")
    ask_decl = getattr(stage, "ask", None)
    if ask_decl is None or not str(getattr(ask_decl, "q", "")).strip():
        raise RunnerError(f"этап {stage_id!r} с kind: gate без ask.q: гейту нечего спрашивать")

    events = journal.read_events(conn, str(deal["id"]))
    # Гейт перед публикацией называется отдельно: его вопрос несёт коммит и
    # `--stat`, а ответ «не публиковать» закрывает дело, а не роняет его.
    guarded = guarded_stage(spec, stage)
    asked, answer, answered_at = _gate_exchange(events, stage_id)
    if asked is not None and answer is None:
        # Вопрос уже задан и ответа ещё нет: второй вопрос не создаётся (его
        # отклонил бы и пул), а шаг честно говорит, что ждёт.
        return {
            "started": False,
            "stage": stage_id,
            "attempt": attempt,
            "question_id": int(asked),
            "reason": f"гейт: вопрос #{asked} уже задан — ответа ещё нет",
        }
    if asked is None:
        return _gate_ask(
            conn, spec, deal, stage, attempt, home=home, clock=clock, guarded=guarded
        )
    return _gate_judge(
        conn,
        deal,
        stage,
        attempt,
        asked,
        answer or {},
        home=home,
        clock=clock,
        answered_at=answered_at,
        guarded=guarded,
    )


def _gate_ask(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    stage: Any,
    attempt: int,
    *,
    home: Path,
    clock: Callable[[], float],
    guarded: Optional[Any] = None,
) -> dict[str, Any]:
    """Задать вопрос гейта: пул, факт `question_asked`, дело в `blocked`.

    Лизинг не берётся и статус `running` не ставится: ждущий человек этап ничего
    не держит, и «идёт попытка» здесь было бы ложью. Вопрос адресуется человеку
    (`routing.route` с властной разметкой: гейт — решение, а не справка), а срок
    ответа — `budget.minutes` этапа.

    У гейта перед публикацией в вопрос едет то, что человек иначе искал бы руками:
    коммит, его `--stat`, отчёт и вердикт ревьюера (`publish_facts`). Коммит
    попадает и в `routing.approved_commit` — по нему потом сверяется дерево.
    """
    facts: dict[str, Any] = {}
    if guarded is not None:
        facts = publish_facts(spec, guarded, journal.read_events(conn, str(deal["id"])))
    question_id = _ask_gate(conn, deal, stage, attempt, home=home, facts=facts)
    now = _now_sec(int(clock()))
    approved = str(facts.get("commit") or "")
    headline = f"гейт {stage.id}: вопрос #{question_id} человеку"
    if approved:
        headline += f", одобряется commit {approved[:PUBLISH_SHORT]}"
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind="needs_input",
        reason=(
            f"{headline} — без ответа "
            f"(варианты: {', '.join(gate_allow(stage)) or '—'}; пул /imsg)"
        ),
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        details={"stage": stage.id, "attempt": int(attempt), "question_id": int(question_id)},
        now=now,
    )
    return {
        "started": True,
        "stage": stage.id,
        "attempt": int(attempt),
        "question_id": int(question_id),
        "reason": f"гейт: вопрос #{question_id} задан человеку, ждём ответа",
    }


def _ask_gate(
    conn: sqlite3.Connection,
    deal: Any,
    stage: Any,
    attempt: int,
    *,
    home: Path,
    facts: Optional[dict[str, Any]] = None,
) -> int:
    """Вопрос гейта в пул через `ask()` — единственный канал вопросов.

    Возвращает номер вопроса. Ответ на живом вопросе этого дела (`ask()` его
    подхватывает) — тот же номер: второй вопрос человеку не задаётся.

    `facts` (только у гейта перед публикацией) разворачивается в теле вопроса:
    короткая строка остаётся `ask.q` из спеки, а коммит, `--stat`, отчёт и вердикт
    идут ниже — человеку не нужно открывать репозиторий, чтобы понять, что он
    одобряет.
    """
    ask_decl = stage.ask
    question = str(ask_decl.q).strip()
    options = [str(option) for option in ask_decl.options]
    address = routing.route(stage, {"kind": "authority"}, 0)
    lines = [question]
    if facts:
        commit = str(facts.get("commit") or "")
        if commit:
            lines += ["", f"одобряется commit {commit[:PUBLISH_SHORT]}"]
        else:
            lines += ["", f"коммит не назван: {facts.get('commit_error') or 'причина не записана'}"]
        lines += ["", str(facts.get("stat") or "—")]
        lines += ["", f"отчёт ревьюера: {facts.get('review') or 'не записан'}"]
        lines += [f"вердикт: {facts.get('verdict') or 'не записан'}"]
    lines += [
        "",
        f"Варианты ответа: {', '.join(options)}. Продолжение — только ответ "
        f"из списка: {', '.join(gate_allow(stage))}.",
    ]
    asked = ask.ask(
        conn,
        {**deal, "home": Path(home)},
        question,
        "\n".join(lines),
        options,
        critical=False,
        timeout=gate_wait_sec(stage),
        routing={
            **address.as_payload(),
            "stage": stage.id,
            "attempt": int(attempt),
            "approved_commit": str((facts or {}).get("commit") or ""),
        },
    )
    return int(asked.question_id)


# Варианты ответа на гейт плана (`docs/ENGINE-1.0.md`, раздел 5): «принять» —
# предложение становится графом; «урезать» и «остановить» — отказ с причиной,
# после которого лид перепланирует (раунд реплана считает хартия).
PLAN_GATE_OPTIONS = ("принять", "урезать", "остановить")
PLAN_GATE_ACCEPT = PLAN_GATE_OPTIONS[0]


def _ordered_events(events: Iterable[Any]) -> list[dict[str, Any]]:
    """События по номеру журнала: порядок решает журнал, а не порядок входа."""
    return sorted(
        (event for event in events or () if isinstance(event, dict)),
        key=lambda item: int(_int(item.get("id")) or 0),
    )


def _pending_plan(events: Iterable[Any]) -> Optional[tuple[int, str, list[Any]]]:
    """Предложение, которое ещё не решено: `(rev, source_stage, stages)`.

    Читается по журналу, а не по памяти прогона: гейт плана может ставить другой
    процесс, а «что решено» — факт, а не состояние в памяти.
    """
    proposals: dict[int, dict[str, Any]] = {}
    decided: set[int] = set()
    for event in _ordered_events(events):
        payload = journal.payload_of(event)
        if str(payload.get("stage") or ""):
            continue  # факт этапа, а не факт плана
        kind = str(event.get("kind") or "")
        rev = int(_int(payload.get("rev")) or 0)
        if kind == transit.PLAN_PROPOSED and rev > 0 and isinstance(payload.get("stages"), list):
            proposals[rev] = payload
        elif kind in (transit.PLAN_ACCEPTED, transit.PLAN_REJECTED) and rev > 0:
            decided.add(rev)
    for rev in sorted(proposals, reverse=True):
        if rev in decided:
            continue
        payload = proposals[rev]
        return rev, str(payload.get("source_stage") or ""), list(payload.get("stages") or [])
    return None


def _plan_gate_fact(events: Iterable[Any], rev: int) -> Optional[dict[str, Any]]:
    """Открытый гейт этой ревизии: последний `plan_gate` с тем же `rev`."""
    found: Optional[dict[str, Any]] = None
    for event in _ordered_events(events):
        if str(event.get("kind") or "") != transit.PLAN_GATE:
            continue
        if int(_int(journal.payload_of(event).get("rev")) or 0) != rev:
            continue
        found = event
    return found


def _plan_gate_answer(events: Iterable[Any], question_id: Any, home: Path) -> Optional[tuple[bool, str]]:
    """Ответ человека на вопрос гейта: `(согласие, слово)`; `None` — ответа нет.

    Журнал хранит только длину ответа, а «принять» отличается от «урезать»
    текстом, поэтому слово читается из пула (`ask.answer_of`) — тем же путём,
    которым его увидел человек. Срок без ответа и ответ без текста согласием не
    считаются: тишина — не решение.
    """
    if question_id is None:
        return None
    asked = str(question_id)
    for event in reversed(_ordered_events(events)):
        if str(event.get("kind") or "") != "question_answered":
            continue
        payload = journal.payload_of(event)
        if str(_question_id(payload) or "") != asked:
            continue
        if str(payload.get("answered_via") or "").strip().lower() == "expired":
            return False, ""
        if int(_int(payload.get("answer_len")) or 0) <= 0:
            return False, ""
        word = str(ask.answer_of(Path(home), question_id) or "").strip()
        return word.strip().lower().startswith(PLAN_GATE_ACCEPT), word
    return None


def _last_plan_attempt(events: Iterable[Any], stage_id: str) -> int:
    """Номер попытки этапа плана: по ней вопрос адресуется тому, кто спросил."""
    attempt = 0
    for event in _ordered_events(events):
        payload = journal.payload_of(event)
        if str(payload.get("stage") or "") != stage_id:
            continue
        if str(event.get("kind") or "") not in ("attempt_started", "attempt_ended", transit.PLAN_PROPOSED):
            continue
        attempt = max(attempt, int(_int(payload.get("attempt")) or 0))
    return attempt


def _ask_plan_gate(
    conn: sqlite3.Connection,
    deal: Any,
    stage_id: str,
    attempt: int,
    rev: int,
    why: str,
    *,
    home: Path,
    now: int,
) -> dict[str, Any]:
    """Вопрос гейта плана в пул (`ask()`) — единственным каналом вопросов.

    Адрес — властный (`authority`): принять граф — решение, а не справка. В
    `routing` едут этап и попытка плана, иначе ответ без адреса transit отнёс бы
    последней попытке журнала, а это может быть не план.
    """
    question = f"План дела (ревизия {rev}) больше порога хартии: принять?"
    lines = [
        question,
        "",
        f"порог: {why}",
        f"Варианты ответа: {', '.join(PLAN_GATE_OPTIONS)}.",
        f"«{PLAN_GATE_ACCEPT}» — предложение станет графом дела; остальные ответы —",
        "отказ с причиной, и лид перепланирует (раунд реплана считает хартия).",
    ]
    address = routing.route(None, {"kind": "authority"}, 0)
    asked = ask.ask(
        conn,
        {**deal, "home": Path(home)},
        question,
        "\n".join(lines),
        list(PLAN_GATE_OPTIONS),
        critical=False,
        routing={**address.as_payload(), "stage": stage_id, "attempt": int(attempt), "plan_rev": rev},
    )
    question_id = int(asked.question_id)
    return _event(
        deal,
        transit.PLAN_GATE,
        {"rev": rev, "reason": why, "question_id": question_id},
        {"stage": stage_id, "attempt": int(attempt), "rev": rev, "question_id": question_id},
        now,
    )


def plan_gate(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    *,
    events: Iterable[Any],
    home: Path,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Гейт плана: предложение дороже порога — вопрос человеку и решение по ответу.

    Образец — гейт-этап (`_gate_ask`/`_gate_exchange`): вопрос задаётся один раз
    через `ask()`, а ответ приходит тем же `ask.sync_answers`, что и ответы
    гейтов. Разница одна: у гейта плана нет своего этапа с `ask` — спрашивать
    нечем, кроме хартии, поэтому вопрос собирается здесь
    (`docs/ENGINE-1.0.md`, раздел 5).

    «Принять» пишет `plan_accepted` с `by: human` — движок этого решения не
    принимал. Любой другой ответ и молчание по сроку пишут `plan_rejected` с
    причиной: лид видит, что именно человек не принял, а раунд реплана считается
    тем же правилом, что и у валидатора.

    Возвращает строку шага: `rev`, `question_id`, `asked` (вопрос задан этим
    вызовом), `answered`, `accepted`, `rejected` и `reason`.
    """
    clock = clock or time.time
    listed = [event for event in events or () if isinstance(event, dict)]
    row: dict[str, Any] = {
        "rev": 0,
        "stage": "",
        "attempt": 0,
        "question_id": None,
        "asked": False,
        "answered": False,
        "accepted": False,
        "rejected": False,
        "reason": "",
    }
    pending = _pending_plan(listed)
    if pending is None:
        row["reason"] = "предложения плана нет: гейту плана нечего решать"
        return row
    rev, stage_id, raw_stages = pending
    row["rev"] = rev
    attempt = _last_plan_attempt(listed, stage_id)
    row.update(stage=stage_id, attempt=int(attempt))
    gate = _plan_gate_fact(listed, rev)
    if gate is None:
        parsed = _parsed_plan(spec, raw_stages, listed)
        why = plan_gate_reason(spec, parsed, transit.graph(spec, listed) + parsed)
        now = _now_sec(int(clock()))
        gate = _ask_plan_gate(
            conn,
            deal,
            stage_id or transit.PLAN_STAGE,
            attempt,
            rev,
            why or "план дороже порога хартии",
            home=home,
            now=now,
        )
        _append_plan_facts(conn, [gate])
        question_id = _question_id(journal.payload_of(gate))
        row.update(
            asked=True,
            question_id=question_id,
            reason=f"план ревизии {rev}: вопрос #{question_id} человеку — ждём ответа",
        )
        return row
    question_id = _question_id(journal.payload_of(gate))
    row["question_id"] = question_id
    answer = _plan_gate_answer(listed, question_id, Path(home))
    if answer is None:
        row["reason"] = f"план ревизии {rev}: вопрос #{question_id} человеку — ответа нет"
        return row
    row["answered"] = True
    accepted, word = answer
    now = _now_sec(int(clock()))
    if accepted:
        row.update(accepted=True, reason=f"план ревизии {rev} принят человеком")
        fact = _event(
            deal,
            transit.PLAN_ACCEPTED,
            {"rev": rev, "by": "human"},
            {"stage": stage_id, "attempt": int(attempt), "rev": rev, "by": "human"},
            now,
        )
    else:
        why = f"человек: {word}" if word else "человек не ответил на гейт плана"
        row.update(rejected=True, reason=f"план ревизии {rev} отклонён: {why}")
        fact = _event(
            deal,
            transit.PLAN_REJECTED,
            {"rev": rev, "why": why, "issues": [], "source_stage": stage_id},
            {"stage": stage_id, "attempt": int(attempt), "rev": rev},
            now,
        )
    _append_plan_facts(conn, [fact])
    return row


def _append_plan_facts(conn: sqlite3.Connection, facts: list[dict[str, Any]]) -> None:
    """Дописать факты плана одной транзакцией: журнал — единственная запись."""
    if not facts:
        return
    with conn:
        journal.append_in_tx(conn, facts)


def _parsed_plan(spec: Any, raw_stages: list[Any], events: Iterable[Any]) -> tuple[Any, ...]:
    """Этапы предложения тем же разбором, что у движка: порог считается по ним."""
    charter = getattr(spec, "charter", None) or spec_module.Charter()
    stages, _issues = spec_module.parse_plan(
        raw_stages, charter=charter, base_ids=[stage.id for stage in transit.graph(spec, events)]
    )
    return tuple(stages)


def _gate_judge(
    conn: sqlite3.Connection,
    deal: Any,
    stage: Any,
    attempt: int,
    question_id: str,
    answer: dict[str, Any],
    *,
    home: Path,
    clock: Callable[[], float],
    answered_at: int = 0,
    guarded: Optional[Any] = None,
) -> dict[str, Any]:
    """Судить ответ человека: `allow` — продолжение, всё остальное — отказ контракта.

    Текст ответа живёт в пуле, а не в журнале (`question_answered` хранит
    `answer_len`), поэтому читается оттуда — только чтение (`ask.answer_of`).
    Истёкший вопрос ответом не считается: это сделал не человек, и решение по
    такому «ответу» было бы выдумкой (по сроку дело паркует `transit`).

    Время факта — не раньше ответа (`answered_at`): суд, записанный раньше того,
    что он судит, fold применил бы до ответа и вернул гейт в ожидание.

    `guarded` — публикующий этап за этим гейтом: «не публиковать» тогда не сбой, а
    решение, и дело закрывается фактом `completed` с причиной «человек не дал ок»
    (`close_deal`). Иначе публикация не случилась бы, а дело висело бы отказом,
    хотя человек сказал ровно то, что хотел.
    """
    if _gate_expired(answer):
        return {
            "started": False,
            "stage": stage.id,
            "attempt": int(attempt),
            "question_id": int(question_id),
            "reason": f"гейт: вопрос #{question_id} закрыт по сроку — человек не ответил",
        }
    text = ask.answer_of(Path(home), question_id)
    matched = gate_answer_allowed(stage, text)
    allowed = ", ".join(gate_allow(stage)) or "—"
    if matched is not None:
        outcome, kind = OUTCOME_DONE, ""
        reason = f"гейт: ответ «{matched}» входит в allow [{allowed}]"
    else:
        outcome, kind = OUTCOME_FAILED, GATE_REFUSED_KIND
        reason = f"гейт: ответ «{_gate_quote(text)}» вне allow [{allowed}] — отказ контракта"
        if guarded is not None:
            reason = (
                f"гейт: {NO_APPROVAL} — ответ «{_gate_quote(text)}» вне allow [{allowed}], "
                "публикации не будет"
            )

    now = _now_sec(max(int(clock()), int(answered_at or 0)))
    ended = _event(
        deal,
        "attempt_ended",
        {
            "run_id": f"{stage.id}#{int(attempt)}",
            "stage": stage.id,
            "attempt": int(attempt),
            "outcome": outcome,
            "kind": kind or None,
            "reason": reason,
            "details": {
                "stage_kind": STAGE_KIND_GATE,
                "question_id": int(question_id),
                "answer": _gate_quote(text),
                "allow": list(gate_allow(stage)),
            },
        },
        {"stage": stage.id, "attempt": int(attempt), "outcome": outcome},
        now,
    )
    journal.append(conn, [ended])
    if outcome == OUTCOME_FAILED and guarded is not None:
        # «Не публиковать» — решение человека, а не сбой движка: дело
        # закрывается без публикации, и причина — ровно слова решения. Отказ
        # контракта остаётся фактом этапа (`attempt_ended` выше), так что
        # поверхность видит и ответ, и чем всё кончилось.
        try:
            deals.close_deal(
                conn,
                str(deal["id"]),
                reason=f"{NO_APPROVAL}: ответ «{_gate_quote(text)}» вне allow [{allowed}]",
                cause=deals.CLOSE_CAUSE_HUMAN,
                source=ENGINE_SOURCE,
                now=now,
            )
        except deals.DealError:
            # Дело уже закрыто или держит человек — закрывать нечего, но и
            # терять факт ответа нельзя: он ниже, в `blocked`.
            _gate_refused_block(conn, deal, stage, attempt, question_id, reason, now)
    elif outcome == OUTCOME_FAILED:
        # Отказ человека виден поверхности: дело останавливается его решением, и
        # без факта журнала экран показывал бы «status=running без живого факта».
        # Парковкой это не делается: парковка спросила бы человека второй раз о
        # том же самом, а он уже ответил.
        _gate_refused_block(conn, deal, stage, attempt, question_id, reason, now)
    return {
        "started": True,
        "stage": stage.id,
        "attempt": int(attempt),
        "outcome": outcome,
        "kind": kind or None,
        "reason": reason,
        "question_id": int(question_id),
        "answer": text,
        "stage_kind": STAGE_KIND_GATE,
        "closed": outcome == OUTCOME_FAILED and guarded is not None,
    }


def _gate_refused_block(
    conn: sqlite3.Connection,
    deal: Any,
    stage: Any,
    attempt: int,
    question_id: str,
    reason: str,
    now: int,
) -> None:
    """Отказ гейта виден поверхности как открытый блок, а не «running без факта»."""
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind="needs_input",
        reason=f"гейт {stage.id}: {reason} — дальше дело не идёт",
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        details={"stage": stage.id, "attempt": int(attempt), "question_id": int(question_id)},
        now=now,
    )


def _gate_expired(answer: dict[str, Any]) -> bool:
    """Закрыт ли вопрос по сроку: тот же признак, что у `transit._expired`.

    `answered_via='expired'` ставит наш же синк ответов, а пустой ответ пул
    оставляет у вопроса, закрытого по сроку: человека за ним не было.
    """
    if str(answer.get("answered_via") or "").strip().lower() == ask.EXPIRED_VIA:
        return True
    return _int(answer.get("answer_len")) == 0


# ---------------------------------------------------------------------- шаг


def run_stage(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    stage_id: str,
    attempt: int,
    *,
    home: Path,
    answer: Optional[str] = None,
    spawn: Optional[Callable[..., Any]] = None,
    clock: Optional[Callable[[], float]] = None,
    monotonic: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Одна попытка этапа: лизинг, дерево, контракт, процесс, факты в журнал.

    Возвращает отчёт о шаге: `started` (попытка действительно пошла),
    `outcome`/`kind`/`reason` — как в событии, `question_id` — если попытка
    задала вопрос, `workdir`/`tree` — где она работала.

    `answer` — ответ человека на вопрос прошлой попытки: с ним процесс
    продолжается резюмом (`RESUME-OK`), без него начинается новая сессия.
    """
    spawner = spawn or spawn_process
    clock = clock or time.time
    monotonic = monotonic or time.monotonic
    # Этап ищется в графе дела, а не в файле: этап, добавленный принятым планом,
    # в `duty.yaml` не объявлен (`docs/ENGINE-1.0.md`, раздел 3).
    spec = transit.graph_spec(spec, journal.read_events(conn, str(deal["id"])))
    stage = next((item for item in spec.stages if item.id == stage_id), None)
    if stage is None:
        raise RunnerError(f"этапа {stage_id!r} нет в спеке {getattr(spec, 'path', '')}")
    attempt = int(attempt)
    if attempt < 1:
        raise RunnerError(f"номер попытки {attempt}: попытки считаются с 1")
    if getattr(stage, "kind", STAGE_KIND_AGENT) == STAGE_KIND_SHELL:
        # Вид этапа — выбор спеки, а не режим движка: попытку исполняет команда,
        # и её шаг живёт отдельной функцией (`run_shell_stage`), чтобы ветка
        # агента осталась ровно той же, что была до 0.0.5.
        return run_shell_stage(
            conn, spec, deal, stage_id, attempt, home=home, spawn=spawn, clock=clock, monotonic=monotonic
        )
    if getattr(stage, "kind", STAGE_KIND_AGENT) == STAGE_KIND_GATE:
        # Гейт — тоже отдельная ветка: ни процесса, ни дерева, ни лизинга у него
        # нет, и общий путь агента для него был бы неверен в каждой строке.
        return run_gate_stage(conn, spec, deal, stage_id, attempt, home=home, clock=clock)
    ttl = ttl_sec(stage)
    deadline_sec = ttl
    engine_pid = os.getpid()
    workdir = attempt_dir(home, deal, stage_id, attempt)
    run_id = f"{stage_id}#{attempt}"

    # Публикация без «ок» на это дерево (0.0.5) проверяется до лизинга и спавна:
    # человек одобрил коммит, а в дереве уже другой — публиковать нечего.
    approval: Optional[Any] = None
    if publishes(stage):
        events = journal.read_events(conn, str(deal["id"]))
        gates = publish_gates(spec, stage)
        try:
            current = resolved_base(spec, stage, events)
        except BaseError as exc:
            approval = publish.Approval(ok=False, reason=str(exc))
        else:
            approval = publish.approval(events, gates, current=current)
        refused = refuse_publish(conn, deal, stage, attempt, approval, clock=clock)
        if refused is not None:
            return refused

    # Потолок дела целиком (0.0.5) проверяется до лизинга и до спавна: расход
    # считается по журналу (`dashboard/budget.py`), поэтому переживает рестарт и
    # не требует счётчика в памяти. Исчерпан — попытка не поднимается вовсе,
    # а отказ ложится фактом (`refuse_budget`).
    spent = budget.refusal(spec, journal.read_events(conn, str(deal["id"])))
    refused = refuse_budget(conn, spec, deal, stage, attempt, spent, clock=clock)
    if refused is not None:
        return refused

    held_at = _now_sec(int(clock()))
    if not lease(
        conn, deal=deal, stage=stage_id, attempt=attempt, pid=engine_pid, ttl=ttl, now=held_at
    ):
        held = lease_of(conn, deal, stage_id) or {}
        return {
            "started": False,
            "stage": stage_id,
            "attempt": attempt,
            "reason": f"этап уже за процессом pid={held.get('pid')}, срок до {held.get('expires_at')}",
        }

    stage_base = str(spec.base or "")
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        # Журнал читается один раз на шаг: из него же берётся база этапа и отчёты
        # предшественников, и два прохода дали бы два ответа на один вопрос.
        events = journal.read_events(conn, str(deal["id"]))
        # База этапа, а не база дела: у первого этапа — `spec.base`, у следующего —
        # коммит предшественника (`base_of_stage`). Разошедшиеся предшественники —
        # отказ контракта, а не инфраструктуры: дерево от одного из них потеряло
        # бы работу другого.
        stage_base = base_of_stage(spec, stage, events)
        tree, cwd = prepare_tree(spec, stage, workdir, base=stage_base)
    except (RunnerError, OSError) as exc:
        kind = FAIL_CONTRACT if isinstance(exc, BaseError) else FAIL_INFRA
        release(conn, deal=deal, stage=stage_id, pid=engine_pid)
        now = _now_sec(int(clock()))
        event = _event(
            deal,
            "attempt_ended",
            {
                "run_id": run_id,
                "stage": stage_id,
                "attempt": attempt,
                "outcome": OUTCOME_FAILED,
                "kind": kind,
                "reason": str(exc),
                "details": {"workdir": str(workdir), "tree": None, "base": stage_base},
            },
            {"stage": stage_id, "attempt": attempt, "outcome": OUTCOME_FAILED},
            now,
        )
        journal.append(conn, [event])
        park = park_exhausted(
            conn, spec, deal, stage, attempt, kind, str(exc), home=home, clock=clock
        )
        return {
            "started": True,
            "stage": stage_id,
            "attempt": attempt,
            "outcome": OUTCOME_FAILED,
            "kind": kind,
            "reason": str(exc),
            "workdir": str(workdir),
            "tree": None,
            "base": stage_base,
            "park": park,
        }

    # Фактическая база дерева: `git rev-parse HEAD` после отвода. База в спеке
    # бывает ревизией (`main`, `HEAD`), а в журнале нужен коммит — иначе разбор
    # «от чего работал этап» снова требовал бы чтения кода.
    actual_base = stage_base
    if tree is not None:
        sha = _sha(Path(tree), "HEAD")
        if sha:
            actual_base = sha

    previous = previous_reports(conn, deal, spec, stage_id, events=events)

    if answer is None:
        # Ответ роли на вопрос прошлой попытки: строки пула у такого вопроса нет,
        # текст ответа живёт в журнале, и продолжение берёт его оттуда — иначе
        # лид ответил бы, а этап продолжил бы работу, не увидев ответа.
        answer = role_answer_before(events, stage_id, attempt)
    session_id = last_session(conn, deal, stage_id) if answer is not None else ""
    contract = build_contract(
        spec,
        deal,
        stage,
        attempt,
        workdir=workdir,
        tree=tree,
        cwd=cwd,
        previous=previous,
        deadline_sec=deadline_sec,
        answer=answer,
        session_id=session_id,
        approved=None
        if approval is None
        else {
            "commit": approval.commit,
            "gate": approval.gate,
            "question_id": approval.question_id,
            "answer": approval.answer,
        },
        events=events,
    )
    contract_file = write_contract(contract_path(workdir, stage_id, attempt), contract)
    query_file = contract_file
    if session_id:
        # Продолжение: вход — ответ человека, а не второй контракт. Контракт уже
        # записан и назван здесь же, чтобы попытка могла перечитать его целиком.
        query_file = write_answer(answer_path(workdir, stage_id, attempt), answer or "", contract_file)

    result_file = result_path(workdir)
    if result_file.exists():
        # Свой отчёт пишет только эта попытка: остаток прошлой (при совпавшем
        # номере) читался бы как её отчёт.
        result_file.unlink()
    command = spawn_command(stage.role, query_file, session_id)
    now = _now_sec(int(clock()))
    try:
        process = spawner(command, cwd)
    except OSError as exc:
        # Процесс не поднялся: лизинг отпускаем, дерево убираем — иначе
        # следующая попытка упрётся в чужой каталог, которого никто не занимает.
        release(conn, deal=deal, stage=stage_id, pid=engine_pid)
        if stage.workspace == "worktree":
            remove_tree(spec, stage, workdir)
        raise RunnerError(f"спавн попытки не удался: {exc}") from exc

    pid = int(process.pid)
    started = _event(
        deal,
        "attempt_started",
        {
            "run_id": run_id,
            "stage": stage_id,
            "attempt": attempt,
            "pid": pid,
            "cmd": command,
            "cwd": str(cwd),
            "workdir": str(workdir),
            "tree": str(tree) if tree is not None else None,
            # Фактическая база дерева (rev): от чего этап работал. Без неё разбор
            # «второй этап не увидел работу первого» снова требовал бы чтения кода.
            "base": actual_base,
            "lease_expires": now + ttl,
            "contract_path": str(contract_file),
            "contract": contract,
            "session_id": session_id or None,
        },
        {"stage": stage_id, "attempt": attempt},
        now,
    )
    try:
        # Одна транзакция: лизинг переезжает к процессу, дело становится
        # `running`, событие ложится рядом. Сбой записи откатывает всё — тогда
        # процесс останавливается, а лизинг возвращается движку.
        # `deals._set_status` — та же запись строки, что у мутаций store: вторая
        # реализация «status + updated_at» разошлась бы с первой.
        with conn:
            written = journal.append_in_tx(conn, [started])
            _hand_over(conn, str(deal["id"]), stage_id, engine_pid, pid, attempt, ttl, now)
            if written:
                deals._set_status(conn, deal, "running", now)
    except sqlite3.Error:
        process.stop()
        if not release(conn, deal=deal, stage=stage_id, pid=pid):
            release(conn, deal=deal, stage=stage_id, pid=engine_pid)
        raise

    budget_tokens = getattr(getattr(stage, "budget", None), "tokens", None)
    try:
        followed = follow(
            conn,
            process,
            deal=deal,
            stage=stage,
            attempt=attempt,
            ttl=ttl,
            deadline=monotonic() + deadline_sec,
            budget_tokens=budget_tokens,
            monotonic=monotonic,
            clock=clock,
        )
    except sqlite3.Error:
        # Журнал не пишется — попытку не бросаем без присмотра: процесс гасим,
        # лизинг отпускаем, отказ идёт вызывающему. Иначе «шаг упал» оставил бы
        # живой процесс и занятый этап.
        process.stop()
        release(conn, deal=deal, stage=stage_id, pid=pid)
        raise
    exit_code = process.exit_code if process.exit_code is not None else process.wait(KILL_GRACE_SEC)
    result, result_error = read_result(result_file)
    outcome, kind, reason = classify(
        result=result,
        result_error=result_error,
        exit_code=exit_code,
        followed=followed,
        timeout_sec=deadline_sec,
    )
    status = str((result or {}).get("status") or "").strip().lower()
    parked = outcome == OUTCOME_DONE and status in BLOCKED_STATUSES
    stderr_file = write_stderr(workdir, process.stderr_text)

    # Гейт пакета: слово «готово» принимается только вместе с проверяемыми
    # доказательствами. Запускается после смерти процесса и до записи
    # `attempt_ended`, чтобы отказ гейта был в журнале тем же фактом, что и смерть.
    now = _now_sec(int(clock()))
    verdict = None
    if outcome == OUTCOME_DONE and not parked and result is not None:
        verdict = evidence_module.verify(
            result,
            stage,
            workdir,
            attempt_started_at=int(started["ts"]),
            now=now,
            root=cwd,
            base=spec.base,
        )
        if not verdict.ok:
            # Класс отказа гейта — из вердикта (`Verdict.kind`, W4), а не из текста:
            # `contract` для несходящегося пакета, `capability` для бюджета.
            outcome, kind, reason = OUTCOME_FAILED, failures.verdict_kind(verdict), verdict.reason()

    # Публикация (0.0.5): «опубликовано» — заявка исполнителя, факт даёт origin.
    # Сверка идёт до коммита в дереве и до `attempt_ended`: подтверждённый sha
    # ложится отдельным событием `published`, а несошедшаяся заявка — отказом
    # контракта в том же факте, что и смерть попытки.
    published_fact: Optional[dict[str, str]] = None
    if outcome == OUTCOME_DONE and not parked and result is not None and publishes(stage):
        fact, why = published_claim(spec, result)
        if fact is None:
            outcome, kind, reason = OUTCOME_FAILED, FAIL_CONTRACT, why
        elif approval is not None and not publish.same_commit(fact["sha"], approval.commit):
            outcome, kind, reason = (
                OUTCOME_FAILED,
                FAIL_CONTRACT,
                f"опубликован commit {fact['sha'][:PUBLISH_SHORT]}, а одобрен "
                f"{approval.commit[:PUBLISH_SHORT]}: ушло не то дерево",
            )
        else:
            published_fact = fact

    # Приговор ревью — факт графа, а не слово в `summary`: отчёт
    # `verdict: {action, target, reason}` становится `collected`/
    # `changes_requested` с `stage` в payload — ровно того требует правило 2 гейта
    # (`dashboard/transit.py`). Пишет движок по evidence-подтверждённому отчёту и
    # в той же транзакции, что и `attempt_ended`; отказ — `contract` (этап с
    # `verdict` в спеке обязан вынести приговор). Один писатель на весь плагин:
    # `report_verdict` — единственная функция, превращающая действие в событие.
    verdict_fact: Optional[dict[str, Any]] = None
    if outcome == OUTCOME_DONE and not parked and result is not None:
        verdict_fact, why = report_verdict(deal, spec, stage, result, attempt, events=events, now=now)
        if verdict_fact is None and why:
            outcome, kind, reason = OUTCOME_FAILED, FAIL_CONTRACT, why

    # Предложение плана — такой же факт графа, как приговор: отчёт
    # `plan: {stages, reason}` становится `plan_proposed` (и решением по нему) в
    # той же транзакции, что и `attempt_ended`. Отказ — `contract`: этап, который
    # должен планировать, не вправе закончиться без плана.
    plan_facts: list[dict[str, Any]] = []
    if outcome == OUTCOME_DONE and not parked and result is not None:
        plan_facts, why = report_plan(deal, spec, stage, result, attempt, events=events, now=now)
        if why:
            outcome, kind, reason = OUTCOME_FAILED, FAIL_CONTRACT, why

    # Работа принята — движок фиксирует её коммитом **в дереве попытки**: только
    # так следующему этапу есть от чего отвести дерево (W14). `dir`/`none` не
    # коммитятся: у первого дерево чужое (каталог дела), у второго его нет.
    # Неудача фиксации — инфра: работа сделана, но «что видел этап» не станет
    # воспроизводимым, и `done` отдавать нельзя.
    committed = ""
    branch = ""
    branch_error = ""
    if outcome == OUTCOME_DONE and tree is not None:
        committed, commit_error = commit_tree(tree, deal, stage_id, attempt)
        if not committed:
            outcome = OUTCOME_FAILED
            kind = FAIL_INFRA
            reason = f"работа попытки не зафиксирована коммитом: {commit_error}"
        else:
            branch, branch_error = anchor_commit(tree, deal, committed)

    # Дерево упавшей попытки убирается только пустым. Правило было перевёрнуто:
    # дерево удалялось ровно тогда, когда работы в коммите ещё не было, и падение
    # на 55-й минуте уносило единственную копию правок (дело d_7d92affb: 1300
    # строк, 284 вызова, коммита нет — и дерево стёрто). Уборка — не побочный
    # эффект падения: пустое дерево убирается, грязное остаётся и называется
    # путём в событии.
    cleaned = False
    cleanup = ""
    kept_tree = ""
    if outcome != OUTCOME_DONE and stage.workspace == "worktree":
        tree_path = Path(workdir) / "tree"
        dirty, _files = tree_dirty(tree_path) if tree_path.exists() else (False, 0)
        if dirty:
            kept_tree = str(tree_path)
        else:
            cleaned, cleanup = remove_tree(spec, stage, workdir)

    payload: dict[str, Any] = {
        "run_id": run_id,
        "stage": stage_id,
        "attempt": attempt,
        "outcome": outcome,
        "kind": kind or None,
        "reason": reason,
        # База этапа и коммит, который произвела попытка: два факта, по которым
        # следующий этап отводит дерево и по которым разбирается «что видел этап».
        "base": actual_base,
        "commit": committed or None,
        "details": {
            "exit_code": exit_code,
            "session_id": followed.session_id or session_id or None,
            "usage_tokens": followed.usage or None,
            "tools": followed.tools,
            "timed_out": followed.timed_out,
            "over_budget": followed.over_budget,
            "workdir": str(workdir),
            "tree": str(tree) if tree is not None else None,
            "cleaned": cleaned,
            "cleanup": cleanup or None,
            # Сохранённое дерево упавшей попытки: адрес работы, которую не
            # успели закоммитить. Пусто — сохранять было нечего.
            "tree_kept": kept_tree or None,
            # Ветка дела и отказ её закрепления: ссылка на работу — факт, а не
            # догадка «коммит где-то есть».
            "branch": branch or None,
            "branch_error": branch_error or None,
            "stderr_path": str(stderr_file) if stderr_file is not None else None,
            "verdict": verdict.as_payload() if verdict is not None else None,
            # Чем этап назвал работу, когда пакет принят (`evidence.witness`):
            # `commit|artifacts`, `verdict` или `published`. Без этого поля в
            # журнале видно только «принято», а не «принято потому, что этап
            # предъявил вот это» — и разбор отказа идёт по коду, а не по факту.
            "witness": verdict.witness if verdict is not None else None,
        },
    }
    if result is not None:
        payload["result"] = result
    ended = _event(
        deal,
        "attempt_ended",
        payload,
        {"stage": stage_id, "attempt": attempt, "outcome": outcome},
        now,
    )
    facts_out = [ended]
    if published_fact is not None:
        facts_out.append(_published_event(deal, stage, attempt, published_fact, now))
    # Приговор ложится рядом с `attempt_ended`, только если попытка действительно
    # принята: отказ коммита или гейта пакета закрыл её `failed`, и приговор такой
    # попытки не пропускал бы работу, которой никто не принимал.
    if verdict_fact is not None and outcome == OUTCOME_DONE:
        facts_out.append(verdict_fact)
    # Факты плана — рядом с `attempt_ended` и только у принятой попытки: отказ
    # коммита или гейта пакета закрыл её `failed`, и граф по такой попытке не растёт.
    if outcome == OUTCOME_DONE:
        facts_out.extend(plan_facts)
    with conn:
        journal.append_in_tx(conn, facts_out)
        conn.execute(
            "DELETE FROM leases WHERE deal_id = ? AND stage = ? AND pid = ?",
            (str(deal["id"]), stage_id, pid),
        )

    question_id = None
    question_target = ""
    question_reason = ""
    if parked:
        question_id, address = _parked(
            conn,
            deal,
            stage,
            attempt,
            result or {},
            home,
            clock,
            spec=spec,
            spawn=spawner,
            previous=previous,
        )
        question_target, question_reason = address.address, address.reason

    # Политика ретраев: если этот класс больше не повторяется (или дело набрало
    # потолок упавших попыток), дело паркуется — вопрос человеку и `blocked` с
    # `block_kind='parked'`. Класс берётся из события, а не из текста причины.
    park = None
    if outcome == OUTCOME_FAILED:
        park = park_exhausted(conn, spec, deal, stage, attempt, kind, reason, home=home, clock=clock)

    return {
        "started": True,
        "stage": stage_id,
        "attempt": attempt,
        "outcome": outcome,
        "kind": kind or None,
        "reason": reason,
        "run_id": run_id,
        "workdir": str(workdir),
        "tree": str(tree) if tree is not None else None,
        "base": actual_base,
        "commit": committed or None,
        "session_id": followed.session_id or session_id or "",
        "usage_tokens": followed.usage,
        "tools": followed.tools,
        "question_id": question_id,
        "question_target": question_target,
        "question_reason": question_reason,
        "verdict": verdict.as_payload() if verdict is not None else None,
        "park": park,
    }


def park_exhausted(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    stage: Any,
    attempt: int,
    kind: str,
    reason: str,
    *,
    home: Path,
    clock: Callable[[], float],
) -> Optional[dict[str, Any]]:
    """Попытки исчерпаны — припарковать дело: вопрос человеку и `blocked`.

    Парковка пишется существующим `blocked` с `block_kind='parked'` (решение
    координатора по W7): отдельного вида события журнала нет, а состояние
    ожидания остаётся `wait_human` — парковка ждёт **человека**. В payload едут
    класс отказа, этап, число попыток и лимит: без них поверхность показывала бы
    «ждёт ответа» и заставляла пересчитывать причину.

    Политика (`dashboard/failures.py`) решает, парковка это или повтор:
    `retries_exhausted` — класс не повторяется (или лимит класса исчерпан),
    `too_many_failures` — сработал предохранитель на дело. `None` — повтор ещё
    разрешён, парковать нечего.

    Вопрос задаёт движок, а не процесс (`clarify` в `--oneshot` ответа не
    получает): продолжение — властное решение человека, поэтому адрес `human`.
    Автоподъёма нет: ответ на вопрос попытку не поднимает, продолжение — только
    явное `unblocked` (`dashboard/transit.py`, правило 9).
    """
    events = journal.read_events(conn, str(deal["id"]))
    policy = failures.policy(stage)
    counts = failures.failures_of(events, stage.id)
    attempts = counts.get(kind, 0)
    total = failures.total_failures(events)
    rule = failures.decide_park(kind=kind, attempts=attempts, total=total, policy_=policy)
    if rule is None:
        return None
    limit = failures.park_limit(rule, policy)
    counter = failures.counter(kind, attempts, limit)
    if rule == failures.REASON_TOO_MANY:
        headline = (
            f"Попытки исчерпаны: у дела {total} упавших попыток из "
            f"{failures.MAX_ROUNDS_PER_DEAL} (последний класс — {kind} на этапе {stage.id})."
        )
    else:
        headline = (
            f"Попытки исчерпаны: {counter} на этапе {stage.id} "
            f"(политика этапа: max={policy.max}, on=[{', '.join(policy.on) or '—'}])."
        )
    question = (
        f"{headline} Продолжать? continue — новая попытка со сбросом счётчиков "
        f"класса, stop — закрыть дело."
    )
    text_full = (
        f"{headline}\n\nПричина последнего отказа: {reason or 'не записана'}\n"
        f"Этап {stage.id}, попытка {attempt}, класс {kind}, счётчик {counter}."
    )
    address = routing.route(stage, {"kind": "authority", "risky": True})
    asked = ask.ask(
        conn,
        {**deal, "home": Path(home)},
        question,
        text_full,
        list(failures.PARK_OPTIONS),
        routing={**address.as_payload(), "stage": stage.id, "attempt": int(attempt)},
    )
    now = _now_sec(int(clock()))
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind=failures.PARKED_KIND,
        reason=f"{headline} причина последнего отказа: {reason or 'не записана'}",
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        details={
            "kind": kind,
            "stage": stage.id,
            "attempts": attempts,
            "limit": limit,
            "park_reason": rule,
            "question_id": int(asked.question_id),
        },
        now=now,
    )
    return {
        "reason": rule,
        "kind": kind,
        "stage": stage.id,
        "attempt": int(attempt),
        "attempts": attempts,
        "limit": limit,
        "total": total,
        "question_id": int(asked.question_id),
        "question_target": address.address,
        "question_reason": address.reason,
    }


def park_gate(
    conn: sqlite3.Connection,
    deal: Any,
    stage: Any,
    reason: str,
    *,
    question_id: Any = None,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Гейт не дождался человека — припарковать дело с причиной гейта.

    Парковка — тот же `blocked` с `block_kind='parked'`, но без класса отказа и
    счётчика: у гейта не было ни попыток, ни ошибки — человек просто не ответил.
    Поэтому в payload едет `gate: true` (`journal` и `transit` по нему называют
    причину, а не «парковка 0/0 unknown»), а причина приходит готовая из решения
    `transit` («гейт: человек не ответил за N»).
    """
    now = _now_sec(int((clock or time.time)()))
    details: dict[str, Any] = {"stage": stage.id, "gate": True}
    if question_id is not None and str(question_id) != "":
        details["question_id"] = int(str(question_id))
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind=failures.PARKED_KIND,
        reason=reason,
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        details=details,
        now=now,
    )
    return {"reason": reason, "stage": stage.id, "question_id": question_id, "gate": True}


def hops_done(conn: sqlite3.Connection, deal: Any, stage_id: str) -> int:
    """Сколько передач по цепочке этот этап уже сделал — по журналу, не по счёту в памяти.

    Считаются `question_asked` с адресом этого этапа (`payload.routing.stage`,
    который пишет `_parked`): каждая такая запись — уже сделанный хоп
    (`dashboard/routing.py`, правило `max_hops`). Вопросы, заданные до появления
    адреса в журнале, не считаются: их этап неизвестен, и приписывать их
    этому — догадка, из-за которой цепочка кончилась бы раньше объявленного.
    """
    count = 0
    for event in journal.read_events(conn, str(deal["id"])):
        if str(event.get("kind") or "") != "question_asked":
            continue
        address = journal.payload_of(event).get("routing")
        if not isinstance(address, dict) or str(address.get("stage") or "") != stage_id:
            continue
        count += 1
    return count


def ask_role(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    stage: Any,
    attempt: int,
    question: dict[str, Any],
    address: Any,
    *,
    home: Path,
    report: Optional[dict[str, Any]] = None,
    previous: Iterable[dict[str, Any]] = (),
    spawn: Optional[Callable[..., Any]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Спросить роль за ответом на вопрос этапа: контракт, процесс, отчёт.

    Фактовый вопрос из `ask.allowed` пул не трогает (`docs/JOURNAL.md`: «второй
    канал не заводится»): отвечает роль, и её ответ — такой же факт журнала, как
    ответ человека, только с автором-ролью. Поэтому здесь нет ни `ask()`, ни
    строки пула: движок собирает контракт ответа (`build_contract` — тот же
    формат входа, что у попытки), спавнит роль (`spawn_command` с профилем
    адресата) и читает её `result.json`.

    Контракт отличается от контракта попытки в трёх местах, и все три — правда о
    том, что делает роль: `role` — адресат, а не роль этапа; в `question` едет
    сам вопрос, а в `asked` — отчёт попытки, которая его задала (`summary` и
    `result.json` целиком: без него «отвечает на вопрос» = «угадывает
    контекст»); `evidence_to_produce` пуст и схема отчёта — `{answer}`, потому
    что роль ничего не сдаёт и гейт пакета к ней не применяется.

    Ответ — `{ok, answer, authority, kind, reason, role, workdir, exit_code}`.
    `authority: true` в отчёте значит «это не мне решать»: вопрос уходит
    человеку, и это не отказ роли. Провал попытки ответа (`infra`/`contract`) —
    тоже не потеря вопроса: `_parked` отправляет его человеку с причиной.
    """
    spawner = spawn or spawn_process
    clock = clock or time.time
    workdir = attempt_dir(home, deal, stage.id, attempt) / ASK_ROLE_SUBDIR
    workdir.mkdir(parents=True, exist_ok=True)
    deadline_sec = ttl_sec(stage)
    events = journal.read_events(conn, str(deal["id"]))
    contract = build_contract(
        spec,
        deal,
        stage,
        attempt,
        workdir=workdir,
        tree=None,
        cwd=workdir,
        previous=list(previous),
        deadline_sec=deadline_sec,
        events=events,
    )
    contract["role"] = str(address.value)
    contract["question"] = dict(question)
    contract["asked"] = {
        "stage": stage.id,
        "attempt": int(attempt),
        "summary": str((report or {}).get("summary") or ""),
        "report": dict(report or {}),
    }
    contract["answer_report"] = {
        "answer": {
            "text": "твой ответ одной фразой — с ним этап продолжит работу",
            "authority": "true — это не мне решать: вопрос уйдёт человеку",
        }
    }
    contract["evidence_to_produce"] = []
    contract["report_schema"] = {"answer": contract["answer_report"]["answer"]}
    contract_file = write_contract(contract_path(workdir, stage.id, attempt), contract)
    answer_file = result_path(workdir)
    if answer_file.exists():
        # Свой отчёт пишет только эта попытка ответа: остаток прошлой читался бы
        # как её ответ.
        answer_file.unlink()
    try:
        process = spawner(spawn_command(str(address.value), contract_file), workdir)
    except OSError as exc:
        return _role_failed(address, workdir, FAIL_INFRA, f"роль {address.value} не поднялась: {exc}")
    code = process.wait(float(deadline_sec))
    if code is None:
        process.stop()
    write_stderr(workdir, getattr(process, "stderr_text", ""))
    result, error = read_result(answer_file)
    text, authority = _role_answer_of(result)
    if code is None:
        return _role_failed(
            address, workdir, FAIL_INFRA, f"роль {address.value} не ответила за {deadline_sec}с"
        )
    if result is None and code != 0:
        return _role_failed(
            address, workdir, FAIL_INFRA, f"роль {address.value} вышла кодом {code} без ответа"
        )
    if result is None:
        return _role_failed(address, workdir, FAIL_CONTRACT, error)
    if not text and not authority:
        return _role_failed(
            address,
            workdir,
            FAIL_CONTRACT,
            f"роль {address.value} не назвала answer.text: {answer_file}",
        )
    return {
        "ok": True,
        "answer": text,
        "authority": authority,
        "kind": "",
        "reason": "",
        "role": str(address.value),
        "workdir": str(workdir),
        "exit_code": code,
    }


def _role_failed(address: Any, workdir: Path, kind: str, reason: str) -> dict[str, Any]:
    """Попытка ответа роли не удалась — отказ с классом, а не пустой ответ.

    Класс считается по тем же источникам, что у попытки этапа (`dashboard/
    failures.py`): не поднялась/не ответила за срок/вышла ненулевым кодом без
    отчёта — `infra`, отчёт есть, а ответа в нём нет — `contract`.
    """
    return {
        "ok": False,
        "answer": "",
        "authority": False,
        "kind": kind,
        "reason": reason,
        "role": str(address.value),
        "workdir": str(workdir),
        "exit_code": None,
    }


def _role_answer_of(result: Optional[dict[str, Any]]) -> tuple[str, bool]:
    """Ответ роли из отчёта: текст и «это не мне решать».

    `answer` бывает строкой (короткая форма) или словарём `{text, authority}`.
    `authority` — только явное `true`: `"true"` строкой и прочие значения
    властным ответом не считаются, иначе опечатка в отчёте решала бы за человека.
    """
    raw = (result or {}).get("answer")
    if isinstance(raw, str):
        return raw.strip(), False
    if not isinstance(raw, dict):
        return "", False
    return str(raw.get("text") or "").strip(), raw.get("authority") is True


def _role_question_fact(
    deal: Any,
    stage: Any,
    attempt: int,
    text: str,
    question: dict[str, Any],
    address: Any,
    hops: int,
    now: int,
) -> dict[str, Any]:
    """Вопрос, ушедший роли: `question_asked` без пула.

    Номер пула и `session_key` у такого вопроса пусты — это не «потерянные
    поля», а правда: строки пула не существует, вопрос прочитает роль из своего
    контракта, а журнал хранит его текст и адрес. `routing.hops` — номер
    передачи по цепочке (`max_hops`): он же потом считается `hops_done`.
    """
    payload: dict[str, Any] = {
        "routing": {
            **address.as_payload(),
            "stage": stage.id,
            "attempt": int(attempt),
            "hops": int(hops) + 1,
        },
        "text": text,
    }
    text_full = question.get("text_full")
    if isinstance(text_full, str) and text_full.strip():
        payload["text_full"] = text_full.strip()
    return _event(
        deal,
        "question_asked",
        payload,
        {"stage": stage.id, "attempt": int(attempt), "role": str(address.value)},
        now,
    )


def _role_answer_fact(
    deal: Any,
    stage_id: str,
    attempt: int,
    role: str,
    hops: int,
    answer: str,
    via: str,
    now: int,
    reason: str = "",
) -> dict[str, Any]:
    """Ответ роли: `question_answered` с автором-ролью, а не с пулом.

    Текст едет в журнале (`answer`), потому что пула у вопроса роли нет — там
    его читать негде. `answer_len` остаётся у всех ответов: у ответа человека он
    единственное, что есть, и одна форма на два источника не должна терять поле.
    `hops` — номер передачи, которая этим фактом закрывается (`routing.hops`
    вопроса плюс один, либо тот же, что записан в вопросе при восстановлении:
    передача была одна, и номер у вопроса и ответа обязан совпасть).
    `via` — чем кончилась передача, когда ответа нет: `redirected` («дальше
    человек») или `failed` (попытка ответа упала) — по ним `transit` отличает
    закрытие вопроса роли от ответа по сроку (`expired`).
    """
    payload: dict[str, Any] = {
        "by": role,
        "hops": int(hops),
        "answer_len": len(answer),
        "routing": {"stage": stage_id, "attempt": int(attempt)},
    }
    if answer:
        payload["answer"] = answer
    if via:
        payload["answered_via"] = via
    if reason:
        payload["reason"] = reason
    return _event(
        deal,
        "question_answered",
        payload,
        {"stage": stage_id, "attempt": int(attempt), "role": role, "via": via},
        now,
    )


def role_answer_before(events: Iterable[Any], stage_id: str, attempt: int) -> Optional[str]:
    """Ответ роли на вопрос попытки `attempt - 1` — вход этой попытки.

    Ответа человека здесь нет и быть не может: у вопроса роли нет строки пула,
    и текст живёт только в журнале. Ответ берётся у **сразу предыдущей** попытки
    (`routing.attempt`), а не «последний известный»: иначе устаревший ответ
    подставлялся бы в третью попытку после падения второй. `None` — роль эту
    попытку не поднимала, и продолжение идёт как обычно.
    """
    for event in reversed(list(events)):
        if not isinstance(event, dict) or str(event.get("kind") or "") != "question_answered":
            continue
        payload = journal.payload_of(event)
        if not str(payload.get("by") or ""):
            continue
        routing_ = payload.get("routing")
        if not isinstance(routing_, dict):
            continue
        if str(routing_.get("stage") or "") != stage_id:
            continue
        if _int(routing_.get("attempt")) != int(attempt) - 1:
            continue
        text = str(payload.get("answer") or "").strip()
        return text or None
    return None


def _asked_role(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    stage: Any,
    attempt: int,
    text: str,
    question: dict[str, Any],
    address: Any,
    hops: int,
    *,
    result: dict[str, Any],
    previous: Iterable[dict[str, Any]],
    home: Path,
    spawn: Optional[Callable[..., Any]],
    clock: Callable[[], float],
) -> tuple[Any, str]:
    """Вопрос адресован роли: журнал вместо пула, спавн роли вместо парковки.

    Вопрос пишется в журнал **до** спавна: пока роль отвечает, дело стоит в
    `wait_human` (`transit`, правило 7 — вопроса без ответа другого состояния
    нет), и это верное состояние, а не «попытка идёт». Ответ ложится той же
    парой `question_asked` → `question_answered`, и этап поднимается **новой**
    попыткой (`role_answer_before` отдаёт ей текст ответа).

    Возвращает адрес, по которому вопрос идёт дальше, и причину переадресации:
    `role:<имя>` — роль ответила, пул не нужен; `human` — роль переадресовала
    (`authority: true`) или не ответила, и вопрос уходит человеку обычным путём.
    """
    now = _now_sec(int(clock()))
    journal.append(conn, [_role_question_fact(deal, stage, attempt, text, question, address, hops, now)])
    answer = ask_role(
        conn,
        spec,
        deal,
        stage,
        attempt,
        {**question, "text": text},
        address,
        home=home,
        report=result,
        previous=previous,
        spawn=spawn,
        clock=clock,
    )
    after = _now_sec(int(clock()))
    if answer["ok"] and not answer["authority"]:
        journal.append(
            conn,
            [
                _role_answer_fact(
                    deal, stage.id, attempt, str(address.value), hops + 1, answer["answer"], "", after
                )
            ],
        )
        return address, ""
    # Вопрос не сгорает: и переадресация, и провал ответа ведут его человеку.
    # Факт закрывает передачу роли (`redirected`/`failed`), причина едет рядом —
    # иначе «почему спросили человека» разбиралось бы по коду.
    why = (
        f"роль {address.value} переадресовала вопрос человеку: authority"
        if answer["ok"]
        else answer["reason"]
    )
    journal.append(
        conn,
        [
            _role_answer_fact(
                deal,
                stage.id,
                attempt,
                str(address.value),
                hops + 1,
                "",
                transit.ROLE_ANSWER_REDIRECTED if answer["ok"] else transit.ROLE_ANSWER_FAILED,
                after,
                reason=why,
            )
        ],
    )
    return routing.Target(
        routing.HUMAN,
        reason=routing.REASON_REDIRECTED if answer["ok"] else routing.REASON_ROLE_FAILED,
    ), why


def _parked(
    conn: sqlite3.Connection,
    deal: Any,
    stage: Any,
    attempt: int,
    result: dict[str, Any],
    home: Path,
    clock: Callable[[], float],
    *,
    spec: Any,
    spawn: Optional[Callable[..., Any]] = None,
    previous: Iterable[dict[str, Any]] = (),
) -> tuple[Optional[int], Any]:
    """Попытка задала вопрос: адрес решает `routing.route`, канал — адрес.

    `clarify` внутри `--oneshot` ответа не получает (`undelivered`,
    `docs/PROBES.md`), поэтому вопрос задаёт движок, а не процесс; текст вопроса
    приходит в `result.json` полем `question`.

    Два канала, и выбор между ними — адрес: роль из `ask.allowed` получает
    вопрос движком (`_asked_role`: журнал и спавн роли), а всё остальное —
    властный или неразмеченный вопрос, исчерпанная цепочка, несуществующий
    адресат — идёт человеку пул-каналом `ask()` с парковкой `needs_input`, как
    до 0.1.0. `route()` уже отправил вопрос человеку, если цепочка исчерпана,
    поэтому спавн роли случается только внутри `max_hops`.
    """
    question = result.get("question")
    if isinstance(question, str):
        question = {"text": question}
    text = ""
    options: list[str] = []
    if isinstance(question, dict):
        text = str(question.get("text") or question.get("q") or "").strip()
        raw_options = question.get("options")
        if isinstance(raw_options, list):
            options = [str(item) for item in raw_options]
    if not text:
        text = str(result.get("summary") or "").strip()
    if not text:
        text = f"попытка {stage.id}#{attempt} просит решения: текста вопроса в отчёте нет"
    hops = hops_done(conn, deal, stage.id)
    address = routing.route(stage, question, hops)
    note = ""
    if address.kind == routing.ROLE:
        address, note = _asked_role(
            conn,
            spec,
            deal,
            stage,
            attempt,
            text,
            question if isinstance(question, dict) else {"text": text},
            address,
            hops,
            result=result,
            previous=previous,
            home=home,
            spawn=spawn,
            clock=clock,
        )
        if address.kind == routing.ROLE:
            return None, address
    asked = ask.ask(
        conn,
        {**deal, "home": Path(home)},
        text,
        question.get("text_full") if isinstance(question, dict) else None,
        options or None,
        routing={**address.as_payload(), "stage": stage.id, "attempt": int(attempt)},
    )
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind="needs_input",
        reason=(
            f"попытка {stage.id}#{attempt}: вопрос #{asked.question_id} "
            f"адресован {address.address} ({address.reason}) — без ответа"
            + (f"; ответ роли: {note}" if note else "")
        ),
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        now=_now_sec(int(clock())),
    )
    return int(asked.question_id), address


def close_answered_block(conn: sqlite3.Connection, deal: Any, *, clock: Optional[Callable[[], float]] = None) -> bool:
    """Закрыть блок `needs_input`, который держал вопрос, — ответ уже в журнале.

    `blocked` с `block_kind='needs_input'` пишет движок, когда попытка спросила
    человека (`_parked`). Ответ приходит отдельным фактом `question_answered`, и
    блок сам по себе не снимается: `fold` видит `blocked` без `unblocked` и
    называет дело `wait_human`, хотя движок уже поднял новую попытку. Это и есть
    «вторая правда о ждёт ответа» — срез говорил бы «нужен человек» про дело,
    которое человек уже разблокировал ответом.

    Поэтому шаг движка, взявший работу после ответа, пишет `unblocked`: блок
    писал он же, и снимает его он же. Парковка (`block_kind='parked'`) не
    трогается вовсе — её снимает только решение человека (`continue`), иначе
    ответ поднимал бы исчерпанный этап сам.

    Снимается ровно тот блок, после которого в журнале есть ответ: чужой
    `needs_input` (поставленный человеком уже после ответа) остаётся открытым.
    """
    if not _answered_needs_input(conn, deal):
        return False
    # Статус читается заново: шаг мог поднять блок уже после того, как вызывающий
    # прочитал строку дела (прогон берёт её один раз на старте).
    row = deals.deal_row(conn, str(deal["id"]))
    if row is None or str(row.get("status") or "") != "blocked":
        # Блока в статусе дела уже нет (его сняли другим путём) — придумывать
        # второй `unblocked` нечего.
        return False
    deals.set_unblocked(
        conn,
        str(deal["id"]),
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        now=_now_sec(int((clock or time.time)())),
    )
    return True


def _answered_needs_input(conn: sqlite3.Connection, deal: Any) -> bool:
    """Ждёт ли дело ответа, который уже пришёл: открытый `needs_input` после ответа.

    «Блока нет», «блок не наш» и «блока ждут, ответа ещё нет» — один ответ, потому
    что действие во всех трёх случаях одно и то же: ничего не писать.
    """
    events = journal.read_events(conn, str(deal["id"]))
    opened: Optional[dict[str, Any]] = None
    answered = False
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == "blocked":
            opened = event
            answered = False
        elif kind == "unblocked":
            opened = None
            answered = False
        elif kind == "question_answered":
            answered = True
    if opened is None or not answered:
        return False
    return str(journal.payload_of(opened).get("block_kind") or "") == "needs_input"


def recover_role_questions(
    conn: sqlite3.Connection,
    deal: Any,
    *,
    home: Path,
    clock: Optional[Callable[[], float]] = None,
) -> list[dict[str, Any]]:
    """Открытый вопрос роли, которого уже никто не ждёт, — человеку, а не в вечность.

    Вопрос роли движок задаёт и получает ответ **одним вызовом** (`_asked_role`):
    пара `question_asked` → `question_answered` пишется одним шагом, и пока роль
    отвечает, дело стоит в `wait_human` — другого состояния у вопроса нет. Значит
    открытый вопрос роли, до которого дожил проход по журналу, никто не
    дожидается: движка, который его задал, уже нет (SIGKILL, разрыв гейтвея, сбой
    записи между двумя фактами).

    Без этого прохода такой вопрос висел бы вечно, и дело было бы живым только до
    `bin/duty close`, то есть работа терялась бы: строки пула у вопроса роли нет —
    ответить человеку нечем, — а спавна роли больше не будет (попытка уже
    закрыта `attempt_ended`, и `_runnable` поднимает только `pending`).

    Поэтому передача закрывается фактом `question_answered` с
    `answered_via='failed'`, а вопрос уходит человеку **тем же путём**, что у
    упавшей попытки ответа: `ask()` (единственный канал пула) плюс `blocked` с
    `block_kind='needs_input'`. Вопрос не сгорает, и следующий прогон двигает
    дело дальше, как только человек ответит. Саму роль не переспрашиваем: ответ
    ждал тот же движок, который ушёл, и «переспросить» значило бы надеяться, что
    он не уйдёт снова.

    Возвращает строки восстановления — по одной на вопрос: этап, попытка, роль,
    номер вопроса в пуле и причина. Пустой список — восстанавливать нечего.
    """
    events = journal.read_events(conn, str(deal["id"]))
    question = _open_role_question(events)
    if question is None:
        return []
    payload = journal.payload_of(question)
    routing_ = payload.get("routing")
    address_of = routing_ if isinstance(routing_, dict) else {}
    target = str(address_of.get("target") or "")
    stage_id = str(address_of.get("stage") or "")
    attempt = int(_int(address_of.get("attempt")) or 0)
    hops = int(_int(address_of.get("hops")) or 0)
    role = target.split(":", 1)[1] if ":" in target else target
    text = str(payload.get("text") or "").strip() or (
        f"попытка {stage_id}#{attempt} просит решения: текста вопроса в журнале нет"
    )
    now = _now_sec(int((clock or time.time)()))
    why = (
        f"вопрос роли {role} остался без ответа: движок, который его задал, "
        "ответ не записал — вопрос уходит человеку"
    )
    journal.append(
        conn,
        [
            _role_answer_fact(
                deal, stage_id, attempt, role, hops, "", transit.ROLE_ANSWER_FAILED, now, reason=why
            )
        ],
    )
    address = routing.Target(routing.HUMAN, reason=routing.REASON_ROLE_FAILED)
    asked = ask.ask(
        conn,
        {**deal, "home": Path(home)},
        text,
        str(payload.get("text_full") or "") or None,
        None,
        routing={**address.as_payload(), "stage": stage_id, "attempt": attempt},
    )
    deals.set_blocked(
        conn,
        str(deal["id"]),
        kind="needs_input",
        reason=f"попытка {stage_id}#{attempt}: {why} (вопрос #{asked.question_id})",
        author=ENGINE_SOURCE,
        source=ENGINE_SOURCE,
        now=now,
    )
    return [
        {
            "stage": stage_id,
            "attempt": attempt,
            "role": role,
            "question_id": int(asked.question_id),
            "reason": why,
        }
    ]


def _open_role_question(events: Iterable[Any]) -> Optional[dict[str, Any]]:
    """Последний вопрос роли без ответа, чья попытка уже закрыта.

    У вопроса роли нет `question_id` — строки пула не существует, — поэтому пара
    «вопрос → ответ» держится порядком журнала: ответ без номера снимает
    последний открытый вопрос без номера (ровно так же, как `journal.fold`,
    `_open_question`). Вопросы человека сюда не попадают: у них номер есть, и
    ждёт их человек, а не движок — переспрашивать за него нечего.

    Третье условие — `attempt_ended` той же попытки **до** вопроса: вопрос роли
    задаётся по закрытой попытке (`_parked` пишет его после `attempt_ended`),
    поэтому «попытка ещё идёт» означало бы, что спрашивать рано и вопрос, может
    быть, ещё дождётся своего ответа.
    """
    closed: set[tuple[str, int]] = set()
    opened: Optional[dict[str, Any]] = None
    asked_by_closed = False
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        payload = journal.payload_of(event)
        if kind == "attempt_ended":
            closed.add((str(payload.get("stage") or ""), int(_int(payload.get("attempt")) or 0)))
            continue
        if kind not in ("question_asked", "question_answered") or _question_id(payload) is not None:
            continue
        if kind == "question_answered":
            opened, asked_by_closed = None, False
            continue
        routing_ = payload.get("routing")
        target = str(routing_.get("target") or "") if isinstance(routing_, dict) else ""
        if not target.startswith(f"{routing.ROLE}:"):
            opened, asked_by_closed = None, False
            continue
        opened = event
        asked_by_closed = (
            str(routing_.get("stage") or ""),
            int(_int(routing_.get("attempt")) or 0),
        ) in closed
    if opened is None or not asked_by_closed:
        return None
    return opened


def step(
    conn: sqlite3.Connection,
    spec: Any,
    deal: Any,
    decision: Any,
    *,
    home: Path,
    answer: Optional[str] = None,
    spawn: Optional[Callable[..., Any]] = None,
    clock: Optional[Callable[[], float]] = None,
    monotonic: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Исполнить решение `transit`: запустить те попытки, которые оно назвало.

    Не `run` — делать нечего: ждать факта, закрыть дело или припарковать вопрос
    решает не этот слой, и выдумывать здесь шаг нельзя. `answer` относится к
    продолжению после вопроса и передаётся в каждую попытку этого шага.

    Перед попытками движок закрывает блок, который ждал ответа
    (`close_answered_block`): ответ человека — уже факт журнала, а открытый
    `blocked` держал бы дело в `wait_human`, хотя движок снова взялся за работу.
    """
    kind = str(getattr(decision, "kind", "") or "")
    if kind != DECISION_RUN:
        return {"started": 0, "skipped": f"решение {kind or '—'} не запускает попыток", "runs": []}
    close_answered_block(conn, deal, clock=clock)
    runs: list[dict[str, Any]] = []
    for stage_id, attempt in getattr(decision, "pair", []) or []:
        runs.append(
            run_stage(
                conn,
                spec,
                deal,
                str(stage_id),
                int(attempt),
                home=home,
                answer=answer,
                spawn=spawn,
                clock=clock,
                monotonic=monotonic,
            )
        )
    return {"started": sum(1 for run in runs if run.get("started")), "runs": runs}
