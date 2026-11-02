# Changelog

Формат: версия → что вошла. Номер версии живёт в трёх файлах и правится только
через `bin/duty-version` (см. `docs/VERSIONING.md`).

## [0.0.1] — в разработке

P0: смотреть, ни на что не влияя.

- каркас плагина: `plugin.yaml` (manifest v2), `__init__.py`, `dashboard/manifest.json`;
- backend `dashboard/plugin_api.py`, только чтение: `/health`, `/sources`,
  `/board/{slug}/tasks`; источники открываются `?mode=ro`, ядро kanban не импортируется;
- экран «сейчас» на том же бэкенде: `GET /now` — активные дела всех досок с
  лизингом, попыткой (pid + `os.kill(pid, 0)`, heartbeat), последним действием и
  таксономией ожиданий (`wait_human` > `capability` > `dead_lease` > `crashed` >
  `working` > `wait_slot` > `wait_gate`) плюс фильтры `?board=` и `?state=`;
- `GET /duty/{board}/{task_id}/attempts` — таймлайн попыток по `task_runs` и
  привязанным к ранам `task_events`;
- контракт P0 и таксономия ожиданий — `docs/DESIGN.md`;
- контракт версий и `bin/duty-version` — `docs/VERSIONING.md`.
