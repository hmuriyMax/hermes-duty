# RUNBOOK — проверить плагин руками

Короткий чек-лист: включить, открыть страницу, дёрнуть роуты, прогнать тесты,
сверить версии, разобраться с пустой страницей. Всё ниже — только чтение
(включение плагина — единственное изменение, и оно в конфиге Hermes, не в данных).

## 1. Включить плагин

```bash
hermes plugins enable hermes-duty
```

Проверить, что он виден и включён (в профиле, где живёт `~/.hermes/plugins`):

```bash
HERMES_HOME=~/.hermes hermes plugins list --plain --no-bundled | grep duty
# enabled  git  0.0.1  hermes-duty
```

Если команда запускается из другого профиля (`HERMES_HOME` указывает на
`~/.hermes/profiles/<role>`), плагина в списке не будет — он лежит в
`~/.hermes/plugins/`. Для проверки исходников плагина это не нужно.

## 2. Открыть страницу `/duty`

Hermes Desktop → пункт сайдбара **«Дела»** (или путь `/duty`). Что должно быть:

- заголовок «Дела», счётчик `сейчас: N`, `· досок X/Y` и подпись
  «обновлено N с назад», которая тикает раз в секунду;
- таблица активных дел, сгруппированная по состояниям ожидания;
- фильтры: выпадающий список досок, чипы состояний, поиск по `id`/заголовку;
- клик по строке — правая панель с таймлайном попыток.

Страница обновляет `/now` раз в 10 секунд. Пустой список — это нормальный ответ:
он означает «активных дел нет», а не «сломалось» (см. п. 6).

## 3. Дёрнуть роуты через TestClient

Без десктопа и без живого сервера: роутер монтируется в тестовое приложение.
Сниппет проверялся на `release/v0.0.1` (`8618ce7`).

```python
import importlib.util
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Какой корень Hermes смотрим. В воркерной сессии HERMES_HOME указывает на профиль
# (~/.hermes/profiles/<role>) — там досок нет, и /now вернёт пусто.
os.environ["HERMES_HOME"] = os.path.expanduser("~/.hermes")

api = Path.home() / ".hermes" / "plugins" / "hermes-duty" / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location("hermes_duty_api", api)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

app = FastAPI()
app.include_router(mod.router, prefix="/api/plugins/hermes-duty")
client = TestClient(app)

print(client.get("/api/plugins/hermes-duty/health").json())
# {'ok': True, 'version': '0.0.1', 'hermes_home': '/Users/…/.hermes',
#  'kanban': {'databases': 19, 'readable': 19, 'task_rows': N},
#  'pool': {'present': True, 'total': N}, 'mode': 'read-only'}

now = client.get("/api/plugins/hermes-duty/now").json()
print(now["count"], now["by_state"], now["errors"], len(now["boards"]))
# например: 6 {'wait_human': 4, …, 'working': 1, 'wait_gate': 1} [] 19

if now["deals"]:
    deal = now["deals"][0]
    timeline = client.get(
        f"/api/plugins/hermes-duty/duty/{deal['board']}/{deal['id']}/attempts"
    ).json()
    print(timeline["title"], timeline["count"], timeline["attempts"][:1])
```

Запускать интерпретатором, где есть `fastapi` (в системном `python3` его нет):
`~/.hermes/bvenv/bin/python probe.py`.

Корень читается из `HERMES_HOME` на каждый запрос (`dashboard/plugin_api.py:65-69`),
поэтому переключить его можно и после импорта модуля: до смены переменной
`/health` отдаёт `hermes_home` профиля, после — новый корень (проверено).

Что ловить в ответах:

| признак | что значит |
|---|---|
| `errors[]` не пуст | источник не прочитан; в `board` — `messaging/pool.db` или slug доски, в `reason` — текст сбоя |
| `boards[].read = false` | доска не читается; её дел в ответе нет |
| `boards[].warnings[]` | не читается вспомогательная таблица (`task_runs`/`task_links`/`task_events`) — факты деградировали |
| `by_state` в ответе | счётчики по состояниям считаются до фильтра `?state=` |
| 400 на `/now?state=…` | состояние не из таксономии (`wait_human`, `capability`, `dead_lease`, `crashed`, `working`, `wait_slot`, `wait_gate`) |
| 404 на `/now?board=…` | такой доски нет |
| два `boards[]` со slug `default` | в `kanban/boards/` есть каталог `default` — он идёт отдельной записью рядом с корневой БД |

## 4. Прогнать pytest

```bash
cd ~/.hermes/plugins/hermes-duty
~/.hermes/bvenv/bin/python -m pytest -q      # 21 passed
```

`python3 -m pytest` из системы падает с `No module named pytest` — это окружение,
а не плагин: нужен venv, где `fastapi` и `pytest` стоят.

Тесты поднимают свои БД в `tmp_path` по реальной схеме, живые источники не читают.

## 5. Проверить шейпер и версии

```bash
node --test tests/model.test.mjs    # 20 pass
bin/duty-model-sync --check         # OK: inlined model matches desktop/model.js
bin/duty-version check              # OK (последняя строка), код 0
```

`duty-model-sync --check` обязателен: если `desktop/model.js` правили, а
`plugin.js` не пересобрали, страница поедет на старой копии шейпера.

## 6. «Страница пустая»

Порядок такой:

1. **Рестарт не нужен.** Роутер плагина hot-mount-ится на первом запросе из
   приложения: если на старте он не смонтирован, фолбэк перехватывает
   `/api/plugins/<name>/…`, монтирует роуты в процессе и повторно маршрутизирует
   тот же запрос (`hermes_cli/web_server_dashboard.py:1085-1118`). Достаточно
   обновить страницу или дождаться следующего опроса (10 с).
2. Проверить, что данные вообще есть: сниппет из п. 3 с правильным `HERMES_HOME`.
   Пустой `deals` при непустом `errors` — это не пустая страница, а непрочитанные
   источники (жёлтая полоса на странице, список в пустом состоянии).
3. Проверить, что десктоп грузит свежую копию `plugin.js`:

   ```bash
   hermes plugins doctor ~/.hermes/plugins/hermes-duty
   ```

   Предупреждение `Desktop runs a stale copy of desktop/plugin.js
   (~/.hermes/desktop-plugins/hermes-duty)` означает, что правки в пакете не
   доехали до приложения: обновить копию через Capabilities → Plugins → Rescan
   (или перезапустить десктоп).
4. Проверить, что плагин включён (п. 1) — выключенный плагин даёт 404 на
   `/api/plugins/hermes-duty/health`, а не пустую страницу.

## 7. Что делать не нужно

- не перезапускать gateway ради страницы;
- не править номер версии руками — только `bin/duty-version`;
- не читать живые БД вручную, если то же видно в `/now`: он открывает их
  `?mode=ro` и отдаёт причину сбоя в `errors[]`.
