"""Спека дела `duty.yaml`: загрузчик и валидатор — чистые функции без БД и сети.

Почему это отдельный слой. Спека — единственное место, где сказано, из каких
этапов состоит работа, какие доказательства принимаются и куда идёт вопрос.
Ошибка в ней сегодня проявляется поздно: «публикация обогнала ревью» — это
неправильный `verdict.changes`, который видно только в момент публикации, а
«этап недостижим» — только когда движок до него никогда не дойдёт. Поэтому
контракт читается **до** запуска: `load_spec()` разбирает файл, `validate_spec()`
возвращает список отказов, и ни один из них не требует запускать что-либо.

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

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

# --------------------------------------------------------------- ключи формата

TOP_KEYS = ("duty", "repo", "base", "stages")
STAGE_KEYS = (
    "id",
    "role",
    "workspace",
    "needs",
    "parallel_with",
    "evidence",
    "retry",
    "budget",
    "ask",
    "verdict",
)
EVIDENCE_KEYS = ("cmd", "must", "timeout_sec")
RETRY_KEYS = ("max", "on")
BUDGET_KEYS = ("tokens", "minutes")
ASK_KEYS = ("allowed", "to_human", "max_hops", "q", "options", "risky")
VERDICT_KEYS = ("approve", "changes", "max_rounds")

WORKSPACES = ("worktree", "dir", "none")
TO_HUMAN = ("decisions_only", "never")

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
PROFILES_DIR = Path.home() / ".hermes" / "profiles"
IMPLICIT_PROFILES = ("default",)

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
    """Бюджет попытки: токены и минуты. Незаданное поле — не ограничено."""

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
class Stage:
    """Этап дела. `workspace: none` — этап только читает и выносит приговор."""

    id: str
    role: str
    workspace: str = "none"
    needs: tuple[str, ...] = ()
    parallel_with: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    retry: Optional[Retry] = None
    budget: Optional[Budget] = None
    ask: Optional[Ask] = None
    verdict: Optional[Verdict] = None
    line: int = 0
    lines: dict[str, int] = field(default_factory=dict)

    def line_of(self, key: str) -> int:
        """Строка ключа внутри этапа; неизвестный ключ — строка самого этапа."""
        return self.lines.get(key, self.line)


@dataclass(frozen=True)
class Spec:
    """Спека дела: имя, база и этапы. `parse_issues` — то, что нашлось при разборе."""

    path: Path
    duty: str
    repo: Optional[str]
    base: Optional[str]
    stages: tuple[Stage, ...]
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
    workspace = mapping.get("workspace", "none")
    if not isinstance(workspace, str):
        issues.append(
            Issue(path, lines.get(prefix + ("workspace",), line), "workspace", f"workspace — одно из {', '.join(WORKSPACES)}")
        )
        workspace = ""
    return Stage(
        id=stage_id,
        role=role if isinstance(role, str) else "",
        workspace=workspace,
        needs=needs,
        parallel_with=parallel,
        evidence=_parse_evidence(mapping, path, lines, prefix, issues),
        retry=_parse_retry(mapping, path, lines, prefix, issues),
        budget=_parse_budget(mapping, path, lines, prefix, issues),
        ask=_parse_ask(mapping, path, lines, prefix, issues),
        verdict=_parse_verdict(mapping, path, lines, prefix, issues),
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
        lines={key: lines[(key,)] for key in TOP_KEYS if (key,) in lines},
        parse_issues=tuple(issues),
    )


# ---------------------------------------------------------------- профили роли


_profiles_cache: Optional[frozenset[str]] = None

# Имя модуля политики в `sys.modules`: плагин грузит модули по пути, пакета нет,
# поэтому второй экземпляр `failures.py` в процессе означал бы второй словарь
# классов — и «единственное место, где они определены» перестало бы быть правдой.
FAILURES_MODULE_NAME = "hermes_duty_failures"


def failure_classes() -> tuple[str, ...]:
    """Классы отказа из `dashboard/failures.py` — словарь живёт там, а не здесь.

    Спека сверяет `retry.on` с этим кортежем: опечатка в классе иначе молча
    выключила бы повтор, а спека при этом считалась бы объявленной.
    """
    import importlib.util
    import sys

    loaded = sys.modules.get(FAILURES_MODULE_NAME)
    if loaded is not None:
        return tuple(loaded.CLASSES)
    path = Path(__file__).resolve().parent / "failures.py"
    module_spec = importlib.util.spec_from_file_location(FAILURES_MODULE_NAME, path)
    if module_spec is None or module_spec.loader is None:  # pragma: no cover - файл выпал из сборки
        return ()
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[FAILURES_MODULE_NAME] = module
    module_spec.loader.exec_module(module)
    return tuple(module.CLASSES)


def known_profiles(root: Optional[Path] = None) -> frozenset[str]:
    """Профили Hermes: `~/.hermes/profiles/*` плюс `default`, снимок один раз на процесс.

    `hermes profiles list` не вызывается: во-первых, это процесс на каждую
    проверку спеки, во-вторых, ответ CLI — не контракт каталога. Каталог и есть
    источник: профиль существует, если существует его папка.
    """
    global _profiles_cache
    if root is None and _profiles_cache is not None:
        return _profiles_cache
    base = Path(root) if root is not None else PROFILES_DIR
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
        if stage.role not in profiles:
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
            lowered = " ".join(cmd.lower().split())
            hit = next((prefix for prefix in FORBIDDEN_CMD_PREFIXES if lowered.startswith(prefix)), None)
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
        if spec.repo is None:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("workspace"),
                    "workspace",
                    f"этап {stage.id!r} просит worktree, а repo не задан: от чего отводить дерево",
                )
            )
        if spec.base is None:
            issues.append(
                Issue(
                    spec.path,
                    stage.line_of("workspace"),
                    "workspace",
                    f"этап {stage.id!r} просит worktree, а base не задан: от чего отводить дерево",
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
    issues: list[Issue] = []
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


def validate_spec(spec: Spec, *, profiles: Optional[frozenset[str]] = None) -> list[Issue]:
    """Все отказы спеки до запуска: структура, граф, доказательства, адрес вопросов.

    `profiles` подставляется явно (тест не зависит от машины); по умолчанию —
    снимок `~/.hermes/profiles/*` + `default`. Проверка `base` как rev живёт
    отдельно — `check_base()`: ей нужен git, а не только спека.
    """
    issues: list[Issue] = list(spec.parse_issues)
    issues += _check_stage_ids(spec)
    issues += _check_roles(spec, profiles if profiles is not None else known_profiles())
    issues += _check_refs(spec)
    issues += _check_cycle(spec)
    issues += _check_entry(spec)
    issues += _check_reachable(spec)
    issues += _check_evidence(spec)
    issues += _check_verdicts(spec)
    issues += _check_workspace(spec)
    issues += _check_ask(spec)
    issues += _check_budget(spec)
    issues += _check_retry(spec)
    return issues


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
