/**
 * Hermes Duty — шейпер данных страницы /duty (чистые функции, без React и DOM).
 *
 * Здесь НЕТ классификации состояний: единственный источник таксономии — бэкенд
 * (`WAITING_PRIORITY` и `waiting.state` в `dashboard/plugin_api.py`). Модуль
 * только раскладывает готовый ответ `/now` по группам в порядке приоритета и
 * форматирует числа для показа. `WAITING_ORDER` ниже — порядок отрисовки, а не
 * правило вывода состояния: неизвестное состояние не «доразбирается», а
 * показывается последней группой со своим именем.
 *
 * ВАЖНО: этот файл не импортируется из `desktop/plugin.js`. Загрузчик
 * десктопных плагинов исполняет `plugin.js` как blob-модуль и отказывает любому
 * спекулятору вне `@hermes/plugin-sdk` / `react*` (включая относительные пути),
 * поэтому тело модуля до строки `export {` дословно вшито в `plugin.js` между
 * маркерами `duty:model-start` / `duty:model-end`. Синхронизация —
 * `bin/duty-model-sync`, расхождение ловит `tests/model.test.mjs`.
 */

// Порядок приоритета ожиданий — копия WAITING_PRIORITY бэкенда, только для отрисовки.
const WAITING_ORDER = [
  'wait_human',
  'capability',
  'dead_lease',
  'crashed',
  'working',
  'wait_slot',
  'wait_gate',
]

// Подпись и тон бейджа по состоянию. Тон — ключ палитры TONE_COLOR, не css.
const STATE_META = {
  wait_human: { label: 'ждёт человека', tone: 'yellow' },
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

// Тон исхода попытки: падение — красное, успех — зелёное, отложенное — жёлтое.
const OUTCOME_TONE = {
  crashed: 'red',
  gave_up: 'red',
  spawn_failed: 'red',
  timed_out: 'red',
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
    attemptNote: attempt
      ? pidAlive === false
        ? 'pid мёртв'
        : pidAlive === true
          ? 'pid жив'
          : 'pid не записан'
      : 'нет попытки',
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

/** Группы строк по `waiting.state`: сначала WAITING_ORDER, затем незнакомые состояния. */
function groupDeals(deals) {
  const buckets = new Map(WAITING_ORDER.map((state) => [state, []]))
  const unknown = []
  for (const deal of deals) {
    if (buckets.has(deal.state)) {
      buckets.get(deal.state).push(deal)
      continue
    }
    let bucket = unknown.find((group) => group.state === deal.state)
    if (!bucket) {
      bucket = { state: deal.state || '—', deals: [] }
      unknown.push(bucket)
    }
    bucket.deals.push(deal)
  }
  const groups = []
  for (const state of WAITING_ORDER) {
    const rows = buckets.get(state)
    if (!rows.length) continue
    const meta = stateMeta(state)
    groups.push({ state, label: meta.label, tone: meta.tone, count: rows.length, deals: rows })
  }
  for (const bucket of unknown) {
    const meta = stateMeta(bucket.state)
    groups.push({ state: bucket.state, label: meta.label, tone: meta.tone, count: bucket.deals.length, deals: bucket.deals })
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

/** Тон исхода попытки (падение/успех/отложено) с запасным правилом по статусу рана. */
function outcomeTone(outcome, status) {
  const key = String(outcome || '')
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
      outcomeTone: outcomeTone(attempt.outcome, attempt.status),
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

export {
  WAITING_ORDER,
  STATE_META,
  TONE_COLOR,
  stateMeta,
  fmtAge,
  fmtTitle,
  fmtClock,
  shapeDeal,
  groupDeals,
  shapeNow,
  filterNow,
  outcomeTone,
  shapeAttempts,
}
