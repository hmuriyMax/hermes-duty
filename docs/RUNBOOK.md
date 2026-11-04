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

## 2. Открыть страницу

**Десктоп**: Hermes Desktop → пункт сайдбара **«Дела»** (внутренний роут
приложения `/duty`; в браузере этот путь — 404, у него нет HTTP-адреса).

**Браузер**: `http://127.0.0.1:18742/web/hermes-duty/` напрямую или
`http://127.0.0.1:8360/duty` через Hub (там же — карточка «Дела» на главной).
На прямом адресе страница проверяется без десктопа вообще.

Что должно быть (на обоих):

- заголовок «Дела», версия плагина, табы **«Сейчас» / «Дело» / «Почему стоит» /
  «Здоровье»** — четыре экрана в порядке спеки; счётчик `сейчас: N`, `· досок X/Y`
  и подпись «обновлено N с назад», которая тикает раз в секунду;
- «Сейчас» — таблица активных дел, сгруппированная по состояниям ожидания,
  фильтры (доска, чипы состояний, поиск по `id`/заголовку/исполнителю);
- таб **«Дело»** (и клик по строке в «Сейчас»): спецификация карточки, факты (попытка,
  лизинг, последнее действие) и таймлайн попыток, кнопка «← к списку»; без выбранного
  дела таб показывает выбор из активных;
- «Почему стоит» — карточки с доказательством из `waiting.evidence`;
- «Здоровье» — дела с первой попытки, ожидание до неё распределением (p50/p90/p99),
  таксономия исходов, таблица по доскам.

Страница обновляет `/now` раз в 10 секунд, `/stats` — раз в 30. Пустой список —
это нормальный ответ: он означает «активных дел нет», а не «сломалось» (см. п. 8).

Если браузер показывает старую разметку — виноват кэш `app.js`: он подключается
с `?v=<дата>`; при правке фронта версию в `dashboard/web/index.html` надо поднять,
иначе вкладка живёт на старом модуле (заголовки `no-store` страхуют только от
кэша промежуточного слоя).

Жёлтая полоса над списком (или такой же список в пустом состоянии) — это
неполнота ответа, а не украшение: недоступная доска, упавший источник, обрезанный
срез (`boards[].truncated`) или деградировавшая таблица (`boards[].warnings[]`).
Разбор причин — таблица в п. 3.

## 3. Дёрнуть роуты через TestClient

Без десктопа и без живого сервера: роутер монтируется в тестовое приложение.
Сниппет проверялся на `release/v0.0.1` (`d76e30a`) интерпретатором `~/.hermes/bvenv/bin/python`.

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
# {'ok': True, 'version': '0.0.3', 'hermes_home': '/Users/…/.hermes',
#  'kanban': {'databases': 19, 'readable': 19, 'task_rows': N},
#  'pool': {'present': True, 'total': N},
#  'journal': {'events': 16, 'deals': 12, 'drift': {'empty_journal': 0}},
#  'mode': 'read-only'}

# Проход ingest в свой журнал: единственное, что плагин пишет (только duty/duty.db).
print(client.post("/api/plugins/hermes-duty/ingest").json()["appended"])   # 16, потом 0
# в том же ответе — сколько ответов пула доехало до журнала:
print(client.post("/api/plugins/hermes-duty/ingest").json()["answers"])    # {'appended': 0, 'errors': []}

# Вопрос по делу — единственный канал вопросов (пул messaging/pool.db, origin='duty').
# Пишется по явному запросу: дело в blocked/needs_input без вопроса, а человек/координатор
# решает, что спросить. Повторный вызов вернёт тот же question_id (deduped).
asked = client.post(
    "/api/plugins/hermes-duty/ask/stack/t_0001",
    json={"text": "Даёшь ок на выкатку?", "text_full": "### Контекст\n\n…", "options": ["ок", "нет"]},
).json()
print(asked)                     # {'question_id': 501, 'created': True, 'deduped': False, 'adopted': False, 'errors': []}
print(client.post("/api/plugins/hermes-duty/ask/stack/t_0001", json={"text": "?"}).json())
# {'question_id': 501, 'created': False, 'deduped': True, 'adopted': False, 'errors': []}
# Подхвачен чужой живой вопрос (origin != 'duty', например сканер карточек):
# id тот же, но факта в журнале нет — роут говорит это полем adopted:
# {'question_id': 512, 'created': False, 'deduped': True, 'adopted': True, 'errors': []}

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
| `boards[].read = false` | доска не читается; её дел в ответе нет. `boards[].note = "empty db"` — файл `kanban.db` нулевого размера |
| `boards[].warnings[]` | не читается вспомогательная таблица (`task_runs`/`task_links`/`task_events`, для причин блока — `task_events(blocked)`) — факты деградировали; на странице это строка `доска <slug>: <причина>` в жёлтой полосе |
| `boards[].truncated = true` | дел на доске `>= DEAL_LIMIT = 500` — показан не весь срез; на странице строка `доска <slug>: срез обрезан лимитом, показано <N> дел` |
| `warnings[]` в `/stats` не пуст | срез доски обрезан `STATS_TASK_LIMIT = 5000` задач: `totals.waiting_sec` и доля первой попытки посчитаны по неполной выборке; на странице «Здоровье» это строка `доска <slug>: срез обрезан лимитом …` над KPI |
| `errors[]` с `board: "messaging/pool.db"` и `reason: "questions truncated: …"` | живых вопросов в пуле больше `QUESTION_LIMIT = 200`: вопрос этого дела мог не войти в срез, в `waiting.evidence` стоит «факт wait_human не проверен» |
| `by_state` в ответе | счётчики по состояниям считаются до фильтра `?state=` |
| `journal: empty — состояние из kanban` в начале `waiting.evidence` | дела нет в журнале (или в журнале нет живого факта): состояние посчитано по kanban, и это сказано словами; `POST /ingest` подберёт дело, и пометка уйдёт |
| `journal.drift.empty_journal` больше нуля в `/health` | столько активных дел журнал ещё не видел; после `POST /ingest` число падает до нуля. Пока оно больше нуля, состояние этих дел — из kanban |
| `journal.note` в `/health` | свой журнал не открылся — это не «ноль событий», причина названа |
| `waiting.human_kind` | `question` — ждёт ответа на вопрос пула; `status` — ждёт решения по статусу (блок, разбор, ревью, мердж), вопроса нет. В шапке «Сейчас» это двумя числами: `N вопросов + M блокировок`; в счётчик входят только эти два вида — `capability`, `no_signal` и `crashed` считаются отдельно, потому что там нужен не ответ и не решение, а доступ/разбор |
| `503` на `POST /ask/{board}/{task_id}` с `errors[].board = "messaging/pool.db"` | вопрос не создан: пул отсутствует, не читается или только на чтение. Событие `question_asked` при этом не пишется — в журнале нет следа вопроса, которого нет в пуле |
| `deduped: true` в ответе `/ask` | живой вопрос по делу уже был — вернули его `question_id`, второй строки в пуле нет |
| `adopted: true` в ответе `/ask` | подхвачен живой вопрос **чужого** продюсера (`origin != 'duty'`): id возвращён, `question_asked` по нему не пишется — ответ на чужой вопрос журналу не виден, и снять факт было бы нечем. Живой вопрос при этом виден в пуле, и `/now` поднимает дело до `wait_human` с пометкой «в журнале вопроса нет» |
| `boards[].warnings[]` строкой `дело t_…: в журнале completed, а kanban status=…` | журнал говорит, что дело закрыто (правило 2 fold), и оно не попало в срез — расхождение названо, а не скрыто |
| 400 на `/now?state=…` | состояние не из таксономии (`wait_human`, `capability`, `dead_lease`, `crashed`, `working`, `no_signal`, `wait_slot`, `wait_gate`) |
| 404 на `/now?board=…` | такой доски нет |
| два `boards[]` со slug `default` | в `kanban/boards/` есть каталог `default` — он идёт отдельной записью рядом с корневой БД |

## 4. Посмотреть дела из терминала (`bin/duty`)

CLI зовёт те же функции, что роуты, — второй набор правил состояний в терминале не
живёт, `--json` печатает ответ роута дословно. Запуск — из каталога плагина; корень
Hermes берётся из `HERMES_HOME`, поэтому в профильной оболочке его надо задать явно
(иначе досок там нет):

```bash
cd ~/.hermes/plugins/hermes-duty
HERMES_HOME=~/.hermes bin/duty now
```

| команда | что показывает |
|---|---|
| `bin/duty now [--board B] [--state S]` | таблица активных дел: доска, `id`, состояние (у `wait_human` — с `/human_kind`), ожидание, возраст, доказательство |
| `bin/duty deal <board> <id>` | карточка дела: факты и таймлайн попыток |
| `bin/duty why <board> <id>` | доказательство `waiting.evidence` и факты, из которых оно собрано |
| `bin/duty why <board> <id> <этап>` | то же плюс приговор последней попытки этапа: исход, класс отказа, причина и первые `details` гейта |
| `bin/duty attempts <board> <id>` | только таймлайн попыток |
| `bin/duty stats [--board B] [--since D]` | p50/p90/p99 ожидания, исходы ранов, доля с первой попытки, таблица по доскам |
| `bin/duty watch [--interval 10]` | тот же `now`, перерисовка в терминале |
| `bin/duty demo [--home DIR] [--keep]` | прогон дела по спеке на песочном корне: шаги учёта (create → claim → ask → answer → gate → comment/attach → ingest → complete → поздний ответ) и 7 проверок-инвариантов; живые БД только читаются |

Идентификаторы двух видов: `t_<hex>` — дело kanban (наблюдаем), `d_<hex>` — своё дело
(store плагина). `duty deal` принимает оба, мутации — только `d_…`.

Мутации своего дела (`docs/OWNERSHIP.md`) — те же функции, что у роутов `/deals…`:

```bash
# завести дело; человеческий вывод печатает его в строке `СОЗДАНО <board>/d_<hex> · ready`,
# а машинный id берут из --json
bin/duty --json create --board duty-demo --title "проверка владения" | grep -o "d_[0-9a-f]*"
bin/duty comment d_<hex> "первый комментарий"
bin/duty block d_<hex> --kind needs_input "нужен доступ"
bin/duty unblock d_<hex>
bin/duty claim d_<hex>        # владелец: --owner, иначе HERMES_PROFILE, иначе пользователь ОС
bin/duty review d_<hex> --reviewer reviewer
bin/duty changes d_<hex> "вернул: нет теста"
bin/duty complete d_<hex> --summary "готово"
bin/duty attach d_<hex> ./report.md   # копия ляжет в duty/attachments/<deal_id>/
bin/duty archive d_<hex>              # PATCH {status: archived}
```

Коды возврата: `0` — ответ получен и `errors[]` пуст; `1` — доски или дела нет; `2` —
источники не прочитались или вход мутации неверный (например, `d_…` там, где ждут `t_…`).
Повтор того же вызова событий не плодит: ответ говорит `событий записано: 0 · повтор`,
и это «уже было», а не «не получилось».

Интерпретатор: в системном `python3` нет `fastapi`, поэтому `bin/duty` перезапускает
себя интерпретатором агента. Свой путь — `DUTY_PYTHON=/path/to/python3`.

## 5. Проверить журнал (`journal.events/deals/drift`)

Состояние ожидания считается fold'ом по своему журналу (`docs/JOURNAL.md`), поэтому
первое, что стоит посмотреть, — три числа в `/health`:

```python
print(client.get("/api/plugins/hermes-duty/health").json()["journal"])
# {'events': 76, 'deals': 19, 'drift': {'empty_journal': 0}}
```

| поле | что значит |
|---|---|
| `events` | сколько строк в журнале (`duty/duty.db`, таблица `events`) |
| `deals` | сколько разных дел журнал знает |
| `drift.empty_journal` | сколько **активных** дел kanban журнал ещё не подобрал: им состояние считается по kanban |
| `note` | причина, если журнал или доска не читаются; `drift = 0` при непрочитанном источнике — это «не проверено», а не «всё хорошо» |

### `drift.empty_journal > 0`

Это не ошибка, а «журнал ещё не догнал доску». Порядок:

1. Прогнать проход ingest и посмотреть, сколько дописалось:

```python
report = client.post("/api/plugins/hermes-duty/ingest").json()
print(report["appended"], report["errors"], report["answers"])   # например 16 [] {"appended": 0, "errors": []}
```

2. Перечитать `/health`: после прохода `drift.empty_journal` падает до нуля. Повторный
   ingest — no-op (`appended: 0`), поэтому гонять его безопасно.
3. Если число **не** упало до нуля — смотреть `errors[]` и `journal.note`: доска не
   читается (тогда её дела не считаются ни в одну сторону), либо в журнал пишет кто-то
   ещё и файл занят (`busy_timeout=2000`), либо дело появилось между проходом и
   проверкой — тогда достаточно прогнать ingest ещё раз.
4. Если в журнале «мусор» после ручных экспериментов — это своя БД, её можно удалить
   (`~/.hermes/duty/duty.db`), но тогда история ожиданий пропадёт: состояние всех дел
   снова будет считаться по kanban (`journal: empty — состояние из kanban` в
   `waiting.evidence`), пока ingest не подберёт дела заново. Журнал начинается с момента
   включения: историю задним числом он не восстанавливает.

### Посмотреть журнал глазами

Таблицы `events`, `deals`, `comments`, `attachments` — в `~/.hermes/duty/duty.db`. Читать
только на чтение (живой файл в WAL):

```bash
~/.hermes/hermes-agent/venv/bin/python3 - <<'PY'
import sqlite3, pathlib
db = pathlib.Path.home() / ".hermes/duty/duty.db"
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
print(conn.execute("select count(*), count(distinct deal_id) from events").fetchone())
for kind, n in conn.execute("select kind, count(*) from events group by kind order by 2 desc"):
    print(f"{kind:18} {n}")
PY
```

Ожидание по одному делу не надо считать руками: `bin/duty why <board> <id>` печатает
`waiting.state`, доказательство и факты, из которых оно собрано; `--json` у той же
команды отдаёт ответ роута целиком.

Почему этап не закрылся — `bin/duty why <board> <id> <этап>`: последняя попытка этапа
(`attempt_started`/`attempt_ended` из `duty/duty.db`), исход, класс отказа
(`infra`/`contract`/`capability`) и первые `details` evidence-гейта. Например:

```
последняя попытка этапа impl:
  попытка #2 · старт 04.11 13:40 · финиш 04.11 13:47 · outcome=failed · kind=contract
  каталог попытки: ~/.hermes/kanban/boards/duty/workspaces/d_1a2b-impl-2
  причина: result.json:evidence[0].exit: evidence.exit: exit=1, а объявлен must=0 · …
  отказы гейта (2):
    - result.json:evidence[0].exit: evidence.exit: exit=1, а объявлен must=0
    - result.json:artifacts[0].sha256: artifact.sha256: sha256 в отчёте 9f2c…, в дереве 4b71…
```

`--json` с этапом отдаёт форму роута плюс ключ `verdict` (сам ответ роута не
переписывается). Правила гейта — `docs/JOURNAL.md`, раздел «Evidence-гейт».

## 6. Прогнать pytest

```bash
cd ~/.hermes/plugins/hermes-duty
~/.hermes/hermes-agent/venv/bin/python -m pytest -q              # все кейсы, live пропущен
~/.hermes/hermes-agent/venv/bin/python -m pytest -m live -q     # живой прогон попытки
```

Живой прогон движка — `tests/test_runner.py::test_live_trivial_stage`: поднимает
настоящий `hermes chat` под профилем `tester` (`workspace: none`, бюджет 2 мин) и
проверяет связку «движок → процесс → поток → событие». Маркер `live` объявлен в
`tests/conftest.py` и без `-m live` такие тесты пропускаются: обычный прогон
модель не вызывает.

Новые кейсы 0.0.2 — `tests/test_screens.py`: `/deal` (активное дело, завершённое
дело без выдуманного состояния, 404, обрезка `body`), `/stats` (первая попытка,
недоступная доска, выборка без ранов). Кейсы 0.0.3 там же: распределение ожидания
(p50/p90/p99 на 1..10 и на объединённой выборке, один элемент, пустая выборка,
обрезанная доска в `warnings[]`).

Кейсы журнала 0.0.3 — `tests/test_journal.py`: схема по контракту (`events`,
`UNIQUE(idempotency_key)`, индекс `(deal_id, id)`, WAL, `busy_timeout=2000`),
`fold` без единой БД (инвариант 4), правила fold (включая `human_kind` и tri-state
pid), повторный ingest → `0` новых строк и тот же `fold` (инвариант 3), `mtime`
`kanban.db`/`state.db` не меняется (инвариант 1), `drift.empty_journal` падает
после ingest, а `GET /now` без `?ingest=1` журнал не создаёт.

Остальные поверхности 0.0.3 — отдельными файлами:

- `tests/test_deals.py` — владение: мутации и их идемпотентность, гейт по родителям,
  атомарность (сбой события откатывает строку), поздний ответ и комментарий не
  воскрешают закрытое дело;
- `tests/test_ask.py` — единственный канал вопросов: дедуп, критичность, два сбоя пула,
  `sync_answers`, `human_kind` до и после вопроса, пул только на чтение во всех прочих
  путях;
- `tests/test_cli.py` — CLI теми же функциями: коды возврата `0`/`1`/`2`, `--json`,
  `watch`, мутации своих дел;
- `tests/test_demo.py` — прогон по спеке на песочном корне (9 кейсов), включая «`kanban.db`
  не изменился ни на байт»;
- `tests/test_frontend_syntax.py` — `desktop/plugin.js` и `dashboard/web/app.js`
  разбираются **парсером ES-модуля** (копия в `.mjs`), потому что `node --check plugin.js`
  проверяет файл как CommonJS и висячую запятую перед `}` пропускает — так в 0.0.3
  десктопный плагин на день выпал из приложения. Красная проба: вернуть запятую — тест
  падает.

Кейсы движка 0.0.4 — `tests/test_runner.py` (34 кейса, модель не вызывается: спавн
подменён фейком, который пишет заданный поток и `result.json`): лизинг не берётся
дважды и забирается после срока, worktree создаётся живым git и убирается у упавшей
попытки, контракт несёт отчёты предшественников и `answer_this_attempt`, таймаут →
`infra`, отсутствие отчёта → `contract`, `blocked` → вопрос в пуле и `wait_human`,
телеметрия по каждому `tool_use`, сбой store откатывает `attempt_started` целиком,
гейт не принимает голое «готово» и ставит `capability` на перерасход из отчёта.
Спека — `tests/test_spec.py` (61 кейс, по тесту на правило отказа), гейт —
`tests/test_evidence.py` (30 кейсов: по тесту на каждое правило отказа плюс happy
path без коммита и с коммитом).

Лента этапов — `tests/test_stagefeed.py` (14 кейсов). Как проверить её руками:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_stagefeed.py -q   # 14 passed
```

- «Попытка — процесс, а не строка статуса»:
  `test_deal_returns_stages_with_attempts` требует у первой попытки `pid`,
  `started_at`, `finished_at`, `duration_sec`, `exit`, `outcome`, `kind`, `tokens`,
  `tools`, `reason` и записи о командах. Красная проба: убрать из `_attempts`
  любое из полей — тест падает.
- «Мёртвый процесс виден без выдуманного статуса»:
  `test_dead_attempt_is_visible_without_invented_state` — попытка без
  `attempt_ended` и с истёкшим лизингом даёт `state: running` (так записано) плюс
  `live: false`, `dead: true`. Красная проба: подставить в `_alive` состояние
  `failed` вместо флагов — тест падает.
- «Чтение ничего не пишет»:
  `test_reading_routes_do_not_write_journal` сверяет размер, `mtime`, `sha256`,
  число строк и набор файлов вокруг `/now` и `/deal`, а
  `test_read_connection_refuses_writes` пробует `INSERT` в соединение ручки и ждёт
  отказ `readonly`. Одного слепка файла мало: открытое на запись соединение,
  которое ничего не записало, оставляет файл точно таким же. Красная проба: убрать
  `?mode=ro` из `journal.connect_ro` — `INSERT` проходит, тест падает.
- «Лента не смотрит на процессы»: `test_stagefeed_has_no_process_lookups` —
  AST-проверка импортов `dashboard/stagefeed.py`. Красная проба: добавить
  `import subprocess` или `os.kill` — тест падает.

Глазами (без браузера): `duty create --spec examples/duty.yaml …`, затем
`GET /deal/<board>/<id>` — в ответе `stages[]` с этапами графа и
`stages_note: ""`; дело без `--spec` даёт пустой `stages[]` и причину в
`stages_note`.

`python3 -m pytest` из системы падает с `No module named pytest` — это окружение,
а не плагин: нужен venv, где `fastapi` и `pytest` стоят.

Тесты поднимают свои БД в `tmp_path` по реальной схеме, живые источники не читают.

Кейсы честности сигналов (ревью RV2) — `test_pid_state_unknown_is_not_reported_as_missing`,
`test_running_without_any_live_signal_is_no_signal`,
`test_foreign_schema_does_not_500_and_names_the_type`,
`test_empty_kanban_db_is_visible_as_unread_board`,
`test_question_pool_limit_is_not_silent`, `test_attempt_reports_where_its_facts_came_from`.
Красная проба к ним: откатить правку (например, вернуть в `_waiting` строку
`'не записан'` вместо `'состояние pid … неизвестно'`) — соответствующий тест
обязан упасть; зелёный тест на откатанном коде означает, что он ничего не проверяет.

## 7. Проверить шейпер и версии

```bash
node --test tests/model.test.mjs tests/web.test.mjs   # 48 pass (36 + 12)
bin/duty-model-sync --check         # OK: inlined model matches desktop/model.js
bin/duty-version check              # OK (последняя строка), код 0
```

`duty-model-sync --check` обязателен: если `desktop/model.js` правили, а
`plugin.js` не пересобрали, страница поедет на старой копии шейпера.

## 8. «Страница пустая»

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

## 9. «Доска есть, а в `/now` её нет»

Значит она выбыла — это не потеря, а правило. Смотреть `/sources`:

```bash
curl -s http://127.0.0.1:18742/api/plugins/hermes-duty/sources \
  -H "X-Hermes-Session-Token: $TOKEN" | python3 -m json.tool | grep -A3 archived
```

- `reason: "_archived: выбывшие доски, не живой источник"` — доска лежит в
  `kanban/boards/_archived/<slug>/`, ядро её закрыло;
- `reason: "board.json: archived"` — рядом с БД лежит камень `board.json` с
  `"archived": true`;
- пусто в `archived[]` и пусто в `boards[]` — доски действительно нет (или файл
  `kanban.db` не создан: обнаружение идёт по файлу, не по каталогу).

Вернуть доску в срез = вернуть её в живое состояние средствами kanban (снять
камень `archived`), а не править плагин: `_archived/` — это то, как ядро хранит
историю, и P0 не переписывает чужие статусы.

## 10. Движок: один шаг попытки (`dashboard/runner.py`)

Шаг движка действует: берёт лизинг этапа, готовит дерево, пишет контракт файлом,
поднимает процесс роли, читает поток и пишет факты в журнал. Модель внутри
движка не вызывается; исполнитель — процесс `hermes chat`.

Что где лежит и куда смотреть:

- **каталог попытки** — `~/.hermes/kanban/boards/<board>/workspaces/<deal>-<stage>-<n>`
  (разрешённый корень `role-access`, `docs/PROBES.md` проба 3). Внутри:
  `.duty/in-<stage>-<n>.json` — входной контракт (цель, границы, отчёты
  предшественников, evidence, адрес вопросов), `.duty/result.json` — отчёт
  попытки, `tree/` — дерево `git worktree`, если этап его просил;
- **лизинг** — строка в таблице `leases` своей `duty/duty.db` (одна на пару
  «дело, этап»). Взятие атомарно, продление — не реже TTL/3, TTL —
  `budget.minutes` или 30 минут. Смотреть: `SELECT * FROM leases`;
- **факты в журнале**: `attempt_started` (pid, фактическая команда, весь контракт),
  `attempt_progress` (по каждому `tool_use`/`tool_result`), `attempt_ended`
  (`outcome`, `kind`, `reason`, `details`, `verdict`, `result`).

Почему в журнале видно «что делается прямо сейчас»: прогресс пишется по ходу
потока, а `fold` копит его счётчиком `progress: {tools, last_tool, last_at}`.
`ps` и хвосты логов для этого не нужны.

### «Готово» проверяется до записи факта

После смерти процесса и до `attempt_ended` движок зовёт гейт
(`dashboard/evidence.py`): сверяет `result.json` с деревом попытки — хеши файлов,
резолв коммита, коды и свежесть объявленных команд, бюджет. Только принятый пакет
даёт `outcome=done`; иначе тот же `attempt_ended` пишется с `outcome=failed`,
`kind` из вердикта и первыми тремя отказами в `reason`. Схема отчёта лежит в
контракте (`report_schema`), так что исполнитель видит требования до работы.
Разбор отказа: `bin/duty why <board> <id> <этап>`.

### Продолжение после вопроса: выбран резюм (`RESUME-OK`)

Проба 1 (`docs/PROBES.md`) дала `RESUME-OK`: id сессии из события `result` держит
контекст на трёх продолжениях подряд и после `clarify`. Поэтому выбор один:

- вопрос задаёт **движок** (`ask()`), а не процесс: `clarify` внутри `--oneshot`
  возвращает `undelivered` и не паркует процесс;
- ответ человека возвращается **резюмом той же сессии**: следующая попытка идёт
  `hermes chat -p <role> --resume <session_id> --query-file <ответ>
  --format stream-json --oneshot`, а `session_id` берётся из
  `attempt_ended.details.session_id`;
- новый процесс «с ответом в контракте» остаётся запасным вариантом на случай
  `RESUME-BROKEN`; сейчас он не используется.

Цена резюма названа там же: +8 тыс. input-токенов на ход и риск, что продолжение
уйдёт гулять по инструментам — поэтому в отчёте попытки видно `usage_tokens`.

**Точка возврата ответа (W5, 2026-11-04):** ответ человека возвращается резюмом
сессии вопроса — `--resume <session_id>` из `attempt_started.payload.session_id`
(он же `attempt_ended.details.session_id`), номер попытки при этом **растёт**:
ответ — новый вход, а не та же попытка, иначе учёт токенов и исходов смешался бы.
Выбор сделан по пробе 1 (`docs/PROBES.md`): три резюма подряд держат контекст
(`787` на всех трёх шагах, `local-4388c999…`), а `clarify` внутри `--oneshot`
ответа не получает (`undelivered`) — поэтому вопрос задаёт движок. Фолбэк «новый
процесс с `answer_this_attempt` в контракте» включается **только** если резюм
реально отвалится в живом прогоне и это будет записано событием; сейчас он не
используется.

### Красные пробы к слою

- убрать `WHERE leases.expires_at <= now` из взятия лизинга — падают
  `test_lease_taken_only_once` и `test_busy_stage_is_not_started_again`;
- вернуть в `fold` учёт `attempt_progress` как state-changing — падает
  `test_attempt_progress_keeps_state_and_closed_deal`;
- заменить `contract` в событии `attempt_started` на путь — падает
  `test_started_event_records_the_real_command`;
- снять уборку дерева у упавшей попытки — падает
  `test_worktree_removed_when_attempt_failed`;
- отключить гейт в `run_stage` — падают
  `test_gate_refuses_bare_done_and_puts_the_issue_into_the_reason` и
  `test_done_without_evidence_cannot_close_the_stage_this_is_the_p3_gate`
  (второй — прямой тезис фазы P3);
- поставить `hops_exhausted` выше проверки `allowed` в `routing.route` — падает
  `test_stage_without_ask_is_routing_failed_not_exhausted_chain`;
- пустить властный вопрос по объявленному дефолту (снять проверку `risky`) —
  падает `test_risky_wins_over_declared_default`;
- не писать адрес в `question_asked` — падают
  `test_routing_failed_reaches_the_journal_event` и
  `test_factual_question_is_addressed_to_the_declared_role`.

## 11. Что делать не нужно

- не перезапускать gateway ради страницы;
- не править номер версии руками — только `bin/duty-version`;
- не читать живые БД вручную, если то же видно в `/now`: он открывает их
  `?mode=ro` и отдаёт причину сбоя в `errors[]`.
