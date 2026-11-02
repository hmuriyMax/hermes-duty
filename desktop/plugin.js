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
const SEARCH_DEBOUNCE_MS = 240
const REQUEST_TIMEOUT_MS = 20_000

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
const STATE_META = {
  wait_human: { label: 'ждёт ответа', tone: 'yellow' },
  capability: { label: 'нужен доступ', tone: 'purple' },
  dead_lease: { label: 'лизинг истёк', tone: 'red' },
  crashed: { label: 'упало', tone: 'red' },
  working: { label: 'работает', tone: 'green' },
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
    boardsTotal: boards.length,
    boardsRead: boards.filter((board) => board.read).length,
    count: deals.length,
    byState,
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
  return { ...model, deals, groups: groupDeals(deals), count: deals.length, byState }
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

/** Правая панель: таймлайн попыток выбранного дела. */
function AttemptsPanel({ sel, onClose }) {
  const query = useAttempts(sel?.board, sel?.id)
  const timeline = useMemo(() => (query.data ? shapeAttempts(query.data) : null), [query.data])

  let body
  if (query.error) {
    body = h(
      'div',
      null,
      h(
        'div',
        { className: 'duty-banner', style: { margin: '0 0 8px' } },
        h('span', null, query.error),
        h(Button, { size: 'xs', variant: 'secondary', onClick: () => void query.reload() }, 'повторить'),
      ),
    )
  } else if (query.loading && !timeline) {
    body = h(TimelineSkeleton, null)
  } else if (!timeline || !timeline.count) {
    body = h('div', { className: 'duty-muted' }, 'попыток нет: у дела ещё не было ранов')
  } else {
    body = h('div', null, ...timeline.attempts.map((attempt, i) => h(AttemptCard, { key: `att-${attempt.runId ?? i}`, attempt })))
  }

  return h(
    'aside',
    { className: 'duty-detail' },
    h(
      'div',
      { className: 'duty-panel-h' },
      h(
        'div',
        { style: { flex: '1 1 auto', minWidth: 0 } },
        h('div', { className: 'duty-mono' }, `${sel.board} · ${sel.id}`),
        h('div', { className: 'duty-panel-title' }, sel.titleFull || '—'),
        h(
          'div',
          { style: { marginTop: '4px' } },
          h(StateBadge, { label: sel.stateLabel, tone: sel.tone, title: sel.state }),
        ),
      ),
      h(
        'button',
        { type: 'button', className: 'duty-icon-btn', title: 'Закрыть панель', onClick: onClose },
        h(Codicon, { name: 'close', size: '0.8rem' }),
      ),
    ),
    sel.evidence ? h('div', { className: 'duty-evidence' }, sel.evidence) : null,
    h('div', { className: 'duty-group-h' }, h('span', null, 'попытки')),
    body,
  )
}

function EmptyState({ view, model, onReset }) {
  const filtered = model.count > 0 && view.count === 0
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
    model.unreachable.length || model.otherErrors.length
      ? h(
          'ul',
          null,
          ...model.unreachable.map((bad) =>
            h('li', { key: `bad-${bad.board}` }, `доска ${bad.board} недоступна: ${bad.reason}`),
          ),
          ...model.otherErrors.map((bad) =>
            h('li', { key: `other-${bad.board}` }, `источник ${bad.board} недоступен: ${bad.reason}`),
          ),
        )
      : null,
  )
}

function DutyPage() {
  const snap = useNow()
  const [board, setBoard] = useState('')
  const [states, setStates] = useState(() => new Set())
  const [query, setQuery] = useState('')
  const [effQuery, setEffQuery] = useState('')
  const [sel, setSel] = useState(null)
  const [tick, setTick] = useState(() => Date.now())

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
  const pick = (deal) => setSel(sel && selectedKey === `${deal.board}/${deal.id}` ? null : deal)
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
    model ? h('span', { className: 'duty-muted duty-num' }, `сейчас: ${view.count}`) : null,
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
        h('span', null, `ошибка запроса /now: ${snap.error}`),
        h(Button, { size: 'xs', variant: 'secondary', onClick: () => void snap.refresh() }, 'повторить'),
      )
    : null

  // Недоступные доски и упавшие источники — жёлтой полосой над списком, чтобы
  // неполнота ответа была видна и тогда, когда дела есть (в пустом состоянии те же
  // строки перечисляет EmptyState).
  const noticeLines = model
    ? [
        ...model.unreachable.map((bad) => `доска ${bad.board} недоступна: ${bad.reason}`),
        ...model.otherErrors.map((bad) => `источник ${bad.board} недоступен: ${bad.reason}`),
      ]
    : []
  const notices = noticeLines.length && view && view.count > 0
    ? h(
        'div',
        { className: 'duty-note' },
        ...noticeLines.map((line, i) => h('div', { key: `notice-${i}` }, line)),
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
    list = h('div', { className: 'duty-list' }, h(EmptyState, { view, model, onReset: resetFilters }))
  } else {
    list = h(
      'div',
      { className: 'duty-list' },
      ...view.groups.map((group) => h(DealGroup, { key: `grp-${group.state}`, group, selectedKey, onPick: pick })),
    )
  }

  return h(
    'div',
    { className: 'duty-page' },
    header,
    banner,
    notices,
    filters,
    h('div', { className: 'duty-body' }, list, sel ? h(AttemptsPanel, { sel, onClose: () => setSel(null) }) : null),
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
