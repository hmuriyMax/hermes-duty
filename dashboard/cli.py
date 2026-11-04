"""CLI hermes-duty: тот же бэкенд, что у роутов, но из терминала.

    duty now [--board B] [--state S]      таблица дел: доска, id, ожидание, возраст, доказательство
    duty deal <board> <id>                карточка дела: факты плюс таймлайн попыток
    duty attempts <board> <id>            только таймлайн попыток
    duty stats [--board B] [--since D]    p50/p90/p99, исходы, «с первой попытки»
    duty why <board> <id>                 доказательство словами бэкенда
    duty why <board> <id> <этап>          то же плюс приговор последней попытки этапа
    duty watch [--interval 10]            тот же `now`, перерисовка в терминале
    duty run <deal_id> [--spec F]         довести своё дело по спеке: решение → шаг → журнал
    duty spec check <file>                спека дела до запуска: OK или file:line: правило: текст
    --json (глобальный флаг)              машиночитаемый вывод: ровно форма роута

Ни одного собственного правила здесь нет: команды вызывают те же функции, что и
маршруты (`dashboard/plugin_api.py`: `now`, `deal`, `duty_attempts`, `stats`), а не
ходят в HTTP и не пересчитывают состояния. `--json` печатает ответ роута дословно
— второй формы у CLI нет, поэтому расхождение «терминал сказал одно, экран другое»
невозможно по построению.

Коды возврата (контракт карточки W3):

- `0` — успех: ответ получен целиком, `errors[]` пуст;
- `1` — доска или дело не найдены (роут ответил 404);
- `2` — ошибка источников: роут ответил ошибкой (503 «не прочиталось», 400
  «неизвестное состояние») либо в ответе есть `errors[]` — часть источников не
  прочитана, и выдавать такой ответ за полный нельзя.

`duty watch` деградацию источников не превращает в падение: печатает `errors[]`
строками и продолжает. Отсутствующая доска — исключение: она не появится сама,
поэтому выход сразу с кодом 1.

`duty run` (движок доводит дело до конца) добавляет к этому свои три кода —
`3` ждёт (`wait`: ответа человека, приговора, паузы, чужого лизинга), `4` упёрлось
в предел шагов, `130` прервано с клавиатуры (`dashboard/driver.py`). Решение «что
дальше» по-прежнему считает `transit`, шаг делает `runner`: у CLI и здесь только
оформление и код возврата.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_SOURCE = 2

# Сколько отказов гейта печатает `duty why` в человеческом выводе: полный список
# лежит в событии и в `--json`, в терминале нужны первые.
VERDICT_DETAILS = 5

# Сколько последних упавших попыток показывает `duty why`: свежие факты и счётчики
# важнее всей истории, а полная лента — `duty attempts`.
FAILURE_LOG_LIMIT = 5

# Метка «я уже перезапускался»: без неё неудачный поиск интерпретатора дал бы
# бесконечную цепочку `execv`.
REEXEC_MARK = "DUTY_CLI_REEXEC"

# `dashboard/plugin_api.py` — тот же модуль, что грузит плагин: пакета у него нет,
# поэтому берём его по пути и кладём в `sys.modules` под стабильным именем.
PLUGIN_API_MODULE_NAME = "hermes_duty_plugin_api"


class NotFound(RuntimeError):
    """Дело или доска не найдены (роут ответил 404) — код возврата 1."""


class SourceFailure(RuntimeError):
    """Источники не прочитались (503/400 роута или `errors[]` ответа) — код 2."""


# --------------------------------------------------------------- бэкенд роутов


def _load_plugin_api() -> Any:
    """Загрузить `dashboard/plugin_api.py` — тот же модуль, что у маршрутов."""
    loaded = sys.modules.get(PLUGIN_API_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(PLUGIN_API_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise SourceFailure(f"plugin_api module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_API_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


DEMO_MODULE_NAME = "hermes_duty_demo"
# Имя модуля спеки: рядом с demo — `dashboard/` грузится по пути, не пакетом.
SPEC_MODULE_NAME = "hermes_duty_spec"
# Журнал: `why <доска> <id> <этап>` читает те же события, что и движок.
JOURNAL_MODULE_NAME = "hermes_duty_journal"
FAILURES_MODULE_NAME = "hermes_duty_failures"
# Драйвер дела: `run` зовёт его, а не собирает цикл «решение → шаг» в терминале.
DRIVER_MODULE_NAME = "hermes_duty_driver"


def _load_demo() -> Any:
    """Загрузить `dashboard/demo.py` — прогон задачи по спеке (не роут)."""
    loaded = sys.modules.get(DEMO_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "demo.py"
    spec = importlib.util.spec_from_file_location(DEMO_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise SourceFailure(f"demo module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[DEMO_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _load_spec() -> Any:
    """Загрузить `dashboard/spec.py` — загрузчик и валидатор спеки дела.

    Спека проверяется тем же модулем, что будет читать её движок: у CLI нет
    своего набора правил, только оформление вывода.
    """
    loaded = sys.modules.get(SPEC_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "spec.py"
    spec = importlib.util.spec_from_file_location(SPEC_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise SourceFailure(f"spec module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[SPEC_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _load_journal() -> Any:
    """Загрузить `dashboard/journal.py` — те же события, что читает движок.

    `duty why <доска> <id> <этап>` показывает приговор последней попытки по
    журналу плагина, а не по своим представлениям о нём: второго чтения тех же
    фактов в терминале быть не должно.
    """
    loaded = sys.modules.get(JOURNAL_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "journal.py"
    spec = importlib.util.spec_from_file_location(JOURNAL_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise SourceFailure(f"journal module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[JOURNAL_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _load_failures() -> Any:
    """Загрузить `dashboard/failures.py` — политику ретраев, один экземпляр на процесс.

    Счётчик `3/2 infra` и `retry_at` считает политика, а не CLI: иначе терминал
    показывал бы лимиты по своей копии правил.
    """
    loaded = sys.modules.get(FAILURES_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "failures.py"
    spec = importlib.util.spec_from_file_location(FAILURES_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - файл выпал из сборки
        raise SourceFailure(f"failures module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[FAILURES_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _load_driver() -> Any:
    """Загрузить `dashboard/driver.py` — движок, который доводит дело до конца.

    `run` не собирает цикл у себя: решение считает `transit`, шаг делает
    `runner`, а драйвер только связывает их. Вторая копия цикла в терминале
    означала бы второй набор поведения.
    """
    loaded = sys.modules.get(DRIVER_MODULE_NAME)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parent / "driver.py"
    spec = importlib.util.spec_from_file_location(DRIVER_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise SourceFailure(f"driver module not found: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[DRIVER_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


_API: Any = None


def api() -> Any:
    """Модуль бэкенда (грузится один раз на процесс)."""
    global _API
    if _API is None:
        _API = _load_plugin_api()
    return _API


def _route(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Вызвать функцию роута и перевести её отказ в код возврата CLI.

    Коды берутся из того, что уже решил роут: 404 — «не найдено» (1), остальное
    (503 «источник не прочитался», 400 «неизвестное состояние») — ошибка
    источников или разбора (2). Мутации отвечают тем же: `DealNotFound` —
    «дела нет» (1), `DealError` — «вход не тот» (2). Второго набора правил у CLI
    нет.
    """
    try:
        return fn(*args, **kwargs)
    except (NotFound, SourceFailure):
        raise
    except Exception as exc:  # отказ роута и есть ответ CLI
        detail = getattr(exc, "detail", None) or str(exc) or f"{type(exc).__name__}: {exc}"
        missing = getattr(exc, "status_code", None) == 404 or isinstance(
            exc, api().deals.DealNotFound
        )
        if missing:
            raise NotFound(str(detail)) from exc
        raise SourceFailure(str(detail)) from exc


def _payload_code(payload: dict[str, Any]) -> int:
    """Код возврата по ответу: `errors[]` значит «часть источников не прочитана»."""
    return EXIT_SOURCE if payload.get("errors") else EXIT_OK


def _agent_pythons() -> list[str]:
    """Кандидаты в интерпретатор агента: в них есть fastapi, нужный бэкенду.

    Смотрим и в `HERMES_HOME`, и в домашний `~/.hermes`: `HERMES_HOME` бывает
    профилем (`~/.hermes/profiles/<role>`), а интерпретатор с зависимостями живёт
    в корне агента — привязывать поиск к одному пути значит ломать CLI в
    профильной оболочке.
    """
    explicit = os.environ.get("DUTY_PYTHON")
    roots = [Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()]
    roots.append(Path.home() / ".hermes")
    found: list[str] = [explicit] if explicit else []
    for root in roots:
        for base in (root / "hermes-agent" / "venv" / "bin", root / "bvenv" / "bin"):
            for name in ("python3", "python"):
                candidate = base / name
                if candidate.is_file() and os.access(candidate, os.X_OK) and str(candidate) not in found:
                    found.append(str(candidate))
    return found


def _ensure_interpreter(argv: Optional[list[str]]) -> None:
    """Перезапустить CLI интерпретатором агента, если в текущем нет fastapi.

    `dashboard/plugin_api.py` импортирует fastapi на уровне модуля, а в системном
    `python3` (Xcode) его нет. Перезапуск `os.execv` оставляет один и тот же код и
    одну форму ответа — это не второй путь вычислений, а тот же самый под другим
    интерпретатором.
    """
    if importlib.util.find_spec("fastapi") is not None:
        return
    args = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get(REEXEC_MARK):
        print(
            "duty: в текущем интерпретаторе нет fastapi, а интерпретатор агента не найден "
            f"({sys.executable}). Укажите его через DUTY_PYTHON.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_SOURCE)
    for candidate in _agent_pythons():
        if Path(candidate).resolve() == Path(sys.executable).resolve():
            continue
        os.environ[REEXEC_MARK] = "1"
        try:
            os.execv(candidate, [candidate, str(Path(__file__).resolve()), *args])
        except OSError:
            continue
    print(
        "duty: не найден интерпретатор с fastapi — ни текущий, ни интерпретатор агента. "
        "Укажите его через DUTY_PYTHON.",
        file=sys.stderr,
    )
    raise SystemExit(EXIT_SOURCE)


# ------------------------------------------------------------------ разбор argv


def since_arg(value: str) -> int:
    """`--since D` → unix-секунды: unix-время, `YYYY-MM-DD` или `YYYY-MM-DDTHH:MM[:SS]`.

    Дата без времени читается как полночь по местным часам: «с этого дня» значит
    «с начала дня», а не «с текущей секунды». Ничего не додумываем: непонятная
    дата — ошибка разбора (`argparse` отвечает кодом 2).
    """
    text = value.strip()
    if text.isdigit():
        return int(text)
    try:
        moment = datetime.datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since: ожидалось unix-время, YYYY-MM-DD или YYYY-MM-DDTHH:MM, получено {value!r}"
        ) from exc
    return int(moment.timestamp())


def build_parser() -> argparse.ArgumentParser:
    """Разбор команд. `--json` объявлен и глобально, и внутри команд: флаг должен
    работать и до команды, и после неё — это одна и та же опция."""
    parser = argparse.ArgumentParser(
        prog="duty",
        description="Дела hermes-duty из терминала: те же функции, что у роутов, без HTTP.",
    )
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод: ровно форма роута")
    commands = parser.add_subparsers(dest="command", metavar="команда")

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        # `default=SUPPRESS`: иначе подпарсер перезапишет `--json`, уже прочитанный
        # до команды (`duty --json now`), своим значением по умолчанию.
        sub.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="машиночитаемый вывод: ровно форма роута",
        )
        return sub

    now = command("now", "таблица дел: доска, id, ожидание, возраст, доказательство")
    now.add_argument("--board", help="только одна доска (slug)")
    now.add_argument("--state", help="только дела в этом waiting.state")

    deal = command("deal", "карточка дела: факты плюс таймлайн попыток")
    deal.add_argument("board")
    deal.add_argument("task_id")

    attempts = command("attempts", "только таймлайн попыток")
    attempts.add_argument("board")
    attempts.add_argument("task_id")

    stats = command("stats", "p50/p90/p99, исходы, доля с первой попытки")
    stats.add_argument("--board", help="только одна доска (slug)")
    stats.add_argument("--since", type=since_arg, help="окно по created_at: unix, YYYY-MM-DD или YYYY-MM-DDTHH:MM")

    why = command("why", "доказательство словами бэкенда; с этапом — приговор его последней попытки")
    why.add_argument("board")
    why.add_argument("task_id")
    why.add_argument(
        "stage",
        nargs="?",
        help="этап дела: печатает последнюю попытку, класс отказа и первые details",
    )

    watch = command("watch", "тот же now, перерисовка в терминале")
    watch.add_argument("--interval", type=float, default=10.0, help="секунд между тиками (по умолчанию 10)")
    watch.add_argument("--board", help="только одна доска (slug)")
    watch.add_argument("--state", help="только дела в этом waiting.state")

    # Драйвер дела (`docs/RUNBOOK.md`): цикл «решение → шаг → перечитать журнал».
    # Решение считает `transit`, шаг делает `runner`; CLI только разбирает argv и
    # оформляет отчёт, поэтому правил перехода здесь нет.
    run = command("run", "довести своё дело по спеке: решение → шаг → перечитать журнал")
    run.add_argument("deal_id", help="id своего дела (d_…): у него есть store и журнал")
    run.add_argument(
        "--spec",
        help="спека дела (duty.yaml): по умолчанию — путь из журнала дела (created.payload.spec)",
    )
    run.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="предел шагов прогона: превышение — код 4 (по умолчанию 20 — DEFAULT_MAX_STEPS драйвера)",
    )
    run.add_argument("--once", action="store_true", help="один шаг и выход (отладка и тесты)")

    # Мутации своего дела (`docs/OWNERSHIP.md`): те же функции, что у роутов
    # `/deals…`, — CLI не заводит второго набора правил.
    create = command("create", "завести своё дело (id d_<hex>)")
    create.add_argument("--board", required=True, help="слаг доски (kanban-доска или своя в store)")
    create.add_argument("--title", required=True, help="заголовок дела")
    create.add_argument("--body-file", help="файл с телом дела (спецификацией)")
    create.add_argument("--assignee", help="профиль-роль исполнителя")
    create.add_argument(
        "--spec",
        help="спека дела (duty.yaml): по ней экран «Дело» рисует этапы; путь разворачивается в абсолютный",
    )
    create.add_argument("--parent", action="append", default=[], help="id своего родителя (d_…); можно несколько")
    create.add_argument(
        "--external-ref",
        help=(
            "внешнее имя дела (например jira:RP-14080): живое дело с этим именем "
            "не дублируется, закрытое — не воскрешается"
        ),
    )

    comment = command("comment", "комментарий в своё дело")
    comment.add_argument("deal_id")
    comment.add_argument("body", help="текст комментария")
    comment.add_argument("--author", default="max", help="автор (по умолчанию max)")

    block = command("block", "заблокировать своё дело")
    block.add_argument("deal_id")
    block.add_argument("reason", nargs="?", default="", help="причина блокировки")
    block.add_argument("--kind", default="needs_input", help="block_kind (по умолчанию needs_input)")

    unblock = command("unblock", "снять блокировку своего дела")
    unblock.add_argument("deal_id")

    claim = command("claim", "взять своё дело в работу")
    claim.add_argument("deal_id")
    claim.add_argument("--owner", help="профиль-владелец (по умолчанию HERMES_PROFILE или пользователь)")

    complete = command("complete", "закрыть своё дело или сдать в ревью")
    complete.add_argument("deal_id")
    complete.add_argument("--summary", default="", help="итог дела")
    complete.add_argument("--review", action="store_true", help="сдать в ревью вместо закрытия")
    complete.add_argument("--reviewer", help="профиль ревьюера")

    review = command("review", "сдать своё дело в ревью")
    review.add_argument("deal_id")
    review.add_argument("--reviewer", help="профиль ревьюера")
    review.add_argument("--summary", default="", help="что сдаётся")

    changes = command("changes", "вернуть своё дело на доработку")
    changes.add_argument("deal_id")
    changes.add_argument("reason", help="причина возврата")
    changes.add_argument("--reviewer", help="кто вернул")

    attach = command("attach", "приложить файл к своему делу")
    attach.add_argument("deal_id")
    attach.add_argument("file", help="путь к файлу")

    archive = command("archive", "пометить своё дело archived (PATCH {status: archived})")
    archive.add_argument("deal_id")

    # Прогон задачи по спеке на песочном корне: не роут, а демонстрация модели
    # (`dashboard/demo.py`). Живые БД только читаются — и это проверяется самим
    # прогоном, поэтому команда безопасна для боевой машины.
    demo = command("demo", "прогон дела по спеке на песочном корне (живые БД не трогает)")
    demo.add_argument("--home", help="песочный корень (по умолчанию временный)")
    demo.add_argument("--keep", action="store_true", help="не удалять песочный корень после прогона")

    # Спека дела до запуска (`docs/SPEC.md`): единственная команда слоя описания.
    # Правила живут в `dashboard/spec.py`, здесь только разбор argv и вывод.
    spec = command("spec", "спека дела: проверка до запуска")
    spec_commands = spec.add_subparsers(dest="spec_command", metavar="подкоманда")
    check = spec_commands.add_parser(
        "check", help="проверить duty.yaml: OK или отказы с file:line", description="проверить duty.yaml до запуска"
    )
    check.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="машиночитаемый вывод: ok и список отказов",
    )
    check.add_argument("file", help="путь к спеке (только абсолютный)")
    return parser


# Опции команд мутаций, у которых значение идёт отдельным словом. Нужны
# `_positionals_first`: без них нормализация спутала бы значение опции с
# позиционным аргументом. Список относится ровно к тем командам, которые она
# нормализует (`block`, `changes`).
VALUE_OPTIONS = ("--kind", "--reviewer")


def _positionals_first(argv: list[str], commands: tuple[str, ...]) -> list[str]:
    """Поставить позиционные аргументы перед опциями для указанных команд.

    `argparse` заполняет позиционные одним куском в начале команды, поэтому
    причина, написанная после опции (`duty block d_… --kind needs_input
    "причина"` — форма из `docs/OWNERSHIP.md`), попадала бы в «unrecognized
    arguments». Здесь argv приводится к тому, что разбор умеет, а не наоборот:
    правило одно и живёт в одном месте.
    """
    words = list(argv)
    index = next((i for i, item in enumerate(words) if not item.startswith("-")), None)
    if index is None or words[index] not in commands:
        return words
    positionals: list[str] = []
    options: list[str] = []
    cursor = index + 1
    while cursor < len(words):
        item = words[cursor]
        if item.startswith("-"):
            options.append(item)
            if item in VALUE_OPTIONS and cursor + 1 < len(words):
                options.append(words[cursor + 1])
                cursor += 2
                continue
        else:
            positionals.append(item)
        cursor += 1
    return words[: index + 1] + positionals + options


# ------------------------------------------------------------------ оформление


def _fmt_age(sec: Any) -> str:
    """Возраст/длительность словами: `45 с`, `12 мин`, `3 ч`, `2 д`."""
    if sec is None or sec == "":
        return "—"
    value = max(0, int(sec))
    if value < 60:
        return f"{value} с"
    if value < 3600:
        return f"{value // 60} мин"
    if value < 86400:
        return f"{value // 3600} ч"
    return f"{value // 86400} д"


def _fmt_clock(at: Any) -> str:
    """Время события: ЧЧ:ММ:СС (попытки идут минутами, секунды нужны для порядка)."""
    if not at:
        return "—"
    return time.strftime("%H:%M:%S", time.localtime(int(at)))


def _fmt_stamp(at: Any) -> str:
    """Метка среза: дата и время, чтобы «сейчас» не путалось со «вчера»."""
    if not at:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(at)))


def _age(at: Any, now: Any) -> Optional[int]:
    """Сколько секунд назад была метка — вычитание двух полей ответа, не правило."""
    if at is None or not now:
        return None
    return max(0, int(now) - int(at))


def _clip(text: Any, width: int) -> str:
    """Одна строка не длиннее `width`: переводы строк ломают таблицу."""
    value = " ".join(str("" if text is None else text).split())
    if len(value) <= width:
        return value
    return value[: width - 1].rstrip() + "…"


def _table(columns: tuple[tuple[str, int], ...], rows: list[list[Any]]) -> str:
    """Простая таблица: ширина колонки — максимум из заголовка и обрезанных ячеек."""
    cells = [[_clip(cell, width) for cell, (_, width) in zip(row, columns)] for row in rows]
    widths: list[int] = []
    for index, (header, _) in enumerate(columns):
        width = len(header)
        for row in cells:
            width = max(width, len(row[index]))
        widths.append(width)
    lines = ["  ".join(header.ljust(widths[i]) for i, (header, _) in enumerate(columns)).rstrip()]
    for row in cells:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _counts(mapping: Any) -> str:
    """Счётчики строкой в порядке убывания: `completed 200 · crashed 12`."""
    items = [(str(key), int(value)) for key, value in (mapping or {}).items()]
    if not items:
        return "—"
    items.sort(key=lambda item: (-item[1], item[0]))
    return " · ".join(f"{key} {value}" for key, value in items)


def _distribution(dist: Any) -> str:
    """Распределение ожидания: перцентили бэкенда как есть, без пересчёта."""
    data = dist or {}
    if not data.get("count"):
        return "нет данных (ни у одного дела не было первой попытки)"
    parts = [
        f"p{pct} {_fmt_age(data.get(f'p{pct}'))}" for pct in (50, 90, 99) if data.get(f"p{pct}") is not None
    ]
    return f"{' · '.join(parts)} (n={data.get('count')}, максимум {_fmt_age(data.get('max'))})"


def _share(first: Any) -> str:
    """«С первой попытки»: доля и её основание, а не одно число без знаменателя."""
    data = first or {}
    with_runs = int(data.get("with_runs") or 0)
    if not with_runs:
        return "нет ранов — доля не считается"
    ok = int(data.get("ok") or 0)
    share = data.get("share")
    percent = "—" if share is None else f"{round(float(share) * 100)}%"
    return f"{ok}/{with_runs} ({percent})"


def _share_cell(first: Any) -> str:
    """Та же доля компактно для таблицы: у доски без ранов — прочерк, «нет ранов» один раз в итоге."""
    data = first or {}
    if not int(data.get("with_runs") or 0):
        return "—"
    share = data.get("share")
    percent = "—" if share is None else f"{round(float(share) * 100)}%"
    return f"{int(data.get('ok') or 0)}/{int(data['with_runs'])} ({percent})"


def _notice_lines(payload: dict[str, Any]) -> list[str]:
    """Строки деградации: что не прочиталось и что прочитано неполно.

    Роут отвечает 200 и называет это в `errors[]`/`warnings[]`; CLI не выдаёт
    частичный ответ за полный, но и не падает из-за него.
    """
    lines: list[str] = []
    for error in payload.get("errors") or []:
        lines.append(f"! источник {error.get('board')}: {error.get('reason')}")
    for warning in payload.get("warnings") or []:
        lines.append(f"! {warning}")
    for board in payload.get("boards") or []:
        for warning in board.get("warnings") or []:
            lines.append(f"! доска {board.get('slug')}: {warning}")
        if board.get("truncated"):
            lines.append(f"! доска {board.get('slug')}: срез обрезан лимитом — картина неполная")
    return lines


# -------------------------------------------------------------------- команды


NOW_COLUMNS = (
    ("ДОСКА", 16),
    ("ID", 14),
    ("СОСТОЯНИЕ", 20),
    ("ОЖИДАНИЕ", 9),
    ("ВОЗРАСТ", 8),
    ("ДЕЛО", 28),
    ("ДОКАЗАТЕЛЬСТВО", 72),
)

STATS_COLUMNS = (
    ("ДОСКА", 16),
    ("ЗАДАЧ", 7),
    ("РАНОВ", 7),
    ("С ПЕРВОЙ", 15),
    ("P50", 8),
    ("P90", 8),
    ("P99", 8),
    ("ОЖИДАНИЙ", 9),
)


def _now_row(deal: dict[str, Any], now: Any) -> list[Any]:
    """Строка таблицы «сейчас» из дела ответа `/now`.

    `ОЖИДАНИЕ` — возраст текущей попытки, а если попытки нет — возраст дела: ровно
    то число, по которому `/now` сортирует дела. `ВОЗРАСТ` — возраст самого дела.
    Обе колонки — вычитание полей ответа, не новые правила.
    """
    waiting = deal.get("waiting") or {}
    attempt = deal.get("attempt") or {}
    state = str(waiting.get("state") or "—")
    human_kind = waiting.get("human_kind")
    if human_kind:
        state = f"{state}/{human_kind}"
    deal_age = _age(deal.get("created_at"), now)
    wait_age = attempt.get("age_sec")
    if wait_age is None:
        wait_age = deal_age
    return [
        deal.get("board"),
        deal.get("id"),
        state,
        _fmt_age(wait_age),
        _fmt_age(deal_age),
        deal.get("title"),
        waiting.get("evidence"),
    ]


def render_now(payload: dict[str, Any]) -> str:
    """Таблица дел: та же сортировка и те же состояния, что у `/now`."""
    lines = [f"ДЕЛА СЕЙЧАС · {_fmt_stamp(payload.get('now'))} · дел: {payload.get('count', 0)}"]
    counted = ", ".join(
        f"{name} {count}" for name, count in (payload.get("by_state") or {}).items() if count
    )
    if counted:
        lines.append(f"по состояниям: {counted}")
    lines.extend(_notice_lines(payload))
    deals = payload.get("deals") or []
    if not deals:
        lines.append("")
        lines.append("активных дел нет")
        return "\n".join(lines)
    lines.append("")
    lines.append(_table(NOW_COLUMNS, [_now_row(deal, payload.get("now")) for deal in deals]))
    lines.append("")
    lines.append("доказательство целиком: duty why <доска> <id> · таймлайн: duty attempts <доска> <id>")
    return "\n".join(lines)


def _board_row(board: dict[str, Any]) -> list[Any]:
    """Строка таблицы «по доскам»: непрочитанная доска — прочерки, а не нули."""
    if not board.get("read"):
        return [board.get("slug"), "—", "—", "не прочитана", "—", "—", "—", "—"]
    first = board.get("first_try") or {}
    dist = board.get("waiting_sec") or {}
    return [
        board.get("slug"),
        board.get("tasks_total"),
        board.get("runs_total"),
        _share_cell(first),
        _fmt_age(dist.get("p50")),
        _fmt_age(dist.get("p90")),
        _fmt_age(dist.get("p99")),
        dist.get("count", 0),
    ]


def render_stats(payload: dict[str, Any]) -> str:
    """Здоровье: перцентили ожидания, исходы и доля с первой попытки."""
    totals = payload.get("totals") or {}
    boards = payload.get("boards") or []
    lines = [
        "ЗДОРОВЬЕ · {} · досок: {} · задач: {} · ранов: {}".format(
            _fmt_stamp(payload.get("now")),
            len(boards),
            totals.get("tasks_total", 0),
            totals.get("runs_total", 0),
        )
    ]
    lines.extend(_notice_lines(payload))
    lines.append("")
    lines.append(f"с первой попытки: {_share(totals.get('first_try'))}")
    lines.append(f"ожидание до первой попытки: {_distribution(totals.get('waiting_sec'))}")
    lines.append(f"исходы ранов: {_counts(totals.get('runs_by_outcome'))}")
    lines.append(f"статусы дел: {_counts(totals.get('tasks_by_status'))}")
    if boards:
        lines.append("")
        lines.append(_table(STATS_COLUMNS, [_board_row(board) for board in boards]))
    return "\n".join(lines)


def _attempt_head(attempt: dict[str, Any]) -> str:
    """Первая строка попытки: кто, чем закончилось, сколько шло."""
    bits = [f"#{attempt.get('run_id')}", str(attempt.get("profile") or "—"), str(attempt.get("status") or "—")]
    if attempt.get("outcome"):
        bits.append(f"outcome={attempt.get('outcome')}")
    bits.append(f"старт {_fmt_clock(attempt.get('start'))}")
    bits.append(f"длительность {_fmt_age(attempt.get('duration_sec'))}")
    return " · ".join(bits)


def render_attempts(payload: dict[str, Any]) -> str:
    """Таймлайн попыток: раны по возрастанию старта плюс их события."""
    lines = [
        "ПОПЫТКИ · {}/{} · {} · {}".format(
            payload.get("board"),
            payload.get("task_id"),
            _clip(payload.get("title"), 80),
            payload.get("count", 0),
        )
    ]
    attempts = payload.get("attempts") or []
    if not attempts:
        lines.append("")
        lines.append("попыток нет")
        return "\n".join(lines)
    for attempt in attempts:
        lines.append("")
        lines.append(_attempt_head(attempt))
        if attempt.get("summary"):
            lines.append(f"  итог: {attempt.get('summary')}")
        if attempt.get("error"):
            lines.append(f"  ошибка: {attempt.get('error')}")
        for event in attempt.get("events") or []:
            summary = event.get("summary") or ""
            lines.append(f"  {_fmt_clock(event.get('at'))}  {event.get('kind')}  {summary}".rstrip())
    return "\n".join(lines)


def _waiting_head(payload: dict[str, Any]) -> str:
    """Строка состояния ожидания: состояние, вид «нужен человек» и приговор журнала."""
    shaped = payload.get("deal")
    if shaped is None:
        if payload.get("terminal"):
            return f"не считается: дело закрыто (status={payload.get('status')}), ждать нечего"
        return (
            "не посчитано: дела нет в срезе «сейчас» — журнал говорит completed "
            "либо срез обрезан лимитом (решение роута /deal)"
        )
    waiting = shaped.get("waiting") or {}
    state = str(waiting.get("state") or "—")
    if waiting.get("human_kind"):
        state = f"{state}/{waiting['human_kind']}"
    return state


def render_deal(payload: dict[str, Any], timeline: Optional[dict[str, Any]], timeline_error: Optional[str]) -> str:
    """Карточка дела: спецификация, факты ожидания и таймлайн попыток."""
    lines = [
        "ДЕЛО {}/{} · {} · {} · создано {}".format(
            payload.get("board"),
            payload.get("id"),
            payload.get("status") or "—",
            payload.get("assignee") or "—",
            _fmt_stamp(payload.get("created_at")),
        )
    ]
    lines.append(f"заголовок: {payload.get('title') or '—'}")
    lines.append(f"состояние ожидания: {_waiting_head(payload)}")
    shaped = payload.get("deal")
    if shaped is not None:
        lines.append(f"доказательство: {(shaped.get('waiting') or {}).get('evidence') or 'не записано'}")
        lines.append("")
        lines.extend(_fact_lines(shaped, payload.get("now")))
    # Тело карточки — спецификация дела (как на экране «Дело»); в CLI оно не
    # печатается, чтобы карточка оставалась фактами и таймлайном. Целиком оно
    # есть в `--json` того же роута — дословно.
    lines.append("")
    if timeline is None:
        lines.append(f"! таймлайн попыток не прочитан: {timeline_error}")
    else:
        lines.append(render_attempts(timeline))
    return "\n".join(lines)


def _fact_lines(shaped: dict[str, Any], now: Any) -> list[str]:
    """Факты дела словами: попытка, лизинг, последнее действие — поля ответа как есть."""
    lines: list[str] = []
    attempt = shaped.get("attempt")
    if attempt is None:
        lines.append("  попытка: нет")
    else:
        alive = {True: "pid жив", False: "pid мёртв", None: "pid не записан или неизвестен"}[
            attempt.get("pid_alive")
        ]
        lines.append(
            f"  попытка: ран {attempt.get('run_id')} {attempt.get('run_status') or '—'}, "
            f"{alive} ({attempt.get('pid') or '—'}), heartbeat {_fmt_age(attempt.get('heartbeat_age_sec'))} назад, "
            f"факты из {attempt.get('source')}"
        )
    lease = shaped.get("lease") or {}
    lease_state = str(lease.get("state") or "—")
    if lease_state == "none":
        lines.append("  лизинг: нет")
    else:
        lines.append(
            f"  лизинг: {lease_state}, истекает через {_fmt_age(lease.get('expires_in_sec'))} "
            f"({lease.get('claim_lock') or '—'})"
        )
    action = shaped.get("last_action") or {}
    lines.append(
        f"  последнее действие: {action.get('kind') or '—'} {_fmt_age(action.get('age_sec'))} назад"
        + (f" — {action.get('summary')}" if action.get("summary") else "")
    )
    last_run = shaped.get("last_run")
    if last_run:
        lines.append(
            f"  последний ран: {last_run.get('run_id')} {last_run.get('status') or '—'} "
            f"outcome={last_run.get('outcome') or '—'}"
        )
    lines.append(f"  возраст дела: {_fmt_age(_age(shaped.get('created_at'), now))}")
    return lines


def render_why(payload: dict[str, Any]) -> str:
    """Доказательство словами бэкенда: тот же `waiting.evidence`, что на экране «Почему стоит».

    Если этап назван (`duty why <доска> <id> <этап>`), ниже печатается приговор
    его последней попытки — из журнала, тем же вердиктом, что записал движок.
    """
    lines = [
        "ПОЧЕМУ СТОИТ {}/{} · {} · {}".format(
            payload.get("board"),
            payload.get("id"),
            payload.get("status") or "—",
            payload.get("assignee") or "—",
        )
    ]
    lines.append(f"заголовок: {payload.get('title') or '—'}")
    lines.append(f"состояние ожидания: {_waiting_head(payload)}")
    shaped = payload.get("deal")
    if shaped is None:
        lines.append("доказательство: не записано — роут /deal не вернул фактов ожидания")
    else:
        lines.append("доказательство (словами бэкенда):")
        lines.append(f"  {(shaped.get('waiting') or {}).get('evidence') or 'не записано'}")
        lines.append("факты, из которых оно собрано:")
        lines.extend(_fact_lines(shaped, payload.get("now")))
    verdict = payload.get("verdict")
    if isinstance(verdict, dict):
        lines.append("")
        lines.append(render_stage_verdict(verdict))
    failures = payload.get("failures")
    if isinstance(failures, dict):
        lines.append("")
        lines.append(render_failure_log(failures))
    return "\n".join(lines)


# ------------------------------------------------------------------- мутации


def render_mutation(payload: dict[str, Any], action: str) -> str:
    """Итог мутации словами: дело, его статус, ожидание и что именно записалось.

    Ни одного нового правила здесь нет: статус, ожидание и доказательство — поля
    ответа роута. `событий записано: 0 · повтор` — это ответ «уже было», а не
    «ничего не получилось», и он назван словами, чтобы пилот не гадал.
    """
    mutation = payload.get("mutation") or {}
    lines = [
        f"{action} {payload.get('board')}/{payload.get('id')} · {payload.get('status') or '—'}",
        f"заголовок: {payload.get('title') or '—'}",
    ]
    if payload.get("external_ref"):
        lines.append(f"внешнее имя: {payload['external_ref']}")
    if payload.get("assignee"):
        lines.append(f"исполнитель: {payload.get('assignee')}")
    shaped = payload.get("deal")
    if shaped is None:
        lines.append(
            "ожидание: не считается — дело закрыто"
            if payload.get("terminal")
            else "ожидание: не посчитано (ответ роута без фактов)"
        )
    else:
        waiting = shaped.get("waiting") or {}
        state = str(waiting.get("state") or "—")
        if waiting.get("human_kind"):
            state = f"{state}/{waiting['human_kind']}"
        lines.append(f"ожидание: {state}")
        lines.append(f"доказательство: {waiting.get('evidence') or 'не записано'}")
    events = mutation.get("events")
    tail = "" if not mutation.get("idempotent") else " · повтор: store не тронут"
    lines.append(f"событий записано: {events if events is not None else '—'}{tail}")
    if payload.get("created") is False:
        # Дело нашлось по внешнему имени: это ответ, а не ошибка, и причина
        # называется словами — иначе «СОЗДАНО d_…» читалось бы как второе дело.
        reason = payload.get("reason") or "живое дело с тем же внешним именем уже есть"
        lines.append(f"нового дела нет: {reason}")
    if payload.get("comments"):
        lines.append(f"комментариев в деле: {len(payload['comments'])}")
    if payload.get("attachments"):
        last = payload["attachments"][-1]
        lines.append(
            f"вложений в деле: {len(payload['attachments'])} "
            f"(последнее: {last.get('filename')} → {last.get('path')})"
        )
    return "\n".join(lines)


def _mutate(fn: Any, action: str, *args: Any, as_json: bool = False, **kwargs: Any) -> int:
    """Выполнить мутацию и напечатать ответ; код возврата — как у остальных команд.

    `--json` печатает ответ дословно (ровно форма роута + блок `mutation`): скрипт
    берёт id своего дела оттуда, а не парсит человеческую строку.
    """
    payload = _route(fn, *args, **kwargs)
    if as_json:
        _emit_json(payload, sys.stdout)
    else:
        print(render_mutation(payload, action), file=sys.stdout)
    return _payload_code(payload)


def _claim_owner(explicit: Optional[str]) -> str:
    """Владелец для `claim`: явный, иначе профиль агента, иначе пользователь ОС.

    Владелец — факт, а не догадка: у агента есть `HERMES_PROFILE`, у человека —
    учётка; подставлять «duty» значило бы назвать владельцем плагин.
    """
    if explicit:
        return explicit
    return os.environ.get("HERMES_PROFILE") or getpass.getuser()


# ----------------------------------------------------------------------- watch

def run_watch(
    interval: float,
    *,
    board: Optional[str] = None,
    state: Optional[str] = None,
    as_json: bool = False,
    ticks: Optional[int] = None,
    stream: Any = None,
) -> int:
    """Перерисовывать `now` в терминале, не падая на деградации источников.

    `ticks` ограничивает число тиков (тесты и разовые прогоны), `None` — вечно,
    до сигнала. Экран чистится только когда вывод — терминал: в файле и в пайпе
    остаются блоки текста, а не escape-последовательности.
    """
    out = stream or sys.stdout
    tick = 0
    while ticks is None or tick < ticks:
        tick += 1
        first_tick = tick == 1
        try:
            payload = _route(api().now, board=board, state=state)
        except NotFound as exc:
            # Доска не появится сама: это не деградация, а неверный аргумент.
            print(f"! {exc}", file=sys.stderr)
            return EXIT_NOT_FOUND
        except SourceFailure as exc:
            # Источник может ожить через тик — печатаем строку и продолжаем. Но не
            # на первом тике: там отказ значит «аргументы неверны или источников
            # нет вовсе», и молча крутиться в ошибке — не ответ. В `--json` строка
            # уходит в stderr: stdout — поток ответов роута, и построчный
            # потребитель не должен получать из него не-JSON.
            _write(sys.stderr if as_json else out, f"! источники: {exc}")
            if first_tick:
                return EXIT_SOURCE
        else:
            body = json.dumps(payload, ensure_ascii=False) if as_json else render_now(payload)
            if out.isatty() and not as_json:
                _write(out, "\x1b[2J\x1b[H" + body)
            else:
                _write(out, body if tick == 1 else "\n" + body)
        if ticks is not None and tick >= ticks:
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:  # pragma: no cover - ручное прерывание
            break
    return EXIT_OK


def _write(stream: Any, text: str) -> None:
    """Один блок вывода плюс сброс буфера: watch должен обновляться сразу."""
    stream.write(text + "\n")
    stream.flush()


# ---------------------------------------------------------------- спека дела


def _issue_dict(issue: Any) -> dict[str, Any]:
    """Один отказ спеки в форме `--json`: те же поля, что печатает человек."""
    return {
        "path": str(issue.path),
        "line": issue.line,
        "rule": issue.rule,
        "message": issue.message,
        "severity": issue.severity,
    }


def spec_report(path: str) -> dict[str, Any]:
    """Проверка спеки: тот же ответ для человека и для `--json`.

    Правила целиком в `dashboard/spec.py`; здесь — только вызов и форма ответа.
    Вход CLI приводится к абсолютному пути здесь: движок принимает только
    абсолютный (иначе «откуда взялась спека» не восстановить в журнале), а
    человек у терминала вправе написать `examples/duty.yaml` — в ответе он
    увидит, какой именно файл проверен.

    Нечитаемая спека (путь, YAML, нет `duty`/`stages`) — не список отказов, а
    отсутствие спеки: она помечается `unreadable`, код возврата 2.
    """
    module = _load_spec()
    absolute = Path(path).expanduser().resolve()
    payload: dict[str, Any] = {
        "ok": False,
        "path": str(absolute),
        "duty": "",
        "stages": 0,
        "errors": 0,
        "warnings": 0,
        "issues": [],
    }
    try:
        spec = module.load_spec(absolute)
    except module.SpecError as exc:
        payload["issues"] = [_issue_dict(exc.issue())]
        payload["errors"] = 1
        payload["unreadable"] = True
        return payload
    issues = module.validate_spec(spec) + module.check_base(spec)
    payload["path"] = str(spec.path)
    payload["duty"] = spec.duty
    payload["stages"] = len(spec.stages)
    payload["issues"] = [_issue_dict(issue) for issue in issues]
    payload["errors"] = len(module.errors(issues))
    payload["warnings"] = len(issues) - payload["errors"]
    payload["ok"] = payload["errors"] == 0
    return payload


def render_spec_check(payload: dict[str, Any]) -> str:
    """`file:line: правило: текст` — либо `OK` (предупреждения печатаются и при OK)."""
    lines = [
        f"{issue['path']}:{issue['line']}: {issue['rule']}: {issue['message']}"
        for issue in payload.get("issues") or []
    ]
    if payload.get("ok"):
        head = f"OK {payload['path']} · дело {payload['duty']} · этапов {payload['stages']}"
        return "\n".join([head] + lines)
    if payload.get("unreadable"):
        return "\n".join(lines + [f"ОТКАЗ · спека не читается: {payload['path']}"])
    return "\n".join(lines + [f"ОТКАЗ · ошибок {payload['errors']}, предупреждений {payload['warnings']}"])


def spec_exit_code(payload: dict[str, Any]) -> int:
    """0 — спека годна, 1 — отказы правил, 2 — спека не читается вовсе."""
    if payload.get("unreadable"):
        return EXIT_SOURCE
    return EXIT_OK if payload.get("ok") else EXIT_NOT_FOUND


# ------------------------------------------------- приговор последней попытки


def stage_verdict(home: Path, deal_id: str, stage: str) -> dict[str, Any]:
    """Последняя попытка этапа: исход, класс отказа, причина и первые details.

    Читается ровно журнал (`dashboard/journal.py`): `attempt_started` даёт номер
    попытки и её начало, `attempt_ended` — исход, класс и вердикт evidence-гейта
    (`details.verdict`). Ничего не пересчитывается: «почему этап стоит» должно
    отвечаться тем же фактом, что видит движок.

    Нет попыток этапа в журнале — это ответ, а не ошибка: дело могло ещё не
    дойти до этапа, и `note` говорит, чего именно нет.
    """
    journal = _load_journal()
    payload: dict[str, Any] = {
        "stage": stage,
        "found": False,
        "attempt": None,
        "started_at": None,
        "ended_at": None,
        "outcome": None,
        "kind": None,
        "reason": "",
        "details": [],
        "workdir": None,
        "note": "",
    }
    home = Path(home)
    conn = journal.open_journal(home, create=False)
    if conn is None:
        payload["note"] = f"журнала ещё нет: {journal.journal_path(home)}"
        return payload
    try:
        events = journal.read_events(conn, str(deal_id))
    finally:
        conn.close()

    starts: dict[int, dict[str, Any]] = {}
    ends: dict[int, dict[str, Any]] = {}
    for event in events:
        item = journal.payload_of(event)
        if str(item.get("stage") or "") != stage:
            continue
        number = item.get("attempt")
        if isinstance(number, bool) or not isinstance(number, (int, str)):
            continue
        try:
            attempt = int(number)
        except (TypeError, ValueError):
            continue
        kind = str(event.get("kind") or "")
        if kind == "attempt_started":
            starts[attempt] = item
        elif kind == "attempt_ended":
            # `ts` берётся из журнала: время закрытия попытки — факт, а не часы CLI.
            ends[attempt] = {**item, "ts": event.get("ts")}

    if not starts and not ends:
        payload["note"] = f"в журнале нет попыток этапа {stage} у дела {deal_id}"
        return payload

    attempt = max(set(starts) | set(ends))
    started = starts.get(attempt) or {}
    ended = ends.get(attempt) or {}
    details = ended.get("details")
    verdict = details.get("verdict") if isinstance(details, dict) else None
    issues: list[str] = []
    if isinstance(verdict, dict) and isinstance(verdict.get("details"), list):
        issues = [str(item) for item in verdict["details"]]
    kind = ended.get("kind")
    payload.update(
        {
            "found": True,
            "attempt": attempt,
            "started_at": started.get("ts"),
            "ended_at": ended.get("ts"),
            "outcome": ended.get("outcome"),
            "kind": kind if isinstance(kind, str) and kind else None,
            "reason": str(ended.get("reason") or ""),
            "details": issues,
            "workdir": (details.get("workdir") if isinstance(details, dict) else None),
            "note": "" if ended else "попытка не закрыта: `attempt_ended` ещё нет",
        }
    )
    return payload


def render_stage_verdict(payload: dict[str, Any]) -> str:
    """Приговор последней попытки словами: попытка, исход, класс, первые details."""
    stage = payload.get("stage")
    lines = [f"последняя попытка этапа {stage}:"]
    if not payload.get("found"):
        lines.append(f"  {payload.get('note') or 'попыток не было'}")
        return "\n".join(lines)
    kind = payload.get("kind") or "—"
    lines.append(
        "  попытка #{} · старт {} · финиш {} · outcome={} · kind={}".format(
            payload.get("attempt"),
            _fmt_stamp(payload.get("started_at")),
            _fmt_stamp(payload.get("ended_at")),
            payload.get("outcome") or "—",
            kind,
        )
    )
    if payload.get("note"):
        lines.append(f"  {payload['note']}")
    if payload.get("workdir"):
        lines.append(f"  каталог попытки: {payload['workdir']}")
    if payload.get("reason"):
        lines.append(f"  причина: {payload['reason']}")
    details = payload.get("details") or []
    if details:
        lines.append(f"  отказы гейта ({len(details)}):")
        lines.extend(f"    - {item}" for item in details[:VERDICT_DETAILS])
    return "\n".join(lines)


def failure_log(home: Path, deal_id: str) -> dict[str, Any]:
    """Последние упавшие попытки дела: класс, счётчик по классу и `retry_at`.

    Читается только журнал: `attempt_ended` с `outcome=failed` — факт движка, класс
    взят из `payload.kind` (его пишет `dashboard/failures.py`), а не выведен из
    текста причины. Счётчик считается в порядке журнала по паре «этап + класс»:
    `3/2 infra` читается как «три отказа класса infra при лимите две повторные
    попытки». Лимит и пауза берутся из политики этапа (`failures.policy`), а
    спека — из `created.payload.spec`; нет спеки — политика по умолчанию, и это
    сказано в `note`, а не выдано за объявленное дело.

    `park` в записи — правило, по которому повтор невозможен
    (`retries_exhausted` / `too_many_failures`): тогда `retry_at` не считается.
    """
    journal = _load_journal()
    failures = _load_failures()
    payload: dict[str, Any] = {"found": False, "items": [], "parked": None, "note": ""}
    home = Path(home)
    conn = journal.open_journal(home, create=False)
    if conn is None:
        payload["note"] = f"журнала ещё нет: {journal.journal_path(home)}"
        return payload
    try:
        events = journal.read_events(conn, str(deal_id))
    finally:
        conn.close()

    stages, spec_note = _spec_stages(events)
    payload["note"] = spec_note
    counts: dict[tuple[str, str], int] = {}
    total = 0
    items: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("kind") or "") != "attempt_ended":
            continue
        item = journal.payload_of(event)
        if str(item.get("outcome") or "").strip().lower() != failures.OUTCOME_FAILED:
            continue
        stage = str(item.get("stage") or "")
        kind = str(item.get("kind") or "").strip() or failures.UNCLASSIFIED
        attempts_of_class = counts.get((stage, kind), 0) + 1
        counts[(stage, kind)] = attempts_of_class
        total += 1
        policy = failures.policy(stages.get(stage))
        rule = failures.decide_park(
            kind=kind, attempts=attempts_of_class, total=total, policy_=policy
        )
        at = int(event.get("ts") or 0)
        items.append(
            {
                "stage": stage,
                "attempt": item.get("attempt"),
                "kind": kind,
                "at": at,
                "attempts": attempts_of_class,
                "limit": failures.park_limit(rule, policy) if rule else policy.max,
                "counter": failures.counter(kind, attempts_of_class, policy.max),
                "retry_at": None if rule else failures.retry_at(attempts_of_class, at),
                "park": rule,
                "reason": str(item.get("reason") or ""),
            }
        )
    payload["found"] = bool(items)
    payload["items"] = items[-FAILURE_LOG_LIMIT:]
    payload["parked"] = journal.parked_of(events)
    if not items:
        payload["note"] = payload["note"] or f"в журнале нет упавших попыток у дела {deal_id}"
    return payload


def _spec_stages(events: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """Этапы спеки дела по журналу: (id → этап, замечание, если спеки нет).

    Путь берётся из `created.payload.spec`: журнал — единственная правда о том, по
    какому графу дело создавалось. Спека не названа или не читается — политика
    этапа неизвестна, и тогда работает умолчание (`failures.policy`), о чём сказано
    в замечании.
    """
    journal = _load_journal()
    path = ""
    for event in events:
        if str(event.get("kind") or "") != "created":
            continue
        raw = journal.payload_of(event).get("spec")
        if isinstance(raw, str) and raw.strip():
            path = raw.strip()
            break
    if not path:
        return {}, "спеки в журнале нет: лимиты взяты по умолчанию (max=1, on=[infra])"
    try:
        spec = _load_spec().load_spec(Path(path))
    except Exception as exc:  # noqa: BLE001 - нечитаемая спека не должна валить why
        return {}, f"спека не читается ({path}): {type(exc).__name__} — лимиты по умолчанию"
    return {stage.id: stage for stage in spec.stages}, ""


def render_failure_log(payload: dict[str, Any]) -> str:
    """Факты отказов словами: этап, класс, счётчик и когда разрешён повтор."""
    lines = ["факты отказов (последние {}):".format(FAILURE_LOG_LIMIT)]
    if not payload.get("found"):
        lines.append(f"  {payload.get('note') or 'упавших попыток нет'}")
    for item in payload.get("items") or []:
        retry = (
            f"retry_at {_fmt_stamp(item['retry_at'])}"
            if item.get("retry_at")
            else f"повтора нет ({item.get('park') or 'класс не повторяется'})"
        )
        lines.append(
            "  {}#{} · {} · счётчик {} · {}".format(
                item.get("stage") or "—",
                item.get("attempt") if item.get("attempt") is not None else "—",
                item.get("kind"),
                item.get("counter"),
                retry,
            )
        )
    parked = payload.get("parked")
    if isinstance(parked, dict):
        lines.append(
            "  парковка: {} на этапе {} — {} (вопрос #{})".format(
                f"{parked.get('attempts')}/{parked.get('limit')} {parked.get('kind')}"
                if parked.get("kind")
                else "попытки исчерпаны",
                parked.get("stage") or "—",
                parked.get("reason") or "решение человека",
                parked.get("question_id") or "—",
            )
        )
    note = payload.get("note")
    if note and payload.get("found"):
        lines.append(f"  {note}")
    return "\n".join(lines)


# ------------------------------------------------------------------------ main


def render_run_step(row: dict[str, Any]) -> str:
    """Строка шага прогона: `impl · попытка 1 · ok (12.4s)` или `verify · попытка 1 · contract: …`.

    Формат — ровно то, что человек видит в терминале по ходу прогона: этап,
    номер попытки и исход. Отказ называется классом и причиной (`contract: нет
    evidence по go build`), а не словом «ошибка»: разбирать его — не работа
    терминала, он показывает факт из события.
    """
    stage = str(row.get("stage") or "—")
    attempt = row.get("attempt") or 0
    if not row.get("started"):
        return f"{stage} · попытка {attempt} · не поднята: {row.get('reason') or 'причина не записана'}"
    outcome = str(row.get("outcome") or "")
    kind = row.get("kind")
    if outcome == _load_failures().OUTCOME_DONE:
        duration = row.get("duration_sec")
        took = f" ({float(duration):.1f}s)" if isinstance(duration, (int, float)) else ""
        return f"{stage} · попытка {attempt} · ok{took}"
    detail = f"{kind}: {row.get('reason')}" if kind else str(row.get("reason") or outcome or "исход не записан")
    return f"{stage} · попытка {attempt} · {detail}"


def _decision_text(decision: dict[str, Any]) -> str:
    """Решение `transit` словами CLI: `run(verify#1)`, `wait(question)`, `done`."""
    kind = str(decision.get("kind") or "—")
    if kind == "run":
        pairs = ", ".join(
            f"{stage}#{attempt}"
            for stage, attempt in zip(decision.get("stages") or [], decision.get("attempts") or [])
        )
        return f"run({pairs})"
    if kind == "wait":
        text = f"wait({decision.get('what') or '—'})"
        if decision.get("retry_at"):
            text += f" до {decision['retry_at']}"
        return text
    if kind == "failed":
        return f"failed({decision.get('fail_kind') or '—'})"
    if kind == "park":
        stages = decision.get("stages") or []
        return f"park({stages[0] if stages else '—'})"
    return kind


def render_run_final(report: dict[str, Any]) -> str:
    """Итог прогона одной строкой: что сделано и чего ждём.

    Ожидание называется тем, чего не хватает (`wait(question)` плюс номер
    вопроса в причине, `wait(retry)` плюс `retry_at`, `wait(gate)` плюс кто
    держит): «вышел с кодом 3» без этих слов человеку ничего не говорит.
    """
    outcome = str(report.get("outcome") or "")
    decision = report.get("decision") or {}
    steps = report.get("steps") or []
    made = len({row.get("n") for row in steps})
    note = str(report.get("note") or decision.get("reason") or "")
    if outcome == "done":
        return f"итог: дело закрыто — {note or 'все этапы закрыты'} (шагов: {made})"
    if outcome == "failed":
        return f"итог: дело остановлено отказом {decision.get('fail_kind') or '—'} — {note} (шагов: {made})"
    if outcome == "park":
        return (
            f"итог: дело припарковано, решает человек — вопрос #{decision.get('question_id') or '—'}, "
            f"{note} (шагов: {made})"
        )
    if outcome == "wait":
        return f"итог: ждём {_decision_text(decision)} — {note}"
    if outcome == "limit":
        return f"итог: предел шагов — {note}"
    if outcome == "interrupted":
        return f"итог: прервано — {note} (шагов: {made})"
    if outcome == "step":
        return f"итог: сделан один шаг (--once) — дальше {_decision_text(decision)}"
    return f"итог: {outcome or '—'} — {note}"


def _emit_json(payload: dict[str, Any], stream: Any) -> None:
    """`--json` — ответ роута дословно, без добавленных ключей."""
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=stream)


def main(argv: Optional[list[str]] = None) -> int:
    """Точка входа CLI: вернуть код 0/1/2, ничего не печатая в stderr без причины."""
    _ensure_interpreter(argv)
    parser = build_parser()
    words = _positionals_first(list(sys.argv[1:] if argv is None else argv), ("block", "changes"))
    args = parser.parse_args(words)
    if not args.command:
        parser.print_help()
        return EXIT_OK
    out = sys.stdout
    try:
        if args.command == "watch":
            return run_watch(
                args.interval, board=args.board, state=args.state, as_json=args.json, stream=out
            )
        if args.command == "run":
            # Прогон дела: цикл живёт в `dashboard/driver.py`, здесь — argv,
            # построчный вывод шагов и итог. Код возврата — из отчёта драйвера
            # (`0/1/2/3/4/130`), а не выдуманный терминалом.
            driver = _load_driver()
            report = _route(
                driver.drive,
                api().hermes_home(),
                args.deal_id,
                spec_path=args.spec,
                max_steps=driver.DEFAULT_MAX_STEPS if args.max_steps is None else args.max_steps,
                once=args.once,
                on_step=None if args.json else (lambda row: print(render_run_step(row), file=out)),
            )
            _emit_json(report, out) if args.json else print(render_run_final(report), file=out)
            return int(report.get("exit_code") or EXIT_OK)
        if args.command == "demo":
            report = _load_demo().run_demo(
                api(), home=Path(args.home) if args.home else None, keep=args.keep,
                echo=None if args.json else (lambda line: print(line, file=out)),
            )
            _emit_json(report, out) if args.json else print(_load_demo().render_demo(report), file=out)
            return EXIT_OK
        if args.command == "now":
            payload = _route(api().now, board=args.board, state=args.state)
            _emit_json(payload, out) if args.json else print(render_now(payload), file=out)
            return _payload_code(payload)
        if args.command == "stats":
            payload = _route(api().stats, board=args.board, since=args.since)
            _emit_json(payload, out) if args.json else print(render_stats(payload), file=out)
            return _payload_code(payload)
        if args.command == "spec":
            if getattr(args, "spec_command", None) != "check":
                print("! нужна подкоманда: duty spec check <file>", file=sys.stderr)
                return EXIT_SOURCE
            payload = spec_report(args.file)
            _emit_json(payload, out) if args.json else print(render_spec_check(payload), file=out)
            return spec_exit_code(payload)
        if args.command == "attempts":
            payload = _route(api().duty_attempts, args.board, args.task_id)
            _emit_json(payload, out) if args.json else print(render_attempts(payload), file=out)
            return _payload_code(payload)
        if args.command == "why":
            payload = _route(api().deal, args.board, args.task_id)
            # Факты отказов и приговор последней попытки берутся из журнала плагина:
            # то же, что видит движок, плюс к форме роута — отдельными ключами
            # (`failures`, `verdict`), чтобы `--json` оставался дословным ответом
            # роута с добавленными фактами, а не второй формой.
            payload = {**payload, "failures": failure_log(api().hermes_home(), args.task_id)}
            if args.stage:
                payload = {**payload, "verdict": stage_verdict(api().hermes_home(), args.task_id, args.stage)}
            _emit_json(payload, out) if args.json else print(render_why(payload), file=out)
            return _payload_code(payload)
        if args.command == "deal":
            payload = _route(api().deal, args.board, args.task_id)
            if args.json:
                # Форма роута `/deal` дословно: таймлайн — отдельный роут
                # `/duty/.../attempts` и своя форма (`duty attempts --json`).
                _emit_json(payload, out)
                return _payload_code(payload)
            timeline: Optional[dict[str, Any]] = None
            timeline_error: Optional[str] = None
            try:
                timeline = _route(api().duty_attempts, args.board, args.task_id)
            except SourceFailure as exc:
                timeline_error = str(exc)
            print(render_deal(payload, timeline, timeline_error), file=out)
            return EXIT_SOURCE if timeline_error else _payload_code(payload)
        if args.command == "create":
            body = ""
            if args.body_file:
                try:
                    body = Path(args.body_file).expanduser().read_text(encoding="utf-8")
                except OSError as exc:
                    print(f"! не читается {args.body_file}: {exc}", file=sys.stderr)
                    return EXIT_SOURCE
            request: dict[str, Any] = {
                "board": args.board,
                "title": args.title,
                "body": body,
                "parents": list(args.parent),
            }
            if args.assignee:
                request["assignee"] = args.assignee
            if args.external_ref:
                request["external_ref"] = args.external_ref
            if args.spec:
                # Путь разворачивается здесь, как у `duty spec check`: store
                # принимает только абсолютный, иначе «откуда взялась спека» не
                # восстановить из журнала (`docs/SPEC.md`).
                request["spec"] = str(Path(args.spec).expanduser().resolve())
            payload = _route(api().deals_create, api().hermes_home(), request, source="cli")
            if args.json:
                # К форме роута добавлен `deal_id` — имя, которым дело называют
                # скрипты (`docs/OWNERSHIP.md`, CLI): у роута это поле зовётся
                # `id`, и переименовывать его там ради терминала нельзя. Больше
                # добавленных ключей нет: `created`/`existing`/`status` уже в
                # ответе роута.
                _emit_json({**payload, "deal_id": payload.get("id")}, out)
            else:
                action = "СОЗДАНО" if payload.get("created") else "ДЕЛО УЖЕ ЕСТЬ"
                print(render_mutation(payload, action), file=out)
            return _payload_code(payload)
        if args.command == "comment":
            request = {"author": args.author, "body": args.body}
            return _mutate(
                api().deals_comment, "КОММЕНТАРИЙ", api().hermes_home(), args.deal_id, request,
                source="cli", as_json=args.json,
            )
        if args.command == "block":
            request = {"kind": args.kind, "reason": args.reason}
            return _mutate(
                api().deals_block, "ЗАБЛОКИРОВАНО", api().hermes_home(), args.deal_id, request,
                source="cli", as_json=args.json,
            )
        if args.command == "unblock":
            return _mutate(
                api().deals_unblock, "РАЗБЛОКИРОВАНО", api().hermes_home(), args.deal_id, {},
                source="cli", as_json=args.json,
            )
        if args.command == "claim":
            request = {"owner": _claim_owner(args.owner)}
            return _mutate(
                api().deals_claim, "ВЗЯТО В РАБОТУ", api().hermes_home(), args.deal_id, request,
                source="cli", as_json=args.json,
            )
        if args.command == "complete":
            request = {"summary": args.summary, "review": bool(args.review), "reviewer": args.reviewer}
            return _mutate(
                api().deals_complete, "ЗАВЕРШЕНО", api().hermes_home(), args.deal_id, request,
                source="cli", as_json=args.json,
            )
        if args.command == "review":
            request = {"reviewer": args.reviewer, "summary": args.summary}
            return _mutate(
                api().deals_review, "СДАНО В РЕВЬЮ", api().hermes_home(), args.deal_id, request,
                source="cli", as_json=args.json,
            )
        if args.command == "changes":
            request = {"reason": args.reason, "reviewer": args.reviewer}
            return _mutate(
                api().deals_changes, "ВОЗВРАЩЕНО НА ДОРАБОТКУ", api().hermes_home(), args.deal_id, request,
                source="cli", as_json=args.json,
            )
        if args.command == "attach":
            try:
                content = Path(args.file).expanduser().read_bytes()
            except OSError as exc:
                print(f"! не читается {args.file}: {exc}", file=sys.stderr)
                return EXIT_SOURCE
            return _mutate(
                api().deals_attach, "ВЛОЖЕНИЕ", api().hermes_home(), args.deal_id,
                filename=Path(args.file).name, content=content, source="cli", as_json=args.json,
            )
        if args.command == "archive":
            return _mutate(
                api().deals_patch, "АРХИВИРОВАНО", api().hermes_home(), args.deal_id,
                {"status": "archived"}, source="cli", as_json=args.json,
            )
    except NotFound as exc:
        print(f"! {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except SourceFailure as exc:
        print(f"! {exc}", file=sys.stderr)
        return EXIT_SOURCE
    return EXIT_SOURCE  # pragma: no cover - неизвестная команда сюда не доходит


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
