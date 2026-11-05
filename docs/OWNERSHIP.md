# Владение делом: контракт 0.0.6

Спека W5. Дополняет `docs/JOURNAL.md` (журнал событий) — здесь описано, чем duty
**владеет**, а не что наблюдает.

## Зачем

Наблюдатель без владения не заменяет kanban: чтобы его погасить, кто-то должен уметь
завести дело, положить в него спеку, ответить в комментариях, заблокировать и закрыть.
С 0.0.3 duty владеет **своим** store дел; с 0.0.4 движок берёт этапы, лизинг и попытки
поверх того же store.

## Границы версии

Внутри: создание и правка дел, комментарии, вложения (метаданные), связи родитель/ребёнок,
переходы `claim`/`block`/`unblock`/`complete`/`request_review`, лизинг этапов движка —
всё в своей БД.

Снаружи: переноса существующих kanban-дел нет. Карточки kanban с 0.0.6 — только
наблюдение (`GET /cards`), делом они не бывают и в свой store не попадают.

**Досок в этой модели нет (0.0.6).** Дело адресуется своим `id` (`d_<hex>`), а
группируется по `repo` — свободной строке, свойству дела. Доска была тем, чем дело
попадало в срез, и вместе с ней ушли таблица `boards`, колонка `deals.board` и
адресация `/deal/{board}/{id}`.

## Хранилище

Та же БД `$HERMES_HOME/duty/duty.db`, отдельные таблицы. Владелец строки — префикс id.

```sql
CREATE TABLE IF NOT EXISTS deals (
  id         TEXT PRIMARY KEY,          -- d_<hex>: свои дела
  repo       TEXT,                      -- группировка (не адрес); NULL = не назван
  title      TEXT NOT NULL,
  body       TEXT NOT NULL DEFAULT '',
  assignee   TEXT,                      -- профиль-роль; NULL = не назначено
  status     TEXT NOT NULL,             -- lifecycle: todo|ready|running|blocked|review|done|archived
  origin     TEXT NOT NULL DEFAULT 'duty',  -- duty | kanban (только для чтения)
  external_ref TEXT,                    -- внешнее имя дела (jira:RP-14080); не уникально
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deal_links (
  parent_id TEXT NOT NULL REFERENCES deals(id),
  child_id  TEXT NOT NULL REFERENCES deals(id),
  PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS comments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id    TEXT NOT NULL REFERENCES deals(id),
  author     TEXT NOT NULL,             -- профиль или 'max'
  body       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  deal_id      TEXT NOT NULL REFERENCES deals(id),
  filename     TEXT NOT NULL,
  path         TEXT NOT NULL,           -- абсолютный путь к файлу
  content_type TEXT,
  size_bytes   INTEGER NOT NULL,
  created_at   INTEGER NOT NULL
);

-- Лизинг этапа движка (0.0.4): кто ведёт этап и до какого срока. Строка одна на
-- пару (дело, этап) — это и есть взаимное исключение двух процессов.
CREATE TABLE IF NOT EXISTS leases (
  deal_id    TEXT NOT NULL REFERENCES deals(id),
  stage      TEXT NOT NULL,
  attempt    INTEGER NOT NULL,
  pid        INTEGER NOT NULL,
  taken_at   INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (deal_id, stage)
);
```

Индексы: `deals_repo(repo, status)`, `comments_deal(deal_id, id)`,
`attachments_deal(deal_id, id)`. `deals_board` снимается пересборкой таблицы, а на
базе, где он остался, — `DROP INDEX IF EXISTS` при открытии на запись.

Схема доводится при открытии на запись: `CREATE TABLE IF NOT EXISTS` колонку в
заведённую таблицу не добавляет, файла миграций у плагина нет, поэтому `deals._migrate`
пересобирает таблицу (`deals_v6`: создать → скопировать → бросить → переименовать) в
явном `BEGIN IMMEDIATE`. Повторный запуск после сбоя ничего не ломает: остаток
прошлого прогона снимается `DROP TABLE IF EXISTS` до `CREATE`.

`deals.status` — **намерение** (жизненный цикл, которым управляют команды), а не ожидание.
Ожидание по-прежнему считается `fold`'ом журнала (`docs/JOURNAL.md`); две правды не
смешиваются: статус отвечает «где дело в процессе», fold — «почему оно стоит».

## Идентификаторы

Дела получают `d_<hex>` — других дел в duty 0.0.6 нет. Префикс `t_<hex>` остался
адресом **карточки kanban**: он не принимается там, где ждут дело (400 с
объяснением), и наблюдение карточек (`GET /cards`) его не выдаёт за адрес дела.
Смешивать нельзя.

Схема доводится при открытии store **на запись** — файла миграций у плагина нет.
`CREATE TABLE IF NOT EXISTS` не добавляет колонку в уже заведённую таблицу, поэтому
`deals._migrate` пересобирает `deals` (0.0.6): `deals_v6` создаётся, строки копируются
с переносом значения старой колонки `board` в новую `repo`, старая таблица бросается,
новая переименовывается. Всё — в явном `BEGIN IMMEDIATE`, с `DROP TABLE IF EXISTS
deals_v6` до `CREATE`: остаток прерванного прогона снимается повторным запуском, а не
руками. Читающая ручка схему не доводит (смотр экрана не меняет файл): она собирает
список колонок по факту схемы, и «колонки ещё нет» читается как «значения нет», а не
как ошибка.

## Внешнее имя дела

Зачем: дело заводят не из терминала, а из тикета («Продолжить» в Jira). Чтобы
повторное нажатие находило **то же** дело, а не создавало второе, у дела есть внешнее
имя — `external_ref`.

Правила:

- **имя — просто строка.** Duty не знает про Jira: не разбирает префикс, не сверяет
  формат, не ходит в трекер. `jira:RP-14080`, `jira:RP-14080#2`, `ops:incident-42` —
  равноправные строки; что в них писать, решает тот, кто создаёт дело;
- **имя не уникально.** Уникального индекса `(repo, external_ref)` нет и не будет: у
  одного тикета бывает несколько дел, и второе дело — законный факт, а не ошибка;
- **живое дело не дублируется.** Если в этом же `repo` есть дело с этим именем и оно
  не `done`/`archived`, `create` возвращает его: `created: false`, `existing: true`,
  причина в `reason`, нового события в журнале нет. Так повторный клик «продолжить»
  не плодит копий;
- **закрытое дело не воскрешается.** `done` и `archived` из поиска исключены: после
  паузы это продолжение, оно заводится новым делом (`created: true`) и видно отдельно
  со своим id, своей лентой и своим таймлайном. Возврат закрытого дела в работу —
  отдельное осознанное действие, а не побочный эффект создания;
- **имя ищется в пределах репозитория.** Тот же `external_ref` в другом `repo` — другое
  дело: один тикет может идти в двух репозиториях, и это два разных дела. `repo IS NULL`
  — своя область «репозиторий не назван», а не «любая»: иначе дело без repo
  перехватывало бы повтор из названного репозитория (`live_deal_by_ref`);
- **срок у имени есть, если его задали.** `dedup_window` (секунды) ограничивает поиск
  свежими делами: живое дело старше окна не переиспользуется — заводится новое. По
  умолчанию окна нет и любое живое дело с этим именем считается тем же;
- **связь с тикетом видна в журнале.** `created.payload.external_ref` пишется вместе
  со снимком дела, поэтому таймлайн отвечает «из какого тикета пришло дело», а не
  только строка store. В карточке дела имя отдаётся полем `external_ref`.

## События

Каждая мутация пишет строку дела и событие **в одной транзакции** (иначе журнал и store
разъедутся). Виды событий и их правила fold определены в `docs/JOURNAL.md` («Набор
событий») — здесь только то, какая команда какой вид пишет. Виды `review_requested`,
`changes_requested`, `collected`, `gate_opened`, `gate_closed`, `patched`, `commented`
и `attached` заведены вместе с W5 и живут в контракте журнала; до W5 их не было ни в
одном документе.

| команда | событие |
|---|---|
| создать дело | `created` (`source='cli'/'api'`, `external_ref`); плюс `gate_opened` с `cause: "created"`, если все родители уже закрыты |
| правка спецификации (`PATCH` без `status`) | `patched` (`fields`) |
| архив (`PATCH {status: archived}`) | `completed` со `status='archived'` |
| блокировка | `blocked` (`block_kind`, `reason`) |
| разблокировка | `unblocked` |
| взять в работу | `claimed` (`owner`) |
| завершить | `completed` |
| на ревью / вернуть | `review_requested`, `changes_requested` (`reviewer`, `summary` / `reason`) |
| вердикт ревью и закрытие | `collected`, `completed` (одной транзакцией) |
| комментарий | `commented` (`author`, `len`) |
| вложение (метаданные) | `attached` (`filename`, `size_bytes`, `content_type`) |
| переход гейта ребёнка (пишет мутация родителя — или `create`, когда гейт открыт в момент создания) | `gate_opened` / `gate_closed` (`from`, `to`, `parent`; у перехода при создании ещё `cause`) |

Закрытость дела считается по последнему **state-changing** событию журнала
(`completed`), а виды из набора `journal.NON_STATE_KINDS` — `commented`, `attached`,
`patched`, `question_answered` — её не снимают: правило fold —
`docs/JOURNAL.md`, правила 2 и 12. Поэтому комментарий, вложение или поздний ответ на
вопрос по закрытому делу остаются фактом таймлайна, а не возвратом в работу.

Отдельно — **кто пишет факты движка** (0.0.5). Кроме мутаций дел, в журнал пишет
движок (`dashboard/runner.py`), и только он:

| факт | кто пишет | что означает |
|---|---|---|
| `attempt_started` / `attempt_progress` / `attempt_ended` | движок | попытка этапа: спавн, шаги, вердикт (в том числе `refused: true` — попытку не поднимали) |
| `question_asked` / `question_answered` | `ask()` (канал вопросов) | вопрос человеку и найденный ответ из пула |
| `published` | движок, **после** `git ls-remote` | заявленный публикующим этапом `sha` действительно есть в origin; в payload — `verified_by`, `ref` |

`published` не пишет ни исполнитель, ни CLI: `published` в `result.json` — заявка, и
фактом журнала она становится только после сверки с origin
(`dashboard/runner.published_claim`). Вид входит в `NON_STATE_KINDS`: он не двигает ни
этап, ни закрытость дела — этап закрыт своим `attempt_ended`.

Граница публикации — в обе стороны:

- **`bin/duty` не публикует и не пушит.** Ни одна команда CLI не запускает `git push`
  и не пишет `published`: он только читает факт из журнала и печатает его
  («опубликовано: sha · ветка · MR», колонка `ПУБЛИКАЦИЯ` в `/now`). Публикующие
  префиксы команд (`git push`, `git commit`) запрещены и как `evidence.cmd`, и как
  `kind: shell cmd` — одна и та же константа `FORBIDDEN_CMD_PREFIXES`
  (`dashboard/spec.py`).
- **Движок тоже не публикует.** Публикацию делает попытка профиля с ролью из
  `PUBLISHING_ROLES` (`publisher`, `deployer`); движок перед спавном сверяет «одобрен
  commit X, в дереве Y» (`dashboard/publish.py`), а после отчёта подтверждает
  заявленный sha через `git ls-remote`.
- **Заявка исполнителя — его слово, а не факт.** Она живёт в его `result.json`
  (`published: {sha, branch, mr_url}`) и рядом в `claimed_sha` события `published`;
  фактом (`sha` — полный из `ls-remote`, `ref`, `verified_by`) её делает движок.
- **`question_asked` тоже пишет движок, а не исполнитель.** Попытка задаёт вопрос
  отчётом (`result.json`), а факт журнала появляется, когда вопрос действительно
  создан в пуле — единственным каналом `ask()`; у гейта (`kind: gate`) его зовёт
  движок, у остальных путей — CLI/API мутация. Ответ находит `ask.sync_answers`
  и пишет `question_answered`.

`idempotency_key` для мутаций: `cli:<deal_id>:<kind>:<sha1(аргументы)>`, для API —
`api:<deal_id>:<kind>:<sha1(тело запроса)>`; повторный тот же вызов не плодит событий.
У `gate_closed` ключ считается от причины перехода, а не от аргументов:
`<source>:<child_id>:<kind>:sha1(<idempotency_key события родителя>|<child_id>|<kind>)`,
поэтому повтор той же причины перехода не плодит событий, а новый переход родителя даёт
новый ключ. У `gate_opened` от `create` причины-события родителя нет: ключ строится по
общим аргументам события — `<source>:<child_id>:gate_opened:sha1({cause: "created",
parents})` (`event_key()`), а родитель в payload — с самым свежим `updated_at` (при
равенстве — по `id`): при обычном ходе это последний закрывший родитель, но `updated_at`
двигают и комментарий, и `patch`.

## API

Роуты дополняют существующие; все — под токеном сессии, как остальные:

```
POST   /deals                    {repo?, title, body?, assignee?, parents?[],
                                  spec?, external_ref?, dedup_window?}            → {deal, created, existing?, reason?}
PATCH  /deals/{id}               {title?, body?, assignee?, status?}
POST   /deals/{id}/comment       {author, body}
POST   /deals/{id}/block         {kind, reason}
POST   /deals/{id}/unblock       {}
POST   /deals/{id}/claim         {owner}
POST   /deals/{id}/complete      {summary, review?, reviewer?}
POST   /deals/{id}/review        {reviewer?, summary?}
POST   /deals/{id}/changes       {reason, reviewer?}
POST   /deals/{id}/attach        multipart: file  → {attachment}
```

Ответы — та же форма, что у `GET /deal/{deal_id}` (дело + факты + таймлайн), чтобы
экран не знал, откуда дело: своё или наблюдаемое. К ней добавлен блок
`mutation: {events, idempotent}`: «сделано» и «уже было» — разные факты, и клиенту
не нужно угадывать их по коду возврата.

Правила:

- `POST /deals` порождает `status='todo'`; `ready` ставится, когда все родители
  `done`/`archived` (`CLOSED_STATUSES`), а дело без родителей получает `ready` сразу —
  это то же правило гейта, что в kanban, но считаем сами;
- `POST /deals` берёт `repo` из спеки, если он там назван, иначе из явного `repo`;
  не назван ни там, ни там — `NULL`, и дело попадает в группу «без репозитория».
  Это группировка, а не адрес: найти дело по `repo` нельзя;
- `POST /deals` отказывает на `board` словами: доски нет, дело адресуется своим id,
  группировка задаётся `repo`;
- `POST /deals` с `external_ref` ищет живое дело с этим именем в этом же `repo` и, если
  нашёл, отвечает им же: `created: false`, `existing: true`, причина в `reason`,
  `mutation.events: 0`. Закрытое (`done`/`archived`) в поиск не попадает — с тем же
  именем заводится новое дело (`created: true`);
- `PATCH` не даёт менять `origin` и переписывать `id`; `repo` тоже не меняется —
  это свойство дела, заданное при создании, и `PATCH {repo}` отвечает отказом с
  причиной, а не молчаливым игнорированием; смена `status` — только через команды,
  кроме `PATCH {status: archived}`;
- `complete` без `review` закрывает дело; `complete {review: true}` и `/review` —
  сдача в ревью; `/changes` возвращает на доработку с обязательной причиной;
- `archived` — конечное состояние: команды жизни (`claim`/`block`/`complete`/
  `review`/`changes`/`unblock`) по архивному делу отвечают ошибкой входа, а не
  воскрешают его молча;
- повтор того же вызова не плодит событий (`mutation.events: 0`,
  `mutation.idempotent: true`), но повтор отличается от «делать нечего»: последнее —
  ошибка входа с причиной;
- ошибки — как в 0.0.2: `errors[]` с причиной, никаких 500 на битом входе.

## CLI

```
duty create [--repo R] --title "…" [--body-file F] [--assignee ROLE] [--parent d_…]…
            [--external-ref jira:RP-14080] [--spec /abs/duty.yaml]
duty comment d_… "текст"
duty block d_… --kind needs_input "причина"
duty unblock d_… | duty complete d_… --summary "…" | duty claim d_…
duty review d_… [--reviewer ROLE] | duty changes d_… "причина"
duty attach d_… FILE | duty archive d_…
duty decide d_… continue|stop | duty close d_… [--reason "…"]
```

`--board` у CLI нет ни у одной команды: дело адресуется своим id, а `--repo` задаёт
группировку (пустая строка — дела без репозитория). `archive` — это тот же
`PATCH {status: archived}`, названный командой: закрытое дело не должно требовать
ручного `PATCH`.
`--json` печатает ответ роута дословно (включая `mutation`), человеческий вывод
называет дело, статус, ожидание и число записанных событий.

У `create --json` к форме роута добавлен один ключ — `deal_id` (у роута это поле
зовётся `id`): скрипты берут id оттуда. Рядом уже есть `created` и, при попадании в
живое дело, `existing: true` с причиной; человеческий вывод в этом случае говорит
«ДЕЛО УЖЕ ЕСТЬ» и называет, что нового дела не появилось.

CLI и API ходят в один модуль (как W3): второго набора правил быть не должно.

## Инварианты (тесты обязательны)

1. Ни одной записи в `kanban.db`/`state.db`: mtime до и после мутаций совпадает.
2. Мутация атомарна: сбой записи события откатывает строку дела (и наоборот).
3. Повторный вызов с теми же аргументами → нет нового события.
4. `origin='kanban'` строки в `deals` не создаются: наблюдение остаётся в журнале.
5. Свой `d_…` id не принимается там, где ждут `t_…`, и наоборот (валидация префикса).
6. UI не изобретает состояние: экран рисует `status` + fold, ничего не вычисляя сам.

## Что НЕ входит

- Аренда, спавн воркеров, учёт попыток, retry/reap, декомпозиция, triage — следующий шаг.
- Перенос существующих kanban-дел в свой store.
- Удаление вложений с диска (только метаданные и `attach`).
- Нотификации по своим делам сверх `ask()`.
