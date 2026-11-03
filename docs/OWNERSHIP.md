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

## События

Каждая мутация пишет строку дела и событие **в одной транзакции** (иначе журнал и store
разъедутся). Используются уже определённые `kind`:

| команда | событие |
|---|---|
| создать дело | `created` (`source='cli'/'api'`) |
| блокировка | `blocked` (`block_kind`, `reason`) |
| разблокировка | `unblocked` |
| взять в работу | `claimed` (`owner`) |
| завершить | `completed` |
| на ревью / вернуть | `review_requested`, `changes_requested` (новые kind — добавить в контракт журнала вместе с тестом) |
| комментарий | `commented` (`author`, `len`) |

`idempotency_key` для мутаций: `cli:<deal_id>:<kind>:<sha1(аргументы)>`, для API —
`api:<deal_id>:<kind>:<sha1(тело запроса)>`; повторный тот же вызов не плодит событий.

## API

Роуты дополняют существующие; все — под токеном сессии, как остальные:

```
POST   /deals                    {board, title, body?, assignee?, parents?[]}   → {deal}
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

- `POST /deals` порождает `status='todo'`; `ready` ставится, когда все родители `done`
  (это то же правило гейта, что в kanban, но считаем сами);
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
