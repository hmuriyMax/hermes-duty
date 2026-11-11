"""Спека дела `duty.yaml`: загрузчик и валидатор — чистые функции без БД и сети.

Почему это отдельный слой. Спека — единственное место, где сказано, из каких
этапов состоит работа, какие доказательства принимаются и куда идёт вопрос.
Ошибка в ней сегодня проявляется поздно: «публикация обогнала ревью» — это
публикующий этап без ревью в предках или неправильный `verdict.changes`, которые
видно только в момент публикации, а «этап недостижим» — только когда движок до
него никогда не дойдёт. Поэтому контракт читается **до** запуска: `load_spec()`
разбирает файл, `validate_spec()` возвращает список отказов, и ни один из них не
требует запускать что-либо.

Границы слоя:

- только `yaml.safe_load` для значений — конструкторы объектов не вызываются;
  позиции узлов (нужны для `file:line`) читаются отдельно через `yaml.compose`
  с тем же `SafeLoader`, это разбор структуры, а не сборка объектов;
- неизвестный ключ — отказ, а не «молча проглотить»: опечатка в `evidance`
  иначе выглядела бы как этап без доказательств;
- функции без БД, сети и запуска процессов: `validate_spec()` не открывает ничего,
  кроме снимка каталога профилей `~/.hermes/profiles/*` (+ `default`), который
  кэшируется в памяти и может быть передан явно. Проверка `base` как rev живёт
  отдельной функцией `check_base()` с подставляемой проверкой rev
  (`git rev-parse --verify`), чтобы тест не зависел от машин с чужими
  репозиториями.

Путь к спеке — всегда абсолютный. Это не придирка: `duty.yaml` лежит в
репозитории дела, а читает его движок из `~/.hermes`, и относительный путь
не даёт восстановить «откуда взялась спека» в журнале.

Контракт формата — `docs/SPEC.md`; он же проверяется `tests/test_spec.py`.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

import yaml

# --------------------------------------------------------------- ключи формата

TOP_KEYS = ("duty", "repo", "base", "budget", "charter", "stages")
STAGE_KEYS = (
    "id",
    "kind",
    "role",
    "workspace",
    "needs",
    "parallel_with",
    "evidence",
    "retry",
    "budget",
    "ask",
    "allow",
    "verdict",
    "cmd",
    "must",
    "artifacts",
)
EVIDENCE_KEYS = ("cmd", "must", "timeout_sec")
RETRY_KEYS = ("max", "on")
BUDGET_KEYS = ("tokens", "minutes")
ASK_KEYS = ("allowed", "to_human", "max_hops", "q", "options", "risky")
VERDICT_KEYS = ("approve", "changes", "max_rounds")
# Хартия (1.0, `docs/ENGINE-1.0.md`): неизменяемые границы дела — потолок числа
# этапов, сколько раз план может быть возвращён на реплан, кем и какими видами
# этапов план вправе расти, когда план спрашивает человека. Спека без блока
# годна: дефолты — это поведение 0.0.8, где план никто не предлагает.
CHARTER_KEYS = ("max_stages", "max_rounds_replan", "allowed_roles", "allowed_kinds", "plan_gate")
PLAN_GATE_KEYS = ("min_stages", "budget_share")
# Что предложение плана вправе сказать про этап, который **уже есть** в графе:
# только порядок (`needs`) и параллель (`parallel_with`). Остальные поля были бы
# переписыванием этапа, а этап объявляется спекой (`parse_plan`, правило `plan.id`).
REORDER_KEYS = ("id", "needs", "parallel_with")
# Цена предложения (`docs/ENGINE-1.0.md`, раздел 5): план называет свою стоимость в
# токенах сам, а не выводится из этапных бюджетов. Ключ один — «токены»; лимиты по
# этапам как политика в 0.1.0 не объявляются.
PLAN_COST_KEYS = ("tokens",)

WORKSPACES = ("worktree", "dir", "none")
TO_HUMAN = ("decisions_only", "never")

# Виды этапа (спека P2, `docs/SPEC.md`): `agent` — процесс профиля роли,
# `shell` — команда с кодом возврата вместо отчёта, `gate` — остановка на
# человеке: этап ничего не исполняет, а задаёт вопрос и читает ответ. Умолчание —
# `agent`: спеки 0.0.4 объявляют этапы без `kind` и обязаны читаться без правок.
STAGE_KINDS = ("agent", "shell", "gate")
DEFAULT_STAGE_KIND = "agent"
SHELL_KIND = "shell"
GATE_KIND = "gate"

# Этап, чей отчёт — предложение плана (1.0, `docs/ENGINE-1.0.md`, раздел 3): в
# seed-графе он называется `coordinator` — координатор дела, он же ведёт его по
# ходу работы и закрывает (решение Макса, 11.11). Слово одно на плагин — по нему
# `runner` требует план отчётом, `transit` читает факты плана, а `evidence`
# принимает предложение свидетелем: три копии строки разошлись бы на первом
# переименовании. `plan` — прежнее имя: дела, заведённые до переименования,
# пишут его в журнале, и их факты читаются через `is_plan_stage`.
PLAN_STAGE = "coordinator"
PLAN_STAGE_LEGACY = ("plan",)


def is_plan_stage(stage_id: Any) -> bool:
    """Этот ли этап — вход дела (координатор): новое имя плюс старые журналы."""
    return str(stage_id or "") == PLAN_STAGE or str(stage_id or "") in PLAN_STAGE_LEGACY


# Профиль, под которым живёт роль координатора: отдельного профиля `coordinator`
# в Hermes нет — процесс координатора — это профиль `lead` (права и скиллы те же,
# имя роли в спеке и журнале — своё).
COORDINATOR_PROFILE = "lead"
PROFILE_ALIASES = {"coordinator": COORDINATOR_PROFILE}


def spawn_profile(role: str) -> str:
    """Профиль Hermes для роли этапа: координатор живёт под профилем `lead`."""
    return PROFILE_ALIASES.get(str(role or ""), str(role or ""))

# Этап, который **закрывает дело** (1.0, `docs/ENGINE-1.0.md`, разделы 9 и 10): в
# seed-графе он называется `close` и идёт ролью лида. Слово одно на плагин — по
# нему валидатор требует приговора и роли, `deals` не даёт закрыть дело «все
# этапы пройдены», а прогон пишет `completed cause=lead_closed` из отчёта лида.
CLOSE_STAGE = "close"

# Роль закрывающего этапа: итог дела — слово координатора, а не текст движка
# (решение Макса, 11.11: close делает тот же координатор, что вёл дело). Хартия
# `allowed_roles` этим набором не расширяется: `close` объявляется в seed-спеке,
# а планом его не добавляют (`parse_plan` отказывает на занятый `id`). `lead` —
# прежняя роль: seed-спеки до переименования валидны и читаются дальше.
CLOSE_ROLES = ("coordinator", "lead")
CLOSE_ROLE = "coordinator"

# Роли координатора — те же, что закрывают дело. Правило `coordinator.scope`
# (решение Макса, 12.11): координаторская роль допустима только на входном этапе
# (без `needs`) и на `close`. Работу делает роль-исполнитель, а не координатор.
# Причина не эстетическая: у профиля `lead` есть чтение, Confluence и GitLab, а
# терминала и записи нет — координатору дешевле прочитать доку самому, чем
# растить граф. Так дело d_48e01a0e ушло в `check` ролью координатора вместо
# этапа `researcher`: 5 этапов researcher на 28 предложенных планов и 3 попытки
# researcher на 61 дело.
COORDINATOR_ROLES = ("coordinator", "lead")

# Дефолты хартии (`docs/ENGINE-1.0.md`, раздел 5): без блока `charter` дело
# живёт по ним, и это ровно поведение 0.0.8 — план никто не предлагает, граф не
# растёт. `max_stages = 12` — потолок графа, `max_rounds_replan = 3` — сколько раз
# этап плана может быть переоткрыт отказом, прежде чем дело получит `failed(contract)`.
DEFAULT_MAX_STAGES = 12
DEFAULT_MAX_ROUNDS_REPLAN = 3

# Код возврата shell-этапа по умолчанию: «команда сходилась» — это 0.
DEFAULT_SHELL_MUST = 0

# Роли, которые действуют вовне: публикуют, мержат, деплоят. Набор — имена
# профилей Hermes, которые заводят под эти права (`publisher`; `deployer`, если
# такой профиль появится). Держится одной константой, чтобы «кто публикует» не
# разъезжалось по коду: по ней решается, требовать ли ревью в предках.
PUBLISHING_ROLES = ("publisher", "deployer")

# Роль, которая выносит приговор. Ревью в предках публикующего этапа — то, что
# делает «гейти PUB на RV, а не на IMP» проверкой, а не договорённостью в скилле.
REVIEWING_ROLE = "reviewer"

# Роли без терминала: их профили не запускают команд (`terminal` в
# disabled_toolsets). Такие этапы доказывают себя отчётом и приговором, а не
# кодом возврата, и требовать от них `evidence {cmd, must}` — заведомо
# невыполнимое требование. Цена ошибки видна на деле d_48e01a0e: лид не смог
# запланировать исследовательский этап (валидатор требовал evidence) и сделал
# всю сверку кода с докой сам в этапе координатора, а в граф вынес только close.
#
# `documenter` — та же природа по другой причине: он пишет документ, а не код
# (`docs/ENGINE-1.0.md` §9: «docs (documenter) → что изменилось и как это
# проверять»). Его результат — заметка тикета и отчёт, дерева репозитория он не
# меняет, поэтому коммита и артефактов внутри дерева у него нет и быть не может.
# Команду-доказательство спека ему всё равно может объявить (`test -s`, `grep -q`
# по заметке) — это проверяется отдельно от свидетеля.
EVIDENCE_FREE_ROLES = (REVIEWING_ROLE, "researcher", "documenter")

# Роли, чьи профили не запускают команд (`terminal` в disabled_toolsets): лид,
# ресерчер, ревьюер. Объявлять такому этапу `evidence` бессмысленно: роль не
# может выполнить команду, а `verify()` не запускает её повторно — он сверяет
# заявленный код возврата с `must`, то есть при невыполнимом требовании отчёт
# либо сочиняет код, либо падает по контракту. Живой случай (d_e575494f): лид
# поставил ревьюеру `go build`/`make lint-full`/`make test`, попытка ревью
# упала на `evidence.missing`, и этап ушёл в переосмысление вместо приговора.
# Отказ поэтому даётся на входе плана — там, где появляется новая работа.
NO_SHELL_ROLES = tuple(dict.fromkeys((*COORDINATOR_ROLES, "researcher", REVIEWING_ROLE)))

# Команды, которые сами меняют мир: это действие, а не доказательство. Спека
# принимает только то, что можно проверить повторным запуском, — поэтому такие
# `cmd` отказывают до запуска, а не после публикации.
FORBIDDEN_CMD_PREFIXES = ("git push", "git commit", "rm -rf", "sudo")

# `timeout_sec` больше часа — это не доказательство, а зависание: отказ.
MAX_TIMEOUT_SEC = 3600

# Рабочее дерево этапа отводится от базы (`docs/SPEC.md`, правило `base`).
GIT_TIMEOUT_SEC = 10

DEFAULT_TIMEOUT_SEC = MAX_TIMEOUT_SEC

# Профили Hermes: снимок каталога читается один раз на процесс.
IMPLICIT_PROFILES = ("default",)


def profiles_dir() -> Path:
    """Каталог профилей: `$HERMES_HOME/profiles`, иначе `~/.hermes/profiles`.

    Корень берётся из `HERMES_HOME`, а не из домашней папки: на Windows home
    Hermes живёт в `%LOCALAPPDATA%\\hermes`, и жёсткий `~/.hermes` давал валидатору
    пустой снимок — спека с ролью `implementer` отказывала «профиля нет».

    `HERMES_HOME` бывает и профилем (`<root>/profiles/<role>`): в профильной
    оболочке `$HERMES_HOME/profiles` не существует, и валидатор видел один
    `default` — любая спека с ролями отказывала «профиля нет в Hermes», хотя
    профили есть. Тогда каталог профилей лежит на уровень выше.
    """
    env = os.environ.get("HERMES_HOME")
    root = Path(env).expanduser() if env else Path.home() / ".hermes"
    if root.parent.name == "profiles" and root.parent.is_dir() and not (root / "profiles").is_dir():
        return root.parent
    return root / "profiles"

ERROR = "error"
WARNING = "warning"


# --------------------------------------------------------------------- отказы


@dataclass(frozen=True)
class Issue:
    """Один отказ или предупреждение спеки: `file:line: правило: текст`.

    `severity` различает «спека не запускается» (`error`) и «правило проверить
    не удалось» (`warning`, например репозиторий дела на этой машине не
    выложен). Предупреждение не превращает годную спеку в отказ, но и не
    исчезает из вывода.
    """

    path: Path
    line: int
    rule: str
    message: str
    severity: str = ERROR

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.message}"


class SpecError(ValueError):
    """Спека не читается вовсе: путь, YAML, отсутствие обязательного поля.

    Отличается от `Issue` тем, что после такой ошибки проверять нечего: без
    `duty` и `stages` объект спеки не существует.
    """

    def __init__(self, path: Path, line: int, rule: str, message: str) -> None:
        self.path = Path(path)
        self.line = line
        self.rule = rule
        self.message = message
        super().__init__(f"{self.path}:{line}: {rule}: {message}")

    def issue(self) -> Issue:
        """Тот же отказ в форме списка отказов — CLI печатает одним форматом."""
        return Issue(self.path, self.line, self.rule, self.message)


# ------------------------------------------------------------------- структуры


@dataclass(frozen=True)
class Evidence:
    """Доказательство этапа: команда и её обязательный код возврата."""

    cmd: str
    must: int
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    line: int = 0


@dataclass(frozen=True)
class Retry:
    """Политика ретраев: сколько раз и на каких классах отказов."""

    max: int = 0
    on: tuple[str, ...] = ()
    line: int = 0


@dataclass(frozen=True)
class Budget:
    """Потолок расхода: токены и минуты. Незаданное поле — не ограничено.

    Одна форма на два уровня: `budget` этапа — потолок одной попытки (у гейта —
    срок ответа человека), `budget` в корне спеки — потолок **дела целиком**,
    суммарно по всем этапам и попыткам (`dashboard/budget.py` считает расход).
    """

    tokens: Optional[int] = None
    minutes: Optional[int] = None
    line: int = 0


@dataclass(frozen=True)
class Ask:
    """Адрес вопросов этапа: кому фактовые, идёт ли властный в пул, сколько хопов."""

    allowed: tuple[str, ...] = ()
    to_human: str = "decisions_only"
    max_hops: int = 0
    q: str = ""
    options: tuple[str, ...] = ()
    risky: bool = False
    line: int = 0


@dataclass(frozen=True)
class Verdict:
    """Приговор ревью: куда идти на approve, куда на changes, сколько раундов."""

    approve: str = "next"
    changes: str = ""
    max_rounds: int = 1
    line: int = 0


@dataclass(frozen=True)
class PlanGate:
    """Порог, после которого план обязан спросить человека (`ask()`).

    Зеро в любом поле — гейт по этому признаку выключен: спека без `plan_gate`
    ведёт себя как 0.0.8, а вопрос человеку не появляется из ниоткуда.
    `min_stages` считает **весь** граф (seed плюс принятый план), `budget_share` —
    долю бюджета дела, которую план забирает этапными бюджетами.
    """

    min_stages: int = 0
    budget_share: float = 0.0
    line: int = 0


@dataclass(frozen=True)
class Charter:
    """Хартия дела: границы, в которых лид вправе растить граф (`parse_plan`).

    Спека без блока `charter` получает эти значения — то есть ведёт себя как
    спека 0.0.8: граф задан файлом и не растёт.

    - `max_stages` — потолок числа этапов дела (seed плюс добавленные планом);
    - `max_rounds_replan` — сколько отказов плана этап `plan` переживает до
      `failed(contract)`;
    - `allowed_roles` — роли, которыми плану разрешено добавлять этапы; пусто —
      хартия роль не ограничивает (действует обычная проверка профиля);
    - `allowed_kinds` — виды этапов, которые план вправе добавить;
    - `plan_gate` — когда план уходит вопросом человеку.
    """

    max_stages: int = DEFAULT_MAX_STAGES
    max_rounds_replan: int = DEFAULT_MAX_ROUNDS_REPLAN
    allowed_roles: tuple[str, ...] = ()
    allowed_kinds: tuple[str, ...] = STAGE_KINDS
    plan_gate: PlanGate = PlanGate()


@dataclass(frozen=True)
class Stage:
    """Этап дела. `workspace: none` — этап только читает и выносит приговор.

    `kind` решает, кто исполняет этап: `agent` — процесс профиля `role`,
    `shell` — команда `cmd` с обязательным кодом возврата `must` и, если
    объявлены, артефактами `artifacts`, `gate` — никто: этап задаёт вопрос
    человеку (`ask.q`/`ask.options`) и читает ответ (допустимые — `allow`).
    Поля чужого вида у этапа ничего не значат и потому отказывают
    (`_check_kind`), а не игнорируются.
    """

    id: str
    role: str
    kind: str = DEFAULT_STAGE_KIND
    # Умолчание зависит от `needs` (см. `_parse_stage`): этап с предшественником
    # получает дерево, вход — нет. `workspace_default` помнит, что значения в
    # спеке не было: отказ и `spec check` обязаны называть умолчание умолчанием.
    workspace: str = "none"
    workspace_default: bool = False
    needs: tuple[str, ...] = ()
    parallel_with: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    retry: Optional[Retry] = None
    budget: Optional[Budget] = None
    ask: Optional[Ask] = None
    # Гейт: ответы человека, которые считаются продолжением. Пусто — допустимы
    # все `ask.options` (умолчание, а не «любой текст»).
    allow: tuple[str, ...] = ()
    verdict: Optional[Verdict] = None
    # shell-этап: команда, её обязательный код возврата и то, что она предъявляет.
    cmd: str = ""
    must: int = DEFAULT_SHELL_MUST
    artifacts: tuple[str, ...] = ()
    line: int = 0
    lines: dict[str, int] = field(default_factory=dict)

    def line_of(self, key: str) -> int:
        """Строка ключа внутри этапа; неизвестный ключ — строка самого этапа."""
        return self.lines.get(key, self.line)


@dataclass(frozen=True)
class Spec:
    """Спека дела: имя, база и этапы. `parse_issues` — то, что нашлось при разборе.

    `budget` — потолок на дело целиком (рядом с `repo`/`base`), `None` — потолка
    нет и дело ограничено только этапными бюджетами: спеки 0.0.4 читаются без
    правок и ведут себя как раньше.

    `charter` — границы роста графа (1.0). Спека без блока получает дефолты
    (`Charter()`), поэтому старые спеки читаются без правок файла, а `stages`
    остаётся seed-ом: принятый план живёт фактами журнала, а не этим полем.
    """

    path: Path
    duty: str
    repo: Optional[str]
    base: Optional[str]
    stages: tuple[Stage, ...]
    budget: Optional[Budget] = None
    charter: Charter = Charter()
    lines: dict[str, int] = field(default_factory=dict)
    parse_issues: tuple[Issue, ...] = ()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(stage.id for stage in self.stages)

    def stage(self, stage_id: str) -> Optional[Stage]:
        return next((stage for stage in self.stages if stage.id == stage_id), None)

    def line_of(self, key: str) -> int:
        return self.lines.get(key, 1)


# ------------------------------------------------------------------ загрузчик


def _line_map(text: str) -> dict[tuple[Any, ...], int]:
    """Путь узла → строка (1-based). Собирается по разобранному дереву, без объектов.

    `yaml.compose` со `SafeLoader` строит только узлы (`MappingNode`,
    `SequenceNode`, `ScalarNode`) и их метки: ни один конструктор YAML не
    вызывается, поэтому значения спеки по-прежнему берутся исключительно из
    `yaml.safe_load`.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:  # pragma: no cover - safe_load уже отказал бы раньше
        return {}
    lines: dict[tuple[Any, ...], int] = {}

    def walk(node: Any, path: tuple[Any, ...]) -> None:
        lines[path] = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                walk(value_node, path + (_canonical_key(key_node.value),))
        elif isinstance(node, yaml.SequenceNode):
            for index, item_node in enumerate(node.value):
                walk(item_node, path + (index,))

    if root is not None:
        walk(root, ())
    return lines


def _lines_of(lines: dict[tuple[Any, ...], int], prefix: tuple[Any, ...]) -> dict[str, int]:
    """Плоская карта «путь внутри этапа → строка» для сообщений `file:line`."""
    flat: dict[str, int] = {}
    for path, line in lines.items():
        if len(path) <= len(prefix) or path[: len(prefix)] != prefix:
            continue
        flat[".".join(str(part) for part in path[len(prefix):])] = line
    return flat


def _unknown_keys(
    data: dict[str, Any],
    allowed: tuple[str, ...],
    path: Path,
    lines: dict[tuple[Any, ...], int],
    prefix: tuple[Any, ...],
    issues: list[Issue],
) -> None:
    """Неизвестный ключ — отказ: иначе опечатка выглядит как «поле не задано»."""
    for key in data:
        if key in allowed:
            continue
        issues.append(
            Issue(
                path,
                lines.get(prefix + (str(key),), lines.get(prefix, 1)),
                "unknown-key",
                f"неизвестный ключ {key!r}; допустимые: {', '.join(allowed)}",
            )
        )


def _int_or_none(value: Any) -> Optional[int]:
    """Целое из YAML. `True` целым не считается: `must: true` — это не exit-код."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _str_tuple(value: Any) -> Optional[tuple[str, ...]]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _canonical_key(key: Any) -> Any:
    """Контрактное написание ключа. YAML 1.1 читает `on`/`off` как True/False.

    В спеке ключ `on` — это `retry.on`, а не булево: без этой нормализации
    `retry: {max: 2, on: [infra]}` выглядел бы как неизвестный ключ `True`.
    Набор ключей контракта закрыт, и `on` — единственное в нём имя, которое YAML
    1.1 разбирает как булево, поэтому обратная замена однозначна.
    """
    if key is True:
        return "on"
    if key is False:
        return "off"
    return key


def _mapping(value: Any) -> Optional[dict[str, Any]]:
    """Словарь с контрактными именами ключей (`_canonical_key`) либо None."""
    if not isinstance(value, dict):
        return None
    return {_canonical_key(key): item for key, item in value.items()}


def _parse_evidence(
    data: dict[str, Any],
    path: Path,
    lines: dict[tuple[Any, ...], int],
    prefix: tuple[Any, ...],
    issues: list[Issue],
) -> tuple[Evidence, ...]:
    raw = data.get("evidence", [])
    line = lines.get(prefix + ("evidence",), lines.get(prefix, 1))
    if not isinstance(raw, list):
        issues.append(Issue(path, line, "evidence", "evidence — список доказательств"))
        return ()
    items: list[Evidence] = []
    for index, entry in enumerate(raw):
        entry_line = lines.get(prefix + ("evidence", index), line)
        mapping = _mapping(entry)
        if mapping is None:
            issues.append(Issue(path, entry_line, "evidence", "доказательство — словарь {cmd, must}"))
            continue
        entry_prefix = prefix + ("evidence", index)
        _unknown_keys(mapping, EVIDENCE_KEYS, path, lines, entry_prefix, issues)
        cmd = mapping.get("cmd")
        must = _int_or_none(mapping.get("must"))
        if must is None:
            issues.append(
                Issue(
                    path,
                    lines.get(entry_prefix + ("must",), entry_line),
                    "evidence.must",
                    f"must={mapping.get('must')!r}: обязательный код возврата — целое число",
                )
            )
            must = 0
        timeout = mapping.get("timeout_sec")
        if timeout is not None and _int_or_none(timeout) is None:
            issues.append(
                Issue(
                    path,
                    lines.get(entry_prefix + ("timeout_sec",), entry_line),
                    "evidence.timeout",
                    f"timeout_sec={timeout!r}: целое число секунд",
                )
            )
            timeout = None
        items.append(
            Evidence(
                cmd=cmd if isinstance(cmd, str) else "",
                must=must,
                timeout_sec=timeout if timeout is not None else DEFAULT_TIMEOUT_SEC,
                line=entry_line,
            )
        )
    return tuple(items)


def _parse_retry(
    data: dict[str, Any],
    path: Path,
    lines: dict[tuple[Any, ...], int],
    prefix: tuple[Any, ...],
    issues: list[Issue],
) -> Optional[Retry]:
    raw = data.get("retry")
    if raw is None:
        return None
    line = lines.get(prefix + ("retry",), lines.get(prefix, 1))
    mapping = _mapping(raw)
    if mapping is None:
        issues.append(Issue(path, line, "retry", "retry — словарь {max, on}"))
        return None
    _unknown_keys(mapping, RETRY_KEYS, path, lines, prefix + ("retry",), issues)
    maximum = _int_or_none(mapping.get("max"))
    on = _str_tuple(mapping.get("on", []))
    return Retry(
        max=maximum if maximum is not None else -1,
        on=on if on is not None else (),
        line=line,
    )


def _parse_budget(
    data: dict[str, Any],
    path: Path,
    lines: dict[tuple[Any, ...], int],
    prefix: tuple[Any, ...],
    issues: list[Issue],
) -> Optional[Budget]:
    raw = data.get("budget")
    if raw is None:
        return None
    line = lines.get(prefix + ("budget",), lines.get(prefix, 1))
    mapping = _mapping(raw)
    if mapping is None:
        issues.append(Issue(path, line, "budget", "budget — словарь {tokens, minutes}"))
        return None
    _unknown_keys(mapping, BUDGET_KEYS, path, lines, prefix + ("budget",), issues)
    values: dict[str, Optional[int]] = {}
    for name in BUDGET_KEYS:
        raw_value = mapping.get(name)
        if raw_value is None:
            values[name] = None
            continue
        number = _int_or_none(raw_value)
        if number is None:
            issues.append(
                Issue(
                    path,
                    lines.get(prefix + ("budget", name), line),
                    "budget",
                    f"budget.{name}={raw_value!r}: бюджет — положительное целое число",
                )
            )
        values[name] = number
    return Budget(tokens=values["tokens"], minutes=values["minutes"], line=line)


def _parse_ask(
    data: dict[str, Any],
    path: Path,
    lines: dict[tuple[Any, ...], int],
    prefix: tuple[Any, ...],
    issues: list[Issue],
) -> Optional[Ask]:
    raw = data.get("ask")
    if raw is None:
        return None
    line = lines.get(prefix + ("ask",), lines.get(prefix, 1))
    mapping = _mapping(raw)
    if mapping is None:
        issues.append(Issue(path, line, "ask", "ask — словарь адреса вопросов"))
        return None
    _unknown_keys(mapping, ASK_KEYS, path, lines, prefix + ("ask",), issues)
    allowed = _str_tuple(mapping.get("allowed", []))
    if allowed is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("ask", "allowed"), line),
                "ask.allowed",
                "allowed — список профилей-ролей, которым адресуются фактовые вопросы",
            )
        )
        allowed = ()
    options = _str_tuple(mapping.get("options", []))
    if options is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("ask", "options"), line),
                "ask.options",
                "options — список вариантов ответа",
            )
        )
        options = ()
    to_human = mapping.get("to_human", "decisions_only")
    hops = mapping.get("max_hops", 0)
    question = mapping.get("q", "")
    return Ask(
        allowed=allowed,
        to_human=to_human if isinstance(to_human, str) else "",
        max_hops=hops if isinstance(hops, int) and not isinstance(hops, bool) else -1,
        q=question if isinstance(question, str) else "",
        options=options,
        risky=bool(mapping.get("risky", False)),
        line=line,
    )


def _parse_verdict(
    data: dict[str, Any],
    path: Path,
    lines: dict[tuple[Any, ...], int],
    prefix: tuple[Any, ...],
    issues: list[Issue],
) -> Optional[Verdict]:
    raw = data.get("verdict")
    if raw is None:
        return None
    line = lines.get(prefix + ("verdict",), lines.get(prefix, 1))
    mapping = _mapping(raw)
    if mapping is None:
        issues.append(Issue(path, line, "verdict", "verdict — словарь {approve, changes, max_rounds}"))
        return None
    _unknown_keys(mapping, VERDICT_KEYS, path, lines, prefix + ("verdict",), issues)
    approve = mapping.get("approve", "next")
    changes = mapping.get("changes", "")
    rounds = mapping.get("max_rounds", 1)
    return Verdict(
        approve=approve if isinstance(approve, str) else "",
        changes=changes if isinstance(changes, str) else "",
        max_rounds=rounds if isinstance(rounds, int) and not isinstance(rounds, bool) else 0,
        line=line,
    )


def _parse_plan_gate(
    data: dict[str, Any],
    path: Path,
    lines: dict[tuple[Any, ...], int],
    prefix: tuple[Any, ...],
    issues: list[Issue],
) -> PlanGate:
    """Порог гейта плана: `{min_stages, budget_share}`, отсутствие — гейт выключен."""
    raw = data.get("plan_gate")
    line = lines.get(prefix + ("plan_gate",), lines.get(prefix, 1))
    if raw is None:
        return PlanGate(line=line)
    mapping = _mapping(raw)
    if mapping is None:
        issues.append(Issue(path, line, "charter.plan_gate", "plan_gate — словарь {min_stages, budget_share}"))
        return PlanGate(line=line)
    _unknown_keys(mapping, PLAN_GATE_KEYS, path, lines, prefix + ("plan_gate",), issues)
    stages = _int_or_none(mapping.get("min_stages", 0))
    if stages is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("plan_gate", "min_stages"), line),
                "charter.plan_gate",
                f"min_stages={mapping.get('min_stages')!r}: порог числа этапов — целое число",
            )
        )
        stages = 0
    share = mapping.get("budget_share", 0.0)
    if isinstance(share, bool) or not isinstance(share, (int, float)):
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("plan_gate", "budget_share"), line),
                "charter.plan_gate",
                f"budget_share={share!r}: доля бюджета дела — число от 0 до 1",
            )
        )
        share = 0.0
    return PlanGate(min_stages=stages, budget_share=float(share), line=line)


def _parse_charter(
    data: dict[str, Any],
    path: Path,
    lines: dict[tuple[Any, ...], int],
    prefix: tuple[Any, ...],
    issues: list[Issue],
) -> Charter:
    """Хартия дела: отсутствие блока — дефолты, неизвестный ключ внутри — отказ.

    Форма разбора та же, что у `verdict` (`_parse_verdict`): типы собираются
    здесь, диапазоны — в `_check_charter`. Спека 0.0.8 без `charter` читается
    как `Charter()`: иначе старые дела потребовали бы правки файла.
    """
    raw = data.get("charter")
    if raw is None:
        return Charter()
    line = lines.get(prefix + ("charter",), lines.get(prefix, 1))
    mapping = _mapping(raw)
    if mapping is None:
        issues.append(
            Issue(path, line, "charter", f"charter — словарь {{{', '.join(CHARTER_KEYS)}}}")
        )
        return Charter()
    _unknown_keys(mapping, CHARTER_KEYS, path, lines, prefix + ("charter",), issues)
    max_stages = mapping.get("max_stages", DEFAULT_MAX_STAGES)
    if _int_or_none(max_stages) is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("charter", "max_stages"), line),
                "charter.max_stages",
                f"max_stages={max_stages!r}: потолок числа этапов — целое число",
            )
        )
        max_stages = DEFAULT_MAX_STAGES
    rounds = mapping.get("max_rounds_replan", DEFAULT_MAX_ROUNDS_REPLAN)
    if _int_or_none(rounds) is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("charter", "max_rounds_replan"), line),
                "charter.max_rounds_replan",
                f"max_rounds_replan={rounds!r}: число раундов реплана — целое число",
            )
        )
        rounds = DEFAULT_MAX_ROUNDS_REPLAN
    roles = _str_tuple(mapping.get("allowed_roles", []))
    if roles is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("charter", "allowed_roles"), line),
                "charter.allowed_roles",
                "allowed_roles — список профилей-ролей, которыми план вправе добавлять этапы",
            )
        )
        roles = ()
    kinds = _str_tuple(mapping.get("allowed_kinds", list(STAGE_KINDS)))
    if kinds is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("charter", "allowed_kinds"), line),
                "charter.allowed_kinds",
                f"allowed_kinds — список видов этапа ({', '.join(STAGE_KINDS)})",
            )
        )
        kinds = STAGE_KINDS
    return Charter(
        max_stages=int(max_stages),
        max_rounds_replan=int(rounds),
        allowed_roles=roles,
        allowed_kinds=kinds,
        plan_gate=_parse_plan_gate(mapping, path, lines, prefix + ("charter",), issues),
    )


def _parse_stage(
    raw: Any,
    index: int,
    path: Path,
    lines: dict[tuple[Any, ...], int],
    issues: list[Issue],
) -> Stage:
    prefix: tuple[Any, ...] = ("stages", index)
    line = lines.get(prefix, 1)
    mapping = _mapping(raw)
    if mapping is None:
        raise SpecError(path, line, "spec", f"этап №{index + 1} — словарь, а не {type(raw).__name__}")
    _unknown_keys(mapping, STAGE_KEYS, path, lines, prefix, issues)
    stage_id = mapping.get("id")
    if not isinstance(stage_id, str) or not stage_id.strip():
        raise SpecError(path, line, "stage.id", f"у этапа №{index + 1} нет id — на него ссылаются needs")
    role = mapping.get("role")
    kind = mapping.get("kind", DEFAULT_STAGE_KIND)
    if not isinstance(kind, str) or kind not in STAGE_KINDS:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("kind",), line),
                "kind",
                f"kind={kind!r}: допустимо {', '.join(STAGE_KINDS)}",
            )
        )
        kind = DEFAULT_STAGE_KIND
    cmd = mapping.get("cmd", "")
    if not isinstance(cmd, str):
        issues.append(Issue(path, lines.get(prefix + ("cmd",), line), "kind.cmd", "cmd — строка команды"))
        cmd = ""
    must = _int_or_none(mapping.get("must", DEFAULT_SHELL_MUST))
    if must is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("must",), line),
                "kind.must",
                f"must={mapping.get('must')!r}: обязательный код возврата — целое число",
            )
        )
        must = DEFAULT_SHELL_MUST
    artifacts = _str_tuple(mapping.get("artifacts", []))
    if artifacts is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("artifacts",), line),
                "kind.artifacts",
                "artifacts — список путей (строк), проверяемых после команды",
            )
        )
        artifacts = ()
    allow = _str_tuple(mapping.get("allow", []))
    if allow is None:
        issues.append(
            Issue(
                path,
                lines.get(prefix + ("allow",), line),
                "kind.allow",
                "allow — список допустимых ответов человека (строк)",
            )
        )
        allow = ()
    needs = _str_tuple(mapping.get("needs", []))
    parallel = _str_tuple(mapping.get("parallel_with", []))
    if needs is None:
        issues.append(Issue(path, lines.get(prefix + ("needs",), line), "needs", "needs — список id этапов"))
        needs = ()
    if parallel is None:
        issues.append(
            Issue(path, lines.get(prefix + ("parallel_with",), line), "parallel_with", "parallel_with — список id этапов")
        )
        parallel = ()
    workspace = mapping.get("workspace")
    workspace_default = workspace is None
    if workspace is None:
        # Этап с предшественником по смыслу продолжает его работу: цепочка
        # «сделал → проверил → принял» иначе неисполнима — артефактов
        # предшественника в каталоге попытки нет (E2E `d_221d021a`: `verify` без
        # `workspace` работал в каталоге попытки и честно не нашёл файл `impl`).
        # Поэтому умолчание — `worktree`, а «дерева нет» стало явным
        # `workspace: none`. У входа предшественника нет и отводить нечего:
        # там умолчание остаётся `none`. Гейт дерева не получает вовсе: он ничего
        # не исполняет, и отводить ему нечего даже после предшественника.
        workspace = "worktree" if needs and kind != GATE_KIND else "none"
    if not isinstance(workspace, str):
        issues.append(
            Issue(path, lines.get(prefix + ("workspace",), line), "workspace", f"workspace — одно из {', '.join(WORKSPACES)}")
        )
        workspace = ""
        workspace_default = False
    return Stage(
        id=stage_id,
        role=role if isinstance(role, str) else "",
        kind=kind,
        workspace=workspace,
        workspace_default=workspace_default,
        needs=needs,
        parallel_with=parallel,
        evidence=_parse_evidence(mapping, path, lines, prefix, issues),
        retry=_parse_retry(mapping, path, lines, prefix, issues),
        budget=_parse_budget(mapping, path, lines, prefix, issues),
        ask=_parse_ask(mapping, path, lines, prefix, issues),
        allow=allow,
        verdict=_parse_verdict(mapping, path, lines, prefix, issues),
        cmd=cmd,
        must=must,
        artifacts=artifacts,
        line=line,
        lines=_lines_of(lines, prefix),
    )


def load_spec(path: str | Path) -> Spec:
    """Прочитать `duty.yaml` в `Spec`. Ошибки разбора, не мешающие читать, — в `parse_issues`.

    Относительный путь отвергается: спека лежит в репозитории дела, а движок
    читает её из `~/.hermes`, и «откуда взялась спека» должно восстанавливаться
    из журнала однозначно.
    """
    spec_path = Path(path).expanduser()
    if not spec_path.is_absolute():
        raise SpecError(spec_path, 1, "path", "путь к спеке должен быть абсолютным: относительный не восстановить в журнале")
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(spec_path, 1, "path", f"спека не читается: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else 1
        problem = getattr(exc, "problem", None) or str(exc)
        raise SpecError(spec_path, line, "yaml", f"не разбирается как YAML: {problem}") from exc
    if not isinstance(data, dict):
        raise SpecError(spec_path, 1, "spec", "спека — YAML-словарь верхнего уровня")
    data = _mapping(data) or {}

    lines = _line_map(text)
    issues: list[Issue] = []
    _unknown_keys(data, TOP_KEYS, spec_path, lines, (), issues)

    duty = data.get("duty")
    if not isinstance(duty, str) or not duty.strip():
        raise SpecError(spec_path, lines.get(("duty",), 1), "duty", "поле duty обязательно: имя дела строкой")
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise SpecError(spec_path, lines.get(("stages",), 1), "spec", "поле stages обязательно: непустой список этапов")

    repo = data.get("repo")
    base = data.get("base")
    for name, value in (("repo", repo), ("base", base)):
        if value is not None and not isinstance(value, str):
            issues.append(
                Issue(
                    spec_path,
                    lines.get((name,), 1),
                    name,
                    f"{name} — строка, а не {type(value).__name__}",
                )
            )
    stages = tuple(
        _parse_stage(raw, index, spec_path, lines, issues) for index, raw in enumerate(raw_stages)
    )
    return Spec(
        path=spec_path,
        duty=duty,
        repo=repo if isinstance(repo, str) and repo.strip() else None,
        base=base if isinstance(base, str) and base.strip() else None,
        stages=stages,
        # Верхний `budget` — потолок на дело целиком, а не на попытку: тот же
        # разбор `{tokens, minutes}`, что у этапа, и те же отказы на мусор.
        budget=_parse_budget(data, spec_path, lines, (), issues),
        charter=_parse_charter(data, spec_path, lines, (), issues),
        lines=_top_lines(lines),
        parse_issues=tuple(issues),
    )


def _top_lines(lines: dict[tuple[Any, ...], int]) -> dict[str, int]:
    """Строки верхнего уровня и полей `budget` — для `file:line` без догадок.

    Ключи верхнего уровня нужны отказам про `repo`/`base`/`stages`, а
    `budget.tokens`/`budget.minutes` — отказам про потолок дела: без них отказ
    указывал бы на первую строку файла, и человек искал бы число не там.
    Тем же способом названы строки хартии (`charter.max_stages`, …): отказ про
    потолок этапов обязан указывать на объявленное число, а не на блок.
    """
    found = {key: lines[(key,)] for key in TOP_KEYS if (key,) in lines}
    for name in BUDGET_KEYS:
        if ("budget", name) in lines:
            found[f"budget.{name}"] = lines[("budget", name)]
    for name in CHARTER_KEYS:
        if ("charter", name) in lines:
            found[f"charter.{name}"] = lines[("charter", name)]
    for name in PLAN_GATE_KEYS:
        if ("charter", "plan_gate", name) in lines:
            found[f"charter.plan_gate.{name}"] = lines[("charter", "plan_gate", name)]
    return found


# ---------------------------------------------------------------- профили роли


_profiles_cache: Optional[frozenset[str]] = None

# Имя модуля политики в `sys.modules`: плагин грузит модули по пути, пакета нет,
# поэтому второй экземпляр `failures.py` в процессе означал бы второй словарь
# классов — и «единственное место, где они определены» перестало бы быть правдой.
FAILURES_MODULE_NAME = "hermes_duty_failures"


def _failures_module() -> Optional[Any]:
    """Модуль `dashboard/failures.py` — один экземпляр на процесс."""
    import importlib.util
    import sys

    loaded = sys.modules.get(FAILURES_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "failures.py"
    module_spec = importlib.util.spec_from_file_location(FAILURES_MODULE_NAME, path)
    if module_spec is None or module_spec.loader is None:  # pragma: no cover - файл выпал из сборки
        return None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[FAILURES_MODULE_NAME] = module
    module_spec.loader.exec_module(module)
    return module


def failure_classes() -> tuple[str, ...]:
    """Классы отказа из `dashboard/failures.py` — словарь живёт там, а не здесь.

    Спека сверяет `retry.on` с этим кортежем: опечатка в классе иначе молча
    выключила бы повтор, а спека при этом считалась бы объявленной.
    """
    module = _failures_module()
    return tuple(module.CLASSES) if module is not None else ()


def failure_class(name: str) -> str:
    """Значение константы класса (`CAPABILITY`, …) из `dashboard/failures.py`.

    Нужно правилу про `retry.on`: спека предупреждает про `capability` по имени,
    а имя берёт из того же словаря, чтобы «стена» не завелась вторым написанием.
    """
    module = _failures_module()
    return str(getattr(module, name, "")) if module is not None else ""


def known_profiles(root: Optional[Path] = None) -> frozenset[str]:
    """Профили Hermes: `$HERMES_HOME/profiles/*` плюс `default`, снимок один раз на процесс.

    `hermes profiles list` не вызывается: во-первых, это процесс на каждую
    проверку спеки, во-вторых, ответ CLI — не контракт каталога. Каталог и есть
    источник: профиль существует, если существует его папка.
    """
    global _profiles_cache
    if root is None and _profiles_cache is not None:
        return _profiles_cache
    base = Path(root) if root is not None else profiles_dir()
    found: set[str] = set(IMPLICIT_PROFILES)
    try:
        found.update(item.name for item in base.iterdir() if item.is_dir() and not item.name.startswith("."))
    except OSError:
        found = set(IMPLICIT_PROFILES)
    snapshot = frozenset(found)
    if root is None:
        _profiles_cache = snapshot
    return snapshot


# -------------------------------------------------------------------- валидатор


def _check_roles(spec: Spec, profiles: frozenset[str]) -> list[Issue]:
    issues: list[Issue] = []
    for stage in spec.stages:
        if stage.kind != DEFAULT_STAGE_KIND:
            # У этапа-команды роли нет: кто её исполняет — это `cmd`, и роль
            # здесь отказывает (`_check_kind`), а не ищется среди профилей.
            continue
        if spawn_profile(stage.role) not in profiles:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("role"),
                    "role",
                    f"профиля {stage.role!r} нет в Hermes (есть: {', '.join(sorted(profiles))})",
                )
            )
    return issues


def _check_stage_ids(spec: Spec) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[str, int] = {}
    for stage in spec.stages:
        if stage.id in seen:
            issues.append(
                Issue(
                    spec.path,
                    stage.line,
                    "stage.id",
                    f"id {stage.id!r} повторяется: первый — строка {seen[stage.id]}, второй — строка {stage.line}",
                )
            )
            continue
        seen[stage.id] = stage.line
    return issues


def _check_refs(spec: Spec) -> list[Issue]:
    """`needs`/`parallel_with` — только на существующие id, и не на себя."""
    issues: list[Issue] = []
    known = set(spec.ids)
    for stage in spec.stages:
        for field_name in ("needs", "parallel_with"):
            values = getattr(stage, field_name)
            for value in values:
                if value == stage.id:
                    issues.append(
                        Issue(spec.path, stage.line_of(field_name), field_name, f"{field_name} этапа {stage.id!r} ссылается на себя")
                    )
                    continue
                if value in known:
                    continue
                issues.append(
                    Issue(
                        spec.path,
                        stage.line_of(field_name),
                        field_name,
                        f"{field_name} этапа {stage.id!r} ссылается на несуществующий этап {value!r}",
                    )
                )
    return issues


def _references_intact(spec: Spec) -> bool:
    """Все ссылки ведут на известные id — иначе граф проверять бессмысленно."""
    known = set(spec.ids)
    return all(
        value in known and value != stage.id
        for stage in spec.stages
        for field_name in ("needs", "parallel_with")
        for value in getattr(stage, field_name)
    )


def _ancestors(spec: Spec) -> dict[str, set[str]]:
    """Предшественники этапа по `needs` (транзитивно, строго)."""
    by_id = {stage.id: stage for stage in spec.stages}
    result: dict[str, set[str]] = {}
    for stage in spec.stages:
        seen: set[str] = set()
        stack = [stage.id]
        while stack:
            current = stack.pop()
            node = by_id.get(current)
            if node is None:
                continue
            for need in node.needs:
                if need in seen:
                    continue
                seen.add(need)
                stack.append(need)
        result[stage.id] = seen
    return result


def ancestors(spec: Spec) -> dict[str, set[str]]:
    """Транзитивные предки каждого этапа по `needs` — один граф на весь плагин.

    Наружу он нужен движку: перед спавном публикующего этапа тот ищет его
    гейт-предков (`dashboard/publish.py`). Вторая реализация обхода разошлась бы
    с валидатором на первом же `needs` через два этапа, и проверка «гейт в
    предках» в спеке и в рантайме отвечала бы по-разному.
    """
    return _ancestors(spec)


def _check_cycle(spec: Spec) -> list[Issue]:
    """Граф `needs` ацикличен: иначе `next(spec, events)` не имеет ответа."""
    by_id = {stage.id: stage for stage in spec.stages}
    state: dict[str, int] = {}  # 1 — в обходе, 2 — обойдён
    path: list[str] = []

    def visit(stage_id: str) -> Optional[list[str]]:
        state[stage_id] = 1
        path.append(stage_id)
        for need in by_id[stage_id].needs:
            if need not in by_id:
                continue
            if state.get(need) == 1:
                return path[path.index(need):] + [need]
            if state.get(need) == 2:
                continue
            found = visit(need)
            if found is not None:
                return found
        path.pop()
        state[stage_id] = 2
        return None

    for stage in spec.stages:
        if state.get(stage.id):
            continue
        found = visit(stage.id)
        if found is None:
            continue
        where = by_id[found[0]].line_of("needs")
        return [Issue(spec.path, where, "cycle", f"граф needs цикличен: {' → '.join(found)}")]
    return []


def _check_entry(spec: Spec) -> list[Issue]:
    """Ровно один вход: этап без `needs`. Два входа — у дела нет начала."""
    entries = [stage for stage in spec.stages if not stage.needs]
    if len(entries) == 1:
        return []
    if not entries:
        return [
            Issue(
                spec.path,
                spec.line_of("stages"),
                "entry",
                "нет входного этапа: каждый этап чего-то ждёт, делу не с чего начаться",
            )
        ]
    return [
        Issue(
            spec.path,
            stage.line,
            "entry",
            f"входных этапов {len(entries)}: {', '.join(stage.id for stage in entries)} — вход должен быть один",
        )
        for stage in entries[1:]
    ]


def _check_reachable(spec: Spec) -> list[Issue]:
    """Все этапы достижимы от входа по `needs`: недостижимый этап не выполнится никогда."""
    entries = [stage for stage in spec.stages if not stage.needs]
    if len(entries) != 1:
        return []  # без единственного входа достижимость не определена — уже отказ `entry`
    children: dict[str, list[str]] = {stage.id: [] for stage in spec.stages}
    for stage in spec.stages:
        for need in stage.needs:
            if need in children:
                children[need].append(stage.id)
    seen = {entries[0].id}
    stack = [entries[0].id]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return [
        Issue(spec.path, stage.line, "reachable", f"этап {stage.id!r} недостижим от входа {entries[0].id!r}")
        for stage in spec.stages
        if stage.id not in seen
    ]


def _forbidden_hit(cmd: str) -> Optional[str]:
    """Публикующий или разрушающий префикс команды; `None` — команда допустима.

    Одно место на `evidence.cmd`, `kind: shell cmd` и валидатор: «действие, а не
    доказательство» — одно правило, и второе его написание разошлось бы с первым.
    """
    lowered = " ".join(str(cmd or "").lower().split())
    return next((prefix for prefix in FORBIDDEN_CMD_PREFIXES if lowered.startswith(prefix)), None)


def _check_evidence(spec: Spec) -> list[Issue]:
    """Доказательство — команда с кодом возврата, а не действие и не пустая строка."""
    issues: list[Issue] = []
    for stage in spec.stages:
        for index, evidence in enumerate(stage.evidence):
            line = stage.lines.get(f"evidence.{index}.cmd", evidence.line)
            cmd = evidence.cmd.strip()
            if not cmd:
                issues.append(Issue(spec.path, line, "evidence.cmd", f"у доказательства №{index + 1} пустой cmd"))
                continue
            hit = _forbidden_hit(cmd)
            if hit is not None:
                issues.append(
                    Issue(
                        spec.path,
                        line,
                        "evidence.forbidden",
                        f"cmd {cmd!r} начинается с {hit!r}: это действие, а не доказательство — "
                        "спека принимает только то, что можно проверить повторным запуском",
                    )
                )
            timeout_line = stage.lines.get(f"evidence.{index}.timeout_sec", evidence.line)
            if evidence.timeout_sec <= 0 or evidence.timeout_sec > MAX_TIMEOUT_SEC:
                issues.append(
                    Issue(
                        spec.path,
                        timeout_line,
                        "evidence.timeout",
                        f"timeout_sec={evidence.timeout_sec}: допустимо от 1 до {MAX_TIMEOUT_SEC} секунд",
                    )
                )
    return issues


def _check_verdicts(spec: Spec) -> list[Issue]:
    """Приговор ревью: `changes` ведёт только назад к предшественнику, `approve` — только вперёд.

    Это и есть гейт «публикация после ревью, а не после реализации»: если
    `changes` указывает не на предшественника, приговор возврата способен
    перескочить через проверку, и такой спекой дело не запускается.
    """
    issues: list[Issue] = []
    known = set(spec.ids)
    ancestors = _ancestors(spec) if _references_intact(spec) else {}
    for stage in spec.stages:
        verdict = stage.verdict
        if verdict is None:
            continue
        if verdict.approve != "next" and verdict.approve not in known:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("verdict.approve", verdict.line),
                    "verdict.approve",
                    f"approve={verdict.approve!r}: допустимо next или id существующего этапа",
                )
            )
        if verdict.max_rounds < 1:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("verdict.max_rounds", verdict.line),
                    "verdict.max_rounds",
                    f"max_rounds={verdict.max_rounds}: раундов ревью должно быть не меньше одного",
                )
            )
        if not verdict.changes:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("verdict.changes", verdict.line),
                    "verdict.changes",
                    f"у этапа {stage.id!r} есть verdict без changes: возвращать некуда",
                )
            )
            continue
        if verdict.changes not in known:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("verdict.changes", verdict.line),
                    "verdict.changes",
                    f"changes={verdict.changes!r}: такого этапа нет",
                )
            )
            continue
        if not _references_intact(spec):
            continue
        if verdict.changes not in ancestors.get(stage.id, set()):
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("verdict.changes", verdict.line),
                    "verdict.changes",
                    f"changes={verdict.changes!r} не предшественник этапа {stage.id!r}: "
                    f"возврат вперёд перескочил бы проверку (предшественники: "
                    f"{', '.join(sorted(ancestors.get(stage.id, set()))) or 'нет'})",
                )
            )
    return issues


def _check_close_stage(spec: Spec) -> list[Issue]:
    """Закрывающий этап: лид, приговор и никакой команды.

    `close` — не обычное имя, а роль этапа в деле 1.0 (`docs/ENGINE-1.0.md`,
    раздел 9): дело закрывает **лид**, и закрытие — его приговор (`approve: next`
    — «дело закрыто», `changes: <этап>` — «вернуть в работу»), а не код возврата
    команды и не тишина. Отсюда три отказа, и все — до запуска:

    - роль обязана быть `lead`: отчёт о том, чем кончилось дело, пишет тот, кто
      вёл дело, а не ревьюер и не исполнитель;
    - `kind` только `agent`: командой дело не закрывается (у `shell` нет
      приговора — его договор `must`), гейт закрывает дело лишь отказом человека;
    - `verdict` объявлен: без него `runner.report_verdict` не требует приговора, и
      этап «просто закончится» — то самое «дело закрылось, а спросить не у кого».

    Хартия `allowed_roles` здесь ни при чём: она про этапы, которые план вправе
    добавить, а `close` объявлен в seed-спеке и планом не переопределяется.
    """
    issues: list[Issue] = []
    for stage in spec.stages:
        if stage.id != CLOSE_STAGE:
            continue
        if stage.kind != DEFAULT_STAGE_KIND:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("kind"),
                    "close.kind",
                    f"у закрывающего этапа {stage.id!r} kind: {stage.kind}: дело закрывает "
                    "его приговор, а не код возврата команды и не ответ человека",
                )
            )
            continue
        if stage.role not in CLOSE_ROLES:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("role"),
                    "close.role",
                    f"у закрывающего этапа {stage.id!r} роль {stage.role!r}: дело закрывает "
                    f"координатор (role: {CLOSE_ROLE}), и хартия этого не расширяет",
                )
            )
        if stage.verdict is None:
            issues.append(
                Issue(
                    spec.path,
                    stage.line,
                    "close.verdict",
                    f"у закрывающего этапа {stage.id!r} нет verdict: approve: next значит "
                    "«дело закрыто», changes: <этап> — «вернуть в работу»",
                )
            )
    return issues


def _check_charter(spec: Spec, profiles: frozenset[str]) -> list[Issue]:
    """Хартия: потолки положительны, разрешённое существует, порог гейта достижим.

    Проверка необязательного блока того же вида, что `_check_verdicts`: спека без
    `charter` проходит её всегда (дефолты годны), а объявленное обязано быть
    исполнимым — иначе «плану разрешено» осталось бы обещанием, которого движок
    не выполнит: роль, которой нет в Hermes, вид, которого нет у этапа, или
    порог гейта выше потолка графа (вопрос человеку не встал бы никогда).
    """
    charter = spec.charter
    issues: list[Issue] = []

    def line_of(key: str) -> int:
        return spec.lines.get(key, spec.line_of("charter"))

    if charter.max_stages < 1:
        issues.append(
            Issue(
                spec.path,
                line_of("charter.max_stages"),
                "charter.max_stages",
                f"max_stages={charter.max_stages}: потолок графа — не меньше одного этапа",
            )
        )
    if charter.max_rounds_replan < 0:
        issues.append(
            Issue(
                spec.path,
                line_of("charter.max_rounds_replan"),
                "charter.max_rounds_replan",
                f"max_rounds_replan={charter.max_rounds_replan}: раундов реплана не может быть меньше нуля",
            )
        )
    for kind in charter.allowed_kinds:
        if kind in STAGE_KINDS:
            continue
        issues.append(
            Issue(
                spec.path,
                line_of("charter.allowed_kinds"),
                "charter.allowed_kinds",
                f"allowed_kinds: вида {kind!r} нет среди {', '.join(STAGE_KINDS)}",
            )
        )
    for role in charter.allowed_roles:
        if role in profiles:
            continue
        issues.append(
            Issue(
                spec.path,
                line_of("charter.allowed_roles"),
                "charter.allowed_roles",
                f"профиля {role!r} нет в Hermes (есть: {', '.join(sorted(profiles))}): "
                "такой ролью план не сможет добавить этап",
            )
        )
    gate = charter.plan_gate
    if gate.min_stages < 0:
        issues.append(
            Issue(
                spec.path,
                line_of("charter.plan_gate.min_stages"),
                "charter.plan_gate",
                f"min_stages={gate.min_stages}: порог числа этапов не может быть меньше нуля",
            )
        )
    elif gate.min_stages > charter.max_stages:
        issues.append(
            Issue(
                spec.path,
                line_of("charter.plan_gate.min_stages"),
                "charter.plan_gate",
                f"min_stages={gate.min_stages} больше max_stages={charter.max_stages}: "
                "такой план отклоняется раньше, чем дойдёт до вопроса человеку",
            )
        )
    if gate.budget_share < 0 or gate.budget_share > 1:
        issues.append(
            Issue(
                spec.path,
                line_of("charter.plan_gate.budget_share"),
                "charter.plan_gate",
                f"budget_share={gate.budget_share}: доля бюджета дела — число от 0 до 1",
            )
        )
    return issues


def _check_review_gate(spec: Spec) -> list[Issue]:
    """Публикующий или рискованный этап обязан иметь ревью в предках.

    Это второе имя того же правила, ради которого слой существует. `verdict.changes`
    бережёт возврат на доработку, но ничего не говорит о том, что между
    реализацией и публикацией стоит приговор. Поэтому этап, который действует
    вовне (`role` из `PUBLISHING_ROLES` — профиль `publisher`, деплой) или сам
    объявлен рискованным (`ask.risky: true`), проверяется отдельно: в его
    транзитивных `needs` должен быть хотя бы один этап с `role: reviewer`.
    Публикация прямо от реализации — ровно «PUB обогнал RV», и такая спека не
    запускается.
    """
    if not _references_intact(spec):
        return []  # ссылки битые — уже отказ `needs`/`parallel_with`, граф не проверить
    by_id = {stage.id: stage for stage in spec.stages}
    ancestors = _ancestors(spec)
    issues: list[Issue] = []
    for stage in spec.stages:
        risky = stage.ask.risky if stage.ask is not None else False
        if stage.role not in PUBLISHING_ROLES and not risky:
            continue
        if any(by_id[need].role == REVIEWING_ROLE for need in ancestors.get(stage.id, set())):
            continue
        issues.append(
            Issue(
                spec.path,
                stage.line,
                "review-gate",
                f"этап {stage.id!r} публикует, но в его предках нет ревью (role: {REVIEWING_ROLE})",
            )
        )
    return issues


def _check_publish_gate(spec: Spec) -> list[Issue]:
    """Публикация требует вопроса человеку в предках — и утвердительного `allow`.

    `review-gate` требует приговора перед публикацией, но приговор — это роль, а
    не человек: ревьюер одобряет дифф, а «публиковать этот коммит» решает
    человек. Поэтому у публикующего этапа (`role` из `PUBLISHING_ROLES`) в
    транзитивных `needs` обязан быть `kind: gate`, и у этого гейта обязан быть
    объявленный `allow`.

    `allow` обязателен именно объявленным: без него продолжением считается любой
    вариант из `ask.options` (`runner.gate_allow`), то есть «не публиковать»
    закрыло бы гейт успехом, и публикация поехала бы на отказе человека. Спека,
    в которой это возможно, — небезопасная, и отказ случается до первого
    процесса, а не в момент пуша.
    """
    if not _references_intact(spec):
        return []  # ссылки битые — уже отказ `needs`/`parallel_with`, граф не проверить
    by_id = {stage.id: stage for stage in spec.stages}
    ancestors = _ancestors(spec)
    issues: list[Issue] = []
    for stage in spec.stages:
        if stage.role not in PUBLISHING_ROLES:
            continue
        gates = [
            by_id[need]
            for need in sorted(ancestors.get(stage.id, set()))
            if by_id[need].kind == GATE_KIND
        ]
        if not gates:
            issues.append(
                Issue(
                    spec.path,
                    stage.line,
                    "publish-gate",
                    f"публикация требует вопроса человеку (kind: {GATE_KIND}) в предках: "
                    f"у публикующего этапа {stage.id!r} его нет",
                )
            )
            continue
        if any(gate.allow for gate in gates):
            continue
        gate = gates[0]
        issues.append(
            Issue(
                spec.path,
                gate.line_of("allow") if "allow" in gate.lines else gate.line,
                "publish-gate",
                f"гейт {gate.id!r} перед публикующим этапом {stage.id!r} не объявляет allow: "
                "без него продолжением считается любой вариант, включая «не публиковать»",
            )
        )
    return issues


def _check_workspace(spec: Spec) -> list[Issue]:
    """`worktree` без `repo`/`base` — не дерево, а догадка: от чего отводить неизвестно."""
    issues: list[Issue] = []
    if spec.repo is not None:
        repo = Path(spec.repo).expanduser()
        if not repo.is_absolute():
            issues.append(
                Issue(
                    spec.path,
                    spec.line_of("repo"),
                    "repo",
                    f"repo={spec.repo!r}: нужен абсолютный путь (или начинающийся с ~)",
                )
            )
    for stage in spec.stages:
        if stage.workspace not in WORKSPACES:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("workspace"),
                    "workspace",
                    f"workspace={stage.workspace!r}: допустимо {', '.join(WORKSPACES)}",
                )
            )
            continue
        if stage.workspace != "worktree":
            continue
        # Откуда взялось `worktree`: этап его попросил или получил по умолчанию
        # (у этапа с `needs`). Второй случай обязан называться умолчанием —
        # иначе человек ищет в своей спеке строку, которой он не писал.
        asked = (
            f"этап {stage.id!r} получает worktree по умолчанию (у этапа есть needs), а"
            if stage.workspace_default
            else f"этап {stage.id!r} просит worktree, а"
        )
        hint = " (дерево не нужно — укажи workspace: none)" if stage.workspace_default else ""
        if spec.repo is None:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("workspace"),
                    "workspace",
                    f"{asked} repo не задан: от чего отводить дерево{hint}",
                )
            )
        if spec.base is None:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("workspace"),
                    "workspace",
                    f"{asked} base не задан: от чего отводить дерево{hint}",
                )
            )
    return issues


def _check_artifacts_without_tree(spec: Spec) -> list[Issue]:
    """Этап в каталоге попытки после предшественника — предупреждение, а не отказ.

    `workspace: none` у этапа с `needs` — законный выбор (этап только спрашивает
    человека или выносит приговор по чужим словам), но с доказательствами он
    противоречив: команды прогоняются в каталоге попытки, где артефактов
    предшественника нет, и объявленное «сделано» проверить не на чем. Ровно на
    этом сломался E2E `d_221d021a`. Отказом это не делается: спека — контракт, а
    осознанный `none` остаётся её правом.
    """
    issues: list[Issue] = []
    for stage in spec.stages:
        if not stage.needs or stage.workspace != "none":
            continue
        # Признак — `evidence` с командами: ожидаемые артефакты спека не
        # описывает (их называет отчёт попытки), а команда уже говорит, что
        # этап что-то прогоняет и что-то предъявляет.
        if not any(evidence.cmd.strip() for evidence in stage.evidence):
            continue
        issues.append(
            Issue(
                spec.path,
                stage.line_of("workspace"),
                "artifacts-without-tree",
                f"этап {stage.id!r} не получает дерево предшественника (workspace: none): "
                "артефакты предшественника в нём не появятся — доказательства считаются "
                "в каталоге попытки; если этап работает по работе предшественника, укажи "
                "workspace: worktree",
                WARNING,
            )
        )
    return issues


def _check_ask(spec: Spec) -> list[Issue]:
    """Адрес вопросов: `to_human` из двух значений, `max_hops` не отрицательный."""
    issues: list[Issue] = []
    for stage in spec.stages:
        ask = stage.ask
        if ask is None:
            continue
        line = stage.lines.get("ask", ask.line)
        if ask.to_human not in TO_HUMAN:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("ask.to_human", line),
                    "ask.to_human",
                    f"to_human={ask.to_human!r}: допустимо {', '.join(TO_HUMAN)} (властные — в пул, остальное объявляется в спеке)",
                )
            )
        if ask.max_hops < 0:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("ask.max_hops", line),
                    "ask.max_hops",
                    "max_hops должен быть целым не меньше нуля",
                )
            )
    return issues


def _check_budget(spec: Spec) -> list[Issue]:
    """Бюджеты положительные — и у этапа, и у дела; дело не меньше суммы этапов."""
    issues: list[Issue] = []
    if spec.budget is not None:
        for name in BUDGET_KEYS:
            value = getattr(spec.budget, name)
            if value is None or value > 0:
                continue
            issues.append(
                Issue(
                    spec.path,
                    spec.line_of(f"budget.{name}"),
                    "budget",
                    f"budget.{name}={value}: бюджет дела — положительное число",
                )
            )
    for stage in spec.stages:
        budget = stage.budget
        if budget is None:
            continue
        line = stage.lines.get("budget", budget.line)
        for name in BUDGET_KEYS:
            value = getattr(budget, name)
            if value is None:
                continue
            if value <= 0:
                issues.append(
                    Issue(
                        spec.path,
                        stage.lines.get(f"budget.{name}", line),
                        "budget",
                        f"budget.{name}={value}: бюджет — положительное число",
                    )
                )
    issues += _check_deal_budget(spec)
    return issues


def _check_deal_budget(spec: Spec) -> list[Issue]:
    """Потолок дела не меньше суммы этапных: иначе спека обещает невозможное.

    Сумма берётся по объявленным числам: этап без `budget.tokens` в неё не
    входит (он не ограничен сам по себе). Считается **только** объявленное —
    никаких умолчаний на дело: спека без верхнего `budget` проходит эту проверку
    всегда, и это её обратная совместимость.

    Гейт не считается: его `budget.minutes` — срок ответа человека, а не расход
    дела (`docs/SPEC.md`), и складывать ожидание с работой значило бы требовать
    бюджета на чужое время.
    """
    cap = spec.budget
    if cap is None:
        return []
    issues: list[Issue] = []
    for name in BUDGET_KEYS:
        declared = getattr(cap, name)
        if declared is None or declared <= 0:
            continue  # непозитивный потолок уже назван отказом выше
        asked = sum(
            int(getattr(stage.budget, name) or 0)
            for stage in spec.stages
            if stage.budget is not None
            and stage.kind != GATE_KIND
            and int(getattr(stage.budget, name) or 0) > 0
        )
        if asked <= declared:
            continue
        issues.append(
            Issue(
                spec.path,
                spec.line_of(f"budget.{name}"),
                "budget",
                f"этапы просят больше, чем есть у дела: сумма budget.{name} этапов "
                f"{asked} больше бюджета дела {declared}",
            )
        )
    return issues


def _check_retry(spec: Spec) -> list[Issue]:
    issues: list[Issue] = []
    classes = failure_classes()
    for stage in spec.stages:
        retry = stage.retry
        if retry is None:
            continue
        line = stage.lines.get("retry", retry.line)
        if retry.max < 0:
            issues.append(
                Issue(spec.path, stage.lines.get("retry.max", line), "retry.max", "retry.max должен быть целым не меньше нуля")
            )
        if not retry.on:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("retry.on", line),
                    "retry.on",
                    "retry.on пуст: ретрай без классов отказов не сработает",
                )
            )
        # `capability` — стена (бюджет, доступ, роль), а не сбой: повтор её обычно
        # не рассасывает. Это предупреждение, а не отказ: выбор автора спеки
        # осознанный, а решение всё равно за политикой движка.
        capability = failure_class("CAPABILITY")
        if capability and capability in retry.on:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("retry.on", line),
                    "retry.on",
                    f"{capability} обычно не рассасывается повтором; если это осознанно — оставь как есть",
                    WARNING,
                )
            )
        # Класс сравнивается со словарём `dashboard/failures.py`: опечатка в
        # `retry.on` иначе молча выключила бы повтор, и спека выглядела бы
        # объявленной, а политика — без классов.
        for kind in retry.on:
            if kind in classes:
                continue
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("retry.on", line),
                    "retry.on",
                    f"retry.on: класса {kind!r} нет среди {', '.join(classes)}",
                )
            )
    return issues


def _check_kind(spec: Spec) -> list[Issue]:
    """Вид этапа: `agent` исполняет профиль, `shell` — команда, `gate` — человек.

    Отказ, а не тихий игнор. `role` у команды означал бы процесс профиля, которого
    никто не поднимает; `ask` — что этап задаст вопрос человеку, а он не умеет
    (вопрос — это отчёт агента); `verdict` — приговор, которого некому вынести;
    `retry` обещал бы повтор класса отказа, а у команды повтор выражается кодом
    возврата `must`, а не классом. Поля команды у `agent` отказывают по той же
    причине: объявленное и не прочитанное — это ложь спеки, а не запас на будущее.
    `allow` — поле гейта: ограничение ответа имеет смысл только у этапа, который
    сам ничего не делает, и у остальных видов оно отказывает.
    """
    issues: list[Issue] = []
    for stage in spec.stages:
        if stage.kind == GATE_KIND:
            issues += _check_gate_kind(spec, stage)
            continue
        if stage.allow:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("allow"),
                    "kind.allow",
                    f"этап {stage.id!r} с kind: {stage.kind} не принимает allow: допустимые ответы "
                    "ограничивают только этап-гейт, который сам ничего не исполняет",
                )
            )
        if stage.kind == SHELL_KIND:
            issues += _check_shell_kind(spec, stage)
            continue
        for key in ("cmd", "must", "artifacts"):
            if key not in stage.lines:
                continue
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of(key),
                    f"kind.{key}",
                    f"{key} у этапа {stage.id!r} с kind: {DEFAULT_STAGE_KIND}: "
                    f"команду исполняет этап kind: shell, а профиль роли её не читает",
                )
            )
    return issues


def _check_shell_kind(spec: Spec, stage: Stage) -> list[Issue]:
    """Правила одного `kind: shell`: нужна команда, не нужны роль, вопрос, приговор, ретрай."""
    issues: list[Issue] = []
    if not stage.cmd.strip():
        issues.append(
            Issue(
                spec.path,
                stage.line_of("kind"),
                "kind.cmd",
                f"этап {stage.id!r} с kind: shell без cmd: команде этапа нечего запускать",
            )
        )
    if stage.role:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("role"),
                "kind.role",
                f"этап {stage.id!r} с kind: shell не принимает role: этап исполняет команда, а не профиль",
            )
        )
    if stage.ask is not None:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("ask"),
                "kind.ask",
                f"этап {stage.id!r} с kind: shell не задаёт вопросов человеку: ask — поле агента, "
                "команда вопросов не задаёт",
            )
        )
    if stage.verdict is not None:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("verdict"),
                "kind.verdict",
                f"этап {stage.id!r} с kind: shell не выносит приговора: verdict — поле агента-ревьюера",
            )
        )
    if stage.retry is not None:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("retry"),
                "kind.retry",
                f"этап {stage.id!r} с kind: shell не принимает retry: политика повтора у команды — "
                "это must, а не класс отказа",
            )
        )
    hit = _forbidden_hit(stage.cmd)
    if hit is not None:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("cmd"),
                "cmd.forbidden",
                f"cmd {stage.cmd!r} начинается с {hit!r}: публикация — дело роли {PUBLISHING_ROLES[0]}, "
                "а не этапа-команды; иначе у публикации появится второй путь мимо неё",
            )
        )
    if stage.evidence:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("evidence"),
                "kind.evidence",
                f"у этапа {stage.id!r} с kind: shell evidence не проверяется: договор команды — "
                "код возврата must и список artifacts",
                WARNING,
            )
        )
    return issues


def _check_gate_kind(spec: Spec, stage: Stage) -> list[Issue]:
    """Правила одного `kind: gate`: вопрос обязателен, ответ ограничен, чужих полей нет.

    Гейт — вид этапа, а не парковка: он сам ничего не делает и ничего не
    исполняет, поэтому `role`, `workspace`, `verdict`, `retry`, `cmd` (и поля
    команды) у него отказывают — объявленное и не прочитанное было бы ложью
    спеки. Обязательное — вопрос (`ask.q`) и варианты ответа (`ask.options`):
    гейт без вопроса нечего ждать, а без вариантов человеку не из чего выбрать.
    `allow` сужает варианты до продолжения: ответ вне списка — отказ этапа, а не
    «продолжаем», и потому `allow` обязан называть существующие варианты.
    """
    issues: list[Issue] = []
    ask_decl = stage.ask
    if ask_decl is None:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("ask"),
                "kind.ask",
                f"этап {stage.id!r} с kind: gate без ask: гейту нечего спрашивать — "
                f"задай ask.q (текст вопроса) и ask.options (варианты ответа)",
            )
        )
    else:
        if not ask_decl.q.strip():
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("ask.q", ask_decl.line),
                    "ask.q",
                    f"этап {stage.id!r} с kind: gate без ask.q: вопрос человеку — работа гейта, "
                    "и текст вопроса берётся из спеки, а не из отчёта процесса",
                )
            )
        if not ask_decl.options:
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("ask.options", ask_decl.line),
                    "ask.options",
                    f"этап {stage.id!r} с kind: gate без ask.options: человеку не из чего выбирать",
                )
            )
        for option in ask_decl.options:
            if option.strip():
                continue
            issues.append(
                Issue(
                    spec.path,
                    stage.lines.get("ask.options", ask_decl.line),
                    "ask.options",
                    f"этап {stage.id!r}: вариант ответа пустой — пустой ответ неотличим от «не ответили»",
                )
            )
        for answer in stage.allow:
            if answer in ask_decl.options:
                continue
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("allow"),
                    "allow",
                    f"этап {stage.id!r}: allow называет ответ {answer!r}, которого нет в ask.options "
                    f"({', '.join(ask_decl.options) or '—'}): такой ответ не выбрать, и allow не сработает",
                )
            )
        # Адрес гейта — человек, а не роль: `ask.allowed`/`max_hops`/`to_human`
        # описывают передачу вопроса роли, а гейт никому его не передаёт.
        for key in ("allowed", "to_human", "max_hops", "risky"):
            if f"ask.{key}" not in stage.lines:
                continue
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of(f"ask.{key}"),
                    "kind.ask",
                    f"этап {stage.id!r} с kind: gate не принимает ask.{key}: гейт спрашивает "
                    f"человека напрямую — маршрут вопроса и его риск здесь не объявляются",
                )
            )
    if stage.role:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("role"),
                "kind.role",
                f"этап {stage.id!r} с kind: gate не принимает role: гейт ничего не исполняет, "
                "процесс профиля поднимать нечему",
            )
        )
    for key in ("workspace", "verdict", "retry", "cmd", "must", "artifacts"):
        if key not in stage.lines:
            continue
        issues.append(
            Issue(
                spec.path,
                stage.line_of(key),
                f"kind.{key}",
                f"этап {stage.id!r} с kind: gate не принимает {key}: гейт не работает в дереве, "
                "не запускает команду и не выносит приговора — он спрашивает человека",
            )
        )
    if stage.evidence:
        issues.append(
            Issue(
                spec.path,
                stage.line_of("evidence"),
                "kind.evidence",
                f"у этапа {stage.id!r} с kind: gate evidence не проверяется: гейт ничего не "
                "исполняет, его договор — вопрос и ответ",
                WARNING,
            )
        )
    return issues


def _check_coordinator_scope(spec: Spec) -> list[Issue]:
    """Координатор ведёт дело и закрывает — работу делает роль-исполнитель.

    Допустимы ровно две позиции координаторской роли: входной этап (без `needs`)
    и `close`. Всё остальное — отказ `coordinator.scope`: этап-исполнитель с
    ролью координатора означает, что исследование, правку или проверку координатор
    делает сам, и детали работы переезжают в его контекст вместо контракта роли.

    Правило закрывает и seed, и план (`parse_plan` отказывает тем же кодом): план
    не добавляет координатора-исполнителя, потому что `needs` у добавленного этапа
    есть всегда, а `close` в графе уже занят.
    """
    issues: list[Issue] = []
    for stage in spec.stages:
        if stage.role not in COORDINATOR_ROLES:
            continue
        if not stage.needs or stage.id == CLOSE_STAGE:
            continue
        issues.append(
            Issue(
                spec.path,
                stage.line_of("role"),
                "coordinator.scope",
                f"этап {stage.id!r} с ролью {stage.role!r}: координатор ведёт дело и "
                f"закрывает, а работу делает роль-исполнитель — координаторская роль "
                f"допустима только на входе (без needs) и на {CLOSE_STAGE!r}",
            )
        )
    return issues


def validate_spec(spec: Spec, *, profiles: Optional[frozenset[str]] = None) -> list[Issue]:
    """Все отказы спеки до запуска: структура, граф, доказательства, адрес вопросов.

    `profiles` подставляется явно (тест не зависит от машины); по умолчанию —
    снимок `~/.hermes/profiles/*` + `default`. Проверка `base` как rev живёт
    отдельно — `check_base()`: ей нужен git, а не только спека.
    """
    issues: list[Issue] = list(spec.parse_issues)
    known = profiles if profiles is not None else known_profiles()
    issues += _check_stage_ids(spec)
    issues += _check_roles(spec, known)
    issues += _check_refs(spec)
    issues += _check_charter(spec, known)
    issues += _check_cycle(spec)
    issues += _check_entry(spec)
    issues += _check_reachable(spec)
    issues += _check_evidence(spec)
    issues += _check_verdicts(spec)
    issues += _check_close_stage(spec)
    issues += _check_coordinator_scope(spec)
    issues += _check_review_gate(spec)
    issues += _check_publish_gate(spec)
    issues += _check_workspace(spec)
    issues += _check_artifacts_without_tree(spec)
    issues += _check_ask(spec)
    issues += _check_budget(spec)
    issues += _check_retry(spec)
    issues += _check_kind(spec)
    return issues


# ------------------------------------------------------------------- план дела

# Адрес отказа в предложении плана. План приходит отчётом попытки
# (`result.json:plan.stages`), а не файлом спеки: сверять его с строками
# `duty.yaml` не с чем. Поэтому отказ называет этап по `id`, а путь — этим
# псевдонимом: `<plan>:1` читается как «в предложении», а не как строка чужого
# файла. Вызывающий может назвать настоящий путь (файл отчёта), передав `path`.
PLAN_PATH = "<plan>"


# Адрес фактовых вопросов по умолчанию у этапа, который добавляет план (решение
# Макса, 12.11). Пустой `allowed` — это не «спросить некого», а
# `human_routing_failed`: фактовый вопрос уходит человеку, и пул заполняется тем,
# что должен был ответить профиль. Так прошли все 58 вопросов журнала — ни одна
# спека и ни один план блока `ask` не объявляли (0 из 77 добавленных планом
# этапов). План объявляет адрес сам: фактовый вопрос — исследователю, властный —
# человеку (`routing.py`, правило 1: `risky` сильнее любого `allowed`).
DEFAULT_PLAN_ASK_ALLOWED = ("researcher",)
DEFAULT_PLAN_ASK_HOPS = 2


def _with_default_ask(stage: Stage) -> Stage:
    """Этап плана без `ask` получает объявленный адрес вопросов.

    Своё имя из `allowed` выбрасывается: роль не отвечает на собственный вопрос
    (`routing._pick`), и объявлять адресом себя значило бы обещать адрес, которого
    нет, — такой этап остаётся без дефолта, а его вопрос честно идёт человеку с
    причиной `allowed_empty`.
    """
    if stage.kind != DEFAULT_STAGE_KIND or stage.ask is not None:
        return stage
    allowed = tuple(name for name in DEFAULT_PLAN_ASK_ALLOWED if name != stage.role)
    if not allowed:
        return stage
    return replace(
        stage,
        ask=Ask(
            allowed=allowed,
            to_human="decisions_only",
            max_hops=DEFAULT_PLAN_ASK_HOPS,
            line=stage.line,
        ),
    )


def _with_repo_workspace(stage: Stage, repo: Optional[str]) -> Stage:
    """Этап плана, который делает работу в репозитории, получает дерево.

    Умолчание `_parse_stage` зависит от `needs`: у входа предшественника нет, и
    этап остаётся с `workspace: none` — верно для гейта и для роли, которая
    только читает. Для производящего этапа в деле с `repo` это ошибка: процесса
    нет в каталоге дела, cwd — каталог попытки, и роль либо работает не там, где
    проверяют (d_e575494f: импл закоммитил в живой чекаут, а `commit.resolve`
    искал коммит в каталоге попытки), либо правит копию, которую никто не увидит.
    Поэтому умолчание уточняется до `dir`: каталог дела из `spec.repo`, без
    второго дерева.

    Явно названный `workspace` не трогается никогда — это решение лида.
    """
    if not repo:
        return stage
    if not stage.workspace_default:
        return stage
    if stage.kind != DEFAULT_STAGE_KIND or stage.needs:
        return stage
    if stage.role in EVIDENCE_FREE_ROLES or stage.role in PUBLISHING_ROLES:
        return stage
    return replace(stage, workspace="dir")


def parse_plan_cost(raw: Any, *, path: Optional[Path] = None) -> tuple[Optional[dict[str, int]], list[Issue]]:
    """Цена предложения плана: `{tokens: N}` или `None`, если цену не назвали.

    План называет свою стоимость сам (`docs/ENGINE-1.0.md`, раздел 5): порог
    `charter.plan_gate.budget_share` — про то, сколько план забирает у дела, а не
    про сумму этапных бюджетов, которую лид может нарисовать любой. Разбор живёт
    здесь, рядом с `parse_plan`, потому что план проверяется одним валидатором, а
    не вторым в `runner`.

    Молчание (`None`) — не ноль: цена не названа, и порог считается по этапным
    бюджетам (фолбэк). `0` и отрицательное — та же неопределённость, то есть
    отказ: «ноль токенов» — утверждение о бесплатной работе, а не о молчании.
    """
    where = Path(path) if path is not None else Path(PLAN_PATH)
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        return None, [
            Issue(where, 1, "plan.cost", f"cost={raw!r}: цена плана — словарь {{{', '.join(PLAN_COST_KEYS)}}}")
        ]
    issues: list[Issue] = []
    extra = sorted(str(key) for key in raw if key not in PLAN_COST_KEYS)
    if extra:
        issues.append(
            Issue(
                where,
                1,
                "plan.cost",
                f"cost называет {', '.join(extra)}: цена плана — только {'/'.join(PLAN_COST_KEYS)}",
            )
        )
    tokens = _int_or_none(raw.get("tokens"))
    if tokens is None or tokens <= 0:
        issues.append(
            Issue(
                where,
                1,
                "plan.cost",
                f"cost.tokens={raw.get('tokens')!r}: цена плана — целое число токенов больше нуля "
                "(не названа цена — не объявляй cost вовсе)",
            )
        )
        return None, issues
    return {"tokens": int(tokens)}, issues


def parse_plan(
    raw_stages: Any,
    *,
    charter: Charter,
    base_ids: Iterable[str],
    base_needs: Optional[Mapping[str, Iterable[str]]] = None,
    started_ids: Iterable[str] = (),
    repo: Optional[str] = None,
    path: Optional[Path] = None,
) -> tuple[tuple[Stage, ...], list[Issue]]:
    """Предложение плана → этапы и отказы; непустые отказы — план не принимается.

    Разбор этапа тот же, что у спеки (`_parse_stage`, та же `STAGE_KEYS`), потому
    что второй разбор разошёлся бы с первым на первом же новом поле. Поверх —
    правила хартии (`docs/ENGINE-1.0.md`, раздел 5), которых у спеки нет:

    - роль и вид этапа — из `charter.allowed_roles`/`allowed_kinds` (пустой
      `allowed_roles` роль не ограничивает: хартия молчит — действует проверка
      профиля у самой спеки);
    - публикацию план не добавляет: этап с ролью из `PUBLISHING_ROLES`
      объявляется в seed-спеке, а `git push` в команде отказывает тем же
      правилом, что у спеки (`_forbidden_hit`);
    - этап, который что-то производит (его свидетель — работа: не `gate`, без
      `verdict`, не публикующий), обязан объявить `evidence` с командой: план не
      вправе добавить этап, который закроется без доказательств;
    - `needs` ведёт только на известный этап, граф плана ацикличен — и цикл,
      замкнутый перестановкой через уже существующие этапы, тоже отказ;
    - `len(seed ∪ план) <= charter.max_stages`.

    **Перестановка.** Запись с `id`, который уже есть в графе, — не новый этап, а
    перестановка: у неё читаются только `needs`/`parallel_with` (`REORDER_KEYS`), а
    любое другое поле — `plan.id` («план не переписывает этап»). Переставлять можно
    только этап, который **не начинали**: `started_ids` — id с фактом попытки, и
    такая запись получает отказ `plan.reorder` («этап уже начат»). Ход, ради
    которого эта проверка существует, — вернуть в работу то, что уже сделано, в
    обход `verdict.changes`.

    `base_ids` — id уже существующего графа (seed плюс принятые ревизии): на них
    плану ссылаться можно, а `base_needs` (их `needs`) нужен, чтобы поймать цикл,
    который перестановка замыкает через существующие этапы. `started_ids` читает
    вызывающий, у которого есть журнал (`runner.report_plan`): сама проверка
    структуры графа от попыток не зависит, и принятый план, чей этап потом начали,
    остаётся графом.
    """
    where = Path(path) if path is not None else Path(PLAN_PATH)
    issues: list[Issue] = []
    if not isinstance(raw_stages, list) or not raw_stages:
        issues.append(Issue(where, 1, "plan.stages", "план — непустой список этапов {id, role, …}"))
        return (), issues
    # Строк в предложении нет: `lines` пуст, и `_parse_stage` берёт номер 1 —
    # отказ называет этап по `id`, а не по строке отчёта.
    lines: dict[tuple[Any, ...], int] = {}
    base = {str(stage_id) for stage_id in base_ids}
    started = {str(stage_id) for stage_id in started_ids}
    stages: list[Stage] = []
    for index, raw in enumerate(raw_stages):
        # Какие поля назвала запись — видно только у сырого словаря, а разбирать
        # перестановку иначе нечем: `_parse_stage` заполняет умолчания.
        declared = _mapping(raw) or {}
        moving = isinstance(declared.get("id"), str) and declared.get("id") in base
        try:
            stage = _parse_stage(raw, index, where, lines, issues)
        except SpecError as exc:
            # Этап без `id` или не словарь — не повод бросать исключение: отказ
            # уезжает в `plan_rejected.issues[]`, и лид видит, что не так.
            issues.append(exc.issue())
            continue
        extra = sorted(set(declared) - set(REORDER_KEYS))
        if moving and extra:
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "plan.id",
                    f"id {stage.id!r} уже есть в графе: перестановка называет только "
                    f"{'/'.join(REORDER_KEYS[1:])}, а поля {', '.join(extra)} переписывают этап",
                )
            )
            continue
        if moving and stage.id in started:
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "plan.reorder",
                    f"этап {stage.id!r} уже начат: переставлять можно только этап, у которого "
                    "нет попытки — сделанное возвращает приговор ревью, а не план",
                )
            )
            continue
        stages.append(stage if moving else _with_repo_workspace(_with_default_ask(stage), repo))
    known = base | {stage.id for stage in stages}
    # Ссылки проверяются у всех записей, включая перестановки: `needs` и
    # `parallel_with` — ровно то, чем перестановка и распоряжается.
    for stage in stages:
        for field_name in ("needs", "parallel_with"):
            for value in getattr(stage, field_name):
                if value == stage.id:
                    issues.append(
                        Issue(
                            where,
                            stage.line,
                            f"plan.{field_name}",
                            f"{field_name} этапа {stage.id!r} ссылается на себя",
                        )
                    )
                    continue
                if value in known:
                    continue
                issues.append(
                    Issue(
                        where,
                        stage.line,
                        f"plan.{field_name}",
                        f"{field_name} этапа {stage.id!r} ссылается на несуществующий этап {value!r}",
                    )
                )
    seen: set[str] = set()
    for stage in stages:
        moving = stage.id in base
        if stage.id in seen:
            issues.append(Issue(where, stage.line, "plan.id", f"id {stage.id!r} повторяется в предложении"))
            continue
        seen.add(stage.id)
        if moving:
            continue  # перестановка: роль, вид и evidence — не её дело
        if stage.kind not in charter.allowed_kinds:
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "charter.allowed_kinds",
                    f"этап {stage.id!r}: вид {stage.kind!r} планом не добавляется — "
                    f"хартия разрешает {', '.join(charter.allowed_kinds) or 'ничего'}",
                )
            )
        if stage.kind == DEFAULT_STAGE_KIND and charter.allowed_roles and stage.role not in charter.allowed_roles:
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "charter.allowed_roles",
                    f"этап {stage.id!r}: роль {stage.role!r} планом не добавляется — "
                    f"хартия разрешает {', '.join(charter.allowed_roles)}",
                )
            )
        if stage.role in PUBLISHING_ROLES:
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "plan.publish",
                    f"этап {stage.id!r} публикует (role: {stage.role}): публикация объявляется "
                    "в seed-спеке и требует ревью в предках — планом она не добавляется",
                )
            )
        if stage.role in COORDINATOR_ROLES:
            # У добавленного планом этапа `needs` есть всегда, а `close` в графе
            # уже занят (`plan.id`), — значит координаторская роль здесь означает
            # этап-исполнитель, который координатор делает сам (кейс d_48e01a0e:
            # `check` ролью координатора вместо этапа `researcher`).
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "coordinator.scope",
                    f"этап {stage.id!r}: план не добавляет координатора-исполнителя — "
                    f"работа адресуется роли (researcher, implementer, reviewer…), а "
                    f"координатор ведёт граф и закрывает дело",
                )
            )
            continue
        if stage.kind == SHELL_KIND and not stage.cmd.strip():
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "kind.cmd",
                    f"этап {stage.id!r} с kind: shell без cmd: команде этапа нечего запускать",
                )
            )
        hit = _forbidden_hit(stage.cmd) if stage.kind == SHELL_KIND else None
        if hit is not None:
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "cmd.forbidden",
                    f"cmd этапа {stage.id!r} начинается с {hit!r}: публикация — дело роли "
                    f"{PUBLISHING_ROLES[0]}, а не этапа плана",
                )
            )
        for evidence in stage.evidence:
            if not evidence.cmd.strip():
                issues.append(
                    Issue(where, stage.line, "evidence.cmd", f"у этапа {stage.id!r} доказательство с пустым cmd")
                )
                continue
            hit = _forbidden_hit(evidence.cmd)
            if hit is None:
                continue
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "evidence.forbidden",
                    f"evidence этапа {stage.id!r} начинается с {hit!r}: это действие, а не доказательство",
                )
            )
        # «Что-то производит» — тот же признак, по которому `evidence.witness`
        # выбирает свидетеля: этап-агент без приговора и не публикующий. Приговор —
        # не работа в дереве, у гейта работы нет вовсе, а команда доказывает себя
        # кодом возврата `must`. Роли без терминала (ресерчер, ревьюер) не
        # производят: их работа — отчёт, и требовать с них команду нельзя.
        if stage.evidence and stage.role in NO_SHELL_ROLES:
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "plan.evidence.role",
                    f"этап {stage.id!r}: у роли {stage.role!r} нет терминала — объявленные "
                    f"evidence ({', '.join(item.cmd for item in stage.evidence)}) нечем выполнить; "
                    "проверяющие команды ставь этапу, который их действительно запускает",
                )
            )
        produces = (
            stage.kind == DEFAULT_STAGE_KIND
            and stage.verdict is None
            and stage.role not in PUBLISHING_ROLES
            and stage.role not in EVIDENCE_FREE_ROLES
        )
        if produces and not any(evidence.cmd.strip() for evidence in stage.evidence):
            issues.append(
                Issue(
                    where,
                    stage.line,
                    "plan.evidence",
                    f"этап {stage.id!r} что-то производит, а evidence пуст: планом добавляется "
                    "только этап, чью работу можно проверить — объяви evidence {cmd, must}",
                )
            )
    # Граф — seed плюс предложение: перестановка меняет `needs` уже существующего
    # этапа, поэтому цикл проверяется по объединённым рёбрам, а не только по
    # добавленным. Иначе `plan.needs: [close]` поверх seed `close.needs: [plan]`
    # принимался бы и вешал дело в ожидании, которого никто не снимет.
    merged: dict[str, tuple[str, ...]] = {
        str(stage_id): tuple(str(need) for need in needs) for stage_id, needs in (base_needs or {}).items()
    }
    merged.update({stage.id: stage.needs for stage in stages})
    trail: list[str] = []
    state: dict[str, int] = {}  # 1 — в обходе, 2 — обойдён

    def cycle(stage_id: str) -> Optional[list[str]]:
        state[stage_id] = 1
        trail.append(stage_id)
        for need in merged[stage_id]:
            if need not in merged:
                continue
            if state.get(need) == 1:
                return trail[trail.index(need):] + [need]
            if state.get(need) == 2:
                continue
            found = cycle(need)
            if found is not None:
                return found
        trail.pop()
        state[stage_id] = 2
        return None

    by_id = {stage.id: stage for stage in stages}
    for stage_id in merged:
        if state.get(stage_id):
            continue
        found = cycle(stage_id)
        if found is None:
            continue
        # Отказ называет этап, который назвало предложение: у seed-этапа строки в
        # предложении нет, и его `line` — строка отчёта, а не выдуманный номер.
        issues.append(
            Issue(
                where,
                by_id[found[0]].line if found[0] in by_id else 1,
                "plan.cycle",
                f"граф плана цикличен: {' → '.join(found)}",
            )
        )
        break
    if len(known) > charter.max_stages:
        issues.append(
            Issue(
                where,
                1,
                "charter.max_stages",
                f"план растит граф до {len(known)} этапов, а хартия держит не больше "
                f"{charter.max_stages}: дело не растёт",
            )
        )
    return tuple(stages), issues


# ------------------------------------------------------- база как существующий rev


def rev_exists(repo: Path, rev: str) -> Optional[bool]:
    """Есть ли `rev` в `repo` как коммит: True/False, None — git не ответил.

    `--verify --quiet` возвращает ненулевой код на отсутствующем rev, а
    `^{commit}` требует именно коммит: база этапа — точка отведения дерева, а не
    тег-дерево и не путь.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def check_base(
    spec: Spec, *, exists: Optional[Callable[[Path, str], Optional[bool]]] = None
) -> list[Issue]:
    """`base` существует как rev в `repo` — проверка, которой нужен git.

    Репозиторий дела может быть не выложен на этой машине (спека живёт в
    `~/.hermes`, дерево — в рабочем каталоге). Тогда это `warning`, а не отказ:
    спека годна, а факт «base резолвится» проверяется там, где дерево есть.
    """
    if not any(stage.workspace == "worktree" for stage in spec.stages):
        return []
    if spec.repo is None or spec.base is None:
        return []  # уже отказ `workspace`
    line = spec.line_of("base")
    repo = Path(spec.repo).expanduser()
    if not repo.is_dir():
        return [
            Issue(
                spec.path,
                line,
                "base",
                f"репозитория {repo} нет на этой машине: base {spec.base!r} не проверен",
                WARNING,
            )
        ]
    probe = exists if exists is not None else rev_exists
    found = probe(repo, spec.base)
    if found is None:
        return [
            Issue(
                spec.path,
                line,
                "base",
                f"git не ответил про {spec.base!r} в {repo}: base не проверен",
                WARNING,
            )
        ]
    if not found:
        return [
            Issue(
                spec.path,
                line,
                "base",
                f"base {spec.base!r} не существует как rev в {repo}: дерево этапа не от чего отвести",
            )
        ]
    return []


# ------------------------------------------------------------------- сводка


def errors(issues: list[Issue]) -> list[Issue]:
    """Отказы, из-за которых спека не запускается."""
    return [issue for issue in issues if issue.severity == ERROR]
