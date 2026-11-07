/**
 * Hermes Duty — desktop-плагин «Дела»: страница /duty (экран «сейчас» + попытки).
 *
 * Форма — как у hermes-tickets: `export default { id, name, register(ctx) }`,
 * страница в `area: 'routes'`, пункт в `area: 'sidebar.nav'`. Данные — ТОЛЬКО
 * через `ctx.rest('/now')`, `ctx.rest('/deals')`, `ctx.rest('/deal/{deal_id}')` и
 * `ctx.rest('/duty/{deal_id}/attempts')`: прямого fetch и шелла из UI нет, ядро не
 * импортируется, файлы плагин не читает.
 *
 * Дело адресуется только своим id `d_<hex>` (0.0.6: доски нет). Классификация
 * состояний — не здесь: страница рисует `waiting.state` и `waiting.evidence` ровно
 * как их отдал бэкенд (`dashboard/plugin_api.py`, `WAITING_PRIORITY`). Логика UI —
 * группировка по `repo`, форматирование чисел и локальные фильтры; вся геометрия и
 * вычисления — в `desktop/model.js` (вшит ниже блоком `duty:model-start`).
 *
 * UI собран без JSX (файл грузится нескомпилированным, см. kanban-graph).
 */
import { Button, Codicon, Input, Streamdown } from '@hermes/plugin-sdk'
import { createElement as h, useCallback, useEffect, useMemo, useRef, useState } from 'react'

const ID = 'hermes-duty'
const PATH = '/duty'
const REFRESH_MS = 10_000
// «Здоровье» — агрегат по всей истории ранов: он не меняется за секунды, а скан
// всех досок дороже среза «сейчас», поэтому обновляется реже.
const STATS_REFRESH_MS = 30_000
const SEARCH_DEBOUNCE_MS = 240
const REQUEST_TIMEOUT_MS = 20_000

// Экраны страницы, в порядке спеки: «Сейчас» — срез, «Завершённые» — список
// закрытых дел, «Дело» — спецификация и таймлайн попыток, «Почему стоит» — тот
// же срез с объяснением, «Здоровье» — агрегат по ранам. «Дело» открывается из
// любой строки, но и сам по себе таб: экран, достижимый только кликом по строке,
// на своём месте не читается.
const TABS = [
  ['now', 'Сейчас'],
  ['deals', 'Завершённые'],
  ['deal', 'Дело'],
  ['why', 'Почему стоит'],
  ['health', 'Здоровье'],
]

// Последнее открытое дело: таб «Дело» возвращает к нему, а не к пустоте.
let lastDeal = null

// ctx.rest, забинденный в register().
let rest = null

// duty:model-start — генерируется bin/duty-model-sync из desktop/model.js; не править руками
/**
 * Hermes Duty — шейпер данных страницы /duty (чистые функции, без React и DOM).
 *
 * Здесь НЕТ классификации состояний и нет копии приоритета бэкенда: единственный
 * источник таксономии — `dashboard/plugin_api.py` (`WAITING_PRIORITY`,
 * `waiting.state`). `/now` уже отдаёт дела отсортированными по приоритету, поэтому
 * группы строятся в порядке первого появления состояния в ответе — страница
 * ничего не пересортировывает и не выводит состояние из фактов. `STATE_META`
 * ниже — только подписи и цвета бейджей для известных состояний; незнакомое
 * состояние рисуется своим именем серым.
 *
 * Группировка списка — по `repo` (0.0.6: доски нет): дело адресуется своим
 * `d_<hex>`, а «чей это рой» показывает репозиторий. Дела без репозитория идут
 * последней группой `без репозитория`; порядок внутри группы — ответ бэкенда.
 * Тот же блок `repos` считает CLI, поэтому числа дел и групп совпадают на вебе,
 * десктопе и в терминале.
 *
 * Два набора берутся из ответа, а не дублируются тут: порядок состояний — по
 * ключам `by_state` (чипы фильтра), набор «падений» — по `crash_outcomes`
 * (красный тон исхода). Так расширение набора на бэкенде не разъезжается с UI.
 *
 * ВАЖНО: этот файл не импортируется из `desktop/plugin.js`. Загрузчик
 * десктопных плагинов исполняет `plugin.js` как blob-модуль и отказывает любому
 * спекулятору вне `@hermes/plugin-sdk` / `react*` (включая относительные пути),
 * поэтому тело модуля до строки `export {` дословно вшито в `plugin.js` между
 * маркерами `duty:model-start` / `duty:model-end`. Синхронизация —
 * `bin/duty-model-sync`, расхождение ловит `tests/model.test.mjs`.
 */

// Подпись и тон бейджа по состоянию — только презентация, не правила. Подпись
// `wait_human` намеренно не говорит «человека»: факт ставится по любому живому
// вопросу пула, включая вопрос агенту (`origin='improver'` в evidence).
// `no_signal` — серый, а не зелёный: дело со `status=running` без рана, pid и
// heartbeat не работает, у него просто нет живого сигнала.
const STATE_META = {
  wait_human: { label: 'ждёт ответа', tone: 'yellow' },
  capability: { label: 'нужен доступ', tone: 'purple' },
  dead_lease: { label: 'лизинг истёк', tone: 'red' },
  crashed: { label: 'упало', tone: 'red' },
  working: { label: 'работает', tone: 'green' },
  no_signal: { label: 'нет сигнала', tone: 'gray' },
  wait_slot: { label: 'ждёт слот', tone: 'gray' },
  wait_gate: { label: 'ждёт гейт', tone: 'gray' },
}

// Подпись `wait_human` по `human_kind` бэкенда: `question` — есть живой вопрос,
// на который можно ответить, `status` — вопроса нет, решение принимается по
// статусу. Одна подпись на оба случая возвращала бы «ждёт ответа» блокировке без
// вопроса, пока шапка в том же кадре пишет «0 вопросов + 1 блокировка».
const WAIT_HUMAN_LABEL = {
  question: 'ждёт ответа',
  status: 'ждёт решения',
}

const TONE_COLOR = {
  yellow: 'var(--ui-yellow)',
  purple: 'var(--ui-purple)',
  red: 'var(--ui-red)',
  green: 'var(--ui-green)',
  gray: 'var(--ui-text-tertiary)',
  blue: 'var(--ui-blue)',
}

// Тон исхода попытки: успех — зелёное, отложенное — жёлтое. Падений здесь нет:
// их набор приходит с бэкенда (`crash_outcomes`), чтобы красный тон не разъехался
// с правилами сервера.
// Исход → тон. `failed` — собственный исход попытки, он красный без всякого
// набора «падений»: без этой записи провал попадал в «незнакомый исход» и
// выглядел серым. Краши (`crashed`, `gave_up`) по-прежнему узнаются только из
// `crash_outcomes` ответа — UI их не угадывает.
const OUTCOME_TONE = {
  done: 'green',
  completed: 'green',
  succeeded: 'green',
  failed: 'red',
  rate_limited: 'yellow',
  reclaimed: 'yellow',
}

const LEASE_LABEL = {
  live: 'жив',
  expired: 'истёк',
  none: 'нет',
  unknown: 'неизвестен',
}

/**
 * Мета состояния; неизвестное состояние показываем как есть серым.
 *
 * `humanKind` уточняет только `wait_human`: «ждёт ответа» — есть живой вопрос,
 * «ждёт решения» — вопроса нет. Остальные состояния от `human_kind` не зависят.
 */
function stateMeta(state, humanKind) {
  const key = String(state || '')
  const meta = STATE_META[key] || { label: key || '—', tone: 'gray' }
  if (key === 'wait_human' && WAIT_HUMAN_LABEL[String(humanKind || '')]) {
    return { label: WAIT_HUMAN_LABEL[String(humanKind)], tone: meta.tone }
  }
  return meta
}

/** Русская форма числа: `1 вопрос`, `2 вопроса`, `5 вопросов`. */
function plural(n, one, few, many) {
  const tail = Math.abs(Number(n) || 0) % 100
  const last = tail % 10
  if (tail > 10 && tail < 20) return many
  if (last === 1) return one
  if (last >= 2 && last <= 4) return few
  return many
}

/**
 * Сколько дел ждёт человека и в каком виде — по `waiting.human_kind` дел.
 *
 * Это два разных «нужен человек»: `question` — есть живой вопрос, на который
 * можно ответить, `status` — вопроса нет, решение принимается по статусу
 * (блок, разбор, ревью, мердж). Считать их одним числом значит обещать вопрос,
 * которого нет, поэтому UI показывает «N вопросов + M блокировок».
 */
function humanCounts(deals) {
  const counts = { question: 0, status: 0 }
  for (const deal of deals || []) {
    const kind = deal && deal.humanKind
    if (kind === 'question' || kind === 'status') counts[kind] += 1
  }
  return counts
}

/** Строка «N вопросов + M блокировок» для шапки экрана «Сейчас». */
function humanLine(counts) {
  const questions = Number(counts && counts.question) || 0
  const statuses = Number(counts && counts.status) || 0
  return `${questions} ${plural(questions, 'вопрос', 'вопроса', 'вопросов')} + ${statuses} ${plural(
    statuses,
    'блокировка',
    'блокировки',
    'блокировок',
  )}`
}

// Подпись группы «репозиторий не назван»: NULL в `repo` — законное состояние
// (`dashboard/plugin_api.py`, `_repos_of`), и в списке оно называется словами, а не
// пустой строкой. Слово то же, что у CLI (`dashboard/cli.py`, WITHOUT_REPO_LABEL):
// терминал и экран обязаны называть одну группу одинаково.
const WITHOUT_REPO_LABEL = 'без репозитория'

// Значение фильтра «все репозитории»: путь репозитория не бывает `*`, а пустая
// строка занята группой «без репозитория» — ровно так же её называет роут
// (`?repo=` — только дела без репозитория, docs/RUNBOOK.md).
const ALL_REPOS = '*'

/** Подпись репозитория: путь как есть, дела без него — «без репозитория». */
function repoLabel(repo) {
  return String(repo || '') || WITHOUT_REPO_LABEL
}

/**
 * Короткое имя репозитория для шапки: последний сегмент пути.
 *
 * В store репозиторий живёт абсолютным путём (`/Users/…/plugins/hermes-duty`), и
 * этот путь как кикер заголовка — шум: человеку нужно имя. Полный путь остаётся
 * в подсказке, `repoLabel` не меняется — по нему группируются дела.
 */
function repoShort(repo) {
  const text = String(repo || '').trim()
  if (!text) return WITHOUT_REPO_LABEL
  const tail = text.replace(/[\\/]+$/, '').split(/[\\/]/).pop()
  return tail || text
}

/** Имя файла без каталогов: полный путь — в подсказке, здесь только хвост. */
function fileLabel(path) {
  const text = String(path || '').trim()
  if (!text) return ''
  const tail = text.replace(/[\\/]+$/, '').split(/[\\/]/).pop()
  return tail || text
}

/** Возраст/длительность в человекочитаемом виде: `45 с`, `12 мин`, `3 ч`, `2 д`. */
function fmtAge(sec) {
  if (sec === null || sec === undefined || sec === '') return '—'
  const value = Number(sec)
  if (!Number.isFinite(value)) return '—'
  const s = Math.max(0, Math.floor(value))
  if (s < 60) return `${s} с`
  if (s < 3600) return `${Math.floor(s / 60)} мин`
  if (s < 86400) return `${Math.floor(s / 3600)} ч`
  return `${Math.floor(s / 86400)} д`
}

/** Заголовок одной строкой с многоточием: ровно не длиннее `max` символов. */
function fmtTitle(title, max) {
  const limit = Number.isFinite(Number(max)) && Number(max) > 4 ? Math.floor(Number(max)) : 64
  const text = String(title ?? '').replace(/\s+/g, ' ').trim()
  if (!text) return '—'
  if (text.length <= limit) return text
  return `${text.slice(0, limit - 1).trimEnd()}…`
}

/** Время события как ЧЧ:ММ:СС — попытки идут минутами, секунды нужны для порядка. */
function fmtClock(at) {
  if (!at) return '—'
  const dt = new Date(Number(at) * 1000)
  if (Number.isNaN(dt.getTime())) return '—'
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`
}

/** Одна строка таблицы «сейчас» из дела ответа `/now`. */
function shapeDeal(deal) {
  const d = deal && typeof deal === 'object' ? deal : {}
  const attempt = d.attempt && typeof d.attempt === 'object' ? d.attempt : null
  const lease = d.lease && typeof d.lease === 'object' ? d.lease : {}
  const action = d.last_action && typeof d.last_action === 'object' ? d.last_action : {}
  const waiting = d.waiting && typeof d.waiting === 'object' ? d.waiting : {}
  const stage = d.stage && typeof d.stage === 'object' ? d.stage : null
  const humanKind = String(waiting.human_kind || '')
  const meta = stateMeta(waiting.state, humanKind)
  const pidAlive = attempt ? attempt.pid_alive : null
  const leaseState = String(lease.state || 'none')
  const attemptAge = attempt && attempt.age_sec !== undefined ? attempt.age_sec : null
  const lastAge = action.age_sec !== undefined ? action.age_sec : null
  const repo = String(d.repo || '')
  // «pid не записан» и «состояние pid неизвестно» (`os.kill` вернул OSError) — разные факты.
  let attemptNote = 'нет попытки'
  if (attempt) {
    attemptNote = attempt.pid ? 'состояние pid неизвестно' : 'pid не записан'
    if (pidAlive === true) attemptNote = 'pid жив'
    if (pidAlive === false) attemptNote = 'pid мёртв'
    // Часть фактов попытки бэкенд добрал из карточки, а не из записи рана
    // (`attempt.source`): подпись не должна звучать как факт рана.
    if (attempt.source === 'task') attemptNote = `${attemptNote}, факты карточки`
  }
  return {
    // Репозиторий дела (0.0.6): доски нет, «чей это рой» показывает группировка.
    // Пусто — законное состояние «без репозитория», подпись даёт `repoLabel`.
    repo,
    repoLabel: repoLabel(repo),
    id: String(d.id || ''),
    title: fmtTitle(d.title),
    titleFull: String(d.title || ''),
    assignee: String(d.assignee || '—'),
    status: String(d.status || '—'),
    state: String(waiting.state || ''),
    stateLabel: meta.label,
    tone: meta.tone,
    evidence: String(waiting.evidence || ''),
    // `human_kind` бэкенда: `question` — есть живой вопрос, `status` — человек
    // нужен, но вопроса нет (`waiting.human_kind`, контракт docs/JOURNAL.md).
    // Пустое значение — состояние не про ожидание человека. Подпись бейджа тоже
    // считается по нему (см. `stateMeta`), иначе «ждёт ответа» звучит у блокировки
    // без вопроса.
    humanKind,
    attemptAge,
    attemptAgeText: fmtAge(attemptAge),
    attemptNote,
    attemptRunId: attempt ? (attempt.run_id ?? null) : null,
    leaseState,
    leaseText: LEASE_LABEL[leaseState] || leaseState,
    leaseExpiresIn: lease.expires_in_sec ?? null,
    lastActionAge: lastAge,
    lastActionAgeText: fmtAge(lastAge),
    lastActionKind: String(action.kind || '—'),
    lastActionSummary: String(action.summary || ''),
    // Этап и номер попытки из журнала (`/now.stage`): «что сейчас» видно в
    // строке, не открывая дело. Пусто — ни одной попытки у дела не было, и
    // подставлять «этап 0» значило бы выдумать граф.
    stageId: stage ? String(stage.id || '') : '',
    stageAttempt: stage ? Number(stage.attempt) || 0 : 0,
    stageLive: stage ? stage.live === true : false,
    stageText: stage
      ? `${String(stage.id || '—')} · попытка ${Number(stage.attempt) || 0}${stage.live === true ? ' · идёт' : ''}`
      : '',
    search: `${d.repo || ''} ${d.id || ''} ${d.title || ''} ${d.assignee || ''} ${waiting.state || ''}`.toLowerCase(),
  }
}

/** Сколько дел в каждом состоянии — по строкам, а не по ответу бэкенда. */
function stateCounts(deals) {
  const counts = {}
  for (const deal of deals || []) {
    const state = (deal && deal.state) || '—'
    counts[state] = (counts[state] || 0) + 1
  }
  return counts
}

/**
 * Подпись состояния для набора строк: `wait_human` с вопросом — «ждёт ответа»,
 * без вопроса — «ждёт решения» (та же развилка, что у бейджа, см. `stateMeta`).
 *
 * Нужна чипам фильтра и подписи группы: подпись считается по видимым делам, а не
 * берётся из одной группы, которой после 0.0.6 (группировка — репозиторий) уже
 * нет.
 */
function stateLabel(state, deals) {
  const kinds = new Set(
    (deals || []).filter((deal) => deal && deal.state === state).map((deal) => deal.humanKind || ''),
  )
  return kinds.size === 1 ? stateMeta(state, [...kinds][0]).label : stateMeta(state).label
}

/**
 * Группы строк по `repo`: сначала названные репозитории, «без репозитория» —
 * отдельной группой и последней.
 *
 * Порядок названных — по имени: репозиторий это подпись группы, а не приоритет
 * (приоритет задаёт бэкенд и он виден по строкам — `/now` сортирует дела
 * `WAITING_PRIORITY`). Внутри группы порядок ответа не пересортировывается, иначе
 * два опроса одного снимка давали бы разный список. То же самое делает CLI
 * (`dashboard/cli.py`, `_repo_groups`): числа дел и групп обязаны совпадать.
 */
function groupDeals(deals) {
  const named = new Map()
  const without = []
  for (const deal of deals || []) {
    const repo = String((deal && deal.repo) || '')
    if (!repo) {
      without.push(deal)
      continue
    }
    if (!named.has(repo)) named.set(repo, [])
    named.get(repo).push(deal)
  }
  const groups = [...named.keys()].sort().map((repo) => ({
    key: repo,
    repo,
    label: repo,
    // Выдуманного тона у группы нет: она про репозиторий, а не про ожидание.
    // Состояния видны в шапке группы и бейджами в строках.
    tone: '',
    state: '',
    count: named.get(repo).length,
    deals: named.get(repo),
    byState: stateCounts(named.get(repo)),
  }))
  if (without.length) {
    groups.push({
      key: '',
      repo: '',
      label: WITHOUT_REPO_LABEL,
      tone: '',
      state: '',
      count: without.length,
      deals: without,
      byState: stateCounts(without),
    })
  }
  return groups
}

/**
 * Состояния группы одной строкой: `ждёт ответа 2 · работает 1`.
 *
 * Порядок — по первому появлению в группе (то есть по приоритету ответа), а не
 * по таблице подписей: второго списка приоритетов в UI нет.
 */
function groupStatesText(group) {
  const rows = []
  for (const deal of (group && group.deals) || []) {
    const state = deal.state || '—'
    const found = rows.find((row) => row.state === state)
    if (found) {
      found.count += 1
      continue
    }
    rows.push({ state, count: 1 })
  }
  return rows.map((row) => `${stateLabel(row.state, group.deals)} ${row.count}`).join(' · ')
}

/**
 * Группы строк по `waiting.state` — в порядке первого появления в ответе.
 *
 * Нужны экрану «Почему стоит»: там группировка по ожиданию и есть объяснение
 * («почему стоит» — это состояние), а список дел с 0.0.6 группируется по `repo`.
 * Порядок групп приоритетный без второго списка приоритетов: `/now` сортирует
 * дела `WAITING_PRIORITY`, а строки внутри группы сохраняют порядок ответа.
 */
function groupByState(deals) {
  const buckets = new Map()
  for (const deal of deals || []) {
    const state = (deal && deal.state) || '—'
    if (!buckets.has(state)) buckets.set(state, [])
    buckets.get(state).push(deal)
  }
  const groups = []
  for (const [state, rows] of buckets) {
    const meta = stateMeta(state)
    groups.push({
      key: state,
      state,
      // «ждёт ответа» — только если вопрос есть у каждого дела в группе; иначе
      // базовая подпись: блокировка без вопроса не обещает ответа.
      label: stateLabel(state, rows),
      tone: meta.tone,
      count: rows.length,
      deals: rows,
      byState: stateCounts(rows),
    })
  }
  return groups
}

/** Группировка-состав репозиториев из ответа (`/now.repos`, `/stats.repos`). */
function shapeRepos(raw) {
  const data = raw && typeof raw === 'object' ? raw : {}
  const list = (Array.isArray(data.list) ? data.list : [])
    .map((item) => ({ repo: String((item && item.repo) || ''), deals: Number(item && item.deals) || 0 }))
    .filter((item) => item.repo)
  return {
    list,
    withoutRepo: Number(data.without_repo) || 0,
    // `read: false` — store дел не прочитался: «репозиториев нет» и «группировка не
    // посчитана» разные факты, и экран не имеет права показывать первое вместо второго.
    read: data.read !== false,
  }
}

/**
 * Модель экрана из ответа `/now`: строки, группы по репозиториям и честные ошибки
 * источников.
 *
 * Досок в ответе больше нет (0.0.6): `repos` отвечает, какие репозитории названы
 * делами store и сколько дел без репозитория — тот же блок считает CLI. Ошибки
 * источников приходят парой `{source, reason}`: источник назван, а не выдан за
 * «доску».
 */
function shapeNow(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const deals = (Array.isArray(data.deals) ? data.deals : []).map(shapeDeal)
  const repos = shapeRepos(data.repos)
  const errors = (Array.isArray(data.errors) ? data.errors : []).map((error) => ({
    source: String((error && error.source) || 'источник'),
    reason: String((error && error.reason) || 'причина не записана'),
  }))
  const byState = stateCounts(deals)
  // Порядок состояний — из ответа бэкенда (`by_state` идёт в порядке
  // WAITING_PRIORITY), а не из порядка ключей таблицы подписей.
  const backendStates =
    data.by_state && typeof data.by_state === 'object' ? Object.keys(data.by_state) : []
  const stateOrder = backendStates.length
    ? backendStates.map(String)
    : [...new Set(deals.map((deal) => deal.state))]
  return {
    now: Number.isFinite(Number(data.now)) ? Number(data.now) : null,
    deals,
    groups: groupDeals(deals),
    repos,
    errors,
    repoCount: repos.list.length,
    withoutRepo: repos.withoutRepo,
    count: deals.length,
    byState,
    stateGroups: groupByState(deals),
    humanCounts: humanCounts(deals),
    stateOrder,
  }
}

/**
 * Локальные фильтры страницы: репозиторий, набор состояний, поиск.
 *
 * `repo` — `ALL_REPOS` (или не задан) — все дела, `''` — ровно дела без
 * репозитория, иначе путь репозитория. Пустая строка как значение — не выдумка:
 * ровно так отличает «без репозитория» от «фильтра нет» бэкенд (`?repo=`).
 */
function filterNow(model, filter) {
  const f = filter && typeof filter === 'object' ? filter : {}
  const repo = f.repo === undefined || f.repo === null ? ALL_REPOS : String(f.repo)
  const states = f.states instanceof Set ? f.states : new Set(Array.isArray(f.states) ? f.states : [])
  const query = String(f.query || '').trim().toLowerCase()
  const deals = (model && model.deals ? model.deals : []).filter(
    (deal) =>
      (repo === ALL_REPOS || (repo === '' ? !deal.repo : deal.repo === repo)) &&
      (!states.size || states.has(deal.state)) &&
      (!query || deal.search.includes(query)),
  )
  return {
    ...model,
    deals,
    groups: groupDeals(deals),
    stateGroups: groupByState(deals),
    count: deals.length,
    byState: stateCounts(deals),
    humanCounts: humanCounts(deals),
  }
}

/* ------------------------------------------- экран «Завершённые» (0.0.7) */

// Подписи терминальных статусов: у закрытого дела нет состояния ожидания, и
// бейдж рисуется по статусу строки store (`done`/`archived`). Тон — тот же набор,
// что у остальных бейджей (`TONE_COLOR`).
const FINISHED_STATUS_META = {
  done: { label: 'завершено', tone: 'green' },
  archived: { label: 'в архиве', tone: 'gray' },
}

/** Мета терминального статуса; неизвестный — своим именем серым. */
function finishedStatusMeta(status) {
  const key = String(status || '')
  return FINISHED_STATUS_META[key] || { label: key || '—', tone: 'gray' }
}

/** Вопросы человеку одной строкой: `2 вопроса · 1 ждёт ответа`. */
function questionsText(questions) {
  const block = questions && typeof questions === 'object' ? questions : {}
  const asked = Number(block.asked) || 0
  const waiting = Number(block.waiting) || 0
  const tail = waiting ? ` · ${waiting} ${plural(waiting, 'ждёт', 'ждут', 'ждут')} ответа` : ''
  return `${asked} ${plural(asked, 'вопрос', 'вопроса', 'вопросов')}${tail}`
}

/**
 * Публикация словами: `{sha, shaShort, branch, ref, mrUrl, verifiedBy, text}`.
 *
 * Один формат на карточку дела (`shapeDealTime`) и на строку «Завершённых»:
 * sha подтверждён `git ls-remote` движка (`dashboard/publish.py`), а не заявкой
 * исполнителя. Нет факта — `null`, а не «опубликовано ничего».
 */
function fmtPublication(raw) {
  const pub = raw && typeof raw === 'object' ? raw : null
  if (!pub || !Object.keys(pub).length) return null
  const sha = String(pub.sha || '')
  return {
    sha,
    shaShort: sha.slice(0, 8),
    branch: String(pub.branch || ''),
    ref: String(pub.ref || ''),
    mrUrl: String(pub.mr_url || ''),
    verifiedBy: String(pub.verified_by || ''),
    text: [sha.slice(0, 8), String(pub.branch || pub.ref || ''), String(pub.verified_by || '')]
      .filter(Boolean)
      .join(' · '),
  }
}

/**
 * Строка экрана «Завершённые» из строки ответа `/deals`.
 *
 * Все числа — поля ответа: `attempts`, `questions`, `duration_sec`, `published`,
 * `cause`, `stages`. Экран ничего не пересчитывает: строка списка и карточка
 * того же дела (`bin/duty deal <id>`) обязаны называть одни числа, а второй
 * расчёт разошёлся бы с первым на первой же нестандартной попытке.
 */
function shapeFinishedRow(raw) {
  const row = raw && typeof raw === 'object' ? raw : {}
  const questions = row.questions && typeof row.questions === 'object' ? row.questions : {}
  const askCount = Number(questions.asked) || 0
  const waitCount = Number(questions.waiting) || 0
  const meta = finishedStatusMeta(row.status)
  const finishedAt = numOrNull(row.finished_at)
  const duration = numOrNull(row.duration_sec)
  const stages = (Array.isArray(row.stages) ? row.stages : []).map(String)
  const repo = String(row.repo || '')
  return {
    id: String(row.id || ''),
    repo,
    repoLabel: repoLabel(repo),
    title: fmtTitle(row.title, 90),
    titleFull: String(row.title || ''),
    assignee: String(row.assignee || '—'),
    status: String(row.status || ''),
    statusLabel: meta.label,
    tone: meta.tone,
    createdAt: numOrNull(row.created_at),
    createdText: fmtClock(row.created_at),
    finishedAt,
    finishedText: fmtClock(finishedAt),
    // `null` — факта закрытия в журнале нет: «время не записано» честнее нуля.
    duration,
    durationText: duration === null ? '—' : fmtAge(duration),
    cause: String(row.cause || ''),
    causeText: String(row.cause || '') || 'причина не записана',
    stages,
    stagesText: stages.join(' → ') || '—',
    attempts: Number(row.attempts) || 0,
    questions: {
      asked: askCount,
      answered: Number(questions.answered) || 0,
      waiting: waitCount,
      text: questionsText(questions),
    },
    published: fmtPublication(row.published),
    search: `${repo} ${row.id || ''} ${row.title || ''} ${row.assignee || ''} ${row.status || ''}`.toLowerCase(),
  }
}

/**
 * Модель экрана «Завершённые» из ответа `/deals`.
 *
 * Порядок — ответа бэкенда (свежие сверху), группы — по `repo` тем же
 * `groupDeals`, что у «Сейчас»: внутри группы строки остаются свежими, а имена
 * групп совпадают с `bin/duty deals`. `note` — причина пустоты словами бэкенда
 * («закрытых дел нет» / «store не прочитался» / «в этом репозитории нет»),
 * экран её печатает, а не выдумывает свою.
 */
function shapeFinished(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const deals = (Array.isArray(data.deals) ? data.deals : []).map(shapeFinishedRow)
  const errors = (Array.isArray(data.errors) ? data.errors : []).map((error) => ({
    source: String((error && error.source) || 'источник'),
    reason: String((error && error.reason) || 'причина не записана'),
  }))
  return {
    now: numOrNull(data.now),
    deals,
    groups: groupDeals(deals),
    count: deals.length,
    total: Number(data.total) || 0,
    limit: Number(data.limit) || 0,
    truncated: data.truncated === true,
    statuses: (Array.isArray(data.statuses) ? data.statuses : []).map(String),
    note: String(data.note || ''),
    errors,
  }
}

/** Тон исхода попытки: «падение» — по набору бэкенда, дальше успех/отложено. */
function outcomeTone(outcome, status, crashOutcomes) {
  const key = String(outcome || '')
  if (key && Array.isArray(crashOutcomes) && crashOutcomes.includes(key)) return 'red'
  if (OUTCOME_TONE[key]) return OUTCOME_TONE[key]
  if (key) return 'gray'
  return String(status || '') === 'running' ? 'green' : 'gray'
}

/**
 * Таймлайн попыток из ответа `/duty/{deal_id}/attempts`.
 *
 * Геометрии здесь нет: полосы на общей оси дела считает `shapeDealTime`
 * (минимум и максимум по `timeline` и `attempts`), и второй расчёт ширины —
 * «доля от самой долгой попытки» — дал бы на экране две несравнимые шкалы.
 * Здесь только факты одной попытки для карточки под шкалой.
 */
function shapeAttempts(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const list = Array.isArray(data.attempts) ? data.attempts : []
  const crashes = Array.isArray(data.crash_outcomes) ? data.crash_outcomes.map(String) : []
  const attempts = list.map((attempt) => {
    const duration = attempt.duration_sec === undefined ? null : attempt.duration_sec
    const events = (Array.isArray(attempt.events) ? attempt.events : []).map((event) => ({
      kind: String(event.kind || '—'),
      at: event.at ?? null,
      clock: fmtClock(event.at),
      summary: String(event.summary || ''),
    }))
    return {
      runId: attempt.run_id ?? null,
      profile: String(attempt.profile || '—'),
      stage: String(attempt.stage || ''),
      status: String(attempt.status || '—'),
      start: attempt.start ?? null,
      end: attempt.end ?? null,
      duration,
      durationText: fmtAge(duration),
      open: attempt.start !== undefined && attempt.start !== null && (attempt.end === undefined || attempt.end === null),
      lease: String(attempt.lease || ''),
      progress: attempt.progress ?? null,
      tools: attempt.tools ?? null,
      // Числа процесса подписываются словами здесь, а не в рендере: у обеих
      // поверхностей одна форма (`шагов N`, `инструментов M`), и разъехаться
      // этой подписи негде.
      progressText: attempt.progress === null || attempt.progress === undefined ? '' : `шагов ${fmtCount(attempt.progress)}`,
      toolsText: attempt.tools === null || attempt.tools === undefined ? '' : `инструментов ${fmtCount(attempt.tools)}`,
      calls: shapeCalls(attempt.calls),
      callsOmitted: numOrNull(attempt.calls_omitted) || 0,
      outcome: String(attempt.outcome || ''),
      outcomeTone: outcomeTone(attempt.outcome, attempt.status, crashes),
      error: String(attempt.error || ''),
      summary: String(attempt.summary || ''),
      events,
    }
  })
  return {
    repo: String(data.repo || ''),
    dealId: String(data.deal_id || ''),
    title: String(data.title || ''),
    count: attempts.length,
    attempts,
  }
}

/**
 * Строки неполноты ответа `/now`: упавший источник и непосчитанная группировка.
 *
 * `stale` — последний запрос не прошёл, на экране прошлый снимок: его замечания
 * уже не факт о настоящем, поэтому отдаём пусто. Иначе «источник X недоступен»
 * переживает починку кода и висит, пока страницу не перезагрузят.
 */
function noticeLines(model, stale = false) {
  if (stale || !model) return []
  const lines = (model.errors || []).map((bad) => `источник ${bad.source} недоступен: ${bad.reason}`)
  // Store дел не прочитан — группировки по репозиториям нет: пустой список групп
  // тут означал бы «репозиториев не названо», а это другой факт.
  if (model.repos && model.repos.read === false) {
    lines.push('store дел не прочитан: группировка по репозиториям не посчитана')
  }
  return lines
}

/* ------------------------------------------- лента этапов и попыток (W6) */

// Состояния этапа приходят из `transit.stage_states` (`done|running|pending|
// failed|wait_human|wait_gate|parked`). Правила не дублируются: здесь только
// подпись и тон. Цвет по заданию: `done` — спокойный зелёный, `running` —
// акцент, `wait_*` — янтарный, `failed` — красный, `pending` — серый.
const STAGE_META = {
  done: { label: 'пройден', tone: 'green' },
  running: { label: 'идёт', tone: 'blue' },
  wait_human: { label: 'ждёт человека', tone: 'yellow' },
  wait_gate: { label: 'ждёт гейт', tone: 'yellow' },
  pending: { label: 'ждёт хода', tone: 'gray' },
  failed: { label: 'отказ', tone: 'red' },
  parked: { label: 'припаркован', tone: 'yellow' },
}

// Чем кончился процесс: `blocked`/`refused` янтарные — это не падение
// исполнения, а отказ гейта или вопрос человеку. Незнакомый исход остаётся
// серым: тон не выдумывается (тот же принцип, что у `outcomeTone`).
// Исход попытки по-русски. Английские слова из журнала (`done`, `failed`)
// на экран не идут: подпись строки — для человека, а не телеметрия.
const OUTCOME_WORD = {
  done: 'готово',
  ok: 'готово',
  success: 'готово',
  passed: 'готово',
  accepted: 'готово',
  failed: 'провал',
  blocked: 'отказ',
  refused: 'отказ',
  rate_limited: 'лимит',
  reclaimed: 'снята',
}

/**
 * Причина отказа одной фразой — для подсказки провалившейся попытки.
 *
 * «Провал» без причины ничего не объясняет, поэтому рядом с исходом идут класс
 * отказа (`contract`, `infra`, …) и текст причины из журнала. Текст обрезается
 * до подписи: целиком он живёт в ленте этапа и в журнале, а подсказка читается
 * поверх таймлайна. Нет причины — нет и фразы: выдумывать её экран не станет.
 */
function barReasonText(bar) {
  const b = bar && typeof bar === 'object' ? bar : {}
  const reason = String(b.reason || '').trim()
  if (!reason) return ''
  const kind = String(b.failureKind || '').trim()
  return `${kind ? `отказ ${kind}` : 'причина'}: ${fmtTitle(reason, 140)}`
}

/**
 * Слово исхода для подписи строки; живёт рядом с тоном, чтобы не разъезжаться.
 *
 * Незнакомый исход показывается как есть: новый класс в журнале — факт, и
 * подменять его на «—» значило бы его спрятать.
 */
function outcomeWord(value) {
  const key = String(value || '')
  return OUTCOME_WORD[key] || key
}

const ATTEMPT_TONE = {
  done: 'green',
  ok: 'green',
  success: 'green',
  passed: 'green',
  accepted: 'green',
  blocked: 'yellow',
  refused: 'yellow',
  rate_limited: 'yellow',
  reclaimed: 'yellow',
  failed: 'red',
}

/** Подпись и тон состояния этапа; незнакомое состояние — как есть, серым. */
function stageMeta(state) {
  const key = String(state || '')
  return STAGE_META[key] || { label: key || '—', tone: 'gray' }
}

/** Число с разделителем разрядов: `15 234` глазом читается, `15234` — нет. */
function fmtCount(value) {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  return String(Math.trunc(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
}

/**
 * Запись объявленной команды: спека против отчёта попытки.
 *
 * `ok: false` без записи — не «упало», а «не доказано»: подпись говорит именно
 * это, иначе отсутствие evidence читалось бы как проваленный build.
 */
function shapeStageEvidence(entry) {
  const e = entry && typeof entry === 'object' ? entry : {}
  const must = e.must === null || e.must === undefined ? null : Number(e.must)
  const got = e.got === null || e.got === undefined ? null : Number(e.got)
  let label = 'не доказано: записи нет'
  let tone = 'gray'
  if (got !== null) {
    const proved = e.ok === true
    label = proved ? `exit ${got} — как объявлено` : `exit ${got}, объявлено ${must === null ? '—' : must}`
    tone = proved ? 'green' : 'red'
  } else if (e.ok === true) {
    label = 'подтверждено'
    tone = 'green'
  }
  return {
    cmd: String(e.cmd || '—'),
    must,
    got,
    mustText: must === null ? '—' : String(must),
    gotText: got === null ? '—' : String(got),
    label,
    tone,
    at: e.at ?? null,
    clock: fmtClock(e.at),
  }
}

/**
 * Одна попытка этапа — процесс: время, длительность, код выхода, токены,
 * инструменты, причина отказа.
 *
 * Длительность есть только у закрытой попытки. У идущей её нет, и подставлять
 * «сколько уже идёт» значило бы мерить прогресс, которого никто не мерил.
 * Живость — отдельный факт от журнала: `live` (лизинг действует), `dead`
 * (лизинг истёк, завершения нет), `unknown` (срок лизинга не записан).
 */
function shapeStageAttempt(attempt) {
  const a = attempt && typeof attempt === 'object' ? attempt : {}
  const outcome = String(a.outcome || '')
  const live = a.live === true
  const dead = a.dead === true
  const lease = String(a.lease || '')
  const duration = a.duration_sec === undefined ? null : a.duration_sec
  let liveText = 'закрыта без исхода'
  if (live) liveText = 'идёт'
  else if (dead) liveText = 'процесс мёртв: лизинг истёк, завершения нет'
  else if (lease === 'unknown') liveText = 'живость неизвестна: лизинг не записан'
  else if (outcome) liveText = `исход: ${outcomeWord(outcome)}`
  const tone = live ? 'blue' : dead ? 'red' : ATTEMPT_TONE[outcome] || 'gray'
  const records = (Array.isArray(a.evidence) ? a.evidence : []).map((record) => {
    const r = record && typeof record === 'object' ? record : {}
    const exit = r.exit === null || r.exit === undefined ? null : Number(r.exit)
    return {
      cmd: String(r.cmd || '—'),
      exit,
      exitText: exit === null ? '—' : String(exit),
      clock: fmtClock(r.started_at),
    }
  })
  return {
    n: Number(a.n) || null,
    number: `#${Number(a.n) || '—'}`,
    runId: a.run_id ?? null,
    stage: String(a.stage || ''),
    role: String(a.role || ''),
    pid: a.pid ?? null,
    pidText: a.pid ? `pid ${a.pid}` : 'pid не записан',
    start: a.started_at ?? null,
    clock: fmtClock(a.started_at),
    finishedAt: a.finished_at ?? null,
    finishClock: fmtClock(a.finished_at),
    duration,
    durationText: live ? 'идёт' : fmtAge(duration),
    exit: a.exit ?? null,
    exitText: a.exit === null || a.exit === undefined ? '—' : String(a.exit),
    outcome,
    kind: String(a.kind || ''),
    tokens: a.tokens ?? null,
    tokensText: fmtCount(a.tokens),
    tools: a.tools ?? null,
    toolsText: fmtCount(a.tools),
    reason: String(a.reason || ''),
    live,
    dead,
    lease,
    liveText,
    tone,
    evidence: records,
    hasEvidence: records.length > 0,
  }
}

/**
 * Этап дела: имя, роль, состояние, номер попытки, ожидание, попытки, evidence.
 *
 * Ожидание вопроса вынесено отдельной строкой с `question_id` и ссылкой на
 * панель пула (`/imsg`): вопрос живёт там, где на него отвечают, а лента — нет.
 * Своей кнопки «ответить»/«перезапустить» экран не заводит: он ничего не решает.
 */
function shapeStage(stage) {
  const s = stage && typeof stage === 'object' ? stage : {}
  const meta = stageMeta(s.state)
  const waits = s.waits && typeof s.waits === 'object' ? s.waits : null
  const needs = (Array.isArray(s.needs) ? s.needs : []).map(String)
  const attempts = (Array.isArray(s.attempts) ? s.attempts : []).map(shapeStageAttempt)
  const evidence = (Array.isArray(s.evidence) ? s.evidence : []).map(shapeStageEvidence)
  const kind = waits ? String(waits.kind || '') : ''
  const questionId =
    waits && waits.question_id !== null && waits.question_id !== undefined ? String(waits.question_id) : ''
  let waitText = ''
  if (kind === 'question') waitText = `вопрос ${questionId ? `#${questionId}` : 'без номера'} — ждёт ответа`
  else if (kind === 'human') waitText = 'ждёт человека: вопроса в пуле нет'
  else if (kind === 'gate') waitText = `ждёт гейт: ${String(waits.reason || 'граф не пускает')}`
  else if (kind === 'parked') {
    // Парковка — не отдельное состояние: экран называет счётчик и правило,
    // а решение остаётся за человеком (docs/JOURNAL.md).
    const cls = String(waits.class || '')
    const spent = waits.attempts === null || waits.attempts === undefined ? '?' : String(waits.attempts)
    const limit = waits.limit === null || waits.limit === undefined ? '?' : String(waits.limit)
    const counter = cls ? `${spent}/${limit} ${cls}` : 'попытки исчерпаны'
    const park = String(waits.park_reason || '')
    waitText = `попытки исчерпаны: ${counter}${park ? ` (${park})` : ''} — решает человек${questionId ? `, вопрос #${questionId}` : ''}`
  } else if (kind === 'retry') {
    waitText = `пауза до ${fmtClock(waits.retry_at)}: повтор после отказа`
  }
  const attempt = Number(s.attempt) || 0
  return {
    id: String(s.id || '—'),
    // Вид этапа (`agent`/`shell`/`gate`) из спеки: подпись строки столбца и
    // значок графа берут его отсюда, а не угадывают по имени этапа.
    kind: String(s.kind || 'agent'),
    role: String(s.role || '—'),
    state: String(s.state || ''),
    stateLabel: meta.label,
    tone: meta.tone,
    attempt,
    attemptText: `попытка ${attempt}`,
    rounds: Number(s.rounds) || 0,
    needs,
    needsText: needs.length ? `после: ${needs.join(', ')}` : '',
    reason: String(s.reason || ''),
    waits,
    waitKind: kind,
    questionId,
    questionHref: questionId ? '/imsg' : '',
    waitText,
    attempts,
    attemptsCount: attempts.length,
    hasAttempts: attempts.length > 0,
    evidence,
    hasEvidence: evidence.length > 0,
  }
}

/**
 * Лента этапов из ответа `/deal` (`stages` + `stages_note`).
 *
 * Пустой список — не ошибка рендера: у дела может не быть спеки, а у этапов —
 * попыток. Причину пустоты называет `note` бэкенда, поэтому экран не гадает.
 * Своих правил «готово/не готово» здесь нет: состояние приходит посчитанным.
 */
function shapeStages(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const stages = (Array.isArray(data.stages) ? data.stages : []).map(shapeStage)
  const attempts = stages.reduce((sum, stage) => sum + stage.attemptsCount, 0)
  return {
    spec: String(data.spec || ''),
    note: String(data.stages_note || ''),
    stages,
    count: stages.length,
    attempts,
    empty: stages.length === 0,
  }
}

/* ------------- компактный столбец этапов и попыток (0.0.8) ------------- */

// Слова видов событий журнала: сводка журнала говорит «5 прогрессов», а не
// «5 attempt_progress». Незнакомый вид печатается как есть — новый класс
// события на экране остаётся фактом, а не прячется за «—».
const EVENT_WORD = {
  attempt_progress: ['прогресс', 'прогресса', 'прогрессов'],
  attempt_started: ['старт', 'старта', 'стартов'],
  attempt_ended: ['конец', 'конца', 'концов'],
  question_asked: ['вопрос', 'вопроса', 'вопросов'],
  question_answered: ['ответ', 'ответа', 'ответов'],
  published: ['публикация', 'публикации', 'публикаций'],
  completed: ['закрытие', 'закрытия', 'закрытий'],
  collected: ['вердикт', 'вердикта', 'вердиктов'],
  changes_requested: ['правки', 'правки', 'правок'],
}

// Предел строки команды в сводке доказательств: столбец 320 px, длинная команда
// в нём — абзац. Полная строка живёт в подсказке (`cmdTitle`).
const CMD_SHORT_LIMIT = 46

// Обёртка запуска, которая не является частью команды: `env -u PYTHONPATH`
// повторяется в каждой строке и не говорит ничего о том, что запускали.
const CMD_ENV_WRAPPER = /^env\s+(-u\s+\S+\s+)+/

/** Слова вида события: тройка для `plural`; незнакомый — как есть. */
function eventWord(kind) {
  return EVENT_WORD[String(kind || '')] || [String(kind || ''), String(kind || ''), String(kind || '')]
}

/**
 * Журнал попытки сводкой по видам: `5 прогрессов · 1 старт · 1 конец`.
 *
 * У одной попытки бывает 374 события `attempt_progress`, и в столбце 320 px они
 * превращают страницу в лог. Порядок постоянный — по убыванию числа, равные — по
 * первому появлению в журнале: иначе сводка прыгала бы между опросами одного и
 * того же дела. Длиннее трёх видов хвост не печатается вовсе (без «и ещё…»):
 * столбец не место для перечня телеметрии.
 */
function eventsSummary(events) {
  const counts = new Map()
  for (const event of Array.isArray(events) ? events : []) {
    const kind = String((event && event.kind) || '').trim()
    if (!kind) continue
    counts.set(kind, (counts.get(kind) || 0) + 1)
  }
  const rows = [...counts.entries()].map(([kind, count], index) => ({ kind, count, index }))
  rows.sort((left, right) => right.count - left.count || left.index - right.index)
  return rows
    .slice(0, 3)
    .map((row) => `${row.count} ${plural(row.count, ...eventWord(row.kind))}`)
    .join(' · ')
}

/**
 * Хвост команды для узкого столбца: без обёртки `env -u PYTHONPATH`, без
 * абсолютных путей, не длиннее 46 знаков.
 *
 * `python3 -m pytest tests/ -q` вместо `/Users/…/venv/bin/python3 -m pytest
 * tests/ -q`: абсолютный путь в строке команды — машинная подробность, и под
 * подсказкой она не нужна. Обрезка здесь, а не CSS-эллипсисом: подсказка обязана
 * нести полную команду, а `text-overflow` её не сохраняет.
 */
function cmdShort(cmd) {
  let text = String(cmd ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  text = text.replace(CMD_ENV_WRAPPER, '')
  text = text
    .split(' ')
    .map((token) =>
      token.startsWith('/')
        ? token.replace(/\/+$/, '').split('/').filter(Boolean).pop() || token
        : token,
    )
    .join(' ')
  if (!text) return '—'
  if (text.length <= CMD_SHORT_LIMIT) return text
  return `${text.slice(0, CMD_SHORT_LIMIT - 1).trimEnd()}…`
}

/**
 * Строка этапа в компактном столбце: имя, вид, попытки, состояние словами.
 *
 * Служебное (`needs`) отдаётся отдельным полем, а не строкой: в столбце 320 px
 * строка «после: …» съедает высоту, которую занимают попытки, — поверхность
 * кладёт её в `title` строки.
 *
 * Раскрыт по умолчанию этап, где дело стоит сейчас (`running`/`wait_human`/
 * `wait_gate`/`parked`, а также `failed` — это тоже «здесь падало»), и этап с
 * проваленной попыткой: решение прошлого круга «провал виден без клика» не
 * отменяется компактностью.
 */
function compactStageView(stage) {
  const s = stage && typeof stage === 'object' ? stage : {}
  const state = String(s.state || '')
  const meta = s.stateLabel ? { label: String(s.stateLabel), tone: String(s.tone || 'gray') } : stageMeta(state)
  const attempts = Array.isArray(s.attempts) ? s.attempts : []
  const count = Number(s.attemptsCount) || attempts.length
  const failed = attempts.some((attempt) => attempt && attempt.tone === 'red')
  const standing = ['running', 'wait_human', 'wait_gate', 'parked', 'failed'].includes(state)
  const open = standing || failed
  const id = String(s.id || '—')
  const kind = String(s.kind || 'agent')
  return {
    id,
    // Вид этапа: у агентских — роль (`implementer`), у остальных — сам вид
    // (`shell`, `gate`) из спеки. Та же подпись, что у дорожки шкалы.
    kindText: kind === 'agent' ? String(s.role || 'профиль') : kind,
    attemptsText: count ? `${count} ${plural(count, 'попытка', 'попытки', 'попыток')}` : 'попыток нет',
    stateLabel: meta.label,
    tone: meta.tone,
    needsText: String(s.needsText || ''),
    // Вопрос человеку остаётся видимой строкой под этапом, а не подсказкой:
    // дело может стоять на человеке, и это не деталь.
    waitText: String(s.waitText || ''),
    waitHref: String(s.questionHref || ''),
    openByDefault: open,
    toggleTitle: `${open ? 'свернуть' : 'развернуть'} попытки этапа ${id}`,
  }
}

/** Запись доказательства строкой столбца: код выхода, команда, время. */
function compactEvidenceRow(record) {
  const r = record && typeof record === 'object' ? record : {}
  // Две формы записи: отчёт попытки (`exit`) и объявленная команда спеки
  // (`got` — что реально получили). Подпись одна: код выхода.
  const exit = r.exit === undefined || r.exit === null ? numOrNull(r.got) : numOrNull(r.exit)
  const cmd = String(r.cmd || '—')
  return {
    exitText: exit === null ? '—' : String(exit),
    // Ноль — доказано, иное — нет; «записи нет» остаётся серой, это не провал.
    tone: exit === null ? 'gray' : exit === 0 ? 'green' : 'red',
    cmdShort: cmdShort(cmd),
    cmdTitle: cmd,
    clock: String(r.clock || fmtClock(r.at ?? r.started_at)),
  }
}

/**
 * Строка попытки в компактном столбце: номер, длительность, исход словами.
 *
 * `stage` — этап из ленты (`shapeStage`), `bar` — попытка с фактами процесса:
 * полоса шкалы (`shapeDealTime`) или попытка ручки `/attempts` (`shapeAttempts`).
 * Доказательства живут только в ленте этапа, поэтому `stage.attempts` ищется по
 * `runId`, а при его отсутствии — по номеру: у двух источников одна попытка
 * называется по-разному.
 *
 * Раскрыта по умолчанию проваленная и живая попытка: провал обязан быть виден
 * без клика.
 */
function compactAttemptView(stage, bar) {
  const s = stage && typeof stage === 'object' ? stage : {}
  const b = bar && typeof bar === 'object' ? bar : {}
  const rows = Array.isArray(s.attempts) ? s.attempts : []
  const runId = b.runId === undefined || b.runId === null ? null : String(b.runId)
  const number = String(b.number || '')
  const n = numOrNull(b.n)
  const own =
    rows.find((row) => {
      if (!row || typeof row !== 'object') return false
      if (runId !== null && row.runId !== undefined && row.runId !== null && String(row.runId) === runId) return true
      if (n !== null && Number(row.n) === n) return true
      return Boolean(number) && String(row.number || '') === number
    }) || null

  // Номер попытки: `#1` из полосы, из записи этапа или из хвоста `runId`
  // (`d_abcdef12:impl#2`). Нет ни одного факта — «—», а не выдуманный ноль.
  let label = number
  if (!label && n !== null) label = `#${n}`
  if (!label) {
    const tail = /#(\d+)$/.exec(runId || '')
    label = tail ? `#${tail[1]}` : '—'
  }

  const open =
    b.open === true || b.live === true || (own !== null && (own.live === true || own.open === true))
  const dead = b.dead === true || (own !== null && own.dead === true)
  const tone = dead
    ? 'red'
    : open
      ? 'blue'
      : String(b.tone || b.outcomeTone || (own && own.tone) || 'gray')

  const outcome = String(b.outcome || (own && own.outcome) || '')
  let outcomeText = 'закрыта без исхода'
  if (dead) outcomeText = 'процесс мёртв'
  else if (open) outcomeText = 'идёт'
  else if (outcome) outcomeText = outcomeWord(outcome)

  // Длительность есть только у закрытой попытки: у идущей её никто не мерил, а
  // у мёртвой нет и конца.
  const durationText = dead
    ? '—'
    : open
      ? 'идёт'
      : String(b.durationText || (own && own.durationText) || '—')

  // Доказательства: свои у попытки, а если их нет — объявленные команды этапа,
  // и только у последней попытки (иначе один и тот же список повторился бы под
  // каждой строкой этапа).
  const ownRecords = own && Array.isArray(own.evidence) ? own.evidence : []
  const stageRecords = Array.isArray(s.evidence) ? s.evidence : []
  const mine = ownRecords.length
    ? ownRecords
    : !rows.length || own === rows[rows.length - 1]
      ? stageRecords
      : []
  const evidenceRows = mine.map(compactEvidenceRow)

  return {
    label,
    durationText,
    outcomeText,
    tone,
    openByDefault: tone === 'red' || open,
    evidenceRows,
    summary: String(b.summary || ''),
    // `error` — как есть; у ленты этапа вместо него `reason` (один факт, два
    // имени в двух ручках), и подписи «ошибка» здесь не выдумывается.
    error: String(b.error || (own && own.reason) || b.reason || ''),
    eventsText: eventsSummary(b.events),
  }
}

/* ---------------------------------------- шкала времени дела (E2, 0.0.5) */

// Минимальная ширина полосы на общей оси: мгновенная попытка не должна исчезать
// из шкалы (те же 2%, что были у относительной полосы), но подпись при этом
// честная — `<1с`, иначе две ширины в процентах читались бы как две секунды.
const BAR_MIN_PCT = 2

// Подпись и тон маркера: журнал назвал событие — экран назвал его тем же словом.
// `tick` — тонкая засечка старта/закрытия попытки (их на оси много); остальные
// виды — то, что случилось с делом, и они рисуются заметно.
const MARKER_META = {
  question: { label: 'вопрос человеку', tone: 'yellow', tick: false },
  answer: { label: 'ответ человека', tone: 'green', tick: false },
  verdict: { label: 'вердикт', tone: 'blue', tick: false },
  budget: { label: 'отказ по бюджету', tone: 'red', tick: false },
  published: { label: 'публикация', tone: 'green', tick: false },
  closed: { label: 'закрыто', tone: 'gray', tick: false },
  started: { label: 'старт попытки', tone: 'gray', tick: true },
  ended: { label: 'конец попытки', tone: 'gray', tick: true },
}

// Причина закрытия по-русски: `cause` из журнала — машинное слово, экран его
// не показывает, но и не выдумывает: незнакомая причина остаётся как есть.
const CLOSE_CAUSE_WORD = {
  all_stages_done: 'все этапы пройдены',
  closed_by_human: 'закрыто человеком',
  stopped_by_human: 'остановлено человеком',
}

/**
 * Тело подписи маркера — то, что человек читает после названия события.
 *
 * Вопрос и ответ берутся текстом из пула (`question_text`/`answer_text` ответа
 * бэкенда), закрытие — словами причины и списком этапов. Выжимка payload'а
 * (`summary`) остаётся запасным путём: у приговора и отказа по бюджету она уже
 * написана словами движком, и второго форматтера здесь не заводится.
 */
function markerBody(type, event) {
  const e = event && typeof event === 'object' ? event : {}
  const text = String(e.summary || '')
  if (type === 'question') {
    const question = String(e.question_text || '')
    if (question) return `«${question}»`
    return 'текст вопроса не сохранён'
  }
  if (type === 'answer') {
    const answer = String(e.answer_text || '')
    if (answer) return `«${answer}»`
    return 'ответ без текста'
  }
  if (type === 'closed') {
    const cause = CLOSE_CAUSE_WORD[String(e.cause || '')] || String(e.cause || '')
    const stages = Array.isArray(e.stages) ? e.stages.filter(Boolean).map(String) : []
    return [cause, stages.length ? `этапы: ${stages.join(' → ')}` : ''].filter(Boolean).join(' · ')
  }
  // `summary` приговора и отказа по бюджету — уже фраза движка, а не JSON;
  // JSON-выжимку в подсказку не пускаем: это телеметрия.
  return text.startsWith('{') ? '' : text
}

/**
 * Вид маркера по событию журнала; пусто — маркера нет.
 *
 * Гейт и отказ по бюджету узнаются по причине из `attempt_ended`
 * (`_payload_summary` берёт `reason`): своего `kind` у отказа нет, он назван
 * словами движка — «гейт: …» и «бюджет дела исчерпан: N/M токенов». Незнакомое
 * событие маркера не получает: «нет события — нет маркера» — правило экрана.
 */
function markerType(kind, summary) {
  const text = String(summary || '')
  if (kind === 'question_asked') return 'question'
  if (kind === 'question_answered') return 'answer'
  if (kind === 'attempt_started') return 'started'
  if (kind === 'attempt_ended') {
    if (/^гейт/.test(text)) return 'verdict'
    if (/бюджет/i.test(text)) return 'budget'
    return 'ended'
  }
  // Приговор ревью — два факта журнала (`journal.VERDICT_FACTS`), а не текст.
  if (kind === 'collected' || kind === 'changes_requested') return 'verdict'
  if (kind === 'published') return 'published'
  if (kind === 'completed') return 'closed'
  return ''
}

/** Время как ЧЧ:ММ — для подписи оси: секунды на шкале дела не читаются. */
function fmtClockShort(at) {
  const full = fmtClock(at)
  return full === '—' ? '—' : full.slice(0, 5)
}

/** Длительность с нижней границей: ноль — `<1с`, а не `0 с`. */
function fmtDuration(sec) {
  const value = Number(sec)
  if (!Number.isFinite(value) || value <= 0) return '<1с'
  return fmtAge(value)
}

/** Лизинг незакрытой попытки словом: `жив`/`лизинг истёк`/`неизвестно`. */
function leaseWord(lease, status) {
  const key = String(lease || '')
  if (key === 'live') return 'жив'
  if (key === 'expired') return 'лизинг истёк'
  if (key === 'dead' || status === 'orphan') return 'лизинг истёк'
  return 'неизвестно'
}

/** Сколько дел в секундах — только конечные числа, иначе `null`. */
function numOrNull(value) {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** Попытка из ответа `/attempts` в полосу шкалы: время, дорожка, подписи. */
function shapeCalls(raw) {
  const list = Array.isArray(raw) ? raw : []
  const calls = []
  for (const item of list) {
    const call = item && typeof item === 'object' ? item : {}
    const at = numOrNull(call.at)
    if (at === null) continue
    const ms = numOrNull(call.ms)
    calls.push({
      seq: numOrNull(call.seq),
      name: String(call.name || '—'),
      at,
      ms,
      // Длительность словами: `null` — результат не пришёл, и «0 мс» здесь было
      // бы враньём (вызов не мгновенный, он просто не доложил).
      msText: ms === null ? 'длительность неизвестна' : fmtDuration(Math.max(0, Math.round(ms / 1000))),
      clock: fmtClock(at),
    })
  }
  return calls
}

/**
 * Раскладка вызовов на дорожке попытки: где чип, какой ширины, на каком уровне.
 *
 * Ширина чипа — логарифм длительности (`3 + 11·log10(1+ms/100)/log10(1+12000/100)`):
 * линейная шкала превратила бы 136 мс в точку, а 11.5 с — в половину дорожки, и
 * таймлайн перестал бы читаться. Уровни нужны, потому что чипы идут вплотную:
 * сосед занят — берём следующий уровень, дорожка растёт вниз.
 *
 * Арифметика общая для веб-страницы и десктопа: разъехавшиеся формулы дали бы две
 * несравнимые картинки одного и того же дела.
 */
function layoutCalls(calls, options) {
  const opts = options && typeof options === 'object' ? options : {}
  const from = numOrNull(opts.from)
  const to = numOrNull(opts.to)
  // Ширина — виртуальная: раскладка считается в ней, а на экран уходит долями
  // (`leftPct`/`widthPct`). Так обе поверхности рисуют одно и то же без замера
  // DOM: чипы масштабируются вместе с дорожкой, а не разъезжаются по ней.
  const width = Number(opts.width) || 1000
  const levelH = Number(opts.levelH) || 12
  const top = Number(opts.top) || 0
  const maxLevels = Math.max(1, Number(opts.maxLevels) || 5)
  const gap = Number(opts.gap) || 2
  const span = from === null || to === null ? 0 : to - from
  const list = Array.isArray(calls) ? calls : []
  if (!list.length || width <= 0 || span <= 0) {
    return { chips: [], levels: 0, height: top + levelH, clipped: 0 }
  }
  const chipWidth = (ms) => {
    const value = Math.max(0, Number(ms) || 0)
    return Math.round(3 + (11 * Math.log10(1 + value / 100)) / Math.log10(1 + 12000 / 100))
  }
  // `ms: null` — вызов без результата. Рисовать его минимальной шириной значило
  // бы утверждать «0 мс»: длительности нет, а не она нулевая. Отдельная ширина и
  // флаг `unfinished` дают поверхности возможность показать это иначе.
  const UNFINISHED_W = 5
  const lastEnd = []
  const chips = []
  let clipped = 0
  for (const call of list) {
    const px = Math.max(0, Math.min(width, ((call.at - from) / span) * width))
    const unfinished = call.ms === null || call.ms === undefined
    const size = unfinished ? UNFINISHED_W : chipWidth(call.ms)
    let level = lastEnd.findIndex((end) => px - end >= gap)
    if (level < 0) {
      if (lastEnd.length >= maxLevels) {
        // Уровни кончились — вызов всё равно показываем: прятать факт хуже, чем
        // наложить его на соседа. Число наложенных считается в `clipped`.
        level = lastEnd.indexOf(Math.min(...lastEnd))
        clipped += 1
      } else {
        level = lastEnd.length
      }
    }
    lastEnd[level] = px + size
    chips.push({
      call,
      left: Math.round(px * 10) / 10,
      width: size,
      leftPct: Math.round((px / width) * 10000) / 100,
      widthPct: Math.round((size / width) * 10000) / 100,
      level,
      top: top + level * levelH,
      unfinished,
    })
  }
  const levels = lastEnd.length
  return { chips, levels, height: top + levels * levelH, clipped }
}

/* ------------------------------------------- вид таймлайна: одна арифметика
 * Обе поверхности рисуют по этим функциям, а сами только подставляют свой
 * элемент и свои CSS-переменные. Иначе «один расчёт на два экрана» — обещание в
 * комментарии, которое никто не проверяет.
 */

// Геометрия строки: свёрнутая полоса, шаг уровня, отступ сверху, потолок уровней
// и запас под раскрытую карту. Числа живут здесь, а не в двух файлах.
/** Геометрия узла графа спеки: одна на обе поверхности. */
const SPEC_NODE = { w: 76, h: 18, gapX: 14, gapY: 6 }

// Геометрия строки попытки: полоса фона, уровни чипов и строка маркеров.
// Резерв оси под подписи краёв («нач. 13:37:35», «кон. 14:08:28»): метка
// центрируется на своей вертикали, поэтому линия метки проходит через текст
// подписи края. Доля считается от ширины графика в 900 px: подпись края там
// занимает 8 %, половина подписи метки — ещё 2.7 %.
const TICK_EDGE_RESERVE_PCT = 12

const TL = { collapsed: 20, levelH: 12, top: 6, maxLevels: 5, pad: 8 }

// Узлы графа спеки — мелкие: это схема зависимостей, а не главный объект экрана.


// Тон вызова — семантический ключ («чтение», «правка», «запуск», «внешнее»), а не
// CSS-цвет: палитры у поверхностей разные, а вид работы — общий факт.
const TOOL_TONE = [
  // Поиск отделён от чтения: в деле их сотни, и одним цветом карта вызовов
  // читалась как «всё одно и то же».
  [/^(search_files|grep|glob|find|ripgrep)/i, 'search'],
  [/^(read_file|view|cat|ls|list|head|tail|stat)/i, 'read'],
  [/^(write_file|patch|apply_patch|edit|create|mkdir|mv|cp|rm)/i, 'write'],
  [/^(terminal|shell|bash|zsh|sh|run|exec|python|node|go|npm|git|make|cargo)/i, 'run'],
  [/^(web_|browser|http|fetch|curl|wget)/i, 'net'],
]

/**
 * Легенда семейств инструментов: одна на обе поверхности.
 *
 * Цвет на экране имеет смысл только вместе с подписью — иначе «жёлтый» надо
 * угадывать. Легенда живёт слева от дорожек, в той же колонке, что и подписи
 * попыток.
 */
const TOOL_LEGEND = [
  { tone: 'read', label: 'файлы' },
  { tone: 'search', label: 'поиск' },
  { tone: 'write', label: 'правка' },
  { tone: 'run', label: 'команды' },
  { tone: 'net', label: 'сеть' },
]

/** Тон инструмента по имени; неизвестный — `other` («не знаю, что это» — факт). */
function toolTone(name) {
  const hit = TOOL_TONE.find(([pattern]) => pattern.test(String(name || '')))
  return hit ? hit[1] : 'other'
}

// Значки вида этапа: пути в `viewBox 0 0 16 16`. Одна геометрия на обе
// поверхности — разъехавшиеся значки читались бы как разные виды этапов.
const KIND_ICON = {
  agent: 'M8 2.4a2.6 2.6 0 1 0 0 5.2 2.6 2.6 0 0 0 0-5.2ZM3 13.2c0-2.5 2.2-4.1 5-4.1s5 1.6 5 4.1',
  shell: 'M4 5.2 7 8l-3 2.8M8.6 11.4h3.6',
  gate: 'M8 2.2 13.4 8 8 13.8 2.6 8Z',
}

/**
 * Вид строки попытки: подпись, штрихи, чипы и надбавка «сколько не показано».
 *
 * `badge` — то, что нельзя прятать: `+N` вызовов отрезано потолком и `⋯M`
 * наложено из-за нехватки уровней. Обе цифры приходят из данных, а не из
 * «сколько влезло».
 */
function attemptView(bar, scale) {
  const b = bar && typeof bar === 'object' ? bar : {}
  const calls = Array.isArray(b.calls) ? b.calls : []
  const axis = (scale && scale.axis) || {}
  const layout = calls.length
    ? layoutCalls(calls, {
        from: axis.start,
        to: axis.end,
        levelH: TL.levelH,
        top: TL.top,
        maxLevels: TL.maxLevels,
      })
    : { chips: [], height: TL.top + TL.levelH, clipped: 0 }
  const omitted = numOrNull(b.callsOmitted) || 0
  const clipped = layout.clipped || 0
  const notes = []
  if (omitted) notes.push(`ещё ${fmtCount(omitted)} ${plural(omitted, 'вызов', 'вызова', 'вызовов')} не показано (потолок)`)
  if (clipped) notes.push(`${fmtCount(clipped)} ${plural(clipped, 'вызов', 'вызова', 'вызовов')} наложено: уровней не хватило`)
  const chips = layout.chips.map((chip) => {
    const call = chip.call || {}
    // Подсказка и инспектор читают одни и те же поля: имя инструмента, время
    // начала и длительность. Поверхность ничего не досчитывает.
    const tip = `${call.name || '—'} · ${call.clock || '—'} · ${call.msText || '—'}`
    return {
      // Идентификатор вызова — ключ выбора в инспекторе (`stage:attempt:seq`).
      id: `${b.stage || '—'}:${b.number || '—'}:${call.seq === null || call.seq === undefined ? '—' : call.seq}`,
      name: call.name || '—',
      clock: call.clock || '—',
      msText: call.msText || '—',
      seq: call.seq === null || call.seq === undefined ? null : call.seq,
      stage: b.stage || '—',
      attempt: b.number || '—',
      leftPct: chip.leftPct,
      widthPct: chip.widthPct,
      top: chip.top,
      level: chip.level,
      tone: toolTone(call.name),
      unfinished: chip.unfinished === true,
      tip,
      title: tip,
    }
  })
  const status = b.outcomeText || (b.open ? b.leaseText : b.outcome || b.status || b.durationText)

  // Фон попытки: длительность видна полосой, а не только по чипам вызовов.
  // Кромка не бывает уже 0.6% — иначе короткая попытка исчезла бы с экрана.
  const bandLeft = b.start === null ? null : b.leftPct
  const bandWidth =
    b.start === null || b.endPct === null
      ? null
      : Math.max(0.6, Math.round((b.endPct - b.leftPct) * 10) / 10)
  const bandCause = barReasonText(b)
  const bandTip =
    b.start === null
      ? ''
      : `${b.clockStart || '—'}–${b.clockEnd || (b.open ? 'идёт' : '—')} · ${b.durationText || '—'}` +
        `${calls.length ? ` · ${fmtCount(calls.length)} ${plural(calls.length, 'вызов', 'вызова', 'вызовов')}` : ''}` +
        `${bandCause ? ` · ${bandCause}` : ''}`

  // Маркеры внутри попытки: вердикты, вопросы человеку, публикация, закрытие.
  // Засечки старта/конца больше не рисуются — их роль играет фон попытки.
  //
  // Интервал попытки полуоткрытый: событие ровно в секунду стыка принадлежит
  // следующей попытке, а не обеим. Иначе вердикт на стыке рисовался дважды —
  // в той попытке, что закончилась, и в той, что началась.
  // Попытка «владеет» отрезком от своего начала до начала следующей: так
  // событие в промежутке между попытками (человек ответил, следующая ещё не
  // началась) остаётся видимым у той попытки, где вопрос задали, и при этом
  // ни одно событие не рисуется дважды.
  const siblings = scale && Array.isArray(scale.bars) ? scale.bars : []
  const nextStart = siblings.reduce((min, other) => {
    if (other === b || other.start === null || b.start === null || other.start <= b.start) return min
    return min === null || other.start < min ? other.start : min
  }, null)
  const inside = (scale && Array.isArray(scale.markers) ? scale.markers : []).filter((marker) => {
    if (marker.type === 'started' || marker.type === 'ended') return false
    if (b.start === null) return false
    if (marker.at < b.start) return false
    return nextStart === null || marker.at < nextStart
  })
  const marks = inside.map((marker) => ({
    type: marker.type,
    tone: marker.tone,
    pct: marker.pct,
    title: marker.title,
    ariaLabel: marker.ariaLabel,
  }))
  // Пауза рисуется в строке попытки, где вопрос задан: ответ мог прийти уже в
  // следующей попытке, но «стояло на человеке» — здесь.
  const waits = (scale && Array.isArray(scale.waitSpans) ? scale.waitSpans : [])
    .filter((span) => b.start !== null && b.end !== null && span.fromAt >= b.start && span.fromAt <= b.end)
    .map((span) => ({
      fromPct: span.fromPct,
      widthPct: span.widthPct,
      title: span.title,
    }))
  // Свёрнутая строка показывает первый уровень вызовов и маркеры; остальные
  // уровни раскрываются по наведению. Высоту считает шейпер — иначе две
  // поверхности разойдутся в том, «сколько строки видно без наведения».
  // Маркеры журнала рисуются вертикальными палками на всю высоту строки, поэтому
  // отдельной полосы под чипами больше нет: высота строки не зависит от того,
  // случился ли в ней вердикт, — иначе соседние попытки разъезжались по высоте.
  const full = Math.max(TL.collapsed, layout.height + TL.pad)
  const collapsed = Math.min(full, TL.collapsed)
  return {
    full,
    collapsed,
    // Тон фона — тон исхода попытки: провал виден полосой на дорожке, а не
    // только словом в подписи слева.
    band:
      bandLeft === null || bandWidth === null
        ? null
        : { leftPct: bandLeft, widthPct: bandWidth, tone: b.tone || 'grey' },
    failed: b.tone === 'red',
    bandTip,
    label: {
      number: b.number || '—',
      status: status || '—',
      tone: b.tone || 'grey',
    },
    ticks: chips.map((chip) => ({
      leftPct: chip.leftPct,
      widthPct: chip.widthPct,
      tone: chip.tone,
      unfinished: chip.unfinished,
    })),
    chips,
    marks,
    waits,
    // Надбавка на экране: `+N` — обрезано данными, `⋯M` — наложено рисунком.
    badge: [omitted ? `+${fmtCount(omitted)}` : '', clipped ? `⋯${fmtCount(clipped)}` : '']
      .filter(Boolean)
      .join(' '),
    note: notes.join(' · '),
    title: [b.title || '', notes.join(' · ')].filter(Boolean).join(' · '),
  }
}

/**
 * Разбивка попыток по этапам одной фразой: `check 1 · review 1 · verify 3`.
 *
 * Пустая строка, когда разбивать нечего (попыток нет или все в одном этапе):
 * подсказка, повторяющая число, ничего не добавляет.
 */
function attemptsBreakdown(lanes, bars) {
  const list = Array.isArray(lanes) ? lanes.filter((lane) => Number(lane.count) > 0) : []
  if (list.length < 2) return ''
  return list.map((lane) => `${lane.label} ${fmtCount(lane.count)}`).join(' · ')
}

/**
 * Блоки документации дела: `[{kind, label, name, path, text, truncated}]`.
 *
 * `body` из store и спека — разные документы, и называть одно другим нельзя.
 * У дел из `bin/duty create` свободного `body` нет вовсе: описание живёт
 * комментариями в шапке `duty.yaml`, поэтому спека показывается первой, а `body`
 * — отдельным блоком, когда он есть. Пустой список — нечего рисовать: панель,
 * которая говорит «пусто», отбирает колонку и ничего не сообщает. Порядок
 * стабилен (тело, затем спека), чтобы поверхность не решала, что важнее.
 */
function docBlocks(data) {
  const src = data || {}
  const out = []
  const body = String(src.body || '').trim()
  if (body) {
    out.push({
      kind: 'body',
      label: 'описание',
      name: '',
      path: '',
      text: body,
      truncated: Boolean(src.body_truncated),
    })
  }
  const specText = String(src.spec_text || '').trim()
  if (specText) {
    out.push({
      kind: 'spec',
      label: 'спецификация',
      name: fileLabel(src.spec),
      path: String(src.spec || ''),
      text: specText,
      truncated: Boolean(src.spec_text_truncated),
    })
  }
  return out
}

/** Вид дорожки этапа: имя, вид, сводка попыток и строки. */
function laneView(lane, bars, scale) {
  const list = Array.isArray(bars) ? bars : []
  const first = list[0]
  const shown = list.reduce((sum, bar) => sum + (Array.isArray(bar.calls) ? bar.calls.length : 0), 0)
  const omitted = list.reduce((sum, bar) => sum + (numOrNull(bar.callsOmitted) || 0), 0)
  const total = shown + omitted
  const meta = [
    `${list.length} ${plural(list.length, 'попытка', 'попытки', 'попыток')}`,
    total
      ? `${fmtCount(total)} ${plural(total, 'вызов', 'вызова', 'вызовов')}${
          omitted ? ` (показано ${fmtCount(shown)})` : ''
        }`
      : '',
  ]
    .filter(Boolean)
    .join(' · ')
  return {
    name: String((lane && lane.label) || '—'),
    kind: first ? String(first.kind || 'agent') : '',
    kindText: first ? (first.kind === 'agent' ? String(first.role || 'профиль') : String(first.kind)) : '',
    icon: first ? KIND_ICON[first.kind] || KIND_ICON.shell : '',
    meta,
    empty: list.length === 0,
    rows: list.map((bar) => ({ key: `${bar.laneKey || ''}-${bar.number || ''}`, view: attemptView(bar, scale) })),
  }
}

function attemptBar(attempt, crashes) {
  const a = attempt && typeof attempt === 'object' ? attempt : {}
  const start = numOrNull(a.start)
  const end = numOrNull(a.end)
  const duration = numOrNull(a.duration_sec)
  const lease = String(a.lease || '')
  const open = start !== null && end === null
  const dead = open && (lease === 'expired' || a.dead === true || a.status === 'orphan')
  const stage = String(a.stage || a.profile || '—')
  return {
    key: String(a.run_id === null || a.run_id === undefined ? '' : a.run_id),
    number: a.attempt ? `#${a.attempt}` : '',
    stage,
    laneKey: stage,
    laneLabel: stage,
    lane: 0,
    // Вид этапа и роль — из попытки: значок на дорожке («профиль», `shell`)
    // говорит то же, что и лента этапов, а не угадывается по имени этапа.
    kind: String(a.stage_kind || 'agent'),
    // Роль приходит как `role` (`/deal`) или `profile` (`/duty/{id}/attempts`) —
    // две ручки зовут одно и то же по-разному, и значок должен знать оба имени.
    role: String(a.role || a.profile || ''),
    start,
    end,
    endAt: null,
    duration,
    durationSec: null,
    durationText: '',
    open,
    lease,
    leaseText: open ? leaseWord(lease, a.status) : '',
    progress: numOrNull(a.progress),
    tools: numOrNull(a.tools),
    // Вызовы инструментов: `[{seq, name, at, ms}]` из телеметрии. Пустой список —
    // у попытки не было телеметрии, а не «вызовов не делала»: это разные факты,
    // и разницу называет `callsText`.
    calls: shapeCalls(a.calls),
    callsOmitted: numOrNull(a.calls_omitted) || 0,
    // Израсходовано токенов, если телеметрия их записала: `null` — не «ноль».
    tokens: numOrNull(a.tokens),
    callsText: '',
    outcome: String(a.outcome || ''),
    status: String(a.status || ''),
    // Причина отказа и его класс (`contract`, `infra`, …): экран называет их
    // в подсказке провалившейся попытки — «провал» без причины ничего не
    // объясняет, а лезть за ней в журнал руками человек не должен.
    reason: String(a.reason || '').trim(),
    failureKind: String(a.kind || '').trim(),
    tone: open ? (dead ? 'red' : 'blue') : outcomeTone(a.outcome, a.status, crashes),
    leftPct: 0,
    endPct: 0,
    widthPct: 0,
    clockStart: fmtClock(start),
    clockEnd: '',
    label: '',
    title: '',
    ariaLabel: '',
    progressText: '',
    toolsText: '',
    // Откуда полоса: `attempts` — ответ ручки попыток, `journal` — пара
    // `attempt_started`/`attempt_ended`, когда ручка попыток пуста (старый бэкенд
    // или дело без рана kanban). Экран показывает разницу, а не прячет.
    source: 'attempts',
  }
}

/**
 * Стадия из выжимки `attempt_started`: там лежит JSON (`{"run_id": "review#1",
 * "stage": "review", …}`), обрезанный по `SUMMARY_LIMIT`. Разбор JSON ломался бы
 * на обрезке, поэтому берём два поля по образцу — их видно в начале строки.
 */
function stageOfSummary(summary) {
  const text = String(summary || '')
  const stage = /"stage":\s*"([^"]+)"/.exec(text)
  if (stage) return stage[1]
  const run = /"run_id":\s*"([^"#]+)#(\d+)"/.exec(text)
  return run ? run[1] : ''
}

/** Номер попытки из той же выжимки: `attempt` первым делом, иначе из `run_id`. */
function attemptOfSummary(summary) {
  const text = String(summary || '')
  const attempt = /"attempt":\s*(\d+)/.exec(text)
  if (attempt) return Number(attempt[1])
  const run = /"run_id":\s*"([^"#]+)#(\d+)"/.exec(text)
  return run ? Number(run[2]) : 0
}

/**
 * Строковые поля выжимки события по образцу `"key": "value"`.
 *
 * `JSON.parse` здесь не годится: бэкенд обрезает `summary` по `SUMMARY_LIMIT`
 * и строка часто недописана. Берём только нужные ключи, которые стоят в начале
 * выжимки; чего нет — того нет, и маркер это честно покажет.
 */
function summaryFields(summary, keys) {
  const text = String(summary || '')
  const out = {}
  for (const key of keys) {
    const found = new RegExp(`"${key}":\\s*"([^"]*)"`).exec(text)
    if (found) out[key] = found[1]
  }
  return out
}

/**
 * Полосы из журнала: пары `attempt_started` ↔ `attempt_ended`, когда ответ
 * попыток пуст.
 *
 * Второй копии расчёта здесь нет — это тот же шейпер, просто другой источник
 * факта: `attempt_ended` гейта и отказа по бюджету пишется без начала
 * (`docs/JOURNAL.md`), и такие события полосой не становятся — они уходят на
 * ленту маркеров.
 *
 * Исход берётся из выжимки `attempt_ended` по образцу `"outcome": "…"`: она
 * обрезана по `SUMMARY_LIMIT`, но ключ стоит в начале строки, и цвет полосы не
 * приходится выдумывать по её длительности.
 */
function journalBars(events, crashes) {
  const bars = []
  const open = []
  for (const event of events) {
    if (!(event.at > 0)) continue
    if (event.kind === 'attempt_started') {
      const stage = stageOfSummary(event.summary)
      const bar = {
        key: `journal:${event.at}:${bars.length}`,
        number: attemptOfSummary(event.summary) ? `#${attemptOfSummary(event.summary)}` : '',
        stage: stage || 'попытка',
        laneKey: stage || 'попытка',
        laneLabel: stage || 'попытка',
        lane: 0,
        start: event.at,
        end: null,
        endAt: null,
        duration: null,
        durationSec: null,
        durationText: '',
        open: false,
        lease: '',
        leaseText: '',
        progress: null,
        tools: null,
        // У полосы из журнала телеметрии нет: событие `attempt_started` знает
        // только про старт. Пустой список здесь — «вызовов в журнале нет», а не
        // «попытка ничего не делала»: экран так и подписывает.
        calls: [],
        callsOmitted: 0,
        callsText: '',
        kind: 'agent',
        role: '',
        outcome: '',
        status: '',
        tone: 'gray',
        leftPct: 0,
        endPct: 0,
        widthPct: 0,
        clockStart: fmtClock(event.at),
        clockEnd: '',
        label: '',
        title: '',
        ariaLabel: '',
        progressText: '',
        toolsText: '',
        source: 'journal',
      }
      bars.push(bar)
      open.push(bar)
    } else if (event.kind === 'attempt_ended' && markerType(event.kind, event.summary) === 'ended') {
      const bar = open.shift()
      if (!bar) continue
      bar.end = event.at
      bar.duration = event.at - bar.start
      bar.outcome = String((/"outcome":\s*"([^"]*)"/.exec(event.summary) || [])[1] || '')
      // Журнал пишет причину и класс отказа рядом с исходом — берём их оттуда,
      // иначе у дела без ручки попыток провал остаётся без объяснения.
      const ended = summaryFields(event.summary, ['reason', 'kind'])
      bar.reason = String(ended.reason || '').trim()
      bar.failureKind = String(ended.kind || '').trim()
      bar.tone = outcomeTone(bar.outcome, '', crashes)
    }
  }
  // Незакрытая пара — та же незакрытая попытка: тянется до `now` пунктиром.
  for (const bar of open) bar.open = true
  return bars
}

/**
 * Шкала времени дела: одна ось, дорожки этапов, полосы попыток, маркеры событий
 * и строка фактов шапки. Единственный расчёт геометрии на обе поверхности.
 *
 * Ось: начало — минимум из `timeline[].at` и `attempts[].start`, конец — максимум
 * из `at`/`end`, у живого дела — `now`. Полоса ставится по `start` и тянется на
 * `duration_sec`; параллельные этапы живут на своих дорожках, поэтому перекрытие
 * видно глазом, а не по легенде.
 *
 * Свой расчёт чисел шапки не заводится: бюджет берётся блоком `budget` ответа,
 * публикация — блоком `published`, закрытие — блоком `closed`; здесь только
 * раскладка по словам и процентам.
 */
/**
 * Граф спеки: узлы — этапы, рёбра — `needs`. Считается здесь, а не на
 * поверхностях: обе рисуют одни и те же координаты, поэтому картинка не может
 * «поехать» на одной из них. Глубина узла — самый длинный путь от корня, так что
 * этап встаёт правее всех, после кого он ждёт.
 */
function specView(rawStages) {
  const stages = (Array.isArray(rawStages) ? rawStages : []).map(shapeStage)
  if (!stages.length) return null
  const byId = new Map(stages.map((stage) => [stage.id, stage]))
  const depths = new Map()
  const depthOf = (id, trail) => {
    if (depths.has(id)) return depths.get(id)
    // Цикл в `needs` — не повод падать: этап встаёт на первый уровень.
    if (trail.has(id)) return 0
    trail.add(id)
    const stage = byId.get(id)
    const needs = stage ? stage.needs.filter((need) => byId.has(need)) : []
    const depth = needs.length ? Math.max(...needs.map((need) => depthOf(need, trail) + 1)) : 0
    trail.delete(id)
    depths.set(id, depth)
    return depth
  }
  const columns = []
  const nodes = stages.map((stage) => {
    const depth = depthOf(stage.id, new Set())
    const row = columns[depth] === undefined ? 0 : columns[depth]
    columns[depth] = row + 1
    // Провал попытки — факт этапа, даже если сейчас он пройден: раньше схема
    // показывала только текущее состояние, и «здесь падало» исчезало из памяти.
    const fails = stage.attempts.filter((attempt) => attempt.tone === 'red').length
    return {
      id: stage.id,
      role: stage.role,
      label: stage.stateLabel,
      tone: stage.tone,
      fails,
      failsText: fails ? `провалов ${fails}` : '',
      kind: stage.kind || 'agent',
      needsText: stage.needsText,
      depth,
      row,
      x: depth * (SPEC_NODE.w + SPEC_NODE.gapX),
      y: row * (SPEC_NODE.h + SPEC_NODE.gapY),
    }
  })
  const edges = []
  for (const stage of stages) {
    for (const need of stage.needs) {
      if (!byId.has(need)) continue
      edges.push({ from: need, to: stage.id })
    }
  }
  const width = Math.max(...nodes.map((node) => node.x + SPEC_NODE.w))
  const height = Math.max(...nodes.map((node) => node.y + SPEC_NODE.h))
  return { nodes, edges, width, height, nodeW: SPEC_NODE.w, nodeH: SPEC_NODE.h }
}

function shapeDealTime(detail, attemptsPayload) {
  const d = detail && typeof detail === 'object' ? detail : {}
  const a = attemptsPayload && typeof attemptsPayload === 'object' ? attemptsPayload : {}
  const now = numOrNull(d.now) ?? numOrNull(a.now)
  // «Живое» — по `terminal` ответа: закрытое дело ось не тянет вперёд, и его
  // длительность перестаёт расти в ту же секунду, когда записан `completed`.
  // Событие закрытия в журнале — тот же признак, если сервер не отдал `terminal`.
  const events = (Array.isArray(d.timeline) ? d.timeline : []).map((event) => {
    const e = event && typeof event === 'object' ? event : {}
    // Текст вопроса и ответа, причина закрытия — поля ответа бэкенда, а не
    // разбор `summary`: журнал хранит номер вопроса, пул — сам вопрос.
    return {
      kind: String(e.kind || ''),
      at: numOrNull(e.at) || 0,
      summary: String(e.summary || ''),
      question_text: e.question_text ? String(e.question_text) : '',
      answer_text: e.answer_text ? String(e.answer_text) : '',
      cause: e.cause ? String(e.cause) : '',
      stages: Array.isArray(e.stages) ? e.stages.map(String) : [],
    }
  })
  const closedInJournal = events.some((event) => markerType(event.kind, event.summary) === 'closed')
  const live = d.terminal !== true && !closedInJournal
  const rows = Array.isArray(a.attempts) ? a.attempts : []
  const crashes = Array.isArray(a.crash_outcomes) ? a.crash_outcomes.map(String) : []
  const bars = rows.length ? rows.map((row) => attemptBar(row, crashes)) : journalBars(events, crashes)

  // Ось: только записанные времена. Пусто — шкалы нет, и рисовать «нулевую ось»
  // значило бы выдумать интервал, которого в журнале не было.
  const stamps = []
  for (const event of events) if (event.at > 0) stamps.push(event.at)
  for (const bar of bars) if (bar.start > 0) stamps.push(bar.start)
  const known = stamps.length > 0
  const axisStart = known ? Math.min(...stamps) : null
  let axisEnd = known ? Math.max(...stamps) : null
  if (live && now !== null) axisEnd = axisEnd === null ? now : Math.max(axisEnd, now)
  // Закрытая попытка без `end`, но с длительностью: конец считаем по ней, иначе
  // полоса оборвалась бы на `start` и шкала «съела» самый длинный этап.
  for (const bar of bars) {
    if (bar.start === null) continue
    const stop = bar.end !== null ? bar.end : bar.start + (bar.duration || 0)
    if (stop > (axisEnd === null ? 0 : axisEnd) && !live) axisEnd = stop
  }
  const span = axisStart === null || axisEnd === null ? 0 : Math.max(1, axisEnd - axisStart)
  const pctOf = (at) => {
    if (axisStart === null || !(at > 0) || span === 0) return 0
    const value = ((at - axisStart) / span) * 100
    return Math.round(Math.max(0, Math.min(100, value)) * 10) / 10
  }

  // Дорожки: сначала этапы графа в его порядке (`stages[]` ответа `/deal`) —
  // тогда дорожек столько же, сколько этапов, и видно, какой этап ещё не
  // начинался; потом этапы, которых в графе нет (попытка без спеки, дело
  // kanban), в порядке появления на оси.
  const lanes = []
  const laneIndex = new Map()
  const addLane = (key) => {
    if (!key || laneIndex.has(key)) return
    laneIndex.set(key, lanes.length)
    lanes.push({ key, label: key, count: 0 })
  }
  for (const stage of Array.isArray(d.stages) ? d.stages : []) addLane(String((stage && stage.id) || ''))
  for (const bar of [...bars].sort((left, right) => (left.start || 0) - (right.start || 0))) {
    addLane(bar.laneKey)
    bar.lane = laneIndex.get(bar.laneKey)
    lanes[bar.lane].count += 1
  }
  if (!lanes.length) bars.forEach((bar) => { bar.lane = 0 })

  for (const bar of bars) {
    const stop = bar.open
      ? (now !== null ? now : bar.start)
      : bar.end !== null
        ? bar.end
        : bar.start === null
          ? null
          : bar.start + (bar.duration || 0)
    bar.endAt = stop
    bar.leftPct = pctOf(bar.start)
    bar.endPct = pctOf(stop)
    bar.widthPct = bar.start === null ? 0 : Math.max(BAR_MIN_PCT, Math.round((bar.endPct - bar.leftPct) * 10) / 10)
    bar.durationSec = bar.open
      ? (bar.start !== null && now !== null ? Math.max(0, now - bar.start) : null)
      : bar.duration !== null
        ? bar.duration
        : bar.start !== null && bar.end !== null
          ? bar.end - bar.start
          : null
    bar.durationText = bar.open ? 'идёт' : fmtDuration(bar.durationSec)
    bar.clockEnd = bar.open ? (now !== null ? fmtClock(now) : '—') : fmtClock(stop)
    bar.progressText = bar.progress === null ? '' : `шагов ${fmtCount(bar.progress)}`
    bar.toolsText = bar.tools === null ? '' : `инструментов ${fmtCount(bar.tools)}`
    // Вызовы: сколько их в телеметрии и не обрезано ли. Это отдельная строка от
    // `инструментов` — там счётчик движка, здесь то, что реально можно нарисовать.
    // Здесь только то, что реально нарисовано: «ещё N не показано» называет
    // вид строки (`attemptView`), иначе одна и та же цифра звучала бы дважды.
    bar.callsText = (bar.calls || []).length
      ? `${fmtCount(bar.calls.length)} ${plural(bar.calls.length, 'вызов', 'вызова', 'вызовов')}`
      : ''
    // Подпись внутри полосы — исход, а не догадка: у незакрытой это слово о
    // лизинге, у закрытой без исхода — её длительность.
    bar.label = bar.open ? bar.leaseText : outcomeWord(bar.outcome) || bar.status || bar.durationText
    // Короткая подпись строки таймлайна: исход по-русски, у незакрытой — лизинг.
    bar.outcomeText = bar.open ? bar.leaseText : outcomeWord(bar.outcome) || bar.status || bar.durationText
    const details = [bar.progressText, bar.toolsText, bar.callsText].filter(Boolean).join(', ')
    const head = `${bar.laneLabel} · попытка ${bar.number || '—'}`
    const interval = `${bar.clockStart}–${bar.clockEnd} (${bar.durationText})`
    const cause = barReasonText(bar)
    const tail = bar.open
      ? `лизинг: ${bar.leaseText}`
      : `исход: ${outcomeWord(bar.outcome) || bar.status || 'не записан'}${cause ? ` · ${cause}` : ''}`
    bar.title = `${head} · ${interval} · ${tail}${details ? ` · ${details}` : ''}`
    bar.ariaLabel = bar.title
  }

  // Маркеры: только факты журнала. Вопрос без ответа получает интервал до `now` —
  // это и есть «сколько человек думает», а не догадка о его решении.
  const markers = []
  const pending = []
  for (const event of events) {
    const type = markerType(event.kind, event.summary)
    if (!type) continue
    const meta = MARKER_META[type]
    const clock = fmtClock(event.at)
    // Подпись маркера — слова, а не payload: номер вопроса и JSON причины
    // человеку ничего не говорят. Текст вопроса и ответа даёт бэкенд
    // (`question_text`/`answer_text` из пула), причину закрытия — поля `cause`.
    const body = markerBody(type, event)
    const title = `${clock} · ${meta.label}${body ? ` — ${body}` : ''}`
    const marker = {
      type,
      kind: event.kind,
      at: event.at,
      pct: pctOf(event.at),
      clock,
      label: meta.label,
      tone: meta.tone,
      tick: meta.tick,
      summary: event.summary,
      // Человеческое тело подписи: вопрос/ответ текстом, причина закрытия
      // словами. Пауза «вопрос → ответ» называет им сам вопрос.
      text: body,
      title,
      ariaLabel: title,
      answered: false,
      waitPct: 0,
      waitText: '',
    }
    if (type === 'question') pending.push(marker)
    // Ответ закрывает первый неотвеченный вопрос: порядок событий и есть порядок
    // ответов, а `question_id` в выжимке бывает обрезан.
    else if (type === 'answer' && pending.length) pending.shift().answered = true
    markers.push(marker)
  }
  // Паузы «вопрос → ответ»: считается на всю ось, потому что ответ часто
  // приходит уже в следующей попытке. Полоса рисуется в строке той попытки, где
  // вопрос был задан, — там, где дело и встало.
  const waitSpans = []
  {
    let asked = null
    for (const marker of markers) {
      if (marker.type === 'question') asked = marker
      else if (marker.type === 'answer' && asked) {
        waitSpans.push({
          fromAt: asked.at,
          toAt: marker.at,
          fromPct: asked.pct,
          widthPct: Math.max(0.4, Math.round((marker.pct - asked.pct) * 10) / 10),
          title: `${asked.clock} → ${marker.clock}: ждал человека${asked.text ? ` — ${asked.text}` : ''}`,
        })
        asked = null
      }
    }
    if (asked && now !== null) {
      waitSpans.push({
        fromAt: asked.at,
        toAt: now,
        fromPct: asked.pct,
        widthPct: Math.max(0.4, Math.round((pctOf(now) - asked.pct) * 10) / 10),
        title: `${asked.clock} → сейчас: ждёт ответа`,
      })
    }
  }

  for (const marker of pending) {
    marker.waitText = 'ждёт ответа'
    if (now !== null && marker.at > 0) {
      marker.waitPct = Math.max(0, Math.round((pctOf(now) - marker.pct) * 10) / 10)
      marker.title = `${marker.title} · ждёт ответа ${fmtAge(now - marker.at)}`
      marker.ariaLabel = marker.title
    }
  }

  // Шапка дела: числа из ответа, ни одного нового расчёта на бэкенде.
  const answered = markers.filter((marker) => marker.type === 'answer').length
  const waiting = pending.length
  const asked = markers.filter((marker) => marker.type === 'question').length
  const budgetRaw = d.budget && typeof d.budget === 'object' ? d.budget : a.budget && typeof a.budget === 'object' ? a.budget : null
  let budget = null
  if (budgetRaw) {
    const units = []
    for (const [unit, label] of [['tokens', 'токенов'], ['minutes', 'минут']]) {
      const part = budgetRaw[unit]
      if (!part || typeof part !== 'object') continue
      const spent = Number(part.spent) || 0
      const limit = Number(part.limit) || 0
      const share = limit > 0 ? Math.min(100, Math.round((spent / limit) * 100)) : 0
      units.push({ unit, label, spent, limit, pct: share, text: `${label} ${fmtCount(spent)} / ${fmtCount(limit)} (${share}%)` })
    }
    const rawText = String(budgetRaw.text || '')
    budget = {
      units,
      exhausted: budgetRaw.exhausted ? String(budgetRaw.exhausted) : '',
      attempts: Number(budgetRaw.attempts) || 0,
      text: units.length ? units.map((item) => item.text).join(' · ') : rawText,
    }
  }
  // Публикация: поле ответа, а если его нет (сервер старше 0.0.5) — событие
  // `published` журнала: те же sha/branch/verified_by, второй расчёт не нужен.
  const pubEvent = [...events].reverse().find((event) => event.kind === 'published')
  const pubRaw = d.published && typeof d.published === 'object'
    ? d.published
    : summaryFields(pubEvent && pubEvent.summary, ['sha', 'branch', 'ref', 'verified_by', 'mr_url'])
  // Форма публикации одна с экраном «Завершённые»: `fmtPublication`.
  const published = fmtPublication(pubRaw)
  // Закрытие: поле ответа, иначе событие журнала (`completed`/`blocked`/`failed`
  // с `cause`) — тот же факт, что рисует маркер `closed`.
  const closedEvent = [...events].reverse().find((event) => markerType(event.kind, event.summary) === 'closed')
  const closedRaw = d.closed && typeof d.closed === 'object'
    ? d.closed
    : closedEvent
      ? { at: closedEvent.at, cause: summaryFields(closedEvent.summary, ['cause']).cause || '' }
      : null
  const closed = closedRaw
    ? {
        at: numOrNull(closedRaw.at),
        clock: fmtClock(closedRaw.at),
        cause: String(closedRaw.cause || ''),
        // Причина закрытия — словами, как и в маркере: `all_stages_done` в
        // человеческой строке читается как сбой экрана, а не как факт.
        text: `закрыто ${fmtClock(closedRaw.at)}${
          closedRaw.cause ? `: ${CLOSE_CAUSE_WORD[String(closedRaw.cause)] || closedRaw.cause}` : ''
        }`,
      }
    : null
  const durationSec = axisStart === null || axisEnd === null ? null : Math.max(0, axisEnd - axisStart)

  // Ось — линейка, а не три подписи (как в согласованном макете `d-merge`):
  // деление выбирается так, чтобы на ширине было 4–8 меток, и через каждую метку
  // идёт вертикальная линия по всем дорожкам. Шаг кратен 2/5/10 минутам, поэтому
  // метки попадают на «круглые» секунды и не пляшут при обновлении.
  const axisTicks = []
  if (axisStart !== null && axisEnd !== null && durationSec > 0) {
    // Лестница делений: берём первое, при котором на оси не больше восьми меток.
    // Иначе часовое дело получало бы тринадцать подписей, а минутное — две.
    const step = [60, 120, 300, 600, 900, 1200, 1800, 2400, 3600, 7200, 10800, 21600, 43200, 86400]
      .find((candidate) => durationSec / candidate <= 8) || 86400
    axisTicks.push({ pct: 0, edge: 'нач.', align: 'start', clock: fmtClock(axisStart) })
    for (let at = Math.ceil(axisStart / step) * step; at < axisEnd; at += step) {
      const pct = Math.round(((at - axisStart) / durationSec) * 1000) / 10
      // Подписи краёв широкие («нач. 13:37:35» — двенадцать знаков), а метка
      // центрируется на своей линии: её текст и вертикаль заходят на подпись
      // края. Резерв — ширина подписи края (8 % при графике в 900 px) плюс
      // половина подписи метки, итого 12 %; внутри него метку не рисуем вовсе,
      // чтобы её линия не перечёркивала слово.
      if (pct < TICK_EDGE_RESERVE_PCT || pct > 100 - TICK_EDGE_RESERVE_PCT) continue
      axisTicks.push({ pct, edge: '', align: 'center', clock: fmtClock(at) })
    }
    axisTicks.push({ pct: 100, edge: 'кон.', align: 'end', clock: fmtClock(axisEnd) })
  }
  // KPI-строка (макет `d-merge`): пять чисел шапки, все — суммы по уже собранным
  // полосам и попыткам. Токены показываем только когда их записала телеметрия:
  // «—» здесь честнее нуля.
  const callsShown = bars.reduce((sum, bar) => sum + bar.calls.length, 0)
  const callsHidden = bars.reduce((sum, bar) => sum + (bar.callsOmitted || 0), 0)
  const tokensKnown = bars.some((bar) => bar.tokens !== null && bar.tokens !== undefined)
  const tokensTotal = bars.reduce((sum, bar) => sum + (numOrNull(bar.tokens) || 0), 0)
  // Легенда семейств инструментов с числами: цвет без подписи — ребус, без
  // числа — «что-то было». Порядок как в `TOOL_LEGEND`, чтобы легенда не
  // прыгала от дела к делу.
  const toolCounts = new Map()
  for (const bar of bars) {
    for (const call of bar.calls) {
      const tone = toolTone(call.name)
      toolCounts.set(tone, (toolCounts.get(tone) || 0) + 1)
    }
  }
  const tools = TOOL_LEGEND.map((item) => ({ tone: item.tone, label: item.label, count: toolCounts.get(item.tone) || 0 }))
    .filter((item) => item.count > 0)

  const kpi = [
    { key: 'duration', label: 'длительность', value: durationSec === null ? '—' : fmtDuration(durationSec) },
    { key: 'stages', label: 'этапов', value: fmtCount(lanes.filter((lane) => lane.count > 0).length) },
    // Разбивка по этапам живёт подсказкой этой ячейки: отдельной строкой она
    // повторяла то же число второй раз.
    {
      key: 'attempts',
      label: 'попыток',
      value: fmtCount(bars.length),
      title: attemptsBreakdown(lanes, bars),
    },
    {
      key: 'calls',
      label: 'вызовов',
      value: callsShown + callsHidden > 0 ? fmtCount(callsShown + callsHidden) : '—',
      note: callsHidden > 0 ? `показано ${fmtCount(callsShown)}` : '',
    },
    tokensKnown
      ? { key: 'tokens', label: 'токенов', value: fmtCount(tokensTotal) }
      : { key: 'tokens', label: 'токенов', value: '—', note: 'телеметрия не писала' },
  ]

  return {
    now,
    live,
    empty: bars.length === 0 && markers.length === 0,
    axis: {
      start: axisStart,
      end: axisEnd,
      spanSec: durationSec,
      live,
      text: axisStart === null || axisEnd === null ? 'времени в журнале нет' : `${fmtClockShort(axisStart)}–${fmtClockShort(axisEnd)}`,
      ticks: axisTicks,
    },
    lanes,
    bars,
    markers,
    waitSpans,
    tools,
    kpi,
    // Граф спеки — из `stages[]` ответа `/deal`; нет этапов — нет и графа.
    spec: specView(d.stages),
    facts: {
      attempts: {
        total: bars.length,
        // «по этапам» — только этапы, где попытки были: `gate 0` в шапке читалось
        // бы как сломанный этап, а не как «ещё не начинался».
        byStage: lanes
          .filter((lane) => lane.count > 0)
          .map((lane) => ({ key: lane.key, label: lane.label, count: lane.count })),
        text: lanes
          .filter((lane) => lane.count > 0)
          .map((lane) => `${lane.label} ${lane.count}`)
          .join(' · '),
        journal: bars.length > 0 && bars.every((bar) => bar.source === 'journal'),
      },
      duration: { sec: durationSec, text: durationSec === null ? '—' : fmtDuration(durationSec), live },
      questions: {
        asked,
        answered,
        waiting,
        text: `${asked} ${plural(asked, 'вопрос', 'вопроса', 'вопросов')}${
          waiting ? ` · ${waiting} ${plural(waiting, 'ждёт', 'ждут', 'ждут')} ответа` : ''
        }`,
      },
      budget,
      published,
      closed,
    },
    counts: { bars: bars.length, lanes: lanes.length, markers: markers.length },
  }
}
// duty:model-end

/* ------------------------------- таймлайн вызовов внутри попытки (0.0.8) */

// Палитра — единственное своё у поверхности: тон приходит из шейпера ключом.
// Тон приходит из шейпера ключом; палитра — единственное своё у поверхности.
const DUTY_TONE_VAR = {
  read: 'var(--ui-blue)',
  search: 'var(--ui-purple)',
  write: 'var(--ui-green)',
  run: 'var(--ui-yellow)',
  net: 'var(--ui-red)',
  other: 'var(--ui-text-tertiary)',
}
function dutyToneVar(tone) {
  return DUTY_TONE_VAR[tone] || DUTY_TONE_VAR.other
}

/**
 * Строка попытки: фон-полоса на всю длительность, чипы вызовов и маркеры внутри.
 *
 * Границы попытки показывает фон, поэтому засечек старта/конца нет, а время
 * начала, конца и длительность живут в подсказке строки — они не спорят с чипами
 * за место на дорожке. Вид приходит готовым (`attemptView`), здесь только DOM.
 */
function DutyAttemptRow({ view, callSel, onSelectCall, toolFilter }) {
  // Подсветка семейства: чужие вызовы гаснут, но остаются на месте — картина
  // дела не рассыпается, просто читается другое.
  const active = Array.isArray(toolFilter) ? toolFilter : []
  const label = h(
    'div',
    { className: 'duty-tl-lbl', 'data-tip': view.bandTip || view.title || '' },
    h('span', { className: 'duty-tl-n' }, view.label.number),
    h('span', { className: `duty-tl-st t-${view.label.tone}` }, view.label.status),
    view.badge ? h('span', { className: 'duty-tl-more', title: view.note || view.title }, view.badge) : null,
  )
  const track = []
  if (view.band) {
    track.push(
      h('span', {
        key: 'band',
        className: `duty-tl-band t-${view.band.tone || 'grey'}`,
        style: { left: `${view.band.leftPct}%`, width: `${view.band.widthPct}%` },
        'data-tip': view.bandTip,
      }),
    )
  }
  for (const [i, wait] of view.waits.entries()) {
    track.push(
      h('span', {
        key: `wait-${i}`,
        className: 'duty-tl-wait',
        style: { left: `${wait.fromPct}%`, width: `${wait.widthPct}%` },
        'data-tip': wait.title,
      }),
    )
  }
  for (const [i, chip] of view.chips.entries()) {
    track.push(
      h('span', {
        key: `call-${i}`,
        className: `duty-tl-call${chip.unfinished ? ' duty-tl-unfinished' : ''}${callSel === chip.id ? ' duty-tl-sel' : ''}${active.length && !active.includes(chip.tone) ? ' duty-tl-dim' : ''}`,
        'data-level': String(chip.level || 0),
        'data-tip': chip.tip || chip.title,
        'aria-label': chip.tip || chip.title,
        onClick: onSelectCall ? (event) => { event.stopPropagation(); onSelectCall(chip.id) } : undefined,
        style: chip.unfinished
          ? { left: `${chip.leftPct}%`, width: `${chip.widthPct}%`, top: `${chip.top}px`, color: dutyToneVar(chip.tone) }
          : { left: `${chip.leftPct}%`, width: `${chip.widthPct}%`, top: `${chip.top}px`, background: dutyToneVar(chip.tone) },
      }),
    )
  }
  if (view.marks.length) {
    track.push(
      h(
        'div',
        { key: 'marks', className: 'duty-tl-marks' },
        ...view.marks.map((mark, i) =>
          h(
            'span',
            {
              key: `mark-${i}`,
              className: `duty-tl-mark t-${mark.tone}`,
              style: { left: `${mark.pct}%` },
              'data-tip': mark.title,
              'aria-label': mark.ariaLabel,
            },
            h('i', null),
          ),
        ),
      ),
    )
  }
  return h(
    'div',
    // Свёрнутая высота — первый уровень вызовов и маркеры, полная раскрывается
    // под указателем. Числа из шейпера, CSS только переключает.
    {
      className: `duty-tl-row${view.failed ? ' is-failed' : ''}`,
      style: { '--duty-tl-collapsed': `${view.collapsed}px`, '--duty-tl-full': `${view.full}px` },
    },
    label,
    h('div', { className: 'duty-tl-track' }, ...track),
  )
}

/** Дорожка этапа: имя, значок вида, сводка и строки попыток. */
function DutyLane({ lane, bars, scale, callSel, onSelectCall, toolFilter }) {
  const view = laneView(lane, bars, scale)
  return h(
    'div',
    { className: 'duty-tl-lane' },
    h(
      'div',
      { className: 'duty-tl-lane-h' },
      h('span', { className: 'duty-tl-lane-name', title: view.name }, view.name),
      view.kind
        ? h(
            'span',
            {
              className: `duty-tl-kind duty-tl-kind-${view.kind}`,
              title: view.kind === 'agent' ? `профиль: ${view.kindText}` : view.kind,
            },
            h(
              'svg',
              { className: 'duty-tl-kind-ico', viewBox: '0 0 16 16', 'aria-hidden': 'true' },
              h('path', { d: view.icon }),
            ),
            view.kindText,
          )
        : null,
      h('span', { className: 'duty-tl-lane-meta' }, view.meta),
    ),
    view.empty
      ? h('div', { className: 'duty-tl-empty' }, 'попыток не было')
      : h(
          'div',
          { className: 'duty-tl-rows' },
          ...view.rows.map((row) =>
            h(DutyAttemptRow, { key: row.key, view: row.view, callSel, onSelectCall, toolFilter }),
          ),
        ),
  )
}

const CSS = `
.duty-page{display:flex;flex-direction:column;height:100%;min-height:0;background:var(--ui-surface-background);color:var(--ui-text-primary);font-size:13px}
.duty-head{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--ui-stroke-tertiary);flex:0 0 auto}
.duty-head h1{font-size:14px;font-weight:600;margin:0}
.duty-muted{color:var(--ui-text-tertiary);font-size:11px}
.duty-num{font-variant-numeric:tabular-nums}
.duty-spacer{flex:1 1 auto}
.duty-icon-btn{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:5px;border:0;background:transparent;color:var(--ui-text-tertiary);cursor:pointer}
.duty-icon-btn:hover{background:color-mix(in srgb, var(--ui-text-primary) 8%, transparent);color:var(--ui-text-primary)}
.duty-banner{display:flex;align-items:center;gap:8px;margin:8px 16px 0;padding:6px 10px;border-radius:6px;color:var(--ui-red);border:1px solid color-mix(in srgb, var(--ui-red) 40%, transparent);background:color-mix(in srgb, var(--ui-red) 12%, transparent);font-size:12px}
.duty-banner span{flex:1 1 auto;word-break:break-word}
.duty-note{margin:6px 16px 0;padding:6px 10px;border-radius:6px;font-size:11px;color:var(--ui-yellow);border:1px solid color-mix(in srgb, var(--ui-yellow) 35%, transparent);background:color-mix(in srgb, var(--ui-yellow) 10%, transparent)}
.duty-filters{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 16px;border-bottom:1px solid var(--ui-stroke-tertiary);flex:0 0 auto}
.duty-select{height:26px;max-width:200px;border-radius:6px;border:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-elevated);color:inherit;font-size:12px;padding:0 6px}
.duty-chips{display:flex;flex-wrap:wrap;gap:4px}
.duty-chip{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 8px;border-radius:999px;border:1px solid var(--ui-stroke-tertiary);background:transparent;color:var(--ui-text-secondary);font-size:11px;cursor:pointer;white-space:nowrap;font-family:inherit}
.duty-chip:hover{background:color-mix(in srgb, var(--ui-text-primary) 8%, transparent)}
.duty-chip.on{border-color:currentColor;color:var(--ui-text-primary)}
.duty-chip .duty-num{opacity:.6}
.duty-body{display:flex;min-height:0;flex:1 1 auto}
.duty-list{flex:1 1 auto;min-width:0;overflow:auto;padding:8px 12px 24px}
.duty-detail{flex:0 0 400px;max-width:46%;border-left:1px solid var(--ui-stroke-tertiary);overflow:auto;padding:12px 14px}
.duty-group{margin:0 0 16px}
.duty-group-h{display:flex;align-items:center;gap:6px;margin:6px 2px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-tertiary)}
.duty-table{width:100%;table-layout:fixed;border-collapse:collapse}
.duty-table th{text-align:left;font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-tertiary);padding:4px 6px;border-bottom:1px solid var(--ui-stroke-tertiary);white-space:nowrap}
.duty-table td{padding:5px 6px;border-bottom:1px solid color-mix(in srgb, var(--ui-stroke-tertiary) 60%, transparent);vertical-align:middle;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.duty-row{cursor:pointer}
.duty-row:hover td{background:color-mix(in srgb, var(--ui-text-primary) 6%, transparent)}
.duty-row.sel td{background:color-mix(in srgb, var(--ui-accent) 12%, transparent)}
.duty-mono{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11px;color:var(--ui-text-secondary)}
.duty-badge{display:inline-flex;align-items:center;gap:4px;padding:1px 7px;border-radius:999px;font-size:11px;white-space:nowrap;border:1px solid color-mix(in srgb, currentColor 45%, transparent);background:color-mix(in srgb, currentColor 14%, transparent)}
.duty-badge i{display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor}
.duty-empty{display:flex;flex-direction:column;gap:8px;align-items:flex-start;padding:48px 16px;color:var(--ui-text-secondary)}
.duty-empty b{color:var(--ui-text-primary);font-size:13px}
.duty-empty ul{margin:0;padding-left:18px;font-size:12px;color:var(--ui-text-tertiary)}
.duty-skel{height:10px;border-radius:4px;background:linear-gradient(90deg,var(--ui-bg-quaternary) 25%,var(--ui-bg-quinary,var(--ui-bg-quaternary)) 37%,var(--ui-bg-quaternary) 63%);background-size:400% 100%;animation:duty-shimmer 1.4s ease-in-out infinite}
.duty-skel-row{display:flex;flex-direction:column;gap:6px;padding:8px 0}
@keyframes duty-shimmer{0%{background-position:100% 0}100%{background-position:0 0}}
.duty-panel-h{display:flex;align-items:flex-start;gap:8px;margin-bottom:6px}
.duty-panel-h .duty-mono{color:var(--ui-text-primary)}
.duty-panel-title{font-size:12px;font-weight:600;word-break:break-word}
.duty-evidence{margin:6px 0 12px;font-size:11px;color:var(--ui-text-secondary);background:var(--ui-bg-quaternary);border-radius:6px;padding:6px 8px;word-break:break-word}
.duty-att-h{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--ui-text-secondary)}
.duty-att-h .duty-num{color:var(--ui-text-primary)}
.duty-error{color:var(--ui-red);font-size:11px;white-space:pre-wrap;word-break:break-word;margin-top:4px}
.duty-summary{font-size:11px;color:var(--ui-text-tertiary);margin-top:4px;word-break:break-word}
/* ------------------------- столбец этапов и попыток (0.0.8)
   Одна панель на две бывшие: строки этапов (~26 px) и попытки внутри них
   (~24 px, отступ 12 px). Провал и живая попытка раскрыты без клика — это
   решение прошлого круга; остальные свёрнуты, иначе столбец снова простыня. */
.duty-deal-stages{display:flex;flex-direction:column;gap:2px}
.duty-stage-block{display:flex;flex-direction:column;min-width:0}
.duty-stage-row{display:flex;align-items:center;gap:6px;height:26px;padding:0 2px;font-size:12px;white-space:nowrap;overflow:hidden;cursor:pointer}
.duty-stage-block + .duty-stage-block .duty-stage-row{border-top:1px solid var(--ui-stroke-tertiary)}
.duty-stage-row:hover{background:color-mix(in srgb, var(--ui-text-primary) 6%, transparent)}
.duty-stage-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.duty-stage-row-id{color:var(--ui-text-primary);font-weight:600;overflow:hidden;text-overflow:ellipsis}
.duty-stage-row-kind{overflow:hidden;text-overflow:ellipsis}
.duty-stage-row-att{color:var(--ui-text-tertiary)}
.duty-stage-row-tog,.duty-att-row-tog{color:var(--ui-text-tertiary);font-size:10px}
.duty-att-item{display:flex;flex-direction:column;min-width:0}
.duty-att-row{display:flex;align-items:center;gap:6px;height:24px;margin-left:12px;padding:0 2px 0 0;font-size:12px;white-space:nowrap;overflow:hidden;cursor:pointer;color:var(--ui-text-secondary)}
.duty-att-row:hover{background:color-mix(in srgb, var(--ui-text-primary) 6%, transparent)}
.duty-att-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.duty-att-row-n{color:var(--ui-text-primary)}
.duty-att-row-tog{margin-left:auto}
.duty-att-ev{display:flex;align-items:baseline;gap:6px;margin-left:12px;padding:2px 2px 2px 0;font-size:11px;color:var(--ui-text-tertiary)}
.duty-att-ev-cmd{color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.duty-att-log{margin-left:12px;padding:2px 2px 2px 0;font-size:11px;color:var(--ui-text-tertiary)}
.duty-att-none{margin-left:12px;padding:2px 2px 2px 0;font-size:11px}
/* Ожидание человека — видимой строкой под этапом: дело может стоять на нём. */
.duty-stage-wait{margin-left:0;padding:2px 2px 4px 12px;font-size:11px;color:var(--ui-yellow);word-break:break-word}
.duty-stage-wait a{color:var(--ui-yellow);text-decoration:underline}
.duty-tabs{display:flex;gap:2px;margin-left:6px}
.duty-tab{height:22px;padding:0 9px;border-radius:999px;border:1px solid transparent;background:transparent;color:var(--ui-text-tertiary);font:inherit;font-size:11px;cursor:pointer;white-space:nowrap}
.duty-tab:hover{background:color-mix(in srgb, var(--ui-text-primary) 8%, transparent);color:var(--ui-text-primary)}
.duty-tab.on{color:var(--ui-accent);background:color-mix(in srgb, var(--ui-accent) 14%, transparent);border-color:color-mix(in srgb, var(--ui-accent) 40%, transparent)}
.duty-scroll{flex:1 1 auto;min-height:0;overflow:auto;padding:10px 16px 24px}
.duty-why-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
.duty-why-card{border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:10px 12px;min-width:0}
.duty-why-title{font-size:12px;font-weight:600;line-height:1.35;word-break:break-word}
.duty-why-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:5px 0 0}
.duty-why-foot{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--ui-text-tertiary)}
.duty-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-bottom:14px}
.duty-kpi{border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:10px 12px}
.duty-kpi-k{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--ui-text-tertiary)}
.duty-kpi-v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;margin:4px 0 2px}
.duty-kpi-track{height:6px;border-radius:3px;background:var(--ui-bg-quaternary);overflow:hidden;margin-top:8px}
.duty-kpi-fill{height:6px;border-radius:3px;background:var(--ui-green)}
.duty-tax{display:flex;flex-direction:column;gap:5px}
.duty-tax-row{display:grid;grid-template-columns:170px 1fr 56px;align-items:center;gap:8px;font-size:11px}
.duty-tax-name{font-family:var(--ui-font-mono, ui-monospace, monospace);color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.duty-tax-track{height:7px;border-radius:4px;background:var(--ui-bg-quaternary);overflow:hidden}
.duty-tax-fill{height:7px;border-radius:4px}
.duty-health-table td{white-space:nowrap;text-overflow:ellipsis}
.duty-finished-table td{white-space:nowrap;text-overflow:ellipsis}
.duty-deal-head{display:flex;flex-direction:column;gap:6px;border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:10px 12px;margin-bottom:12px}
.duty-deal-title{font-size:13px;font-weight:600;word-break:break-word}
.duty-deal-facts{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--ui-text-secondary)}
.duty-deal-facts b{color:var(--ui-text-primary);font-weight:600}
.duty-deal-body{display:flex;flex-direction:column;gap:12px;min-width:0}
/* Две колонки карточки дела: документы (растут) и тонкий столбец этапов — ровно
   320 px, не растёт: в нём строки этапов и попыток, а не абзацы. */
.duty-deal-cols{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:14px;align-items:start;min-width:0}
/* Узкое окно: колонки друг под другом, документы — первыми, этапы — следом. */
@media (max-width:1100px){.duty-deal-cols{grid-template-columns:1fr}}
.duty-deal-spec,.duty-deal-docs,.duty-deal-stages{min-width:0}
.duty-deal-scale{grid-column:1/-1;min-width:0}
.duty-spec{margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11px;line-height:1.5;color:var(--ui-text-secondary);max-height:60vh;overflow:auto;border:1px solid var(--ui-stroke-tertiary);border-radius:6px;padding:8px}
/* Спецификация дела — markdown, а не дамп: pre-блок переносил строки так, что
   YAML разъезжался, а заголовки и списки читались как текст. Шрифт возвращаем к
   обычному, моно — только для кода. */
.duty-md{white-space:normal;font-family:var(--ui-font-default, inherit);font-size:12.5px;line-height:1.55;padding:10px 12px}
.duty-md h1,.duty-md h2,.duty-md h3,.duty-md h4{margin:12px 0 6px;line-height:1.3;font-weight:600;color:var(--ui-text-primary)}
.duty-md h1{font-size:15px}
.duty-md h2{font-size:13.5px;border-bottom:1px solid var(--ui-stroke-tertiary);padding-bottom:4px}
.duty-md h3{font-size:13px}
.duty-md h4{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-secondary)}
.duty-md p{margin:6px 0}
.duty-md ul,.duty-md ol{margin:6px 0;padding-left:20px;list-style:revert}
.duty-md li{margin:2px 0}
.duty-md code{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11px;background:var(--ui-bg-quaternary);border:1px solid var(--ui-stroke-tertiary);border-radius:4px;padding:1px 5px}
.duty-md pre{background:var(--ui-bg-quaternary);border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:9px 11px;overflow:auto;margin:8px 0}
.duty-md pre code{background:none;border:none;padding:0;line-height:1.5}
.duty-md blockquote{margin:8px 0;padding:2px 0 2px 10px;border-left:2px solid var(--ui-stroke-tertiary);color:var(--ui-text-tertiary)}
.duty-md table{border-collapse:collapse;margin:8px 0;font-size:12px;display:block;overflow-x:auto}
.duty-md th,.duty-md td{border:1px solid var(--ui-stroke-tertiary);padding:5px 9px;text-align:left}
.duty-md th{background:var(--ui-bg-quaternary);font-weight:600;color:var(--ui-text-primary)}
.duty-md hr{border:none;border-top:1px solid var(--ui-stroke-tertiary);margin:12px 0}
.duty-md a{text-decoration:underline;text-underline-offset:2px}
.duty-scale-axis{position:relative;height:15px;margin:0 0 6px}
.duty-scale-tick{position:absolute;font-size:11px;color:var(--ui-text-tertiary);white-space:nowrap;font-variant-numeric:tabular-nums}
.duty-scale-tick.align-start{transform:translateX(0)}
.duty-scale-tick.align-center{transform:translateX(-50%)}
.duty-scale-tick.align-end{transform:translateX(-100%)}
/* ------------------------------------- таймлайн вызовов (0.0.8)
 * Свёрнутая попытка — 20 px, раскрытие по наведению/фокусу. Высота приходит
 * переменной --duty-tl-full со строки, переход делает CSS. */

.duty-scale-plot{position:relative}
.duty-scale-grid{position:absolute;top:-2px;bottom:0;left:0;right:0;pointer-events:none;z-index:0}
.duty-scale-grid-line{position:absolute;top:0;bottom:0;border-left:1px dashed color-mix(in srgb, var(--ui-text-tertiary) 34%, transparent)}
.duty-tl-lanes{position:relative;z-index:1;display:flex;flex-direction:column;gap:10px}
.duty-tl-lane{display:flex;flex-direction:column;gap:4px}
.duty-tl-lane + .duty-tl-lane{border-top:1px solid var(--ui-border-primary, rgba(127,127,127,.2));padding-top:10px}
.duty-tl-lane-h{display:flex;align-items:center;gap:8px;min-height:20px}
.duty-tl-lane-name{font-size:12.5px;font-weight:600;color:var(--ui-text-primary);letter-spacing:.01em}
.duty-tl-lane-meta{margin-left:auto;font-size:11px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums;white-space:nowrap}
.duty-tl-kind{display:inline-flex;align-items:center;gap:5px;height:19px;padding:0 7px;border-radius:5px;border:1px solid var(--ui-border-primary, rgba(127,127,127,.3));background:var(--ui-bg-quaternary);font-size:10.5px;font-weight:600;color:var(--ui-text-secondary)}
.duty-tl-kind-ico{width:12px;height:12px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.duty-tl-kind-agent{color:var(--ui-text-primary);border-color:var(--ui-blue);background:color-mix(in srgb, var(--ui-blue) 12%, transparent)}
.duty-tl-kind-agent .duty-tl-kind-ico{stroke:var(--ui-blue)}
.duty-tl-kind-gate{color:var(--ui-yellow);border-color:var(--ui-yellow);background:color-mix(in srgb, var(--ui-yellow) 12%, transparent)}
.duty-tl-rows{display:flex;flex-direction:column;gap:4px}
.duty-tl-row{position:relative;display:grid;grid-template-columns:var(--duty-tl-gutter, 186px) 1fr;gap:var(--duty-tl-gap, 8px);align-items:stretch;border-radius:6px;height:var(--duty-tl-collapsed, 20px);transition:height .12s ease}
.duty-tl-row:hover,.duty-tl-row:focus-visible{height:var(--duty-tl-full, 20px)}
/* Глубокие уровни вызовов видны только в раскрытой строке: visibility, а не display —
 * чипы позиционированы абсолютно, разметка не прыгает, подсказки не обрезаются. */
.duty-tl-call[data-level]:not([data-level="0"]){visibility:hidden}
.duty-tl-row:hover .duty-tl-call[data-level],.duty-tl-row:focus-visible .duty-tl-call[data-level]{visibility:visible}
.duty-tl-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
/* Колонки подписи фиксированы: иначе статус разной ширины двигал время в каждой строке. */
.duty-tl-lbl{display:grid;grid-template-columns:26px 1fr auto;align-self:center;align-items:center;gap:6px;line-height:20px;white-space:nowrap;font-variant-numeric:tabular-nums}
.duty-tl-lbl>*{overflow:hidden;text-overflow:ellipsis}
.duty-tl-n{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:10px;color:var(--ui-text-tertiary);text-align:right}
.duty-tl-st{font-size:10.5px;font-weight:700;color:var(--ui-text-secondary)}
.duty-tl-st.t-green{color:var(--ui-green)}
.duty-tl-st.t-red{color:var(--ui-red)}
.duty-tl-st.t-yellow{color:var(--ui-yellow)}
.duty-tl-st.t-blue{color:var(--ui-blue)}
.duty-tl-ms{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:10px;color:var(--ui-text-tertiary);text-align:right}
/* Надбавка «сколько не показано»: обрезанное нельзя показывать полным. */
.duty-tl-more{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:9.5px;font-weight:700;color:var(--ui-yellow);padding:0 3px;border-radius:3px;background:color-mix(in srgb, var(--ui-yellow) 16%, transparent)}
/* Вызов без результата: полый чип — длительности нет, а не ноль. */
.duty-tl-call.duty-tl-unfinished,.duty-tl-tick.duty-tl-unfinished{background:transparent !important;box-shadow:inset 0 0 0 1.5px currentColor !important;opacity:.9}
.duty-tl-track{position:relative;height:100%}
.duty-tl-track{position:relative;min-height:20px}
/* Фон попытки: длительность видна полосой, а не только по чипам вызовов. */
.duty-tl-band{position:absolute;top:1px;bottom:1px;border-radius:4px;background:color-mix(in srgb, var(--ui-text-tertiary) 13%, transparent);border:1px solid color-mix(in srgb, var(--ui-text-tertiary) 22%, transparent)}
/* Тон исхода: провал читается полосой на дорожке, а не только словом слева. */
.duty-tl-band.t-red{background:color-mix(in srgb, var(--ui-red) 15%, transparent);border-color:color-mix(in srgb, var(--ui-red) 45%, transparent)}
.duty-tl-band.t-yellow{background:color-mix(in srgb, var(--ui-yellow) 13%, transparent);border-color:color-mix(in srgb, var(--ui-yellow) 38%, transparent)}
.duty-tl-band.t-blue{background:color-mix(in srgb, var(--ui-blue) 12%, transparent);border-color:color-mix(in srgb, var(--ui-blue) 34%, transparent)}
/* Провалившаяся попытка: красная кромка в подписи и номер красным. */
.duty-tl-row.is-failed .duty-tl-lbl{border-left:2px solid var(--ui-red);padding-left:6px;margin-left:-8px}
.duty-tl-row.is-failed .duty-tl-n{color:var(--ui-red)}
/* Пауза «вопрос → ответ»: время, которое дело стояло на человеке. */
.duty-tl-wait{position:absolute;top:1px;bottom:1px;border-radius:3px;background:color-mix(in srgb, var(--ui-yellow) 26%, transparent);border-left:1.5px solid var(--ui-yellow);border-right:1.5px solid var(--ui-yellow)}
/* Маркеры событий — внутри строки той попытки, где событие произошло. */
/* Маркеры журнала — вертикальные палки на всю высоту строки: точка на нижней кромке
 * убегала при разворачивании, а палка стоит на своём времени всегда. */
.duty-tl-marks{position:absolute;inset:0;pointer-events:none}
.duty-tl-mark{position:absolute;top:-3px;bottom:-3px;width:3px;transform:translateX(-50%);pointer-events:auto;cursor:pointer}
/* Палка тонкая: попадание мышью расширяется невидимым «полем» вокруг. */
.duty-tl-mark::before{content:'';position:absolute;left:-4px;right:-4px;top:0;bottom:0}
.duty-tl-mark i{display:block;width:100%;height:100%;border-radius:2px;background:currentColor;opacity:.9}
.duty-tl-mark.t-green{color:var(--ui-green)}
.duty-tl-mark.t-red{color:var(--ui-red)}
.duty-tl-mark.t-yellow{color:var(--ui-yellow)}
.duty-tl-mark.t-blue{color:var(--ui-blue)}
.duty-tl-mark.t-gray{color:var(--ui-text-tertiary)}
.duty-tl-call{position:absolute;height:10px;border-radius:2px;cursor:pointer;box-shadow:inset 0 0 0 1px rgba(0,0,0,.22)}
.duty-tl-row:hover .duty-tl-call{box-shadow:inset 0 0 0 1px rgba(0,0,0,.45)}
/* Подсказка — общий слой для чипа, полосы попытки, подписи и маркера. */
.duty-tl-call::after,.duty-tl-band::after,.duty-tl-lbl::after,.duty-tl-mark::after,.duty-tl-wait::after{content:attr(data-tip);position:absolute;left:50%;bottom:calc(100% + 7px);transform:translateX(-50%);padding:3px 7px;border-radius:5px;white-space:normal;max-width:240px;width:max-content;text-align:left;background:var(--ui-surface-raised, var(--ui-surface-background));color:var(--ui-text-primary);border:1px solid var(--ui-stroke-tertiary);box-shadow:0 8px 20px rgba(0,0,0,.38);font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:10.5px;line-height:1.5;opacity:0;pointer-events:none;transition:opacity .09s ease;z-index:30}
.duty-tl-call:hover::after,.duty-tl-band:hover::after,.duty-tl-lbl:hover::after,.duty-tl-mark:hover::after,.duty-tl-wait:hover::after{opacity:1}
.duty-tl-call.duty-tl-sel{box-shadow:inset 0 0 0 1.5px var(--ui-accent),0 0 0 2px color-mix(in srgb, var(--ui-accent) 35%, transparent)}
.duty-tl-insp{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:26px;margin-top:10px;padding:4px 9px;border-radius:6px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-surface-raised, var(--ui-surface-background));font-size:11.5px}
.duty-tl-insp.empty{color:var(--ui-text-tertiary);border-style:dashed;background:transparent}
.duty-tl-insp-dot{width:8px;height:8px;border-radius:2px;background:var(--ui-text-tertiary)}
.duty-kpi{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
.duty-kpi-cell{display:grid;grid-auto-flow:column;align-items:baseline;gap:6px;padding:5px 10px;border-radius:7px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-surface-raised, var(--ui-surface-background))}
.duty-kpi-v{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums}
.duty-kpi-l{font-size:10.5px;color:var(--ui-text-tertiary)}
.duty-kpi-n{font-size:10px;color:var(--ui-text-tertiary);opacity:.8}
/* Ось и сетка начинаются там же, где дорожки: колонка подписей — одна ширина. */
.duty-scale-head{display:grid;grid-template-columns:var(--duty-tl-gutter, 186px) 1fr;gap:var(--duty-tl-gap, 8px);align-items:end;margin-bottom:6px;--duty-tl-gutter:186px;--duty-tl-gap:8px}
.duty-scale-keys{display:flex;flex-direction:column;gap:1px;font-size:10px;color:var(--ui-text-tertiary);line-height:1.45}
.duty-scale-keys-h{font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;opacity:.7}
.duty-scale-key{display:grid;grid-template-columns:7px 1fr auto;align-items:center;gap:5px;white-space:nowrap;cursor:pointer;border-radius:4px;padding:1px 3px;margin:0 -3px}
.duty-scale-key:hover{background:color-mix(in srgb, var(--ui-text-tertiary) 12%, transparent)}
.duty-scale-key:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
/* Выбранное семейство: легенда говорит, что именно подсвечено на дорожках. */
.duty-scale-key.on{background:color-mix(in srgb, var(--ui-accent) 16%, transparent);color:var(--ui-text-primary)}
.duty-scale-key.on .duty-scale-key-n{opacity:1;font-weight:600}
/* Чужие семейства гаснут, а не исчезают: картина дела остаётся целой. */
.duty-tl-call.duty-tl-dim{opacity:.18;filter:saturate(.35);pointer-events:none}
.duty-tl-row:hover .duty-tl-call.duty-tl-dim{opacity:.3}
.duty-scale-key-l{overflow:hidden;text-overflow:ellipsis}
.duty-scale-key-n{font-family:var(--ui-font-mono, ui-monospace, monospace);font-variant-numeric:tabular-nums;opacity:.8}
.duty-scale-key-dot{width:7px;height:7px;border-radius:2px;background:var(--ui-text-tertiary)}
.duty-scale-key-dot.duty-t-read{background:var(--ui-blue)}
.duty-scale-key-dot.duty-t-search{background:var(--ui-purple)}
.duty-scale-key-dot.duty-t-write{background:var(--ui-green)}
.duty-scale-key-dot.duty-t-run{background:var(--ui-yellow)}
.duty-scale-key-dot.duty-t-net{background:var(--ui-red)}
/* Граф — полосой: мелкие узлы, а не главный объект экрана. */
.duty-spec-strip{display:flex;align-items:center;gap:8px;margin-top:4px}
.duty-spec-node.has-fail rect{stroke:var(--ui-red);stroke-width:1.4;stroke-dasharray:3 2}
.duty-spec-node.has-fail .duty-spec-id{fill:var(--ui-red)}
.duty-deal-head-top{align-items:flex-start}
.duty-deal-head-side{display:flex;align-items:center;gap:8px;flex:none}
.duty-scale-wrap{display:grid;grid-template-columns:minmax(0, 1fr) auto;gap:14px;align-items:start}
.duty-scale-main{min-width:0}
.duty-scale-side{display:flex;flex-direction:column;gap:1px;min-width:104px;padding-left:10px;border-left:1px solid var(--ui-stroke-tertiary);font-size:10px;color:var(--ui-text-tertiary);line-height:1.45}
.duty-spec-strip-name{font-size:10.5px;color:var(--ui-text-tertiary);text-align:right;white-space:nowrap}
.duty-spec-graph{display:block;width:auto;height:auto;max-width:100%;overflow:visible;fill:none;stroke:none}
.duty-spec-edge{fill:none;stroke:var(--ui-stroke-tertiary);stroke-width:1}
.duty-spec-node rect{fill:var(--ui-surface-raised, var(--ui-surface-background));stroke:var(--ui-stroke-tertiary);stroke-width:1}
.duty-spec-node .duty-spec-id{fill:var(--ui-text-secondary);font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:9px}
.duty-spec-node.t-green rect{stroke:var(--ui-green)}
.duty-spec-node.t-red rect{stroke:var(--ui-red)}
.duty-spec-node.t-yellow rect{stroke:var(--ui-yellow)}
.duty-spec-node.t-blue rect{stroke:var(--ui-accent)}
@media (prefers-reduced-motion: reduce){.duty-tl-row,.duty-tl-mini,.duty-tl-stack,.duty-tl-call{transition:none}}

.duty-scale-lanes{display:flex;flex-direction:column;gap:5px}
.duty-scale-lane{display:grid;grid-template-columns:92px 1fr;gap:8px;align-items:center}
.duty-scale-lane-k{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11.5px;color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Дорожка этапа без попыток — пустая: точка-многоточие отличает её от этапа, где попытка просто шла вне окна. */
.duty-scale-lane-none{color:var(--ui-text-tertiary);font-style:normal}
.duty-scale-track{position:relative;height:20px;border-radius:5px;background:var(--ui-bg-quaternary);overflow:hidden}
.duty-scale-bar{position:absolute;top:3px;bottom:3px;display:flex;align-items:center;overflow:hidden;border-radius:4px;border:1px solid currentColor;background:color-mix(in srgb, currentColor 22%, transparent);color:var(--ui-text-tertiary)}
.duty-scale-bar:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.duty-scale-bar.open{background:transparent;border-style:dashed}
.duty-scale-bar-k{font-size:10.5px;color:var(--ui-text-primary);padding:0 5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.duty-scale-markers{position:relative;height:42px;margin-top:10px;border-top:1px solid var(--ui-stroke-tertiary)}
.duty-scale-marker{position:absolute;display:flex;flex-direction:column;align-items:center;transform:translateX(-50%)}
.duty-scale-dot{display:grid;place-items:center;width:14px;height:14px;border-radius:50%;border:1px solid currentColor;background:var(--ui-surface-background);font-size:9.5px;line-height:1}
.duty-scale-marker.tick .duty-scale-dot{width:1px;height:12px;border:0;border-radius:0;background:currentColor}
.duty-scale-marker:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.duty-scale-wait{position:absolute;bottom:0;height:0;border-top:2px dashed currentColor;transform:translateY(-4px);color:var(--ui-yellow)}
.duty-scale-wait-k{position:absolute;left:4px;top:-14px;font-size:10.5px;white-space:nowrap}
.duty-scale-legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;font-size:11.5px}
`

/* ---------------- данные ---------------- */

/** Текст ошибки из чего угодно, что бросил ctx.rest. */
function errText(error) {
  if (!error) return 'неизвестная ошибка'
  const detail = error.detail ?? error.payload?.detail ?? error.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail) return JSON.stringify(detail)
  return String(error.message || error)
}

/** Экран «сейчас»: первый запрос сразу, дальше раз в 10 с; старые данные не затираются. */
function useNow() {
  const [snap, setSnap] = useState({ data: null, error: null, loading: true, updatedAt: 0 })
  const inflight = useRef(false)
  const alive = useRef(true)

  const load = useCallback(async () => {
    if (inflight.current) return
    inflight.current = true
    try {
      const data = await rest('/now', { timeoutMs: REQUEST_TIMEOUT_MS })
      if (!alive.current) return
      setSnap({ data, error: null, loading: false, updatedAt: Date.now() })
    } catch (error) {
      if (!alive.current) return
      setSnap((prev) => ({ ...prev, error: errText(error), loading: false }))
    } finally {
      inflight.current = false
    }
  }, [])

  useEffect(() => {
    alive.current = true
    void load()
    const timer = setInterval(() => void load(), REFRESH_MS)
    return () => {
      alive.current = false
      clearInterval(timer)
    }
  }, [load])

  return { ...snap, refresh: load }
}

/** Таймлайн попыток выбранного дела. Смена выбора отменяет старый ответ (по ключу). */
function useAttempts(dealId) {
  const [state, setState] = useState({ key: '', data: null, error: null, loading: false })
  const key = dealId ? String(dealId) : ''

  const load = useCallback(async () => {
    if (!dealId) return
    const mine = String(dealId)
    setState((prev) => ({ key: mine, data: prev.key === mine ? prev.data : null, error: null, loading: true }))
    try {
      const data = await rest(
        `/duty/${encodeURIComponent(dealId)}/attempts`,
        { timeoutMs: REQUEST_TIMEOUT_MS },
      )
      setState((prev) => (prev.key === mine ? { key: mine, data, error: null, loading: false } : prev))
    } catch (error) {
      setState((prev) => (prev.key === mine ? { ...prev, error: errText(error), loading: false } : prev))
    }
  }, [dealId])

  useEffect(() => {
    void load()
  }, [load])

  return { ...state, reload: load }
}

/* ---------------- данные: «дело» и «здоровье» ---------------- */

/** Экран «Здоровье»: агрегат по ранам. Запрашивается только когда экран открыт. */
function useStats(enabled) {
  const [snap, setSnap] = useState({ data: null, error: null, loading: false })
  const alive = useRef(true)

  const load = useCallback(async () => {
    setSnap((prev) => ({ ...prev, loading: true }))
    try {
      const data = await rest('/stats', { timeoutMs: REQUEST_TIMEOUT_MS })
      if (alive.current) setSnap({ data, error: null, loading: false })
    } catch (error) {
      // Прошлый снимок не затираем: экран не мигает пустотой при обрыве.
      if (alive.current) setSnap((prev) => ({ data: prev.data, error: errText(error), loading: false }))
    }
  }, [])

  useEffect(() => {
    alive.current = true
    if (!enabled) return () => { alive.current = false }
    void load()
    const timer = setInterval(() => void load(), STATS_REFRESH_MS)
    return () => {
      alive.current = false
      clearInterval(timer)
    }
  }, [enabled, load])

  return { ...snap, reload: load }
}

/* ---------------- данные: «завершённые» (0.0.7) ---------------- */

/**
 * Экран «Завершённые»: `GET /deals` — список закрытых дел.
 *
 * Запрашивается только когда экран открыт: это чтение всей истории закрытых
 * дел, и держать его в памяти «Сейчас» незачем. Прошлый снимок не затирается
 * ошибкой — экран не мигает пустотой при обрыве.
 */
function useFinishedFeeds(enabled) {
  const [snap, setSnap] = useState({ data: null, error: null, loading: false })
  const alive = useRef(true)

  const load = useCallback(async () => {
    setSnap((prev) => ({ ...prev, loading: true }))
    try {
      const data = await rest('/deals', { timeoutMs: REQUEST_TIMEOUT_MS })
      if (alive.current) setSnap({ data, error: null, loading: false })
    } catch (error) {
      if (alive.current) setSnap((prev) => ({ data: prev.data, error: errText(error), loading: false }))
    }
  }, [])

  useEffect(() => {
    alive.current = true
    if (!enabled) return () => { alive.current = false }
    void load()
    const timer = setInterval(() => void load(), STATS_REFRESH_MS)
    return () => {
      alive.current = false
      clearInterval(timer)
    }
  }, [enabled, load])

  return { ...snap, reload: load }
}

/** Экран «Дело»: спецификация + тот же факт-набор, что в `/now`. */
function useDeal(dealId) {
  const [state, setState] = useState({ key: '', data: null, error: null, loading: false })
  const key = dealId ? String(dealId) : ''

  const load = useCallback(async () => {
    if (!key) return
    setState((prev) => ({ key, data: prev.key === key ? prev.data : null, error: null, loading: true }))
    try {
      const data = await rest(`/deal/${encodeURIComponent(dealId)}`, {
        timeoutMs: REQUEST_TIMEOUT_MS,
      })
      setState((prev) => (prev.key === key ? { key, data, error: null, loading: false } : prev))
    } catch (error) {
      setState((prev) => (prev.key === key ? { ...prev, error: errText(error), loading: false } : prev))
    }
  }, [dealId, key])

  useEffect(() => {
    void load()
    const timer = setInterval(() => void load(), REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  return { ...state, reload: load }
}

/* ---------------- отрисовка ---------------- */

function StateBadge({ label, tone, title }) {
  return h(
    'span',
    { className: 'duty-badge', style: { color: TONE_COLOR[tone] || TONE_COLOR.gray }, title },
    h('i', null),
    label,
  )
}

function DealRow({ deal, selected, onPick }) {
  const attemptTitle = deal.attemptAge === null
    ? `попытки нет (${deal.attemptNote})`
    : `попытка ${deal.attemptAgeText} назад, ${deal.attemptNote}`
  const leaseTitle = deal.leaseExpiresIn === null
    ? `лизинг: ${deal.leaseState}`
    : `лизинг: ${deal.leaseState}, ${deal.leaseExpiresIn > 0 ? 'ещё' : 'истёк'} ${fmtAge(Math.abs(deal.leaseExpiresIn))}`
  return h(
    'tr',
    {
      className: selected ? 'duty-row sel' : 'duty-row',
      onClick: () => onPick(deal),
      title: [deal.repoLabel, deal.evidence].filter(Boolean).join(' · '),
    },
    h('td', { className: 'duty-mono', title: deal.id }, deal.id),
    h('td', { title: deal.titleFull }, deal.title),
    h('td', { title: deal.assignee }, deal.assignee),
    h('td', { className: 'duty-muted duty-mono', title: deal.stageText || 'этап не начат' }, deal.stageText || '—'),
    h('td', { className: 'duty-num', title: attemptTitle }, deal.attemptAgeText),
    h('td', { title: leaseTitle }, deal.leaseText),
    h('td', { className: 'duty-num', title: `${deal.lastActionKind}: ${deal.lastActionSummary || '—'}` }, deal.lastActionAgeText),
    h('td', null, h(StateBadge, { label: deal.stateLabel, tone: deal.tone, title: deal.evidence })),
  )
}

/**
 * Группа списка — репозиторий (0.0.6): имя, число дел и состояния группы одной
 * строкой. Столбца «репозиторий» в строках нет — он повторял бы заголовок.
 */
function DealGroup({ group, selectedKey, onPick }) {
  return h(
    'section',
    { className: 'duty-group' },
    h(
      'div',
      { className: 'duty-group-h' },
      h('span', { className: group.repo ? 'duty-mono' : 'duty-mono duty-muted', title: group.repo || 'репозиторий не назван' }, group.label),
      h('span', { className: 'duty-num' }, `${group.count}`),
      h('span', { className: 'duty-spacer' }),
      h('span', { className: 'duty-muted' }, groupStatesText(group)),
    ),
    h(
      'table',
      { className: 'duty-table' },
      h(
        'colgroup',
        null,
        h('col', { style: { width: '104px' } }),
        h('col', null),
        h('col', { style: { width: '132px' } }),
        h('col', { style: { width: '104px' } }),
        h('col', { style: { width: '68px' } }),
        h('col', { style: { width: '64px' } }),
        h('col', { style: { width: '72px' } }),
        h('col', { style: { width: '124px' } }),
      ),
      h(
        'thead',
        null,
        h(
          'tr',
          null,
          h('th', null, 'дело'),
          h('th', null, 'заголовок'),
          h('th', null, 'исполнитель'),
          h('th', null, 'этап'),
          h('th', null, 'попытка'),
          h('th', null, 'лизинг'),
          h('th', null, 'действие'),
          h('th', null, 'состояние'),
        ),
      ),
      h(
        'tbody',
        null,
        ...group.deals.map((deal) =>
          h(DealRow, {
            key: deal.id,
            deal,
            selected: selectedKey === deal.id,
            onPick,
          }),
        ),
      ),
    ),
  )
}

/* ---------------- столбец этапов и попыток дела (0.0.8) ---------------- */

/**
 * Попытки этапа для столбца.
 *
 * Факты процесса (`live`/`dead`, лизинг, код выхода) знает только лента `/deal`,
 * журнал попытки — только ручка `/duty/{id}/attempts`. Своих номеров и исходов
 * экран не считает: попытка ленты ищется в ответе ручки по `runId` (оба зовут
 * одну попытку одним именем), и ей подмешивается один факт — `events`.
 */
function stageColumnBars(stage, timeline) {
  const rows = Array.isArray(stage.attempts) ? stage.attempts : []
  const extra = timeline && Array.isArray(timeline.attempts) ? timeline.attempts : []
  if (!rows.length) return extra.filter((attempt) => attempt.stage === stage.id)
  return rows.map((row) => {
    if (row.runId === null || row.runId === undefined) return row
    const found = extra.find((attempt) => String(attempt.runId) === String(row.runId))
    return found ? { ...row, events: found.events } : row
  })
}

/**
 * Строка попытки: `#n`, длительность, исход словами, глиф раскрытия.
 *
 * Раскрытая показывает доказательства, потом `summary` и `error`, потом журнал —
 * сводкой по видам (`5 прогрессов · 1 старт · 1 конец`), а не списком событий:
 * у одной попытки их бывает триста, и в столбце 320 px это лог, а не строка.
 */
function CompactAttemptRow({ stage, attempt, open, onToggle }) {
  const view = compactAttemptView(stage, attempt)
  const children = [
    h(
      'div',
      {
        key: 'row',
        className: open ? 'duty-att-row open' : 'duty-att-row',
        title: view.outcomeText,
        'aria-expanded': open ? 'true' : 'false',
        onClick: onToggle,
      },
      h('span', { className: 'duty-mono duty-att-row-n' }, view.label),
      h('span', { className: 'duty-num duty-att-row-dur' }, view.durationText),
      h(StateBadge, { label: view.outcomeText, tone: view.tone, title: view.outcomeText }),
      h('span', { className: 'duty-spacer' }),
      h('span', { className: 'duty-att-row-tog' }, open ? '▾' : '▸'),
    ),
  ]
  if (!open) return h('div', { className: 'duty-att-item' }, ...children)
  view.evidenceRows.forEach((row, i) => {
    children.push(
      h(
        'div',
        { key: `ev-${i}`, className: 'duty-att-ev' },
        // Полная команда — в подсказке: обрезка сделана в шейпере, а не CSS-эллипсисом.
        h(StateBadge, { label: `exit ${row.exitText}`, tone: row.tone, title: row.cmdTitle }),
        h('span', { className: 'duty-mono duty-att-ev-cmd', title: row.cmdTitle }, row.cmdShort),
        h('span', { className: 'duty-muted' }, row.clock),
      ),
    )
  })
  if (view.summary) children.push(h('div', { key: 'summary', className: 'duty-summary' }, view.summary))
  if (view.error) children.push(h('div', { key: 'error', className: 'duty-error' }, view.error))
  if (view.eventsText) children.push(h('div', { key: 'log', className: 'duty-att-log duty-muted' }, view.eventsText))
  return h('div', { className: 'duty-att-item' }, ...children)
}

/**
 * Столбец этапов и попыток в карточке дела.
 *
 * Одна панель вместо двух (широкой ленты этапов и отдельной панели попыток):
 * шапка попытки повторялась в обеих, и страница росла на три с половиной экрана.
 * Раскрытие строк — состояние экрана (`useState`): шейпер отдаёт только умолчание
 * (`openByDefault` — провал и живая попытка раскрыты без клика).
 * Пустой граф печатает причину (`note` бэкенда: нет спеки, спека не читается).
 */
function CompactStages({ feed, timeline }) {
  const [openStages, setOpenStages] = useState({})
  const [openAtts, setOpenAtts] = useState({})
  const head = h('div', { className: 'duty-group-h' }, h('span', null, feed.empty ? 'этапы' : `этапы · ${feed.count}`))
  if (feed.empty) {
    return h('div', { className: 'duty-deal-stages' }, head, h('div', { className: 'duty-muted' }, feed.note || 'этапов нет'))
  }
  const blocks = feed.stages.map((stage) => {
    const view = compactStageView(stage)
    const open = openStages[view.id] === undefined ? view.openByDefault : openStages[view.id]
    const bars = stageColumnBars(stage, timeline)
    const children = [
      h(
        'div',
        {
          key: 'row',
          className: open ? 'duty-stage-row open' : 'duty-stage-row',
          // Служебное (`needs`) — в подсказке строки: отдельной строкой оно
          // съедало высоту, которую занимают попытки.
          title: view.needsText || view.toggleTitle,
          'aria-expanded': open ? 'true' : 'false',
          onClick: () => setOpenStages((prev) => ({ ...prev, [view.id]: !open })),
        },
        h('span', { className: 'duty-mono duty-stage-row-id' }, view.id),
        h('span', { className: 'duty-muted duty-stage-row-kind' }, view.kindText),
        h('span', { className: 'duty-num duty-stage-row-att' }, view.attemptsText),
        h('span', { className: 'duty-spacer' }),
        h(StateBadge, { label: view.stateLabel, tone: view.tone, title: view.stateLabel }),
        h('span', { className: 'duty-stage-row-tog' }, open ? '▾' : '▸'),
      ),
    ]
    // Ожидание человека остаётся видимой строкой: дело может стоять на нём.
    if (view.waitText) {
      children.push(
        h(
          'div',
          { key: 'wait', className: 'duty-stage-wait' },
          view.waitHref ? h('a', { href: view.waitHref }, view.waitText) : view.waitText,
        ),
      )
    }
    if (!bars.length && open) children.push(h('div', { key: 'none', className: 'duty-muted duty-att-none' }, 'попыток нет'))
    if (open) {
      bars.forEach((attempt, index) => {
        const key = `${stage.id}#${index}`
        const attOpen = openAtts[key] === undefined ? compactAttemptView(stage, attempt).openByDefault : openAtts[key]
        children.push(
          h(CompactAttemptRow, {
            key: `att-${key}`,
            stage,
            attempt,
            open: attOpen,
            onToggle: () => setOpenAtts((prev) => ({ ...prev, [key]: !attOpen })),
          }),
        )
      })
    }
    return h('div', { className: 'duty-stage-block', key: `st-${stage.id}` }, ...children)
  })
  return h(
    'div',
    { className: 'duty-deal-stages' },
    head,
    // Путь к спеке — именем файла, полный путь в подсказке.
    feed.spec ? h('div', { className: 'duty-mono duty-muted', title: feed.spec }, fileLabel(feed.spec)) : null,
    ...blocks,
  )
}


// Неполнота ответа /now одной строкой — общая для жёлтой полосы над списком и
// для пустого состояния, иначе «досок 19/19» врёт про полноту среза. Собирает её
// модель (`noticeLines`), потому что правило «устаревший снимок замечаний не
// имеет» проверяется тестом, а не глазами при ревью рендера.

/* ---------------- шкала времени дела (E2) ---------------- */

// Глиф маркера — только значок: слово и время приходят из шейпера (`label`,
// `title`), чтобы подпись не разъезжалась между десктопом и вебом.
const MARKER_GLYPH = {
  question: '?',
  answer: '+',
  verdict: 'v',
  budget: '!',
  published: '↑',
  closed: '■',
  started: '',
  ended: '',
}

/** Строка фактов шапки дела: те же числа, что в вебе (`shapeDealTime`). */
function DealFactSpans({ scale }) {
  const facts = scale.facts
  return h(
    'div',
    { className: 'duty-deal-facts' },
    // Попытки и длительность стоят в KPI: второй раз они читаются как другой факт.
    h('span', { title: 'вопросы человеку по таймлайну' }, 'вопросы: ', h('b', null, facts.questions.text)),
    facts.budget
      ? h(
          'span',
          { title: facts.budget.exhausted ? `потолок исчерпан: ${facts.budget.exhausted}` : 'потолок дела целиком' },
          'бюджет: ',
          h('b', null, facts.budget.text || '—'),
        )
      : null,
    facts.published
      ? h(
          'span',
          { title: facts.published.mrUrl || facts.published.ref || 'факт журнала' },
          'публикация: ',
          h('b', { className: 'duty-mono' }, facts.published.text || '—'),
        )
      : null,
    facts.closed ? h('span', { title: facts.closed.cause || '' }, 'закрытие: ', h('b', null, facts.closed.text)) : null,
  )
}

/** Инспектор вызова: инструмент, начало, длительность, место в деле. */
function CallInspector({ call }) {
  if (!call) {
    return h('div', { className: 'duty-tl-insp empty' }, 'выбери вызов на дорожке — покажу инструмент, начало и длительность')
  }
  return h(
    'div',
    { className: 'duty-tl-insp' },
    h('span', { className: 'duty-tl-insp-dot', style: { background: TONE_COLOR[call.tone] || TONE_COLOR.gray } }),
    h('b', null, call.name),
    h('span', { className: 'duty-mono' }, call.clock),
    h('span', null, call.msText),
    h('span', { className: 'duty-muted' }, `${call.stage} · попытка ${call.attempt}${call.seq === null ? '' : ` · вызов ${call.seq}`}`),
  )
}

/** KPI-строка дела: пять чисел шапки, все — из шейпера (`scale.kpi`). */
function KpiStrip({ scale }) {
  if (!scale.kpi || !scale.kpi.length) return null
  return h(
    'div',
    { className: 'duty-kpi' },
    ...scale.kpi.map((item) =>
      h(
        'div',
        { className: 'duty-kpi-cell', key: `kpi-${item.key}`, title: item.title || '' },
        h('span', { className: 'duty-kpi-v' }, item.value),
        h('span', { className: 'duty-kpi-l' }, item.label),
        item.note ? h('span', { className: 'duty-kpi-n' }, item.note) : null,
      ),
    ),
  )
}

/**
 * Граф спеки компактной полосой: мелкие узлы и рёбра `needs`.
 *
 * Схема зависимостей — не главный объект экрана: крупные прямоугольники
 * занимали пол-экрана и ничего не сообщали. Координаты считает шейпер.
 */
function SpecStrip({ spec }) {
  if (!spec) return null
  const pos = new Map(spec.nodes.map((node) => [node.id, node]))
  const edges = spec.edges
    .map((edge) => {
      const from = pos.get(edge.from)
      const to = pos.get(edge.to)
      if (!from || !to) return null
      return h('path', {
        key: `e-${edge.from}-${edge.to}`,
        className: 'duty-spec-edge',
        d: `M${from.x + spec.nodeW},${from.y + spec.nodeH / 2} L${to.x},${to.y + spec.nodeH / 2}`,
      })
    })
    .filter(Boolean)
  return h(
    'div',
    { className: 'duty-spec-strip' },
    h('span', { className: 'duty-spec-strip-name' }, 'граф спеки'),
    h(
      'svg',
      {
        className: 'duty-spec-graph',
        viewBox: `0 0 ${spec.width} ${spec.height}`,
        width: spec.width,
        height: spec.height,
        role: 'img',
        'aria-label': `граф этапов: ${spec.nodes.map((node) => node.id).join(' → ')}`,
      },
      ...edges,
      ...spec.nodes.map((node) =>
        h(
          'g',
          { key: `n-${node.id}`, className: `duty-spec-node t-${node.tone}${node.fails ? ' has-fail' : ''}` },
          h(
            'title',
            null,
            `${node.id} · ${node.label}${node.failsText ? ` · ${node.failsText}` : ''}${node.needsText ? ` · ${node.needsText}` : ''}`,
          ),
          h('rect', { x: node.x, y: node.y, width: spec.nodeW, height: spec.nodeH, rx: 4 }),
          h('text', {
            x: node.x + spec.nodeW / 2,
            y: node.y + spec.nodeH / 2 + 3,
            className: 'duty-spec-id',
            textAnchor: 'middle',
          }, node.id),
        ),
      ),
    ),
  )
}

/**
 * Выбранный вызов по ключу `stage:attempt:seq`.
 *
 * Ищется тем же видом, которым рисуются чипы (`attemptView`): второго описания
 * вызова у инспектора нет.
 */
function callOf(scale, callId) {
  if (!callId) return null
  for (const bar of scale.bars) {
    for (const chip of attemptView(bar, scale).chips) {
      if (chip.id === callId) return chip
    }
  }
  return null
}

/** Панель шкалы времени: подписанная ось, дорожка на этап, полосы и маркеры. */
function ScalePanel({ scale, callSel, onSelectCall, toolFilter, onToggleTool }) {
  const active = Array.isArray(toolFilter) ? toolFilter : []
  const head = h(
    'div',
    { className: 'duty-group-h' },
    h('span', null, 'шкала времени'),
    h('span', { className: 'duty-mono' }, scale.axis.text),
  )
  if (scale.empty) {
    return h('div', { className: 'duty-deal-scale' }, head, h('div', { className: 'duty-muted' }, 'событий и попыток нет: рисовать нечего'))
  }
  const kinds = []
  for (const marker of scale.markers) {
    if (marker.tick || kinds.some((item) => item.type === marker.type)) continue
    kinds.push(marker)
  }
  // Шкала: дорожки слева, легенда семейств — правой колонкой, как в макете
  // `d-merge`. Ось стоит ровно над дорожками: слева пустая колонка шириной
  // подписей, справа — та же область, где рисуются чипы.
  return h(
    'div',
    { className: 'duty-deal-scale' },
    head,
    h(
      'div',
      { className: 'duty-scale-wrap' },
      h(
        'div',
        { className: 'duty-scale-main' },
        h(
          'div',
          { className: 'duty-scale-head' },
          h('div', { className: 'duty-scale-head-gap' }),
          h(
            'div',
            { className: 'duty-scale-axis' },
            ...scale.axis.ticks.map((tick) =>
              h(
                'span',
                { className: `duty-scale-tick align-${tick.align}`, style: { left: `${tick.pct}%` }, title: tick.clock },
                tick.edge ? `${tick.edge} ${tick.clock}` : tick.clock,
              ),
            ),
          ),
        ),
        // Дорожки этапов — таймлайн вызовов: свёрнутая полоса, карта вызовов по
        // наведению. Сетка по меткам оси одна на все дорожки: время читается
        // поперёк, а не угадывается по трём подписям (макет `d-merge`).
        h(
          'div',
          { className: 'duty-scale-plot' },
          h(
            'div',
            { className: 'duty-scale-grid', 'aria-hidden': 'true' },
            ...scale.axis.ticks.map((tick) =>
              h('span', { key: `grid-${tick.pct}`, className: 'duty-scale-grid-line', style: { left: `${tick.pct}%` } }),
            ),
          ),
          h(
            'div',
            { className: 'duty-tl-lanes' },
            ...scale.lanes.map((lane, index) =>
              h(DutyLane, {
                key: `lane-${lane.key}`,
                lane,
                bars: scale.bars.filter((bar) => bar.lane === index),
                scale,
                callSel,
                onSelectCall,
                toolFilter: active,
              }),
            ),
          ),
        ),
      ),
      // Легенда семейств инструментов: числа вызовов приходят из шейпера
      // (`scale.tools`), поверхность их только печатает.
      h(
        'div',
        { className: 'duty-scale-side' },
        h('span', { className: 'duty-scale-keys-h' }, 'инструменты'),
        ...scale.tools.map((item) =>
          h(
            'span',
            {
              className: `duty-scale-key${active.includes(item.tone) ? ' on' : ''}`,
              key: `key-${item.tone}`,
              role: 'button',
              tabIndex: 0,
              'aria-pressed': active.includes(item.tone) ? 'true' : 'false',
              'aria-label': `${item.label}: ${item.count}. Клик — подсветить вызовы этого семейства на дорожках`,
              onClick: () => onToggleTool && onToggleTool(item.tone),
              onKeyDown: (event) => {
                if (!onToggleTool || (event.key !== 'Enter' && event.key !== ' ')) return
                event.preventDefault()
                onToggleTool(item.tone)
              },
            },
            h('span', { className: `duty-scale-key-dot duty-t-${item.tone}` }),
            h('span', { className: 'duty-scale-key-l' }, item.label),
            h('span', { className: 'duty-scale-key-n' }, String(item.count)),
          ),
        ),
      ),
    ),
    h(CallInspector, { call: callOf(scale, callSel) }),
    kinds.length
      ? h(
          'div',
          { className: 'duty-scale-legend' },
          ...kinds.map((marker) =>
            h(
              'span',
              { key: `lg-${marker.type}`, style: { color: TONE_COLOR[marker.tone] || TONE_COLOR.gray } },
              `${MARKER_GLYPH[marker.type] || '•'} ${marker.label}`,
            ),
          ),
        )
      : null,
    scale.facts.attempts.journal ? h('div', { className: 'duty-muted' }, 'полосы собраны из журнала: ответ попыток пуст') : null,
  )
}

/* ---------------- экран «почему стоит» ---------------- */

/**
 * Карточка одного дела: состояние и доказательство словами бэкенда.
 *
 * Ничего не досчитывается: `waiting.evidence` — это факты, которые сработали в
 * бэкенде (лизинг, pid, heartbeat, граф зависимостей, пул вопросов). Экран
 * показывает их целиком, чтобы «почему стоит» можно было проверить, а не
 * поверить.
 */
function WhyCard({ deal, onOpen }) {
  return h(
    'article',
    { className: 'duty-why-card' },
    h('div', { className: 'duty-why-title' }, deal.titleFull || deal.title),
    h(
      'div',
      { className: 'duty-why-meta' },
      h(StateBadge, { label: deal.stateLabel, tone: deal.tone, title: deal.state }),
      h('span', { className: 'duty-mono' }, `${deal.repoLabel} · ${deal.id}`),
      h('span', { className: 'duty-muted' }, deal.assignee),
    ),
    h('div', { className: 'duty-evidence', style: { margin: '6px 0 0' } }, deal.evidence || 'доказательство не записано'),
    h(
      'div',
      { className: 'duty-why-foot' },
      h('span', null, `попытка: ${deal.attemptAgeText} (${deal.attemptNote})`),
      h('span', null, `действие: ${deal.lastActionAgeText} назад`),
      h(Button, { size: 'xs', variant: 'secondary', onClick: () => onOpen(deal) }, 'открыть дело'),
    ),
  )
}

/**
 * Группа «Почему стоит»: состояния по-прежнему группируют объяснения, поэтому
 * заголовок секции — подпись состояния; репозиторий в карточке назван рядом с id.
 */
function WhyScreen({ model, stale, onOpen }) {
  const lines = noticeLines(model, stale)
  return h(
    'div',
    { className: 'duty-scroll' },
    lines.length
      ? h('div', { className: 'duty-note', style: { margin: '0 0 10px' } }, ...lines.map((line, i) => h('div', { key: `n-${i}` }, line)))
      : null,
    h(
      'div',
      { className: 'duty-muted', style: { margin: '0 2px 10px' } },
      'группа — состояние ожидания, текст в рамке — доказательство из бэкенда; страница ничего не досчитывает',
    ),
    !model.count
      ? h('div', { className: 'duty-empty' }, h('b', null, 'нечего объяснять: активных дел нет'))
      : h(
          'div',
          null,
          ...model.stateGroups.map((group) =>
            h(
              'section',
              { key: `why-${group.state}`, style: { marginBottom: '16px' } },
              h(
                'div',
                { className: 'duty-group-h' },
                h(StateBadge, { label: group.label, tone: group.tone, title: group.state }),
                h('span', { className: 'duty-num' }, `${group.count}`),
                h('span', { className: 'duty-mono' }, group.state),
              ),
              h(
                'div',
                { className: 'duty-why-grid' },
                ...group.deals.map((deal) => h(WhyCard, { key: deal.id, deal, onOpen })),
              ),
            ),
          ),
        ),
  )
}

/* ---------------- экран «здоровье» ---------------- */

function Kpi({ title, value, sub, barPct }) {
  return h(
    'div',
    { className: 'duty-kpi' },
    h('div', { className: 'duty-kpi-k' }, title),
    h('div', { className: 'duty-kpi-v' }, value),
    h('div', { className: 'duty-muted' }, sub),
    barPct === null || barPct === undefined
      ? null
      : h('div', { className: 'duty-kpi-track' }, h('div', { className: 'duty-kpi-fill', style: { width: `${barPct}%` } })),
  )
}

/**
 * Таксономия исходов: доля каждого исхода среди всех ранов.
 *
 * Наборы успеха и падения приходят в ответе (`success_outcomes`/
 * `crash_outcomes`) — страница не решает сама, что считать падением.
 */
function Taxonomy({ stats }) {
  const entries = Object.entries(stats.totals.attempts_by_outcome || {}).sort((a, b) => b[1] - a[1])
  const crashes = new Set(stats.crash_outcomes || [])
  const success = new Set(stats.success_outcomes || [])
  if (!entries.length) return h('div', { className: 'duty-muted' }, 'попыток нет: таксономия считается по журналу дел')
  const max = entries[0][1] || 1
  return h(
    'div',
    { className: 'duty-tax' },
    ...entries.map(([key, count]) => {
      const tone = crashes.has(key) ? TONE_COLOR.red : success.has(key) ? TONE_COLOR.green : TONE_COLOR.gray
      return h(
        'div',
        { className: 'duty-tax-row', key: `tax-${key}` },
        h('span', { className: 'duty-tax-name', title: key }, key),
        h('div', { className: 'duty-tax-track' }, h('div', { className: 'duty-tax-fill', style: { width: `${Math.max(1, Math.round((count / max) * 100))}%`, background: tone } })),
        h('span', { className: 'duty-num', style: { textAlign: 'right' } }, `${count}`),
      )
    }),
  )
}

/**
 * Таблица «по репозиториям»: те же группы и числа, что печатает `bin/duty stats`.
 * Источник — `repos[]` и `without_repo` ответа `/stats`: доски в статистике не
 * участвуют (0.0.6), «без репозитория» — законная строка.
 */
function RepoHealthTable({ stats }) {
  const groups = [...(stats.repos || []), ...(stats.without_repo && stats.without_repo.deals ? [stats.without_repo] : [])]
  if (!groups.length) return h('div', { className: 'duty-muted' }, 'дел в store нет: считать нечего')
  return h(
    'table',
    { className: 'duty-table duty-health-table' },
    h(
      'thead',
      null,
      h(
        'tr',
        null,
        h('th', null, 'репозиторий'),
        h('th', null, 'дел'),
        h('th', null, 'попыток'),
        h('th', null, 'с первой попытки'),
        h('th', null, 'ожидание p50 / p90'),
        h('th', null, 'ожиданий'),
      ),
    ),
    h(
      'tbody',
      null,
      ...groups.map((group) => {
        const share = group.first_try.share
        return h(
          'tr',
          { key: `rh-${group.repo || 'without'}` },
          h('td', { className: 'duty-mono' }, group.repo || WITHOUT_REPO_LABEL),
          h('td', { className: 'duty-num' }, `${group.deals}`),
          h('td', { className: 'duty-num' }, `${group.attempts_total}`),
          h('td', { className: 'duty-num' }, `${group.first_try.ok}/${group.first_try.closed} · ${share === null ? '—' : `${Math.round(share * 100)}%`}`),
          h('td', { className: 'duty-num' }, group.waiting_sec.p50 === null ? '—' : `${fmtAge(group.waiting_sec.p50)} / ${fmtAge(group.waiting_sec.p90)}`),
          h('td', { className: 'duty-num' }, group.waiting_sec.count ? `${group.waiting_sec.count}` : '—'),
        )
      }),
    ),
  )
}

function HealthScreen({ stats }) {
  if (stats.error && !stats.data) {
    return h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-banner', style: { margin: 0 } }, h('span', null, stats.error), h(Button, { size: 'xs', variant: 'secondary', onClick: () => void stats.reload() }, 'повторить')))
  }
  if (!stats.data) {
    return h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 5 }, (_, i) => h('div', { className: 'duty-skel', key: `hs-${i}`, style: { width: `${92 - i * 10}%` } }))))
  }
  const data = stats.data
  const totals = data.totals
  const repos = data.repos || []
  const without = data.without_repo || null
  const crashes = new Set(data.crash_outcomes || [])
  const crashRuns = Object.entries(totals.attempts_by_outcome || {}).reduce((acc, [key, count]) => (crashes.has(key) ? acc + count : acc), 0)
  const share = totals.first_try.share
  return h(
    'div',
    { className: 'duty-scroll' },
    stats.error ? h('div', { className: 'duty-banner', style: { margin: '0 0 10px' } }, h('span', null, `снимок не обновлён: ${stats.error}`), h(Button, { size: 'xs', variant: 'secondary', onClick: () => void stats.reload() }, 'повторить')) : null,
    data.errors.length
      ? h('div', { className: 'duty-note', style: { margin: '0 0 10px' } }, ...data.errors.map((err, i) => h('div', { key: `he-${i}` }, `источник ${err.source} недоступен: ${err.reason}`)))
      : null,
    data.warnings.length
      ? h('div', { className: 'duty-note', style: { margin: '0 0 10px' } }, ...data.warnings.map((text, i) => h('div', { key: `hw-${i}` }, text)))
      : null,
    // Окно — часть числа: «за сутки» и «за всё время» — разные метрики.
    h(
      'div',
      { className: 'duty-muted', style: { margin: '0 0 10px' } },
      `окно: ${(data.window || {}).source === 'default' ? 'сутки по умолчанию' : 'задано вручную'} (${fmtAge((data.window || {}).sec)}) · числа считаются по журналу дел; карточки kanban — отдельное наблюдение`,
    ),
    h(
      'div',
      { className: 'duty-kpis' },
      h(Kpi, { title: 'дела с первой попытки', value: share === null ? '—' : `${Math.round(share * 100)}%`, sub: `${totals.first_try.ok} из ${totals.first_try.closed} этапов, закрытых попытками`, barPct: share === null ? null : Math.round(share * 100) }),
      h(Kpi, { title: 'ожидание до первой попытки: p50 / p90', value: totals.waiting_sec.p50 === null ? '—' : `${fmtAge(totals.waiting_sec.p50)} / ${fmtAge(totals.waiting_sec.p90)}`, sub: totals.waiting_sec.count ? `p99: ${fmtAge(totals.waiting_sec.p99)} · максимум: ${fmtAge(totals.waiting_sec.max)} · выборка: ${totals.waiting_sec.count} дел (created_at → started_at)` : 'выборки нет: у дел среза нет ни одного рана (created_at → started_at)' }),
      h(Kpi, { title: 'попыток всего', value: `${totals.attempts_total}`, sub: `падений: ${crashRuns}${totals.attempts_total ? ` · ${Math.round((crashRuns / totals.attempts_total) * 100)}%` : ''}` }),
      h(Kpi, { title: 'репозиториев', value: `${repos.length}`, sub: `дел в store: ${totals.deals} · без репозитория: ${without ? without.deals : 0}` }),
    ),
    h('div', { className: 'duty-group-h' }, h('span', null, 'таксономия исходов попыток'), h('span', { className: 'duty-mono' }, 'по журналу дел')),
    h(Taxonomy, { stats: data }),
    h('div', { className: 'duty-group-h', style: { marginTop: '14px' } }, h('span', null, 'репозитории')),
    h(RepoHealthTable, { stats: data }),
    h(
      'div',
      { className: 'duty-muted', style: { marginTop: '10px' } },
      'успех и падение — наборы исходов из ответа бэкенда; «с первой попытки» — этап журнала закрыт первой же попыткой и успешно; p50/p90/p99 — метод ближайшего ранга по объединённой выборке дел репозитория (сортировка одна, интерполяции нет); доски в статистике не участвуют',
    ),
  )
}

/* ---------------- экран «завершённые» (0.0.7) ---------------- */

/**
 * Список закрытых дел: причина, этапы, попытки, вопросы, длительность, публикация.
 *
 * Числа — поля ответа `/deals` (`shapeFinished`, шейпер общий с вебом): ни
 * страница, ни шейпер их не пересчитывают, иначе строка списка спорила бы с
 * карточкой того же дела (`bin/duty deal <id>`).
 */
function FinishedTable({ group, onPick }) {
  return h(
    'table',
    { className: 'duty-table duty-finished-table' },
    h(
      'thead',
      null,
      h(
        'tr',
        null,
        h('th', null, 'закрыто'),
        h('th', null, 'дело'),
        h('th', null, 'заголовок'),
        h('th', null, 'статус'),
        h('th', null, 'попыток'),
        h('th', null, 'вопросов'),
        h('th', null, 'длительность'),
        h('th', null, 'причина'),
        h('th', null, 'этапы'),
        h('th', null, 'публикация'),
      ),
    ),
    h(
      'tbody',
      null,
      ...group.deals.map((deal) =>
        h(
          'tr',
          {
            key: deal.id,
            className: 'duty-row',
            title: [deal.repoLabel, deal.causeText].filter(Boolean).join(' · '),
            onClick: () => onPick(deal),
          },
          h('td', { className: 'duty-mono' }, deal.finishedText),
          h('td', { className: 'duty-mono', title: deal.id }, deal.id),
          h('td', { title: deal.titleFull }, deal.title),
          h('td', null, h(StateBadge, { label: deal.statusLabel, tone: deal.tone, title: deal.status })),
          h('td', { className: 'duty-num', title: 'попыток в журнале' }, `${deal.attempts}`),
          h('td', { className: 'duty-num', title: 'вопросы человеку по журналу' }, deal.questions.text),
          h('td', { className: 'duty-num', title: 'длительность дела: события журнала и попытки' }, deal.durationText),
          h('td', { title: deal.causeText }, deal.causeText),
          h('td', { className: 'duty-muted', title: deal.stagesText }, deal.stagesText),
          h(
            'td',
            {
              className: 'duty-muted',
              title: deal.published ? deal.published.mrUrl || deal.published.ref || 'факт журнала' : 'публикации нет',
            },
            deal.published ? deal.published.text : '—',
          ),
        ),
      ),
    ),
  )
}

function FinishedScreen({ finished, onPick }) {
  if (finished.error && !finished.data) {
    return h(
      'div',
      { className: 'duty-scroll' },
      h(
        'div',
        { className: 'duty-banner', style: { margin: 0 } },
        h('span', null, finished.error),
        h(Button, { size: 'xs', variant: 'secondary', onClick: () => void finished.reload() }, 'повторить'),
      ),
    )
  }
  if (!finished.data) {
    return h(
      'div',
      { className: 'duty-scroll' },
      h(
        'div',
        { className: 'duty-skel-row' },
        ...Array.from({ length: 5 }, (_, i) => h('div', { className: 'duty-skel', key: `fs-${i}`, style: { width: `${92 - i * 10}%` } })),
      ),
    )
  }
  const model = shapeFinished(finished.data)
  return h(
    'div',
    { className: 'duty-scroll' },
    finished.error
      ? h(
          'div',
          { className: 'duty-banner', style: { margin: '0 0 10px' } },
          h('span', null, `снимок не обновлён: ${finished.error}`),
          h(Button, { size: 'xs', variant: 'secondary', onClick: () => void finished.reload() }, 'повторить'),
        )
      : null,
    model.errors.length
      ? h(
          'div',
          { className: 'duty-note', style: { margin: '0 0 10px' } },
          ...model.errors.map((err, i) => h('div', { key: `fe-${i}` }, `источник ${err.source} недоступен: ${err.reason}`)),
        )
      : null,
    // Что это за список — словами: `/now` про активные, здесь — про сделанное.
    h(
      'div',
      { className: 'duty-muted', style: { margin: '0 0 10px' } },
      'здесь только завершённые дела — закрытые и архивные: причина закрытия, этапы, попытки, вопросы и длительность из журнала; карточки kanban — отдельное наблюдение, не дела',
    ),
    h(
      'div',
      { className: 'duty-muted duty-num', style: { margin: '0 0 10px' } },
      `дел: ${model.count} из ${model.total} · статусы: ${model.statuses.join(', ') || '—'}`,
    ),
    model.truncated
      ? h(
          'div',
          { className: 'duty-note', style: { margin: '0 0 10px' } },
          h('div', null, `показаны не все: лимит ${model.limit}, всего ${model.total} — список режется лимитом /deals`),
        )
      : null,
    model.count
      ? h(
          'div',
          { className: 'duty-list' },
          ...model.groups.map((group) =>
            h(
              'section',
              { className: 'duty-group', key: `fd-${group.repo || 'without'}` },
              h(
                'div',
                { className: 'duty-group-h' },
                h('span', { className: group.repo ? 'duty-mono' : 'duty-mono duty-muted', title: group.repo || 'репозиторий не назван' }, group.label),
                h('span', { className: 'duty-num' }, `${group.count}`),
                h('span', { className: 'duty-spacer' }),
                h('span', { className: 'duty-muted' }, `свежее закрытие: ${group.deals[0].finishedText}`),
              ),
              h(FinishedTable, { group, onPick }),
            ),
          ),
        )
      : h(
          'div',
          { className: 'duty-empty' },
          h('b', null, 'завершённых дел нет'),
          // Причина пустоты — словами бэкенда (`note`), а не своя догадка.
          h('span', { className: 'duty-muted' }, model.note || 'в store нет ни одного закрытого дела'),
        ),
  )
}

/* ---------------- экран «дело» ---------------- */

function DealScreen({ sel, onBack }) {
  const detail = useDeal(sel.id)
  const query = useAttempts(sel.id)
  const deal = useMemo(() => (detail.data && detail.data.deal ? shapeDeal(detail.data.deal) : null), [detail.data])
  const timeline = useMemo(() => (query.data ? shapeAttempts(query.data) : null), [query.data])
  // Шкала времени — тот же шейпер, что в вебе (`shapeDealTime`): ось, дорожки,
  // полосы и маркеры считаются в одном месте, второго расчёта геометрии нет.
  const scale = useMemo(() => shapeDealTime(detail.data, query.data), [detail.data, query.data])
  // Лента этапов приходит тем же ответом `/deal` (`stages`), что и голова дела:
  // одна ручка — один кадр, этапы не могут отставать от фактов дела.
  const feed = useMemo(() => shapeStages(detail.data), [detail.data])
  // Выбранный вызов живёт на экране дела: клик по чипу открывает инспектор, но
  // строку попытки не разворачивает — развёртка осталась под указателем.
  const [callSel, setCallSel] = useState('')
  const selectCall = useCallback((id) => {
    setCallSel((prev) => (prev === id ? '' : id))
  }, [])
  // Подсветка семейства инструментов: набор тонов из легенды. Клик по семейству
  // гасит остальные вызовы, повторный — снимает подсветку.
  const [toolFilter, setToolFilter] = useState([])
  const toggleTool = useCallback((tone) => {
    setToolFilter((prev) => (prev.includes(tone) ? prev.filter((item) => item !== tone) : [...prev, tone]))
  }, [])

  const head = h(
    'div',
    { className: 'duty-deal-head' },
    // Заголовок: слева имя дела и граф этапов (что прошло и где падало), справа
    // в углу — статус и возврат к списку. Граф раньше висел отдельной панелью
    // внизу и занимал место, ничего не сообщая.
    h(
      'div',
      { className: 'duty-panel-h duty-deal-head-top' },
      h(
        'div',
        { style: { flex: '1 1 auto', minWidth: 0 } },
        h(
          'div',
          { className: 'duty-mono', title: repoLabel((detail.data && detail.data.repo) || sel.repo) },
          `${repoShort((detail.data && detail.data.repo) || sel.repo)} · ${sel.id}`,
        ),
        h('div', { className: 'duty-deal-title' }, (detail.data && detail.data.title) || sel.titleFull || '—'),
        detail.data ? h(SpecStrip, { spec: scale.spec }) : null,
      ),
      h(
        'div',
        { className: 'duty-deal-head-side' },
        deal
          ? h(StateBadge, { label: deal.stateLabel, tone: deal.tone, title: deal.state })
          : detail.data
            // Закрытое дело: статус словами (`завершено`/`в архиве`), значение
            // store — в подсказке.
            ? h(StateBadge, { label: finishedStatusMeta(detail.data.status).label, tone: 'gray', title: detail.data.status })
            : null,
        h(Button, { size: 'xs', variant: 'secondary', onClick: onBack }, '← к списку'),
      ),
    ),
    deal ? h('div', { className: 'duty-evidence' }, deal.evidence || 'доказательство не записано') : null,
    detail.data ? h(DealFactSpans, { scale }) : null,
    detail.data
      ? h(
          'div',
          { className: 'duty-deal-facts' },
          h('span', null, 'статус: ', h('b', null, detail.data.status || '—')),
          h('span', null, 'исполнитель: ', h('b', null, detail.data.assignee || '—')),
          deal ? h('span', null, 'попытка: ', h('b', null, `${deal.attemptAgeText} (${deal.attemptNote})`)) : null,
          deal ? h('span', null, 'лизинг: ', h('b', null, deal.leaseText)) : null,
          deal ? h('span', null, 'действие: ', h('b', null, `${deal.lastActionAgeText} назад`)) : null,
        )
      : null,
  )

  let body
  if (detail.error && !detail.data) {
    body = h('div', { className: 'duty-banner', style: { margin: 0 } }, h('span', null, detail.error), h(Button, { size: 'xs', variant: 'secondary', onClick: () => void detail.reload() }, 'повторить'))
  } else if (!detail.data) {
    body = h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 4 }, (_, i) => h('div', { className: 'duty-skel', key: `ds-${i}`, style: { width: `${90 - i * 12}%` } })))
  } else {
    const docs = docBlocks(detail.data)
    // Документы дела: тело из store и спека — разные документы, подменять один
    // другим нельзя (у дел из `bin/duty create` описания в store нет вовсе).
    // Нет документов — нет колонки: столбец этапов остаётся один на всю ширину.
    const stages = h(CompactStages, { feed, timeline })
    body = h(
      'div',
      { className: 'duty-deal-body' },
      // Шкала — первой и во всю ширину: это главный факт дела, а спецификация и
      // столбец этапов — пояснение к нему.
      h(KpiStrip, { scale }),
      h(ScalePanel, { scale, callSel, onSelectCall: selectCall, toolFilter, onToggleTool: toggleTool }),
      docs.length
        ? h(
            'div',
            { className: 'duty-deal-cols' },
            h(
              'div',
              { className: 'duty-deal-docs' },
              ...docs.map((doc) =>
                h(
                  'div',
                  { className: 'duty-deal-spec', key: `doc-${doc.kind}` },
                  h(
                    'div',
                    { className: 'duty-group-h', title: doc.path || '' },
                    h('span', null, doc.name ? `${doc.label} · ${doc.name}` : doc.label),
                    doc.truncated ? h('span', { className: 'duty-mono' }, 'обрезана лимитом') : null,
                  ),
                  doc.kind === 'spec'
                    ? h('pre', { className: 'duty-spec duty-doc-src duty-mono' }, doc.text)
                    : h('div', { className: 'duty-spec duty-md' }, h(Streamdown, null, doc.text)),
                ),
              ),
            ),
            stages,
          )
        : stages,
    )
  }

  return h('div', { className: 'duty-scroll' }, head, body)
}

function EmptyState({ view, model, onReset, stale }) {
  const filtered = model.count > 0 && view.count === 0
  const lines = noticeLines(model, stale)
  return h(
    'div',
    { className: 'duty-empty' },
    h('b', null, filtered ? 'под фильтры ничего не подошло' : 'нет активных дел'),
    h(
      'span',
      { className: 'duty-muted' },
      `репозиториев в store: ${model.repoCount} · дел без репозитория: ${model.withoutRepo}`,
    ),
    filtered ? h(Button, { size: 'xs', variant: 'secondary', onClick: onReset }, 'сбросить фильтры') : null,
    lines.length
      ? h('ul', null, ...lines.map((line, i) => h('li', { key: `notice-${i}` }, line)))
      : null,
  )
}

function DutyPage() {
  const snap = useNow()
  const [screen, setScreen] = useState('now')
  // Фильтр по репозиторию вместо доски: ALL_REPOS — все, '' — без репозитория.
  const [repo, setRepo] = useState(ALL_REPOS)
  const [states, setStates] = useState(() => new Set())
  const [query, setQuery] = useState('')
  const [effQuery, setEffQuery] = useState('')
  const [sel, setSel] = useState(null)
  const [tick, setTick] = useState(() => Date.now())
  // «Здоровье» запрашивается только когда экран открыт: это скан всех досок.
  const stats = useStats(screen === 'health')
  // «Завершённые» — тоже по требованию: список закрытых дел не нужен «Сейчас».
  const finished = useFinishedFeeds(screen === 'deals')

  // Поиск — локальный и с задержкой: данные не перезапрашиваются на каждый символ.
  useEffect(() => {
    const timer = setTimeout(() => setEffQuery(query), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query])

  // Подпись «обновлено N с назад» тикает раз в секунду, независимо от запросов.
  useEffect(() => {
    const timer = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])

  const model = useMemo(() => (snap.data ? shapeNow(snap.data) : null), [snap.data])
  const view = useMemo(
    () => (model ? filterNow(model, { repo, states, query: effQuery }) : null),
    [model, repo, states, effQuery],
  )

  if (!rest) {
    return h('div', { className: 'duty-page' }, h('div', { className: 'duty-banner' }, 'плагин не зарегистрирован'))
  }

  const updatedAgo = snap.updatedAt ? Math.max(0, Math.floor((tick - snap.updatedAt) / 1000)) : null
  const selectedKey = sel ? sel.id : ''
  // Клик по делу открывает его собственным экраном: у дела есть спецификация и
  // таймлайн попыток, и в узкую панель они не помещаются.
  const pick = (deal) => {
    lastDeal = deal
    setSel(deal)
    setScreen('deal')
  }
  const toggleState = (state) =>
    setStates((prev) => {
      const next = new Set(prev)
      if (next.has(state)) next.delete(state)
      else next.add(state)
      return next
    })
  const resetFilters = () => {
    setRepo(ALL_REPOS)
    setStates(new Set())
    setQuery('')
    setEffQuery('')
  }

  const header = h(
    'header',
    { className: 'duty-head' },
    h('h1', null, 'Дела'),
    h(
      'div',
      { className: 'duty-tabs' },
      ...TABS.map(([key, label]) =>
        h(
          'button',
          {
            key,
            type: 'button',
            className: screen === key ? 'duty-tab on' : 'duty-tab',
            onClick: () => {
              setScreen(key)
              // «Дело» возвращает к последнему открытому; остальные табы — без выбора.
              setSel(key === 'deal' ? lastDeal : null)
            },
          },
          label,
        ),
      ),
    ),
    model ? h('span', { className: 'duty-muted duty-num' }, `сейчас: ${view.count}`) : null,
    model && view
      ? h(
          'span',
          {
            className: 'duty-muted duty-num',
            title: 'ждёт человека: N вопросов (есть живой вопрос) + M блокировок (вопроса нет)',
          },
          humanLine(view.humanCounts),
        )
      : null,
    model ? h('span', { className: 'duty-muted' }, `· репозиториев ${model.repoCount} · без репозитория ${model.withoutRepo}`) : null,
    h('span', { className: 'duty-spacer' }),
    snap.loading && !model
      ? h('span', { className: 'duty-muted' }, 'загрузка…')
      : h('span', { className: 'duty-muted duty-num' }, updatedAgo === null ? 'ещё не обновлялось' : `обновлено ${updatedAgo} с назад`),
    h(
      'button',
      { type: 'button', className: 'duty-icon-btn', title: 'Обновить сейчас', onClick: () => void snap.refresh() },
      h(Codicon, { name: 'refresh', size: '0.85rem' }),
    ),
  )

  const banner = snap.error
    ? h(
        'div',
        { className: 'duty-banner' },
        h(
          'span',
          null,
          model
            ? `снимок от ${fmtClock(model.now)} — обновление не удалось: ${snap.error}`
            : `ошибка запроса /now: ${snap.error}`,
        ),
        h(Button, { size: 'xs', variant: 'secondary', onClick: () => void snap.refresh() }, 'повторить'),
      )
    : null

  // Упавшие источники и неполный срез — жёлтой полосой над списком, чтобы
  // неполнота ответа была видна и тогда, когда дела есть (в пустом состоянии те же
  // строки перечисляет EmptyState). Показываем их только по свежему ответу: при
  // `snap.error` экран держит прошлый снимок, и его замечания — не факт о
  // настоящем (иначе «источник X недоступен» висит вечно, хотя последний ответ его
  // уже не содержит).
  const stale = Boolean(snap.error)
  const noticeRows = noticeLines(model, stale)
  const notices = noticeRows.length && view && view.count > 0
    ? h(
        'div',
        { className: 'duty-note' },
        ...noticeRows.map((line, i) => h('div', { key: `notice-${i}` }, line)),
      )
    : null

  const filters = model
    ? h(
        'div',
        { className: 'duty-filters' },
        h(
          'select',
          {
            className: 'duty-select',
            value: repo,
            onChange: (e) => setRepo(e.target.value),
            title: 'Фильтр по репозиторию',
          },
          h('option', { value: ALL_REPOS }, 'все репозитории'),
          // «Без репозитория» — отдельная группа, и в списке фильтра она тоже
          // отдельная строка: пустое значение отличает её от «фильтра нет».
          model.withoutRepo > 0
            ? h('option', { value: '' }, `${WITHOUT_REPO_LABEL} (${model.withoutRepo})`)
            : null,
          ...model.repos.list.map((item) =>
            h('option', { value: item.repo, key: `repo-${item.repo}` }, `${item.repo} (${item.deals})`),
          ),
        ),
        h(
          'div',
          { className: 'duty-chips' },
          h(
            'button',
            {
              type: 'button',
              className: states.size ? 'duty-chip' : 'duty-chip on',
              onClick: () => setStates(new Set()),
            },
            'все',
            h('span', { className: 'duty-num' }, `${model.count}`),
          ),
          ...model.stateOrder.map((state) => {
            const meta = stateMeta(state)
            // Подпись чипа — та же, что у бейджа: «ждёт ответа» стоит только там,
            // где в этом состоянии действительно есть вопрос.
            const count = model.byState[state] || 0
            return h(
              'button',
              {
                type: 'button',
                key: `chip-${state}`,
                className: states.has(state) ? 'duty-chip on' : 'duty-chip',
                style: { color: TONE_COLOR[meta.tone] },
                title: state,
                onClick: () => toggleState(state),
              },
              stateLabel(state, model.deals),
              h('span', { className: 'duty-num' }, `${count}`),
            )
          }),
        ),
        h(Input, {
          style: { width: '220px', height: '26px' },
          placeholder: 'поиск: id, заголовок',
          value: query,
          onChange: (e) => setQuery(e.target.value),
        }),
      )
    : null

  let list
  if (!model) {
    list = h(
      'div',
      { className: 'duty-list' },
      h(
        'div',
        { className: 'duty-skel-row' },
        ...Array.from({ length: 6 }, (_, i) => h('div', { className: 'duty-skel', key: `boot-${i}`, style: { width: `${95 - i * 8}%` } })),
      ),
    )
  } else if (!view.count) {
    list = h('div', { className: 'duty-list' }, h(EmptyState, { view, model, onReset: resetFilters, stale }))
  } else {
    list = h(
      'div',
      { className: 'duty-list' },
      ...view.groups.map((group) => h(DealGroup, { key: `grp-${group.repo || 'without'}`, group, selectedKey, onPick: pick })),
    )
  }

  // Экраны переключаются табами; данные для каждого — свои, из одного и того же
  // бэкенда. «Дело» открывается из списка или из «Почему стоит».
  let content
  if (screen === 'deal' && sel) {
    content = h(DealScreen, {
      key: sel.id,
      sel,
      onBack: () => {
        setSel(null)
        setScreen('now')
      },
    })
  } else if (screen === 'deal') {
    // Дело ещё не выбрано: показываем активные дела — экран обязан быть
    // самостоятельным, а не только следствием клика по строке.
    content = model
      ? h(
          'div',
          { className: 'duty-scroll' },
          h('div', { className: 'duty-why-meta' }, 'выбери дело из активных'),
          ...view.groups.map((group) => h(DealGroup, { key: `pick-${group.repo || 'without'}`, group, selectedKey, onPick: pick })),
        )
      : h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 4 }, (_, i) => h('div', { className: 'duty-skel', key: `ds-${i}`, style: { width: `${92 - i * 9}%` } }))))
  } else if (screen === 'why') {
    content = model
      ? h(WhyScreen, { model, stale, onOpen: pick })
      : h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 6 }, (_, i) => h('div', { className: 'duty-skel', key: `ws-${i}`, style: { width: `${95 - i * 8}%` } }))))
  } else if (screen === 'health') {
    content = h(HealthScreen, { stats })
  } else if (screen === 'deals') {
    content = h(FinishedScreen, { finished, onPick: pick })
  } else {
    content = h('div', { className: 'duty-body' }, list)
  }

  return h(
    'div',
    { className: 'duty-page' },
    header,
    screen === 'now' || screen === 'why' ? banner : null,
    screen === 'now' ? notices : null,
    screen === 'now' ? filters : null,
    content,
  )
}

/* ---------------- plugin ---------------- */

export default {
  id: ID, // должен совпадать с именем папки
  name: 'Дела',
  register(ctx) {
    rest = ctx.rest.bind(ctx)

    const style = document.createElement('style')
    style.dataset.plugin = ID
    style.textContent = CSS
    document.head.appendChild(style)

    ctx.onDispose(() => {
      style.remove()
      rest = null
    })

    // Полноэкранная страница «Дела» — вместо чата (area 'routes').
    ctx.register({
      id: 'duty-page',
      area: 'routes',
      data: { path: '/duty' },
      render: () => h(DutyPage),
    })

    // Пункт в сайдбаре рядом с «Тикетами» (area 'sidebar.nav', order 61).
    ctx.register({
      id: 'duty-nav',
      area: 'sidebar.nav',
      order: 61,
      data: { label: 'Дела', path: '/duty', codicon: 'pulse' },
    })
  },
}