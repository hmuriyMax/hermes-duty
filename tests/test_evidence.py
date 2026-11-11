"""Evidence-гейт: «готово» без проверяемого пакета не принимается.

Каждая проверка слоя — отдельный тест, и отказ каждой называется своим правилом
(`evidence.exit`, `artifact.sha256`, `commit.foreign`, …): гейт, который умеет
только «нет», заставляет человека читать код вместо причины.

Дерево попытки здесь настоящее (`tmp_path`), коммит — настоящий (`git init`):
подставленный «sha резолвится» проверял бы тест, а не гейт. Ни одна команда из
`evidence` не запускается — слой сверяет заявленное с деревом, а не исполняет
работу второй раз.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Начало попытки: свежесть evidence считается от него, а не от «сейчас».
STARTED = 1_790_000_000
NOW = STARTED + 60


def load_module(name: str, filename: str) -> Any:
    """Модуль плагина по пути — под тем же именем, что и у остального кода."""
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evidence = load_module("hermes_duty_evidence", "evidence.py")
spec_module = load_module("hermes_duty_spec", "spec.py")


# ------------------------------------------------------------------ фикстуры


def make_stage(
    *,
    evidence_items: tuple[tuple[str, int], ...] = (),
    tokens: Optional[int] = None,
    workspace: str = "none",
    role: str = "implementer",
    verdict: Any = None,
    kind: str = "agent",
) -> Any:
    """Этап спеки: объявленные команды, бюджет и вид — только через него гейт их узнаёт."""
    return spec_module.Stage(
        id="impl",
        role=role,
        kind=kind,
        workspace=workspace,
        evidence=tuple(spec_module.Evidence(cmd=cmd, must=must) for cmd, must in evidence_items),
        budget=spec_module.Budget(tokens=tokens) if tokens is not None else None,
        verdict=verdict,
    )


def write(root: Path, name: str, text: str = "готово\n") -> tuple[str, str]:
    """Файл в дереве попытки: (путь, настоящий полный sha256)."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return name, hashlib.sha256(path.read_bytes()).hexdigest()


def report(root: Path, *, cmd: str = "go test ./...", must: int = 0, **fields: Any) -> dict[str, Any]:
    """Отчёт, который сходится с деревом: его и портят отдельные тесты."""
    path, digest = write(root, "notes.txt")
    built: dict[str, Any] = {
        "status": "done",
        "artifacts": [{"path": path, "sha256": digest}],
        "evidence": [{"cmd": cmd, "exit": must, "started_at": STARTED}],
        "summary": "фикс в шапке, тесты зелёные",
    }
    built.update(fields)
    return built


def verify(result: Any, stage: Any, workdir: Path, **kwargs: Any) -> Any:
    """Гейт с неподвижными часами: начало попытки и «сейчас» приходят снаружи."""
    kwargs.setdefault("attempt_started_at", STARTED)
    kwargs.setdefault("now", NOW)
    return evidence.verify(result, stage, workdir, **kwargs)


def rules(verdict: Any) -> list[str]:
    return [issue.rule for issue in verdict.errors]


def warnings(verdict: Any) -> list[str]:
    return [issue.rule for issue in verdict.warnings]


def git(root: Path, *args: str) -> str:
    """Настоящий git в дереве: коммит проверяется им же, что и в жизни."""
    done = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def git_repo(root: Path) -> str:
    """Репозиторий с одним коммитом: база попытки. Возвращает его sha."""
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("ok\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return git(root, "rev-parse", "HEAD")


def commit(root: Path, name: str = "b.txt", text: str = "работа\n") -> str:
    """Новый коммит поверх текущего: работа этой попытки."""
    (root / name).write_text(text, encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "work")
    return git(root, "rev-parse", "HEAD")


# ------------------------------------------------------------------ 1. схема


def test_status_outside_the_closed_set_is_a_contract_refusal(tmp_path: Path) -> None:
    """`status` вне {done, blocked, refused} — не «почти верный», а незнание: отказ."""
    verdict = verify({"status": "ok", "summary": "готово"}, make_stage(), tmp_path)
    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert verdict.errors[0].where == "result.json:status"
    assert rules(verdict) == [evidence.RULE_STATUS]


def test_done_without_evidence_cannot_close_the_stage_this_is_the_p3_gate(tmp_path: Path) -> None:
    """Гейт P3: `status: done` без `evidence` при объявленных командах этап не закрывает.

    Это и есть фаза P3 целиком: слово «готово» без проверяемого пакета не
    принимается, даже если процесс завершился нулём и отчёт разобрался.
    """
    stage = make_stage(evidence_items=(("go test ./...", 0),))
    path, digest = write(tmp_path, "notes.txt")
    bare = {
        "status": "done",
        "artifacts": [{"path": path, "sha256": digest}],
        "summary": "готово",
    }
    verdict = verify(bare, stage, tmp_path)
    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert verdict.errors[0].where == "result.json:evidence"
    assert rules(verdict) == [evidence.RULE_REQUIRED]
    assert "evidence" in str(verdict.errors[0])


def test_done_without_commit_and_artifacts_is_refused(tmp_path: Path) -> None:
    """`done` без коммита и без артефактов: работа ничем не названа."""
    verdict = verify({"status": "done", "evidence": [], "summary": "готово"}, make_stage(), tmp_path)
    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert rules(verdict) == [evidence.RULE_REQUIRED]
    assert verdict.errors[0].where == "result.json:artifacts"
    # Текст отказа прежний: обычный этап по-прежнему обязан назвать работу.
    assert "работа не названа ни коммитом, ни файлами" in str(verdict.errors[0])


def test_publishing_stage_witnesses_published(tmp_path: Path) -> None:
    """Публикующий этап называет публикацию, а не дерево: `published` — его свидетель.

    Дефект FIX-W (живой прогон TF3-c): `publish` не меняет дерево, его свидетель —
    `published: {sha, branch}`, который движок сверяет с origin (`git ls-remote`),
    а гейт пакета требовал `commit|artifacts` и отказывал каждой честной попытке.
    """
    stage = make_stage(role="publisher")
    built = {
        "status": "done",
        "evidence": [],
        "summary": "запушил в origin",
        "published": {"sha": "a" * 40, "branch": "main"},
    }

    verdict = verify(built, stage, tmp_path)

    assert (verdict.ok, verdict.kind) == (True, None), verdict.details
    assert verdict.witness == evidence.WITNESS_PUBLISHED
    assert evidence.witness(stage) == evidence.WITNESS_PUBLISHED


def test_publishing_stage_without_published_is_refused_by_its_own_witness(tmp_path: Path) -> None:
    """Публикующий без `published` — отказ, и он называет свидетель этого этапа."""
    stage = make_stage(role="publisher")
    path, digest = write(tmp_path, "notes.txt")
    built = {
        "status": "done",
        "artifacts": [{"path": path, "sha256": digest}],
        "evidence": [],
        "summary": "готово",
    }

    verdict = verify(built, stage, tmp_path)

    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert rules(verdict) == [evidence.RULE_REQUIRED]
    assert verdict.errors[0].where == "result.json:published"
    assert "этап публикации: нужен published {sha, branch}" in str(verdict.errors[0])
    assert "commit/artifacts не требуются" in str(verdict.errors[0])


def test_verdict_stage_witnesses_verdict(tmp_path: Path) -> None:
    """Этап с приговором называет приговор: `verdict` вместо коммита и файлов."""
    stage = make_stage(role="reviewer", verdict=spec_module.Verdict(approve="next", changes="impl"))
    built = {
        "status": "done",
        "evidence": [],
        "summary": "ревью прошло",
        "verdict": {"action": "approve", "target": "next", "reason": "дифф чистый"},
    }

    verdict = verify(built, stage, tmp_path)

    assert (verdict.ok, verdict.kind) == (True, None), verdict.details
    assert verdict.witness == evidence.WITNESS_VERDICT
    assert evidence.witness(stage) == evidence.WITNESS_VERDICT


def test_verdict_stage_without_verdict_is_refused_by_its_own_witness(tmp_path: Path) -> None:
    """Этап с приговором без приговора: отказ называет `verdict`, а не «нет работы»."""
    stage = make_stage(role="reviewer", verdict=spec_module.Verdict(approve="next", changes="impl"))

    verdict = verify({"status": "done", "evidence": [], "summary": "ревью прошло"}, stage, tmp_path)

    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert verdict.errors[0].where == "result.json:verdict"
    assert "нужен verdict {action, target, reason}" in str(verdict.errors[0])


def test_witness_is_chosen_by_the_stage_not_one_for_all(tmp_path: Path) -> None:
    """Свидетель выбирается по этапу: обычный `agent` без `verdict` — `commit|artifacts`."""
    assert evidence.witness(make_stage()) == evidence.WITNESS_WORK
    assert evidence.witness(make_stage(kind="shell")) == evidence.WITNESS_WORK
    # Публикующая роль — не «обычный этап», даже если это команда.
    assert evidence.witness(make_stage(role="deployer")) == evidence.WITNESS_PUBLISHED


def test_researcher_witness_is_the_report(tmp_path: Path) -> None:
    """У роли без терминала свидетель — отчёт, a не коммит: коммита ей взять неоткуда.

    Кейс d_48e01a0e: исследовательский этап нельзя было запланировать (evidence) —
    и, даже пройди он гейт плана, гейт отчёта требовал бы `commit|artifacts`,
    которых у read-only роли не бывает.
    """
    stage = make_stage(role="researcher")
    assert evidence.witness(stage) == evidence.WITNESS_REPORT
    schema = evidence.report_schema(stage)
    assert "commit|artifacts" not in schema["required_for_done"]
    assert evidence.WITNESS_REPORT in schema["required_for_done"]

    ok = verify({"status": "done", "evidence": [], "summary": "сверка кода с докой: расхождений нет"}, stage, tmp_path)
    assert ok.ok is True, [str(issue) for issue in ok.errors]

    empty = verify({"status": "done", "evidence": [], "summary": "  "}, stage, tmp_path)
    assert empty.ok is False
    assert empty.errors[0].where == "result.json:report"


def test_documenter_witness_is_the_report(tmp_path: Path) -> None:
    """Документер доказывает себя отчётом: коммита в дереве у него не бывает.

    Дело доказывается документом, а документ лежит вне дерева попытки (заметка
    тикета). Требовать `commit|artifacts` от `docs`-этапа — значит требовать
    коммит документа в репозиторий, которого никто не просил (`ENGINE-1.0.md`
    §9: «docs (documenter) → что изменилось и как это проверять»).
    """
    stage = make_stage(role="documenter")
    assert evidence.witness(stage) == evidence.WITNESS_REPORT
    assert "commit|artifacts" not in evidence.report_schema(stage)["required_for_done"]

    ok = verify({"status": "done", "evidence": [], "summary": "записал итог v35 в заметку RP-14080"}, stage, tmp_path)
    assert ok.ok is True, [str(issue) for issue in ok.errors]

    silent = verify({"status": "done", "evidence": [], "summary": " "}, stage, tmp_path)
    assert silent.ok is False


def test_documenter_evidence_command_still_runs(tmp_path: Path) -> None:
    """Команда-доказательство по документу проверяется, даже если роль без дерева.

    Документер пишет заметку; «задокументировал» без проверки — это слово, а не
    факт. Спека вправе попросить `test -s`/`grep -q` по самому документу; гейт
    сверяет заявленный код возврата с `must` (`verify` команд не перезапускает —
    их запускает попытка и записывает `exit`).
    """
    note = tmp_path / "index.md"
    note.write_text("## Итог валидации v35\n", encoding="utf-8")
    cmd = f"grep -q 'v35' {note}"
    stage = make_stage(role="documenter", evidence_items=((cmd, 0),))

    ok = verify({"status": "done", "summary": "записал",
                 "evidence": [{"cmd": cmd, "exit": 0, "started_at": STARTED + 5}]}, stage, tmp_path)
    assert ok.ok is True, [str(issue) for issue in ok.errors]

    failed = verify({"status": "done", "summary": "записал",
                     "evidence": [{"cmd": cmd, "exit": 1, "started_at": STARTED + 5}]}, stage, tmp_path)
    assert failed.ok is False

    # Молчание про команду — тоже отказ: доказательство объявлено, а не предъявлено.
    missing = verify({"status": "done", "summary": "записал", "evidence": []}, stage, tmp_path)
    assert missing.ok is False


def test_done_without_summary_is_refused(tmp_path: Path) -> None:
    """`done` без `summary`: итог не назван, проверять нечего."""
    built = report(tmp_path, summary="   ")
    verdict = verify(built, make_stage(), tmp_path)
    assert verdict.ok is False
    assert [issue.where for issue in verdict.errors] == ["result.json:summary"]


def test_extra_field_is_a_warning_not_a_refusal(tmp_path: Path) -> None:
    """Лишнее поле — предупреждение: исполнитель вправе добавить контекст."""
    built = report(tmp_path, notes="человеку на разбор")
    verdict = verify(built, make_stage(), tmp_path)
    assert (verdict.ok, verdict.kind) == (True, None)
    assert warnings(verdict) == [evidence.RULE_EXTRA]
    assert verdict.details[0].where == "result.json:notes"
    assert verdict.details[0].severity == evidence.WARNING


# ------------------------------------------------------------- 2. артефакты


def test_artifact_hash_mismatch_is_refused(tmp_path: Path) -> None:
    """Хеш не сходится: файл есть, но он не тот, что заявлен."""
    path, _ = write(tmp_path, "notes.txt")
    built = report(tmp_path, artifacts=[{"path": path, "sha256": "0" * 64}])
    verdict = verify(built, make_stage(), tmp_path)
    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert verdict.errors[0].where == "result.json:artifacts[0].sha256"
    assert rules(verdict) == [evidence.RULE_ARTIFACT_SHA]


def test_truncated_hash_does_not_prove_the_file(tmp_path: Path) -> None:
    """Усечённый `sha256` («9f2c…») — не доказательство, а его отсутствие."""
    path, digest = write(tmp_path, "notes.txt")
    built = report(tmp_path, artifacts=[{"path": path, "sha256": digest[:12] + "…"}])
    verdict = verify(built, make_stage(), tmp_path)
    assert verdict.ok is False
    assert rules(verdict) == [evidence.RULE_ARTIFACT_SHA]
    assert "полный хеш" in str(verdict.errors[0])


def test_missing_artifact_file_is_refused(tmp_path: Path) -> None:
    """Заявленного файла в дереве нет: артефакт — обещание, а не факт."""
    built = report(tmp_path, artifacts=[{"path": "нет-такого.go", "sha256": "1" * 64}])
    verdict = verify(built, make_stage(), tmp_path)
    assert verdict.ok is False
    assert verdict.errors[0].where == "result.json:artifacts[0].path"
    assert rules(verdict) == [evidence.RULE_ARTIFACT_MISSING]


def test_missing_artifact_names_the_measure(tmp_path: Path) -> None:
    """«Файла нет» называет мерку: иначе не понять, чего не хватило — файла или дерева.

    Этап без дерева (`workspace: none`) ищет в каталоге попытки, и отказ обязан
    это сказать: без мерки человек читает код вместо причины.
    """
    built = report(tmp_path, artifacts=[{"path": "нет-такого.go", "sha256": "1" * 64}])
    verdict = verify(built, make_stage(), tmp_path)
    message = str(verdict.errors[0])
    assert f"файла нет: {tmp_path.resolve() / 'нет-такого.go'}" in message
    assert "искали относительно каталог попытки" in message
    assert "workspace: worktree" not in message


def test_missing_artifact_in_a_tree_stage_names_the_tree(tmp_path: Path) -> None:
    """Этап с деревом (`workspace: worktree`) ищет в дереве — это и называется."""
    tree = tmp_path / "tree"
    report(tmp_path)
    built = report(tree, artifacts=[{"path": "нет-такого.go", "sha256": "1" * 64}])
    verdict = verify(built, make_stage(workspace="worktree"), tmp_path, root=tree)
    message = str(verdict.errors[0])
    assert rules(verdict) == [evidence.RULE_ARTIFACT_MISSING]
    assert "искали относительно дерево попытки" in message


def test_artifact_in_the_attempt_tree_hints_workspace_worktree(tmp_path: Path) -> None:
    """Регрессия на E2E `d_221d021a`: файл есть в дереве, а мерка — каталог попытки.

    Тестер скопировал дерево `impl` в свою рабочую область, прогнал тесты и
    отчитался артефактом — гейт честно отказывал, потому что по мерке каталога
    попытки файла нет. Отказ обязан назвать подсказку, а не только «нету».
    """
    path, digest = write(tmp_path / "tree", "dashboard/failures.py", "классы отказа\n")
    built = report(tmp_path, artifacts=[{"path": path, "sha256": digest}])
    verdict = verify(built, make_stage(), tmp_path)
    message = str(verdict.errors[0])
    assert rules(verdict) == [evidence.RULE_ARTIFACT_MISSING]
    assert f"файл есть в дереве попытки ({(tmp_path / 'tree' / path).resolve()})" in message
    assert "укажи workspace: worktree, чтобы работать в дереве" in message


def test_artifact_hint_is_not_invented_when_the_tree_has_no_file(tmp_path: Path) -> None:
    """Подсказки нет, если файла нет и в дереве: это «файла нет вовсе», а не мерка."""
    (tmp_path / "tree").mkdir()
    built = report(tmp_path, artifacts=[{"path": "нет-такого.go", "sha256": "1" * 64}])
    verdict = verify(built, make_stage(), tmp_path)
    message = str(verdict.errors[0])
    assert "файл есть в дереве попытки" not in message
    assert "workspace: worktree" not in message


@pytest.mark.parametrize("path", ["../снаружи.txt", "/etc/passwd"])
def test_artifact_path_outside_the_attempt_tree_is_refused(tmp_path: Path, path: str) -> None:
    """Путь наружу (`..` или абсолютный) — отказ, даже если файл существует."""
    root = tmp_path / "tree"
    root.mkdir()
    (tmp_path / "снаружи.txt").write_text("чужое\n", encoding="utf-8")
    built = report(root, artifacts=[{"path": path, "sha256": "2" * 64}])
    verdict = verify(built, make_stage(), root)
    assert verdict.ok is False
    assert verdict.errors[0].where == "result.json:artifacts[0].path"
    assert rules(verdict) == [evidence.RULE_ARTIFACT_PATH]


# ---------------------------------------------------------------- 3. коммит


def test_commit_that_does_not_resolve_is_refused(tmp_path: Path) -> None:
    """Коммит не резолвится в дереве — отказ: sha взят не из этой работы."""
    root = tmp_path / "tree"
    git_repo(root)
    built = report(root, commit="deadbeef" * 5)
    verdict = verify(built, make_stage(workspace="worktree"), root, base="HEAD")
    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert verdict.errors[0].where == "result.json:commit"
    assert rules(verdict) == [evidence.RULE_COMMIT_RESOLVE]


def test_commit_that_was_ancestor_of_base_is_refused(tmp_path: Path) -> None:
    """Коммит-предок базы — «принёс готовое», то есть чужое: отказ."""
    root = tmp_path / "tree"
    base = git_repo(root)
    built = report(root, commit=base)
    verdict = verify(built, make_stage(workspace="worktree"), root, base=base)
    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert rules(verdict) == [evidence.RULE_COMMIT_FOREIGN]
    assert verdict.errors[0].where == "result.json:commit"


def test_own_commit_on_top_of_base_is_accepted(tmp_path: Path) -> None:
    """Свой коммит поверх базы — работа этой попытки: пакет принимается."""
    root = tmp_path / "tree"
    base = git_repo(root)
    fresh = commit(root)
    built = report(root, commit=fresh)
    verdict = verify(built, make_stage(workspace="worktree"), root, base=base)
    assert (verdict.ok, verdict.kind) == (True, None)
    assert [issue.rule for issue in verdict.details] == []


# -------------------------------------------------------------- 4. evidence


def test_missing_evidence_record_is_refused(tmp_path: Path) -> None:
    """По объявленной команде записи нет — отказ, а не «наверное запускал»."""
    stage = make_stage(evidence_items=(("go test ./...", 0), ("go build ./...", 0)))
    built = report(tmp_path)
    verdict = verify(built, stage, tmp_path)
    assert verdict.ok is False
    assert rules(verdict) == [evidence.RULE_EVIDENCE_MISSING]
    assert "go build ./..." in str(verdict.errors[0])


def test_exit_code_differs_from_must_is_refused(tmp_path: Path) -> None:
    """`exit` не равен объявленному `must`: команда шла, но не так, как надо."""
    stage = make_stage(evidence_items=(("go test ./...", 0),))
    built = report(tmp_path, evidence=[{"cmd": "go test ./...", "exit": 1, "started_at": STARTED}])
    verdict = verify(built, stage, tmp_path)
    assert verdict.ok is False
    assert verdict.errors[0].where == "result.json:evidence[0].exit"
    assert rules(verdict) == [evidence.RULE_EVIDENCE_EXIT]


def test_evidence_older_than_the_attempt_is_refused(tmp_path: Path) -> None:
    """Прогон вчерашнего дня — не доказательство сегодняшней работы."""
    stage = make_stage(evidence_items=(("go test ./...", 0),))
    built = report(tmp_path, evidence=[{"cmd": "go test ./...", "exit": 0, "started_at": STARTED - 86_400}])
    verdict = verify(built, stage, tmp_path)
    assert verdict.ok is False
    assert verdict.errors[0].where == "result.json:evidence[0].started_at"
    assert rules(verdict) == [evidence.RULE_EVIDENCE_STALE]


def test_evidence_without_started_at_has_no_freshness_proof(tmp_path: Path) -> None:
    """Нет `started_at` — свежесть не доказана: отказ того же правила `evidence.stale`."""
    stage = make_stage(evidence_items=(("go test ./...", 0),))
    built = report(tmp_path, evidence=[{"cmd": "go test ./...", "exit": 0}])
    verdict = verify(built, stage, tmp_path)
    assert verdict.ok is False
    assert rules(verdict) == [evidence.RULE_EVIDENCE_STALE]
    assert "свежесть не доказана" in str(verdict.errors[0])


def test_evidence_from_the_future_is_refused(tmp_path: Path) -> None:
    """`started_at` позже текущего времени — запись из будущего, а не факт."""
    stage = make_stage(evidence_items=(("go test ./...", 0),))
    built = report(tmp_path, evidence=[{"cmd": "go test ./...", "exit": 0, "started_at": NOW + 10}])
    verdict = verify(built, stage, tmp_path)
    assert verdict.ok is False
    assert rules(verdict) == [evidence.RULE_EVIDENCE_FUTURE]


# -------------------------------------------------------------- 5. подмена


def test_command_substituted_by_a_space_is_refused(tmp_path: Path) -> None:
    """«Почти та же команда» — отказ: `-count=1` и `-count 1` это разные флаги."""
    stage = make_stage(evidence_items=(("go test -count=1 ./...", 0),))
    built = report(tmp_path, evidence=[{"cmd": "go test -count 1 ./...", "exit": 0, "started_at": STARTED}])
    verdict = verify(built, stage, tmp_path)
    assert verdict.ok is False
    assert verdict.errors[0].where == "result.json:evidence[0].cmd"
    assert rules(verdict) == [evidence.RULE_EVIDENCE_SUBSTITUTED]
    assert "-count=1" in str(verdict.errors[0])


def test_command_with_different_spacing_is_the_same_command(tmp_path: Path) -> None:
    """Пробелы, которые на смысл не влияют, нормализуются: команда та же."""
    stage = make_stage(evidence_items=(("go   test   ./...", 0),))
    built = report(tmp_path, evidence=[{"cmd": " go test ./...  ", "exit": 0, "started_at": STARTED}])
    verdict = verify(built, stage, tmp_path)
    assert (verdict.ok, verdict.kind) == (True, None)


# ---------------------------------------------------------------- 6. бюджет


def test_token_budget_overrun_is_capability_not_contract(tmp_path: Path) -> None:
    """Работа сделана, но вышла за бюджет — `capability`, а не подлог контракта."""
    stage = make_stage(tokens=1000)
    built = report(tmp_path, usage={"total_tokens": 5000})
    verdict = verify(built, stage, tmp_path)
    assert (verdict.ok, verdict.kind) == (False, "capability")
    assert rules(verdict) == [evidence.RULE_BUDGET]
    assert verdict.errors[0].where == "result.json:usage.total_tokens"


def test_budget_within_the_limit_does_not_refuse(tmp_path: Path) -> None:
    """Пакет сходится и бюджет в рамках — попытка принимается."""
    stage = make_stage(tokens=1000)
    built = report(tmp_path, usage={"total_tokens": 900})
    verdict = verify(built, stage, tmp_path)
    assert (verdict.ok, verdict.kind) == (True, None)


# ------------------------------------------------------------- happy paths


def test_happy_path_without_commit_for_none_workspace(tmp_path: Path) -> None:
    """Этап без дерева: пакет — артефакт с хешем и evidence, коммита нет и не надо."""
    stage = make_stage(evidence_items=(("bin/duty demo", 0),), workspace="none")
    built = report(tmp_path, cmd="bin/duty demo", must=0)
    verdict = verify(built, stage, tmp_path)
    assert (verdict.ok, verdict.kind, verdict.details) == (True, None, [])
    assert "commit" not in built


def test_happy_path_with_commit_and_evidence(tmp_path: Path) -> None:
    """Ветка работы: свой коммит, файл с хешем, объявленные команды со свежим кодом."""
    root = tmp_path / "tree"
    base = git_repo(root)
    fresh = commit(root)
    workdir = root / ".duty"
    workdir.mkdir()
    stage = make_stage(evidence_items=(("go build ./...", 0), ("go test ./...", 0)), workspace="worktree")
    path, digest = write(root, "internal/widget/x.go", "package widget\n")
    built = {
        "status": "done",
        "commit": fresh,
        "artifacts": [{"path": path, "sha256": digest}],
        "evidence": [
            {"cmd": "go build ./...", "exit": 0, "started_at": "2026-11-04T09:59:30Z"},
            {"cmd": "go test ./...", "exit": 0, "started_at": "2026-11-04T10:00:00Z"},
        ],
        "summary": "тристейт в клиенте",
    }
    verdict = verify(
        built,
        stage,
        workdir,
        root=root,
        base=base,
        attempt_started_at=int(evidence._stamp("2026-11-04T09:59:00Z")),
        now=int(evidence._stamp("2026-11-04T10:05:00Z")),
    )
    assert (verdict.ok, verdict.kind, [str(issue) for issue in verdict.details]) == (True, None, [])


def test_blocked_report_needs_no_package(tmp_path: Path) -> None:
    """`status: blocked` не заявляет «готово»: пакета у вопроса нет и быть не может."""
    verdict = verify({"status": "blocked", "summary": "нужно решение"}, make_stage(), tmp_path)
    assert (verdict.ok, verdict.kind, verdict.details) == (True, None, [])


def test_refused_report_needs_no_package(tmp_path: Path) -> None:
    """`status: refused` — класс ставит движок; гейт пакета не требует."""
    verdict = verify({"status": "refused", "summary": "нет доступа"}, make_stage(), tmp_path)
    assert (verdict.ok, verdict.kind) == (True, None)


def test_contract_issues_outrank_capability(tmp_path: Path) -> None:
    """Пакет не сходится и бюджет превышен — класс `contract`: подлог важнее рамок."""
    stage = make_stage(evidence_items=(("go test ./...", 0),), tokens=10)
    built = report(tmp_path, evidence=[], usage={"total_tokens": 500})
    verdict = verify(built, stage, tmp_path)
    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert evidence.RULE_EVIDENCE_MISSING in rules(verdict)
    assert evidence.RULE_BUDGET in rules(verdict)


def test_reason_names_first_three_details(tmp_path: Path) -> None:
    """Причина отказа — первые три details: строка читается, полный список в details."""
    stage = make_stage(evidence_items=(("go test ./...", 0), ("go build ./...", 0)), tokens=10)
    built = {
        "status": "done",
        "artifacts": [{"path": "нет-такого", "sha256": "3" * 64}],
        "evidence": [{"cmd": "go vet ./...", "exit": 1}],
        "usage": {"total_tokens": 99},
    }
    verdict = verify(built, stage, tmp_path)
    assert verdict.ok is False
    assert len(verdict.errors) >= 4
    assert verdict.reason().count(" · ") == 2
    assert str(verdict.errors[0]) in verdict.reason()


def test_report_schema_speaks_the_declared_commands(tmp_path: Path) -> None:
    """Схему отчёта гейт отдаёт в контракт: о правилах узнают до работы, не из отказа."""
    stage = make_stage(evidence_items=(("go test ./...", 0),), workspace="worktree")
    schema = evidence.report_schema(stage)
    assert schema["status"] == ["done", "blocked", "refused"]
    assert schema["required_for_done"] == ["commit|artifacts", "evidence", "summary"]
    assert schema["evidence"] == [{"cmd": "go test ./...", "exit": 0, "started_at": "unix-секунды"}]
    assert "published" not in schema and "verdict" not in schema


def test_report_schema_requires_the_witness_of_the_stage(tmp_path: Path) -> None:
    """Контракт и гейт требуют одного свидетеля: иначе настоящее правило — в отказе."""
    publishing = evidence.report_schema(make_stage(role="publisher"))
    assert publishing["required_for_done"] == ["published", "evidence", "summary"]
    assert publishing["published"]["sha"].startswith("полный или сокращённый sha")

    reviewing = evidence.report_schema(
        make_stage(role="reviewer", verdict=spec_module.Verdict(approve="next", changes="impl"))
    )
    assert reviewing["required_for_done"] == ["verdict", "evidence", "summary"]
    assert set(reviewing["verdict"]) == {"action", "target", "reason"}


def test_non_object_report_is_refused(tmp_path: Path) -> None:
    """Отчёт-список вместо объекта: разбирать нечего, проверять нечего."""
    verdict = verify(["готово"], make_stage(), tmp_path)
    assert (verdict.ok, verdict.kind) == (False, "contract")
    assert rules(verdict) == [evidence.RULE_REPORT]
