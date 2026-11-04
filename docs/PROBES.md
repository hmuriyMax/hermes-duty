# Пробы до P2 — измерения, а не предположения

2026-11-04, macOS `27.2`, `Apple M2 Pro`, Hermes `v0.22.5+4.ga9069c2.dirty (2026.9.24)`. Модель
попыток — `claude-agentllm-small` через локальный прокси `http://127.0.0.1:59119/v1`
(`/Users/mshchemilkin/.hermes/bin/hermes-ozon-proxy-v2.py`, PID 3019). Все пробы гонялись
в пустом временном каталоге `/tmp/p-probe.gzKq6O` (`mktemp -d`), точные команды — в
разделах ниже. Числа — фактические; где не удалось померить, так и написано.

Сводка (на неё опираются W3/W5):

| проба | вердикт |
|---|---|
| 1. продолжение по ответу | **RESUME-OK** — контекст держится на трёх резюмах подряд и после `clarify`; но `clarify` внутри `--oneshot` ответа не получает (`undelivered`), поэтому точку возврата даёт движок, а не сам процесс |
| 2. стоимость попытки | одиночный тривиальный вызов 3,19–6,43 с (медиана 5,82) и 7 880 input-токенов; **машина терпит 16 одновременных попыток без единого отказа**, дальше — только рост задержки (24–34 с) и 429, поглощённые прокси |
| 3. роль под процессом | граница путей задаётся профилем и зашита в `role-access`; каталог попытки **обязан** приходить в контракте и лежать под `~/.hermes/kanban/boards/<board>/workspaces/` — иначе read-only роль его не прочитает |

---

## Проба 1: продолжение по ответу, end-to-end

### Что мерили

Связку «процесс напечатал id → ответ пользователя → продолжение с сохранённым контекстом»:
переживает ли контекст три `--resume` подряд и переживает ли он худший случай — сессию,
которая сама задала вопрос через `clarify`.

### Как мерили

Ключи сверены с `hermes chat --help`: `-q/--query`, `--oneshot`, `--format {text,stream-json}`,
`--resume/-r <SESSION_ID>`, `--query-file PATH`. `--format stream-json` подразумевает `--quiet`
и одиночный запрос.

```bash
cd $(mktemp -d)
# 1. свежая сессия с маркером
hermes chat -p tester --format stream-json --oneshot -q "Запомни число 787. Ответь строго: 'запомнил'."
# 2-4. три резюма подряд, без подсказки числа
hermes chat -p tester --format stream-json --oneshot --resume <ID> -q "Какое число я просил запомнить? Ответь только числом."
# 5. худший случай: сессия задаёт вопрос
hermes chat -p tester --format stream-json --oneshot -q "…Обязательно вызови инструмент clarify с одним вопросом: 'В какой каталог сохранять отчёт?'…"
# 6. резюм с ответом
hermes chat -p tester --format stream-json --oneshot --resume <ID-clarify> -q "Сохраняй в /tmp/reports. Ответь двумя строками: 1) про какой проект был отчёт; 2) какой вопрос ты мне задавал дословно."
```

### Что получили

Формат потока: `{"type":"system","subtype":"init"}`, затем `text` (куски ответа), при вызове
инструментов — `tool_use` + `tool_result`, в конце `result` с `session_id`, `tokens`
(`input/output/total/cache_read/cache_write`) и `duration_ms`. **`system/init.session_id` пустой** —
id надёжно берётся из `result` (и дублируется в stderr строкой `Session: <id>`).

Шаги 1–4, одна и та же сессия `local-4388c9998bb973d9c48b4f9f3dbc278a6c794ef85116585a05e9482a94240e1b`:

| шаг | запрос | ответ | wall, с | `duration_ms` | tokens in/out/total | tool_use |
|---|---|---|---|---|---|---|
| 1 | запомни число | «запомнил» | 10,30 | 8 999 | 15 895 / 700 / 16 595 | 0 |
| 2 | резюм №1 | **787** | 4,18 | 2 624 | 23 928 / 997 / 24 925 | 0 |
| 3 | резюм №2 | **787** | 2,16 | 785 | 31 993 / 1 040 / 33 033 | 0 |
| 4 | резюм №3 | **787** | 2,40 | 1 061 | 40 090 / 1 094 / 41 184 | 0 |

Контекст не сползает: все три резюма вернули 787, id сессии на всех шагах один и тот же,
`--resume` не создаёт новую сессию. Виден линейный рост `input`: **+8 000 токенов на каждый
следующий ход** — история переотправляется целиком, третье продолжение стоит 41 тыс. input-токенов
на тривиальный вопрос.

Шаги 5–6, худший случай. `clarify` в `--oneshot` **вызывается** (в потоке есть `tool_use`
и `tool_result`) и мгновенно возвращает:

```json
{"responses": [{"question": "В какой каталог сохранять отчёт?", "choices_offered": ["/tmp/reports", "~/reports"],
  "status": "unanswered", "user_response": null}],
 "outcome": "undelivered",
 "notice": "single-query mode: no user available to answer. Pick the best choices using your own judgment, or make the most reasonable assumption you can, and continue."}
```

То есть процесс **не паркуется на вопросе** и не ждёт: хук `clarify` в одиночном режиме отвечает
сам (`_single_query_clarify_callback` / `_oneshot_clarify_callback` в
`~/.hermes/hermes-agent/hermes_cli/`). В прогоне модель после этого честно сказала «ответа нет,
отчёт не сохраняю» и попросила каталог текстом. Резюм с ответом (`local-23a6df02…`) контекст
сохранил: на вопрос «про какой проект был отчёт» вернулось «проект «Дюна»», на «какой вопрос ты
задавал» — дословно «В какой каталог сохранять отчёт?». Цена этого резюма: wall 35,63 с,
`duration_ms` 34 059, 60 151 input-токенов, 3 вызова `terminal`/`read_file` — модель на
продолжении пошла искать отчёт по файловой системе.

### Вердикт

**RESUME-OK**: id сессии достаточно как точка возврата — контекст держится и после трёх резюмов
подряд, и после `clarify`.

### Что это значит для движка

1. Точка возврата — `session_id` из события `result` (не из `system/init`). Хранить его в попытке
   и продолжать через `--resume <id>`; `--resume` не создаёт новую сессию, поэтому id стабилен
   на всю цепочку «вопрос → ответ → продолжение».
2. `clarify` внутри `--oneshot` **не является** способом задать вопрос: он возвращает `undelivered`
   и процесс идёт дальше. Вопрос обязан задавать движок (вне процесса попытки), а ответ
   возвращать резюмом. Отсюда W5 не нужен «новый процесс с файлом-входом» ради контекста —
   файл-вход нужен только чтобы передать ответ без shell-экранирования (`--query-file`).
3. Резюм не бесплатен: +8 тыс. input-токенов на ход, и продолжение может «уйти гулять» по
   инструментам (в замере 3 tool_use и 34 с вместо 2 с). Контракт попытки должен ограничивать
   `--max-turns` и `-t/--toolsets`, иначе одно продолжение стоит как пять обычных.
4. Одиночный процесс надёжно не «зависает» на вопросе — но и не сообщает движку, что вопрос был
   задан: `outcome: undelivered` виден только в `tool_result` внутри потока. Движку, который
   полагается на «процесс вернулся с вопросом», нужен свой канал (поле контракта), а не разбор
   stdout.

---

## Проба 2: стоимость одной попытки

### Что мерили

Wall time и токены тривиального вызова (`hermes chat -p tester --oneshot`, «скажи ок»),
оверхед обвязки без модели и поведение при одновременных вызовах: растёт ли время, есть ли
очередь или лимит у локального прокси `127.0.0.1:59119`, появляются ли 429/таймауты.

### Как мерили

```bash
# три последовательных вызова
for i in 1 2 3; do /usr/bin/time -p hermes chat -p tester --format stream-json --oneshot -q "скажи ок"; done
# параллельные N вызовов (fanout.sh: запуск N фоновых, wait, сбор real/duration_ms/tokens)
bash fanout.sh 2 ; bash fanout.sh 4 ; bash fanout.sh 8 ; bash fanout.sh 10 ; bash fanout.sh 16
# со стороны прокси
lsof -nP -iTCP:59119 -sTCP:LISTEN          # PID 3019, hermes-ozon-proxy-v2.py
tail -240 /private/tmp/hermes-ozon-proxy.log
grep -c "ThreadingHTTPServer" ~/.hermes/bin/hermes-ozon-proxy-v2.py
```

### Что получили

Последовательно (`/usr/bin/time -p`, событие `result`):

| прогон | wall, с | `duration_ms` | обвязка (wall − duration), мс | tokens in/out/total |
|---|---|---|---|---|
| 1 | 5,82 | 4 345 | 1 475 | 7 880 / 121 / 8 001 |
| 2 | 6,43 | 5 145 | 1 285 | 7 880 / 89 / 7 969 |
| 3 | 3,19 | 1 842 | 1 348 | 7 880 / 47 / 7 927 |
| **медиана** | **5,82** | — | 1 348 | — |

Неизменный `input` 7 880 токенов на все три прогона — это стоимость системного промпта
профиля `tester` при одном ходе; `output` 47–121 — сам ответ. Обвязка (старт процесса до
события `init` и завершение после `result`) — **1,29–1,48 с**, дальше идёт модель.
Спеку «8 с и ~15k токенов» замер не подтверждает в этой конфигурации: 3,2–6,4 с и ~7,9 тыс.
токенов; 15–16 тыс. input-токенов появились в пробе 1 на первом шаге (15 895) — почему при
том же профиле и `tool_use = 0` вышло вдвое больше, я **не установил**; числа пробы 1 и пробы 2
по этой причине не сравнимы напрямую.

Параллельно (все прогоны `rc=0`, ни одного 429/таймаута на стороне клиента):

| N | wall каждого прогона, с | суммарно |
|---|---|---|
| 2 | 5,50 / 4,90 | 5,5 с |
| 4 | 6,96 / 6,95 / 6,97 / 6,96 | 7 с |
| 8 | 13,75 / 12,33 / 6,11 / 7,69 / 6,98 / 11,17 / 7,69 / 7,67 | 14 с |
| 10 | 18,15 / 29,10 / 33,27 / 18,15 / 23,83 / 23,69 / 23,70 / 24,78 / 39,21 / 23,67 | 39 с |
| 16 | 29,12 / 27,76 / 29,11 / 24,01 / 29,13 / 32,49 / 30,31 / 29,11 / 24,31 / 33,90 / 33,89 / 33,90 / 29,09 / 23,88 / 29,09 / 27,74 | ≤ 34 с |

Прокси своего лимита не имеет: `hermes-ozon-proxy-v2.py` поднимает
`http.server.ThreadingHTTPServer` (строка 1589), то есть поток на соединение без семафора и
очереди. Ограничение приходит **от апстрима**: в его логе `/private/tmp/hermes-ozon-proxy.log`
15 записей `✅ Upstream 429 восстановился после Ns`; две записи в `12:59:26`
приходятся на прогон N=10, десять (`13:02:13`–`13:02:19`) — на N=16. Ретрай внутри прокси:
`_UPSTREAM_RETRY_BACKOFF = (1.0, 3.0)`. Лог живёт в `/private/tmp/hermes-ozon-proxy.log`
(дескрипторы 1w/2w у PID 3019), на момент замера в нём было 240 строк и он дописывался —
более ранние записи были обрезаны извне, так что 429 до 12:59 в нём нет и «сколько всего 429
было» по логу не восстановить.

### Вердикт

**16 одновременных попыток машина терпит без единого отказа; отказ не найден до 16 включительно.**
Цена — задержка: одиночный вызов 3,2–6,4 с, при 4 — ~7 с, при 8 — 6–14 с, при 10 — 18–39 с,
при 16 — 24–34 с.

### Что это значит для движка

1. Лимит одновременности задаёт не прокси и не машина, а апстрим, и он не сообщает о себе
   отказом — только задержкой. Значит распараллеливание до 4 дел движок может включать сам,
   а выше — только с явным потолком в контракте и с метрикой «сколько ждём», иначе деградация
   будет невидимой (успешные, но в 6 раз более долгие попытки).
2. Бюджет одного дела: ~7,9 тыс. input-токенов на ход + 1,3–1,5 с обвязки + резюмы по
   +8 тыс. за ход. Это число должно попасть в `stats`, иначе «стоимость попытки» останется
   оценкой из спеки.
3. 429 клиенту не доходят (прокси ретраит 1 с/3 с), поэтому попытка не должна трактовать
   долгий ответ как ошибку: таймаут попытки надо ставить заведомо выше 39 с, иначе при
   параллельной нагрузке движок начнёт убивать живые попытки.

---

## Проба 3: роль под процессом

### Что мерили

Что реально видит и может процесс попытки под конкретным профилем: есть ли shell, какие корни
путей доступны на чтение, что запрещено. Отдельно — можно ли выдать каталог попытки через
переменные окружения.

### Как мерили

```bash
# reviewer: читает плагин, файл в чужом scratch и в каталоге попытки доски
hermes chat -p reviewer --format stream-json --oneshot -q "Вызови read_file для пути <путь>. Дословно верни результат инструмента."
# reviewer + попытка выдать каталог через env
HERMES_KANBAN_TASK=t_e26bf31b HERMES_KANBAN_WORKSPACE=<каталог> hermes chat -p reviewer --format stream-json --oneshot -q "Вызови read_file …"
# reviewer: есть ли shell
hermes chat -p reviewer --format stream-json --oneshot -q "Выполни через инструмент terminal команду echo REVIEWER-SHELL-OK. Если инструмента нет — напиши NO-TERMINAL-TOOL."
# tester: shell, запрет записи, границы чтения, наследование env
hermes chat -p tester --format stream-json --oneshot -q "Выполни через terminal: echo TASK=$HERMES_KANBAN_TASK; echo WS=$HERMES_KANBAN_WORKSPACE; echo PWD=$PWD; id -un"
hermes chat -p tester --format stream-json --oneshot -q "Вызови write_file …"
HERMES_PROBE_MARKER=ENVPASS-1 HERMES_KANBAN_TASK=… hermes chat -p tester --format stream-json --oneshot -q "read_file(<чужой профиль>) и terminal: env | grep -E 'HERMES_PROBE_MARKER|HERMES_KANBAN_(TASK|WORKSPACE)' || echo NOTFOUND"
```

Исходник правил — `~/.hermes/plugins/role-access/__init__.py` (хук `pre_tool_call`),
роль берётся из имени каталога `HERMES_HOME`.

### Что получили

| профиль | shell | корни чтения | что запрещено |
|---|---|---|---|
| `reviewer` (read-only) | **нет** — ответ `NO-TERMINAL-TOOL`, `terminal` в `agent.disabled_toolsets` | `~/GolandProjects`, `~/.hermes/skills`, `<profile>/skills`, `~/.hermes/tasks`, `~/.hermes/plans`, `~/.hermes/kanban/attachments`, `~/.hermes/kanban/boards`, `<profile>/cache/spillover` (по коду ещё `$HERMES_KANBAN_WORKSPACE`, но только при заданном `$HERMES_KANBAN_TASK` — живьём не сработало, см. ниже) | всё, чего нет в allowlist: shell, browser, `computer_use`, чтение вне корней; запись (`write_file`/`patch`) — **только** `<workdir>/.duty/result.json` своей попытки по контракту движка (см. «Права ролей» в конце файла) |
| `tester` (worker) | **есть** — `terminal` отработал, `id -un` = `mshchemilkin`, backend `local`, cwd = каталог вызывающего | **ограничений нет** — прочитал `~/.hermes/profiles/implementer/cache/scratch/envprobe/marker.txt` | `write_file`, `patch`, `browser*`, `computer_use` (роль), плюс `memory`, `skill_manage`, `delegate_task`, `cronjob_manage`, `manage_catalog`, `desktop_project` |
| `implementer` (worker) | есть (эта же проба идёт под ним) | ограничений нет | те же `WORKER_DENY`, запись разрешена |
| `lead` / `researcher` / `ops` | по коду — как `reviewer` (все входят в `READONLY`); **живьём не мерил** | те же корни | то же |

Точные ответы инструментов:

- `read_file ~/.hermes/plugins/hermes-duty/desktop/plugin.js` (reviewer) →
  `{"error": "role-access: read path is outside permitted roots"}`. Плагины у read-only закрыты —
  как и записано в спеке.
- `read_file /Users/mshchemilkin/.hermes/kanban/boards/imsg-gateway/workspaces/t_65c52db8/README.final-E14.md`
  (reviewer) → файл прочитан (3 строки из 442). Каталог `kanban/boards/**` — корень, значит
  **workspace попытки доски читается read-only ролью без всякой настройки**.
- `read_file /tmp/p-probe.gzKq6O/marker.txt` (reviewer) → `outside permitted roots`.
- `read_file /Users/mshchemilkin/.hermes/profiles/reviewer/cache/scratch` (reviewer) →
  `outside permitted roots`. Сам reviewer в ответе предлагал положить файл туда — **это неверно**,
  разрешён только `<profile>/cache/spillover`.
- Тот же `/tmp`-файл с `HERMES_KANBAN_TASK=t_e26bf31b` и `HERMES_KANBAN_WORKSPACE=/tmp/p-probe.gzKq6O`
  в окружении вызывающего → снова `outside permitted roots`. Повтор с каталогом
  `~/.hermes/profiles/implementer/cache/scratch/envprobe` (оба env выставлены) → тоже отказ.
  Окружение вызывающего до хука не доходит, выдать корень через env из `hermes chat` нельзя.
- Внутри попытки (`tester`) `terminal` не видит переменных вызывающего:
  `echo $HERMES_KANBAN_TASK` / `$HERMES_KANBAN_WORKSPACE` — пусто, `HERMES_PROBE_MARKER` —
  `NOTFOUND` (`terminal.env_passthrough: []`). Каталог попытки в env передавать бессмысленно.
- Практическое следствие уже наблюдалось в чужом прогоне: `lead` в рассуждении отметил, что
  `role-access` блокирует чтение вне разрешённых корней и что корень `~/.hermes` (список профилей,
  произвольные каталоги) ему недоступен
  (`~/.hermes/kanban/boards/sx-widgets/logs/t_8c38b130.log`) — то есть read-only роль не видит
  и «очевидно нужный» ей корень.

### Вердикт

**Граница путей задаётся профилем и зашита в `role-access`; каталог попытки нужно передавать
в контракте и держать под `~/.hermes/kanban/boards/<board>/workspaces/` — только так read-only
роль его прочитает.**


**Поправка после W19 (04.11): «read-only не пишет ничего» больше не верно в одном месте.**
Право записи выдаёт не роль, а контракт попытки: read-only профиль пишет ровно
`<workdir>/.duty/result.json` своей попытки — и ничего кроме него (репозиторий, `tree/`,
`.git` остаются закрытыми). Проба 3 мерила мир **до** этой правки, поэтому её строка
«запись (`write_file`/`patch`) запрещена» относилась ко времени замера; действующая
таблица прав — раздел «Права ролей» в конце файла.
### Что это значит для движка

1. Контракт попытки обязан объявлять **каталог попытки** явным полем, а движок — создавать его
   под `~/.hermes/kanban/boards/<board>/workspaces/<task>/` (уже разрешённый корень), а не
   в `mktemp -d`, `~/…` или профильном `cache/scratch`: эти три места для `reviewer`/`lead`
   невидимы, и ревью будет «пустым» не из-за отсутствия находок, а из-за отсутствия доступа.
2. `HERMES_KANBAN_TASK`/`HERMES_KANBAN_WORKSPACE` — не механизм границы: из вызывающего
   процесса они не доходят ни до хука, ни до `terminal` попытки. Расширять глобальные корни
   плагина ради движка не нужно и нельзя — контракт должен попадать в разрешённое поддерево.
3. Роль попытки определяет не только права, но и **наличие инструментов**: под read-only
   профилем у попытки нет shell вообще (`NO-TERMINAL-TOOL`), значит «попытка с evidence-командой»
   под `reviewer` не выполнима физически — evidence должен собирать процесс с профилем из
   `WORKERS` (`tester`/`implementer`), а read-only роль получает уже готовый лог из контракта.
4. `tester` и `implementer` не ограничены по путям чтения — изоляция «попытка видит только свой
   каталог» **не обеспечивается** `role-access`; если движку нужна такая граница, её даёт
   только сам движок (chroot/контейнер/явный cwd), а не профиль.

---

## Права ролей: что можно роли и что режет `role-access`

Действующая таблица прав — по коду guard, не по памяти о пробе 3 выше. Источник —
`~/.hermes/plugins/role-access/__init__.py` (`READONLY`, `WORKERS`, `WORKER_DENY`,
`check_read`, `check_attempt_report`); профиль поверх этого решает, какие инструменты
вообще выданы (`platform_toolsets.cli` минус `agent.disabled_toolsets`).

| роль | семейство в guard | чтение | запись файлов (`write_file`/`patch`) | отчёт попытки `.duty/result.json` | shell (`terminal`) |
|---|---|---|---|---|---|
| `implementer` | `WORKERS` | guard пути не проверяет | **разрешена** (профиль выдаёт бандл `file`) | пишет как обычную запись в каталоге попытки | есть |
| `tester` | `WORKERS` | guard пути не проверяет | **запрещена**: роль в списке `tester`/`publisher`/`documenter` — guard режет `write_file`, `patch`, `browser*`, `computer_use`; правка идёт через shell | пишет **из shell** (`terminal`), потому что `write_file` роли не выдан | есть |
| `reviewer` | `READONLY` | только разрешённые корни (`check_read`: `~/GolandProjects`, `~/.hermes/skills`, `<профиль>/skills`, `~/.hermes/tasks`, `~/.hermes/plans`, `~/.hermes/kanban/attachments`, `~/.hermes/kanban/boards`, `<профиль>/cache/spillover`) | **только** `<workdir>/.duty/result.json` своей попытки (`check_attempt_report`); всё прочее — отказ | **единственная разрешённая запись.** Нужен бандл `role_file_write` в профиле: guard разрешает вызов, но инструмент должен быть выдан | **нет** (`terminal` в `agent.disabled_toolsets`) |
| `documenter` | `WORKERS` | guard пути не проверяет | **запрещена** (та же тройка) — правка через shell | пишет из shell, как `tester` | есть |

Что именно режет guard вне `report_to` (одна проверка, а не набор запретов):

1. **Read-only роль пишет только отчёт своей попытки.** Сходиться должно всё: этап,
   номер и каталог из транскрипта сессии; файл `<workdir>/.duty/in-<этап>-<n>.json`,
   записанный движком; роль/этап/номер/`workdir` в этом файле; `report_to` ровно
   `<workdir>/.duty/result.json`; каталог под `~/.hermes/kanban/boards/**`. Не сошлось —
   `role-access: роль read-only; разрешена запись только отчёта попытки: <путь>`, без
   контракта — `…но контракта попытки с report_to у сессии нет`.
2. **Репозиторий и дерево остаются закрытыми на запись даже с контрактом:** разрешён один
   файл, а не каталог. `tree/`, `.git`, код и `docs/` read-only роли не пишутся.
3. **Read-only роль не получает shell** — `terminal` не в `READ_TOOLS`, поэтому «попытка с
   evidence-командой» под `reviewer` физически неисполнима: evidence собирает процесс с
   профилем из `WORKERS` (`tester`/`implementer`), а ревьюер получает готовые файлы и
   выносит приговор.
4. **Вне allowlist — отказ, а не тишина:** `tool is not in the readonly allowlist` для
   любого инструмента read-only роли, которого нет в списке; `read path is outside
   permitted roots` для чтения вне корней; `generic dispatch is forbidden` для `tool_call`.
5. **Рабочая роль не пишет в чужой файл-источник:** у `WORKERS` guard сужает не запись, а
   инструменты — `skill_manage`, `memory`, `cronjob_manage`, `delegate_task`,
   `manage_catalog`, `desktop_project` закрыты всем рабочим ролям (`WORKER_DENY`).

Живая проверка (прогон `d_6e764042`, 04.11, журнал `attempt_progress`):

| попытка | роль | инструменты из журнала | отчёт на диске |
|---|---|---|---|
| `impl#1` | `implementer` | `terminal` 17, `write_file` 4, `read_file` 1, `patch` 1 | `…/d_6e764042-impl-1/.duty/result.json` (1 646 Б) |
| `verify#1` | `tester` | `terminal` 8, `read_file` 4, `tool_search` 2, `process_manage` 1 — **ни одного `write_file`** | `…/d_6e764042-verify-1/.duty/result.json` (1 592 Б) — написан из shell |
| `review#1` | `reviewer` | `search_files` 14, `read_file` 10, `write_file` 1 — **ни одного `terminal`** | `…/d_6e764042-review-1/.duty/result.json` (6 998 Б) |

То есть `reviewer` пишет **только** `.duty/result.json` (одна запись за попытку), а
`tester` вообще не имеет `write_file` и кладёт отчёт из `terminal`. Профиль `reviewer` при
этом выдаёт бандл `role_file_write` в `platform_toolsets.cli` и держит `terminal` в
`agent.disabled_toolsets` — без первого отчёт было бы нечем писать, без второго ревьюер
имел бы shell.

**Чего эта таблица не гарантирует.** Guard — это проверка имён инструментов и аргументов,
а не изоляция ОС: у `WORKERS` есть shell, и произвольная команда остаётся возможной
(`role-access/README.md`, «Limits — not a sandbox»). Изоляция «попытка видит только свой
каталог» профилем не обеспечивается — её даёт только сам движок (cwd/дерево попытки).
