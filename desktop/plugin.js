/**
 * Hermes Duty — desktop-плагин «Дела»: страница /duty (экран «сейчас» + попытки).
 *
 * Форма — как у hermes-tickets: `export default { id, name, register(ctx) }`,
 * страница в `area: 'routes'`, пункт в `area: 'sidebar.nav'`. Данные — ТОЛЬКО
 * через `ctx.rest('/now')` и `ctx.rest('/duty/{board}/{id}/attempts')`: прямого
 * fetch и шелла из UI нет, ядро не импортируется, файлы плагин не читает.
 *
 * Классификация состояний — не здесь: страница рисует `waiting.state` и
 * `waiting.evidence` ровно как их отдал бэкенд (`dashboard/plugin_api.py`,
 * `WAITING_PRIORITY`). Единственная логика UI — группировка по этому полю,
 * форматирование чисел и локальные фильтры.
 *
 * UI собран без JSX (файл грузится нескомпилированным, см. kanban-graph).
 */
import { Button, Codicon, Input } from '@hermes/plugin-sdk'
import { createElement as h, useCallback, useEffect, useMemo, useRef, useState } from 'react'

const ID = 'hermes-duty'
const PATH = '/duty'
const REFRESH_MS = 10_000
// «Здоровье» — агрегат по всей истории ранов: он не меняется за секунды, а скан
// всех досок дороже среза «сейчас», поэтому обновляется реже.
const STATS_REFRESH_MS = 30_000
const SEARCH_DEBOUNCE_MS = 240
const REQUEST_TIMEOUT_MS = 20_000

// Экраны страницы, в порядке спеки: «Сейчас» — срез, «Дело» — спецификация и
// таймлайн попыток, «Почему стоит» — тот же срез с объяснением, «Здоровье» —
// агрегат по ранам. «Дело» открывается из любой строки, но и сам по себе таб:
// экран, достижимый только кликом по строке, на своём месте не читается.
const TABS = [
  ['now', 'Сейчас'],
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
const OUTCOME_TONE = {
  done: 'green',
  completed: 'green',
  succeeded: 'green',
  rate_limited: 'yellow',
  reclaimed: 'yellow',
}

const LEASE_LABEL = {
  live: 'жив',
  expired: 'истёк',
  none: 'нет',
  unknown: 'неизвестен',
}

/** Мета состояния; неизвестное состояние показываем как есть серым. */
function stateMeta(state) {
  const key = String(state || '')
  return STATE_META[key] || { label: key || '—', tone: 'gray' }
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
  const meta = stateMeta(waiting.state)
  const pidAlive = attempt ? attempt.pid_alive : null
  const leaseState = String(lease.state || 'none')
  const attemptAge = attempt && attempt.age_sec !== undefined ? attempt.age_sec : null
  const lastAge = action.age_sec !== undefined ? action.age_sec : null
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
    board: String(d.board || ''),
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
    // Пустое значение — состояние не про ожидание человека.
    humanKind: String(waiting.human_kind || ''),
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
    search: `${d.board || ''} ${d.id || ''} ${d.title || ''} ${d.assignee || ''} ${waiting.state || ''}`.toLowerCase(),
  }
}

/**
 * Группы строк по `waiting.state` — в порядке первого появления в ответе.
 *
 * `/now` сортирует дела по `WAITING_PRIORITY` бэкенда, поэтому порядок групп
 * получается приоритетным без второго списка приоритетов в UI; строки внутри
 * группы сохраняют порядок ответа.
 */
function groupDeals(deals) {
  const buckets = new Map()
  for (const deal of deals) {
    const state = deal.state || '—'
    if (!buckets.has(state)) buckets.set(state, [])
    buckets.get(state).push(deal)
  }
  const groups = []
  for (const [state, rows] of buckets) {
    const meta = stateMeta(state)
    groups.push({ state, label: meta.label, tone: meta.tone, count: rows.length, deals: rows })
  }
  return groups
}

/**
 * Модель экрана из ответа `/now`: строки, группы, доски и честный счётчик ошибок.
 *
 * `boards` отвечает на вопрос «сколько досок реально прочиталось»: запись с
 * `read !== true` — недоступная доска, её причина берётся из `errors`. Ошибки, не
 * относящиеся к доске (например, недоступный пул вопросов), уходят отдельно в
 * `otherErrors` — их нельзя выдавать за «доска недоступна».
 */
function shapeNow(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const deals = (Array.isArray(data.deals) ? data.deals : []).map(shapeDeal)
  const boards = (Array.isArray(data.boards) ? data.boards : []).map((board) => ({
    slug: String(board.slug || ''),
    read: board.read === true,
    deals: Number(board.deals) || 0,
    truncated: board.truncated === true,
    warnings: Array.isArray(board.warnings) ? board.warnings.map(String) : [],
  }))
  const errors = (Array.isArray(data.errors) ? data.errors : []).map((error) => ({
    board: String(error.board || ''),
    reason: String(error.reason || 'причина не записана'),
  }))
  const unreachable = boards
    .filter((board) => !board.read)
    .map((board) => ({
      board: board.slug,
      reason: (errors.find((error) => error.board === board.slug) || {}).reason || 'причина не записана',
    }))
  const boardSlugs = new Set(boards.map((board) => board.slug))
  // Прочитанная доска тоже бывает неполной: срез упёрся в лимит дел
  // (`truncated`) или вспомогательная таблица не прочиталась (`warnings`).
  // Молчать про это нельзя — доска тогда выглядит полной.
  const truncatedNotices = boards
    .filter((board) => board.truncated)
    .map((board) => `доска ${board.slug}: срез обрезан лимитом, показано ${board.deals} дел`)
  const warningNotices = boards.flatMap((board) =>
    board.warnings.map((warning) => `доска ${board.slug}: ${warning}`),
  )
  const byState = {}
  for (const deal of deals) byState[deal.state] = (byState[deal.state] || 0) + 1
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
    boards,
    errors,
    otherErrors: errors.filter((error) => !boardSlugs.has(error.board)),
    unreachable,
    truncatedNotices,
    warningNotices,
    boardsTotal: boards.length,
    boardsRead: boards.filter((board) => board.read).length,
    count: deals.length,
    byState,
    humanCounts: humanCounts(deals),
    stateOrder,
  }
}

/** Локальные фильтры страницы: доска, набор состояний, поиск по id/title/assignee. */
function filterNow(model, filter) {
  const f = filter && typeof filter === 'object' ? filter : {}
  const board = String(f.board || '')
  const states = f.states instanceof Set ? f.states : new Set(Array.isArray(f.states) ? f.states : [])
  const query = String(f.query || '').trim().toLowerCase()
  const deals = (model && model.deals ? model.deals : []).filter(
    (deal) =>
      (!board || deal.board === board) &&
      (!states.size || states.has(deal.state)) &&
      (!query || deal.search.includes(query)),
  )
  const byState = {}
  for (const deal of deals) byState[deal.state] = (byState[deal.state] || 0) + 1
  return { ...model, deals, groups: groupDeals(deals), count: deals.length, byState, humanCounts: humanCounts(deals) }
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
 * Таймлайн попыток из ответа `/duty/{board}/{id}/attempts`.
 *
 * `barPct` — ширина полосы попытки относительно самой долгой в этом таймлайне
 * (минимум 2%, чтобы мгновенная попытка не исчезала), так короче/длиннее видно
 * глазом, без абсолютной шкалы.
 */
function shapeAttempts(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const list = Array.isArray(data.attempts) ? data.attempts : []
  const crashes = Array.isArray(data.crash_outcomes) ? data.crash_outcomes.map(String) : []
  const longest = list.reduce((acc, attempt) => Math.max(acc, Number(attempt.duration_sec) || 0), 0) || 1
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
      status: String(attempt.status || '—'),
      start: attempt.start ?? null,
      end: attempt.end ?? null,
      duration,
      durationText: fmtAge(duration),
      barPct: Math.max(2, Math.round(((Number(duration) || 0) / longest) * 100)),
      outcome: String(attempt.outcome || ''),
      outcomeTone: outcomeTone(attempt.outcome, attempt.status, crashes),
      error: String(attempt.error || ''),
      summary: String(attempt.summary || ''),
      events,
    }
  })
  return {
    board: String(data.board || ''),
    taskId: String(data.task_id || ''),
    title: String(data.title || ''),
    count: attempts.length,
    attempts,
  }
}

/**
 * Строки неполноты ответа `/now`: недоступная доска, упавший источник,
 * обрезанная или деградировавшая доска.
 *
 * `stale` — последний запрос не прошёл, на экране прошлый снимок: его замечания
 * уже не факт о настоящем, поэтому отдаём пусто. Иначе «доска X недоступна»
 * переживает починку кода и висит, пока страницу не перезагрузят.
 */
function noticeLines(model, stale = false) {
  if (stale || !model) return []
  return [
    ...model.unreachable.map((bad) => `доска ${bad.board} недоступна: ${bad.reason}`),
    ...model.otherErrors.map((bad) => `источник ${bad.board} недоступен: ${bad.reason}`),
    ...model.truncatedNotices,
    ...model.warningNotices,
  ]
}
// duty:model-end

/* ---------------- стили страницы ---------------- */

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
.duty-att{border:1px solid var(--ui-stroke-tertiary);border-radius:6px;padding:8px;margin-bottom:8px}
.duty-att-h{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--ui-text-secondary)}
.duty-att-h .duty-num{color:var(--ui-text-primary)}
.duty-bar-track{height:6px;border-radius:3px;background:var(--ui-bg-quaternary);overflow:hidden;margin:6px 0 4px}
.duty-bar{height:6px;border-radius:3px;background:currentColor}
.duty-events{margin:6px 0 0;padding-left:14px;font-size:11px;color:var(--ui-text-secondary)}
.duty-events li{margin:1px 0}
.duty-error{color:var(--ui-red);font-size:11px;white-space:pre-wrap;word-break:break-word;margin-top:4px}
.duty-summary{font-size:11px;color:var(--ui-text-tertiary);margin-top:4px;word-break:break-word}
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
.duty-deal-head{display:flex;flex-direction:column;gap:6px;border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:10px 12px;margin-bottom:12px}
.duty-deal-title{font-size:13px;font-weight:600;word-break:break-word}
.duty-deal-facts{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--ui-text-secondary)}
.duty-deal-facts b{color:var(--ui-text-primary);font-weight:600}
.duty-deal-body{display:grid;grid-template-columns:minmax(280px,1fr) minmax(320px,1fr);gap:12px;align-items:start}
@media (max-width:1080px){.duty-deal-body{grid-template-columns:1fr}}
.duty-deal-spec,.duty-deal-attempts{min-width:0}
.duty-spec{margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11px;line-height:1.5;color:var(--ui-text-secondary);max-height:60vh;overflow:auto;border:1px solid var(--ui-stroke-tertiary);border-radius:6px;padding:8px}
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
function useAttempts(board, taskId) {
  const [state, setState] = useState({ key: '', data: null, error: null, loading: false })
  const key = board && taskId ? `${board}/${taskId}` : ''

  const load = useCallback(async () => {
    if (!board || !taskId) return
    const mine = `${board}/${taskId}`
    setState((prev) => ({ key: mine, data: prev.key === mine ? prev.data : null, error: null, loading: true }))
    try {
      const data = await rest(
        `/duty/${encodeURIComponent(board)}/${encodeURIComponent(taskId)}/attempts`,
        { timeoutMs: REQUEST_TIMEOUT_MS },
      )
      setState((prev) => (prev.key === mine ? { key: mine, data, error: null, loading: false } : prev))
    } catch (error) {
      setState((prev) => (prev.key === mine ? { ...prev, error: errText(error), loading: false } : prev))
    }
  }, [board, taskId])

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

/** Экран «Дело»: спецификация + тот же факт-набор, что в `/now`. */
function useDeal(board, taskId) {
  const [state, setState] = useState({ key: '', data: null, error: null, loading: false })
  const key = board && taskId ? `${board}/${taskId}` : ''

  const load = useCallback(async () => {
    if (!key) return
    setState((prev) => ({ key, data: prev.key === key ? prev.data : null, error: null, loading: true }))
    try {
      const data = await rest(`/deal/${encodeURIComponent(board)}/${encodeURIComponent(taskId)}`, {
        timeoutMs: REQUEST_TIMEOUT_MS,
      })
      setState((prev) => (prev.key === key ? { key, data, error: null, loading: false } : prev))
    } catch (error) {
      setState((prev) => (prev.key === key ? { ...prev, error: errText(error), loading: false } : prev))
    }
  }, [board, taskId, key])

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
      title: deal.evidence || undefined,
    },
    h('td', { className: 'duty-mono', title: deal.board }, deal.board),
    h('td', { className: 'duty-mono', title: deal.id }, deal.id),
    h('td', { title: deal.titleFull }, deal.title),
    h('td', { title: deal.assignee }, deal.assignee),
    h('td', { className: 'duty-num', title: attemptTitle }, deal.attemptAgeText),
    h('td', { title: leaseTitle }, deal.leaseText),
    h('td', { className: 'duty-num', title: `${deal.lastActionKind}: ${deal.lastActionSummary || '—'}` }, deal.lastActionAgeText),
    h('td', null, h(StateBadge, { label: deal.stateLabel, tone: deal.tone, title: deal.evidence })),
  )
}

function DealGroup({ group, selectedKey, onPick }) {
  return h(
    'section',
    { className: 'duty-group' },
    h(
      'div',
      { className: 'duty-group-h' },
      h(StateBadge, { label: group.label, tone: group.tone, title: group.state }),
      h('span', { className: 'duty-num' }, `${group.count}`),
    ),
    h(
      'table',
      { className: 'duty-table' },
      h(
        'colgroup',
        null,
        h('col', { style: { width: '96px' } }),
        h('col', { style: { width: '104px' } }),
        h('col', null),
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
          h('th', null, 'доска'),
          h('th', null, 'дело'),
          h('th', null, 'заголовок'),
          h('th', null, 'исполнитель'),
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
            key: `${deal.board}/${deal.id}`,
            deal,
            selected: selectedKey === `${deal.board}/${deal.id}`,
            onPick,
          }),
        ),
      ),
    ),
  )
}

/** Скелетон таймлайна: пока грузится, экран не пустой. */
function TimelineSkeleton() {
  return h(
    'div',
    null,
    ...Array.from({ length: 3 }, (_, i) =>
      h(
        'div',
        { className: 'duty-att duty-skel-row', key: `skel-${i}` },
        h('div', { className: 'duty-skel', style: { width: '52%' } }),
        h('div', { className: 'duty-skel', style: { width: '100%', height: '6px' } }),
        h('div', { className: 'duty-skel', style: { width: '78%' } }),
      ),
    ),
  )
}

function AttemptCard({ attempt }) {
  const tone = TONE_COLOR[attempt.outcomeTone] || TONE_COLOR.gray
  return h(
    'div',
    { className: 'duty-att' },
    h(
      'div',
      { className: 'duty-att-h' },
      h('span', { className: 'duty-mono' }, `#${attempt.runId ?? '—'}`),
      h('span', null, attempt.profile),
      h('span', { className: 'duty-num' }, attempt.durationText),
      h('span', { className: 'duty-spacer' }),
      h('span', {
        className: 'duty-badge',
        style: { color: tone },
        title: `status=${attempt.status}${attempt.outcome ? `, outcome=${attempt.outcome}` : ''}`,
      }, attempt.outcome || attempt.status),
    ),
    h('div', { className: 'duty-bar-track' }, h('div', { className: 'duty-bar', style: { width: `${attempt.barPct}%`, color: tone } })),
    attempt.summary ? h('div', { className: 'duty-summary' }, attempt.summary) : null,
    attempt.error ? h('div', { className: 'duty-error' }, attempt.error) : null,
    attempt.events.length
      ? h(
          'ul',
          { className: 'duty-events' },
          ...attempt.events.map((event, i) =>
            h(
              'li',
              { key: `ev-${i}` },
              h('span', { className: 'duty-mono' }, event.clock),
              ' · ',
              event.kind,
              event.summary ? ` — ${event.summary}` : '',
            ),
          ),
        )
      : null,
  )
}

// Неполнота ответа /now одной строкой — общая для жёлтой полосы над списком и
// для пустого состояния, иначе «досок 19/19» врёт про полноту среза. Собирает её
// модель (`noticeLines`), потому что правило «устаревший снимок замечаний не
// имеет» проверяется тестом, а не глазами при ревью рендера.

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
      h('span', { className: 'duty-mono' }, `${deal.board} · ${deal.id}`),
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
          ...model.groups.map((group) =>
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
                ...group.deals.map((deal) => h(WhyCard, { key: `${deal.board}/${deal.id}`, deal, onOpen })),
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
  const entries = Object.entries(stats.totals.runs_by_outcome || {}).sort((a, b) => b[1] - a[1])
  const crashes = new Set(stats.crash_outcomes || [])
  const success = new Set(stats.success_outcomes || [])
  if (!entries.length) return h('div', { className: 'duty-muted' }, 'ранов нет: таксономия считается по task_runs')
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

function BoardHealthTable({ stats }) {
  return h(
    'table',
    { className: 'duty-table duty-health-table' },
    h(
      'thead',
      null,
      h(
        'tr',
        null,
        h('th', null, 'доска'),
        h('th', null, 'задач'),
        h('th', null, 'ранов'),
        h('th', null, 'с первой попытки'),
        h('th', null, 'ожидание p50 / p90'),
        h('th', null, 'неполнота'),
      ),
    ),
    h(
      'tbody',
      null,
      ...stats.boards.map((board) => {
        if (!board.read) {
          return h(
            'tr',
            { key: `bh-${board.slug}` },
            h('td', { className: 'duty-mono' }, board.slug),
            h('td', { colSpan: 5, className: 'duty-muted' }, board.note || 'доска недоступна'),
          )
        }
        const share = board.first_try.share
        const incompleteness = [board.truncated ? 'срез обрезан лимитом' : '', ...(board.warnings || [])]
          .filter(Boolean)
          .join('; ')
        return h(
          'tr',
          { key: `bh-${board.slug}` },
          h('td', { className: 'duty-mono' }, board.slug),
          h('td', { className: 'duty-num' }, `${board.tasks_total}`),
          h('td', { className: 'duty-num' }, `${board.runs_total}`),
          h('td', { className: 'duty-num' }, `${board.first_try.ok}/${board.first_try.with_runs} · ${share === null ? '—' : `${Math.round(share * 100)}%`}`),
          h('td', { className: 'duty-num' }, board.waiting_sec.p50 === null ? '—' : `${fmtAge(board.waiting_sec.p50)} / ${fmtAge(board.waiting_sec.p90)}`),
          h('td', { className: 'duty-muted' }, incompleteness || '—'),
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
  const crashes = new Set(data.crash_outcomes || [])
  const crashRuns = Object.entries(totals.runs_by_outcome || {}).reduce((acc, [key, count]) => (crashes.has(key) ? acc + count : acc), 0)
  const share = totals.first_try.share
  const readBoards = data.boards.filter((board) => board.read).length
  return h(
    'div',
    { className: 'duty-scroll' },
    stats.error ? h('div', { className: 'duty-banner', style: { margin: '0 0 10px' } }, h('span', null, `снимок не обновлён: ${stats.error}`), h(Button, { size: 'xs', variant: 'secondary', onClick: () => void stats.reload() }, 'повторить')) : null,
    data.errors.length
      ? h('div', { className: 'duty-note', style: { margin: '0 0 10px' } }, ...data.errors.map((err, i) => h('div', { key: `he-${i}` }, `источник ${err.board} недоступен: ${err.reason}`)))
      : null,
    data.warnings.length
      ? h('div', { className: 'duty-note', style: { margin: '0 0 10px' } }, ...data.warnings.map((text, i) => h('div', { key: `hw-${i}` }, text)))
      : null,
    h(
      'div',
      { className: 'duty-kpis' },
      h(Kpi, { title: 'дела с первой попытки', value: share === null ? '—' : `${Math.round(share * 100)}%`, sub: `${totals.first_try.ok} из ${totals.first_try.with_runs} дел, у которых были раны`, barPct: share === null ? null : Math.round(share * 100) }),
      h(Kpi, { title: 'ожидание до первой попытки: p50 / p90', value: totals.waiting_sec.p50 === null ? '—' : `${fmtAge(totals.waiting_sec.p50)} / ${fmtAge(totals.waiting_sec.p90)}`, sub: totals.waiting_sec.count ? `p99: ${fmtAge(totals.waiting_sec.p99)} · максимум: ${fmtAge(totals.waiting_sec.max)} · выборка: ${totals.waiting_sec.count} дел (created_at → started_at)` : 'выборки нет: у дел среза нет ни одного рана (created_at → started_at)' }),
      h(Kpi, { title: 'ранов всего', value: `${totals.runs_total}`, sub: `падений: ${crashRuns}${totals.runs_total ? ` · ${Math.round((crashRuns / totals.runs_total) * 100)}%` : ''}` }),
      h(Kpi, { title: 'прочитано досок', value: `${readBoards} / ${data.boards.length}`, sub: `задач в срезе: ${totals.tasks_total}` }),
    ),
    h('div', { className: 'duty-group-h' }, h('span', null, 'таксономия исходов'), h('span', { className: 'duty-mono' }, 'по всем ранам')),
    h(Taxonomy, { stats: data }),
    h('div', { className: 'duty-group-h', style: { marginTop: '14px' } }, h('span', null, 'доски')),
    h(BoardHealthTable, { stats: data }),
    h(
      'div',
      { className: 'duty-muted', style: { marginTop: '10px' } },
      'успех и падение — наборы исходов из ответа бэкенда; «с первой попытки» — первый по времени ран дела уже успешен; p50/p90/p99 — метод ближайшего ранга по объединённой выборке дел (сортировка одна, интерполяции нет)',
    ),
  )
}

/* ---------------- экран «дело» ---------------- */

function DealScreen({ sel, onBack }) {
  const detail = useDeal(sel.board, sel.id)
  const query = useAttempts(sel.board, sel.id)
  const deal = useMemo(() => (detail.data && detail.data.deal ? shapeDeal(detail.data.deal) : null), [detail.data])
  const timeline = useMemo(() => (query.data ? shapeAttempts(query.data) : null), [query.data])

  const head = h(
    'div',
    { className: 'duty-deal-head' },
    h(
      'div',
      { className: 'duty-panel-h' },
      h(
        'div',
        { style: { flex: '1 1 auto', minWidth: 0 } },
        h('div', { className: 'duty-mono' }, `${sel.board} · ${sel.id}`),
        h('div', { className: 'duty-deal-title' }, (detail.data && detail.data.title) || sel.titleFull || '—'),
      ),
      h(Button, { size: 'xs', variant: 'secondary', onClick: onBack }, '← к списку'),
    ),
    deal
      ? h(StateBadge, { label: deal.stateLabel, tone: deal.tone, title: deal.state })
      : detail.data
        ? h(StateBadge, { label: `завершено: ${detail.data.status}`, tone: 'gray', title: detail.data.status })
        : null,
    deal ? h('div', { className: 'duty-evidence' }, deal.evidence || 'доказательство не записано') : null,
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
    body = h(
      'div',
      { className: 'duty-deal-body' },
      h(
        'div',
        { className: 'duty-deal-spec' },
        h('div', { className: 'duty-group-h' }, h('span', null, 'спецификация'), detail.data.body_truncated ? h('span', { className: 'duty-mono' }, 'обрезана лимитом') : null),
        detail.data.body
          ? h('pre', { className: 'duty-spec' }, detail.data.body)
          : h('div', { className: 'duty-muted' }, 'тело дела пустое'),
      ),
      h(
        'div',
        { className: 'duty-deal-attempts' },
        h('div', { className: 'duty-group-h' }, h('span', null, 'попытки'), h('span', { className: 'duty-num' }, `${timeline ? timeline.count : '…'}`)),
        query.error
          ? h('div', { className: 'duty-banner', style: { margin: 0 } }, h('span', null, query.error), h(Button, { size: 'xs', variant: 'secondary', onClick: () => void query.reload() }, 'повторить'))
          : !timeline
            ? h(TimelineSkeleton, null)
            : !timeline.count
              ? h('div', { className: 'duty-muted' }, 'попыток нет: у дела ещё не было ранов')
              : h('div', null, ...timeline.attempts.map((attempt, i) => h(AttemptCard, { key: `att-${attempt.runId ?? i}`, attempt }))),
      ),
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
      `прочитано досок: ${model.boardsRead} из ${model.boardsTotal}`,
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
  const [board, setBoard] = useState('')
  const [states, setStates] = useState(() => new Set())
  const [query, setQuery] = useState('')
  const [effQuery, setEffQuery] = useState('')
  const [sel, setSel] = useState(null)
  const [tick, setTick] = useState(() => Date.now())
  // «Здоровье» запрашивается только когда экран открыт: это скан всех досок.
  const stats = useStats(screen === 'health')

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
    () => (model ? filterNow(model, { board, states, query: effQuery }) : null),
    [model, board, states, effQuery],
  )

  if (!rest) {
    return h('div', { className: 'duty-page' }, h('div', { className: 'duty-banner' }, 'плагин не зарегистрирован'))
  }

  const updatedAgo = snap.updatedAt ? Math.max(0, Math.floor((tick - snap.updatedAt) / 1000)) : null
  const selectedKey = sel ? `${sel.board}/${sel.id}` : ''
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
    setBoard('')
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
    model ? h('span', { className: 'duty-muted' }, `· досок ${model.boardsRead}/${model.boardsTotal}`) : null,
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

  // Недоступные доски, упавшие источники и обрезанные/деградировавшие доски —
  // жёлтой полосой над списком, чтобы неполнота ответа была видна и тогда, когда
  // дела есть (в пустом состоянии те же строки перечисляет EmptyState).
  // Показываем их только по свежему ответу: при `snap.error` экран держит прошлый
  // снимок, и его замечания — не факт о настоящем (иначе «доска _archived
  // недоступна» висит вечно, хотя последний ответ её уже не содержит).
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
            value: board,
            onChange: (e) => setBoard(e.target.value),
            title: 'Фильтр по доске',
          },
          h('option', { value: '' }, 'все доски'),
          ...model.boards.map((item) =>
            h('option', { value: item.slug, key: `board-${item.slug}` }, item.read ? item.slug : `${item.slug} (недоступна)`),
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
              meta.label,
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
      ...view.groups.map((group) => h(DealGroup, { key: `grp-${group.state}`, group, selectedKey, onPick: pick })),
    )
  }

  // Экраны переключаются табами; данные для каждого — свои, из одного и того же
  // бэкенда. «Дело» открывается из списка или из «Почему стоит».
  let content
  if (screen === 'deal' && sel) {
    content = h(DealScreen, {
      key: `${sel.board}/${sel.id}`,
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
          ...view.groups.map((group) => h(DealGroup, { key: `pick-${group.state}`, group, selectedKey, onPick: pick })),
        )
      : h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 4 }, (_, i) => h('div', { className: 'duty-skel', key: `ds-${i}`, style: { width: `${92 - i * 9}%` } })))),
  } else if (screen === 'why') {
    content = model
      ? h(WhyScreen, { model, stale, onOpen: pick })
      : h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 6 }, (_, i) => h('div', { className: 'duty-skel', key: `ws-${i}`, style: { width: `${95 - i * 8}%` } }))))
  } else if (screen === 'health') {
    content = h(HealthScreen, { stats })
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
