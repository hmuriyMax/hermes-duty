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

export {
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
  noticeLines,
}
