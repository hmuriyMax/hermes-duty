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
- desktop-страница `/duty` (`desktop/plugin.js` + пункт сайдбара «Дела»): таблица
  активных дел группами по `waiting.state`, локальные фильтры (доска, чипы
  состояний, поиск по `id`/заголовку), таймлайн попыток в правой панели,
  автообновление `/now` раз в 10 с с подписью «обновлено N с назад» и красной
  плашкой с «повторить» при ошибке (данные не затираются);
- шейпер страницы — `desktop/model.js` (чистые функции, тесты
  `tests/model.test.mjs` на фикстурах `tests/fixtures/*.json`); в `plugin.js` он
  вшит дословно (`bin/duty-model-sync`), потому что загрузчик десктопа не
  резолвит относительные импорты вне SDK;
- контракт P0 и таксономия ожиданий — `docs/DESIGN.md`;
- контракт версий и `bin/duty-version` — `docs/VERSIONING.md`.
