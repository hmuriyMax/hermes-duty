# Владение делом: контракт 0.0.3

Спека W5. Дополняет `docs/JOURNAL.md` (журнал событий) — здесь описано, чем duty
**владеет**, а не что наблюдает.

## Зачем

Наблюдатель без владения не заменяет kanban: чтобы его погасить, кто-то должен уметь
завести дело, положить в него спеку, ответить в комментариях, заблокировать и закрыть.
В 0.0.3 duty получает **свой** store дел; движок (аренда, спавн воркеров, retry) остаётся
на следующую версию.

## Границы версии

Внутри: создание и правка дел, комментарии, вложения (метаданные), связи родитель/ребёнок,
переходы `claim`/`block`/`unblock`/`complete`/`request_review`, всё — в своей БД.

Снаружи: нет спавна процессов, нет аренды и ретраев, нет декомпозиции, нет переноса
существующих kanban-дел. Наблюдение за kanban-делами продолжает работать как в W1.

## Хранилище

Та же БД `$HERMES_HOME/duty/duty.db`, отдельные таблицы. Владелец строки — префикс id.

```sql
CREATE TABLE IF NOT EXISTS boards (
  slug       TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
  id         TEXT PRIMARY KEY,          -- d_<hex>: свои дела
  board      TEXT NOT NULL REFERENCES boards(slug),
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
```

`deals.status` — **намерение** (жизненный цикл, которым управляют команды), а не ожидание.
Ожидание по-прежнему считается `fold`'ом журнала (`docs/JOURNAL.md`); две правды не
смешиваются: статус отвечает «где дело в процессе», fold — «почему оно стоит».

## Идентификаторы

Свои дела получают `d_<hex>`, kanban-дела остаются `t_<hex>`. Префикс — единственный
способ отличить владение от наблюдения в API, CLI и на экранах; смешивать нельзя.

Колонка `external_ref` появилась в 0.0.4. `CREATE TABLE IF NOT EXISTS` не добавляет
колонку в уже заведённую таблицу, поэтому при открытии store **на запись** схема
доводится идемпотентным `ALTER TABLE deals ADD COLUMN external_ref TEXT`; отдельного
файла миграций у плагина нет. Читающая ручка схему не доводит (смотр экрана не меняет
файл): она собирает список колонок по факту схемы, и «колонки ещё нет» читается как
«внешнего имени нет», а не как ошибка.

## Внешнее имя дела

Зачем: дело заводят не из терминала, а из тикета («Продолжить» в Jira). Чтобы
повторное нажатие находило **то же** дело, а не создавало второе, у дела есть внешнее
имя — `external_ref`.

Правила:

- **имя — просто строка.** Duty не знает про Jira: не разбирает префикс, не сверяет
  формат, не ходит в трекер. `jira:RP-14080`, `jira:RP-14080#2`, `ops:incident-42` —
  равноправные строки; что в них писать, решает тот, кто создаёт дело;
- **имя не уникально.** Уникального индекса `(board, external_ref)` нет и не будет: у
  одного тикета бывает несколько дел, и второе дело — законный факт, а не ошибка;
- **живое дело не дублируется.** Если на этой же доске есть дело с этим именем и оно
  не `done`/`archived`, `create` возвращает его: `created: false`, `existing: true`,
  причина в `reason`, нового события в журнале нет. Так повторный клик «продолжить»
  не плодит копий;
- **закрытое дело не воскрешается.** `done` и `archived` из поиска исключены: после
  паузы это продолжение, оно заводится новым делом (`created: true`) и видно отдельно
  со своим id, своей лентой и своим таймлайном. Возврат закрытого дела в работу —
  отдельное осознанное действие, а не побочный эффект создания;
- **имя ищется в пределах доски.** Тот же `external_ref` на другой доске — другое
  дело: доска это пространство работы, а не ярлык;
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
POST   /deals                    {board, title, body?, assignee?, parents?[],
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

Ответы — та же форма, что у `/deal/{board}/{id}` (дело + факты + таймлайн), чтобы
экран не знал, откуда дело: своё или наблюдаемое. К ней добавлен блок
`mutation: {events, idempotent}`: «сделано» и «уже было» — разные факты, и клиенту
не нужно угадывать их по коду возврата.

Правила:

- `POST /deals` порождает `status='todo'`; `ready` ставится, когда все родители
  `done`/`archived` (`CLOSED_STATUSES`), а дело без родителей получает `ready` сразу —
  это то же правило гейта, что в kanban, но считаем сами;
- `POST /deals` с `external_ref` ищет живое дело с этим именем на этой доске и, если
  нашёл, отвечает им же: `created: false`, `existing: true`, причина в `reason`,
  `mutation.events: 0`. Закрытое (`done`/`archived`) в поиск не попадает — с тем же
  именем заводится новое дело (`created: true`);
- `PATCH` не даёт менять `origin` и переписывать `id`; смена `status` — только через
  команды, кроме `PATCH {status: archived}`;
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
duty create --board B --title "…" [--body-file F] [--assignee ROLE] [--parent d_…]…
            [--external-ref jira:RP-14080] [--spec /abs/duty.yaml]
duty comment d_… "текст"
duty block d_… --kind needs_input "причина"
duty unblock d_… | duty complete d_… --summary "…" | duty claim d_…
duty review d_… [--reviewer ROLE] | duty changes d_… "причина"
duty attach d_… FILE | duty archive d_…
```

`archive` — это тот же `PATCH {status: archived}`, названный командой: закрытое
дело, которое убирают с доски, не должно требовать ручного `PATCH`.
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
