"""Hermes Duty — backend desktop-плагина «Дела».

P0: только чтение. Плагин открывает живые источники в режиме `?mode=ro` и
показывает, что в них есть, — он ничего не пишет ни в kanban, ни в pool, ни в
state.db, и не пересчитывает статусы карточек.

Маршруты монтируются как `/api/plugins/hermes-duty/<...>`.

Источники (поиск от корня Hermes, без хардкода путей):

- `kanban.db` в корне — живая БД доски `default` (остальные доски лежат в
  `kanban/boards/<slug>/kanban.db`);
- `messaging/pool.db` — пул вопросов (для таксономии ожиданий, тоже только чтение);
- `profiles/<role>/state.db` — сессии, из которых берётся «последнее действие».

Ядро kanban и его диспетчер не импортируются: P0 не должен зависеть от кода,
который мы собираемся заменить.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()


def _plugin_version() -> str:
    """Номер версии — один источник (plugin.yaml), а не четвёртая копия в коде."""
    meta = Path(__file__).resolve().parent.parent / "plugin.yaml"
    try:
        for line in meta.read_text().splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return "0.0.0"


VERSION = _plugin_version()


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _ro(path: Path) -> str:
    """URI для открытия БД строго на чтение (не создаёт файл, не берёт write-lock)."""
    return path.resolve().as_uri() + "?mode=ro"


def _query(path: Path, sql: str) -> list[dict[str, Any]]:
    """Один read-only запрос; на любой сбой возвращает пустой список, а не падает."""
    try:
        with closing(sqlite3.connect(_ro(path), uri=True, timeout=2.0)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            return [dict(row) for row in conn.execute(sql).fetchall()]
    except Exception:
        return []


def _table_exists(path: Path, table: str) -> bool:
    rows = _query(path, "SELECT name FROM sqlite_master WHERE type='table'")
    return any(str(r.get("name")) == table for r in rows)


def _discover_boards(home: Path) -> list[dict[str, Any]]:
    """Все живые kanban-БД: корневая (доска default) + boards/<slug>/kanban.db."""
    found: list[dict[str, Any]] = []
    root = home / "kanban.db"
    if root.is_file() and root.stat().st_size > 0:
        found.append({"slug": "default", "path": str(root)})
    boards_dir = home / "kanban" / "boards"
    if boards_dir.is_dir():
        for child in sorted(boards_dir.iterdir()):
            db = child / "kanban.db"
            if child.is_dir() and db.is_file() and db.stat().st_size > 0:
                found.append({"slug": child.name, "path": str(db)})
    return found


def _board_summary(entry: dict[str, Any]) -> dict[str, Any]:
    path = Path(entry["path"])
    out: dict[str, Any] = {
        "slug": entry["slug"],
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "mtime": int(path.stat().st_mtime),
        "readable": False,
        "tasks": 0,
        "blocked": 0,
        "running": 0,
    }
    if not _table_exists(path, "tasks"):
        # Пустая/служебная БД — не ошибка, честно помечаем.
        out["note"] = "no tasks table"
        return out
    rows = _query(
        path,
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked, "
        "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running FROM tasks",
    )
    if rows:
        out["readable"] = True
        out["tasks"] = int(rows[0].get("n") or 0)
        out["blocked"] = int(rows[0].get("blocked") or 0)
        out["running"] = int(rows[0].get("running") or 0)
    return out


def _pool_summary(home: Path) -> Optional[dict[str, Any]]:
    db = home / "messaging" / "pool.db"
    if not db.is_file():
        return None
    rows = _query(db, "SELECT origin, status, COUNT(*) AS n FROM questions GROUP BY origin, status")
    return {
        "path": str(db),
        "by_origin_status": rows,
        "total": sum(int(r.get("n") or 0) for r in rows),
    }


@router.get("/health")
def health() -> dict[str, Any]:
    """Живая проверка: плагин смонтирован и источники читаются на чтение."""
    home = _hermes_home()
    boards = _discover_boards(home)
    readable = [b for b in boards if _table_exists(Path(b["path"]), "tasks")]
    pool = _pool_summary(home)
    return {
        "ok": True,
        "version": VERSION,
        "hermes_home": str(home),
        "kanban": {
            "databases": len(boards),
            "readable": len(readable),
            "task_rows": sum(
                int((_query(Path(b["path"]), "SELECT COUNT(*) AS n FROM tasks") or [{"n": 0}])[0].get("n") or 0)
                for b in readable
            ),
        },
        "pool": {"present": pool is not None, "total": (pool or {}).get("total", 0)},
        "mode": "read-only",
    }


@router.get("/sources")
def sources() -> dict[str, Any]:
    """Что плагин видит: доски, пул, профили. Ничего не меняет."""
    home = _hermes_home()
    profiles_dir = home / "profiles"
    profiles = sorted(
        p.name for p in profiles_dir.iterdir() if p.is_dir() and (p / "state.db").is_file()
    ) if profiles_dir.is_dir() else []
    return {
        "version": VERSION,
        "hermes_home": str(home),
        "boards": [_board_summary(b) for b in _discover_boards(home)],
        "pool": _pool_summary(home),
        "profiles": profiles,
    }


@router.get("/board/{slug}/tasks")
def board_tasks(slug: str) -> dict[str, Any]:
    """Сырой срез карточек доски для будущего экрана «сейчас» (read-only)."""
    home = _hermes_home()
    match = [b for b in _discover_boards(home) if b["slug"] == slug]
    if not match:
        raise HTTPException(status_code=404, detail=f"board '{slug}' not found")
    path = Path(match[0]["path"])
    rows = _query(
        path,
        "SELECT id, title, assignee, status, block_kind, consecutive_failures, "
        "worker_pid, current_run_id, created_at, started_at, completed_at "
        "FROM tasks WHERE status NOT IN ('done','archived') ORDER BY created_at DESC LIMIT 200",
    )
    return {"slug": slug, "path": str(path), "count": len(rows), "tasks": rows}
