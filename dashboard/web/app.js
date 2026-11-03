/* Hermes Duty — веб-версия (0.0.2).
 *
 * Четыре экрана поверх двух API-ответов плагина:
 *   #/now                    «Сейчас»            — GET /now
 *   #/why                    «Почему стоит»      — GET /now (группировка + evidence)
 *   #/health                 «Здоровье»          — GET /stats
 *   #/deal/<board>/<task_id> «Дело»              — GET /deal/{board}/{id} + /duty/.../attempts
 *
 * Шейпер ответов не дублируется: он импортируется из `./model.js`, а бэкенд
 * отдаёт тот же файл, что вшит в десктопный плагин. Таксономия состояний и
 * формулировки доказательств — единственный источник бэкенда (`waiting.state`,
 * `waiting.evidence`), страница их только рисует.
 *
 * Токен берём из `window.__HERMES_SESSION_TOKEN__` (вшит бэкендом в HTML) и шлём
 * заголовком `X-Hermes-Session-Token` ко всем `/api/plugins/hermes-duty/*`.
 */
import {
  filterNow,
  fmtAge,
  fmtClock,
  noticeLines,
  shapeAttempts,
  shapeDeal,
  shapeNow,
  stateMeta,
} from './model.js'

const TOKEN = window.__HERMES_SESSION_TOKEN__ || ''
const BASE = '/api/plugins/hermes-duty'
const HDR = TOKEN ? { 'X-Hermes-Session-Token': TOKEN } : {}
const REFRESH_MS = 10000
const HEALTH_REFRESH_MS = 30000
const TIMEOUT_MS = 15000
const SEARCH_DEBOUNCE_MS = 240

/* ---------------------------------------------------------------- утилиты */

const app = document.getElementById('app')
const stampEl = document.getElementById('stamp')
const verEl = document.getElementById('ver')

function h(tag, props, ...children) {
  const el = document.createElement(tag)
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue
    if (key === 'class') el.className = value
    else if (key === 'text') el.textContent = value
    else if (key === 'html') el.innerHTML = value
    else if (key.startsWith('on')) el.addEventListener(key.slice(2).toLowerCase(), value)
    else if (key === 'dataset') Object.assign(el.dataset, value)
    else el.setAttribute(key, value === true ? '' : value)
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue
    el.append(child instanceof Node ? child : document.createTextNode(String(child)))
  }
  return el
}

const clear = (node) => {
  while (node.firstChild) node.removeChild(node.firstChild)
  return node
}

const pct = (share) => (share === null || share === undefined ? '—' : `${Math.round(share * 100)}%`)

async function api(path) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
  let res
  try {
    res = await fetch(BASE + path, { signal: ctrl.signal, headers: HDR, cache: 'no-store' })
  } catch (err) {
    throw new Error(err.name === 'AbortError' ? `таймаут: ${TIMEOUT_MS / 1000} с` : err.message || 'сеть')
  } finally {
    clearTimeout(timer)
  }
  let data = null
  try {
    data = await res.json()
  } catch {
    /* не JSON — разберём по коду */
  }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`)
  return data || {}
}

/* ------------------------------------------------------------------ бейдж */

function Badge(state) {
  const meta = stateMeta(state)
  return h('span', { class: `badge t-${meta.tone}`, title: state || '' }, h('i'), meta.label)
}

/* ------------------------------------------------------------- состояние */

let route = { name: 'now' }
let timer = null
let stampAt = null
let ticker = null

const snap = { data: null, error: null, loading: false }
const filter = { board: '', states: new Set(), query: '', effQuery: '' }
const health = { data: null, error: null, loading: false }
const dealSnap = { key: '', data: null, attempts: null, error: null, loading: false }

function setStamp(at) {
  stampAt = at
}

function tickStamp() {
  if (!stampAt) {
    stampEl.textContent = ''
    return
  }
  const ago = Math.max(0, Math.floor((Date.now() - stampAt) / 1000))
  stampEl.textContent = ago < 2 ? 'обновлено только что' : `обновлено ${ago} с назад`
}

/* ------------------------------------------------------------ экран «сейчас» */

async function loadNow() {
  snap.loading = true
  try {
    snap.data = await api('/now')
    snap.error = null
    setStamp(Date.now())
  } catch (err) {
    snap.error = err.message
  } finally {
    snap.loading = false
    render()
  }
}

function noticesBlock(lines) {
  if (!lines.length) return null
  return h('div', { class: 'note' }, ...lines.map((line) => h('div', null, line)))
}

function errorBanner(text, retry) {
  return h('div', { class: 'banner' }, h('span', null, text), h('button', { class: 'icon-btn', onclick: retry, title: 'Повторить' }, '⟳'))
}

function dealRow(deal) {
  const attemptTitle =
    deal.attemptAge === null
      ? `попытки нет (${deal.attemptNote})`
      : `попытка ${deal.attemptAgeText} назад, ${deal.attemptNote}`
  const leaseTitle =
    deal.leaseExpiresIn === null
      ? `лизинг: ${deal.leaseState}`
      : `лизинг: ${deal.leaseState}, ${deal.leaseExpiresIn > 0 ? 'ещё' : 'истёк'} ${fmtAge(Math.abs(deal.leaseExpiresIn))}`
  const open = () => {
    location.hash = `#/deal/${encodeURIComponent(deal.board)}/${encodeURIComponent(deal.id)}`
  }
  return h(
    'tr',
    { class: 'row', tabindex: '0', title: deal.evidence || '', onclick: open, onkeydown: (e) => (e.key === 'Enter' ? open() : null) },
    h('td', { class: 'mono' }, deal.board),
    h('td', { class: 'mono' }, deal.id),
    h('td', { class: 'ttl', title: deal.titleFull }, deal.title),
    h('td', null, deal.assignee),
    h('td', { class: 'n', title: attemptTitle }, deal.attemptAgeText),
    h('td', { title: leaseTitle }, deal.leaseText),
    h('td', { class: 'n', title: `${deal.lastActionKind}: ${deal.lastActionSummary || '—'}` }, deal.lastActionAgeText),
    h('td', null, Badge(deal.state)),
  )
}

function groupBlock(group) {
  return h(
    'section',
    { class: 'grp' },
    h('div', { class: 'grp-h' }, Badge(group.state), h('span', { class: 'n' }, `${group.count}`)),
    h(
      'table',
      null,
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
      h('tbody', null, ...group.deals.map(dealRow)),
    ),
  )
}

function filtersBlock(model) {
  const toggle = (state) => {
    if (filter.states.has(state)) filter.states.delete(state)
    else filter.states.add(state)
    render()
  }
  return h(
    'div',
    { class: 'filters' },
    h(
      'select',
      {
        title: 'Фильтр по доске',
        onchange: (e) => {
          filter.board = e.target.value
          render()
        },
      },
      h('option', { value: '' }, 'все доски'),
      ...model.boards.map((board) =>
        h('option', { value: board.slug, selected: filter.board === board.slug }, board.read ? board.slug : `${board.slug} (недоступна)`),
      ),
    ),
    h(
      'div',
      { class: 'chips' },
      h(
        'button',
        {
          class: filter.states.size ? 'chip' : 'chip on',
          onclick: () => {
            filter.states.clear()
            render()
          },
        },
        'все',
        h('span', { class: 'n' }, `${model.count}`),
      ),
      ...model.stateOrder.map((state) => {
        const meta = stateMeta(state)
        return h(
          'button',
          {
            class: `chip t-${meta.tone}${filter.states.has(state) ? ' on' : ''}`,
            title: state,
            onclick: () => toggle(state),
          },
          meta.label,
          h('span', { class: 'n' }, `${model.byState[state] || 0}`),
        )
      }),
    ),
    h('input', {
      type: 'search',
      placeholder: 'поиск: id, заголовок, исполнитель',
      value: filter.query,
      oninput: (e) => {
        filter.query = e.target.value
        clearTimeout(filter.debounce)
        filter.debounce = setTimeout(() => {
          filter.effQuery = filter.query
          render()
        }, SEARCH_DEBOUNCE_MS)
      },
    }),
  )
}

function viewNow() {
  if (!snap.data && snap.error) return errorBanner(`ошибка запроса /now: ${snap.error}`, loadNow)
  if (!snap.data) {
    return h(
      'div',
      { class: 'grp' },
      h(
        'div',
        { class: 'panel-b' },
        ...[0, 1, 2, 3, 4, 5].map((i) => h('div', { class: 'skel', style: `width:${95 - i * 9}%` })),
      ),
    )
  }
  const model = shapeNow(snap.data)
  const view = filterNow(model, filter)
  const stale = Boolean(snap.error)
  const lines = noticeLines(model, stale)
  const parts = []

  if (verEl && !verEl.textContent && snap.data.by_state) verEl.textContent = ''

  if (snap.error) {
    parts.push(
      errorBanner(`снимок от ${fmtClock(model.now)} — обновление не удалось: ${snap.error}`, loadNow),
    )
  }
  if (view.count > 0) {
    const note = noticesBlock(lines)
    if (note) parts.push(note)
  }
  parts.push(filtersBlock(model))

  if (!view.count) {
    parts.push(
      h(
        'div',
        { class: 'empty' },
        h('b', null, model.count > 0 ? 'под фильтры ничего не подошло' : 'нет активных дел'),
        h('span', { class: 'muted' }, `прочитано досок: ${model.boardsRead} из ${model.boardsTotal}`),
        lines.length ? h('ul', null, ...lines.map((line) => h('li', null, line))) : null,
      ),
    )
  } else {
    parts.push(...view.groups.map(groupBlock))
  }
  return h('div', null, ...parts)
}

/* -------------------------------------------------------- экран «почему стоит» */

function whyCard(deal) {
  const open = () => {
    location.hash = `#/deal/${encodeURIComponent(deal.board)}/${encodeURIComponent(deal.id)}`
  }
  return h(
    'article',
    { class: 'why-card' },
    h('h3', null, deal.titleFull || deal.title),
    h(
      'div',
      { class: 'why-meta' },
      Badge(deal.state),
      h('span', { class: 'mono' }, `${deal.board} · ${deal.id}`),
      h('span', { class: 'muted' }, deal.assignee),
    ),
    h('div', { class: 'why-evidence', text: deal.evidence || 'доказательство не записано' }),
    h(
      'div',
      { class: 'why-foot' },
      h('span', null, `попытка: ${deal.attemptAgeText} (${deal.attemptNote})`),
      h('span', null, `действие: ${deal.lastActionAgeText} назад`),
      h('a', { href: `#/deal/${encodeURIComponent(deal.board)}/${encodeURIComponent(deal.id)}`, onclick: open }, 'открыть дело →'),
    ),
  )
}

function viewWhy() {
  if (!snap.data && snap.error) return errorBanner(`ошибка запроса /now: ${snap.error}`, loadNow)
  if (!snap.data) return h('div', { class: 'empty' }, h('b', null, 'загрузка…'))

  const model = shapeNow(snap.data)
  const stale = Boolean(snap.error)
  const parts = []
  if (snap.error) parts.push(errorBanner(`снимок от ${fmtClock(model.now)} — обновление не удалось: ${snap.error}`, loadNow))
  const note = noticesBlock(noticeLines(model, stale))
  if (note) parts.push(note)

  parts.push(
    h(
      'div',
      { class: 'note' },
      h(
        'div',
        null,
        'Здесь только то, что бэкенд назвал сам: группа — это состояние ожидания, текст под заголовком — его доказательство (факты из лизинга, pid, heartbeat, графа зависимостей и пула вопросов). Страница ничего не досчитывает.',
      ),
    ),
  )

  if (!model.count) {
    parts.push(h('div', { class: 'empty' }, h('b', null, 'нечего объяснять: активных дел нет')))
    return h('div', null, ...parts)
  }

  for (const group of model.groups) {
    parts.push(
      h('div', { class: 'sec-h' }, h('h2', null, group.label), h('span', { class: 'num' }, `${group.count} · ${group.state}`)),
    )
    parts.push(h('div', { class: 'why-grid' }, ...group.deals.map(whyCard)))
  }
  return h('div', null, ...parts)
}

/* ----------------------------------------------------------- экран «здоровье» */

async function loadHealth() {
  health.loading = true
  try {
    health.data = await api('/stats')
    health.error = null
    setStamp(Date.now())
  } catch (err) {
    health.error = err.message
  } finally {
    health.loading = false
    render()
  }
}

function kpi(title, value, sub, barPct) {
  return h(
    'div',
    { class: 'kpi' },
    h('div', { class: 'k' }, title),
    h('div', { class: 'v' }, value),
    h('div', { class: 's' }, sub),
    barPct === null || barPct === undefined ? null : h('div', { class: 'kpi-bar' }, h('i', { style: `width:${barPct}%` })),
  )
}

function taxonomy(stats) {
  const totals = stats.totals
  const entries = Object.entries(totals.runs_by_outcome || {}).sort((a, b) => b[1] - a[1])
  if (!entries.length) return h('div', { class: 'empty' }, h('b', null, 'ранов нет'), h('span', { class: 'muted' }, 'таксономия исходов считается по task_runs'))
  const max = entries[0][1] || 1
  const crashes = new Set(stats.crash_outcomes || [])
  const success = new Set(stats.success_outcomes || [])
  return h(
    'div',
    { class: 'tax' },
    ...entries.map(([key, count]) => {
      const tone = crashes.has(key) ? 'crash' : success.has(key) ? 'ok' : key === 'blocked' ? 'warn' : ''
      return h(
        'div',
        { class: `tax-row ${tone}` },
        h('span', { class: 'name', title: key }, key),
        h('div', { class: 'tbar' }, h('i', { style: `width:${Math.max(1, Math.round((count / max) * 100))}%` })),
        h('span', { class: 'tnum' }, `${count}`),
      )
    }),
  )
}

function boardTable(stats) {
  const rows = stats.boards.map((board) => {
    if (!board.read) {
      return h(
        'tr',
        null,
        h('td', { class: 'mono' }, board.slug),
        h('td', { colspan: '5', class: 'muted' }, board.note || 'доска недоступна'),
      )
    }
    const share = board.first_try.share
    return h(
      'tr',
      null,
      h('td', { class: 'mono' }, board.slug),
      h('td', { class: 'n' }, `${board.tasks_total}`),
      h('td', { class: 'n' }, `${board.runs_total}`),
      h('td', { class: 'n' }, `${board.first_try.ok}/${board.first_try.with_runs} · ${pct(share)}`),
      h('td', { class: 'n' }, board.waiting_sec.p50 === null ? '—' : `${fmtAge(board.waiting_sec.p50)} / ${fmtAge(board.waiting_sec.p90)}`),
      h(
        'td',
        { class: 'muted' },
        [board.truncated ? 'срез обрезан лимитом' : '', ...(board.warnings || [])].filter(Boolean).join('; ') || '—',
      ),
    )
  })
  return h(
    'div',
    { class: 'panel' },
    h('div', { class: 'panel-h' }, 'по доскам'),
    h(
      'table',
      null,
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
      h('tbody', null, ...rows),
    ),
  )
}

function viewHealth() {
  if (!health.data && health.error) return errorBanner(`ошибка запроса /stats: ${health.error}`, loadHealth)
  if (!health.data) return h('div', { class: 'empty' }, h('b', null, 'считаю здоровье…'))

  const stats = health.data
  const totals = stats.totals
  const crashes = new Set(stats.crash_outcomes || [])
  const crashRuns = Object.entries(totals.runs_by_outcome || {})
    .filter(([key]) => crashes.has(key))
    .reduce((acc, [, count]) => acc + count, 0)
  const crashShare = totals.runs_total ? crashRuns / totals.runs_total : null
  const readBoards = stats.boards.filter((board) => board.read).length

  const parts = []
  if (health.error) parts.push(errorBanner(`обновление не удалось: ${health.error}`, loadHealth))
  if (stats.errors.length) {
    parts.push(noticesBlock(stats.errors.map((err) => `источник ${err.board} недоступен: ${err.reason}`)))
  }
  if (stats.warnings && stats.warnings.length) parts.push(noticesBlock(stats.warnings))

  parts.push(
    h(
      'div',
      { class: 'kpis' },
      kpi(
        'дела с первой попытки',
        pct(totals.first_try.share),
        `${totals.first_try.ok} из ${totals.first_try.with_runs} дел, у которых были раны`,
        totals.first_try.share === null ? null : Math.round(totals.first_try.share * 100),
      ),
      kpi(
        'ожидание до первой попытки: p50 / p90',
        totals.waiting_sec.p50 === null ? '—' : `${fmtAge(totals.waiting_sec.p50)} / ${fmtAge(totals.waiting_sec.p90)}`,
        totals.waiting_sec.count
          ? `p99: ${fmtAge(totals.waiting_sec.p99)} · максимум: ${fmtAge(totals.waiting_sec.max)} · выборка: ${totals.waiting_sec.count} дел (created_at → started_at)`
          : 'выборки нет: у дел среза нет ни одного рана (created_at → started_at)',
      ),
      kpi('ранов всего', `${totals.runs_total}`, `падений: ${crashRuns} · ${pct(crashShare)}`),
      kpi('прочитано досок', `${readBoards} / ${stats.boards.length}`, `задач в срезе: ${totals.tasks_total}`),
    ),
  )

  parts.push(h('div', { class: 'sec-h' }, h('h2', null, 'таксономия исходов'), h('span', { class: 'num' }, 'по всем ранам')))
  parts.push(h('div', { class: 'panel' }, h('div', { class: 'panel-b' }, taxonomy(stats))))

  parts.push(h('div', { class: 'sec-h' }, h('h2', null, 'доски')))
  parts.push(boardTable(stats))

  parts.push(
    h(
      'div',
      { class: 'note' },
      h(
        'div',
        null,
        'Как это считается: успех попытки — исход рана из набора успеха (передаётся бэкендом), падение — из набора crash. «С первой попытки» — у дела первый по времени ран уже успешен. p50/p90/p99 ожидания — метод ближайшего ранга (позиция ceil(p/100 · n) в возрастающем ряду, без интерполяции) по объединённой выборке; обрезанный срез доски помечен в колонке «неполнота» и в предупреждениях выше, чтобы доля и распределение не выглядели полными.',
      ),
    ),
  )
  return h('div', null, ...parts)
}

/* ------------------------------------------------------------- экран «дело» */

async function loadDeal(board, taskId) {
  const key = `${board}/${taskId}`
  if (dealSnap.key !== key) {
    dealSnap.key = key
    dealSnap.data = null
    dealSnap.attempts = null
    dealSnap.error = null
  }
  dealSnap.loading = true
  try {
    const [detail, attempts] = await Promise.all([
      api(`/deal/${encodeURIComponent(board)}/${encodeURIComponent(taskId)}`),
      api(`/duty/${encodeURIComponent(board)}/${encodeURIComponent(taskId)}/attempts`),
    ])
    if (dealSnap.key === key) {
      dealSnap.data = detail
      dealSnap.attempts = attempts
      dealSnap.error = null
      setStamp(Date.now())
    }
  } catch (err) {
    if (dealSnap.key === key) dealSnap.error = err.message
  } finally {
    dealSnap.loading = false
    render()
  }
}

function attemptCard(attempt) {
  return h(
    'div',
    { class: 'att' },
    h(
      'div',
      { class: 'att-h' },
      h('span', { class: 'mono' }, `#${attempt.runId ?? '—'}`),
      h('span', { class: 'muted' }, attempt.profile),
      h('span', { class: 'tnum' }, attempt.durationText),
      h('span', { class: 'sp' }),
      h('span', { class: `badge t-${attempt.outcomeTone}`, title: `status=${attempt.status}${attempt.outcome ? `, outcome=${attempt.outcome}` : ''}` }, attempt.outcome || attempt.status),
    ),
    h('div', { class: `track t-${attempt.outcomeTone}` }, h('div', { class: 'bar', style: `width:${attempt.barPct}%` })),
    attempt.summary ? h('div', { class: 'att-summary' }, attempt.summary) : null,
    attempt.error ? h('div', { class: 'att-error' }, attempt.error) : null,
    attempt.events.length
      ? h(
          'ul',
          { class: 'events' },
          ...attempt.events.map((event) =>
            h('li', null, h('span', { class: 'mono' }, event.clock), ' · ', event.kind, event.summary ? ` — ${event.summary}` : ''),
          ),
        )
      : null,
  )
}

function viewDeal(board, taskId) {
  if (dealSnap.error && !dealSnap.data) {
    return h(
      'div',
      null,
      errorBanner(dealSnap.error, () => loadDeal(board, taskId)),
      h('a', { href: '#/now' }, '← к списку дел'),
    )
  }
  if (!dealSnap.data) {
    return h('div', { class: 'grp' }, h('div', { class: 'panel-b' }, h('div', { class: 'skel', style: 'width:60%' }), h('div', { class: 'skel', style: 'width:90%' })))
  }

  const data = dealSnap.data
  // `deal` в ответе — сырые факты (attempt/lease/last_action/waiting); подписи и
  // «pid жив» собирает общий шейпер, чтобы экран «Дело» и таблица «Сейчас»
  // говорили об одном и том же одними словами.
  const deal = data.deal ? shapeDeal(data.deal) : null
  const timeline = dealSnap.attempts ? shapeAttempts(dealSnap.attempts) : null

  const head = h(
    'div',
    { class: 'deal-head' },
    h('div', { class: 'mono muted' }, `${data.board} · ${data.id}`),
    h('h1', null, data.title || '—'),
    h(
      'div',
      { class: 'why-meta' },
      deal ? Badge(deal.state) : h('span', { class: 'badge t-gray' }, h('i'), `завершено: ${data.status}`),
    ),
    h(
      'div',
      { class: 'deal-facts' },
      h('span', null, 'статус: ', h('b', null, data.status || '—')),
      h('span', null, 'исполнитель: ', h('b', null, data.assignee || '—')),
      deal ? h('span', null, 'попытка: ', h('b', null, `${deal.attemptAgeText} (${deal.attemptNote})`)) : null,
      deal ? h('span', null, 'лизинг: ', h('b', null, `${deal.leaseText}`)) : null,
      h('span', null, 'создано: ', h('b', null, fmtClock(data.created_at))),
      deal ? h('span', null, 'последнее действие: ', h('b', null, `${deal.lastActionAgeText} назад`)) : null,
    ),
    deal && deal.evidence ? h('div', { class: 'why-evidence', style: 'margin-top:10px', text: deal.evidence }) : null,
  )

  const left = h(
    'div',
    { class: 'panel' },
    h('div', { class: 'panel-h' }, `спецификация${data.body_truncated ? ' (обрезана лимитом)' : ''}`),
    h('div', { class: 'panel-b' }, data.body ? h('pre', { class: 'body' }, data.body) : h('div', { class: 'muted' }, 'тело дела пустое')),
  )

  let attemptsBody
  if (!timeline) attemptsBody = h('div', { class: 'panel-b' }, h('div', { class: 'skel', style: 'width:70%' }))
  else if (!timeline.count) attemptsBody = h('div', { class: 'panel-b muted' }, 'попыток нет: у дела ещё не было ранов')
  else attemptsBody = h('div', null, ...timeline.attempts.map(attemptCard))

  const right = h('div', { class: 'panel' }, h('div', { class: 'panel-h' }, `попытки · ${timeline ? timeline.count : '…'}`), attemptsBody)

  return h(
    'div',
    null,
    h('div', { style: 'margin-bottom:10px' }, h('a', { href: '#/now' }, '← к списку дел')),
    head,
    h('div', { class: 'two-col' }, left, right),
  )
}

/* ---------------------------------------------------------------- маршруты */

function parseRoute() {
  const raw = (location.hash || '#/now').replace(/^#/, '')
  const parts = raw.split('/').filter(Boolean)
  if (parts[0] === 'deal' && parts[1] && parts[2]) {
    return { name: 'deal', board: decodeURIComponent(parts[1]), id: decodeURIComponent(parts[2]) }
  }
  if (parts[0] === 'why') return { name: 'why' }
  if (parts[0] === 'health') return { name: 'health' }
  return { name: 'now' }
}

function render() {
  const body = clear(app)
  if (route.name === 'now') body.append(viewNow())
  else if (route.name === 'why') body.append(viewWhy())
  else if (route.name === 'health') body.append(viewHealth())
  else body.append(viewDeal(route.board, route.id))

  for (const link of document.querySelectorAll('#tabs a')) {
    link.classList.toggle('on', link.dataset.tab === route.name)
  }
}

function schedule() {
  clearTimeout(timer)
  const ms = route.name === 'health' ? HEALTH_REFRESH_MS : REFRESH_MS
  timer = setTimeout(run, ms)
}

function run() {
  if (route.name === 'now') void loadNow()
  else if (route.name === 'why') void loadNow()
  else if (route.name === 'health') void loadHealth()
  else void loadDeal(route.board, route.id)
}

function onRoute() {
  route = parseRoute()
  // Данные, которых нет для нового экрана, не переиспользуем: иначе «Здоровье»
  // покажет старый снимок «сейчас» под своими KPI.
  if (route.name === 'health' && !health.data) {
    stampAt = null
    tickStamp()
  }
  render()
  run()
  schedule()
}

window.addEventListener('hashchange', onRoute)
document.getElementById('refresh').addEventListener('click', () => run())

// Версия плагина — из /health: одна правда о сборке, вшитая в шапку.
async function loadVersion() {
  try {
    const info = await api('/health')
    if (verEl) verEl.textContent = info.version ? `v${info.version}` : ''
  } catch {
    /* шапка без версии — не повод ломать экран */
  }
}

ticker = setInterval(tickStamp, 1000)
tickStamp()
void loadVersion()
onRoute()
