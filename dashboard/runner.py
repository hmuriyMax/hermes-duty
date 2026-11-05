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
   `<home>/kanban/boards/<board>/workspaces/<deal>-<stage>-<attempt>` — это
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
EVIDENCE_MODULE_NAME = "hermes_duty_evidence"
ROUTING_MODULE_NAME = "hermes_duty_routing"
FAILURES_MODULE_NAME = "hermes_duty_failures"


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
# команду. Здесь — только исполнение: что запускать, решила спека.
STAGE_KIND_AGENT = "agent"
STAGE_KIND_SHELL = "shell"

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
WORKSPACES_SUBDIR = ("kanban", "boards")

# Служебный каталог попытки рядом с деревом: контракт и отчёт.
DUTY_SUBDIR = ".duty"

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
    """Каталог попытки: `<home>/kanban/boards/<board>/workspaces/<deal>-<stage>-<n>`.

    Под `kanban/boards/**` — потому что это разрешённый корень чтения read-only
    роли: каталог в `mktemp` или профильном `cache/scratch` роль не увидит, и
    ревью окажется пустым не из-за отсутствия находок, а из-за отсутствия
    доступа.
    """
    base = Path(home)
    for part in WORKSPACES_SUBDIR:
        base = base / part
    return base / str(deal["board"]) / "workspaces" / f"{deal['id']}-{stage}-{int(attempt)}"


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
        # worktree». Попытка всегда отводится от коммита, поэтому detach —
        # не обход, а верная модель: ветку двигает не попытка, а публикация.
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
    rows = []
    for need in needs:
        hit = found.get(need)
        commit = attempt_commit(hit[1]) if hit is not None else ""
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
        elif kind in ("collected", "changes_requested"):
            verdicts[stage] = "approve" if kind == "collected" else "changes"
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
    if session_id:
        contract["session_id"] = session_id
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
        "board": str(deal["board"]),
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
    if outcome == OUTCOME_DONE and tree is not None:
        committed, commit_error = commit_tree(tree, deal, stage_id, attempt)
        if not committed:
            outcome = OUTCOME_FAILED
            kind = FAIL_INFRA
            reason = f"работа попытки не зафиксирована коммитом: {commit_error}"

    cleaned = False
    cleanup = ""
    if outcome != OUTCOME_DONE and stage.workspace == "worktree":
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
            "verdict": verdict.as_payload() if verdict is not None else None,
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
    ttl = ttl_sec(stage)
    deadline_sec = ttl
    engine_pid = os.getpid()
    workdir = attempt_dir(home, deal, stage_id, attempt)
    run_id = f"{stage_id}#{attempt}"

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

    # Работа принята — движок фиксирует её коммитом **в дереве попытки**: только
    # так следующему этапу есть от чего отвести дерево (W14). `dir`/`none` не
    # коммитятся: у первого дерево чужое (каталог дела), у второго его нет.
    # Неудача фиксации — инфра: работа сделана, но «что видел этап» не станет
    # воспроизводимым, и `done` отдавать нельзя.
    committed = ""
    if outcome == OUTCOME_DONE and tree is not None:
        committed, commit_error = commit_tree(tree, deal, stage_id, attempt)
        if not committed:
            outcome = OUTCOME_FAILED
            kind = FAIL_INFRA
            reason = f"работа попытки не зафиксирована коммитом: {commit_error}"

    cleaned = False
    cleanup = ""
    if outcome != OUTCOME_DONE and stage.workspace == "worktree":
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
            "stderr_path": str(stderr_file) if stderr_file is not None else None,
            "verdict": verdict.as_payload() if verdict is not None else None,
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
    with conn:
        journal.append_in_tx(conn, [ended])
        conn.execute(
            "DELETE FROM leases WHERE deal_id = ? AND stage = ? AND pid = ?",
            (str(deal["id"]), stage_id, pid),
        )

    question_id = None
    question_target = ""
    question_reason = ""
    if parked:
        question_id, address = _parked(conn, deal, stage, attempt, result or {}, home, clock)
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


def _parked(
    conn: sqlite3.Connection,
    deal: Any,
    stage: Any,
    attempt: int,
    result: dict[str, Any],
    home: Path,
    clock: Callable[[], float],
) -> tuple[int, Any]:
    """Попытка задала вопрос: единственный канал `ask()` и дело в `wait_human`.

    `clarify` внутри `--oneshot` ответа не получает (`undelivered`,
    `docs/PROBES.md`), поэтому вопрос задаёт движок, а не процесс; текст вопроса
    приходит в `result.json` полем `question`.

    Адрес вопроса решает `routing.route()` — без модели и по объявлениям спеки:
    фактовый вопрос — роли из `ask.allowed`, властный и неразмеченный — человеку,
    исчерпанная цепочка — человеку, несуществующий адресат — человеку с
    `routing_failed`. Здесь только исполнение: факт в пул и адрес в журнал.
    """
    question = result.get("question")
    address = routing.route(stage, question, hops_done(conn, deal, stage.id))
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
