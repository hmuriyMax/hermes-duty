/* Hermes Duty — веб-версия (0.0.7).
 *
 * Пять экранов поверх трёх API-ответов плагина:
 *   #/now             «Сейчас»            — GET /now
 *   #/deals           «Завершённые»       — GET /deals
 *   #/why             «Почему стоит»      — GET /now (группировка + evidence)
 *   #/health          «Здоровье»          — GET /stats
 *   #/deal/<deal_id>  «Дело»              — GET /deal/{deal_id} + /duty/{deal_id}/attempts
 *
 * Дело адресуется ТОЛЬКО своим id `d_<hex>` (0.0.6: доски нет). Прежний адрес
 * `#/deal/<board>/<id>` — отказ: страница называет, что доски больше нет, вместо
 * того чтобы открыть полусломанную карточку. Список дел группируется по `repo`,
 * дела без репозитория — отдельной группой и последней.
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
  ALL_REPOS,
  WITHOUT_REPO_LABEL,
  attemptView,
  laneView,
  filterNow,
  fmtAge,
  fmtClock,
  groupStatesText,
  humanLine,
  noticeLines,
  plural,
  repoLabel,
  repoShort,
  fileLabel,
  docBlocks,
  finishedStatusMeta,
  shapeAttempts,
  shapeDeal,
  shapeDealTime,
  shapeFinished,
  shapeNow,
  shapeStages,
  compactStageView,
  compactAttemptView,
  stateLabel,
  stateMeta,
  TOOL_LEGEND,
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
const humanEl = document.getElementById('human')

// Элементы с этой осью — не HTML: `createElement('svg')` создаёт элемент в
// HTML-namespace, и браузер его не рисует вовсе. Ошибка не видна в разметке, и
// именно поэтому список назван явно, а не выводится из имени тега.
const SVG_NS = 'http://www.w3.org/2000/svg'
const SVG_TAGS = new Set(['svg', 'path', 'circle', 'rect', 'line', 'polyline', 'g', 'use', 'text', 'title'])

function h(tag, props, ...children) {
  const el = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag)
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue
    // `className` у SVGElement только для чтения: присваивание молча падает
    // исключением и роняет всю отрисовку, поэтому класс ставится атрибутом.
    if (key === 'class') el.setAttribute('class', value)
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

/* Markdown для тела дела. Библиотека вендорена (`vendor/marked.min.js`): страница
 * живёт в прослойке без сети, поэтому брать её с CDN нельзя. Рендер идёт через
 * `template`, из которого вычищается всё исполняемое (`script`/`style`/`iframe`,
 * атрибуты `on*`, `javascript:`-ссылки): тело дела пишет человек или воркер, но
 * страница не должна выполнять чужой код.
 */
function mdNode(text, fallbackClass) {
  const marked = window.marked
  if (!marked || typeof marked.parse !== 'function' || !text) {
    return h('pre', { class: fallbackClass || 'body' }, text || '')
  }
  const tpl = document.createElement('template')
  tpl.innerHTML = marked.parse(text, { gfm: true, breaks: false })
  for (const bad of tpl.content.querySelectorAll('script, style, iframe, object, embed, link, meta')) bad.remove()
  for (const el of tpl.content.querySelectorAll('*')) {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase()
      const value = String(attr.value || '').toLowerCase()
      if (name.startsWith('on') || (name === 'href' && value.startsWith('javascript:'))) el.removeAttribute(attr.name)
    }
  }
  const box = h('div', { class: 'md' })
  box.append(tpl.content)
  return box
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

function Badge(state, humanKind, label) {
  const meta = stateMeta(state, humanKind)
  return h('span', { class: `badge t-${meta.tone}`, title: state || '' }, h('i'), label || meta.label)
}

/* ------------------------------------------------------------- состояние */

let route = { name: 'now' }
let timer = null
let stampAt = null
let ticker = null

const snap = { data: null, error: null, loading: false }
const filter = { repo: ALL_REPOS, states: new Set(), query: '', effQuery: '' }
const health = { data: null, error: null, loading: false }
const finished = { data: null, error: null, loading: false }
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
  // Адрес дела — только его id (0.0.6): доски в маршруте больше нет.
  const open = () => {
    location.hash = `#/deal/${encodeURIComponent(deal.id)}`
  }
  return h(
    'tr',
    {
      class: 'row',
      tabindex: '0',
      title: [deal.repoLabel, deal.evidence].filter(Boolean).join(' · '),
      onclick: open,
      onkeydown: (e) => (e.key === 'Enter' ? open() : null),
    },
    h('td', { class: 'mono' }, deal.id),
    h('td', { class: 'ttl', title: deal.titleFull }, deal.title),
    h('td', null, deal.assignee),
    // Этап и номер попытки из журнала (`/now.stage`): «что сейчас» видно в
    // строке, без открытия дела. Прочерк — у дела ещё не было попытки.
    h('td', { class: 'mono muted', title: deal.stageText || 'этап не начат' }, deal.stageText || '—'),
    h('td', { class: 'n', title: attemptTitle }, deal.attemptAgeText),
    h('td', { title: leaseTitle }, deal.leaseText),
    h('td', { class: 'n', title: `${deal.lastActionKind}: ${deal.lastActionSummary || '—'}` }, deal.lastActionAgeText),
    h('td', null, Badge(deal.state, deal.humanKind)),
  )
}

/**
 * Группа списка — репозиторий (0.0.6). Заголовок называет репозиторий и число
 * дел в нём; состояния группы подписаны одной строкой, чтобы «ждёт ответа» не
 * терялось при смене группировки. Столбца «репозиторий» в строках нет — он был бы
 * повтором заголовка группы.
 */
function groupBlock(group) {
  return h(
    'section',
    { class: 'grp' },
    h(
      'div',
      { class: 'grp-h' },
      h('span', { class: group.repo ? 'grp-k mono' : 'grp-k mono muted', title: group.repo || 'репозиторий не назван' }, group.label),
      h('span', { class: 'n' }, `${group.count}`),
      h('span', { class: 'grp-s muted' }, groupStatesText(group)),
    ),
    h(
      'table',
      null,
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
      h('tbody', null, ...group.deals.map(dealRow)),
    ),
  )
}

/**
 * Фильтр по репозиторию вместо доски: `ALL_REPOS` — все дела, пустое значение —
 * ровно дела без репозитория (так же их отличает роут: `?repo=`). Список
 * репозиториев — состав store из `/now.repos`, а не только видимый срез: иначе
 * фильтр по репозиторию с одними закрытыми делами было бы не выбрать.
 */
function filtersBlock(model) {
  const toggle = (state) => {
    if (filter.states.has(state)) filter.states.delete(state)
    else filter.states.add(state)
    render()
  }
  const options = [h('option', { value: ALL_REPOS, selected: filter.repo === ALL_REPOS }, 'все репозитории')]
  if (model.withoutRepo > 0) {
    options.push(
      h('option', { value: '', selected: filter.repo === '' }, `${WITHOUT_REPO_LABEL} (${model.withoutRepo})`),
    )
  }
  for (const item of model.repos.list) {
    options.push(
      h('option', { value: item.repo, selected: filter.repo === item.repo }, `${item.repo} (${item.deals})`),
    )
  }
  return h(
    'div',
    { class: 'filters' },
    h(
      'select',
      {
        title: 'Фильтр по репозиторию',
        onchange: (e) => {
          filter.repo = e.target.value
          render()
        },
      },
      ...options,
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
        // Подпись чипа — та же, что у бейджа: «ждёт ответа» стоит только там,
        // где в этом состоянии действительно есть вопрос (видимые дела).
        return h(
          'button',
          {
            class: `chip t-${meta.tone}${filter.states.has(state) ? ' on' : ''}`,
            title: state,
            onclick: () => toggle(state),
          },
          stateLabel(state, model.deals),
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

  // «N вопросов + M блокировок» по тому, что сейчас на экране (с фильтрами):
  // вопрос — на него можно ответить, блокировка — нужен человек без вопроса.
  if (humanEl) {
    humanEl.textContent = view.count ? humanLine(view.humanCounts) : ''
    humanEl.title = view.count
      ? `ждёт человека: ${humanLine(view.humanCounts)}`
      : 'никто не ждёт человека'
  }

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
        h(
          'span',
          { class: 'muted' },
          `репозиториев в store: ${model.repoCount} · дел без репозитория: ${model.withoutRepo}`,
        ),
        lines.length ? h('ul', null, ...lines.map((line) => h('li', null, line))) : null,
      ),
    )
  } else {
    parts.push(...view.groups.map(groupBlock))
  }
  return h('div', null, ...parts)
}

/* ----------------------------------------------------- экран «Завершённые» */

// Список закрытых дел — единственное место, где видно, что уже сделано: `/now`
// отвечает только про активные. Данные — `GET /deals` дословно: причина, этапы,
// попытки, вопросы, длительность и публикация приходят с бэкенда, страница их не
// пересчитывает (`shapeFinished`, `desktop/model.js`).

async function loadFinished() {
  finished.loading = true
  try {
    finished.data = await api('/deals')
    finished.error = null
    setStamp(Date.now())
  } catch (err) {
    finished.error = err.message
  } finally {
    finished.loading = false
    render()
  }
}

/** Бейдж терминального статуса: у закрытого дела нет состояния ожидания. */
function finishedBadge(deal) {
  return h('span', { class: `badge t-${deal.tone}`, title: deal.status }, h('i'), deal.statusLabel)
}

function finishedRow(deal) {
  const open = () => {
    location.hash = `#/deal/${encodeURIComponent(deal.id)}`
  }
  const pubTitle = deal.published ? deal.published.mrUrl || deal.published.ref || 'факт журнала' : 'публикации нет'
  return h(
    'tr',
    {
      class: 'row',
      tabindex: '0',
      title: `${deal.repoLabel} · ${deal.causeText}`,
      onclick: open,
      onkeydown: (e) => (e.key === 'Enter' ? open() : null),
    },
    h('td', { class: 'mono' }, deal.finishedText),
    h('td', { class: 'mono', title: deal.id }, deal.id),
    h('td', { class: 'ttl', title: deal.titleFull }, deal.title),
    h('td', null, finishedBadge(deal)),
    h('td', { class: 'n', title: 'попыток в журнале' }, `${deal.attempts}`),
    h('td', { class: 'n', title: 'вопросы человеку по журналу' }, deal.questions.text),
    h('td', { class: 'n', title: 'длительность дела: события журнала и попытки' }, deal.durationText),
    h('td', { title: deal.causeText }, deal.causeText),
    h('td', { class: 'muted', title: deal.stagesText }, deal.stagesText),
    h('td', { class: 'muted', title: pubTitle }, deal.published ? deal.published.text : '—'),
  )
}

/** Группа «Завершённых» — репозиторий: самый свежий закрытый первым. */
function finishedGroupBlock(group) {
  return h(
    'section',
    { class: 'grp' },
    h(
      'div',
      { class: 'grp-h' },
      h('span', { class: group.repo ? 'grp-k mono' : 'grp-k mono muted', title: group.repo || 'репозиторий не назван' }, group.label),
      h('span', { class: 'n' }, `${group.count}`),
      h('span', { class: 'grp-s muted' }, `свежее закрытие: ${group.deals[0].finishedText}`),
    ),
    h(
      'table',
      null,
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
      h('tbody', null, ...group.deals.map(finishedRow)),
    ),
  )
}

function viewFinished() {
  if (!finished.data && finished.error) {
    return errorBanner(`ошибка запроса /deals: ${finished.error}`, loadFinished)
  }
  if (!finished.data) {
    return h(
      'div',
      { class: 'grp' },
      h(
        'div',
        { class: 'panel-b' },
        ...[0, 1, 2, 3].map((i) => h('div', { class: 'skel', style: `width:${92 - i * 10}%` })),
      ),
    )
  }
  const model = shapeFinished(finished.data)
  const parts = []
  if (finished.error) {
    parts.push(
      errorBanner(`снимок от ${fmtClock(model.now)} — обновление не удалось: ${finished.error}`, loadFinished),
    )
  }
  if (model.errors.length) {
    parts.push(noticesBlock(model.errors.map((err) => `источник ${err.source} недоступен: ${err.reason}`)))
  }
  parts.push(
    h(
      'div',
      { class: 'note' },
      h(
        'div',
        null,
        'Здесь только завершённые дела — закрытые и архивные. Числа строки (попытки, вопросы, длительность) — те же факты журнала, что в карточке дела: открыть её можно по id. Карточки kanban — отдельное наблюдение, не дела.',
      ),
    ),
  )
  parts.push(
    h(
      'div',
      { class: 'muted' },
      `дел: ${model.count} из ${model.total} · статусы: ${model.statuses.join(', ') || '—'}`,
    ),
  )
  if (model.truncated) {
    parts.push(noticesBlock([`показаны не все: лимит ${model.limit}, всего ${model.total} — список режется лимитом /deals`]))
  }
  if (!model.count) {
    parts.push(
      h(
        'div',
        { class: 'empty' },
        h('b', null, 'завершённых дел нет'),
        h('span', { class: 'muted' }, model.note || 'в store нет ни одного закрытого дела'),
      ),
    )
    return h('div', null, ...parts)
  }
  parts.push(...model.groups.map(finishedGroupBlock))
  return h('div', null, ...parts)
}

/* -------------------------------------------------------- экран «почему стоит» */

function whyCard(deal) {
  const open = () => {
    location.hash = `#/deal/${encodeURIComponent(deal.id)}`
  }
  return h(
    'article',
    { class: 'why-card' },
    h('h3', null, deal.titleFull || deal.title),
    h(
      'div',
      { class: 'why-meta' },
      Badge(deal.state, deal.humanKind),
      h('span', { class: 'mono' }, `${deal.repoLabel} · ${deal.id}`),
      h('span', { class: 'muted' }, deal.assignee),
    ),
    h('div', { class: 'why-evidence', text: deal.evidence || 'доказательство не записано' }),
    h(
      'div',
      { class: 'why-foot' },
      h('span', null, `попытка: ${deal.attemptAgeText} (${deal.attemptNote})`),
      h('span', null, `действие: ${deal.lastActionAgeText} назад`),
      h('a', { href: `#/deal/${encodeURIComponent(deal.id)}`, onclick: open }, 'открыть дело →'),
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

  for (const group of model.stateGroups) {
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
  const entries = Object.entries(totals.attempts_by_outcome || {}).sort((a, b) => b[1] - a[1])
  if (!entries.length) return h('div', { class: 'empty' }, h('b', null, 'попыток нет'), h('span', { class: 'muted' }, 'таксономия считается по журналу дел'))
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

/**
 * Таблица «по репозиториям»: те же группы и числа, что печатает `bin/duty stats`.
 *
 * Источник — `repos[]` и `without_repo` ответа `/stats`: доски в статистике не
 * участвуют (0.0.6), «без репозитория» — законная строка, а не пропуск.
 */
function repoTable(stats) {
  const groups = [...(stats.repos || []), ...(stats.without_repo && stats.without_repo.deals ? [stats.without_repo] : [])]
  const rows = groups.map((group) => {
    const share = group.first_try.share
    return h(
      'tr',
      null,
      h('td', { class: 'mono' }, group.repo || WITHOUT_REPO_LABEL),
      h('td', { class: 'n' }, `${group.deals}`),
      h('td', { class: 'n' }, `${group.attempts_total}`),
      h('td', { class: 'n' }, `${group.first_try.ok}/${group.first_try.closed} · ${pct(share)}`),
      h('td', { class: 'n' }, group.waiting_sec.p50 === null ? '—' : `${fmtAge(group.waiting_sec.p50)} / ${fmtAge(group.waiting_sec.p90)}`),
      h('td', { class: 'n' }, group.waiting_sec.count ? `${group.waiting_sec.count}` : '—'),
      h('td', { class: 'muted' }, '—'),
    )
  })
  return h(
    'div',
    { class: 'panel' },
    h('div', { class: 'panel-h' }, 'по репозиториям'),
    groups.length
      ? h(
          'table',
          null,
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
              h('th', null, 'неполнота'),
            ),
          ),
          h('tbody', null, ...rows),
        )
      : h('div', { class: 'panel-b muted' }, 'дел в store нет: считать нечего'),
  )
}

function viewHealth() {
  if (!health.data && health.error) return errorBanner(`ошибка запроса /stats: ${health.error}`, loadHealth)
  if (!health.data) return h('div', { class: 'empty' }, h('b', null, 'считаю здоровье…'))

  const stats = health.data
  const totals = stats.totals
  const repos = stats.repos || []
  const without = stats.without_repo || null
  // Падения — по наборам исходов бэкенда, а не по своему списку.
  const crashes = new Set(stats.crash_outcomes || [])
  const outcomeEntries = Object.entries(totals.attempts_by_outcome || {})
  const crashRuns = outcomeEntries
    .filter(([key]) => crashes.has(key))
    .reduce((acc, [, count]) => acc + count, 0)

  const parts = []
  if (health.error) parts.push(errorBanner(`обновление не удалось: ${health.error}`, loadHealth))
  if (stats.errors.length) {
    parts.push(noticesBlock(stats.errors.map((err) => `источник ${err.source} недоступен: ${err.reason}`)))
  }
  if (stats.warnings && stats.warnings.length) parts.push(noticesBlock(stats.warnings))

  // Окно — часть числа, а не украшение: «за сутки» и «за всё время» — разные метрики.
  const win = stats.window || {}
  parts.push(
    h(
      'div',
      { class: 'muted' },
      `окно: ${win.source === 'default' ? 'сутки по умолчанию' : 'задано вручную'} (${fmtAge(win.sec)}) · числа считаются по журналу дел; карточки kanban — отдельное наблюдение`,
    ),
  )

  parts.push(
    h(
      'div',
      { class: 'kpis' },
      kpi(
        'дела с первой попытки',
        pct(totals.first_try.share),
        `${totals.first_try.ok} из ${totals.first_try.closed} этапов, закрытых попытками`,
        totals.first_try.share === null ? null : Math.round(totals.first_try.share * 100),
      ),
      kpi(
        'ожидание до первой попытки: p50 / p90',
        totals.waiting_sec.p50 === null ? '—' : `${fmtAge(totals.waiting_sec.p50)} / ${fmtAge(totals.waiting_sec.p90)}`,
        totals.waiting_sec.count
          ? `p99: ${fmtAge(totals.waiting_sec.p99)} · максимум: ${fmtAge(totals.waiting_sec.max)} · выборка: ${totals.waiting_sec.count} дел (created_at → started_at)`
          : 'выборки нет: у дел среза нет ни одного рана (created_at → started_at)',
      ),
      kpi('попыток всего', `${totals.attempts_total}`, `падений: ${crashRuns}${totals.attempts_total ? ` · ${pct(crashRuns / totals.attempts_total)}` : ''}`),
      kpi(
        'репозиториев',
        `${repos.length}`,
        `дел в store: ${totals.deals} · без репозитория: ${without ? without.deals : 0}`,
      ),
    ),
  )

  parts.push(h('div', { class: 'sec-h' }, h('h2', null, 'таксономия исходов попыток'), h('span', { class: 'num' }, 'по журналу дел')))
  parts.push(h('div', { class: 'panel' }, h('div', { class: 'panel-b' }, taxonomy(stats))))

  parts.push(h('div', { class: 'sec-h' }, h('h2', null, 'репозитории')))
  parts.push(repoTable(stats))

  parts.push(
    h(
      'div',
      { class: 'note' },
      h(
        'div',
        null,
        'Как это считается: успех попытки — исход из набора успеха (передаётся бэкендом), падение — из набора crash. «С первой попытки» — этап журнала закрыт первой же попыткой и успешно. p50/p90/p99 ожидания — метод ближайшего ранга (позиция ceil(p/100 · n) в возрастающем ряду, без интерполяции) по объединённой выборке дел репозитория. Группа — `repo` строки дела; доски в статистике не участвуют.',
      ),
    ),
  )
  return h('div', null, ...parts)
}

/* ------------------------------------------------------------- экран «дело» */

// «Дело» — полноценный экран: таб открывает последнее открытое дело, а если
// такого нет — список активных дел, из которого видно, куда идти. Запоминаем в
// localStorage: переключение табов не должно терять контекст. Адрес дела — его id
// (0.0.6): доски в ключе больше нет, и старый ключ `board/id` при чтении просто не
// совпадёт с форматом — покажется «последнего нет», а не битая ссылка.
const LAST_DEAL = 'duty:last-deal'

function rememberDeal(dealId) {
  try {
    localStorage.setItem(LAST_DEAL, String(dealId))
  } catch {
    /* приватный режим / выключенное хранилище — не повод ломать экран */
  }
}

function lastDeal() {
  let raw = ''
  try {
    raw = localStorage.getItem(LAST_DEAL) || ''
  } catch {
    return null
  }
  // Прежний ключ был `board/id` — два сегмента; такой адрес больше не открыть.
  if (!raw || raw.includes('/')) return null
  return { id: raw }
}

async function loadDeal(dealId) {
  rememberDeal(dealId)
  const key = String(dealId)
  if (dealSnap.key !== key) {
    dealSnap.key = key
    dealSnap.data = null
    dealSnap.attempts = null
    dealSnap.error = null
  }
  dealSnap.loading = true
  try {
    const [detail, attempts] = await Promise.all([
      api(`/deal/${encodeURIComponent(dealId)}`),
      api(`/duty/${encodeURIComponent(dealId)}/attempts`),
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

/**
 * «Дело» без выбранного дела: последнее открытое плюс активные дела списком.
 * Экран обязан быть достижим сам по себе — иначе он существует только как
 * побочный эффект клика по строке, и его «нет» на самом деле «не видно».
 */
function viewDealPicker() {
  const model = snap.data ? shapeNow(snap.data) : null
  const last = lastDeal()
  const parts = [h('div', { class: 'sec-h' }, h('h2', null, 'дело'), h('span', { class: 'num' }, 'открыть дело'))]
  if (last) {
    parts.push(
      h(
        'div',
        { class: 'note' },
        h('div', null, 'последнее открытое: '),
        h('a', { href: `#/deal/${encodeURIComponent(last.id)}` }, last.id),
      ),
    )
  }
  if (!model) {
    parts.push(h('div', { class: 'panel' }, h('div', { class: 'panel-b' }, h('div', { class: 'skel', style: 'width:60%' }))))
    return h('div', null, ...parts)
  }
  parts.push(...(model.groups.length ? model.groups.map(groupBlock) : [h('div', { class: 'empty' }, 'активных дел нет')]))
  return h('div', null, ...parts)
}

/**
 * Отказ по старому адресу дела: `#/deal/<board>/<id>`.
 *
 * Молча открыть дело по второму сегменту значило бы оставить в закладках адрес,
 * которого нет; показать половину карточки — выдать отказ за успех. Поэтому экран
 * называет, что доски больше нет, и даёт ссылку на id из прежнего адреса.
 */
function viewDealBadAddress(board, dealId) {
  // Форма id — та же, что у бэкенда (`deals.is_own`: `^d_[0-9a-f]{6,}$`): адрес,
  // который бэкенд заведомо не примет, не должен превращаться в ссылку.
  const id = dealId && /^d_[0-9a-f]{6,}$/.test(dealId) ? dealId : ''
  return h(
    'div',
    { class: 'empty' },
    h('b', null, 'так дело больше не адресуется'),
    h(
      'span',
      { class: 'muted' },
      `в адресе ${board}/${dealId} первая часть — доска, а досок с 0.0.6 нет: дело называется своим id d_<hex>.`,
    ),
    id
      ? h('a', { href: `#/deal/${encodeURIComponent(id)}` }, `открыть ${id} →`)
      : h('span', { class: 'muted' }, 'id дела выглядит как d_<hex> — возьмите его из списка.'),
    h('a', { href: '#/deal' }, '← к списку дел'),
    h('a', { href: '#/now' }, '← к срезу «сейчас»'),
  )
}

/* ------------------------------- столбец этапов и попыток (0.0.8) */

// Раскрытие строк — состояние поверхности, а не факт дела: шейпер отдаёт только
// умолчание (`openByDefault` — провал и живая попытка раскрыты без клика), а что
// человек открыл или свернул руками, помнит экран. Ключи включают id дела: имена
// этапов у двух дел совпадают, а раскрытия — нет.
const stageOpen = new Map()
const attOpen = new Map()

/** Раскрыта ли строка: ручное решение поверх умолчания шейпера. */
const openNow = (overrides, key, byDefault) => (overrides.has(key) ? overrides.get(key) : byDefault)

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

/** Доказательство строкой: код выхода тоном, обрезанная команда, время. */
function attEvNode(row) {
  return h(
    'div',
    { class: 'att-ev' },
    // Полная команда — в подсказке: обрезка сделана в шейпере, а не CSS-эллипсисом.
    h('span', { class: `badge t-${row.tone}` }, `exit ${row.exitText}`),
    h('span', { class: 'mono att-ev-cmd', title: row.cmdTitle }, row.cmdShort),
    h('span', { class: 'muted' }, row.clock),
  )
}

/**
 * Попытка столбца: число, длительность, исход словами, глиф раскрытия.
 *
 * Раскрытая показывает доказательства, потом `summary` и `error`, потом журнал —
 * сводкой по видам (`5 прогрессов · 1 старт · 1 конец`), а не списком событий:
 * у одной попытки их бывает триста, и в столбце 320 px это лог, а не строка.
 */
function attRowNodes(stage, bar, key, open, rebuild) {
  const view = compactAttemptView(stage, bar)
  const nodes = [
    h(
      'div',
      {
        class: open ? 'att-row open' : 'att-row',
        title: view.outcomeText,
        'aria-expanded': open ? 'true' : 'false',
        onclick: () => {
          attOpen.set(key, !open)
          rebuild()
        },
      },
      h('span', { class: 'mono att-row-n' }, view.label),
      h('span', { class: 'att-row-dur' }, view.durationText),
      h('span', { class: `att-row-out t-${view.tone}` }, view.outcomeText),
      h('span', { class: 'sp' }),
      h('span', { class: 'att-row-tog' }, open ? '▾' : '▸'),
    ),
  ]
  if (!open) return nodes
  for (const row of view.evidenceRows) nodes.push(attEvNode(row))
  if (view.summary) nodes.push(h('div', { class: 'att-summary' }, view.summary))
  if (view.error) nodes.push(h('div', { class: 'att-error' }, view.error))
  if (view.eventsText) nodes.push(h('div', { class: 'att-log' }, view.eventsText))
  return nodes
}

/**
 * Этап столбца: одна строка (~26 px) плюс попытки, если раскрыт.
 *
 * Служебное (`needs`) живёт в подсказке строки: отдельной строкой «после: …» оно
 * съедало высоту, которую занимают попытки. Ожидание вопроса — наоборот, видимой
 * строкой: дело может стоять на человеке, и это не деталь для подсказки.
 */
function stageBlockNode(dealId, stage, timeline, rebuild) {
  const view = compactStageView(stage)
  const stageKey = `${dealId}:${view.id}`
  const open = openNow(stageOpen, stageKey, view.openByDefault)
  const bars = stageColumnBars(stage, timeline)
  const nodes = [
    h(
      'div',
      {
        class: open ? 'stage-row open' : 'stage-row',
        title: view.needsText || view.toggleTitle,
        'aria-expanded': open ? 'true' : 'false',
        onclick: () => {
          stageOpen.set(stageKey, !open)
          rebuild()
        },
      },
      h('span', { class: 'mono stage-row-id' }, view.id),
      h('span', { class: 'stage-row-kind muted' }, view.kindText),
      h('span', { class: 'stage-row-att' }, view.attemptsText),
      h('span', { class: 'sp' }),
      h('span', { class: `badge t-${view.tone}`, title: view.stateLabel }, h('i'), view.stateLabel),
      h('span', { class: 'stage-row-tog' }, open ? '▾' : '▸'),
    ),
  ]
  if (view.waitText) {
    nodes.push(
      h(
        'div',
        { class: 'stage-wait' },
        view.waitHref ? h('a', { href: view.waitHref }, view.waitText) : view.waitText,
      ),
    )
  }
  if (!open) return h('div', { class: 'stage-block' }, ...nodes)
  if (!bars.length) nodes.push(h('div', { class: 'att-none muted' }, 'попыток нет'))
  bars.forEach((bar, index) => {
    const attKey = `${stageKey}#${index}`
    nodes.push(...attRowNodes(stage, bar, attKey, openNow(attOpen, attKey, compactAttemptView(stage, bar).openByDefault), rebuild))
  })
  return h('div', { class: 'stage-block' }, ...nodes)
}

/**
 * Столбец этапов и попыток в карточке дела.
 *
 * Одна панель вместо двух (широкой ленты этапов и отдельной панели попыток):
 * шапка попытки повторялась в обеих, и страница росла на три с половиной экрана.
 * Пустой граф печатает причину (`stages_note` бэкенда: нет спеки, спека не
 * читается) — «этапов нет» и «спеки нет» разные факты.
 */
function stagesColumn(dealId, data, timeline) {
  const feed = shapeStages(data)
  const host = h('div', { class: 'stages-list' })
  const rebuild = () => {
    clear(host)
    if (feed.empty) {
      host.append(h('div', { class: 'panel-b muted' }, feed.note || 'этапов нет'))
      return
    }
    host.append(...feed.stages.map((stage) => stageBlockNode(dealId, stage, timeline, rebuild)))
  }
  rebuild()
  return h(
    'div',
    { class: 'panel deal-stages' },
    h('div', { class: 'panel-h' }, feed.empty ? 'этапы' : `этапы · ${feed.count}`),
    // Путь к спеке — именем файла, полный путь в подсказке.
    feed.spec ? h('div', { class: 'stages-spec mono muted', title: feed.spec }, fileLabel(feed.spec)) : null,
    host,
  )
}

/* -------------------------------------------- шкала времени дела (E2) */

// Глиф маркера — только значок; слово и время приходят из шейпера (`label`,
// `title`), чтобы подпись не разъезжалась между вебом и десктопом.
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

/**
 * Строка фактов шапки дела. Числа целиком из `shapeDealTime`: попытки по этапам,
 * длительность (у живого дела — до «сейчас», с пометкой), вопросы человеку,
 * бюджет дела и факт публикации.
 */
function dealFactSpans(scale) {
  const facts = scale.facts
  // Числа, которые уже стоят в KPI (попытки, длительность), здесь не повторяются:
  // одна цифра в двух местах читается как два разных факта.
  const parts = [
    h('span', { title: 'вопросы человеку по таймлайну' }, 'вопросы: ', h('b', null, facts.questions.text)),
  ]
  if (facts.budget) {
    parts.push(
      h(
        'span',
        { title: facts.budget.exhausted ? `потолок исчерпан: ${facts.budget.exhausted}` : 'потолок дела целиком' },
        'бюджет: ',
        h('b', null, facts.budget.text || '—'),
      ),
    )
  }
  if (facts.published) {
    parts.push(
      h(
        'span',
        { title: facts.published.mrUrl || facts.published.ref || 'факт журнала' },
        'публикация: ',
        h('b', { class: 'mono' }, facts.published.text || '—'),
      ),
    )
  }
  if (facts.closed) {
    parts.push(h('span', { title: facts.closed.cause || '' }, 'закрытие: ', h('b', null, facts.closed.text)))
  }
  return parts
}

/** Маркер события: засечка старта/закрытия попытки или заметная точка события. */
function markerNode(marker, index) {
  return h(
    'span',
    {
      class: `scale-marker t-${marker.tone}${marker.tick ? ' tick' : ''}`,
      style: `left:${marker.pct}%;top:${(index % 2) * 13}px`,
      title: marker.title,
      'aria-label': marker.ariaLabel,
      role: 'img',
      tabindex: '0',
    },
    h('span', { class: 'scale-marker-dot' }, MARKER_GLYPH[marker.type] || ''),
  )
}

/** Легенда маркеров: только те виды, что реально есть в журнале этого дела. */
function markerLegend(scale) {
  const seen = []
  for (const marker of scale.markers) {
    if (marker.tick || seen.some((item) => item.type === marker.type)) continue
    seen.push(marker)
  }
  if (!seen.length) return null
  return h(
    'div',
    { class: 'scale-legend' },
    ...seen.map((marker) =>
      h('span', { class: `t-${marker.tone}` }, `${MARKER_GLYPH[marker.type] || '•'} ${marker.label}`),
    ),
    // Про засечки старта/конца здесь молчим: их больше нет, длительность
    // показывает фон попытки, а факты — палки-маркеры.

  )
}

/* ------------------------------------- таймлайн вызовов внутри попытки (0.0.8) */

// Палитра — единственное, что остаётся своего у поверхности: тон вызова
// приходит из шейпера ключом (`read`/`write`/`run`/`net`/`other`), а во что он
// красится, знает только свой CSS.
// Тон вызова приходит из шейпера ключом; здесь — только палитра поверхности.
// Поиск отделён от чтения: в деле их сотни, и одним цветом карта вызовов
// читалась как «всё одно и то же».
const TONE_VAR = {
  read: 'var(--accent)',
  search: 'var(--purple)',
  write: 'var(--ok)',
  run: 'var(--warn)',
  net: 'var(--danger)',
  other: 'var(--text-3)',
}
function toneVar(tone) {
  return TONE_VAR[tone] || TONE_VAR.other
}

/**
 * Строка попытки: фон-полоса на всю длительность, чипы вызовов и маркеры внутри.
 *
 * Границы попытки показывает фон, поэтому засечек старта/конца больше нет, а
 * время начала, конца и длительность живут в подсказке строки — они не спорят с
 * чипами за место на дорожке.
 */
function attemptRowNode(view) {
  const label = h(
    'div',
    { class: 'tl-lbl', 'data-tip': view.bandTip || view.title || '' },
    h('span', { class: 'tl-n' }, view.label.number),
    h('span', { class: `tl-st t-${view.label.tone}` }, view.label.status),
    // Надбавка «не всё показано» — в подписи, а не поверх дорожки: она не
    // должна перекрывать чипы.
    view.badge ? h('span', { class: 'tl-more', title: view.note || view.title }, view.badge) : null,
  )
  const track = h('div', { class: 'tl-track' })
  if (view.band) {
    track.append(
      h('span', {
        class: `tl-band t-${view.band.tone || 'grey'}`,
        style: `left:${view.band.leftPct}%;width:${view.band.widthPct}%`,
        'data-tip': view.bandTip,
      }),
    )
  }
  // Пауза «вопрос → ответ» — подсветка времени, которое дело стояло на человеке.
  for (const wait of view.waits) {
    track.append(
      h('span', {
        class: 'tl-wait',
        style: `left:${wait.fromPct}%;width:${wait.widthPct}%`,
        'data-tip': wait.title,
      }),
    )
  }
  for (const chip of view.chips) {
    track.append(
      h('span', {
        class: `tl-call${chip.unfinished ? ' tl-unfinished' : ''}`,
        'data-level': String(chip.level || 0),
        'data-tone': chip.tone,
        style: chip.unfinished
          ? `left:${chip.leftPct}%;top:${chip.top}px;width:${chip.widthPct}%;color:${toneVar(chip.tone)}`
          : `left:${chip.leftPct}%;top:${chip.top}px;width:${chip.widthPct}%;background:${toneVar(chip.tone)}`,
        'data-tip': chip.tip,
        role: 'button',
        tabindex: '-1',
        'aria-label': chip.tip,
        'data-call-id': chip.id,
      }),
    )
  }
  if (view.marks.length) {
    const marks = h('div', { class: 'tl-marks' })
    for (const mark of view.marks) {
      marks.append(
        h(
          'span',
          {
            class: `tl-mark t-${mark.tone}`,
            style: `left:${mark.pct}%`,
            'data-tip': mark.title,
            'aria-label': mark.ariaLabel,
          },
          h('i'),
        ),
      )
    }
    track.append(marks)
  }
  return h(
    'div',
    // Свёрнутая высота показывает первый уровень вызовов и маркеры, полная
    // раскрывается под указателем. Числа — из шейпера, CSS только переключает.
    {
      class: `tl-row${view.failed ? ' is-failed' : ''}`,
      style: `--tl-collapsed:${view.collapsed}px;--tl-full:${view.full}px`,
    },
    label,
    track,
  )
}

/** Дорожка этапа: имя, значок вида, состояние и строки попыток. */
function laneNode(lane, bars, scale) {
  const view = laneView(lane, bars, scale)
  const head = h(
    'div',
    { class: 'tl-lane-h' },
    h('span', { class: 'tl-lane-name', title: view.name }, view.name),
    view.kind
      ? h(
          'span',
          {
            class: `tl-kind tl-kind-${view.kind}`,
            title: view.kind === 'agent' ? `профиль: ${view.kindText}` : view.kind,
          },
          h(
            'svg',
            { class: 'tl-kind-ico', viewBox: '0 0 16 16', 'aria-hidden': 'true' },
            h('path', { d: view.icon }),
          ),
          view.kindText,
        )
      : null,
    h('span', { class: 'sp' }),
    h('span', { class: 'tl-lane-meta' }, view.meta),
  )
  return h(
    'div',
    { class: 'tl-lane' },
    head,
    view.empty
      ? h('div', { class: 'tl-row-empty' }, 'попыток не было')
      : h('div', { class: 'tl-rows' }, ...view.rows.map((row) => attemptRowNode(row.view))),
  )
}

// Подсветка семейства инструментов: набор тонов, выбранных в легенде. Клик по
// семейству гасит всё остальное, повторный — снимает подсветку. Состояние
// принадлежит экрану (как выбор вызова), а цвета и подписи даёт шейпер.
const dutyToolFilter = new Set()

// Применить фильтр к уже нарисованной панели: чипы чужих семейств гаснут,
// легенда показывает, какие семейства выбраны.
function applyToolFilter(panel) {
  const active = dutyToolFilter.size > 0
  for (const key of panel.querySelectorAll('.scale-key')) {
    const on = dutyToolFilter.has(key.getAttribute('data-tone'))
    key.classList.toggle('on', on)
    key.setAttribute('aria-pressed', on ? 'true' : 'false')
  }
  for (const chip of panel.querySelectorAll('.tl-call')) {
    const dim = active && !dutyToolFilter.has(chip.getAttribute('data-tone'))
    chip.classList.toggle('tl-dim', dim)
    // Погашенный чип не должен ловить клик и подсказку: он «не участвует».
    if (dim) chip.setAttribute('aria-hidden', 'true')
    else chip.removeAttribute('aria-hidden')
  }
}

// Выбранный вызов и его данные: выбор живёт на экране «Дело» и переживает
// перерисовку панели. Ключ — `stage:attempt:seq` из шейпера, индекс собирается
// при отрисовке шкалы: у поверхности нет своего знания о вызовах.
let dutySelectedCallId = ''
const dutyCallIndex = new Map()

/** Выбранный вызов или `null`; индекс наполняет `scalePanel`. */
function selectedCall() {
  return dutySelectedCallId ? dutyCallIndex.get(dutySelectedCallId) || null : null
}

/** KPI-строка дела: пять чисел шапки, все — из шейпера (`scale.kpi`). */
function kpiStrip(scale) {
  return h(
    'div',
    { class: 'duty-kpi' },
    ...scale.kpi.map((item) =>
      h(
        'div',
        { class: 'duty-kpi-cell', title: item.title || '' },
        h('span', { class: 'duty-kpi-v' }, item.value),
        h('span', { class: 'duty-kpi-l' }, item.label),
        item.note ? h('span', { class: 'duty-kpi-n' }, item.note) : null,
      ),
    ),
  )
}

/**
 * Инспектор вызова: что за инструмент, когда начался, сколько шёл.
 *
 * Экран показывает факты вызова, а не «открывает» попытку второй раз: строка
 * попытки остаётся свёрнутой, развёртка живёт только под указателем.
 */
function inspectorNode(call) {
  if (!call) {
    return h('div', { class: 'tl-insp empty' }, 'выбери вызов на дорожке — покажу инструмент, начало и длительность')
  }
  return h(
    'div',
    { class: 'tl-insp' },
    h('span', { class: `tl-insp-dot t-${call.tone}` }),
    h('b', null, call.name),
    h('span', { class: 'mono muted' }, call.clock),
    h('span', null, call.msText),
    h('span', { class: 'muted' }, `${call.stage} · попытка ${call.attempt}${call.seq === null ? '' : ` · вызов ${call.seq}`}`),
  )
}

/**
 * Граф спеки компактной полосой: мелкие узлы и рёбра `needs`.
 *
 * Это схема зависимостей, а не главный объект экрана: крупные прямоугольники
 * занимали пол-экрана и ничего не сообщали. Координаты считает шейпер
 * (`scale.spec`), поверхности только рисуют.
 */
function specStrip(spec) {
  if (!spec) return null
  const pos = new Map(spec.nodes.map((node) => [node.id, node]))
  const edges = spec.edges
    .map((edge) => {
      const from = pos.get(edge.from)
      const to = pos.get(edge.to)
      if (!from || !to) return null
      const x1 = from.x + spec.nodeW
      const y1 = from.y + spec.nodeH / 2
      const x2 = to.x
      const y2 = to.y + spec.nodeH / 2
      return h('path', { class: 'spec-edge', d: `M${x1},${y1} L${x2},${y2}` })
    })
    .filter(Boolean)
  return h(
    'div',
    { class: 'spec-strip' },
    h('span', { class: 'spec-strip-name' }, 'граф спеки'),
    h(
      'svg',
      {
        class: 'spec-graph',
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
          { class: `spec-node t-${node.tone}${node.fails ? ' has-fail' : ''}` },
          h(
            'title',
            null,
            `${node.id} · ${node.label}${node.failsText ? ` · ${node.failsText}` : ''}${node.needsText ? ` · ${node.needsText}` : ''}`,
          ),
          h('rect', { x: node.x, y: node.y, width: spec.nodeW, height: spec.nodeH, rx: 4 }),
          h('text', { x: node.x + spec.nodeW / 2, y: node.y + spec.nodeH / 2 + 3, class: 'spec-id', 'text-anchor': 'middle' }, node.id),
        ),
      ),
    ),
  )
}

/**
 * Панель шкалы времени: подписанная ось, дорожка на этап, полосы попыток и лента
 * маркеров. Геометрия приходит посчитанной (`shapeDealTime`); здесь только DOM.
 */
function scalePanel(scale) {
  // Индекс вызовов: чип и инспектор читают один и тот же вид (`attemptView`).
  dutyCallIndex.clear()
  for (const bar of scale.bars) {
    for (const chip of attemptView(bar, scale).chips) dutyCallIndex.set(chip.id, chip)
  }
  if (scale.empty) {
    return h(
      'div',
      { class: 'panel', style: 'margin-bottom:14px' },
      h('div', { class: 'panel-h' }, 'шкала времени'),
      h('div', { class: 'panel-b muted' }, 'событий и попыток нет: рисовать нечего'),
    )
  }
  const panel = h(
    'div',
    { class: 'panel', style: 'margin-bottom:14px' },
    h('div', { class: 'panel-h' }, `шкала времени · ${scale.axis.text}`),
    h(
      'div',
      { class: 'panel-b' },
      // Ось стоит ровно над дорожками: слева пустая колонка шириной подписей,
      // справа — та же область, где рисуются чипы.
      h(
        'div',
        { class: 'scale-wrap' },
        h(
        'div',
        { class: 'scale-main' },
        h(
        'div',
        { class: 'scale-head' },
        h('div', { class: 'scale-head-gap' }),
        h(
          'div',
          { class: 'scale-axis' },
          // Линейка: подпись у каждой метки, у краёв — словами «нач./кон.».
          ...scale.axis.ticks.map((tick) =>
            h(
              'span',
              { class: `scale-tick align-${tick.align}`, style: `left:${tick.pct}%`, title: tick.clock },
              tick.edge ? `${tick.edge} ${tick.clock}` : tick.clock,
            ),
          ),
        ),
      ),
      // Дорожки этапов — таймлайн вызовов: полоса на попытку, раскрывается по
      // наведению. Полосы `scale-bar` остались в шейпере (desktop 0.0.7), но
      // второй рисунок того же дела на экране не нужен.
      // Одна нулевая линия на все дорожки: часы читаются по сетке, а не
      // угадываются по трём подписям на оси (как в макете `d-merge`).
      h(
        'div',
        { class: 'scale-plot' },
        h(
          'div',
          { class: 'scale-grid', 'aria-hidden': 'true' },
          ...scale.axis.ticks.map((tick) =>
            h('span', { class: 'scale-grid-line', style: `left:${tick.pct}%` }),
          ),
        ),
        h(
        'div',
        { class: 'tl-lanes' },
        ...scale.lanes.map((lane, index) =>
          laneNode(
            lane,
            scale.bars.filter((bar) => bar.lane === index),
            scale,
          ),
        ),
        ),
      ),
        ),
        // Легенда семейств — правой колонкой: числа вызовов из шейпера
        // (`scale.tools`), а не пять слов из константы.
        h(
          'div',
          { class: 'scale-side' },
          h('span', { class: 'scale-keys-h' }, 'инструменты'),
          ...scale.tools.map((item) =>
            h(
              'span',
              {
                class: 'scale-key',
                'data-tone': item.tone,
                role: 'button',
                tabindex: '0',
                'aria-pressed': 'false',
                'aria-label': `${item.label}: ${item.count}. Клик — подсветить вызовы этого семейства на дорожках`,
              },
              h('span', { class: `scale-key-dot t-${item.tone}` }),
              h('span', { class: 'scale-key-l' }, item.label),
              h('span', { class: 'scale-key-n' }, String(item.count)),
            ),
          ),
        ),
      ),
      h('div', { class: 'tl-insp-host' }, inspectorNode(selectedCall(scale))),
      // Легенда маркеров: что означают точки внутри строк попыток.
      markerLegend(scale),
      scale.facts.attempts.journal ? h('div', { class: 'muted' }, 'полосы собраны из журнала: ответ попыток пуст') : null,
    ),
  )
  // Фильтр, выбранный в легенде, переживает перерисовку данных: экран
  // пересобирается по новому кадру, а подсветка остаётся там, где её оставили.
  applyToolFilter(panel)

  // Легенда семейств — переключатели: клик подсвечивает семейство на дорожках,
  // повторный снимает. Клавиатура ходит теми же шагами (Enter/Space).
  const toggleTool = (key) => {
    const tone = key.getAttribute('data-tone') || ''
    if (!tone) return
    if (dutyToolFilter.has(tone)) dutyToolFilter.delete(tone)
    else dutyToolFilter.add(tone)
    applyToolFilter(panel)
  }
  panel.addEventListener('click', (event) => {
    const key = event.target.closest && event.target.closest('.scale-key')
    if (!key) return
    event.preventDefault()
    toggleTool(key)
  })
  panel.addEventListener('keydown', (event) => {
    const key = event.target.closest && event.target.closest('.scale-key')
    if (!key || (event.key !== 'Enter' && event.key !== ' ')) return
    event.preventDefault()
    toggleTool(key)
  })

  // Клик по вызову выбирает его, но не разворачивает строку попытки: развёртка
  // живёт под указателем, а выбор — в инспекторе под шкалой.
  panel.addEventListener('click', (event) => {
    const chip = event.target.closest && event.target.closest('.tl-call')
    const host = panel.querySelector('.tl-insp-host')
    if (!chip || !host || chip.classList.contains('tl-dim')) return
    const id = chip.getAttribute('data-call-id') || ''
    dutySelectedCallId = dutySelectedCallId === id ? '' : id
    for (const el of panel.querySelectorAll('.tl-call.tl-sel')) el.classList.remove('tl-sel')
    if (dutySelectedCallId) chip.classList.add('tl-sel')
    host.replaceChildren(inspectorNode(selectedCall()))
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur()
  })
  return panel
}

function viewDeal(dealId) {
  if (dealSnap.error && !dealSnap.data) {
    return h(
      'div',
      null,
      errorBanner(dealSnap.error, () => loadDeal(dealId)),
      h('a', { href: '#/deal' }, '← к списку дел'),
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
  // Шкала времени: ось, дорожки этапов, полосы и маркеры — из одного шейпера
  // (`shapeDealTime`), того же, что вшит в десктопный плагин.
  const scale = shapeDealTime(data, dealSnap.attempts)

  // Заголовок: слева repo/id, имя дела и граф этапов (список того, что прошло,
  // и того, где падало), справа — статус. Статус раньше стоял в левой колонке и
  // отбирал место у графа, а сам граф висел отдельной панелью внизу.
  const head = h(
    'div',
    { class: 'deal-head' },
    h(
      'div',
      { class: 'deal-head-top' },
      h(
        'div',
        { class: 'deal-head-main' },
        h('div', { class: 'mono muted', title: repoLabel(data.repo) }, `${repoShort(data.repo)} · ${data.id}`),
        h('h1', null, data.title || '—'),
        specStrip(scale.spec),
      ),
      h(
        'div',
        { class: 'deal-head-side' },
        // Закрытое дело: статус словами (`завершено`/`в архиве`), а не значением store.
        deal ? Badge(deal.state, deal.humanKind) : h('span', { class: 'badge t-gray' }, h('i'), finishedStatusMeta(data.status).label),
      ),
    ),
    h(
      'div',
      { class: 'deal-facts' },
      // Статус уже стоит бейджем справа, исполнитель у дела без рана не записан:
      // «исполнитель: —» — это пустое поле, а не факт.
      data.assignee ? h('span', null, 'исполнитель: ', h('b', null, data.assignee)) : null,
      deal ? h('span', null, 'попытка: ', h('b', null, `${deal.attemptAgeText} (${deal.attemptNote})`)) : null,
      deal ? h('span', null, 'лизинг: ', h('b', null, `${deal.leaseText}`)) : null,
      h('span', null, 'создано: ', h('b', null, fmtClock(data.created_at))),
      deal ? h('span', null, 'последнее действие: ', h('b', null, `${deal.lastActionAgeText} назад`)) : null,
    ),
    // Факты шкалы: попытки по этапам, длительность, вопросы, бюджет, публикация.
    h('div', { class: 'deal-facts' }, ...dealFactSpans(scale)),
    deal && deal.evidence ? h('div', { class: 'why-evidence', style: 'margin-top:10px', text: deal.evidence }) : null,
  )

  // Документы дела: тело из store и спека — разные вещи, и рисовать вместо одной
  // другую нельзя (у дел из `bin/duty create` описания в store нет вовсе).
  // Нет документов — нет колонки: описание забирает ширину, а столбец этапов
  // остаётся один и на всю ширину.
  const docs = docBlocks(data)
  const left = docs.length
    ? h(
        'div',
        { class: 'panel' },
        ...docs.map((doc) =>
          h(
            'div',
            { class: 'panel-doc' },
            h(
              'div',
              { class: 'panel-h', title: doc.path || '' },
              doc.name ? `${doc.label} · ${doc.name}` : doc.label,
              doc.truncated ? ' (обрезана лимитом)' : '',
            ),
            doc.kind === 'spec'
              ? h('pre', { class: 'panel-b doc-src mono' }, doc.text)
              : h('div', { class: 'panel-b' }, mdNode(doc.text)),
          ),
        ),
      )
    : null

  // Этапы и попытки — одним тонким столбцом (320 px) рядом с описанием: шапка
  // попытки раньше повторялась в широкой ленте этапов и в отдельной панели
  // попыток, и страница дела росла на три с половиной экрана.
  const stages = stagesColumn(data.id, data, timeline)

  return h(
    'div',
    null,
    h('div', { style: 'margin-bottom:10px' }, h('a', { href: '#/deal' }, '← к списку дел')),
    head,
    kpiStrip(scale),
    scalePanel(scale),
    left ? h('div', { class: 'deal-cols' }, left, stages) : stages,
  )
}

/* ---------------------------------------------------------------- маршруты */

function parseRoute() {
  const raw = (location.hash || '#/now').replace(/^#/, '')
  const parts = raw.split('/').filter(Boolean)
  if (parts[0] === 'deal') {
    // Адрес дела — один сегмент: `#/deal/<d_hex>`. Два сегмента — прежний адрес
    // `#/deal/<board>/<id>`; он получает отказ, а не открытую наполовину карточку.
    if (parts.length > 2) return { name: 'deal', bad: true, board: decodeURIComponent(parts[1]), id: decodeURIComponent(parts[2]) }
    if (parts[1]) return { name: 'deal', id: decodeURIComponent(parts[1]) }
    return { name: 'deal' }
  }
  if (parts[0] === 'why') return { name: 'why' }
  if (parts[0] === 'health') return { name: 'health' }
  // «Завершённые» — отдельный экран списка: `#/deals` (0.0.7).
  if (parts[0] === 'deals') return { name: 'deals' }
  return { name: 'now' }
}

function render() {
  const body = clear(app)
  if (route.name === 'now') body.append(viewNow())
  else if (route.name === 'why') body.append(viewWhy())
  else if (route.name === 'health') body.append(viewHealth())
  else if (route.name === 'deals') body.append(viewFinished())
  else if (route.bad) body.append(viewDealBadAddress(route.board, route.id))
  else body.append(route.id ? viewDeal(route.id) : viewDealPicker())

  for (const link of document.querySelectorAll('#tabs a')) {
    link.classList.toggle('on', link.dataset.tab === route.name)
  }
  // Счётчик «вопросов + блокировок» — про срез «сейчас»: на других экранах он
  // показывал бы число от прошлого снимка.
  if (humanEl && route.name !== 'now') humanEl.textContent = ''
}

function schedule() {
  clearTimeout(timer)
  // «Здоровье» и «Завершённые» — по журналу за всю историю: они не меняются за
  // секунды, и опрашивать их так же часто, как срез «сейчас», незачем.
  const slow = route.name === 'health' || route.name === 'deals'
  const ms = slow ? HEALTH_REFRESH_MS : REFRESH_MS
  timer = setTimeout(run, ms)
}

function run() {
  if (route.name === 'now') void loadNow()
  else if (route.name === 'why') void loadNow()
  else if (route.name === 'health') void loadHealth()
  else if (route.name === 'deals') void loadFinished()
  else if (route.bad) void loadNow()
  else if (route.id) void loadDeal(route.id)
  else void loadNow()
}

function onRoute() {
  route = parseRoute()
  // Данные, которых нет для нового экрана, не переиспользуем: иначе «Здоровье»
  // покажет старый снимок «сейчас» под своими KPI, а «Завершённые» — вчерашний
  // список под новым заголовком.
  if ((route.name === 'health' && !health.data) || (route.name === 'deals' && !finished.data)) {
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
