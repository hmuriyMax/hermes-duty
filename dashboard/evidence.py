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
   у обычного этапа, `verdict` у этапа с приговором, `published` у публикующего,
   `report` у роли-отчёта (`spec.EVIDENCE_FREE_ROLES` и `tester`: такая роль
   проверяет чужую работу, а не делает свою, и коммит в её отчёте называет
   проверенное), `plan` у этапа плана; вместо работы
   этап-производитель вправе предъявить пустоту (`nothing: {reason}`), и тогда
   дерево обязано это подтвердить (`HEAD` = база попытки, `git status` пуст).
   Лишнее поле — предупреждение, а не отказ: исполнитель вправе добавить контекст.
2. **Артефакты**: путь относительный и не выводит из дерева попытки, файл есть,
   `sha256` заявлен целиком и совпадает с содержимым. Файл, найденный во втором
   корне (дерево попытки ↔ каталог попытки), **принимается**: где именно он лежит
   — форма записи, а не содержание работы, и место находки называется
   предупреждением `artifact.elsewhere` в `details`. Отказ `artifact.missing`
   остался для «файла нет нигде» и называет оба корня, где искали: этот диагноз
   человек разбирал руками (E2E `d_221d021a`, живые `d_b9c480c8`, `d_76b269ce`,
   `d_95f55146`).
3. **Коммит**: `git -C <дерево> rev-parse --verify <commit>^{commit}` и он **не
   предок базы**: предок базы означает «принёс готовое», то есть чужое.
4. **Команды этапа**: по каждой объявленной `{cmd, must}` есть запись с тем же
   `cmd`, `exit == must` и `started_at` не раньше начала попытки (вчерашний
   прогон — не доказательство сегодняшней работы).
5. **Подмена команды**: `cmd` совпадает со спекой после нормализации пробелов и
   снятия внешней обёртки оболочки (`sh -c '<cmd>'` ≡ `<cmd>`,
   `_strip_shell_wrapper`); «почти та же команда» — отказ, а не «пойдёт».
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
    # Пустота (0.2.1): {reason} — «работы для этапа не было». Законный исход
    # этапа-производителя, а не уход от работы: гейт сверяет его с деревом, а
    # причина называет, почему править было нечего (`nothing.dirty`).
    "nothing",
)

# Имена правил: ими отказ называется в `details` и в `duty why`, чтобы человек
# искал причину по имени, а не по тексту.
RULE_REPORT = "report"
RULE_STATUS = "status"
RULE_REQUIRED = "required"
RULE_EXTRA = "extra"
RULE_ARTIFACT_PATH = "artifact.path"
RULE_ARTIFACT_MISSING = "artifact.missing"
# Файл есть, но не в том корне, от которого объявлен (0.2.2). Отдельное имя, а не
# `artifact.missing` с предупреждением: разбор отказов считает их по правилу
# (`docs/SPEC.md`, `contract_refusals.by_rule`), и найденный файл в той же куче,
# что и пропавшие, читался бы как «артефактов не было вовсе».
RULE_ARTIFACT_ELSEWHERE = "artifact.elsewhere"
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
RULE_NOTHING_REASON = "nothing.reason"
RULE_NOTHING_DIRTY = "nothing.dirty"
RULE_NOTHING_STAGE = "nothing.stage"
# Пустота — альтернатива работе, а не дополнение: коммит или артефакты рядом с
# `nothing` означают, что работа была, и отказ называется своим правилом. Судить
# их правилами работы (`commit.foreign`) нельзя: текст того отказа велит
# предъявить `nothing`, а исполнитель его уже предъявил.
RULE_NOTHING_COMMIT = "nothing.commit"
RULE_NOTHING_ARTIFACTS = "nothing.artifacts"
# Слияние полос (0.2.2, `spec.MERGE_KIND`). Три правила, по одному на способ
# соврать про fan-in: свидетелем назван не merge-коммит (`merge.parents`),
# сторона полосы исчезла из отчёта или выброшена целиком без обоснования
# (`merge.dropped`), конфликт остался в дереве (`merge.conflict`). Живой дефект,
# ради которого версия делалась: `d_c13d53da` 16.11 — две полосы, зависимый этап
# отказался строиться, потому что слияния не было вовсе.
RULE_MERGE_PARENTS = "merge.parents"
RULE_MERGE_DROPPED = "merge.dropped"
RULE_MERGE_CONFLICT = "merge.conflict"


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
# Этап-отчёт (ресерчер, ревьюер без приговора, `tester`): он проверяет, а не делает
# — его работа это отчёт. Свидетель — непустой `summary` с находками; требовать
# commit/artifacts значило бы требовать ложь. Кейс
# d_48e01a0e: исследовательский этап нельзя было ни запланировать (evidence),
# ни закрыть (свидетель работы) — поэтому лид делал сверку сам в этапе
# координатора.
WITNESS_REPORT = "report"
# Этап-производитель, которому править было нечего (0.2.1): `nothing: {reason}`
# вместо `commit|artifacts`. Живое дело `d_12d5fc44`: `research` ответил
# «расхождений нет», лид оставил в плане `fix`, исполнитель честно ничего не
# менял и предъявил базовый коммит — гейт назвал это чужим готовым
# (`commit.foreign`), дело встало `contract` → `plan_rethink`, а лид закрыл его
# словами «этап упал контрактом, потому что работы не было». В контракте не было
# законного способа сказать «делать нечего», и цена выяснения — 8 минут, 4.9 млн
# токенов и 79 вызовов. Лазейкой он при этом не становится: `HEAD` обязан совпасть
# с базой попытки, `git status` — быть пустым, а причина — называть, почему править
# было нечего (`nothing.reason`, `nothing.dirty`).
WITNESS_NOTHING = "nothing"

# Роли, чей отчёт и есть свидетель (0.2.2): они проверяют чужую работу, а не
# делают свою. Набор один на весь плагин — `spec.REPORT_ONLY_ROLES`: там же
# объяснено, почему `tester` стоит рядом с ролями без дерева, а не входит в
# `EVIDENCE_FREE_ROLES` (этот набор читает `spec.parse_plan`: этапу плана надо
# объявить `evidence`, и тестер не должен лишаться той же обязанности).
#
# Цена прежнего поведения видна в живом журнале: 18 отказов `commit.foreign` за
# неделю, из них 9 — на этапах `tester`, и последний честный (дело `d_95f55146`,
# этап `check2`: `make lint` 0, `go test -count=1` 0, sha256 файлов сошлись с
# отчётом `impl2`) лёг контрактом только за то, что в `commit` был назван
# ПРОВЕРЕННЫЙ коммит `fc4a88e`, который к моменту проверки уже был предком базы.
# Работу эта проверка не находит — она её запрещает называть.
REPORT_WITNESS_ROLES = spec_module.REPORT_ONLY_ROLES

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

# Обёртки оболочки — тот же закрытый список, что у спеки (`spec.SHELL_WRAPPERS`):
# спека отказывает такой записи на входе плана, а гейт пакета считает
# `sh -c '<cmd>'` и `<cmd>` одной командой (`_strip_shell_wrapper`).
SHELL_WRAPPERS = tuple(spec_module.SHELL_WRAPPERS)

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


def _strip_shell_wrapper(text: str) -> str:
    """`sh -c '<cmd>'` → `<cmd>`: обёртка задаёт форму запуска, а не команду.

    Старые спеки (1.x seed, планы до 0.2.1, живые дела) несут `evidence.cmd`
    завёрнутым в `sh -c '…'`, а роль запускает инструмент напрямую — гард
    терминала режет обёртку. Прогон честный, команда та же, и судить её как
    подмену (`evidence.substituted`) значило наказывать за форму записи (живые
    дела `d_fb987c74`, `d_fe6c91d0`). Поэтому сравнение снимает внешнюю обёртку
    вместе с её кавычками: `sh -c 'make lint'` ≡ `make lint`. Что под обёрткой —
    сравнивается как есть, так что настоящая подмена (другая команда) остаётся
    подменой.
    """
    for wrapper in SHELL_WRAPPERS:
        if not text.startswith(wrapper + " ") and text != wrapper:
            continue
        rest = text[len(wrapper):].strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
            rest = rest[1:-1].strip()
        return rest
    return text


def _normalise_cmd(value: Any) -> str:
    """Команда без различий в пробелах и без обёртки оболочки.

    Нормализация — только про форму записи: пробелы схлопываются, внешний
    `sh -c '<cmd>'` снимается (`_strip_shell_wrapper`). Текст команды, меняющий
    смысл от порядка слов или от неразрывного пробела, обязан совпадать
    посимвольно.
    """
    return " ".join(_strip_shell_wrapper(" ".join(str(value or "").split())).split())


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
    - роль из `REPORT_WITNESS_ROLES` — этап проверяет, а не делает: его работа —
      отчёт, и коммит в нём называет, ЧТО смотрели;
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
    if str(getattr(stage, "role", "") or "") in REPORT_WITNESS_ROLES:
        # Роль без терминала или проверяющая чужими руками: дерево она не меняет,
        # её работа — отчёт, и свидетель у неё один и тот же (`WITNESS_REPORT`).
        return WITNESS_REPORT
    return WITNESS_WORK


def nothing_reason(result: dict[str, Any]) -> str:
    """Причина пустоты из отчёта: строка или пусто, если её нет.

    Причина — часть контракта, а не украшение: «работы не было» без неё не
    отвечает на вопрос «почему», и следующий, кто откроет дело, пойдёт искать
    работу заново. Функция одна на гейт и на движок: отказ `nothing.reason` и
    факт `attempt_ended` обязаны читать причину одинаково.
    """
    nothing = result.get(WITNESS_NOTHING)
    if not isinstance(nothing, dict):
        return ""
    return str(nothing.get("reason") or "").strip()


def _witness_problem(kind: str, result: dict[str, Any]) -> Optional[Issue]:
    """Чем свидетель этапа не предъявлен; `None` — предъявлен в своей форме.

    Отказ называет, что принимается **у этого** этапа: «нет коммита» на
    публикующем этапе отправляло исполнителя искать работу, которую тот и не
    делал (живой прогон TF3-c: `publish` и `review` отказывали всегда).
    """
    if kind in (WITNESS_VERDICT, WITNESS_PUBLISHED, WITNESS_PLAN, WITNESS_REPORT):
        # У этих этапов свидетель свой, и пустота его не заменяет: ревьюер,
        # публикующий и этап плана обязаны предъявить приговор, публикацию или
        # план — «работы не было» от них означало бы, что этап не сделал свою
        # работу, а не что работы для него не нашлось.
        if WITNESS_NOTHING in result:
            return Issue(
                _where(WITNESS_NOTHING),
                RULE_NOTHING_STAGE,
                f"свидетель этого этапа — {kind}, а не ничего: nothing принимается "
                "только у этапа-производителя (commit|artifacts)",
            )
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
            "этап со свидетелем-отчётом: нужен summary с находками; commit/artifacts "
            "не требуются — работа такого этапа это отчёт",
        )
    if WITNESS_NOTHING in result:
        # Пустота — альтернатива работе, а не дополнение к ней: причина обязана
        # назвать, почему править было нечего, а коммит и артефакты рядом с ней
        # судятся своими правилами (`nothing.commit`, `nothing.artifacts`) —
        # дерево отвечает следом (`nothing.dirty`).
        if not nothing_reason(result):
            return Issue(
                _where(WITNESS_NOTHING, key="reason"),
                RULE_NOTHING_REASON,
                "nothing без reason: назови одной строкой, почему работы не было "
                "(например: «research ответил: расхождений нет»)",
            )
        return None
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
        # Контракт попытки называет обязательным то, что спросит гейт. У этапа
        # без объявленных команд (`research`/`docs`: роль без терминала) схема
        # отчёта просит только `report`/`summary` — и отказ «доказательства
        # объявленных команд не предъявлены» был неправдой: команд не было,
        # а попытка терялась на отсутствующем пустом списке (живое: verify
        # d_a216d50b 14.11, researcher прислал done по своей же схеме).
        declared = tuple(getattr(stage, "evidence", ()) or ())
        if declared:
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
    entries: list[Any],
    root: Path,
    issues: list[Issue],
    *,
    measure: str,
    tree: Path,
    attempt: Optional[Path] = None,
) -> None:
    """Артефакты в дереве попытки: путь внутри, файл есть, sha256 совпадает.

    `measure` — как называется мерка в отказе (`дерево попытки` / `каталог
    попытки` / `каталог дела`), `tree` — где лежало бы дерево этой попытки,
    `attempt` — каталог попытки: второй корень для той же работы. Файл,
    найденный во втором корне, **принимается**: где именно он лежит — форма
    записи, а не содержание работы (живые дела `d_b9c480c8`, `d_76b269ce`,
    `d_95f55146`: артефакт `.duty/duty_test.log` объявлен от дерева, а лёг в
    каталог попытки). Место находки называется предупреждением `artifact.elsewhere`
    в `details`, а `artifact.missing` остаётся для «файла нет нигде» — с обоими
    корнями в тексте, чтобы человек не догадывался, где искали. Имена разные
    намеренно: найденный файл и пропавший не должны складываться в одно правило
    при разборе отказов (`contract_refusals.by_rule`).
    """
    others = [path for path in (tree, attempt) if path is not None and path != root]
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
            # Второй корень: та же работа, другая мерка. Принимается, но
            # называется — иначе «где лежит файл» выяснялось бы руками.
            found = next(
                (
                    candidate
                    for candidate in ((other / relative).resolve() for other in others)
                    if candidate.is_file()
                ),
                None,
            )
            if found is not None:
                issues.append(
                    Issue(
                        f"{where}.path",
                        RULE_ARTIFACT_ELSEWHERE,
                        f"артефакт найден не там, где искали: {found}; работа принята — "
                        f"объявляй путь от того корня, где этап работает ({measure})",
                        severity=WARNING,
                    )
                )
                target = found
            else:
                # Мерка в отказе — часть контракта: «файла нет» без неё заставляет
                # человека гадать, чего не хватило — файла или дерева.
                message = f"файла нет: {target}; искали относительно {measure}"
                if others:
                    message += f" и {', '.join(str(other) for other in others)}"
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


def _check_commit(
    commit: str,
    root: Path,
    base: Optional[str],
    issues: list[Issue],
    repo: Optional[Path] = None,
) -> None:
    """Коммит резолвится в дереве и не был предком базы до попытки.

    `repo` — каталог дела из спеки: вторая мерка для коммита. Нужна потому, что
    `workspace: none` у производящего этапа оставляет процесс в каталоге попытки,
    а работу исполнитель нередко фиксирует в живом чекауте (`d_a8213c01`: коммит
    `b7608f3` есть в ветке дела, а `commit.resolve` искал его в песочнице и
    отказал — этап упал контрактом уже после того, как всё было сделано). Коммит,
    найденный в каталоге дела, принимается, но называется предупреждением: работа
    в ветке, а этап работал не там, где проверяют.
    """
    resolved = _rev_parse(root, commit)
    measured = root
    if resolved is None and repo is not None and repo != root:
        found = _rev_parse(repo, commit) if repo.is_dir() else None
        if found is not None:
            resolved, measured = found, repo
            issues.append(
                Issue(
                    _where("commit"),
                    RULE_COMMIT_RESOLVE,
                    f"commit={commit!r} нашёлся в каталоге дела {repo}, а не в дереве попытки "
                    f"{root}: этап работал вне дерева — назови workspace: dir или worktree",
                    severity=WARNING,
                )
            )
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
    base_sha = _rev_parse(measured, str(base))
    if base_sha is None:
        issues.append(
            Issue(
                _where("commit"),
                RULE_COMMIT_BASE,
                f"база {base!r} не резолвится в {measured}: предок не проверен",
                severity=WARNING,
            )
        )
        return
    ancestor = _is_ancestor(measured, resolved, base_sha)
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
                f"commit={commit} уже был предком базы {base} до попытки: либо это чужое "
                "готовое, либо работы для этапа не было — тогда предъяви "
                "nothing: {reason} вместо commit",
            )
        )


def _check_commit_reference(commit: str, root: Path, issues: list[Issue]) -> None:
    """Коммит, названный этапом со своим свидетелем: ссылка, а не доказательство.

    У вердиктного/публикующего этапа и у роли с отчётом коммит ничего не
    доказывает — он называет, ЧТО смотрели (`docs/ENGINE-2.0.md`, свидетель).
    Поэтому проверяется ровно одно: имя резолвится в дереве. Ни «предок базы»
    (`commit.foreign`), ни поиск в каталоге дела здесь не судятся: текст
    `commit.foreign` велит предъявить работу, которой от этого этапа никто не
    требует, — и одобрение падало за точность формулировки (d_3b6306a1).
    Нерезолвящееся имя — предупреждение: оно не отказ, потому что ссылка может
    вести на коммит, которого в песочнице этапа нет (дерево shallow, чужой реф).
    """
    if _rev_parse(root, commit) is not None:
        return
    issues.append(
        Issue(
            _where("commit"),
            RULE_COMMIT_RESOLVE,
            f"commit={commit!r}: ссылка не резолвится в {root} — этап со своим свидетелем "
            "не доказывает работу коммитом, но имя должно указывать на существующий коммит",
            severity=WARNING,
        )
    )


def _check_nothing(root: Path, base: Optional[str], issues: list[Issue]) -> None:
    """Пустота проверяема или её нет: `HEAD` равен базе, а `git status` пуст.

    «Работы не было» — законный исход этапа-производителя, но только если дерево
    это подтверждает. Иначе свидетель стал бы способом уйти от работы: сказать
    «править нечего» и не править. Поэтому доказательство берётся у самого дерева
    (`git status --porcelain` и `HEAD` против базы попытки), а не из слов отчёта.
    """
    code, out = _git(["status", "--porcelain"], root)
    if code != 0:
        issues.append(
            Issue(
                _where(WITNESS_NOTHING),
                RULE_NOTHING_DIRTY,
                f"git status --porcelain не ответил в {root}: {out or code} — "
                "пустота не доказана",
            )
        )
        return
    if out.strip():
        issues.append(
            Issue(
                _where(WITNESS_NOTHING),
                RULE_NOTHING_DIRTY,
                f"ты назвал пустоту, а в дереве изменения: {out[:120]}",
            )
        )
        return
    if not base:
        # База не объявлена — сравнивать `HEAD` не с чем. Дерево при этом чисто:
        # этого достаточно, чтобы «работы не было» не осталось без проверки.
        return
    head = _rev_parse(root, "HEAD")
    base_sha = _rev_parse(root, str(base))
    if base_sha is None:
        issues.append(
            Issue(
                _where(WITNESS_NOTHING),
                RULE_COMMIT_BASE,
                f"база {base!r} не резолвится в {root}: HEAD с базой не сверен",
                severity=WARNING,
            )
        )
        return
    if head is None:
        issues.append(
            Issue(
                _where(WITNESS_NOTHING),
                RULE_NOTHING_DIRTY,
                f"HEAD не резолвится в {root}: пустота не доказана",
            )
        )
        return
    if head != base_sha:
        issues.append(
            Issue(
                _where(WITNESS_NOTHING),
                RULE_NOTHING_DIRTY,
                f"ты назвал пустоту, а в дереве работа после базы: HEAD {head[:12]}… "
                f"не равен базе попытки {base_sha[:12]}…",
            )
        )


def _check_nothing_work(
    commit: str, artifacts: Any, root: Path, base: Optional[str], issues: list[Issue]
) -> None:
    """Работа рядом с пустотой: коммит и артефакты судятся правилами пустоты.

    Отчёт «работы не было» с базовым коммитом — не чужая работа, а сама пустота,
    названная дважды: контракт требует доказательства, а дерево на чистом месте
    предъявляет базу. Поэтому предок базы принимается, а работа (новый коммит или
    перечисленные файлы) называется своим правилом — иначе исполнитель читает
    `commit.foreign`, чей текст велит предъявить `nothing`, уже предъявленный.
    """
    named = artifacts if isinstance(artifacts, list) else []
    if named:
        issues.append(
            Issue(
                _where("artifacts"),
                RULE_NOTHING_ARTIFACTS,
                f"в отчёте nothing, а артефактов {len(named)}: работа была — убери "
                "nothing и назови её в artifacts",
            )
        )
    if not commit:
        return
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
    if not ancestor:
        issues.append(
            Issue(
                _where("commit"),
                RULE_NOTHING_COMMIT,
                f"commit={resolved[:12]}… не предок базы {base[:12]}…: работа была — "
                "убери nothing и назови её коммитом",
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
    repo: Optional[str] = None,
    merge_brief: Optional[dict[str, Any]] = None,
) -> Verdict:
    """Приговор пакету попытки: сверка заявленного с деревом, без запуска команд.

    `root` — рабочий каталог попытки (cwd процесса: дерево `worktree`, каталог
    дела `dir`, каталог попытки `none`): относительные пути артефактов режутся по
    нему, а не по `workdir`, где лежит отчёт. `base` — база спеки: по ней
    проверяется, что заявленный коммит не был готов до попытки. `workdir` — не
    только место отчёта: дерево попытки лежит внутри него (`TREE_SUBDIR`), и
    отказ `artifact.missing` по этому пути отличает «файла нет» от «этап смотрел
    в каталог, а файл — в дереве». `merge_brief` — бриф, который движок передал
    резолверу слияния: по нему отчёт слияния сверяется с фактом, а не сам с собой.
    """
    if not isinstance(result, dict):
        return Verdict(
            ok=False,
            kind=KIND_CONTRACT,
            details=[Issue(_where(""), RULE_REPORT, f"отчёт — объект, а пришло {type(result).__name__}")],
        )

    root_path = Path(root if root is not None else workdir).resolve()
    # Каталог дела — вторая мерка коммита: у `workspace: none` процесс стоит в
    # каталоге попытки, а коммит исполнитель делает в живом чекауте.
    repo_path = Path(str(repo)).expanduser().resolve() if repo else None
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
        commit = result.get("commit")
        commit_text = commit.strip() if isinstance(commit, str) else ""
        # Пустота проверяется только там, где она вообще может быть принята
        # (`_witness_problem` уже отказал этапам со своим свидетелем и назвал
        # причину): пустое дерево у ревьюера ничего бы не доказывало.
        accepted_nothing = WITNESS_NOTHING in result and witness(stage) == WITNESS_WORK
        if accepted_nothing:
            # Принятая пустота переводит коммит и артефакты в другой вопрос: не
            # «докажи работу», а «работа всё-таки была?». Живой случай d_12d5fc44:
            # исполнитель сделал ровно то, что велит контракт (`nothing` + база
            # дерева), а `_check_commit` называл это `commit.foreign` — отказом,
            # чей текст велит предъявить `nothing`, уже предъявленный. Поэтому
            # рядом с пустотой судятся её правила, а дерево отвечает следом.
            _check_nothing_work(commit_text, artifacts, root_path, base, issues)
            _check_nothing(root_path, base, issues)
        else:
            _check_artifacts(
                list(artifacts or []),
                root_path,
                issues,
                measure=measure,
                tree=tree_path,
                attempt=Path(workdir).resolve(),
            )
            if commit_text:
                # Коммит — свидетель РАБОТЫ (`witness` = `commit|artifacts`). У
                # вердиктного этапа, у публикующего, у этапа плана и у роли с
                # отчётом свидетель свой (`verdict`/`published`/`plan`/`report`),
                # и контракт прямо говорит «commit/artifacts не требуются». Но
                # названный коммит там — обычное дело: ревьюер пишет «смотрел
                # вот этот», — и судить его как присвоение работы значило отказывать
                # одобрение за точность (живое дело d_3b6306a1: шесть попыток
                # ревью и ~23 млн токенов на отказе `commit.foreign`). Поэтому у
                # таких этапов коммит читается как ССЫЛКА: спрашиваем только,
                # резолвится ли он в дереве.
                if witness(stage) == WITNESS_WORK:
                    _check_commit(commit_text, root_path, base, issues, repo=repo_path)
                else:
                    _check_commit_reference(commit_text, root_path, issues)
            # Слияние (0.2.2): свидетель — merge-коммит, и отчёт называет, что
            # взято с каждой стороны. Проверка идёт после коммита: сначала «это
            # вообще коммит этого дерева», потом «это коммит слияния».
            if str(getattr(stage, "kind", "") or "") == spec_module.MERGE_KIND:
                _check_merge(result, stage, root_path, commit_text, issues, brief=merge_brief)
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
    # пакета нет вовсе, и называть там нечего. Принятая пустота называется своим
    # свидетелем: в журнале должно быть видно, что этап закрыт не работой, а
    # `nothing` — иначе «принято» сливается с «работой».
    accepted = None
    if status == STATUS_DONE:
        accepted = WITNESS_NOTHING if WITNESS_NOTHING in result else witness(stage)
    errors = [issue for issue in issues if issue.severity == ERROR]
    if not errors:
        return Verdict(ok=True, kind=None, details=issues, witness=accepted)
    kind = KIND_CONTRACT if any(issue.kind == KIND_CONTRACT for issue in errors) else KIND_CAPABILITY
    return Verdict(ok=False, kind=kind, details=issues)


def _git_lines(args: list[str], cwd: Path) -> list[str]:
    """`git` в дереве — список строк stdout: списку файлов нужны переводы строк.

    `_git` собирает ответ одной строкой (так читается причина отказа), а конфликт
    называть одной строкой нельзя: имена файлов — список, по которому человек
    ищет, что разводить.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]


def _unmerged(root: Path) -> list[str]:
    """Файлы, оставшиеся в конфликте слияния: `git diff --name-only --diff-filter=U`."""
    return _git_lines(["diff", "--name-only", "--diff-filter=U"], root)


def _parents_count(root: Path, commit: str) -> Optional[int]:
    """Сколько родителей у коммита; `None` — git не ответил."""
    code, out = _git(["rev-list", "--parents", "-n", "1", commit], root)
    if code != 0 or not out:
        return None
    return max(0, len(out.split()) - 1)


def _check_merge(
    result: dict[str, Any],
    stage: Any,
    root: Path,
    commit: str,
    issues: list[Issue],
    brief: Optional[dict[str, Any]] = None,
) -> None:
    """Пакет этапа-слияния: merge-коммит, обе стороны названы, конфликтов не осталось.

    Свидетель слияния — коммит, и он обязан быть **merge-коммитом** (два и более
    родителя): коммит с одним родителем означает, что одна полоса осталась за
    бортом, даже если отчёт говорит «слито» (`merge.parents`). Вторая половина —
    отчёт: полос две (и больше), и про каждую надо сказать, что из неё взято
    (`merge.dropped`). «Взял одну сторону целиком» — не запрещённое решение, но
    оно называется (`taken` + `why`): тихое «выбрал первую попавшуюся» — это и
    есть та потеря логики, ради которой версия делалась.

    `brief` — то, что движок передал резолверу (файл контракта попытки): в нём
    конфликтные файлы и коммиты полос. С ним отчёт сверяется не по слову: файл,
    разведённый молча, сторона не из этого слияния и сторона, названная чужим
    коммитом, — тот же `merge.dropped`, только теперь проверяемый по факту, а не
    по обещанию отчёта.

    Конфликт, оставшийся в дереве, судится первым и называется файлами
    (`merge.conflict`): пока он там, коммита слияния нет, и остальные проверки
    говорили бы о несуществующем пакете.
    """
    conflicts = _unmerged(root)
    if conflicts:
        issues.append(
            Issue(
                _where("merge", None, "files"),
                RULE_MERGE_CONFLICT,
                f"слияние не собрано: в дереве {root} остался конфликт — файлы "
                f"{', '.join(conflicts)}; разведи их (обе стороны) и закоммить",
            )
        )
        return
    block = result.get("merge")
    if not isinstance(block, dict):
        issues.append(
            Issue(
                _where("merge"),
                RULE_REPORT,
                "этап-слияние отчитывается полем merge: {files, sides} — по каждой полосе "
                "{lane, sha, intent, joined}, иначе не видно, что взято с каждой стороны",
            )
        )
        return
    if not isinstance(block.get("files"), list):
        issues.append(
            Issue(
                _where("merge", None, "files"),
                RULE_REPORT,
                "merge.files — список конфликтных файлов (пустой список, если конфликтов не было)",
            )
        )
    named: dict[str, Any] = {}
    sides = block.get("sides")
    for position, side in enumerate(sides if isinstance(sides, list) else []):
        if not isinstance(side, dict):
            issues.append(Issue(_where("merge", None, "sides"), RULE_REPORT, f"sides[{position}] — словарь"))
            continue
        lane = str(side.get("lane") or "").strip()
        if lane:
            named[lane] = side
        if not str(side.get("intent") or "").strip():
            issues.append(
                Issue(
                    _where("merge", None, "sides"),
                    RULE_MERGE_DROPPED,
                    f"сторона {lane or '—'} не назвала намерения (intent): что она хотела — "
                    "неизвестно, и «сохранено» не проверить",
                )
            )
        if not str(side.get("joined") or "").strip():
            issues.append(
                Issue(
                    _where("merge", None, "sides"),
                    RULE_MERGE_DROPPED,
                    f"сторона {lane or '—'} не назвала, что из неё вошло в результат (joined)",
                )
            )
    for need in tuple(getattr(stage, "needs", ()) or ()):
        if str(need) in named:
            continue
        issues.append(
            Issue(
                _where("merge", None, "sides"),
                RULE_MERGE_DROPPED,
                f"полоса {str(need)!r} не названа в отчёте: её работа либо вошла молча, "
                "либо потеряна — назови {lane, sha, intent, joined}",
            )
        )
    taken = str(block.get("taken") or "").strip()
    if taken and not str(block.get("why") or "").strip():
        issues.append(
            Issue(
                _where("merge", None, "taken"),
                RULE_MERGE_DROPPED,
                f"сторона {taken!r} взята целиком, а «why» пуст: «взял одну сторону целиком» — "
                "решение, а не тишина — назови, почему намерение другой стороны сохранить "
                "было нельзя (или сохрани его)",
            )
        )
    if brief is not None:
        _check_merge_against_brief(block, named, taken, brief, issues)
    if not commit:
        return
    count = _parents_count(root, commit)
    if count is not None and count < 2:
        issues.append(
            Issue(
                _where("commit"),
                RULE_MERGE_PARENTS,
                f"commit={commit} — коммит с одним родителем, а свидетель слияния — merge-коммит "
                "с двумя и более: полоса, которой нет среди родителей, в него не вошла",
            )
        )


def _check_merge_against_brief(
    block: dict[str, Any],
    named: dict[str, Any],
    taken: str,
    brief: dict[str, Any],
    issues: list[Issue],
) -> None:
    """Сверка отчёта слияния с брифом, который движок передал резолверу.

    Бриф называет конфликтные файлы и коммит каждой полосы. Отчёт обязан говорить
    о том же: иначе «обе стороны сохранены» проверялось бы по слову отчёта, а не
    по факту. Класс у всех трёх находок один — `merge.dropped`: разведено молча
    (файла нет в `merge.files`), сторона не из этого слияния, сторона названа
    чужим коммитом. Во всех трёх случаях по отчёту не видно, чья работа вошла в
    результат, — а это ровно та потеря логики, ради которой версия делалась.
    """
    lanes = {
        str(lane.get("lane") or ""): str(lane.get("sha") or "")
        for lane in brief.get("lanes") or ()
        if isinstance(lane, dict) and str(lane.get("lane") or "")
    }
    files = block.get("files")
    resolved = {str(name) for name in files} if isinstance(files, list) else set()
    for name in brief.get("conflict_files") or ():
        if str(name) in resolved:
            continue
        issues.append(
            Issue(
                _where("merge", None, "files"),
                RULE_MERGE_DROPPED,
                f"файл {str(name)!r} был в конфликте, а в merge.files его нет: конфликт разведён "
                "молча — назови файлы, которые действительно правил",
            )
        )
    for lane_name, side in named.items():
        if lane_name not in lanes:
            issues.append(
                Issue(
                    _where("merge", None, "sides"),
                    RULE_MERGE_DROPPED,
                    f"сторона {lane_name!r} не полоса этого слияния "
                    f"(полосы: {', '.join(sorted(lanes)) or '—'}): отчёт называет источник, "
                    "которого вливание не касалось",
                )
            )
            continue
        reported = str(side.get("sha") or "").strip().lower()
        actual = lanes[lane_name].strip().lower()
        if reported and actual and min(len(reported), len(actual)) >= 7 and (
            actual.startswith(reported) or reported.startswith(actual)
        ):
            continue
        issues.append(
            Issue(
                _where("merge", None, "sides"),
                RULE_MERGE_DROPPED,
                f"сторона {lane_name!r} названа коммитом {reported[:12] or '—'}, "
                f"а полоса — {actual[:12] or '—'}: чья работа вошла, не проверить",
            )
        )
    if taken and taken not in lanes:
        issues.append(
            Issue(
                _where("merge", None, "taken"),
                RULE_MERGE_DROPPED,
                f"взята целиком сторона {taken!r}, которой нет среди полос слияния "
                f"({', '.join(sorted(lanes)) or '—'}): обоснование говорит не о той стороне",
            )
        )


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
        # Отчёт и есть свидетель: `commit|artifacts` взять неоткуда — требование
        # повторяло бы witness. Объявленные команды — другое дело: их гейт
        # проверяет отдельно от свидетеля (`_check_evidence`), поэтому роль,
        # которая их объявила (tester гоняет `go test` и `make lint`), обязана
        # принести и записи по ним.
        required = [WITNESS_REPORT, "summary"] if not evidence else [WITNESS_REPORT, "evidence", "summary"]
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
    nothing_offer: dict[str, str] = {}
    merge: dict[str, Any] = {}
    if str(getattr(stage, "kind", "") or "") == spec_module.MERGE_KIND:
        # Отчёт слияния — «что взято с каждой стороны»: без этих полей отказ
        # (merge.dropped) узнавался бы только после работы, а требование
        # сохранности обеих сторон — часть контракта (спека 0.2.2, пункт 4).
        merge = {
            "files": "список разведённых конфликтных файлов (пустой — слияние прошло без конфликта)",
            "sides": "по каждой полосе {lane, sha, intent, joined}: что сторона хотела и что из неё вошло",
            "taken": "id полосы, чьё изменение выброшено целиком (тогда why обязателен)",
            "why": "одна строка: почему сторону нельзя было сохранить",
        }
    if wanted == WITNESS_WORK:
        # Пустота — законный исход этапа-производителя, и о ней исполнитель должен
        # узнать из контракта, а не из отказа `commit.foreign`: «править нечего»
        # без этого поля читалось как «работа не названа». Требование поэтому
        # читается как выбор: работа (`commit|artifacts`) **или** пустота. Список,
        # который обещает `commit|artifacts` и рядом предлагает `nothing`, велит
        # прислать оба поля — и гейт называл это `commit.foreign`.
        required = [f"{WITNESS_WORK}|{WITNESS_NOTHING}", "evidence", "summary"]
        nothing_offer = {
            "reason": "вместо commit/artifacts: почему работы не было "
            "(дерево обязано быть чистым: HEAD = база попытки, git status пуст). "
            "commit можно указать как базу или её предка — это то же доказательство; "
            "новый коммит с nothing — отказ nothing.commit, артефакты — nothing.artifacts"
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
    if nothing_offer:
        schema["nothing"] = nothing_offer
    if merge:
        schema["merge"] = merge
    return schema
