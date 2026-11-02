# Changelog

Формат: версия → что вошла. Номер версии живёт в трёх файлах и правится только
через `bin/duty-version` (см. `docs/VERSIONING.md`).

## [0.0.1] — в разработке

P0: смотреть, ни на что не влияя.

- каркас плагина: `plugin.yaml` (manifest v2), `__init__.py`, `dashboard/manifest.json`;
- backend `dashboard/plugin_api.py`, только чтение: `/health`, `/sources`,
  `/board/{slug}/tasks`; источники открываются `?mode=ro`, ядро kanban не импортируется;
- контракт P0 и таксономия ожиданий — `docs/DESIGN.md`;
- контракт версий и `bin/duty-version` — `docs/VERSIONING.md`.
