"""Резолвер конфликта слияния (0.2.2): бриф, отчёт, отказ, уборка, повтор.

Механика самого слияния проверена соседним сьютом (`tests/test_merge_runner.py`):
полосы ведут свои ветки, чистый fan-in даёт merge-коммит с двумя родителями.
Здесь проверяется **резолвер** — то, что движок делает, когда полосы столкнулись:

* бриф резолвера — это контракт самого этапа слияния (лид планирует один этап):
  конфликтные файлы, дифф каждой полосы от общей базы, уже влитые полосы,
  ветки и деревья сторон, запрет стратегий и форма отчёта;
* отчёт обязан назвать, что взято с каждой стороны, — и то, что он называет,
  сверяется с переданным брифом: чужой коммит, лишняя сторона, молчание о
  разведённом файле — отказ (`merge.dropped`), а не решение;
* гейты слияния, объявленные лидом, приносит резолвер: код команды — часть
  отчёта, поэтому «развёл» без прогона по слитому дереву не принимается;
* не смог — отказ `merge.conflict` с файлами и обоими sha, деревья сторон целы;
  смог — merge-коммит на ветке дела, а ветки и деревья полос убраны.

Ни одна попытка не поднимает модель: полосы и резолвер — фейковый спавн, но
резолвер реально разводит конфликт в живом репозитории и коммитит в состоянии
merge — иначе тест проверял бы подделку, а не движок.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Optional

from test_merge_runner import (  # noqa: E402 - пакета нет, pytest кладёт `tests/` в путь
    NOW,
    Fake,
    ended_of,
    events,
    git,
    git_repo,
    lane_run,
    lanes_spec,
    make_deal,
    make_home,
    open_store,
    runner,
    tree_of,
    write_spec,
)


class Resolver(Fake):
    """Резолвер с настраиваемым отчётом: разводит конфликт и отвечает как велено.

    Отличия от резолвера соседнего сьюта — ровно те, что нужны правилам отчёта:
    какие файлы названы разведёнными (`files`), какие стороны (`sides`), какой
    код у принесённых гейтов (`evidence`). Работа при этом настоящая: файл
    пишется в дереве, `git add` + `git commit` в состоянии merge даёт коммит с
    двумя родителями.
    """

    def __init__(
        self,
        *,
        text: Optional[str] = "from a and from b\n",
        files: Optional[list[str]] = None,
        sides: Optional[list[dict[str, Any]]] = None,
        taken: str = "",
        why: str = "",
        evidence: Optional[list[dict[str, Any]]] = None,
        commit: bool = True,
        status: str = "done",
    ) -> None:
        super().__init__()
        self.text = text
        self.files = files
        self.sides = sides
        self.taken = taken
        self.why = why
        self.evidence = evidence
        self.commit = commit
        self.status = status
        self.contracts: list[dict[str, Any]] = []
        # Деревья сторон на момент спавна: после успешного слияния движок их
        # убирает, и проверять существование можно только здесь.
        self.lane_trees: list[bool] = []

    def _sides(self, merge: dict[str, Any]) -> list[dict[str, Any]]:
        if self.sides is not None:
            return self.sides
        return [
            {
                "lane": str(lane["lane"]),
                "sha": str(lane["sha"]),
                "intent": f"полоса {lane['lane']} правила свой файл",
                "joined": "влита, столкновение разведено по существу",
            }
            for lane in merge.get("lanes") or []
        ]

    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "Resolver":
        query_file = Path(cmd[list(cmd).index("--query-file") + 1])
        contract = json.loads(query_file.read_text(encoding="utf-8"))
        self.contracts.append(contract)
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        merge = contract.get("merge") or {}
        self.lane_trees = [
            Path(str(lane["tree"])).is_dir()
            for lane in merge.get("lanes") or []
            if lane.get("tree")
        ]
        if self.text is not None and merge.get("conflict_files"):
            name = str(merge["conflict_files"][0])
            (Path(cwd) / name).write_text(self.text, encoding="utf-8")
            git(cwd, "add", name)
            if self.commit:
                git(cwd, "commit", "-q", "--no-edit")
        sha = git(cwd, "rev-parse", "HEAD") if self.commit else ""
        report: dict[str, Any] = {
            "status": self.status,
            "summary": "конфликт разведён по существу",
            "evidence": list(self.evidence or []),
            "commit": sha,
            "merge": {
                "files": list(self.files if self.files is not None else merge.get("conflict_files") or []),
                "sides": self._sides(merge),
                "taken": self.taken,
                "why": self.why,
            },
        }
        target = Path(str(contract.get("report_to") or (query_file.parent / "result.json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return self


class PairLane(Fake):
    """Полоса, которая правит два файла сразу: две стороны — два конфликта."""

    def __init__(self, files: dict[str, str]) -> None:
        super().__init__(files=dict(files))
        self.names = dict(files)

    def _report(self, query_file: Path, cwd: Path) -> None:
        contract = json.loads(query_file.read_text(encoding="utf-8"))
        target = Path(str(contract.get("report_to") or (query_file.parent / "result.json")))
        artifacts = [
            {
                "path": name,
                "sha256": __import__("hashlib").sha256((Path(cwd) / name).read_bytes()).hexdigest(),
            }
            for name in self.names
        ]
        report = {
            "status": "done",
            "summary": "две правки одной полосы",
            "artifacts": artifacts,
            "evidence": [],
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


class PartialResolver(Fake):
    """Резолвер, который развёл не всё: один файл добавлен, второй остался в споре."""

    def __init__(self, name: str, text: str) -> None:
        super().__init__()
        self.name = name
        self.text = text
        self.contracts: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], cwd: Path, env: Any = None) -> "PartialResolver":
        query_file = Path(cmd[list(cmd).index("--query-file") + 1])
        contract = json.loads(query_file.read_text(encoding="utf-8"))
        self.contracts.append(contract)
        self.calls.append({"cmd": list(cmd), "cwd": Path(cwd)})
        (Path(cwd) / self.name).write_text(self.text, encoding="utf-8")
        git(cwd, "add", self.name)
        merge = contract.get("merge") or {}
        report: dict[str, Any] = {
            "status": "done",
            "summary": "развёл один файл, второй не тронул",
            "evidence": [],
            "merge": {
                "files": list(merge.get("conflict_files") or []),
                "sides": [
                    {"lane": "impl_a", "intent": "своя версия", "joined": "влита"},
                    {"lane": "impl_b", "intent": "своя версия", "joined": "влита"},
                ],
            },
        }
        target = Path(str(contract.get("report_to") or (query_file.parent / "result.json")))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return self


class Conflict:
    """Две полосы, столкнувшиеся в одном файле: готовое состояние для резолвера.

    `gate` добавляет слиянию объявленный гейт (`evidence: cmd: "true"`): на
    конфликтном пути его обязан прогнать резолвер, и отчёт о коде команды —
    часть пакета слияния.
    """

    def __init__(self, tmp_path: Path, *, gate: bool = False) -> None:
        self.home = make_home(tmp_path)
        self.conn = open_store(self.home)
        self.deal = make_deal(self.conn)
        self.repo = git_repo(tmp_path)
        text = textwrap.dedent(
            f"""
            duty: lanes
            repo: {self.repo}
            base: HEAD
            stages:
              - id: impl_a
                role: implementer
                workspace: worktree
                parallel_with: [impl_b]
              - id: impl_b
                role: implementer
                workspace: worktree
                parallel_with: [impl_a]
              - id: join
                kind: merge
                needs: [impl_b, impl_a]
                workspace: worktree
            """
        )
        if gate:
            # Гейт объявлен у самого слияния: на конфликтном пути его прогоняет
            # резолвер, и код команды становится частью пакета слияния.
            text += '    evidence:\n      - cmd: "true"\n        must: 0\n'
        self.spec = write_spec(tmp_path, text)
        self.sha_a = lane_run(self.conn, self.spec, self.deal, self.home, "impl_a", "shared.txt", "from a\n")
        self.sha_b = lane_run(self.conn, self.spec, self.deal, self.home, "impl_b", "shared.txt", "from b\n")

    def run(self, resolver: Fake, attempt: int = 1) -> dict[str, Any]:
        return runner.run_stage(
            self.conn, self.spec, self.deal, "join", attempt, home=self.home,
            spawn=resolver, clock=lambda: NOW,
        )


# ------------------------------------------------ 1. бриф резолвера и один этап


def test_the_resolver_is_the_merge_stage_itself_with_the_handoff(tmp_path: Path) -> None:
    """Конфликт поднимает резолвера ролью этапа, и его бриф — весь развод целиком.

    Лид планирует ОДИН этап слияния, а не «merge + отдельный resolver»: режим
    резолва — часть этапа. Потому и роль у попытки — роль этапа (умолчание
    слияния — `implementer`), и дерево то же, и форма отчёта уже названа.
    """
    state = Conflict(tmp_path)
    resolver = Resolver()
    report = state.run(resolver)
    assert report["outcome"] == "done", report
    assert len(resolver.calls) == 1, "на конфликт поднимается ровно одна попытка-резолвер"

    contract = resolver.contracts[0]
    assert contract["role"] == "implementer", "резолвер — роль этапа слияния"
    assert ["-p", "implementer"] == resolver.calls[0]["cmd"][2:4], resolver.calls[0]["cmd"]
    merge = contract["merge"]
    assert merge["conflict_files"] == ["shared.txt"]
    assert merge["order"] == ["impl_b", "impl_a"], "порядок вливания — объявление в плане"
    assert merge["applied"] == ["impl_b"], "первая полоса уже влита, столкнулись на второй"
    assert merge["base"] == git(state.repo, "merge-base", state.sha_a, state.sha_b), merge["base"]
    lanes = {str(lane["lane"]): lane for lane in merge["lanes"]}
    assert set(lanes) == {"impl_a", "impl_b"}
    assert "from a" in lanes["impl_a"]["diff"] and "from b" in lanes["impl_b"]["diff"], lanes
    assert lanes["impl_a"]["branch"] == runner.lane_branch(state.deal, "impl_a")
    assert resolver.lane_trees and all(resolver.lane_trees), "резолвер видит деревья сторон"
    assert "merge" in contract["report_schema"], "форма отчёта — до работы, а не из отказа"


# ------------------------------------ 2. повтор: причина прошлого отказа в брифе


def test_the_retry_names_the_previous_refusal_and_the_same_conflict(tmp_path: Path) -> None:
    """Второй резолвер знает, почему упал первый, и видит тот же конфликт.

    Повтор по обычной политике (`contract` — один повтор) без причины прошлой
    попытки повторял бы и ошибку; при этом состояние merge живёт в дереве, и
    развод продолжается с того же места.
    """
    state = Conflict(tmp_path)
    first = state.run(Resolver(text=None, commit=False, status="refused"))
    assert first["outcome"] == "failed" and first["retry"] is True, first

    retry = Resolver()
    second = state.run(retry, attempt=2)
    assert second["outcome"] == "done", second
    contract = retry.contracts[0]
    assert contract["merge"]["conflict_files"] == ["shared.txt"]
    failures = contract.get("previous_failures") or []
    assert failures, "бриф повтора называет прошлый отказ"
    assert "merge.conflict" in json.dumps(failures, ensure_ascii=False)
    reused = [event for event in events(state.conn, state.deal) if str(event["kind"]) == "tree_reused"]
    assert reused, "второй развод идёт в сохранённом дереве конфликта, а не с базы"


# ------------------------------------ 3. развёл: merge-коммит на ветке дела, полосы убраны


def test_a_resolved_conflict_lands_on_the_deal_branch_and_cleans_the_lanes(tmp_path: Path) -> None:
    """Разведённый конфликт закрепляется веткой дела, ветки и деревья полос убраны.

    Уборка — только после закрепления merge-коммита: до этого ветка полосы —
    единственная ссылка на работу стороны, и удалять её значило бы потерять
    полосу, которую как раз и пришли сохранять.
    """
    state = Conflict(tmp_path)
    report = state.run(Resolver())
    merge_sha = str(report["commit"])

    assert git(state.repo, "rev-parse", f"duty/{state.deal['id']}") == merge_sha
    parents = git(state.repo, "rev-list", "--parents", "-n", "1", merge_sha).split()
    assert set(parents[1:]) == {state.sha_a, state.sha_b}, parents

    branches = git(state.repo, "branch", "--list", "--format=%(refname:short)").splitlines()
    assert runner.lane_branch(state.deal, "impl_a") not in branches, "ветка полосы убрана"
    assert runner.lane_branch(state.deal, "impl_b") not in branches
    assert not tree_of(state.home, state.deal, "impl_a").exists(), "дерево полосы убрано"
    assert not tree_of(state.home, state.deal, "impl_b").exists()

    ended = ended_of(state.conn, state.deal, "join")
    assert len(ended["details"]["merge"]["cleaned"]) == 2, ended["details"]["merge"]
    assert not ended["details"]["merge"]["kept"], ended["details"]["merge"]


# ------------------------- 4. отчёт сверяется с брифом: молчание — потеря, а не решение


def test_a_file_resolved_in_silence_is_refused(tmp_path: Path) -> None:
    """Разведённый файл, о котором отчёт молчит, — отказ: «что развёл» не проверить.

    `merge.files` — не украшение отчёта, а перечень разведённого: пустой список
    при двух сторонах в одном файле означает, что конфликт закрыт молча.
    """
    state = Conflict(tmp_path)
    report = state.run(Resolver(files=[]))
    assert report["outcome"] == "failed", report
    assert report["kind"] == "contract"
    reason = str(report["reason"])
    assert "merge.dropped" in reason and "shared.txt" in reason, reason


def test_a_side_named_by_a_stranger_lane_is_refused(tmp_path: Path) -> None:
    """Сторона, которой нет среди полос слияния, — отказ: отчёт говорит не о деле.

    Слияние собирает ровно `needs` этапа; сторона `impl_c` в отчёте означает,
    что резолвер называет источник, которого в слиянии не было.
    """
    state = Conflict(tmp_path)
    sides = [
        {"lane": "impl_a", "sha": state.sha_a, "intent": "своя версия", "joined": "влита"},
        {"lane": "impl_b", "sha": state.sha_b, "intent": "своя версия", "joined": "влита"},
        {"lane": "impl_c", "sha": state.sha_a, "intent": "чужая полоса", "joined": "влита"},
    ]
    report = state.run(Resolver(sides=sides))
    assert report["outcome"] == "failed", report
    assert "merge.dropped" in str(report["reason"])
    assert "impl_c" in str(report["reason"])


def test_a_side_named_by_the_wrong_commit_is_refused(tmp_path: Path) -> None:
    """Сторона названа не тем коммитом — отказ: тогда неизвестно, чья работа вошла.

    Коммит полосы известен движку из журнала; отчёт, называющий другой sha (или
    заглушку), делает сверку «что взято» невыполнимой.
    """
    state = Conflict(tmp_path)
    sides = [
        {"lane": "impl_a", "sha": "0" * 40, "intent": "своя версия", "joined": "влита"},
        {"lane": "impl_b", "sha": state.sha_b, "intent": "своя версия", "joined": "влита"},
    ]
    report = state.run(Resolver(sides=sides))
    assert report["outcome"] == "failed", report
    assert "merge.dropped" in str(report["reason"])
    assert state.sha_a[:12] in str(report["reason"]), report["reason"]


def test_a_taken_lane_outside_the_merge_is_refused(tmp_path: Path) -> None:
    """`taken` называет полосу вне слияния — отказ: обоснование не про эту сторону.

    «Взял одну сторону целиком» — законное решение, но только про сторону этого
    слияния: иначе `why` объясняет то, чего в дереве не было.
    """
    state = Conflict(tmp_path)
    report = state.run(Resolver(text="from b\n", taken="impl_c", why="так проще"))
    assert report["outcome"] == "failed", report
    assert "merge.dropped" in str(report["reason"])
    assert "impl_c" in str(report["reason"])


# ------------------------------------- 5. гейты слияния приносит резолвер


def test_the_declared_gates_must_come_back_from_the_resolver(tmp_path: Path) -> None:
    """Гейт, объявленный у слияния, — часть отчёта резолвера: «развёл» без кода не принято.

    «Зелёная полоса + зелёная полоса ≠ зелёный merge»: собранное дерево обязан
    кто-то прогнать, и на конфликтном пути это делает резолвер. Пустой список
    доказательств при объявленной команде — отказ с её именем.
    """
    state = Conflict(tmp_path, gate=True)
    report = state.run(Resolver())
    assert report["outcome"] == "failed", report
    assert report["kind"] == "contract"
    assert "true" in str(report["reason"]), report["reason"]


def test_a_red_gate_from_the_resolver_is_refused(tmp_path: Path) -> None:
    """Код гейта проверяется: разведённый конфликт с красной командой не принимается.

    Разведённое слияние, упавшее на гейте, говорит своей причиной: класс
    `merge.conflict` — про неразведённый конфликт в дереве, а не про любой отказ.
    """
    state = Conflict(tmp_path, gate=True)
    report = state.run(Resolver(evidence=[{"cmd": "true", "exit": 1, "started_at": NOW}]))
    assert report["outcome"] == "failed", report
    assert "evidence.exit" in str(report["reason"]), report["reason"]
    assert "merge.conflict" not in str(report["reason"]), "конфликт разведён — класс не о нём"


# ------------------- 6. развёл не всё: класс merge.conflict называет оставшееся


def test_a_conflict_left_in_the_tree_is_named_though_the_report_says_done(tmp_path: Path) -> None:
    """Один файл разведён, второй остался в споре — отказ называет оставшийся.

    «Готово» в отчёте не отменяет `git diff --diff-filter=U`: класс
    `merge.conflict` судится по дереву, а не по слову, и обе стороны остаются
    на диске — работы полос никто не теряет.
    """
    home = make_home(tmp_path)
    conn = open_store(home)
    deal = make_deal(conn)
    repo = git_repo(tmp_path)
    spec = lanes_spec(tmp_path, repo)
    first = runner.run_stage(
        conn, spec, deal, "impl_a", 1, home=home,
        spawn=PairLane({"shared.txt": "a shared\n", "other.txt": "a other\n"}), clock=lambda: NOW,
    )
    second = runner.run_stage(
        conn, spec, deal, "impl_b", 1, home=home,
        spawn=PairLane({"shared.txt": "b shared\n", "other.txt": "b other\n"}), clock=lambda: NOW,
    )
    assert first["outcome"] == "done" and second["outcome"] == "done", (first, second)

    partial = PartialResolver("shared.txt", "a shared and b shared\n")
    report = runner.run_stage(conn, spec, deal, "join", 1, home=home, spawn=partial, clock=lambda: NOW)
    assert sorted(partial.contracts[0]["merge"]["conflict_files"]) == ["other.txt", "shared.txt"]
    assert report["outcome"] == "failed", report
    assert report["kind"] == "contract"
    reason = str(report["reason"])
    assert "merge.conflict" in reason and "other.txt" in reason, reason
    assert "shared.txt" not in reason, "назван только оставшийся в споре файл"
    assert tree_of(home, deal, "impl_a").is_dir(), "дерево полосы осталось при отказе"
    ended = ended_of(conn, deal, "join")
    assert ended["details"]["tree_kept"] == str(tree_of(home, deal, "join"))
    named = [str(lane["tree"]) for lane in ended["details"]["merge"]["lanes"]]
    assert str(tree_of(home, deal, "impl_a")) in named, "путь дерева стороны назван в факте"
    assert str(tree_of(home, deal, "impl_b")) in named

