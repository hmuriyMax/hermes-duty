"""Evidence-гейт: «готово» без проверяемого пакета не принимается.

Слой **не действует**: ни одной команды из `evidence` он не запускает (их уже
выполнил исполнитель), в сеть не ходит и тексту `summary` не верит. Он сверяет
заявленное в `result.json` с тем, что лежит в дереве попытки, и отвечает пакетом
отказов:

- `contract` — пакет не сходится с деревом: нет файла, чужой хеш, подменённая
  команда, вчерашний прогон, коммит не резолвится или был до попытки;
- `capability` — работа сделана, но вышла за бюджет этапа;
- `infra` этот слой не выдаёт никогда: «процесс не дожил» — факт движка
  (`dashboard/runner.py`, три источника смерти), а не свойство пакета.

Порядок проверок — от «что вообще заявлено» к «чем это подтверждено»:

1. **Схема отчёта**: `status` из закрытого набора; для `done` обязательны
   `evidence`, `summary` и **свидетель этапа** (`witness`): `commit`|`artifacts`
   у обычного этапа, `verdict` у этапа с приговором, `published` у публикующего.
   Лишнее поле — предупреждение, а не отказ: исполнитель вправе добавить контекст.
2. **Артефакты**: путь относительный и не выводит из дерева попытки, файл есть,
   `sha256` заявлен целиком и совпадает с содержимым. У отсутствующего файла
   отказ называет мерку («искали относительно дерево/каталог попытки») и, если
   файл нашёлся в дереве попытки, подсказывает `workspace: worktree`: этот
   диагноз человек разбирал руками (E2E `d_221d021a`).
3. **Коммит**: `git -C <дерево> rev-parse --verify <commit>^{commit}` и он **не
   предок базы**: предок базы означает «принёс готовое», то есть чужое.
4. **Команды этапа**: по каждой объявленной `{cmd, must}` есть запись с тем же
   `cmd`, `exit == must` и `started_at` не раньше начала попытки (вчерашний
   прогон — не доказательство сегодняшней работы).
5. **Подмена команды**: `cmd` совпадает со спекой посимвольно после нормализации
   пробелов; «почти та же команда» — отказ, а не «пойдёт».
6. **Бюджет**: `usage.total_tokens` больше `stage.budget.tokens` — `capability`.

`verify()` — чистая функция по входу: `result`, `stage`, дерево попытки, время её
начала и «сейчас» приходят аргументами, часы внутри не читаются. Единственное
обращение наружу — `git` за разрешением коммита и проверкой предка: коммит,
проверенный словами, ничего не доказывает.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Спека грузится тем же модулем, что у остального плагина: «этап публикует»
# решает одна константа `spec.PUBLISHING_ROLES`, и второй её копии здесь быть не
# должно — иначе гейт пакета и валидатор спеки разойдутся на первом `deployer`.
SPEC_MODULE_NAME = "hermes_duty_spec"


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


spec_module = _load(SPEC_MODULE_NAME, "spec.py")

# Классы отказа, которые выдаёт слой. `infra` здесь нет намеренно: он ставится
# по фактам движка (код выхода, отсутствие отчёта, таймаут), а не по пакету.
KIND_CONTRACT = "contract"
KIND_CAPABILITY = "capability"

# `severity` в `Issue`: `error` делает вердикт отказом, `warning` только
# называется в details (лишнее поле, непроверенная база).
ERROR = "error"
WARNING = "warning"

# Набор статусов отчёта (`docs/JOURNAL.md`, шаг 7 спеки). Всё остальное — не
# «почти верный статус», а незнание того, что заявлено: проверять нечего.
STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"
STATUS_REFUSED = "refused"
STATUSES = (STATUS_DONE, STATUS_BLOCKED, STATUS_REFUSED)

# Поля отчёта, которые слой понимает. Остальные — предупреждением: пакет не
# ломается от лишнего `notes`, но и молчать о нём не надо.
REPORT_KEYS = (
    "status",
    "commit",
    "artifacts",
    "evidence",
    "summary",
    "usage",
    "question",
    "session_id",
    # Публикация (0.0.5): заявка {sha, branch, mr_url}. Проверяет её движок
    # (`runner.published_claim`), а не этот слой: `git ls-remote` — сеть и git,
    # а гейт пакета остаётся сверкой заявленного с деревом.
    "published",
    # Приговор ревью (0.0.5): {action, target, reason}. Читает его движок
    # (`runner.report_verdict`) и пишет фактом графа; гейт пакета проверяет
    # только то, что отчёт — объект нужной формы, а не смысл приговора.
    "verdict",
    # План дела (1.0): {stages, reason}. Читает и проверяет его движок
    # (`runner.report_plan` по `spec.parse_plan`); гейт пакета проверяет форму —
    # этап плана не предъявляет работу в дереве (`docs/ENGINE-1.0.md`, раздел 4).
    "plan",
)

# Имена правил: ими отказ называется в `details` и в `duty why`, чтобы человек
# искал причину по имени, а не по тексту.
RULE_REPORT = "report"
RULE_STATUS = "status"
RULE_REQUIRED = "required"
RULE_EXTRA = "extra"
RULE_ARTIFACT_PATH = "artifact.path"
RULE_ARTIFACT_MISSING = "artifact.missing"
RULE_ARTIFACT_SHA = "artifact.sha256"
RULE_COMMIT_RESOLVE = "commit.resolve"
RULE_COMMIT_FOREIGN = "commit.foreign"
RULE_COMMIT_BASE = "commit.base"
RULE_EVIDENCE_MISSING = "evidence.missing"
RULE_EVIDENCE_EXIT = "evidence.exit"
RULE_EVIDENCE_STALE = "evidence.stale"
RULE_EVIDENCE_FUTURE = "evidence.future"
RULE_EVIDENCE_SUBSTITUTED = "evidence.substituted"
RULE_BUDGET = "budget.tokens"

# Свидетели этапа (0.0.5): «готово» доказывается тем, что этап по своей природе
# может предъявить, а не одним списком на всех. Обычный этап называет работу
# (`commit` или `artifacts`), этап с приговором — приговор (`verdict`), публикующий
# — то, что ушло в origin (`published`). Выбор делает `witness(stage)`.
WITNESS_WORK = "commit|artifacts"
WITNESS_VERDICT = "verdict"
WITNESS_PUBLISHED = "published"
# Этап плана (1.0) предъявляет не работу в дереве, а граф: `plan: {stages,
# reason}`. Требовать от него коммит значило бы требовать ложь — план ещё не
# работа (`docs/ENGINE-1.0.md`, раздел 4). Смысл предложения проверяет не гейт
# пакета, а `spec.parse_plan` в движке (`runner.report_plan`).
WITNESS_PLAN = "plan"
# Этап без терминала (ресерчер, ревьюер без приговора): он не запускает команд и
# не пишет в дерево — его работа это отчёт. Свидетель — непустой `summary` с
# находками; требовать commit/artifacts значило бы требовать ложь. Кейс
# d_48e01a0e: исследовательский этап нельзя было ни запланировать (evidence),
# ни закрыть (свидетель работы) — поэтому лид делал сверку сам в этапе
# координатора.
WITNESS_REPORT = "report"

# Имя отчёта в тексте отказа: `result.json:evidence[0].exit` читается как
# `file:line` спеки, только «строка» здесь — путь внутри отчёта.
REPORT_NAME = "result.json"

# Сколько первых отказов попадает в `reason` события `attempt_ended`: полный
# список лежит в `details`, а причина должна читаться в одну строку.
REASON_LIMIT = 3

# Сколько ждать `git`: разрешение коммита — не сборка, но и не мгновенное.
GIT_TIMEOUT_SEC = 10

# Полная длина sha256 в hex: усечённый хеш ничего не доказывает, поэтому
# «9f2c…» — это не совпадение, а отсутствие доказательства.
SHA_LEN = 64

# С какого сходства команда считается «почти той же»: ниже — уже не подмена, а
# другая команда (и тогда отказ называется «нет записи», а не «подменил»).
SIMILAR = 0.8

# Дерево попытки лежит внутри её каталога (`dashboard/runner.py`,
# `prepare_tree`). Путь нужен отказу `artifact.missing`: «файла нет вовсе» и
# «файл есть, но этап смотрел в каталог, а не в дерево» — разные диагнозы, и
# второй человек разбирал руками (E2E `d_221d021a`).
TREE_SUBDIR = "tree"

# Мерка, относительно которой ищется артефакт: отказ обязан её назвать, иначе
# «файла нет» не отвечает на вопрос «где искали».
MEASURE_TREE = "дерево попытки"
MEASURE_ATTEMPT = "каталог попытки"
MEASURE_DIR = "каталог дела"


@dataclass(frozen=True)
class Issue:
    """Один отказ гейта: где в отчёте, каким правилом и что именно не сошлось.

    `where` — `file:line`-подобная точка (`result.json:evidence[0].exit`),
    `kind` — класс отказа, который она даёт: `contract` или `capability`.
    """

    where: str
    rule: str
    message: str
    severity: str = ERROR
    kind: str = KIND_CONTRACT

    def __str__(self) -> str:
        return f"{self.where}: {self.rule}: {self.message}"


@dataclass(frozen=True)
class Verdict:
    """Приговор пакету: принят или нет, каким классом и с какими details."""

    ok: bool
    kind: Optional[str] = None
    details: list[Issue] = field(default_factory=list)
    # Принятый свидетель пакета (`witness(stage)`): он же уходит в
    # `attempt_ended.details`, чтобы в журнале было видно, чем этап назвал работу,
    # а не только «принято». У непринятого пакета его нет: свидетель не принят.
    witness: Optional[str] = None

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.details if issue.severity == ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.details if issue.severity == WARNING]

    def reason(self, limit: int = REASON_LIMIT) -> str:
        """Причина отказа одной строкой: первые `limit` отказов, полный — в details."""
        return " · ".join(str(issue) for issue in self.errors[:limit])

    def as_payload(self) -> dict[str, Any]:
        """Вердикт для события `attempt_ended`: класс, флаг, свидетель и все details."""
        return {
            "ok": bool(self.ok),
            "kind": self.kind,
            "witness": self.witness,
            "details": [str(issue) for issue in self.details],
        }


# ------------------------------------------------------------------ вспомогательное


def _where(section: str, index: Optional[int] = None, key: str = "") -> str:
    """Точка отказа в форме `result.json:evidence[0].exit`."""
    place = f"{REPORT_NAME}:{section}"
    if index is not None:
        place += f"[{int(index)}]"
    if key:
        place += f".{key}"
    return place


def _int(value: Any) -> Optional[int]:
    """Целое из отчёта; `true`/строка без цифр целым не считаются."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stamp(value: Any) -> Optional[int]:
    """Unix-секунды из числа или ISO-8601: `started_at` сравнивается с журналом.

    Принимаются оба вида, потому что исполнитель пишет отчёт руками, а журнал
    считает секундами. Неразобранное время — не «свежее по умолчанию», а
    отсутствие доказательства.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def _normalise_cmd(value: Any) -> str:
    """Команда без различий в пробелах: `" ".join(text.split())`.

    Нормализация — только про пробелы: текст команды, меняющий смысл от
    порядка слов или от неразрывного пробела, обязан совпадать посимвольно.
    """
    return " ".join(str(value or "").split())


def _sha256(path: Path) -> Optional[str]:
    """sha256 файла или `None`, если он не читается."""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    """`git` в дереве попытки: код возврата и хвост вывода без переводов строк."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, " ".join(str(exc).split())
    return done.returncode, " ".join((done.stdout or done.stderr or "").split())


def _rev_parse(root: Path, rev: str) -> Optional[str]:
    """Полный sha коммита или `None`: `<rev>^{commit}` отсекает тег и дерево."""
    code, out = _git(["rev-parse", "--verify", f"{rev}^{{commit}}"], root)
    if code != 0 or not out:
        return None
    return out.split()[0]


def _is_ancestor(root: Path, commit: str, base: str) -> Optional[bool]:
    """`commit` — предок или равен `base`; `None` — git не ответил."""
    code, _ = _git(["merge-base", "--is-ancestor", commit, base], root)
    if code == 0:
        return True
    if code == 1:
        return False
    return None


def witness(stage: Any) -> str:
    """Какой свидетель принимается от этапа: по его возможностям, а не один на всех.

    Этап, который по своей природе не меняет дерево, не может назвать работу
    коммитом или файлами — и требовать от него `commit|artifacts` значит требовать
    ложь. Поэтому свидетель выбирается по признакам самой спеки, без новых флагов:

    - у этапа есть `verdict` — он выносит приговор, и приговора достаточно
      (`runner.report_verdict` пишет по нему факт графа);
    - `role` из `spec.PUBLISHING_ROLES` — этап действует вовне, и его свидетель
      `published: {sha, branch}`, который сверяется с origin строже дерева
      (`runner.published_claim`, `git ls-remote`);
    - всё остальное называет работу: `commit` или `artifacts`.
    """
    if getattr(stage, "verdict", None) is not None:
        return WITNESS_VERDICT
    if str(getattr(stage, "role", "") or "") in spec_module.PUBLISHING_ROLES:
        return WITNESS_PUBLISHED
    if (
        spec_module.is_plan_stage(getattr(stage, "id", ""))
        and str(getattr(stage, "kind", spec_module.DEFAULT_STAGE_KIND)) == spec_module.DEFAULT_STAGE_KIND
    ):
        return WITNESS_PLAN
    if str(getattr(stage, "role", "") or "") in spec_module.EVIDENCE_FREE_ROLES:
        # Роль без терминала или пишущая документ, а не код: дерево она не меняет,
        # её работа — отчёт, и свидетель у неё один и тот же (`WITNESS_REPORT`).
        return WITNESS_REPORT
    return WITNESS_WORK


def _witness_problem(kind: str, result: dict[str, Any]) -> Optional[Issue]:
    """Чем свидетель этапа не предъявлен; `None` — предъявлен в своей форме.

    Отказ называет, что принимается **у этого** этапа: «нет коммита» на
    публикующем этапе отправляло исполнителя искать работу, которую тот и не
    делал (живой прогон TF3-c: `publish` и `review` отказывали всегда).
    """
    if kind == WITNESS_VERDICT:
        if isinstance(result.get("verdict"), dict):
            return None
        return Issue(
            _where(WITNESS_VERDICT),
            RULE_REQUIRED,
            "этап с приговором: нужен verdict {action, target, reason}; "
            "commit/artifacts не требуются",
        )
    if kind == WITNESS_PUBLISHED:
        published = result.get(WITNESS_PUBLISHED)
        if isinstance(published, dict) and str(published.get("sha") or "").strip():
            return None
        return Issue(
            _where(WITNESS_PUBLISHED),
            RULE_REQUIRED,
            "этап публикации: нужен published {sha, branch}; commit/artifacts не требуются",
        )
    if kind == WITNESS_PLAN:
        plan = result.get(WITNESS_PLAN)
        stages = plan.get("stages") if isinstance(plan, dict) else None
        if isinstance(stages, list) and stages:
            return None
        return Issue(
            _where(WITNESS_PLAN),
            RULE_REQUIRED,
            "этап плана: нужен plan {stages: [...], reason}; commit/artifacts не требуются — "
            "предложение проверяет движок (spec.parse_plan), а не гейт пакета",
        )
    if kind == WITNESS_REPORT:
        summary = result.get("summary")
        if isinstance(summary, str) and summary.strip():
            return None
        return Issue(
            _where(WITNESS_REPORT),
            RULE_REQUIRED,
            "этап без терминала: нужен summary с находками; commit/artifacts "
            "не требуются — работа такого этапа это отчёт",
        )
    commit = result.get("commit")
    claimed_commit = commit.strip() if isinstance(commit, str) else ""
    artifacts = result.get("artifacts")
    claimed_artifacts = artifacts if isinstance(artifacts, list) else []
    if claimed_commit or claimed_artifacts:
        return None
    return Issue(
        _where("artifacts"),
        RULE_REQUIRED,
        "status: done без commit и без artifacts: работа не названа ни коммитом, ни файлами",
    )


# ----------------------------------------------------------------------- проверки


def _check_schema(result: dict[str, Any], stage: Any, issues: list[Issue]) -> Optional[str]:
    """Схема отчёта; возвращает нормализованный `status` или `None`, если он чужой."""
    status = str(result.get("status") or "").strip().lower()
    for key in result:
        if key not in REPORT_KEYS:
            issues.append(
                Issue(
                    _where(str(key)),
                    RULE_EXTRA,
                    f"лишнее поле {key!r}: не отказ, но гейт его не читает",
                    severity=WARNING,
                )
            )
    if status not in STATUSES:
        issues.append(
            Issue(
                _where("status"),
                RULE_STATUS,
                f"status={result.get('status')!r}: допустимо {', '.join(STATUSES)}",
            )
        )
        return None
    if status != STATUS_DONE:
        # `blocked` и `refused` не заявляют «готово»: пакета у них нет и быть не
        # может, поэтому схема пакета к ним не применяется.
        return status

    commit = result.get("commit")
    if commit is not None and not isinstance(commit, str):
        issues.append(Issue(_where("commit"), RULE_REQUIRED, "commit — строка sha"))
    artifacts = result.get("artifacts")
    if artifacts is not None and not isinstance(artifacts, list):
        issues.append(Issue(_where("artifacts"), RULE_REQUIRED, "artifacts — список {path, sha256}"))
    problem = _witness_problem(witness(stage), result)
    if problem is not None:
        issues.append(problem)
    if "evidence" not in result:
        issues.append(
            Issue(
                _where("evidence"),
                RULE_REQUIRED,
                "status: done без evidence: доказательства объявленных команд не предъявлены",
            )
        )
    elif not isinstance(result.get("evidence"), list):
        issues.append(Issue(_where("evidence"), RULE_REQUIRED, "evidence — список {cmd, exit, started_at}"))
    if not str(result.get("summary") or "").strip():
        issues.append(Issue(_where("summary"), RULE_REQUIRED, "status: done без summary: итог не назван"))
    return status


def _check_artifacts(
    entries: list[Any], root: Path, issues: list[Issue], *, measure: str, tree: Path
) -> None:
    """Артефакты в дереве попытки: путь внутри, файл есть, sha256 совпадает.

    `measure` — как называется мерка в отказе (`дерево попытки` / `каталог
    попытки` / `каталог дела`), `tree` — где лежало бы дерево этой попытки.
    Отсутствующий файл называется вместе с меркой: без неё «файла нет» не
    говорит, чего не хватило — файла или дерева, которое никто не создавал.
    """
    for index, entry in enumerate(entries):
        where = _where("artifacts", index)
        if not isinstance(entry, dict):
            issues.append(Issue(where, RULE_ARTIFACT_PATH, "артефакт — словарь {path, sha256}"))
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw.strip():
            issues.append(Issue(f"{where}.path", RULE_ARTIFACT_PATH, "path не задан"))
            continue
        relative = Path(raw)
        if relative.is_absolute():
            issues.append(
                Issue(
                    f"{where}.path",
                    RULE_ARTIFACT_PATH,
                    f"path={raw!r}: абсолютный путь — вне дерева попытки",
                )
            )
            continue
        if ".." in relative.parts:
            issues.append(
                Issue(
                    f"{where}.path",
                    RULE_ARTIFACT_PATH,
                    f"path={raw!r}: выход за пределы дерева попытки",
                )
            )
            continue
        target = (root / relative).resolve()
        if target != root and root not in target.parents:
            issues.append(
                Issue(
                    f"{where}.path",
                    RULE_ARTIFACT_PATH,
                    f"path={raw!r}: резолвится вне {root}",
                )
            )
            continue
        if not target.is_file():
            # Мерка в отказе — часть контракта: «файла нет» без неё заставляет
            # человека гадать, чего не хватило — файла или дерева.
            message = f"файла нет: {target}; искали относительно {measure}"
            if tree != root:
                in_tree = (tree / relative).resolve()
                if in_tree.is_file():
                    # Файл в дереве попытки есть, а этап работал в каталоге —
                    # ровно случай E2E `d_221d021a`: отчёт верный, мерка чужая.
                    message += (
                        f"; файл есть в дереве попытки ({in_tree}) — этап работает в каталоге "
                        "попытки, укажи workspace: worktree, чтобы работать в дереве"
                    )
            issues.append(Issue(f"{where}.path", RULE_ARTIFACT_MISSING, message))
            continue
        claimed = entry.get("sha256")
        claimed_text = claimed.strip().lower() if isinstance(claimed, str) else ""
        if len(claimed_text) != SHA_LEN:
            issues.append(
                Issue(
                    f"{where}.sha256",
                    RULE_ARTIFACT_SHA,
                    f"sha256={claimed!r}: нужен полный хеш ({SHA_LEN} hex), иначе файл не доказан",
                )
            )
            continue
        actual = _sha256(target)
        if actual is None:
            issues.append(Issue(f"{where}.sha256", RULE_ARTIFACT_SHA, f"файл не читается: {target}"))
            continue
        if actual != claimed_text:
            issues.append(
                Issue(
                    f"{where}.sha256",
                    RULE_ARTIFACT_SHA,
                    f"sha256 в отчёте {claimed_text[:12]}…, в дереве {actual[:12]}… — файл не тот",
                )
            )


def _check_commit(commit: str, root: Path, base: Optional[str], issues: list[Issue]) -> None:
    """Коммит резолвится в дереве и не был предком базы до попытки."""
    resolved = _rev_parse(root, commit)
    if resolved is None:
        issues.append(
            Issue(
                _where("commit"),
                RULE_COMMIT_RESOLVE,
                f"commit={commit!r}: git rev-parse --verify не нашёл коммит в {root}",
            )
        )
        return
    if not base:
        return
    base_sha = _rev_parse(root, str(base))
    if base_sha is None:
        issues.append(
            Issue(
                _where("commit"),
                RULE_COMMIT_BASE,
                f"база {base!r} не резолвится в {root}: предок не проверен",
                severity=WARNING,
            )
        )
        return
    ancestor = _is_ancestor(root, resolved, base_sha)
    if ancestor is None:
        issues.append(
            Issue(
                _where("commit"),
                RULE_COMMIT_BASE,
                f"git merge-base не ответил про {resolved[:12]}… и базу {base}: предок не проверен",
                severity=WARNING,
            )
        )
        return
    if ancestor:
        issues.append(
            Issue(
                _where("commit"),
                RULE_COMMIT_FOREIGN,
                f"commit={commit} уже был предком базы {base} до попытки — это чужое готовое",
            )
        )


def _record_problem(
    record: dict[str, Any], index: int, must: int, attempt_started_at: int, now: int
) -> Optional[Issue]:
    """Чем запись evidence не годится: код возврата, свежесть или ничего."""
    code = _int(record.get("exit"))
    if code is None:
        return Issue(
            _where("evidence", index, "exit"),
            RULE_EVIDENCE_EXIT,
            f"exit={record.get('exit')!r}: нужен код возврата {must}",
        )
    if code != int(must):
        return Issue(
            _where("evidence", index, "exit"),
            RULE_EVIDENCE_EXIT,
            f"exit={code}, а объявлен must={must}",
        )
    started = _stamp(record.get("started_at"))
    if started is None:
        return Issue(
            _where("evidence", index, "started_at"),
            RULE_EVIDENCE_STALE,
            f"started_at={record.get('started_at')!r}: свежесть не доказана",
        )
    if started < int(attempt_started_at):
        return Issue(
            _where("evidence", index, "started_at"),
            RULE_EVIDENCE_STALE,
            f"started_at={started} старше начала попытки {int(attempt_started_at)}: "
            "результат вчерашнего прогона",
        )
    if started > int(now):
        return Issue(
            _where("evidence", index, "started_at"),
            RULE_EVIDENCE_FUTURE,
            f"started_at={started} позже текущего времени {int(now)}: запись из будущего",
        )
    return None


def _similar(left: str, right: str) -> bool:
    """«Почти та же команда»: либо различие только в пробелах, либо высокая близость."""
    if not left or not right:
        return False
    if "".join(left.split()) == "".join(right.split()):
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= SIMILAR


def _check_evidence(
    declared: Any, records: list[Any], attempt_started_at: int, now: int, issues: list[Issue]
) -> None:
    """Объявленные команды этапа: запись есть, код тот, прогон не старше попытки."""
    entries = [record for record in records if isinstance(record, dict)]
    for index, item in enumerate(declared or ()):
        cmd = getattr(item, "cmd", "")
        must = _int(getattr(item, "must", 0))
        declared_cmd = _normalise_cmd(cmd)
        if must is None:
            continue
        matching = [
            (position, record)
            for position, record in enumerate(entries)
            if _normalise_cmd(record.get("cmd")) == declared_cmd
        ]
        if not matching:
            near = [
                position
                for position, record in enumerate(entries)
                if _similar(declared_cmd, _normalise_cmd(record.get("cmd")))
            ]
            if near:
                position = near[0]
                issues.append(
                    Issue(
                        _where("evidence", position, "cmd"),
                        RULE_EVIDENCE_SUBSTITUTED,
                        f"cmd={_normalise_cmd(entries[position].get('cmd'))!r} не совпадает со "
                        f"спекой {_normalise_cmd(cmd)!r} посимвольно",
                    )
                )
            else:
                issues.append(
                    Issue(
                        _where("evidence"),
                        RULE_EVIDENCE_MISSING,
                        f"нет записи по команде {_normalise_cmd(cmd)!r} (must={must})",
                    )
                )
            continue
        # Годится любая запись, у которой сходятся и код, и свежесть; отказ
        # называется по первой, иначе «одна свежая» терялась бы за старой.
        problem: Optional[Issue] = None
        for position, record in matching:
            found = _record_problem(record, position, int(must), attempt_started_at, now)
            if found is None:
                problem = None
                break
            if problem is None:
                problem = found
        if problem is not None:
            issues.append(problem)


def _check_budget(result: dict[str, Any], stage: Any, issues: list[Issue]) -> None:
    """Бюджет этапа: превышение — `capability`, а не подлог контракта."""
    budget = getattr(stage, "budget", None)
    limit = _int(getattr(budget, "tokens", None)) if budget is not None else None
    if limit is None:
        return
    usage = result.get("usage")
    total = _int(usage.get("total_tokens")) if isinstance(usage, dict) else None
    if total is None:
        return
    if total > limit:
        issues.append(
            Issue(
                _where("usage", None, "total_tokens"),
                RULE_BUDGET,
                f"total_tokens={total} больше бюджета этапа {limit}: работа сделана за рамками",
                kind=KIND_CAPABILITY,
            )
        )


# ------------------------------------------------------------------------- вход


def verify(
    result: Any,
    stage: Any,
    workdir: Path,
    attempt_started_at: int,
    now: int,
    *,
    root: Optional[Path] = None,
    base: Optional[str] = None,
) -> Verdict:
    """Приговор пакету попытки: сверка заявленного с деревом, без запуска команд.

    `root` — рабочий каталог попытки (cwd процесса: дерево `worktree`, каталог
    дела `dir`, каталог попытки `none`): относительные пути артефактов режутся по
    нему, а не по `workdir`, где лежит отчёт. `base` — база спеки: по ней
    проверяется, что заявленный коммит не был готов до попытки. `workdir` — не
    только место отчёта: дерево попытки лежит внутри него (`TREE_SUBDIR`), и
    отказ `artifact.missing` по этому пути отличает «файла нет» от «этап смотрел
    в каталог, а файл — в дереве».
    """
    if not isinstance(result, dict):
        return Verdict(
            ok=False,
            kind=KIND_CONTRACT,
            details=[Issue(_where(""), RULE_REPORT, f"отчёт — объект, а пришло {type(result).__name__}")],
        )

    root_path = Path(root if root is not None else workdir).resolve()
    # Мерка называется по `workspace` этапа: `worktree` ищет в дереве, `dir` — в
    # каталоге дела, `none` — в каталоге попытки. Это тот же выбор, что делает
    # движок (`runner.prepare_tree`), названный словами в отказе.
    measure = {
        "worktree": MEASURE_TREE,
        "dir": MEASURE_DIR,
    }.get(str(getattr(stage, "workspace", "") or ""), MEASURE_ATTEMPT)
    tree_path = (Path(workdir) / TREE_SUBDIR).resolve()
    issues: list[Issue] = []
    status = _check_schema(result, stage, issues)
    if status is None:
        return Verdict(ok=False, kind=KIND_CONTRACT, details=issues)
    if status == STATUS_DONE:
        artifacts = result.get("artifacts")
        _check_artifacts(list(artifacts or []), root_path, issues, measure=measure, tree=tree_path)
        commit = result.get("commit")
        commit_text = commit.strip() if isinstance(commit, str) else ""
        if commit_text:
            _check_commit(commit_text, root_path, base, issues)
        records = result.get("evidence")
        if isinstance(records, list):
            # Отсутствие поля целиком уже названо схемой: повторять его на каждой
            # объявленной команде — шум, а не уточнение.
            _check_evidence(
                getattr(stage, "evidence", ()),
                list(records),
                int(attempt_started_at),
                int(now),
                issues,
            )
        # Бюджет считается по заявленной работе: `blocked`/`refused` её не
        # предъявляют, и перерасход там — не отказ пакета, а факт движка.
        _check_budget(result, stage, issues)

    # Свидетель, которым этап называет работу (`witness`): у `blocked`/`refused`
    # пакета нет вовсе, и называть там нечего.
    accepted = witness(stage) if status == STATUS_DONE else None
    errors = [issue for issue in issues if issue.severity == ERROR]
    if not errors:
        return Verdict(ok=True, kind=None, details=issues, witness=accepted)
    kind = KIND_CONTRACT if any(issue.kind == KIND_CONTRACT for issue in errors) else KIND_CAPABILITY
    return Verdict(ok=False, kind=kind, details=issues)


def report_schema(stage: Any) -> dict[str, Any]:
    """Схема отчёта для контракта попытки: исполнитель видит её до работы.

    Гейт, о схеме которого узнают только из отказа, — не контракт, а ловушка.
    Поэтому схема уходит в файл контракта тем же слоем, который её и проверяет.

    Поле `verdict` появляется только у этапа с приговором (`verdict` в спеке):
    это единственный способ, которым ревьюер говорит «одобрено» структурно, а не
    словом в `summary`, — иначе следующему этапу нечего прочитать и гейт графа
    (`dashboard/transit.py`, правило 2) ждёт приговор, которого никто не писал.
    Требуемый свидетель (`required_for_done`) называет `witness(stage)`: контракт
    и гейт обязаны требовать от этапа одно и то же, иначе исполнитель узнаёт
    настоящее требование из отказа.
    """
    evidence = [
        {"cmd": getattr(item, "cmd", ""), "exit": getattr(item, "must", 0), "started_at": "unix-секунды"}
        for item in getattr(stage, "evidence", ())
    ]
    wanted = witness(stage)
    required = [wanted, "evidence", "summary"]
    if wanted == WITNESS_REPORT:
        # Отчёт и есть свидетель: `evidence` у роли без терминала пуст, а
        # `commit|artifacts` взять неоткуда — требование повторяло бы witness.
        required = [WITNESS_REPORT, "summary"]
    verdict: dict[str, str] = {}
    if wanted == WITNESS_VERDICT:
        verdict = {
            "action": "approve (одобрено) | changes (на доработку) — приговор этапа",
            "target": "next | id этапа | \"\" — по умолчанию: approve → next, changes → verdict.changes спеки",
            "reason": "одна строка: на чём основан приговор",
        }
    published: dict[str, str] = {}
    if wanted == WITNESS_PUBLISHED:
        published = {
            "sha": "полный или сокращённый sha того, что ушло в origin (сверяется с git ls-remote)",
            "branch": "ветка, в которую опубликовано, или пустая строка",
            "mr_url": "ссылка на MR — заявка, не проверяется движком",
        }
    plan: dict[str, Any] = {}
    if wanted == WITNESS_PLAN:
        plan = {
            "stages": "непустой список этапов плана {id, role, kind, needs, evidence, budget}",
            "reason": "одна строка: почему граф именно такой",
        }
    schema: dict[str, Any] = {
        "status": list(STATUSES),
        "required_for_done": required,
        "artifacts": {"path": "относительный путь внутри дерева попытки", "sha256": "полный sha256 файла"},
        "evidence": evidence,
        "usage": {"total_tokens": "необязательно: сверяется с бюджетом этапа"},
    }
    if verdict:
        schema["verdict"] = verdict
    if published:
        schema["published"] = published
    if plan:
        schema["plan"] = plan
    return schema
