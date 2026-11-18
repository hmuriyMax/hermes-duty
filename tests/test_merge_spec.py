"""Этап-слияние параллельных полос (0.2.2): вход плана, топология и его база.

Проверяется контракт версии на том, что видно **до запуска** и в первой точке
после него — выборе базы зависимого этапа. Механика слияния (merge-коммит,
резолвер, отказы `merge.conflict`/`merge.dropped`) живёт в `tests/test_merge_runner.py`:
здесь её нет, потому что спеке нечего сказать про то, как движок вливает полосы.

1. `kind: merge` — вид этапа, который план вправе добавить: роль резолвера
   (умолчание `implementer`), своё дерево (`workspace: worktree`), не меньше двух
   полос; поля других видов (`cmd`, `ask`, `verdict`) отказывают, а не игнорируются;
2. `plan.parallel.workspace` — производитель в параллельной полосе не работает в
   общем каталоге дела (`dir`): две полосы правили бы один файл без ветки и без
   слияния (живое `d_c13d53da`). Проверяющие этапы правилом не ограничены, а уже
   принятый граф им не пересуживается;
3. `plan.parallel.fanin` — параллельные полосы без сходящегося `merge`
   предупреждают, а не молчат (ровно сегодняшний дефект: `parallel_with` разрешён,
   fan-in мёртв);
4. порядок слияния — порядок `needs` этапа (объявление в плане, не время): повтор
   того же плана даёт тот же порядок;
5. база зависимого этапа после слияния — merge-коммит (родство принимается
   `git merge-base --is-ancestor`), а прежний отказ «предшественники разошлись»
   остаётся для графа без `merge`.

Часы не читаются: всё решается формой графа и фактами, которые передаются явно.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Профили — снимок каталога, а не машина прогона: тест не должен зависеть от того,
# кого завели в Hermes. Те же имена, что в `tests/test_spec.py`.
PROFILES = frozenset(
    {"default", "implementer", "tester", "reviewer", "publisher", "documenter", "researcher", "planner", "lead"}
)


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути — под тем же именем, что и у остального кода.

    Имя важнее, чем кажется: `runner` грузит `spec` через `sys.modules`, и вторая
    копия модуля дала бы тесту другой класс `Stage` — сравнение структур разошлось
    бы на `isinstance` там, где спека и движок говорят об одном типе.
    """
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec_module = load_module("hermes_duty_spec", "spec.py")
runner = load_module("hermes_duty_runner", "runner.py")

Stage = spec_module.Stage

# Граф, в который план добавляет: вход дела и две полосы-предшественника, на
# которые слияние и ссылается.
GRAPH = {
    "plan": Stage(id="plan", role="coordinator", workspace="none"),
}


def plan(raw: list[dict[str, Any]], *, charter: Any = None, base_stages: Any = None) -> tuple:
    """`parse_plan` по хартии по умолчанию: база названа, но git не спрашивается.

    `base`/`repo` передаются, потому что у worktree-этапа они предусловие
    (`plan.base`), а проверяется здесь не база, а полосы: `exists` отвечает «есть».
    """
    return spec_module.parse_plan(
        raw,
        charter=charter or spec_module.Charter(),
        base_stages=base_stages if base_stages is not None else GRAPH,
        repo=str(ROOT),
        base="HEAD",
        exists=lambda *_args: True,
    )


def rules(found: list) -> set[str]:
    return {issue.rule for issue in found}


def load_spec(tmp_path: Path, text: str) -> Any:
    """Спека на диске: путь абсолютный, как требует загрузчик."""
    path = tmp_path / "duty.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return spec_module.load_spec(path)


def lanes() -> list[dict[str, Any]]:
    """Две полосы из общей базы: каждая в своём дереве, объявлены друг другу.

    `evidence` у полос объявлен, потому что план добавляет производителей — без
    него отказывает `plan.evidence` (`tests/test_spec.py`), и отказ был бы про
    доказательства, а не про полосы.
    """
    return [
        {
            "id": "impl_a",
            "role": "implementer",
            "workspace": "worktree",
            "parallel_with": ["impl_b"],
            "evidence": [{"cmd": "ls a", "must": 0}],
        },
        {
            "id": "impl_b",
            "role": "implementer",
            "workspace": "worktree",
            "parallel_with": ["impl_a"],
            "evidence": [{"cmd": "ls b", "must": 0}],
        },
    ]


def merge_stage(**extra: Any) -> dict[str, Any]:
    """Этап слияния по умолчанию: две полосы, своё дерево, роль не названа."""
    raw: dict[str, Any] = {"id": "join", "kind": "merge", "needs": ["impl_a", "impl_b"]}
    raw.update(extra)
    return raw


def graph_spec(stages: dict[str, Any]) -> Any:
    """Граф в форме спеки для `gate_warnings`: этапы, путь, пустой чартер.

    `gate_warnings` принимает `Spec` — её читают и `spec check`, и `transit.graph_spec`,
    поэтому тест собирает ту же форму, а не `SimpleNamespace`: проверка ревью-гейта
    спрашивает `spec.ids`, и подделка разошлась бы с настоящим входом.
    """
    return spec_module.Spec(
        path=Path("<graph>"),
        duty="graph",
        repo=None,
        base=None,
        stages=tuple(stages.values()),
    )


# ------------------------------------------- 1. kind: merge разбирается и судится


def test_a_merge_stage_gets_the_resolver_role_and_its_own_tree_by_default() -> None:
    """`kind: merge` без роли и без дерева получает умолчания: резолвер и worktree.

    Умолчание роли — часть контракта, а не удобство: движку нужно кого-то поднять
    на конфликт, и лид, объявивший слияние, не обязан помнить имя профиля. Дерево
    умолчанием даётся по тому же правилу, что любому этапу с предшественником, —
    но у слияния это не привычка, а условие: merge-коммит пишется в дерево этапа.
    """
    stages, found = plan(lanes() + [merge_stage()])
    assert found == [], [issue.message for issue in found]
    join = next(stage for stage in stages if stage.id == "join")
    assert join.kind == "merge"
    assert join.role == spec_module.MERGE_RESOLVER_ROLE == "implementer"
    assert join.workspace == "worktree"
    assert join.workspace_default is True, "дерево получено по умолчанию, а не объявлено"
    assert join.needs == ("impl_a", "impl_b"), "полосы сохранили порядок объявления"


def test_the_declared_resolver_role_is_kept() -> None:
    """Названная роль резолвера не подменяется умолчанием."""
    stages, found = plan(lanes() + [merge_stage(role="implementer", workspace="worktree")])
    assert found == [], [issue.message for issue in found]
    assert next(stage for stage in stages if stage.id == "join").role == "implementer"


def test_a_merge_stage_cannot_be_a_lane() -> None:
    """`parallel_with` у слияния — отказ `merge.parallel`, а не полоса слияния.

    Merge-коммит — работа всего дела, и закрепляется он веткой дела
    (`runner.anchor_commit` без `lane`). Объявленное `parallel_with` сделало бы
    его полосой: коммит лёг бы в `duty/<дело>-<этап>`, и зависимые этапы ждали бы
    работу, которой никто не публикует.
    """
    stages, found = plan(lanes() + [merge_stage(workspace="worktree", parallel_with=["impl_a"])])
    assert "merge.parallel" in rules(found), [issue.message for issue in found]
    assert next(stage for stage in stages if stage.id == "join").needs == ("impl_a", "impl_b")


def test_the_merge_stage_is_never_asked_for_a_lane() -> None:
    """`runner.lane_of` у этапа слияния пуст: ветка слияния — ветка дела.

    Второй рубеж к `merge.parallel`: полоса спрашивается у каждого этапа перед
    закреплением коммита, и ответ «join — полоса» увёл бы merge-коммит с ветки
    дела даже там, где вход плана объявление пропустил.
    """
    merge = Stage(id="join", role="implementer", kind="merge", needs=("impl_a", "impl_b"),
                  workspace="worktree", parallel_with=("impl_a",))
    lane = Stage(id="impl_a", role="implementer", workspace="worktree", parallel_with=("impl_b",))
    partner = Stage(id="impl_b", role="implementer", workspace="worktree", parallel_with=("impl_a",))
    spec = SimpleNamespace(stages=(merge, lane, partner))
    assert runner.lane_of(spec, merge) == ""
    assert runner.lane_of(spec, lane) == "impl_a"


@pytest.mark.parametrize(
    ("case", "extra", "rule"),
    [
        ("one lane", {"workspace": "worktree", "needs": ["impl_a"]}, "merge.needs"),
        ("no lanes", {"workspace": "worktree", "needs": []}, "merge.needs"),
        ("shared directory", {"workspace": "dir"}, "merge.workspace"),
        ("no tree", {"workspace": "none"}, "merge.workspace"),
        ("report role", {"workspace": "worktree", "role": "reviewer"}, "merge.role"),
        ("coordinator role", {"workspace": "worktree", "role": "coordinator"}, "merge.role"),
        ("publisher role", {"workspace": "worktree", "role": "publisher"}, "merge.role"),
        ("command field", {"workspace": "worktree", "cmd": "git merge impl_b"}, "kind.cmd"),
        (
            "question field",
            {"workspace": "worktree", "ask": {"q": "Сливаем?", "options": ["да", "нет"]}},
            "kind.ask",
        ),
        (
            "verdict field",
            {"workspace": "worktree", "verdict": {"approve": "next", "changes": "join", "max_rounds": 1}},
            "kind.verdict",
        ),
    ],
)
def test_a_merge_stage_refuses_what_it_cannot_do(case: str, extra: dict[str, Any], rule: str) -> None:
    """Чужие поля и неполный fan-in — отказ на входе плана, а не тихий игнор.

    Слияние одной полосы с собой, дерево в общем каталоге, роль без терминала,
    команда, вопрос человеку и приговор — это не «почти слияние»: объявленное и
    не прочитанное было бы ложью спеки, а отказ называет правило.
    """
    _, found = plan(lanes() + [merge_stage(**extra)])
    assert rule in rules(found), (case, [issue.message for issue in found])


def test_a_merge_stage_is_allowed_by_a_charter_that_names_the_three_old_kinds() -> None:
    """Хартия живых дел перечисляет виды явно — слияние всё равно добавляется.

    Шаблон чартера держит `allowed_kinds: [agent, shell, gate]`, а правку хартии
    после старта дела никто не делает: запретить fan-in этим списком значило бы
    запретить его ровно там, где параллель уже работает (`d_c13d53da`). Слияние не
    добавляет работы — оно собирает объявленные полосы в один коммит.
    """
    charter = spec_module.Charter(
        max_stages=24, allowed_roles=("implementer",), allowed_kinds=("agent", "shell", "gate"), declared=True
    )
    stages, found = plan(lanes() + [merge_stage()], charter=charter)
    assert found == [], [issue.message for issue in found]
    assert "join" in {stage.id for stage in stages}


def test_the_seed_spec_carries_a_merge_stage_through_validation(tmp_path: Path) -> None:
    """Seed-спека с `kind: merge` читается и проходит `validate_spec` без отказов.

    Слияние — вид этапа, а не режим плана: файл вправе объявить его сам, и тогда
    правила `validate_spec` (роль существует, дерево есть, полос не меньше двух)
    судят его тем же входом, что и этап плана.
    """
    spec = load_spec(
        tmp_path,
        """
        duty: полосы
        repo: {repo}
        base: main
        stages:
          - id: plan
            role: coordinator
            workspace: none
          - id: impl_a
            role: implementer
            workspace: worktree
            needs: [plan]
            parallel_with: [impl_b]
          - id: impl_b
            role: implementer
            workspace: worktree
            needs: [plan]
            parallel_with: [impl_a]
          - id: join
            kind: merge
            needs: [impl_a, impl_b]
        """.format(repo=tmp_path),
    )
    found = spec_module.validate_spec(spec, profiles=PROFILES)
    assert spec_module.errors(found) == [], [issue.message for issue in found]
    join = spec.stage("join")
    assert join is not None
    assert join.role == spec_module.MERGE_RESOLVER_ROLE


# ------------------------------- 2. параллельная полоса не работает в общем каталоге


def test_a_producer_in_a_shared_directory_refuses_the_lane() -> None:
    """Производитель в полосе на `dir` — отказ с именами полос.

    Общий каталог дела значит, что две полосы правят один файл без ветки и без
    слияния: работа одной стороны молча затирает другую. Отказ называет обе
    полосы — лид иначе ищет нарушителя в графе на десяток этапов.
    """
    proposal = [
        {"id": "impl_a", "role": "implementer", "workspace": "worktree", "parallel_with": ["impl_b"]},
        {"id": "impl_b", "role": "implementer", "workspace": "dir", "parallel_with": ["impl_a"]},
    ]
    _, found = plan(proposal)
    hits = [issue for issue in found if issue.rule == "plan.parallel.workspace"]
    assert len(hits) == 1, [issue.message for issue in found]
    assert "'impl_a'" in hits[0].message and "'impl_b'" in hits[0].message
    assert "workspace: dir" in hits[0].message


@pytest.mark.parametrize(
    ("case", "producer"),
    [
        ("reviewer", {"id": "check", "role": "reviewer", "workspace": "dir"}),
        ("tester", {"id": "check", "role": "tester", "workspace": "dir"}),
        ("researcher", {"id": "check", "role": "researcher", "workspace": "dir"}),
        # Команда с доказательством — проверка, а не работа (`spec._checks_work`),
        # и `plan.merge.gates` прямо называет такой этап гейтом за слиянием.
        (
            "shell",
            {
                "id": "check",
                "kind": "shell",
                "cmd": "go test ./...",
                "workspace": "dir",
                "evidence": [{"cmd": "go test ./...", "must": 0}],
            },
        ),
    ],
)
def test_a_checking_stage_in_a_lane_keeps_its_shared_directory(case: str, producer: dict[str, Any]) -> None:
    """Проверяющий этап с `parallel_with` на общем дереве остаётся законным.

    Его свидетель — отчёт, а не `commit|artifacts`: он ничего не правит, и общий
    каталог ему не мешает. Правило полос говорит о тех, кто производит работу.
    Тот же признак читает `plan.merge.gates` (`spec._checks_work`), поэтому shell-
    гейт, который правило слияния советует поставить, правилом полос не ловится.
    """
    proposal = [
        {"id": "impl_a", "role": "implementer", "workspace": "worktree"},
        dict(producer, parallel_with=["impl_a"]),
    ]
    _, found = plan(proposal)
    assert "plan.parallel.workspace" not in rules(found), (case, [issue.message for issue in found])


def test_a_shell_stage_with_artifacts_still_needs_its_own_branch() -> None:
    """Объявленные `artifacts` — уже работа: полоса остаётся обязанной веткой.

    Граница проходит по работе, а не по виду этапа: команда, которая называет
    файлы артефактами, делит их с соседом в общем каталоге, и правило полос
    возвращается к ней целиком.
    """
    proposal = [
        {"id": "impl_a", "role": "implementer", "workspace": "worktree"},
        {
            "id": "pack",
            "kind": "shell",
            "cmd": "make dist",
            "workspace": "dir",
            "parallel_with": ["impl_a"],
            "evidence": [{"cmd": "make dist", "must": 0}],
            "artifacts": ["dist/app.tar.gz"],
        },
    ]
    _, found = plan(proposal)
    hits = [issue for issue in found if issue.rule == "plan.parallel.workspace"]
    assert hits, [issue.message for issue in found]
    assert "'pack'" in hits[0].message


def test_a_producer_without_a_tree_is_not_a_shared_directory() -> None:
    """`workspace: none` у производителя отказом не считается: общего файла нет.

    У такого этапа нет ни дерева, ни коммита — он не делит файл с соседом, а
    «производит, а коммита нет» ему выносит гейт пакета (`evidence.verify`), а не
    вход плана. Иначе правило запрещало бы чтение рядом с работой.
    """
    proposal = [
        {"id": "impl_a", "role": "implementer", "workspace": "worktree", "parallel_with": ["impl_b"]},
        {"id": "impl_b", "role": "implementer", "workspace": "none", "parallel_with": ["impl_a"]},
    ]
    _, found = plan(proposal)
    assert "plan.parallel.workspace" not in rules(found), [issue.message for issue in found]


def test_an_accepted_graph_is_not_re_judged_by_a_later_plan() -> None:
    """Уже принятые полосы в общем каталоге не пересуживаются новым планом.

    Журнал не переписываем: дело, принявшее такие полосы до 0.2.2, читается как
    раньше (`d_c13d53da` и родня). Правило смотрит только на пару, в которой есть
    сторона из ЭТОГО предложения, и `replay` его не спрашивает вовсе.
    """
    accepted = {
        "plan": Stage(id="plan", role="coordinator", workspace="none"),
        "impl_a": Stage(id="impl_a", role="implementer", workspace="dir", parallel_with=("impl_b",)),
        "impl_b": Stage(id="impl_b", role="implementer", workspace="dir", parallel_with=("impl_a",)),
    }
    _, found = plan([{"id": "impl_c", "role": "implementer", "workspace": "worktree", "needs": ["impl_a"]}], base_stages=accepted)
    assert "plan.parallel.workspace" not in rules(found), [issue.message for issue in found]

    _, replay_found = spec_module.parse_plan(
        [{"id": "impl_a", "workspace": "dir", "parallel_with": ["impl_b"]}],
        charter=spec_module.Charter(),
        base_stages=accepted,
        repo=str(ROOT),
        base="HEAD",
        replay=True,
    )
    assert "plan.parallel.workspace" not in rules(replay_found), [issue.message for issue in replay_found]


def test_the_seed_spec_refuses_a_producer_in_a_shared_directory(tmp_path: Path) -> None:
    """Seed-файл с полосой в общем каталоге — отказ `spec check`, а не предупреждение.

    Этапы файла объявляет человек, и ошибка та же: две полосы правят один файл без
    ветки. Отказ приходит на входе (`duty spec check`), а не через два этапа.
    """
    spec = load_spec(
        tmp_path,
        """
        duty: полосы
        repo: {repo}
        base: main
        stages:
          - id: impl_a
            role: implementer
            workspace: dir
            parallel_with: [impl_b]
          - id: impl_b
            role: implementer
            workspace: dir
            parallel_with: [impl_a]
        """.format(repo=tmp_path),
    )
    rules_found = rules(spec_module.validate_spec(spec, profiles=PROFILES))
    assert "plan.parallel.workspace" in rules_found


# ------------------------------------------------ 3. полосы без fan-in предупреждают


def test_lanes_without_a_merge_warn_that_the_fan_in_is_missing(tmp_path: Path) -> None:
    """Две полосы без сходящегося `merge`: `spec check` предупреждает, а не молчит.

    Ровно сегодняшний дефект: `parallel_with` разрешён, полосы разошлись, и
    зависимый этап отказывается строиться «предшественники разошлись». Дело при
    таком графе запускается (форма графа — подсказка), но предупреждение называет
    обе полосы и ход, которым его закрывают.
    """
    spec = load_spec(
        tmp_path,
        """
        duty: полосы
        repo: {repo}
        base: main
        stages:
          - id: plan
            role: coordinator
            workspace: none
          - id: impl_a
            role: implementer
            workspace: worktree
            needs: [plan]
            parallel_with: [impl_b]
          - id: impl_b
            role: implementer
            workspace: worktree
            needs: [plan]
            parallel_with: [impl_a]
          - id: check
            role: tester
            workspace: none
            needs: [impl_a, impl_b]
        """.format(repo=tmp_path),
    )
    found = spec_module.validate_spec(spec, profiles=PROFILES)
    assert spec_module.errors(found) == [], [issue.message for issue in found]
    warnings = [issue for issue in found if issue.rule == "plan.parallel.fanin"]
    assert len(warnings) == 1, [issue.message for issue in found]
    assert warnings[0].severity == spec_module.WARNING
    assert "'impl_a'" in warnings[0].message and "'impl_b'" in warnings[0].message


@pytest.mark.parametrize("case", ["direct", "through the graph"])
def test_a_merge_that_takes_both_lanes_silences_the_warning(case: str) -> None:
    """Слияние, сходящееся на обе полосы, снимает предупреждение — и напрямую, и через цепочку.

    «Сходится» — значит полоса в транзитивных `needs` этапа слияния: слияние
    собирает то, что объявлено его предшественниками. Прямой случай — `needs:
    [impl_a, impl_b]`, через цепочку — `join` за промежуточным этапом, которому
    полосы нужны.
    """
    graph = {
        "plan": Stage(id="plan", role="coordinator", workspace="none"),
        "impl_a": Stage(id="impl_a", role="implementer", workspace="worktree", parallel_with=("impl_b",)),
        "impl_b": Stage(id="impl_b", role="implementer", workspace="worktree", parallel_with=("impl_a",)),
    }
    if case == "direct":
        graph["join"] = Stage(
            id="join", role="implementer", kind="merge", needs=("impl_a", "impl_b"), workspace="worktree"
        )
    else:
        graph["pair"] = Stage(id="pair", role="tester", workspace="none", needs=("impl_a", "impl_b"))
        graph["join"] = Stage(id="join", role="implementer", kind="merge", needs=("pair",), workspace="worktree")
    found = spec_module.gate_warnings(graph_spec(graph))
    assert "plan.parallel.fanin" not in rules(found), [issue.message for issue in found]


def test_the_plan_validator_warns_while_accepting_the_graph() -> None:
    """Граф без fan-in принимает план и получает предупреждение тем же входом.

    Предупреждение живёт в `gate_warnings` — одной двери для `spec check` и
    `duty_plan`: отказом оно не становится (лид вправе собрать граф по частям), но
    и потеряться не может — валидатор плана возвращает его в `warnings`.
    """
    stages, found = plan(lanes())
    assert found == [], [issue.message for issue in found]
    graph = dict(GRAPH)
    graph.update({stage.id: stage for stage in stages})
    warnings = spec_module.gate_warnings(graph_spec(graph))
    assert "plan.parallel.fanin" in rules(warnings)


# ------------------------------------------- 4. порядок слияния — порядок объявления


def test_the_merge_order_is_the_declared_one_and_repeats() -> None:
    """Тот же план дважды даёт тот же порядок слияния — порядок `needs`, не время.

    Детерминизм здесь не эстетика: результат повторного прогона не должен зависеть
    от того, чей коммит легла раньше, иначе один и тот же план даёт разные
    merge-коммиты. Порядок берётся из объявления (`needs`), а не сортируется по
    алфавиту и не измеряется часами.
    """
    raw = lanes() + [merge_stage(needs=["impl_b", "impl_a"], workspace="worktree")]
    first, first_issues = plan(list(raw))
    second, second_issues = plan(list(raw))
    assert first_issues == [] and second_issues == []
    assert first == second
    join = next(stage for stage in first if stage.id == "join")
    assert join.needs == ("impl_b", "impl_a"), "объявленный порядок сохранён"


# ---------------------------------------- 5. база зависимого этапа после слияния


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def lanes_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Репозиторий с двумя разошедшимися полосами и merge-коммитом из них.

    Возвращает (репозиторий, sha полосы a, sha полосы b, sha слияния) — ровно то,
    что движок видит после fan-in: два коммита из общей базы и их общий потомок.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    for branch, text in (("lane-a", "from a\n"), ("lane-b", "from b\n")):
        git(repo, "checkout", "-q", "-b", branch, base)
        (repo / f"{branch}.txt").write_text(text, encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", f"work of {branch}")
    sha_a = git(repo, "rev-parse", "lane-a")
    sha_b = git(repo, "rev-parse", "lane-b")
    git(repo, "checkout", "-q", "lane-a")
    git(repo, "merge", "--no-ff", "-m", "join the lanes", "lane-b")
    merge = git(repo, "rev-parse", "HEAD")
    assert git(repo, "rev-list", "--parents", "-n", "1", merge).count(" ") == 2, "это merge-коммит"
    return repo, sha_a, sha_b, merge


def ended(stage: str, commit: str, ts: int) -> dict[str, Any]:
    """Факт успешной попытки этапа: движок читает из него произведённый коммит."""
    return {
        "kind": "attempt_ended",
        "ts": ts,
        "payload": {"stage": stage, "attempt": 1, "outcome": "done", "commit": commit},
    }


def test_the_stage_after_a_merge_is_based_on_the_merge_commit(tmp_path: Path) -> None:
    """Зависимый этап получает базой merge-коммит, а не одну из полос.

    Родство проверяется тем же `git merge-base --is-ancestor`, что и во всём
    движке: merge-коммит — общий потомок обеих полос, поэтому дерево от него не
    теряет работу ни одной стороны. Это и есть fan-in, ради которого версия
    делалась.
    """
    repo, sha_a, sha_b, merge = lanes_repo(tmp_path)
    spec = SimpleNamespace(repo=str(repo), base="main", stages=())
    dependent = Stage(id="gates", role="tester", needs=("join",), workspace="worktree")
    facts = [ended("impl_a", sha_a, 10), ended("impl_b", sha_b, 11), ended("join", merge, 12)]
    assert runner.base_of_stage(spec, dependent, facts) == merge


def test_diverging_lanes_without_a_merge_still_refuse_to_build_a_base(tmp_path: Path) -> None:
    """Граф без `merge` не отменяет отказа: «предшественники разошлись».

    Отказ остаётся честным, пока слияния никто не объявил: дерево, отведённое от
    одной полосы, потеряло бы работу другой. `merge` — единственный ход, который
    такой отказ снимает, и он объявляется в графе явно.
    """
    repo, sha_a, sha_b, _merge = lanes_repo(tmp_path)
    spec = SimpleNamespace(repo=str(repo), base="main", stages=())
    dependent = Stage(id="gates", role="tester", needs=("impl_a", "impl_b"), workspace="worktree")
    facts = [ended("impl_a", sha_a, 10), ended("impl_b", sha_b, 11)]
    with pytest.raises(runner.BaseError) as refused:
        runner.base_of_stage(spec, dependent, facts)
    message = str(refused.value)
    assert sha_a[:12] in message and sha_b[:12] in message, message
